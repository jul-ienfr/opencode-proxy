# Audit consolidé — dépôts tiers et jambe free

**Date**: 2026-11-06  
**Périmètre**: neuf dépôts fournis par l'opérateur, le proxy `opencode-proxy-one`,
les correctifs appliqués pendant l'audit, l'état runtime et les divergences
restantes.  
**Paquet Spec Kit**: [`specs/004-repo-parity-audit/`](../specs/004-repo-parity-audit/spec.md)  
**Recherche détaillée**: [`specs/004-repo-parity-audit/research.md`](../specs/004-repo-parity-audit/research.md)  
**Mesures**: [`specs/004-repo-parity-audit/measurements.md`](../specs/004-repo-parity-audit/measurements.md)

---

## 1. Résumé exécutif

Les neuf dépôts convergent sur un noyau de gate : **quatre conditions de
forme** (UA `opencode/<version>` ≥ 1.18.0, session `ses_` canonique `…ffe`,
un outil nommé exactement `read`, `stream: true`) plus le bloc `<env>` avec
`Working directory:`, et un TLS de type Bun/BoringSSL.

Le quota est un axe séparé : il est keyé par **bloc d'egress** (`/64` IPv6,
`/32` IPv4), avec reset **00:00 UTC**. WARP IPv4 est une NAT partagée, donc
la rotation IPv4 n'ajoute pas de headroom ; un `/64` IPv6 routé le ferait.

Le proxy passe le gate sur une IP fraîche, mais il reste trois écarts
importants :

1. **Identité hybride** : corps et ordre 2.2.5, UA/project/session 1.19.5.
2. **Session stable par conversation** : contredit la mesure afree de
   shadow-ban, non tranchée.
3. **Capacité** : **trois blocs dans le pool** — direct IPv4, WARP IPv4 et
   WARP IPv6 `2a09:bac5:32d4:be::/64` — avec rotation 6 × 200. Les stations
   VPN saines sortent toujours sur des IP 429 ; l'hôte n'a pas d'IPv6
   globale, mais le tunnel WARP en fournit une.

Quatre correctifs ont été appliqués pendant l'audit : `properties` non vides,
garde `<env>` (premier couple), retrait de `tool_choice: none`, et **plafond de
cooldown 20 min réellement appliqué à la station** (pas seulement au ledger).

---

## 2. Méthode et qualité de preuve

Pour chaque dépôt : README + fichiers de code critiques (gate, identité,
transport, sessions, endpoints), puis confrontation aux mesures live du proxy.
Chaque fait est classé :

- **[M]** mesuré par le dépôt (probe documenté) ;
- **[R]** reproduit par nous le 2026-11-06 ;
- **[D]** documenté seulement ;
- **[I]** inféré du code.

Les faits [M] ne sont pas des contrats permanents : le gate et les modèles
changent. Les faits [R] sont datés et reproductibles par `quickstart.md`.

---

## 3. Verdict par dépôt

| Dépôt | Langage | Apport principal | Verdict |
|---|---|---|---|
| `xljiulang/opencode-proxy` | Rust | Gate 7 outils, TLS 2.2.5 exact, alias `fn_*` | **Référence wire** |
| `rianilham28/opencode2api` | Rust | `<env>` premier couple, `tool_choice`, trim `[DONE]`, lanes | **Le plus précis** |
| `xd88dx/afree-proxy` | Go | Quota ~200/5 h par IP, RA=minuit, session par appel | **Référence quota** |
| `alztrk/opencode-ip-rotator` | Python | WARP, schéma `read`, gate minimal | **Piste capacité** |
| `beastbroak30/opencode-zen-proxy` | Python | Backend `opencode serve`, health-check modèles | **Repli architectural** |
| `nahdd123uosackr/opencode_proxy` | JS/Go | ClientHello Bun exact, pas de keep-alive après 429 | **Référence TLS** |
| `xueweijian/opencode-edge-proxy` | JS | Edge US, geo-gate, patch SSE `finish`/`[DONE]` | **Référence geo/SSE** |
| `developingchet/zengate` | Node | Gateway sur vrai `opencode serve`, quotas structurés | **Repli architectural** |
| `ebouther/opencode-unlimited-proxy` | Python | 4 gates mesurés, quota par bloc `/64`, WARP IPv6 | **Référence gate/quota** |

