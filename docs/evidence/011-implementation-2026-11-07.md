# Preuves — implémentation 011 (2026-11-07)

**Feature** : `specs/011-station-fleet-usability/`  
**Audit** : `docs/AUDIT_STATIONS_NORDVPN_WARP_2026-11-07.md`  
**Statut** : première itération d'implémentation (P0 partiel + consolidation).  
**Aucune ressource payante, aucun second compte.**

## 1. Changements livrés

### Consolidation

- `ops/lifecycle.py` : `SlotSpec`, `SlotObservation`, `SlotState`, `SlotStatus`,
  `SlotDecision`, `LifecycleEngine`.
- `ops/lifecycle_policy.py` : `RepairPolicy`, `StartupPolicy`, `RotationBudget`.
- `ops/lifecycle_evidence.py` : preuves et logs communs.
- `ops/desired_state.py` : `DesiredStationSpec`, `DesiredEndpointSpec`,
  génération, hash, validation.
- `ops/reconciler.py` : diff desired/actual, aucun `delete`.
- `ops/providers/base.py`, `nordvpn.py`, `warp.py` : protocole et adapters
  réels (specs/observe/create/recreate/delete/rotate/probe/persist).
- `free/quota_ledger.py` : `quota_block()` unique ; `block_for_egress` et
  `block_for_ip` délèguent.
- `free/egress_probe.py` : résolveur d'egress unique.
- `free/egress_reconcile.py` : réconciliation live, buckets dupliqués,
  `unique_bucket_count`.

### WARP

- `free/warp_rotation.py` : loopback `127.0.0.1`, limites mémoire/PID, labels,
  env, budget horaire **persisté dans `registry.db`** (`RotationBudgetStore`,
  table `rotation_jobs`).
- `free/warp_provisioner.py` : `plan/apply/delete`, unicité label/container/
  port/block, rollback signalé.
- `free/pool.py` : `duplicate_egress_groups`, `unique_egress_buckets`,
  `refresh_socks5_egress`, `_socks5_client_usable`, statut enrichi,
  compteurs honnêtes ; un timeout sur un endpoint managé demande désormais une
  rotation (recréation du conteneur mort).
- `free/capacity.py` : `unique_buckets`, avertissement `duplicate_block`,
  `usable = min(endpoints, buckets)`.
- `dashboard/api.py` : `GET /api/stations`,
  `GET /api/stations/{provider}/{station}`,
  `GET /api/stations/reconcile-plan` (diagnostic read-only),
  `POST /api/vpn/socks5/refresh-egress`,
  `POST /api/vpn/station/{sid}/probe`.
- `scripts/station_audit.py` : audit CLI JSON.
- `scripts/warp_recreate_loopback.py` : migration des conteneurs WARP vers
  `127.0.0.1` avec réconciliation de l'egress.
- `scripts/measure_nordvpn_sessions.py` : mesure la limite de sessions
  NordVPN en démarrant les stations une par une.
- `scripts/measure_nordvpn_wg_sessions.py` : mesure WireGuard multi-hosts
  avec des conteneurs Gluetun temporaires.
- `scripts/station_soak.py` : soak court via `/api/stations`, synthèse
  disponibilité/buckets/doublons.

### NordVPN / exploitation

- `vpn/manager.py` : un conteneur vivant n'est plus supprimé au boot sur une
  simple divergence de stack (reconcile non destructif).
- `scripts/fresh_chain_supervisor.py` : `_nordvpn_maintained()` ; la
  maintenance ne s'arrête plus quand NordVPN n'est pas sélectionné pour le
  trafic ; probe d'egress NordVPN en arrière-plan toutes les 60 s pour
  rafraîchir `station_health` (fin des états `auth`/`repairing` périmés).
- `opencode.py` : `free.pool`, `free.warp_rotation`, `free.quota_ledger`,
  `station_repair` rattachés à `debug.log`.
- `scripts/refresh_nord_credentials.py` : écriture atomique + clé WireGuard
  dans `vpn_configs/wireguard.env`.
- `ops/env_store.py` : écriture `.env` atomique et sérialisée (verrou
  inter-processus, tmp unique) ; utilisé par le superviseur et les chaînes.

