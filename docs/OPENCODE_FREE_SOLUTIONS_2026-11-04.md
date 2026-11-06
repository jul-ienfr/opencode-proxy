# OpenCode free — solutions de contournement existantes

**Date** : 2026-11-04 | **Objet** : comparer les projets qui rendent les modèles
free OpenCode utilisables hors du client officiel, et voir lesquels s'appliquent
à six stations VPN avec quota par IP.

---

## 1. Ce que le gate vérifie réellement

D'après les captures et les issues du projet :

| Couche | Exigence | Source |
|---|---|---|
| Client | `User-Agent` commençant par `opencode/` | issue #42029 |
| Identité | `x-opencode-client`, `x-opencode-project`, `x-opencode-session`, `x-opencode-session-id`, `x-session-affinity`, `x-session-id` | captures 2.2.5 |
| Tracing | `b3`, `traceparent` | capture 2.2.5 |
| Corps | préambule harness complet + bloc `<env>` avec `Working directory:` | opencode-free-bridge |
| Outils | manifeste d'outils réaliste (pas `tools: []`, pas un stub `read`) | zen-proxy |
| Flux | `stream: true`, `stream_options.include_usage`, `max_completion_tokens` | capture 2.2.5 |
| TLS | pile Bun ; un stack Node avec les mêmes en-têtes prend 429 | zen-proxy |
| Quota | par IP, journalier, reset à minuit ; IPv4 et IPv6 ont des quotas séparés | dsh-llm-opencode-zen |
| Réseau | IP datacenter/VPN partagées flagged | issues #56719, #58186 |

Conclusion : le format est une condition d'**acceptation** ; le quota est une
condition de **disponibilité**, indexée sur l'IP (et le couple compte/workspace
quand un credential est présent).

---

## 2. Les familles de solutions

### A. Injection d'en-têtes seulement

Le proxy réécrit `User-Agent` et `x-opencode-*`, sans reconstruire le préambule.

| Projet | Étoiles | Mis à jour | Ce qu'il fait | Limite |
|---|---:|---|---|---|
| `bigdata2211it-web/opencode-free-proxy` | 127 | 2026-05-18 | OpenAI + Anthropic, injecte les en-têtes `x-opencode-*` | ancien (mai), ne gère pas le préambule 2.x |
| `Ritard563/dsh-opencode` | 4 | 2026-08-16 | proxy Node de 130 lignes, UA `opencode/1.18.18…` | UA ancien, pas de préambule |
| `lutfi-zain/pi-opencode-fix` | 19 | 2026-10-25 | extension Pi/omp, injecte les en-têtes CLI | spécifique Pi/omp |

**Verdict** : insuffisant depuis le durcissement 2.x ; le gate refuse un corps
sans préambule et sans outils, même avec les bons en-têtes.

### B. Émulation complète du wire

Le proxy rejoue l'identité réseau, le préambule, les outils et la pile TLS.

| Projet | Étoiles | Mis à jour | Ce qu'il fait | Points forts |
|---|---:|---|---|---|
| `12errh/zen-proxy` | 130 | 2026-11-03 | OpenAI + Anthropic, UA, préambule `agent-preamble.txt`, outils `agent-tools.json`, Bun obligatoire, dashboard, fallback modèle | le plus complet sur le gate ; actif ; MIT |
| `azrinaldi/opencode-free-bridge` | 87 | 2026-11-03 | OpenAI + Responses, identité auto-actualisée, catalogue live, bloc `<env>`, proxy egress HTTP/HTTPS, Docker | gère le `429` de préambule ; egress proxy intégré ; actif |

**Verdict** : c'est la référence technique. `zen-proxy` est le plus strict sur
le TLS Bun ; `opencode-free-bridge` est le plus pratique pour brancher un pool
d'IP (proxy egress) et le bloc `<env>`.

### C. Transport par le vrai client

| Projet | Étoiles | Mis à jour | Ce qu'il fait | Limite |
|---|---:|---|---|---|
| `Fly143/OpenCode-Zen-free-api` | 22 | 2026-10-20 | lance `opencode serve` et expose `/v1/chat/completions` | nécessite le binaire opencode ; 23 Ko d'outils envoyés ; région-gated |

**Verdict** : ce n'est pas de l'impersonation, c'est le vrai client. Plus
robuste face au gate, mais toujours limité par l'IP de sortie.

### D. Pools de clés / comptes + routage

| Projet | Étoiles | Mis à jour | Ce qu'il fait | Compte requis |
|---|---:|---|---|---|
| `jasonxu114514/opencode2api` | 1407 | 2026-10-29 | proxy Go, pools de clés et de proxys, routage, WebUI, OpenAI + Anthropic | oui (Zen/Go) |
| `routatic/proxy` | 981 | 2026-10-30 | Claude Code vers OpenCode Go/Zen/Bedrock, sélection auto | oui |
| `6Kmfi6HP/opencode2api` | 148 | 2026-10-23 | Zen/Go vers Codex/Claude Code | oui |
| `kirafishy/OCFreeRelay` | 33 | 2026-08-21 | pool multi-clés/comptes, affinité sticky, rotation sur 429, pool de proxys | oui |
| `xiaozhe7772222/dsh-opencode-zen` | 30 | 2026-08-20 | plugin DSH, 6 modèles free, rotation multi-clés | non pour l'anonyme, oui pour le pool |