Détail, fichiers lus et citations : `specs/004-repo-parity-audit/research.md`.

---

## 4. État actuel

| Élément | Valeur |
|---|---|
| Proxy | PID 16200, `0.0.0.0:4000` |
| Mode | `proxy_mode: socks5` (WARP IPv4 + IPv6 + direct) |
| Blocs | **3 dans le pool** : direct `37.69.230.71`, WARP IPv4 `104.28.230.202`, WARP IPv6 `2a09:bac5:32d4:be::/64` |
| Stations VPN | 3 healthy (`-2`, `-3`, `-5`) mais IP **429** ; 2 unhealthy (`-4`, `-6`). Superviseur arrêté. |
| IP directe | `37.69.230.71` |
| WARP | `warp-test` healthy, SOCKS5 `127.0.0.1:11080`, IP `104.28.230.202` |
| `free.cooldown_cap_s` | 1200 |
| `ip_quota.ledger_max_block_s` | 1200 |
| Suite ciblée | exit 0 (~148 tests collectés) |
| Commit | **aucun** — le `git diff` est la seule trace |

Tests live après correctifs :

| Test | Statut |
|---|---|
| `ping` | 200 `pong` |
| `tool_choice: none` + `<env></env>` invalide + `read` | 200 |
| `tool_choice: none` + `<env>` valide | 200 `Bonjour.` |
| Code réel (`mul`) | 200 |
| Stream SSE | 200, `[DONE]`, frame `cost` |
| WARP probe 2.2.5 | 200, 5,6 s |

---

## 5. Journal des changements

### 5.1 Cette session (2026-11-06)