## 2. Tests

| Suite | Résultat |
|---|---|
| `tests/test_plan_011_baseline.py` + `test_plan_011_consolidation.py` | **27 passed, 0 failed** |
| `tests/test_desired_state.py`, `test_reconciler.py`, `test_warp_provisioner.py` | **23 passed** |
| `tests/test_rotation_budget.py`, `test_env_store.py` | **4 passed** |
| `tests/test_provider_adapters.py` | **4 passed** |
| Suite ciblée stations/WARP/capacity (8 fichiers) | **115 passed, 0 failed** |
| `tests/test_plan_011_live.py -m docker` | **5 XPASS, 1 XFAIL** |

Le seul XFAIL live restant : `AUTH_FAILED` NordVPN.

## 3. État live après implémentation (2026-11-07 15:35)

```text
GET /healthz                         = 200
GET /api/stations                    = 12 stations
WARP                                  = 6/6 client-utilisables
WARP buckets                          = 6/6 uniques
WARP ports                            = 127.0.0.1 uniquement
NordVPN conteneurs                    = 6/6 créés
NordVPN client-utilisables             = 2–3/6 selon les probes (variable)
capacity_degraded                     = false (WARP)
usable_stations                       = 6
boot_error                            = "boot 2-3/6: connected 2-3 ..."
```

Le **faux 6/6** est corrigé : les compteurs suivent l'état réel, le doublon
WARP a été supprimé, les 6 conteneurs WARP sont en loopback et les 6 endpoints
répondent à un probe OpenCode réel. Les stations NordVPN répondent par
intermittence (2, 4, 5 selon les probes) ; 1, 3 et 6 restent en
`AUTH_FAILED`/tunnel sans réponse.

La configuration avait basculé en `proxy_mode: vpn` + `egress_providers:
[warp, nordvpn]`, ce qui faisait tomber la capacité à 0/6 (santé NordVPN
stricte). Elle a été remise en `proxy_mode: socks5` + `[warp]` pour garantir
les 6 stations WARP ; la flotte NordVPN reste maintenue par le superviseur et
reste disponible comme fallback.

Le diagnostic `GET /api/stations/reconcile-plan` renvoie actuellement
10 `noop`, 1 `repair` (NordVPN 3) et 1 `start` (NordVPN 6) : le moteur de
reconcile est branché en lecture seule et confirme que WARP est conforme.

### Soak court (120 s, 8 échantillons)

```json
{
  "nordvpn": {"min_usable": 3, "avg_usable": 3.0, "max_usable": 3},
  "warp": {"min_usable": 6, "avg_usable": 6.0, "max_usable": 6,
           "unique_buckets_min": 6},
  "duplicate_samples": 0
}
```

WARP est stable 6/6, 6 buckets, 0 doublon sur la fenêtre. NordVPN reste stable
à 3/6. Le soak 24 h reste à exécuter.

Un soak 24 h a été démarré le 2026-11-07 à 17:49Z (`scripts/station_soak.py
--duration 86400 --interval 30`, PID 33384). Il écrit un échantillon toutes les
30 s dans `logs/station_soak.jsonl` et la synthèse finale dans
`logs/station_soak.json`.

## 4. blocage NordVPN

`python scripts/refresh_nord_credentials.py --check` renvoie `200` avec
username/password/WireGuard key valides, et les credentials locaux
correspondent à la réponse API. Malgré cela :

- OpenVPN renvoie `AUTH_FAILED` ;
- WireGuard ne passe pas le healthcheck DNS (MTU/PMTUD en échec).

Le blocage est donc externe au code de cycle de vie (compte, sessions ou
réseau), pas un bug de réconciliation. Piste suivante : campagne de mesure de
la limite de sessions (4/5/6/7/8) et test WireGuard sur un host différent,
comme prévu dans `research.md` D-11/D-12.

Une première exécution de `scripts/measure_nordvpn_sessions.py --apply` a été
faite : une seule station (WireGuard) n'avait pas d'egress, puis un test
OpenVPN TCP sur `87.120.102.83:443` a reçu `Connection reset`. Ce n'est donc
pas une preuve de limite de sessions : il faut refaire la mesure avec un
protocole/host validé (UDP 1194 ou WireGuard sur un host qui répond).

