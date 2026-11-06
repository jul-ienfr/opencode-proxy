# Écosystème OpenCode free — recherche exhaustive

**Date** : 2026-11-04
**Périmètre** : GitHub (189 dépôts via 18 requêtes), GitLab (64), Codeberg,
Gitee, npm (143 paquets), Docker Hub (102 images), issues du projet, READMEs.
**But** : recenser toutes les familles de contournement du free OpenCode,
comprendre ce qu'elles font, où elles en sont, et ce qui s'applique à six
stations VPN avec quota par IP.

---

## 1. Le gate v2, tel que les projets l'ont reverse-engineered

| Couche | Exigence | Source |
|---|---|---|
| Endpoint | `/inference/v1/{chat/completions,responses,messages}` (v2) | 9router #4845 |
| UA | `opencode/<channel>/<version>/<client>`, ex. `opencode/latest/2.0.26/cli` ; version ≥ 1.17 | 9router #4105 |
| Client | `x-opencode-client: cli` | 9router #4845 |
| Session | `ses_` + 12 hex + 14 base62 (30 caractères), miroir `x-opencode-session-id` | 9router #4105 |
| Affinité | `x-session-affinity`, `X-Session-Id` | capture 2.2.5 |
| Tracing | `b3`, `traceparent` | capture 2.2.5 |
| Projet | `x-opencode-project` = hash ou `global` | capture 2.2.5 |
| Requête | `x-opencode-request` **absent** en v2 | 9router #4845 |
| Auth anonyme | `Authorization: Bearer public` | captures |
| Corps | préambule harness complet + bloc `<env>` avec `Working directory:` | opencode-free-bridge |
| Outils | `read` et `shell` avec schémas **verbatim** ; `tool_choice != none` | 9router #4845 |
| Flux | `stream: true`, `stream_options.include_usage`, `max_completion_tokens` | capture 2.2.5 |
| TLS | ClientHello Bun : 17 cipher suites, 12 extensions, ordre exact | opencode-free-bridge |
| Quota | par IP, journalier, reset à minuit ; **IPv4 et IPv6 = buckets séparés** | dsh-llm-opencode-zen |
| Réseau | IP datacenter/VPN/partagées flagged | issues #56719, #58186 |

Deux niveaux distincts :

- **403 `FreeTierError`** : le gate refuse la *forme* ou l'*origine* de la requête.
- **429 `FreeUsageLimitError`** : la forme est acceptée, mais le quota de l'IP
  (ou de la session) est refusé.