| # | Fichier | Fonction | Avant | Après | Raison | Test |
|---|---|---|---|---|---|---|
| C-01 | `opencode.py` | `_free_official_placeholder_defs` | `properties: {}` | `properties: {path}` | un schéma vide ne compte pas dans le gate 7 outils | `test_official_placeholders_are_minimal_but_keep_names` |
| C-02 | `opencode.py` | `_free_env_block_valid` (nouvelle) | scan de tous les couples | premier couple seulement ; `<env x="1">` invalide | un premier couple vide empoisonne la requête | `test_env_guard_*` |
| C-03 | `opencode.py` | `_free_ensure_env_block` (nouvelle) | — | insert un bloc valide en tête | `<env>` est un credential de corps | `test_env_guard_adds_block_*` |
| C-04 | `opencode.py` | `_free_wire_body` | garde absente sur le retour rapide | `need_env` vérifié avant le retour | un corps « signature OK » sans `<env>` partait tel quel | `test_env_guard_*` |
| C-05 | `opencode.py` | `_free_wire_body` | `none` retiré seulement si la grille était injectée | `none` retiré de toute copie free | le gate refuse `none` | `test_tool_choice_none_is_dropped_from_free_wire` |
| C-06 | `opencode.py` | `_free_cooldown_cap_seconds` (nouvelle) | — | `free.cooldown_cap_s`, défaut 1200 s | borner le blocage station | `test_cooldown_cap_*` |
| C-07 | `opencode.py` | `_free_429_cooldown_seconds` | header honoré jusqu'à 24 h | `min(brut, cap)` | RA = minuit UTC, pas recovery | `test_cooldown_*` |
| C-08 | `opencode.py` | appelant 429 | `trial_dead` 6 h non plafonné | `min(max(...), cap)` | sans header, l'IP restait parkée 6 h | `test_trial_dead_quota` |
| C-09 | `free/quota_ledger.py` | `max_block_s`, `note_result` | `reset_at = ts + RA` | `ts + min(RA, max_block_s)` | re-test sans attendre minuit | `test_retry_after_cap_reprobes_before_midnight` |
| C-10 | `free/pool.py` | construction du ledger | — | passe `ledger_max_block_s` | activer le plafond en production | tests quota |
| C-11 | `config.yaml` | config | — | `cooldown_cap_s: 1200`, `ledger_max_block_s: 1200` | valeurs opérationnelles | — |
| C-12 | `tests/` | 4 fichiers | attentes historiques | nouveaux tests et fixtures `<env>` | verrouiller les correctifs | exit 0 |
| C-13 | `docs/` | 2 fichiers + ce document | — | analyse des dépôts et audit | traçabilité | — |
| C-14 | `config.yaml` | `ip_registry.dns.fallback_order` | `[dot, plain]` | `[dot]` | `plain` casse le DNS gluetun sur ce réseau | stations 2/3 healthy |
| C-15 | `config.yaml` | `fast_discovery_transport` | `openvpn-udp` | `wireguard` | OpenVPN `AUTH_FAILED`, WG authentifie | témoin WG healthy |
| C-16 | `logs/compose-hostname-*.yml` | override station | `plain` + OpenVPN | `dot` + WG + MTU 1419 | recipe station validé | témoin healthy |
| C-17 | `scripts/fresh_chain_supervisor.py` | `_preferred_transport` | `auto` conservé | `auto` → wireguard | OpenVPN `AUTH_FAILED`, WG authentifie | child relancé en WG (au prochain start) |
| C-18 | `scripts/direct_socks5.py` | nouveau | — | pont SOCKS5 direct | deuxième egress sans code pool | 4 × 200 en rotation |
| C-19 | `config.yaml` | `proxy_mode` / `socks5_proxies` | `direct` / `[]` | `socks5` / WARP + direct | deux stations explicites | rotation + stream |
| C-20 | `scripts/start_direct_socks5.ps1` + `contracts/egress.md` | nouveau | — | démarrage idempotent + contrat | reproductibilité | — |
| C-21 | `free/pool.py` | `Socks5Endpoint` | pid `host:port` | `label`, `ipv6`, `egress_ip` + sonde IPv6 | station IPv6 distincte | 3 passed |
| C-22 | `opencode.py` | `_get_pooled_curl_session`, `_do_free_request_curl_cffi` | session unique | `curl_options` + variant `-ipv6` | forcer IPv6 sans polluer IPv4 | 6 × 200 |
| C-23 | `config.yaml` | `socks5_proxies` | 2 entrées | 3 entrées labellisées | exposer le `/64` | rotation live |

### 5.2 Travaux antérieurs (spec 003, non commités)

| Zone | Changement |
|---|---|
| Ordre 2.2.5 | `_OPENCODE_HEADER_ORDER_225` (16 en-têtes) + passage des vrais headers |
| Project | `x-opencode-project: global` n'est plus remplacé par le hash du dépôt |
| Trace | trace ID stable par session |
| Rotation | `ROTATION_VERSIONS=("1.19.5",)` + migration |
| Quota | `FreeIPPool.clear_model_global()` + endpoint de purge |
| Credentials | `scripts/refresh_nord_credentials.py` |
| Probe | `scripts/test_free_egress.py --client cli|desktop --project --dry-run` |
| E2E | `tests/test_official_wire_e2e.py` |

---

## 6. Matrice de divergences

### 6.1 Identité et wire