Un test complémentaire (`scripts/measure_nordvpn_wg_sessions.py`, hosts
distincts) donne 1 session OK puis la 2e sans egress. Comme les conteneurs
temporaires partagent la même clé et la même `WIREGUARD_ADDRESSES`, ce résultat
ne distingue pas encore « limite de compte » de « réutilisation de clé/adresse
WireGuard ». Il faut des clés/adresses distinctes ou un test OpenVPN par
session pour conclure.

## 5. Fichiers de test ajoutés

- `tests/test_plan_011_baseline.py`
- `tests/test_plan_011_consolidation.py`
- `tests/test_plan_011_live.py`
- `tests/test_desired_state.py`
- `tests/test_reconciler.py`
- `tests/test_warp_provisioner.py`
- `tests/test_rotation_budget.py`
- `tests/test_env_store.py`
- `tests/test_provider_adapters.py`

## 6. Prochaines actions

1. Mesurer la limite de sessions NordVPN et choisir 5 ou 6 stations actives.
2. Tester WireGuard NordVPN sur un host validé (al41 ou autre) avec la clé
   fraîche.
3. Brancher le budget de rotation persisté dans `registry.db` (T049).
4. Compléter les adapters `NordVpnProvider`/`WarpProvider` (T101/T102) et le
   pipeline d'évidence (T108).
5. Soak 24 h et rapport final.

## 7. Correctif client desktop (2026-11-07 21:11Z)

### Causes racines

- Le preflight free renvoyait un 429 `Free quota exhausted on all VPN
  stations` dès qu'un cooldown court (20-60 s) était actif, alors que des
  stations WARP/NordVPN restaient disponibles. Le client desktop affichait
  `Request quota exhausted` sans laisser au proxy le temps de basculer.
- Un amont pouvait fermer le flux sans produire d'octet : le garde de flux
  transformait cela en `502 Upstream produced no output (empty stream)`,
  visible par le client sur environ 1 requête sur 5.
- Un rôle de message non standard (`developer`/`tool` orphelin) pouvait
  faire répondre `InvalidParameter: invalid message role` à l'upstream.

### Changements

- `_sanitize_free_messages()` : `developer` -> `system`, rôle inconnu et
  `tool` orphelin -> `user`, `tool_calls` invalides retirés.
- `_free_preflight_gate()` : attente interne des cooldowns courts (budget
  `free.preflight_inline_wait_s: 30`) puis re-vérification, sur les trois
  routes (`/v1/messages`, `/v1/chat/completions`, `/v1/responses`).
  Un 429 n'est renvoyé que si le délai dépasse le budget ou si le quota
  journalier est réellement épuisé.
- `retry_until_success` activé : rejoue côté proxy les 429/5xx et les flux
  vides avant le premier octet, avec keepalives SSE, `max_wait_s: 180` et
  `max_delay_s: 15`. Le client ne voit plus ces échecs transitoires.
- `ops/env_store.py` : verrou et `os.replace` retentés sur Windows
  (`PermissionError` transitoire), pour ne plus perdre une clé `.env`.
- Rotation WARP : au plus 2 rotations par scan
  (`fallback.max_rotations_per_scan: 2`) et station remise saine dès que la
  rotation a validé l'egress, pour ne plus faire tomber la flotte à 2/6
  pendant un cycle de rotation.
- `/api/vpn-status` : en `auto_egress` avec WARP/proxies, la capacité est
  calculée sur les endpoints SOCKS5 (6/6) et non plus sur les seuls
  conteneurs NordVPN (0/6).
- `config.yaml` : `proxy_mode: auto_egress`, `egress_providers:
  [warp, nordvpn]`, `free.preflight_inline_wait_s: 30`,
  `retry_until_success.enabled: true`.

### Preuves client

- Requêtes du client `opencode/latest/2.2.5/desktop` depuis le redémarrage
  du proxy à 21:11Z : **67/67 succès, 0 échec** (dernière à 21:36:49Z).