9router (#4845) ajoute un point important : un replay **octet pour octet**
d'une requête du client peut encore recevoir 403, car le gate vérifie aussi
« qui a originaire la requête ». La parade est de lancer le vrai
`opencode serve` et de l'utiliser comme transport. Les autres projets arrivent
au 200 par une reconstruction complète du wire (UA, session canonique, outils,
TLS Bun, préambule).

---

## 2. Les six familles de solutions

### A. Injection d'en-têtes seulement

Réécriture de `User-Agent` et `x-opencode-*`, sans préambule ni outils.

| Projet | Étoiles | Statut | Limite |
|---|---:|---|---|
| `bigdata2211it-web/opencode-free-proxy` | 127 | stale 2026-05-18 | ne gère pas le préambule 2.x |
| `Ritard563/dsh-opencode` | 4 | 2026-08-16 | UA ancien |
| `lutfi-zain/pi-opencode-fix` | 19 | 2026-10-25 | spécifique Pi/omp |
| `xup61069/zen-header-injector` | 0 | 2026-08-16 | minimal |

**Verdict** : insuffisant depuis le durcissement 2.x.

### B. Émulation complète du wire

Rejoue l'identité, le préambule, les outils et la pile TLS.

| Projet | Étoiles | Statut | Ce qu'il fait |
|---|---:|---|---|
| `12errh/zen-proxy` | 130 | actif 2026-11-03 | UA + préambule `agent-preamble.txt` + `agent-tools.json` + Bun, OpenAI/Anthropic, dashboard, fallback |
| `azrinaldi/opencode-free-bridge` | 87 | actif 2026-11-03 | identité auto-actualisée, `<env>`, catalogue live, egress proxy HTTP, Docker, TLS Bun |
| `jcleng/opencode-free-provider` | 16 | actif 2026-11-04 | proxy local, fingerprint client |
| `developingchet/zengate` | 10 | actif 2026-11-01 | keyless, chat/responses/tools/vision |
| `festum/zen-free` | 4 | actif 2026-11-02 | reverse proxy, parle comme le CLI |
| `coferkim/Opencode-Relay` | 4 | actif 2026-11-03 | simulation complète, OpenAI chat |
| `rianilham28/opencode2api` | 3 | actif 2026-10-28 | fingerprint 2.x, quatre dialectes |

**Verdict** : c'est la référence technique. `zen-proxy` et
`opencode-free-bridge` sont les deux implémentations les plus complètes.

### C. Transport par le vrai client

Le proxy lance `opencode serve` et lui délègue l'appel.

| Projet | Étoiles | Statut | Ce qu'il fait |
|---|---:|---|---|
| `Fly143/OpenCode-Zen-free-api` | 22 | actif 2026-10-20 | `opencode serve` → OpenAI `/v1/chat/completions` |
| `Caeser-Zhang/agent-docker-platform` | 1 | 2026-09-30 | conteneurs Docker par utilisateur, `opencode serve`, FastAPI + SSE |
| `Jerry-Wu-GitHub/opencode-server-reverse-proxy` | 1 | 2026-09-14 | reverse proxy du serveur local |
| `TiaraBasori/OpenCode2API` | 425 | actif 2026-10-21 | wrap OpenCode en API OpenAI |

**Verdict** : la voie la plus robuste face au gate d'origine ; toujours limitée
par l'IP. `agent-docker-platform` est le modèle exact d'une plateforme
multi-stations.

### D. Pools de clés / comptes + routage

| Projet | Étoiles | Statut | Ce qu'il fait |
|---|---:|---|---|
| `cheaper-inference/OmniRoute` | 84 451 | actif 2026-11-04 | gateway, 375 providers, 150+ free, 1 400+ modèles, fallback quota-aware |
| `Alishahryar1/free-claude-code` | 66 333 | actif 2026-11-04 | 1,3 Md de tokens free, multi-harnais |
| `decolua/9router` | 31 933 | actif 2026-10-28 | routeur free, 40+ providers, fallback, adapter OpenCode |
| `lidge-jun/opencodex` | 18 202 | actif 2026-11-04 | proxy universel Codex/Claude Code |
| `Soju06/codex-lb` | 3 416 | actif 2026-11-03 | load-balancer de comptes Codex |
| `jasonxu114514/opencode2api` | 1 407 | actif 2026-10-29 | Go, pool de clés/proxys, routage, WebUI |
| `routatic/proxy` | 981 | actif 2026-10-30 | Claude Code → Go/Zen/Bedrock |
| `6Kmfi6HP/opencode2api` | 148 | actif 2026-10-23 | Zen/Go → Codex/Claude |
| `kirafishy/OCFreeRelay` | 33 | actif 2026-08-21 | pool multi-clés/comptes, affinité sticky, pool de proxys |
| `xiaozhe7772222/dsh-opencode-zen` | 30 | actif 2026-08-20 | plugin DSH, 6 modèles, rotation multi-clés |

**Verdict** : fiable pour du volume, mais repose sur des comptes/clés.

### E. Rotation d'IP / pools d'egress

| Projet | Étoiles | Statut | Ce qu'il fait |
|---|---:|---|---|
| `ebouther/opencode-unlimited-proxy` | 48 | actif 2026-10-27 | pool d'IP d'egress dédupliquées, contourne 403, sans clé |
| `alztrk/opencode-ip-rotator` | 32 | 2026-07-27 | rotation Cloudflare WARP + SQLite |
| `jiuwangka/opencode-ip-mihomo` | 7 | 2026-08-23 | mihomo, abonnements, pool HTTP/HTTPS/SOCKS5, IP unique par cycle |
| `gkhantyln/opencode-warp` | 1 | 2026-11-02 | WARP, rafraîchissement d'IP, protection 429 |
| `GlariaLuminous/dsh-zen-desktop` | 2 | 2026-10-24 | bypass 429 Windows, changement de réseau |

**Verdict** : la seule famille qui attaque le vrai facteur limitant sans compte.
`opencode-unlimited-proxy` est le plus proche de ton besoin (pool d'IP, 403).

### F. Fallback local / providers

| Solution | Compte | Quota | Qualité |
|---|---|---|---|
| Ollama / LM Studio / vLLM | non | aucun | selon modèle et matériel |
| OpenRouter free, Gemini free, Groq | oui | selon compte | variable |

---

## 3. Tableau de synthèse (les plus pertinents)

| Projet | Famille | Étoiles | MAJ | Compte | IP | OpenAI | Anthropic | Statut |
|---|---|---:|---|---|---|---|---|---|
| `OmniRoute` | D | 84 451 | 11-04 | oui | pool | oui | oui | actif |
| `free-claude-code` | D | 66 333 | 11-04 | oui | pool | oui | oui | actif |
| `9router` | D+C | 31 933 | 10-28 | oui | pool | oui | oui | actif |
| `opencodex` | D | 18 202 | 11-04 | oui | pool | oui | oui | actif |
| `opencode2api` (jasonxu) | D | 1 407 | 10-29 | oui | pool | oui | oui | actif |
| `routatic/proxy` | D | 981 | 10-30 | oui | pool | oui | oui | actif |
| `OpenCode2API` | C | 425 | 10-21 | non | direct | oui | non | actif |
| `zen-gate` | B | 289 | 11-02 | non | direct | oui | oui | actif |
| `zen-proxy` | B | 130 | 11-03 | non | direct | oui | oui | actif |
| `opencode-free-proxy` | A | 127 | 05-18 | non | direct | oui | oui | stale |
| `opencode-free-bridge` | B | 87 | 11-03 | non | **egress** | oui | non | actif |
| `opencode-llm-proxy` | C | 86 | 10-29 | non | direct | oui | oui | actif |
| `opencode-unlimited-proxy` | E | 48 | 10-27 | non | **pool** | oui | non | actif |
| `opencode-ip-rotator` | E | 32 | 07-27 | non | **WARP** | oui | non | peu actif |
| `OCFreeRelay` | D+E | 33 | 08-21 | oui | **pool** | oui | non | actif |
| `Fly143` | C | 22 | 10-20 | non | direct | oui | non | actif |
| `jcleng/opencode-free-provider` | B | 16 | 11-04 | non | direct | oui | non | actif |
| `zengate` | B | 10 | 11-01 | non | direct | oui | non | actif |
| `dsh-opencode-zen` | D | 30 | 08-20 | partiel | direct | oui | non | actif |
| `agent-docker-platform` | C | 1 | 09-30 | non | **Docker** | oui | non | prototype |

---

## 4. Ce que chaque famille apprend pour six stations

1. **Le format est nécessaire mais pas suffisant.** Les projets B et C
   obtiennent 200 sur une IP propre ; sur une IP flagged, ils prennent 403/429.
2. **Le quota est journalier par IP** (reset minuit) et **IPv4/IPv6 sont
   séparés**. Six stations = six buckets, pas un bucket partagé.
3. **La session canonique** (`ses_` + 12 hex + 14 base62) est obligatoire ; les
   UUID `ses_<uuid>` sont refusés en 403.
4. **Le TLS Bun** est inspecté ; un stack Node avec les mêmes en-têtes échoue.
5. **Le gate d'origine** peut refuser un replay parfait ; le vrai
   `opencode serve` est la parade (9router #4845, Fly143).
6. **Le bloc `<env>` avec `Working directory:`** est requis ; sans lui, 429 même
   avec le bon UA et les bons outils.

---

## 5. Scénarios pour ton architecture

| Priorité | Scénario | Compte | Fiabilité | Effort |
|---|---|---|---|---|
| 1 | Stations + pool d'egress résidentiel/mobile + wire 2.x | non | moyenne-bonne | moyen |
| 2 | Stations + IPv6 (buckets séparés) + wire 2.x | non | moyenne | moyen |
| 3 | Vrai client `opencode serve` par station + IP propre | non | bonne | moyen |
| 4 | WARP rotator + wire 2.x | non | faible-moyenne | faible |
| 5 | Pool de clés/comptes (`opencode2api`, `OCFreeRelay`) | oui | bonne | élevé |
| 6 | Fallback local (Ollama) | non | garantie | faible |

### Correctifs concrets à appliquer au proxy

- Générer des sessions `ses_` + 12 hex + 14 base62.
- Passer `x-opencode-client` à `cli` pour le chemin anonyme.
- UA `opencode/latest/2.2.5/cli`.
- Retirer `x-opencode-request` (v2).
- Garder `b3`, `traceparent`, `x-session-affinity`, `X-Session-Id`.
- Vérifier le bloc `<env>` et les schémas `read`/`shell` verbatim.
- Tenir un ledger `(station, egress_ip, jour)` avec reset minuit.
- Tester IPv6 par station ; un préfixe = plusieurs buckets.
- Ajouter un egress proxy HTTP par station (`opencode-free-bridge` montre le
  pattern) et un pool résidentiel/mobile.

---

## 6. Risques

- Projets non officiels ; usage intensif ciblable.
- Modèles free rotatifs, endpoints qui changent (`/zen` → `/inference`).
- Clés/comptes en pool suspendables.
- WARP/datacenter souvent flagged comme les stations.
- Le vrai client reste la voie la moins risquée, mais ne crée pas de quota.