| Dimension | xljiulang | rianilham | afree | alztrk | beastbroak | nahdd | **Ours** |
|---|---|---|---|---|---|---|---|
| UA | `opencode/latest/2.2.5/cli` | `opencode/latest/2.2.2/cli` | `opencode/latest/2.1.4/cli` | `opencode/latest/2.1.4/cli` | `1.18.35` | `2.1.4` / Bun | **`opencode/1.19.5 …`** |
| TLS | BoringSSL wreq, 2.2.5 exact | BoringSSL pin Bun | non documenté | TLS 1.2 OpenSSL | httpx | uTLS Bun exact | **curl_cffi 1.19.5, JA3 1523504b** |
| En-têtes | 14 + `org-id` | 10 + tracing | minimal | minimal | minimal | forward | **16 exacts** |
| `<env>` | oui, premier system | oui, premier couple | oui (gate tools) | oui, préfixe strict | oui | oui | **oui + garde** |
| `read` | 1 des 7 | seul requis | via gate tools | seul requis | `ZEN_TOOLS` | canonique | **13 + `read`** |
| Outils | 7 gate + alias | caller + `read` | non documenté | `read` + caller | 12 | non documenté | **13 + 9 client** |
| Session | par thread/process | par process | **par appel** | par appel | par user 30 min | par session | **par (IP, conversation) 30 min** |
| Trace | par requête | process + span | non documenté | non documenté | non documenté | non documenté | **par session** |
| Project | Git dérivé | 40 hex aléatoire | non documenté | non documenté | non documenté | non documenté | **par IP / `global`** |
| Budget | 32k si absent | renomme | non documenté | non documenté | non documenté | non documenté | **clamp 32k** |
| `tool_choice` | non forcé | `none` retiré | non documenté | non documenté | non documenté | non documenté | **`none` retiré** |

### 6.2 Quota et opérations

| Dimension | xljiulang | rianilham | afree | alztrk | beastbroak | nahdd | **Ours** |
|---|---|---|---|---|---|---|---|
| Quota | par requête | par clé | **~200 req/5 h par IP** | par IP | par modèle | par IP | **(IP, jour, bucket), profil 500** |
| 429 | journalisé | cooldown clé 120 s | passthrough + 20 min | rotation WARP | passthrough | ferme la connexion | **passthrough + 20 min** |
| Egress | direct/proxy | lanes collantes | pool + binding | WARP + proxies | direct/`serve` | pont uTLS | **direct + VPN + WARP testé** |
| Rotation | aucune | TTL 80 % | stratégie pool | registration WARP | aucune | aucune | **stations VPN + allocateur** |
| Endpoint | Responses passthrough | catalogue `api` + muse | **auto-learn** | chat/responses | explicite | non documenté | **`free_endpoint_for` muse/spark/jev** |
| Erreurs | typées | typées | 429 passthrough | typées | 502 wrapper | non documenté | **enveloppes OpenAI/Anthropic** |
| Streaming | oui | oui | oui | oui | oui | oui | **oui, amont forcé + repli** |
| Keep-alive | pool wreq | lanes | non documenté | non documenté | httpx | **désactivé** | **pool curl, réutilisé après 429** |

### 6.3 Statut des divergences

| Dimension | Statut | Référence |
|---|---|---|
| UA / identité 2.2.5 | **ouvert** | `T-401` |
| TLS | aligné par JA3, implémentation différente | — |
| En-têtes / ordre | aligné (superset) | `contracts/free-wire.md` |
| `<env>` | aligné + corrigé | `T-101`, `T-102` |
| `read` / nombre d'outils | **contradiction non tranchée**, notre grille satisfait les deux | `T-413` |
| Session | **ouvert / risqué** | `T-402` |
| Trace | cosmétique | `T-411` |
| Project | intentionnel (pas de fuite) | — |
| Budget / `tool_choice` | aligné après correctif | `T-103` |
| Quota | **à calibrer** | `T-406` |
| 429 | aligné afree | `contracts/cooldown.md` |
| Egress / six stations | **ouvert** | `T-403`, `T-404` |
| Endpoint par modèle | aligné (code), à vérifier live | `T-410` |
| Keep-alive après 429 | **risque non mesuré** | `T-408` |
| Frame `cost` | **risque client strict** | `T-407` |
| Alias d'outils | **non nécessaire sur le modèle actuel** | `T-409` |