- Rejeu du payload desktop exact (229 messages tronqués, 13 outils,
  `stream: true`) : **4/4 HTTP 200**, TTFB 3,9-15,5 s.
- Flux simples `deepseek-v4.1-flash-free` : **5/5 HTTP 200**, TTFB < 2,4 s.
- `proxy_misc_total{name="retry_until_success:retry"} = 4` : quatre
  échecs transitoires ont été rejoués en interne et ont fini en 200.
- Aucun `config persist failed` ni `[free-400-stream]` depuis le
  redémarrage.

### État flotte au moment de la preuve

- WARP : 6 blocs `/64` uniques, 5/6 client-utilisables ; la 6e station
  (`warp-restore3`) était parquée après un 429 et se réinitialise à
  `21:25:04Z`.
- Après réinitialisation et probe des six ports SOCKS5 : **WARP 6/6
  client-utilisables, 6 blocs uniques, 0 doublon** (21:26Z).
- `/api/vpn-status` : **usable_stations 6/6, configured 6, capacity_degraded
  basé sur le quota restant** ; la capacité n'affiche plus 0/6 en
  `auto_egress`.
- NordVPN : 2-3/6 client-utilisables ; la flotte reste maintenue par le
  superviseur comme secours, sans être requise pour le client.

### Tests

- `tests/test_free_preflight_gate.py` : 2 passed.
- `tests/test_free_message_roles.py` : 5 passed.
- `tests/test_retry_until_success.py` + `tests/test_empty_guard_middleware.py` :
  passés.
- Gate 011 consolidée + SOCKS5 + baseline + env_store + WARP : **140 passed**.
- Live 011 : **6 passed, 1 xfail** (NordVPN 6/6).

## 8. Impersonation des chemins et tool_calls (2026-11-08)

### Diagnostic confirmé

L'impersonation free cache bien le nom d'utilisateur au provider :
`free/station_identity.py` remplace `C:\Users\julie\...` par
`/workspace/oc-<station>-<hash>` dans le prompt système et dans tous les
corps envoyés à l'amont. Vérifié sur le wire : le provider ne voit ni
`C:\Users\julie` ni le nom du workspace.

Mais cette substitution cassait les tool_calls :

- le modèle renvoyait `/workspace/oc-<station>-<hash>/fichier` ;
- sur Windows, le client résolvait ce chemin en
  `C:\workspace\oc-...`, qui n'existait pas ;
- certains modèles écrivaient aussi `filePath` au lieu du `path` exigé par
  les schémas OpenCode, d'où `path: Missing key`.

La trace exacte a été retrouvée dans la session OpenCode :
`File not found: /workspace/warp-ipv6-2/src/core/database.ts` puis
`The folder C:\workspace does not exist`.

### Correctif

- `free/station_identity.py` : `workspace_env_cwd()` ajoute un hash court du
  workspace réel à la racine factice. Deux projets ouverts sur la même
  station ne partagent plus la même jonction.
- `ensure_workspace_alias()` crée `C:\workspace\oc-<station>-<hash>` comme
  jonction vers le vrai workspace. Le provider ne voit que le chemin
  factice ; le client local résout le vrai fichier.
- `streaming/tool_paths.py` + `streaming/tool_path_healing.py` : middleware
  SSE qui renomme `filePath`/`file_path`/`filename` en `path` sur les
  tool_calls `edit`/`read`/`write`, avant livraison au client.
- `_ChatToolCallHealer` et `_collect_chat_completion` traduisent aussi les
  racines factices vers les racines réelles quand le handler le permet.

### Preuves

- Le chemin `/workspace/oc-warp-restore3-9a8b65dd/AGENTS.md` est résolu par
  le client et lit le vrai `AGENTS.md` du workspace.
- Test live : le tool_call revient avec
  `C:\Users\julie\Downloads\opencode-proxy-one\AGENTS.md` (chemin réel)
  après traduction.
- Tests : `tests/test_workspace_alias.py` (2), `tests/test_tool_call_path_healing.py`
  (9), et la gate SSE/middleware (153 passed sur le lot ciblé).