**Verdict** : c'est la voie la plus fiable pour du volume, mais elle repose sur
des comptes/clés. Incompatible avec « free sans mon compte ».

### E. Rotation d'IP

| Projet | Étoiles | Mis à jour | Ce qu'il fait | Limite |
|---|---:|---|---|---|
| `alztrk/opencode-ip-rotator` | 32 | 2026-07-27 | rotation Cloudflare WARP + proxy, SQLite d'usage | WARP peut être flagged ; projet peu actif |
| `OCFreeRelay` (pool) | 33 | 2026-08-21 | pool de proxys HTTP/SOCKS5, binding par worker | comptes/clés |
| `opencode-free-bridge` (egress) | 87 | 2026-11-03 | proxy egress HTTP/HTTPS par requête | HTTP/HTTPS seulement, pas SOCKS |
| `9Router` (cité par le bridge) | — | — | plusieurs hôtes = plusieurs IP, failover sur 429 | infrastructure multi-machine |

**Verdict** : c'est la seule famille qui attaque le vrai facteur limitant
(quota par IP) sans compte. Elle demande un pool d'IP propres.

### F. Fallback local

| Solution | Compte | Quota | Qualité | Mise en place |
|---|---|---|---|---|
| Ollama / LM Studio / vLLM | aucun | aucun | dépend du modèle et du CPU/GPU | locale |

**Verdict** : la seule source garantie, permanente, sans compte. Elle ne fournit
pas les modèles OpenCode, mais elle rend le proxy servable même quand le free
est refusé.

### G. Providers alternatifs

OpenRouter free, Gemini free, Groq, Cerebras, Mistral… : clé et compte requis,
donc exclus par la contrainte « sans mon compte ». Utile seulement comme
fallback si un compte dédié est acceptable.

---

## 3. Tableau de synthèse

| Projet | Approche | Compte | IP | OpenAI | Anthropic | Streaming | Outils | Statut |
|---|---|---|---|---|---|---|---|---|
| `zen-proxy` | wire complet + Bun | non | au choix | oui | oui | oui | oui | actif |
| `opencode-free-bridge` | wire + `<env>` + egress | non | proxy HTTP | oui | non | oui | oui | actif |
| `opencode-free-proxy` | en-têtes | non | direct | oui | oui | oui | oui | stale |
| `Fly143` | vrai client | non | direct | oui | non | oui | oui | actif |
| `opencode2api` (jasonxu) | clés + proxys | oui | pool | oui | oui | oui | oui | actif |
| `routatic/proxy` | clés + routage | oui | pool | oui | oui | oui | oui | actif |
| `OCFreeRelay` | clés + proxys | oui | pool | oui | non | oui | oui | actif |
| `opencode-ip-rotator` | WARP | non | rotation | oui | non | oui | ? | peu actif |
| Ollama | local | non | local | oui | non | oui | selon modèle | stable |

---

## 4. Ce qui s'applique à six stations VPN

Les stations sont des IP datacenter. Le quota est par IP, et les IP datacenter
sont précisément celles que le gate signale. Trois conséquences :

1. **Le format est déjà bon** dans le proxy (capture 2.2.5, 13 outils,
   `max_completion_tokens`, Bun TLS). Ce n'est pas le levier.
2. **Chaque station a son propre quota** ; il faut un ledger par
   `(station, egress_ip)` et ne pas réutiliser une IP flagged pour une autre
   station.
3. **Il faut ajouter des IP** : résidentiel/mobile, WARP, ou IPv6. Le projet
   `dsh-llm-opencode-zen` note que IPv4 et IPv6 ont des quotas séparés ; activer
   IPv6 sur les stations peut doubler les buckets sans compte.

### Scénarios classés

| Priorité | Scénario | Compte | Fiabilité | Effort |
|---|---|---|---|---|
| 1 | Stations + pool d'egress résidentiel/mobile + wire 2.2.5 | non | moyenne à bonne | moyen |
| 2 | Stations + IPv6 (quotas séparés) + wire 2.2.5 | non | moyenne | moyen |
| 3 | WARP rotator + wire 2.2.5 | non | faible à moyenne | faible |
| 4 | Vrai client (`Fly143`) sur IP propre | non | bonne | moyen |
| 5 | Pool de clés/comptes (`opencode2api`/`OCFreeRelay`) | oui | bonne | élevé |
| 6 | Fallback local (Ollama) | non | garantie | faible |

---

## 5. Recommandation

- **Court terme** : garder le wire 2.2.5 déjà en place, ajouter un **pool
  d'egress** (résidentiel/mobile ou IPv6) et un **ledger de quota par IP**.
  `opencode-free-bridge` est la meilleure référence pour l'egress proxy ;
  `zen-proxy` pour le wire Bun.
- **Sans IP propre** : le free restera 429. Aucun en-tête ne compense une IP
  flagged.
- **Garantie de service** : fallback local Ollama, même dégradé.
- **Si un compte dédié devient acceptable** : `opencode2api` (1407★) est le
  plus mature pour les pools de clés/proxys.

## 6. Risques

- Ces projets sont non officiels ; l'usage intensif peut être ciblé.
- Les modèles free tournent et peuvent disparaître.
- Les clés/comptes dans un pool peuvent être suspendus.
- Les IP WARP/datacenter peuvent être flagged comme les stations.
- Le vrai client (`Fly143`) reste la voie la moins risquée vis-à-vis du gate,
  mais elle ne crée pas de quota.