---

## 7. Risques

| Risque | Probabilité | Impact | Mitigation |
|---|---|---|---|
| L'hybride UA 1.19.5 + corps 2.2.5 devient détectable | moyenne | gate 403/429 | `T-401` : profil 2.2.5 complet |
| Le shadow-ban de session coûte l'IP directe | inconnue | perte 1 jour | `T-402` sur IP sacrifiable |
| Le plafond 20 min re-teste une IP encore épuisée | élevée | quelques 429 de sonde | ledger rebloque, coût borné |
| WARP non rotatif | certaine sur cet hôte | une seule station WARP | `T-403` autres hôtes |
| WARP IPv4 = NAT partagée | élevée | headroom instable | direct en repli, modèle par bloc |
| Hôte sans IPv6 globale | certaine | plafond 2–3 blocs | `T-416` hôte Linux `/64` |
| Le pont direct s'arrête | moyenne | perte du round-robin | `T-414` démarrage auto |
| Six stations VPN : 3 healthy mais IP 429, superviseur en OpenVPN `AUTH_FAILED` | certaine | objectif 6 stations | `T-404` forcer WireGuard au claim + IP fraîches |
| Rien n'est commité | certaine | perte de l'état | `T-405` commit |
| `quota_per_ip` surestimé | moyenne | rotation tardive | `T-406` calibrage |
| Frame `cost` rejetée par un client strict | faible | erreur de stream | `T-407` test SDK |

---

## 7.1 Deuxième lot de dépôts (2026-11-06)

### Le gate est 4 conditions, pas 7 outils

`ebouther/opencode-unlimited-proxy` a mesuré une variable à la fois
(2026-10-16) :

| Gate | Règle | Notre état |
|---|---|---|
| 1. UA | `opencode/<version>` ≥ 1.18.0 ; `1.17.2` → 426 ; tiers → 403 | UA 1.19.5 → OK |
| 2. Session | `x-opencode-session` exact, `ses_` + 12 hex + 14 base62, **`…ffe`** et horloge récente | mint conforme + test |
| 3. `read` | un seul outil nommé exactement `read` ; compte/noms sans effet | 13 outils = sur-ensemble |
| 4. Stream | `stream:false` → 403 | forcé |
| 5. `<env>` | `Working directory:` requis (mesure 2026-10-25) | garde |

**Conséquence** : `T-413` est clos. `read` seul suffit ; la grille à 13 outils
reste un sur-ensemble sûr. `x-opencode-session-id` seul ne suffit pas ; notre
quartet 2.2.5 contient le bon header.

### Le quota est par bloc, pas par IP

- Compteur keyé par **bloc d'egress** : `/64` IPv6, `/32` IPv4.
- Reset **00:00 UTC** (cohérent avec nos `Retry-After` ~61–68k s).
- WARP IPv4 = une NAT partagée : la rotation IPv4 n'ajoute pas de headroom.
- WARP IPv6 avec un `/64` routé : chaque exit lie sa source et obtient son
  propre bloc. C'est le seul multiplicateur propre.
- Notre `server_ip_key` tronque déjà l'IPv6 au `/64` ; le ledger est aligné.

**Limite de cet hôte** : `Get-NetIPAddress` ne montre qu'une IPv6 link-local
(`fe80::`), aucune globale. La voie `/64` WARP est donc **indisponible sur ce
Windows**. Pour six blocs distincts, il faut un hôte Linux avec un `/64` routé,
ou six blocs IPv4 distincts (proxies résidentiels, autres fournisseurs).

### Geo-gate et SSE

- `xueweijian` : certains modèles renvoient `not available in your country` ;
  une sortie US (Wasmer Edge) passe. Aucun en-tête ne corrige. Le catalogue
  free actuel est chat-native ; le geo-gate ne concerne pas `deepseek`.
- SSE : `xueweijian` complète `finish_reason`/`[DONE]` **seulement s'ils
  manquent**, jamais sur `/responses`. Notre proxy fait déjà mieux : il ajoute
  la clôture manquante et transforme une vraie troncature en erreur, pas en
  faux `stop`.

### zengate

Gateway Node qui lance le vrai `opencode serve` et traduit OpenAI ↔ sessions.
Apports : classification quota structurée (`upstream_quota_exhausted` vs
`upstream_rate_limited`), `Retry-After` multi-heures préservé, profil strict
qui confirme que le prompt/tools du backend sont la signature du free. Non
adopté : architecture lourde, session par requête, tools émulés dans le prompt.

**Complément** : l'analyse des 12 dépôts « OpenCode Unlimited » (dont
`ebouther`, `Tazmyan`, `ngynlaam`, `Nirusaki`, `hansputera`) est dans
[`docs/REPOS_12_UNLIMITED_2026-11-06.md`](REPOS_12_UNLIMITED_2026-11-06.md).
Elle confirme le quota par bloc, documente les faux leviers (device ID, session)
et les limites de la rotation WARP.

## 7.2 Roadmap capacité — six blocs, pas six sessions

Le quota est par bloc ; le nombre de stations utiles est donc le nombre de
**blocs distincts**, pas le nombre de conteneurs ou de sessions.

| Bloc actuel | Type | Headroom |
|---|---|---|
| `37.69.230.71` | IPv4 résidentiel `/32` | frais |
| `104.28.230.202` | WARP IPv4, NAT partagée `/32` | partagé, peut s'épuiser |

Pour six stations, il manque **quatre blocs distincts**. Options, par ordre
de solidité :

1. **Hôte Linux avec IPv6 `/64` routé + WARP `stack: ipv6`** : chaque exit
   lie sa source et obtient son propre `/64` — plusieurs blocs sur un seul
   hôte. C'est la voie recommandée par `ebouther`, et la seule qui multiplie
   proprement.
2. **Edge deployments** (`Wasmer Edge`, Vercel, Cloudflare Workers) : un
   egress US par déploiement, utile aussi pour le geo-gate. Chaque déploiement
   = un bloc ; attention aux limites gratuites.
3. **IP NordVPN fraîches** : un bloc IPv4 par IP, mais le pool est partagé et
   les IP testées sont épuisées. À re-tester après reset, pas à empiler.
4. **Ne jamais compter** deux sessions, deux conteneurs ou deux comptes sur
   la même IP comme deux blocs.

Conclusion : le levier n'est pas le nombre de sessions NordVPN, c'est le
nombre de blocs. Sur ce Windows sans IPv6 globale, le plafond réaliste est
2–3 blocs (direct, WARP, éventuellement un edge US) tant qu'un hôte Linux
avec `/64` n'est pas disponible.

## 7.3 Balayage GitHub du 2026-11-06

Recherche API sur 12 requêtes (`opencode proxy`, `free`, `quota`, `zen`,
`unlimited`, `warp`, `ip rotation`, `gateway`, `opencode2api`, `free tier`,
`ipv6`), triée par `pushed_at` décroissant : **365 dépôts uniques**. La
majorité sont des UIs/tableaux de bord ; le sous-ensemble utile :

| Dépôt | MAJ | Langage | Apport | Verdict |
|---|---|---|---|---|
| `Deury08/warp-minimal` | 2026-11-06 | Go | WARP MASQUE UDP 443, exits IPv6 `/64`, userspace, binaire Windows | **Top find** |
| `wbi35772/opencode2api-win` | 2026-11-04 | Go | Gateway Windows + WARP pool, session-affinity 429 | Session inefficace |
| `vispar-tech/openrot` | 2026-11-01 | Python | Bridge WARP, 429 self-rotation, swap prompt de titre | À inspecter |
| `acruz6421-bot/opencode-infinite-proxy` | 2026-10-29 | JS | WARP rotation + reset session/project | Session inefficace |
| `leangtg123-art/opencode-termux-unlimited` | 2026-11-04 | Shell | Pool WARP ARM64 Termux | Hors Windows |
| `Ashineqq/opencode2api` | 2026-10-18 | Go | IPv6-first egress, 403 par DNS IPv4 détourné | Piste 403 |
| `scyslz/opencode-forward` | 2026-10-13 | Go | En-têtes CLI, isolation IPv4/IPv6 | Référence |
| `Munkh-Orgi1/opencode-free` | 2026-10-30 | Batch | Launcher Windows, rotation WARP | Simple |

### Tests décisifs

1. **Le WARP existant a déjà un bloc IPv6 200.** Trace :
   `ip=2a09:bac5:32d4:be::13:23e`, `warp=on`. Probe `/inference` avec notre
   wire exact, `IPRESOLVE=6` → **200** ; `IPRESOLVE=4` → **200**. C'est un
   troisième bloc, sans nouvelle registration.
2. **La rotation de session ne débloque pas une IP 429.** Trois sessions
   fraîches sur `186.247.77.36` → **3 × 429** (RA ~61 905 s). Le quota est
   par bloc ; l'idée `wbi35772` ne s'applique pas ici.
3. **`warp-minimal` démarre sur Windows mais ne s'enregistre pas** :
   `POST v0a2158/reg` expire (throttle Cloudflare). Le binaire est bon ; à
   retenter plus tard ou depuis une autre IP.

### Conséquence

Le bloc IPv6 est **maintenant exposé** comme station (`warp-ipv6`,
`IPRESOLVE=6`, egress `2a09:…/64` dans le ledger) : six requêtes, 6 × 200.
Ensuite, `warp-minimal` peut fournir plusieurs `/64` si l'enregistrement passe.
La rotation de session/projet n'est pas le levier quota.

## 8. Ce qu'il faut retenir

- **Le gate est compris** : `<env>` valide, `read`, `stream: true`, TLS Bun.
  Les trois mesures qui comptent sont dans `contracts/`.
- **Le 429 n'est pas un défaut de wire** : le clean-room l'a prouvé (même
  verdict client officiel / replay). C'est un quota d'IP.
- **Le cooldown était le vrai blocage** : une IP 429 était parkée 6–24 h. Elle
  est maintenant re-testable après 20 min.
- **La capacité est passée à deux stations** : WARP + pont direct, en rotation
  dans le pool `socks5` (4 × 200, stream 200). Les stations VPN saines sortent
  toujours sur des IP 429. Les six stations demandent des IP fraîches, pas
  seulement un tunnel réparé.
- **Le recipe station est trouvé** : WireGuard + DoT + MTU 1419 +
  `HEALTH_VPN_DURATION_INITIAL=60s` est healthy. `plain` casse le DNS,
  OpenVPN échoue `AUTH_FAILED`, et l'override généré forçait `plain` après
  deux échecs DoT.
- **L'identité reste hybride** : c'est la dernière incohérence de wire, à
  corriger par une capture 2.2.5 complète, pas par un changement d'UA isolé.

## 9. Références

- `specs/004-repo-parity-audit/spec.md` — spécification de l'audit.
- `specs/004-repo-parity-audit/research.md` — fiches dépôts.
- `specs/004-repo-parity-audit/plan.md` — plan et décisions.
- `specs/004-repo-parity-audit/tasks.md` — tâches ouvertes.
- `specs/004-repo-parity-audit/measurements.md` — mesures.
- `specs/004-repo-parity-audit/contracts/` — invariants.
- `specs/003-official-client-parity/` — parité wire antérieure.
- `docs/REPO_INTEL_2026-11-06.md` — notes de recherche.
- `docs/PLAN_RETABLISSEMENT_FREE_2026-11-05.md` — runbook de rétablissement.
- `docs/IPV6_WARP_STATION_2026-11-06.md` — implémentation et mesures de la
  station IPv6.
