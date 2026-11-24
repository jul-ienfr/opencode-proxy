# Audit WARP / egress alternatif — 2026-11-20

Branche : `019-audit-optimisation-flotte-free`
Périmètre : Cloudflare WARP (egress alternatif), profils wire, catalogue d'egress (partie WARP), `gluetun/`.
Méthode : lecture seule sur le code, preuves live (Docker, `logs/debug.log`, `logs/registry.db` en lecture seule). Aucun secret divulgué (noms de clés uniquement).

---

## 1. Résumé exécutif

1. **Le probe post-rotation est cassé par un changement de schéma de capture** : il lit `capture["body"]` / `capture["url"]` (`opencode.py:24380-24386`) mais les captures `2.2.5`/`2.3.0` ne portent que `body_raw` (`free/captures/2.2.5.json`). Conséquence mesurée : `quota_exhausted` = 599 échecs de rotation, et **toutes** les IP refusées sont quand même appliquées au ledger comme brûlées.
2. **Deux constructions de `wire_fp` incompatibles** coexistent : `free/wire_profile.py:169` (corps+UA+ordre en-têtes+JA3) vs `free/ip_registry/adapters/probe.py:291` (endpoint+version+noms d'en-têtes+UA+client+sha256). `free/classify_429.py` **n'utilise jamais** `wire_fp_match`/`ip_proven` (2 occurrences = signature + docstring) → la distinction brûlure-de-forme / quota réel est morte.
3. **`ip_state` n'a aucune ligne `provider='warp'`** (vérifié : 0 ligne, alors que `ip_state_events` en a 116) : toute lecture `_store.get("warp", ip)` de `opencode.py:16031` retourne `None`, donc `ip_proven=False` **en permanence** pour WARP.
4. **`rotate_exhausted_managed` tourne toutes les 30 s** (`opencode.py:3181`) et logge `budget horaire atteint` à chaque tour sans cooldown de log → 1884 lignes `[warp-rotate]` en une journée.
5. **`_expire_recreated` écrit ~474 lignes `egress/dead` par cycle** (`free/egress_adapters/warp.py:171-189`) : 175 560 des 182 913 observations WARP (96 %), soit 56 % des 326 812 lignes de `registry.db` (167 Mo) — écriture amplifiée sur des blocs périmés, toutes les 30 min.
6. **Le pool ne sélectionne plus les endpoints IPv4 WARP** (7 des 12 lignes v4 sont `enabled: false`), mais `reconcile_warp_ipv4` les ré-écrit toutes les 300 s — churn config confirmé par `[warp-ipv4]` (55 lignes/jour).
7. **Capacité comptée à double** : `free/pool/accounting.py:31-47` compte les blocs IPv4 `/32` dans `unique_buckets`, alors que `free/provider_capabilities.py:92-98` déclare `capacity=0` pour WARP IPv4. Mesure : 17 buckets comptés vs 12 réels (constitution XVIII).
8. **`RotationBudgetStore` fuit** : 3810 lignes `rotation_jobs`, plus ancienne au 2026-11-07, aucun `DELETE`/purge nulle part (grep vide) ; 134 lignes `finished_at IS NULL`.
9. **`WarpProvisioner` (218 l.) et `wire_admission.py` (81 l.) morts en production** ; `WarpProvider.create/recreate/probe/persist` sans appelant (seul `specs()` est utilisé, pour l'affichage read-only).
10. **Profil `1.19.5` pointé vers un fichier supprimé** (`free/profiles/verified_profiles.json:26` → `free/captures/1.19.5.json`, absent) : 6 des 12 stations WARP servent un corps templaté au lieu de la capture, sans trace dans les logs.

---

## 2. Carte des modules WARP

| Module | LOC | Rôle | Appelants réels |
|---|---|---|---|
| `free/warp_rotation.py` | 705 | Rotation Docker (restart/recreate), budget, health, egress, `RotationBudgetStore`, `WarpDockerProvider` | `free/pool/__init__.py:911-926` (instanciation), `:2821-2850`, `:3105` |
| `free/warp_provisioner.py` | 218 | Plan/apply idempotent de conteneurs WARP | `ops/providers/warp.py:13,164`, `scripts/warp_recreate_loopback.py:33`, `scripts/archive/autoscale_max_ips.py:40` |
| `free/egress_adapters/warp.py` | 191 | Adaptateur catalogue WARP (probe → `store.apply`) | `free/egress_runtime.py:20,424`, `scripts/egress_catalog.py:30,197` |
| `free/egress_reconcile.py` | ~120 | `bucket_for_endpoint`, `duplicate_bucket_groups`, `reconcile_endpoint` | `free/pool/__init__.py:2955,4920,4927,4956,5015`, `free/warp_rotation.py:364` |
| `free/egress_probe.py` | ~123 | Résolution d'egress SOCKS5 (curl_cffi → curl CLI) | `free/warp_rotation.py:639`, `ops/providers/warp.py:11,145`, `scripts/warp_recreate_loopback.py:31` |
| `free/egress_catalog.py` | 440 | Domaine pur (option, `/64`, transition, statuts) | `free/egress_store.py`, `free/egress_rotation.py`, `free/egress_runtime.py` |
| `free/egress_store.py` | 1078 | SQLite `registry.db` : `egress_options/observations/runs` | `free/egress_runtime.py:361`, dashboard routers |
| `free/egress_runtime.py` | 479 | Composition root : lit config, construit store+adapters+scheduler | `opencode.py:4208-4213` |
| `ops/providers/warp.py` | 176 | Adaptateur noyau 011 (plan/observe/create/recreate/rotate) | `dashboard/routers/station_routes.py:131,170` (`specs()` seulement) |
| `free/wire_profile.py` | 241 | Empreinte wire (`wire_fp`) + `conformance()` | `opencode.py:8265-8267` (`fingerprint_wire`) ; `conformance`/`is_conformant` = morts |
| `free/wire_admission.py` | 81 | Admission par `wire_fp` (observe/convert/compat) | **aucun** (mort) |
| `free/provider_capabilities.py` | 100 | `bucket_key`, `capacity` (WARP v4 = 0), `is_reserve_eligible` | `free/ip_registry/adapters/probe.py` (nordvpn seulement) |
| `gluetun/` | 1101 fichiers, 31 Mo | Fork Go de gluetun (15 commits locaux non upstream) | **non construit** : `docker-compose.yml` utilise l'image pinnée `qmcgaw/gluetun@sha256:4af0c804…` |

### Flux réel (provision → rotation → lease → bind → probe → serve)

```
config.yaml ip_rotation.socks5_proxies (24 lignes warp + 1 static)
   │
   ├─ free/pool/__init__.py:_rebuild_static_eps (4737) → Socks5Endpoint(label, ipv6, block, provider='warp')
   │     └─ free/egress_provider.block_for_egress / family_for_egress (19, 28)
   │
   ├─ PROVISION (hors runtime) : scripts/warp_recreate_loopback.py --apply
   │     └─ WarpProvisioner._run_args (190) → docker run -p 127.0.0.1:PORT:1080 …
   │        (le rotator recrée aussi, avec sa propre copie de l'argv : warp_rotation.py:576-603)
   │
   ├─ CATALOGUE (toutes les 1800 s) : free/egress_scheduler → WarpEgressAdapter.refresh (44)
   │     ├─ _warp_containers(config) (egress_runtime.py:277) : rows[:cap] avec cap = max_options = 24
   │     ├─ probe → _catalog_warp_probe (opencode.py:3241) → probe_proxy_free_gate (egress_probes.py:18)
   │     │     └─ _do_free_request_curl_cffi (11050) avec le wire officiel
   │     └─ store.apply(Observation(FREE_GATE)) → transition (egress_catalog.py:272) → status usable/quota/dead
   │
   ├─ SÉLECTION (chemin chaud) : free/pool._socks5_client_usable (3885)
   │     ├─ _socks5_usable → _station_usable (bad_until, ledger, health)
   │     └─ egress_ip présent + health ∈ {usable, probing, suspect}
   │
   ├─ BIND : _do_free_request_curl_cffi (11153-11163) pose CurlOpt.IPRESOLVE=2 (v6) / 1 (v4)
   │     + clé de pool par famille → bind 127.0.0.1:PORT:1080 (Socks5Endpoint.socks5_url:372)
   │
   ├─ PROBE POST-ROTATION : WarpRotator.rotate (460) → pool._post_rotation_probe_cb
   │     └─ _free_post_rotation_probe (opencode.py:24344) — LIT capture["body"]/["url"] (cassé)
   │
   └─ SERVE : opencode.py:_do_free_request_curl_cffi → opencode.ai/zen/v1/chat/completions
```

### ROTATION (déclencheurs)

```
opencode.py:3177 _warp_rotation_watchdog (toutes les 30 s)
  ├─ pool.rotate_exhausted_managed() (pool:2897)
  │    ├─ ledger.is_exhausted / model_ip_bad / trial_dead / duplicate_bucket / health repairing
  │    ├─ politique park (fallback.park_window_s = 1800) si un autre endpoint est servable
  │    └─ _request_warp_rotation(ep, cause) (2821)
  │         └─ si IPv4 managé → bascule sur le jumeau IPv6 (même container) ; sinon refuse
  │              └─ WarpRotator.request_rotation (216) : INFLIGHT + cooldown 600 s + budget 3/h
  │                   └─ thread daemon → WarpRotator._run (272) → rotate (397)
  │                        ├─ attempt 1 : docker restart (timeout 60)
  │                        ├─ attempts 2-4 : docker rm -f + docker run (timeout 60/120)
  │                        ├─ _wait_healthy (618) : poll 3 s/2 s jusqu'à 90 s
  │                        ├─ _resolve_egress (636) : curl_cffi/curl CLI, jusqu'à 30 s
  │                        ├─ refus : same_block / duplicate_block (v6) / quota_exhausted (probe)
  │                        └─ _apply (489) : endpoint.egress_ip, pool._per_station, health.repair_succeeded,
  │                             persist(yaml_set) + _refresh_siblings (354)
  └─ pool.reconcile_warp_ipv4 (4932, throttlé 300 s) : active 1 IPv4 par egress distinct
```

---

## 3. Findings

### P0

#### P0-1 — Le probe post-rotation ne peut plus s'exécuter : schéma de capture changé, `capture["body"]`/`capture["url"]` absents

**Preuve**
- `opencode.py:24380-24386` :
  ```python
  with open(capture_path, encoding="utf-8") as fh:
      capture = json.load(fh)
  body = copy.deepcopy(capture["body"])
  url = str(capture.get("url") or "")
  ```
  `except Exception` → `return False`.
- Schéma réel (working tree) : `free/captures/2.2.5.json` et `2.3.0.json` portent `body_raw` (43 453 / 47 978 octets) mais **ni `body` ni `url`** (`python -c` : `2.2.5` → `has body: False | has url: False`).
- **Changement de schéma identifié** : la version *committée* (`git show HEAD:free/captures/2.2.5.json`) est au schéma `opencode-free-capture/v1` avec `body` (dict) **et** `url = 'https://opencode.ai/inference/openai/v1/chat/completions'` ; idem `1.19.5.json`. Les fichiers du working tree ont été réécrits le **2026-11-17 à 17:29-17:30** (`ls --time-style=full-iso`), par `scripts/opencode_client_sync.py` — qui persiste `"body_raw"` (`:515`) et ne conserve `body` que dans l'objet en mémoire (`:125-126`), sans `url`. Le lecteur du probe n'a pas suivi.
- Seuls `free/captures/archive/2.2.5-20261104T170151Z.json` et `free/official_payload.json` portent encore `body` (ancien schéma).
- Corrélation temporelle (indicative, non causale) : les rotations réussies chutent de 183 (11-16) → 49 (11-17) → 12 (11-18) → 1 (11-20) ; `quota_exhausted` est présent avant le 11-17 (donc une partie pouvait être un vrai quota upstream), mais la bascule de schéma rend désormais **toute** lecture `capture["body"]` impossible.
- Mesure live : `'reason': 'quota_exhausted'` = **599** occurrences dans `logs/debug.log`. `quota_exhausted` apparaît **une seule fois** dans tout `free/warp_rotation.py` (ligne 466), dans la branche `if verdict is False` (`:460-467`) : ce verdict ne peut provenir que d'un probe **invoqué et ayant répondu `False`**, donc le probe s'exécute bien et échoue.
  *Nuance d'observabilité* : les messages du probe passent par `_log` (`opencode.py:642` → `dashboard.display.log`), qui écrit dans le **buffer mémoire du dashboard** (`dashboard/display.py:230-236`), **pas** dans `debug.log` — d'où `grep -c "post-rotation-probe" logs/debug.log` = 0. L'absence de trace fichier ne contredit donc pas l'exécution ; elle ajoute un finding d'observabilité (voir P2-13).
- Conséquence sur les rotations : `ok:True` = 537, `ok:False` = 947 sur la fenêtre du log (13→20 nov), dont 599 `quota_exhausted` et 264 `duplicate_block`, tous à `attempts: 4` (le maximum).

**Preuve live (chaîne complète)** — WARP 11 IPv6, 2026-11-20T11:32:09Z :
```
[free.warp_rotation] [warp-rotate] WARP 11 IPv6: {'ok': False,
  'new_ip': '2a09:bac5:32d4:14f0::216:c3', 'block': '2a09:bac5:32d4:14f0::/64',
  'attempts': 4, 'reason': 'quota_exhausted'}
```
Et dans `config.yaml`, la ligne `WARP 11 IPv6` porte **exactement** cette IP refusée : `egress_ip = 2a09:bac5:32d4:14f0::216:c3`, `block = 2a09:bac5:32d4:14f0::/64`. Même chose pour WARP 12 IPv6 (`2a09:bac5:32d3:14f0::216:ba`, `attempts: 4`, `quota_exhausted`). La chaîne est donc : probe cassé → verdict `False` → `_apply_tracking` (`warp_rotation.py:541-570`) → `persist` → `yaml_set` → `config.yaml` contient une IP non prouvée, marquée `bad_cause="rotation_rejected"` pour 60 s.

**Impact** — Une IP fraîche est systématiquement jugée « quota épuisé » après 4 recréations de conteneur, puis **écrite dans la config comme egress courant** sans preuve. `rotate_exhausted_managed` (`pool:3055`) la retente en boucle (`_retry_causes` contient `rotation_rejected`). Coût : 4 × (`docker rm -f` + `docker run` + 90 s de health + 30 s d'egress) par rotation refusée, soit ~6-8 min de churn Docker — mesuré : 599 rotations sur 1484 dans cette branche. Constitution VII : aucun succès sans preuve de contenu ; ici, c'est un **échec** prononcé sans preuve non plus, et une IP non prouvée propagée à la config et au ledger.

**Recommandation** — Lire `body_raw` (avec `json.loads`) en repli de `body`, et dériver `url` de `data.get("endpoint")` ou de la constante d'endpoint free. Ajouter un test qui ouvre la capture **réellement active** et vérifie que `body` et `url` sont extractibles. Effort **S**.

---

#### P0-2 — Deux `wire_fp` incompatibles ; `classify_429` ignore `wire_fp_match` et `ip_proven`

**Preuve**
- Construction A — `free/wire_profile.py:162-178` :
  `sha256(corps canonique ␟ UA ␟ ordre des en-têtes ␟ JA3)`.
  Appelée par `opencode.py:8267` (`_note_current_wire_fp`) puis stockée dans la ContextVar `_current_free_attempt["wire_fp"]`.
- Construction B — `free/ip_registry/adapters/probe.py:291-310` :
  `sha256(json({endpoint, version, header_names, user_agent, client, body_sha256}))`.
  C'est la seule valeur écrite dans `ip_state.wire_fp` (via `probe.py:210,225,264`).
- Comparaison — `opencode.py:16032` : `_st.wire_fp == _ctx_fp` compare **A** (ctx) à **B** (ip_state) → toujours faux.
- `free/classify_429.py:181-182` : `ip_proven` et `wire_fp_match` apparaissent **exactement 2 fois chacun** dans tout le fichier (la signature + une ligne de docstring). Le corps de `classify_429` (lignes 214-284) ne les lit jamais : la classification dépend uniquement du texte du corps.
- `free/ip_state.py` : aucune ligne `provider='warp'` en base (requête lecture seule : `ip_state` = 279 lignes `nordvpn` seulement ; `ip_state_events` = 573 `nordvpn` + 116 `warp`).
- `opencode.py:16031` : `_st = _ip_state_mod.store().get(_prov, _attempt_ip)` avec `_prov="warp"` → `None` → `_proven=False` toujours.
- Trace live des événements warp : `ip_state_events.ip = 'socks5:WARP 3 IPv6'`, `'socks5:WARP 12'` — des **identifiants logiques**, pas des IP, tous classés `bucket exhausted (FreeUsageLimitError)` avec un `wire_fp` identique (`c333745d…`) pour des stations différentes.

**Impact** — Constitution XVIII : la règle « un 429 sur une IP jamais prouvée n'est pas un quota consommé » est inopérante pour WARP. Tout 429 `FreeUsageLimitError` est compté comme quota consommé, sans preuve de format. Deux implémentations de la même notion, dont une seule est branchée.

**Recommandation** — Une seule fonction `wire_fp` (celle de `wire_profile`), utilisée par le probe et par le chemin client. Faire consommer `ip_proven`/`wire_fp_match` dans `classify_429` (la branche `FORMAT` doit exiger la preuve d'un mismatch). Effort **M**.

---

#### P0-3 — `ip_state` n'est jamais alimenté pour WARP : `ip_proven` structurellement faux

**Preuve**
- Écrivain unique de `ip_state` : `free/ip_registry/adapters/probe.py`, avec `provider="nordvpn"` **codé en dur** aux lignes 115, 185, 258.
- Aucun autre écrivain : `grep -rn "ip_state" --include="*.py" free/ opencode.py scripts/` → seul `opencode.py:16031` (lecture), `:25586-25623` (lecture dashboard), `scripts/fresh_chain.py` (lecture).
- Base : `SELECT COUNT(*) FROM ip_state WHERE provider='warp'` = **0**.
- Le label logique (`socks5:WARP 3 IPv6`) est utilisé comme « IP » dans les événements, ce qui viole l'invariant « une IP = une station » (principe V) côté journal.

**Impact** — Le classifieur 429 pour WARP est aveugle : ni preuve, ni burn, ni mismatch. Combiné à P0-2, tout 429 WARP est un quota consommé non prouvé.

**Recommandation** — Écrire `ip_state` pour WARP depuis `free/ip_registry/adapters/probe.py` en passant le provider en paramètre, ou depuis le chemin client WARP (`opencode.py:16031`) avec le `bucket_key` `warp:/64`. Effort **M**.

---

### P1

#### P1-1 — Capacité WARP comptée à double : les `/32` IPv4 entrent dans `unique_buckets`

**Preuve**
- `free/provider_capabilities.py:92-98` :
  ```python
  def capacity(provider: str, ip: str) -> int:
      """Capacité d'un bucket : 0 pour WARP IPv4, 1 sinon."""
  ```
- `free/pool/accounting.py:31-41` : `block = endpoint.block_key or endpoint.block or block_for_egress(egress_ip)` → `blocks[name].add(block)`, sans jamais consulter `capacity()`. Un `/32` IPv4 est donc compté comme un bucket.
- Mesure sur la config : 17 endpoints WARP actifs → 12 blocs `/64` + 5 blocs `/32` = **17 `unique_buckets`** comptés, capacité réelle **12**.
- `grep -rn "\.capacity("` : aucun consommateur de `provider_capabilities.capacity` dans le repo hors son propre module.

**Impact** — Constitution XVIII : l'IPv4 WARP est une NAT partagée et ne crée aucune capacité. Le dashboard Capacité (`dashboard/api.py:1587`, `free/capacity.py:evaluate_capacity`) et `free/capacity.py` surévaluent la flotte de 41 % (5/12). Les poids `egress_weights.warp: 24` (`config.yaml:156`) s'appliquent à cette capacité gonflée.

**Recommandation** — Filtrer les blocs IPv4 WARP hors de `blocks[]` dans `accounting.py` (ou appeler `capacity()` et ignorer les buckets à 0), et exposer un `unique_capacity` distinct de `unique_buckets` dans l'UI. Effort **S**.

---

#### P1-2 — `rotate_exhausted_managed` toutes les 30 s sans cooldown de log : 1884 lignes/jour

**Preuve**
- `opencode.py:3179-3187` : `while True: await asyncio.sleep(30); started = pool.rotate_exhausted_managed()`.
- `free/warp_rotation.py:243-247` : `logger.warning("[warp-rotate] %s: budget horaire atteint (store)", label)` — émis **à chaque appel** refusé, sans throttle.
- Mesure : `grep "2026-11-20" logs/debug.log | grep -c "warp-rotate"` = **1884** ; `budget horaire atteint` = 1884 - (quelques rotations) ; par station : WARP 12 IPv6 = 643, WARP 7 IPv6 = 397.
- Le log est en `logger.warning` (pas `debug`) et `attach_module_logger("free.warp_rotation")` (`opencode.py:669`) force l'écriture dans `debug.log` même avec `DEBUG=false`.

**Impact** — `logs/debug.log` = 22 Mo sur 7 jours, dominé par ces lignes. Le bruit masque les rotations réelles (`ok:True` = 537 vs 947 échecs). Coût I/O et rotation de fichiers.

**Recommandation** — Throttler le message par label (1×/5 min max) ou passer en `debug` avec un compteur agrégé loggé toutes les N itérations. Effort **S**.

---

#### P1-3 — `WarpRotator._run` : `_refresh_siblings` s'exécute sous le verrou global `_ROTATE_LOCK`

**Preuve**
- `free/warp_rotation.py:277` : `with _ROTATE_LOCK:` englobe l'appel `rotate()` **et** `_refresh_siblings()` (`:326`).
- `_refresh_siblings` (`:354-395`) appelle `reconcile_endpoint(other, persist=persist, timeout_s=self.egress_timeout_s)` — `egress_reconcile.py:84` → `endpoint.refresh_egress(timeout_s)` = **I/O réseau** (30 s de budget).
- Le verrou est un `threading.Lock` **global de module** (`:38`), partagé par toutes les stations.

**Impact** — Une rotation qui atteint `_refresh_siblings` bloque **toute** autre rotation WARP pendant la durée du probe réseau. Avec `attempts: 4` et 90 s de health par tentative (P0-1), la fenêtre de blocage atteint plusieurs minutes. Le commentaire `:310-313` justifie le verrou pour l'application de l'egress, pas pour le probe réseau qui suit.

**Recommandation** — Sortir `_refresh_siblings` du `with _ROTATE_LOCK` (l'appliquer après la libération, ou sous un verrou par container). Effort **S**.

---

#### P1-4 — Les deux constructeurs d'argv `docker run` WARP n'appliquent aucune limite de logs

**Preuve**
- `free/warp_rotation.py:576-603` (`_recreate`) : `docker run` avec `--cap-add NET_ADMIN`, `-p`, `--restart unless-stopped`, `--memory 512m`, `--pids-limit 128`, 3 labels `opencode.*`, `--env OPENCODE_STATION_LABEL`. **Aucune option `--log-driver`/`--log-opt`**, aucun `--cpus`.
- `free/warp_provisioner.py:190-218` (`_run_args`) : mêmes options, également sans limite de logs ni `--cpus`. Les deux copies diffèrent sur la source des valeurs (`spec.memory`/`spec.pids_limit` vs littéraux), sur l'ordre des `--env` (`sorted(...)` côté provisioner, un seul env côté rotator) et sur la présence de `spec.env` (provisioner seulement).
- Vérification live : `docker inspect warp-1 -f '{{.HostConfig.LogConfig.Type}}/{{.HostConfig.LogConfig.Config}}'` → `local/map[]` (aucune borne) et `NanoCpus=0`. Les 10 conteneurs gluetun, eux, ont `max-size: "10m"`, `max-file: "3"`, `cpus: 0.5` (`docker-compose.yml:8-14`).
- `ops/desired_state.py:97,102,110` définit un contrat (`provider ∈ {warp, proxies}`, `host == "127.0.0.1"` obligatoire pour WARP, image par défaut `caomingjun/warp:latest`) que `_recreate` ne valide jamais avant de lancer `docker run`.

**Impact** — Croissance non bornée des logs des 12 conteneurs WARP (rotation fréquente : `warp-8` a redémarré il y a ~1 min lors de l'inspection). La duplication de l'argv fait diverger silencieusement rotator et provisioner (un futur `--cpus`/`--log-opt` ajouté à un seul endroit). Constitution XVII (maintenabilité).

**Recommandation** — Une seule fonction de construction d'argv partagée, avec `--log-driver json-file --log-opt max-size=10m --log-opt max-file=3` et `--cpus` alignés sur gluetun. Effort **M**.

---

#### P1-5 — `RotationBudgetStore` : 3810 lignes, aucun purge, 134 jobs jamais clôturés

**Preuve**
- `free/warp_rotation.py:46-53` : docstring « les entrées de plus d'une heure sont purgées à la lecture ».
- Réalité : `allow()` (`:98-112`) fait un `SELECT COUNT(*) … WHERE started_at >= cutoff` — **lecture filtrée, aucune suppression**. `grep -rn "DELETE FROM rotation_jobs"` = aucun résultat.
- Base : 3810 lignes, plus ancienne `2026-11-07T16:50:28Z` (13 jours), `finished_at IS NULL` = 134.
- `finish()` (`:143-155`) est appelé dans le `finally` de `_run` (`:343-348`), mais un process tué pendant la rotation laisse la ligne ouverte ; ces lignes ouvertes sont comptées par `allow()` pendant 1 h après leur `started_at` puis restent en base pour toujours.

**Impact** — La table croît indéfiniment (3810 lignes / 13 j, ~293/j) dans `registry.db`, la même base que le ledger de quota. La docstring ment sur le comportement.

**Recommandation** — Purger dans `_ensure()` ou dans `allow()` : `DELETE FROM rotation_jobs WHERE started_at < ?` (cutoff 24 h), et clôturer les `finished_at IS NULL` plus vieux que le timeout de rotation. Effort **S**.

---

#### P1-6 — `reconcile_warp_ipv4` ré-active les endpoints IPv4 que l'opérateur a désactivés

**Preuve**
- `config.yaml` : sur les 12 lignes WARP IPv4, **7 sont `enabled: false`** (WARP 1, 2, 3, 5, 6, 8, 9) et **5 sont `enabled: true`** (WARP 4, 7, 10, 11, 12) — état incohérent avec la politique « un seul IPv4 par egress » que `reconcile_warp_ipv4` prétend appliquer. Vérifié : `enabled v6: 12 | enabled v4: 5`. Note : 3 des 5 IPv4 actives partagent le même `/32` (`104.28.230.202` : WARP 10 ; `104.28.198.201` : WARP 8 désactivée / WARP 12 activée).
- `free/pool/__init__.py:4968-4975` : pour chaque groupe d'egress IPv4 identique, `group.sort(key=_station)`, `keep = group[0]`, puis `endpoint.enabled = want` où `want = (endpoint is keep)` — **écrase la valeur opérateur**.
- `:4990-4995` persiste via `yaml_set("ip_rotation", "socks5_proxies", rows)` → réécrit `config.yaml` toutes les 300 s.
- Mesure : 55 lignes `[warp-ipv4]` le 2026-11-20 seul.
- Le commentaire `:4933-4940` reconnaît que « activer les douze endpoints duplique les buckets et fausse les poids », mais l'implémentation active activement un endpoint par IP au lieu de simplement désactiver les doublons.

**Impact** — Conflit d'autorité config vs runtime : le runtime gagne et réécrit le YAML. Un opérateur qui désactive les IPv4 WARP (décision conforme à XVIII, cf. P1-1) voit ses lignes réactivées au prochain tick. Churn d'écriture disque sur `config.yaml`.

**Recommandation** — Ne jamais ré-activer : `want = endpoint.enabled and (endpoint is keep)` (désactivation seulement), et ne persister que si la valeur a effectivement changé. Effort **S**.

---

#### P1-7 — Le profil vérifié `1.19.5` pointe vers un fichier de capture supprimé

**Preuve**
- `free/profiles/verified_profiles.json:26` : `"body_capture": "free/captures/1.19.5.json"`.
- Disque : `ls free/captures/*.json` → seulement `2.2.5.json` et `2.3.0.json`. `git status` : `D free/captures/1.19.5.json`.
- `free/runtime_identity.py:116-128` (`_capture_body`) : `path.read_text` → `except Exception: return None` → `build_body` (`:185-186`) retombe sur `template_official_body`.
- `free/runtime_identity.py:52-58` : `runtime_profile_versions` retourne les 2 versions vérifiées ≥ `RUNTIME_FLOOR=(1,19,5)`, soit `('2.2.5', '1.19.5')`.
- `resolve_station_identity` (`:70-92`) + `registry.select_for_station` (`:425-445`) distribuent par HMAC : sur les labels `WARP n IPv6`, **6 des 12** reçoivent `1.19.5` (WARP 1, 2, 4, 6, 8 + warp:warp-1/2/4/6).
- Le post-rotation probe (`opencode.py:24372`) et `_do_free_request_curl_cffi` (`:11105-11108`) exigent un profil non nul, mais le repli de `build_body` est **silencieux**.

**Impact** — Six stations WARP servent un corps `template_official_body` (reconstruit) au lieu de la capture `1.19.5` d'origine, tout en annonçant l'UA/les en-têtes du profil `1.19.5`. Constitution XVIII : « une sonde de qualification et la sonde live envoient le corps officiel exact » — le corps est ici un template, pas la capture. Aucune trace dans les logs (le repli n'est pas loggé).

**Recommandation** — Soit restaurer `free/captures/1.19.5.json`, soit retirer le profil `1.19.5` du registre (`RUNTIME_FLOOR` à `(2,2,5)`). Logguer un `warning` explicite quand `_capture_body` retourne `None`. Effort **S**.

---

#### P1-8 — Le probe de catalogue WARP promeut `usable` sur un 200 sans vérifier le contenu (VII)

**Preuve**
- `free/egress_validation.py:96-115` (`classify_outcome`) : `classify_result(int(outcome.status), …)` → `egress_catalog.py:402-404` : `if code == 200: return OptionStatus.USABLE`.
- `free/egress_catalog.py:289-294` (`transition`) : `result == USABLE` → `status=USABLE`, `expires=now+ttl_usable_s` (43 200 s).
- `free/egress_adapters/warp.py:123-141` : c'est ce chemin qui écrit l'option pour un probe réel.
- Le champ `ProbeOutcome.body` existe (`egress_validation.py:26`) et est rempli (`egress_probes.py:70`), mais `classify_outcome` ne l'inspecte pas.
- Contraste : `free/profile_registry.py:67-79` (`classify_evidence`) exige `content_ok and not error_frame` ; `opencode.py:24237-24243` (`_canary_verdict`) exige `text_delta`/`thinking_delta` et rejette `event: error`. Ces deux gardes existent mais ne sont pas appliquées au catalogue.

**Impact** — Constitution VII : « un 200 ne compte que si le flux a produit du contenu et pas de `event: error` ». Une réponse 200 + `event: error` (le mode de panne documenté pour `jev-1.13-free`) promeut une option WARP `usable` pour 12 h. L'option alimente ensuite la sélection du pool.

**Recommandation** — Passer `outcome.body` à un vérificateur de contenu dans `classify_outcome` (réutiliser `_canary_verdict` ou `classify_evidence`), et refuser `USABLE` sur trame d'erreur. Effort **M**.

---

### P2

#### P2-1 — `wire_admission.py` (81 l.) : module entier mort

**Preuve** — `grep -rn "wire_admission" --include="*.py"` → 2 résultats : le module lui-même et `tests/unit/test_wire_admission.py:2`. `admit(` → 5 résultats, tous dans `free/wire_admission.py`. Aucun import en production. `MODE_OBSERVE/MODE_CONVERT/MODE_COMPAT` : aucun consommateur.

**Impact** — 81 lignes + un fichier de test à maintenir ; le mode « compat ne cible jamais un bucket prouvé » n'est pas appliqué en production.

**Recommandation** — Soit brancher `admit()` dans `_socks5_client_usable`/la sélection, soit supprimer le module. Effort **S**.

---

#### P2-2 — `WarpProvisioner` : plan/apply/delete morts ; seule la génération d'argv est utilisée par les scripts

**Preuve**
- Importeurs : `ops/providers/warp.py:13`, `scripts/warp_recreate_loopback.py:33`, `scripts/archive/autoscale_max_ips.py:40` (archivé).
- `ops/providers/warp.py` : `specs()` est appelé par `dashboard/routers/station_routes.py:170`. En revanche `create()` (`:104`), `recreate()` (`:110`), `delete()` (`:124`), `probe()` (`:141`), `persist()` (`:161`) n'ont **aucun appelant** (`grep -rn "\.create(\|\.recreate(\|\.probe(\|\.persist("` sur `dashboard/` et `ops/` → seuls `ops/providers/warp.py` lui-même et `self.provisioner.delete`).
- `WarpProvisioner.plan()`/`apply()`/`delete()` ne sont appelés que par `ops/providers/warp.py` (mort) et les tests.
- `scripts/warp_recreate_loopback.py:144-145` appelle `provisioner._runner(...)` et `provisioner._run_args(...)` — deux **méthodes privées**.

**Impact** — 218 lignes dont ~120 sans usage runtime ; le contrat `ops.lifecycle`/`ops.reconciler` (feature 011) n'est pas appliqué à la flotte WARP. Le script officiel contourne l'API publique.

**Recommandation** — Promouvoir `_run_args`/`_runner` en API publique (`build_run_args`, `run`) et supprimer `plan/apply/delete` ou les brancher dans le reconcileur. Effort **M**.

---

#### P2-3 — `gluetun/` : fork Go de 31 Mo (1101 fichiers, 15 commits locaux) jamais construit

**Preuve**
- `docker-compose.yml:26,185,296,384,…` : image `qmcgaw/gluetun@sha256:4af0c804003481e100180e1f8d4d5e9c48163fedc0f88d061881b615aef9d183` (digest pinné).
- `docker-compose.yml` n'a que 2 `build: .` (lignes 101 = `opencode-proxy`, 701 = `vpn-alerts`) — aucun `build` pointant vers `gluetun/`.
- `grep -rn "gluetun/Dockerfile\|build.*gluetun\|./gluetun"` hors `gluetun/` : aucun résultat.
- `git ls-files gluetun` = 0 fichier (non suivi par le dépôt parent, pas de `.gitmodules`) ; `gluetun/.git` est un dépôt **imbriqué** avec `origin` = `016-control-plane` et 15 commits locaux non poussés (`git log origin/master..HEAD` = 15), dont `fix(control): keep a tunnel with a fresh egress proof`.
- État non commité dans le sous-dépôt : `M internal/dns/ladder_config.go`, `M internal/dns/ladder_integration_test.go`.

**Impact** — 31 Mo de code source Go inerte dans le dépôt de travail, avec un état git imbriqué divergent (15 commits + 2 modifications non commitées). Risque de confusion sur la source de vérité du conteneur gluetun. Le nom `gluetun/` suggère à tort que ce fork est déployé.

**Recommandation** — Décider : soit builder ce fork (ajouter un service compose `build: ./gluetun`), soit l'extraire du répertoire de travail (le déplacer hors du repo ou l'ajouter au `.gitignore` + documenter). Effort **S** (décision) / **M** (si adoption du fork).

---

#### P2-4 — Duplication `_warp_containers` : `free/egress_runtime.py:277` et `scripts/egress_catalog.py:65`

**Preuve** — Les deux fonctions sont identiques ligne à ligne (mêmes filtres `provider == "warp"`, `managed`, mêmes 5 clés `container/egress_ip/block/enabled/healthy`, `healthy` dérivé de `enabled`). Diff : `scripts/egress_catalog.py` inline `cfg.get("ip_rotation")`, `egress_runtime.py` utilise `_section(config, "ip_rotation")`.

**Impact** — Un correctif sur le filtrage (ex. exclure les IPv4 pour XVIII) doit être appliqué deux fois. Le script `scripts/egress_catalog.py --provider warp` sonde donc la même liste que le runtime.

**Recommandation** — Exporter `_warp_containers` depuis `free/egress_runtime.py` (ou un module `free/egress_warp.py`) et l'importer dans le script. Effort **S**.

---

#### P2-5 — `duplicate_block` pour IPv6 : 4 recréations systématiques, 264 échecs

**Preuve**
- `free/warp_rotation.py:438-441` :
  ```python
  if new_block and new_block in forbidden:
      if ipv6:
          last_reason = "duplicate_block"
          continue
  ```
  `forbidden` est construit depuis **tous** les autres endpoints actifs (`:280-301`) plus `ledger.exhausted_ips()`.
- Mesure : `duplicate_block` = 264 occurrences, **toutes** à `attempts: 4` (le maximum). Le cas IPv4, lui, accepte explicitement le partage (`:442-450`).
- Les blocs WARP sont réutilisés par Cloudflare (commentaire `:40-41` : « Un restart peut rendre le même /64 »). Avec 12 stations et un pool de blocs Cloudflare limité, la collision est fréquente.

**Impact** — 4 × (`docker rm -f` + `docker run` + health 90 s + egress 30 s) pour finalement appliquer un bloc dupliqué via `_apply_tracking`. Le `MAX_ROTATION_ATTEMPTS=4` a été choisi pour trouver un bloc frais, mais le taux d'échec (264/1264 rotations) suggère que la contrainte est plus forte que le budget.

**Recommandation** — Réduire à 2 tentatives pour `duplicate_block` (le bloc sera de toute façon appliqué et marqué `rotation_rejected`), et ajouter un backoff entre les tentatives. Effort **S**.

---

#### P2-6 — `_note_health_probe` appelé deux fois de suite (ligne dupliquée)

**Preuve** — `free/warp_rotation.py:512-513` :
```python
pool._note_health_probe(endpoint, True, cause="rotation")
pool._note_health_probe(endpoint, True, cause="rotation")
```
Vérifié : `grep -n "_note_health_probe("` montre exactement ces 2 lignes consécutives dans `warp_rotation.py` (les autres appels sont dans `pool/__init__.py`). `_note_health_probe` (`pool:1637-1651`) incrémente les compteurs de `StationHealthRegistry.observe_probe` (`station_health.py:80-120`), qui exige `good_needed=2` probes pour revenir à `usable`.

**Impact** — Le double appel fait passer une station de `repairing` à `usable` en **un seul** événement de rotation (2 probes comptés), contournant la politique « 2 probes bons requis ». Soit c'est un doublon accidentel, soit une intention non documentée ; dans les deux cas le code ne dit pas laquelle.

**Recommandation** — Si l'intention est de réparer immédiatement : supprimer le second appel et appeler `health.repair_succeeded()` (déjà fait juste au-dessus, `:510-511`). Sinon documenter. Effort **S**.

---

#### P2-7 — `rotate_exhausted_managed` : le ledger est relu dans la boucle pour chaque endpoint

**Preuve** — `free/pool/__init__.py:2962-2982` : pour chaque `ep` de `managed`, `self._get_ledger()` (ligne 2968) puis `ledger.is_exhausted(raw, None)`, puis `self._ledger_ip(ep)`, puis pour chaque modèle de `_models` (`:2976`) un `model_ip_bad(_mid, ep)`. `_models` est l'ensemble de tous les modèles vus (`:2937-2941`).

**Impact** — Complexité O(endpoints × modèles) par scan, toutes les 30 s, dans la boucle asyncio (l'appel est **synchrone**, pas `to_thread`). Avec 12 endpoints × N modèles, le coût est modéré aujourd'hui mais croît linéairement. `reconcile_warp_ipv4`, lui, est bien envoyé en `asyncio.to_thread` (`opencode.py:3193`).

**Recommandation** — Hisser `_get_ledger()` et un snapshot des états hors boucle, ou déporter le scan complet dans un thread. Effort **S**.

---

#### P2-8 — `_maintenance_loop` dort 24 h avant la 1ʳᵉ purge : 327 k observations, 23 707 lignes au-delà de la rétention, `registry.db` = 167 Mo

**Preuve**
- `free/egress_runtime.py:169-177` :
  ```python
  while True:
      await asyncio.sleep(86_400.0)
      try: await asyncio.to_thread(self.maintenance)
  ```
  La purge de rétention et l'export JSON n'ont lieu qu'après **24 h** de fonctionnement continu — et `maintenance()` (`:161-167`) appelle `store.purge()` (`free/egress_store.py:930-947`), qui supprime bien `observed_at < now - retention_s`.
- Mesure base (lecture seule) : `egress_observations` = **326 812 lignes**, dont **23 707 au-delà de la rétention de 7 j** (`config.yaml` : `egress_catalog.retention_days: 7`), plus ancienne à 7,7 jours. Répartition : **warp 182 913** (56 %), public 140 321, nordvpn 3 578.
- `egress_options` = 31 900 lignes ; `logs/registry.db` = **167 Mo** + WAL 5,5 Mo.
- L'export JSON `logs/egress_catalog.json` (3,3 Mo) date du **2026-11-19 19:01** — soit ~18 h, cohérent avec un cycle de 24 h jamais atteint depuis le dernier redémarrage.

**Impact** — Un redémarrage quotidien (fréquent : `[warp-rotate]` montre des redémarrages le 11-19 et 11-20) reporte indéfiniment la purge. La table WARP est le premier contributeur (182 913 observations) : le catalogue sonde 24 conteneurs toutes les 1800 s, mais les entrées **désactivées/incomplètes** sont aussi journalisées. `registry.db` (167 Mo) est la même base que le ledger de quota et `rotation_jobs` — la croissance dégrade les accès du chemin chaud.

**Recommandation** — Appeler `maintenance()` une fois dans `start()` avant d'entrer dans la boucle, et réduire la fréquence du refresh WARP quand aucun endpoint n'a changé. Effort **S**.

---

#### P2-9 — `reconcile_warp_ipv4` ne persiste pas les egress mis à jour par `reconcile_endpoint`

**Preuve** — `free/pool/__init__.py:4960-4995` : `reconcile_endpoint(endpoint, timeout_s=timeout_s)` est appelé **sans** `persist=`, alors que `refresh_socks5_egress` (`:5024-5028`) passe bien `persist=self._persist_socks5_egress`. Ensuite la boucle de persistance (`:4982-4995`) écrit `row["egress_ip"]` et `row["block"]` **depuis l'endpoint en mémoire** — ce qui fonctionne, mais uniquement pour les endpoints du groupe.

**Impact** — L'endpoint du groupe dont `reconcile_endpoint` a échoué (`continue` ligne 4962-4963) garde son `egress_ip` périmé, et la ligne `rows` correspondante est écrite avec l'ancienne valeur. Le `yaml_set` réécrit donc `config.yaml` avec des IP potentiellement périmées.

**Recommandation** — Passer `persist=self._persist_socks5_egress` à `reconcile_endpoint` et ne réécrire `rows` que pour les endpoints dont la réconciliation a réussi. Effort **S**.

---

#### P2-10 — `_expire_recreated` : amplification d'écriture de ~474 lignes/cycle, soit 56 % des observations de la base

**Preuve**
- `free/egress_adapters/warp.py:171-189` :
  ```python
  for option in store.list_options(provider=self.provider, limit=500):
      if option.station not in seen_blocks.values():
          continue
      current = seen_blocks.get(option.block)
      if current is None:
          store.apply(option, Observation(result=DEAD, …))
  ```
  `seen_blocks` est `{block: container}` pour les blocs **observés dans ce cycle**. Un `option.block` qui n'est **pas** dans `seen_blocks` est marqué `DEAD`.
- Le piège : `seen_blocks` est peuplé par bloc (`:84-86` : `unit = option.block or egress; if unit in seen_blocks: continue`). Comme chaque ligne WARP de `config.yaml` porte **un seul** `block` (l'egress courant), `seen_blocks` contient **une entrée par conteneur** — mais `egress_options` en contient **517** pour les 12 stations (chaque rotation crée une nouvelle option ; les anciennes ne sont jamais purgées, `ttl_dead_s=604800`).
- Arithmétique mesurée : `egress_observations` pour `provider='warp'` = **182 913**, dont **175 560 en `phase='egress', result='dead'`** (96 %). Sur 7,72 jours et 48 cycles/jour (`discovery_interval_s: 1800`), cela fait **~474 lignes `egress/dead` par cycle** — soit exactement l'ordre de `list_options(limit=500)`.
- Autre preuve : les options les plus observées ont **549 observations** chacune (`104.28.198.201/32`, `104.28.230.203/32`, … sous `station='warp-1'`), soit une par cycle pendant toute la fenêtre. Ces blocs IPv4 n'existent plus dans la config (P1-6 les a désactivés) mais sont ré-expirés à chaque cycle.
- En comparaison : `free_gate/usable` = 4 492 et `liveness/reachable` = 89 — le vrai travail de sonde est **2 %** du volume écrit.

**Impact** — 56 % des 326 812 observations de `registry.db` (167 Mo) sont produites par cette seule boucle, à chaque cycle, pour ré-écrire le même verdict `dead` sur des blocs périmés. Écritures SQLite transactionnelles (`store.apply` = `_upsert_option_locked` + `INSERT` observation) 474 fois par cycle, toutes les 30 min, en concurrence avec le ledger de quota et `rotation_jobs` sur la même base. C'est la cause principale de la taille de `registry.db` et du retard de purge (P2-8).

**Recommandation** — (1) Ne considérer comme « remplacé » qu'un bloc **appartenant à une station présente dans le cycle** (`station in seen_stations`), pas « `station` figure quelque part dans `seen_blocks.values()` » ; (2) ignorer les options déjà `dead` dont `last_checked_at` n'a pas bougé ; (3) purger les options `dead` plus agressivement que 30 j (`egress_store.py:933`). Effort **S**.

---

#### P2-11 — `egress_runtime._warp_containers` : `healthy` est un alias d'`enabled`, jamais mesuré

**Preuve** — `free/egress_runtime.py:292-293` :
```python
"enabled": bool(row.get("enabled", True)),
"healthy": bool(row.get("enabled", True)),
```
`free/egress_adapters/warp.py:80` : `meta={"healthy": bool(row.get("healthy", True)), …}`, et `:104-121` : sans probe injecté, `status = 200 if healthy else 0` → `REACHABLE`/`DEAD`.

**Impact** — La santé est dérivée de la config, pas d'une mesure. Un conteneur WARP `unhealthy` (constaté live : `warp-11` est `unhealthy`) reste annoncé `healthy: true` tant que sa ligne config est `enabled`.

**Recommandation** — Lire l'état Docker (`docker inspect -f {{.State.Health.Status}}`) dans `_warp_containers`, ou laisser le champ vide et ne pas promouvoir sur cette base. Effort **S**.

---

#### P2-12 — `MAX_ROTATION_ATTEMPTS=4` × health 90 s + egress 30 s : jusqu'à 8 min de churn par rotation

**Preuve** — `free/warp_rotation.py:28-29` (`HEALTH_TIMEOUT_S = 90.0`, `EGRESS_TIMEOUT_S = 30.0`), `:42` (`MAX_ROTATION_ATTEMPTS = 4`), `:416-476` (boucle). Pire cas : 4 × (rm -f 60 s + run 120 s + health 90 s + egress 30 s) = ~20 min théoriques, ~6-8 min typiques.
Mesure : 935 rotations ont atteint `attempts: 4` ; le watchdog tourne toutes les 30 s et `reconcile_warp_ipv4` toutes les 300 s.

**Impact** — Churn Docker intense (confirmé : `warp-8` « Up About a minute », `warp-3` « Up 30 minutes » — redémarrages récents). Sur un hôte Windows partagé avec 10 conteneurs gluetun, la charge est significative.

**Recommandation** — Backoff entre tentatives, plafond global d'essais par fenêtre, et réduction à 2 tentatives pour `duplicate_block`/`same_block`. Effort **S**.

---

#### P2-13 — Les diagnostics du probe post-rotation ne vont jamais dans un fichier

**Preuve**
- `opencode.py:642` : `from dashboard.display import log as _log`.
- `dashboard/display.py:230-236` : `log()` n'écrit que dans `log_lines` (deque mémoire de 200 lignes, affichée dans le dashboard). Il **ne** touche ni `_debug_file` ni un logger stdlib.
- `grep -c "post-rotation-probe" logs/debug.log` = **0** alors que 599 rotations se sont terminées sur le verdict du probe.
- Comparaison : `free.warp_rotation` bénéficie de `attach_module_logger("free.warp_rotation")` (`opencode.py:669`) → ses messages `logger.*` finissent bien dans `debug.log`. Les messages `_log(...)` du probe, non.

**Impact** — Le seul diagnostic de la cause racine P0-1 (« capture illisible », « aucun profil vérifié », « échec après N essais ») est invisible en post-mortem : le buffer mémoire est perdu au redémarrage, et `debug.log` ne le contient pas. C'est précisément ce qui a permis au bug de persister sans être vu.

**Recommandation** — Dans `_free_post_rotation_probe`, remplacer `_log(...)` par un `logger` attaché (`attach_module_logger("free.post_rotation_probe")`) ou appeler `logger.warning` en parallèle. Effort **S**.

---

### P3

#### P3-1 — `block_for_ip`/`block_for_ledger_key` : double indirection inutile
`free/warp_rotation.py:158-171` : `block_for_ledger_key` → `block_for_ip` → `quota_block`. Deux wrappers pour un seul appel. **Effort S.**

#### P3-2 — `WarpDockerProvider` : adaptateur 007 sans consommateur de production
`free/warp_rotation.py:654-696`. `grep -rn "WarpDockerProvider"` → le module + `tests/test_warp_rotation.py:12,101,108`. Aucun import runtime. **Effort S.**

#### P3-3 — `free/egress_validation.py` : `liveness_result` et `normalize_observation_payload` jamais appelés
`grep -rn "liveness_result\|normalize_observation_payload"` → uniquement les définitions (`:117`, `:127`) et `__all__` (`:140-141`). **Effort S.**

#### P3-4 — `free/egress_provider.py` : `measure_egress` non utilisé en production
`grep -rn "measure_egress"` → `free/egress_provider.py:28,112` + `tests/test_egress_provider.py`. **Effort S.**

#### P3-5 — `free/provider_capabilities.py` : `capacity()` et `is_reserve_eligible()` sans consommateur
`grep -rn "\.capacity("` = aucun résultat ; `is_reserve_eligible` → `free/provider_capabilities.py:99` seulement. Or `capacity()` est précisément la fonction qui encode XVIII (WARP v4 = 0) — elle est morte alors que le comptage (P1-1) l'ignore. **Effort S.**

#### P3-6 — `free/wire_profile.py` : `conformance()` et `is_conformant()` sans consommateur de production
`grep -rn "is_conformant\|conformance("` → `free/wire_profile.py:209,240,241` + `tests/unit/test_wire_profile.py`. Le contrôle de conformité avant envoi sur une IP prouvée (XVIII) n'est pas branché. **Effort M** (branchement) / S (suppression).

#### P3-7 — `free/profile_generator.py`, `free/profile_verifier.py` : jamais appelés hors `__init__`
`grep` : `profile_generator` → `free/__init__.py:24,37` seulement ; `ProfileVerifier` → `free/profile_verifier.py` seulement. Aucune boucle de re-vérification des profils dans `opencode.py` (`grep -rn "verified_profiles\|profile" opencode.py | grep -i "task\|loop\|refresh\|verify"` = vide). **Effort M.**

#### P3-8 — Profils vérifiés expirés depuis le 2026-11-13
`free/profiles/verified_profiles.json` : `expires_at` = `2026-11-13T09:34Z` / `09:41Z` (TTL 7 j). Aujourd'hui 2026-11-20 → `refresh_status` les passe `stale`. `refresh_status` (`profile_registry.py:258-263`) les re-promeut en `verified` **uniquement** s'ils ont ≥ 2 `pass_blocks`, et il **réécrit `verified_at`/`expires_at` à `now`** sans nouvelle preuve live (vérifié : deux appels successifs à `verified()` donnent des `expires_at` différents). Les preuves datent du 2026-11-06. **Effort M.**

#### P3-9 — `logs/identities/` : 6376 fichiers d'identité, jamais purgés
`ls logs/identities/ | wc -l` = 6376 ; préfixes = adresses IPv6 `2a09_bac5_…` (une identité par egress observé). Aucun purgeur (`grep -rn "identities" --include="*.py" | grep -i "purge\|clean\|prune"` = vide). **Effort S.**

#### P3-10 — `logs/*.py` : ~100 scripts de debug jetables dans le répertoire de travail
`ls logs/*.py` → 100+ fichiers (`inspect_*.py`, `query_*.py`, `peek_*.py`, `fix_*.py`). Non suivis par git (`.gitignore:15-25` couvre `logs/*.db*` etc.) mais présents. Inclut `logs/inspect_warp_quota.py`, `logs/inspect_warp_stats.py`, `logs/test_warp_six.py`, `logs/ast_visible_spawn.py`, `logs/find_visible_spawn.py`. **Effort S.**

#### P3-11 — `scripts/__pycache__/warp_bound_fleet.cpython-312.pyc` orphelin
Le `.pyc` existe (11 807 octets, 2026-11-14) mais `scripts/warp_bound_fleet.py` est absent. **Effort S.**

#### P3-12 — `scripts/warp_recreate_loopback.py` : `_wait_healthy` sans `creationflags`
`scripts/warp_recreate_loopback.py:48-60` : `subprocess.run(…, timeout=15, check=False)` sans `creationflags=CREATE_NO_WINDOW`. Le gate `scripts/platform_audit.py:28` exempte `scripts/` (CLI opérateur), donc ce n'est **pas** une violation XVI, mais c'est le seul script WARP qui ouvre potentiellement une fenêtre. À noter pour cohérence. **Effort S.**

---

## 4. Inventaire du code mort

| Élément | Fichier:ligne | Preuve d'absence d'appelant |
|---|---|---|
| `free/wire_admission.py` (module entier) | `free/wire_admission.py:1-81` | `grep -rn "wire_admission"` → module + `tests/unit/test_wire_admission.py:2` uniquement |
| `admit()`, `admit_many()`, `MODE_*` | `free/wire_admission.py:14-17,40,72` | 0 consommateur hors tests |
| `WarpDockerProvider` | `free/warp_rotation.py:654-696` | `grep -rn "WarpDockerProvider"` → module + `tests/test_warp_rotation.py:12,101,108` |
| `WarpProvisioner.plan/apply/delete` | `free/warp_provisioner.py:129,160,181` | appelés par `ops/providers/warp.py` (mort) + tests ; `ops/providers/warp.py:104-161` (create/recreate/delete/probe/persist) sans appelant |
| `WarpProvider.create/recreate/delete/probe/persist` | `ops/providers/warp.py:104,110,124,141,161` | `grep -rn "\.create(\|\.recreate(\|\.probe(\|\.persist(" dashboard/ ops/` → aucun |
| `measure_egress` | `free/egress_provider.py:28` | `grep -rn "measure_egress"` → module + tests |
| `liveness_result` | `free/egress_validation.py:117` | `grep` → définition + `__all__` seulement |
| `normalize_observation_payload` | `free/egress_validation.py:127` | idem |
| `provider_capabilities.capacity()` | `free/provider_capabilities.py:92` | `grep -rn "\.capacity("` → aucun résultat repo |
| `is_reserve_eligible()` | `free/provider_capabilities.py:99` | `grep` → définition seule |
| `wire_profile.conformance()`, `is_conformant()` | `free/wire_profile.py:209,240` | `grep` → module + `tests/unit/test_wire_profile.py` |
| `profile_generator` (module) | `free/profile_generator.py` | `grep -rn "profile_generator"` hors `free/__init__.py:24,37` et tests → aucun |
| `ProfileVerifier` | `free/profile_verifier.py:51` | `grep -rn "ProfileVerifier"` → module + `free/__init__.py` |
| `RotationBudgetStore.finish()` (partiellement) | `free/warp_rotation.py:143` | appelé, mais 134 lignes `finished_at IS NULL` jamais réparées |
| `scripts/archive/autoscale_max_ips.py` | `scripts/archive/autoscale_max_ips.py:1-25` | archivé explicitement (docstring), non planifié (specs/013 T001/T051) |
| `scripts/__pycache__/warp_bound_fleet.cpython-312.pyc` | `scripts/__pycache__/` | source `.py` absente |
| `free/ip_state.py` provider `warp` | `free/ip_state.py` (usage) | 0 ligne en base ; seul écrivain = `probe.py` avec `"nordvpn"` codé en dur |

**Absence de TODO/FIXME** : `grep -rn "TODO\|FIXME\|XXX\|HACK\|deprecated\|DEPRECATED"` sur les 7 modules WARP = **0 résultat**. Aucun `print(`/`breakpoint()`/`pdb` non plus.

---

## 5. Options / config sans consommateur

| Clé | Emplacement | Constat |
|---|---|---|
| `egress_catalog.providers.warp.max_options: 24` | `config.yaml:1149` | utilisé comme **cap de troncature** (`egress_adapters/warp.py:55`) : avec 24 lignes WARP managées, la 25ᵉ (row `direct`, `provider: static`) est écartée du catalogue — cohérent, mais le nom `max_options` masque un `rows[:cap]` |
| `egress_catalog.providers.warp.revalidate_interval_s` | absent de la section warp (`config.yaml:1145-1153`) | `ProviderPolicy.revalidate_interval_s` existe (`egress_catalog.py:64`) mais n'est lu **que** pour `Provider.PUBLIC` (`egress_runtime.py:418`) — clé morte pour WARP |
| `egress_catalog.providers.warp.ttl_*` | `config.yaml:1151-1153` | consommés via `ProviderPolicy.from_config` (OK) |
| `ip_rotation.station_max_rotations_per_hour: 3` / `station_min_rotation_interval_s: 600` | `config.yaml:674-675` | consommés par `free/pool/__init__.py:913-914` (WARP) **et** `vpn/manager.py:1802` (NordVPN) — double usage légitime mais nom trompeur (`station_` pour WARP) |
| `ip_rotation.fallback.order` → `warp_alt` | `config.yaml:167` | `warp_alt` est dans `DEFAULT_ORDER`/`KNOWN_PROVIDERS` (`free/fallback.py:17,28`) mais **aucun** endpoint ne porte ce provider (`_provider_matches` ne connaît que `warp`/`proxies`, `pool:4898-4904`) → entrée inatteignable |
| `ip_rotation.qualification_probe: true` | `config.yaml:152` | seul consommateur : `scripts/fresh_chain_supervisor.py:546` (chaîne NordVPN) — sans effet sur WARP |
| `free.workspace_path_masking: false` | `config.yaml:1051` | consommé par `opencode.py:10918` (OK) ; `free.station_identity: true` (`:1051`) active le chemin profil (OK) |
| `ip_rotation.post_rotation_probe.{enabled,max_attempts,max_tokens}` | `config.yaml:254-257` | consommé (`opencode.py:24353-24360`) mais **inopérant** à cause de P0-1 |
| `.env` / `.env.example` | — | **aucune** variable WARP (grep `-i warp` = 0 résultat dans les deux) ; `socks5_proxies` est documenté en commentaire (`.env.example:77-79`) mais la source réelle est `config.yaml` |
| `OPENCODE_STATION_LABEL` | `warp_rotation.py:599`, `warp_provisioner.py:214` | **écrit** dans le conteneur, **jamais lu** (grep repo = 2 résultats, tous deux des écritures) |

---

## 6. Duplication

| Logique | Emplacements | Nature |
|---|---|---|
| Argv `docker run` WARP | `free/warp_rotation.py:576-603` / `free/warp_provisioner.py:190-218` | 25 lignes quasi identiques ; divergent sur `--memory`/`--pids-limit` (littéraux vs `spec.*`), l'ordre des `--env`, et l'absence de labels `opencode.*` dans aucun des deux pour les logs |
| `_warp_containers` | `free/egress_runtime.py:277` / `scripts/egress_catalog.py:65` | identique (P2-4) |
| Mesure d'egress | `free/egress_probe.resolve_egress` / `free/egress_provider.measure_egress` / `free/egress_reconcile.reconcile_endpoint` | 3 chemins qui appellent `endpoint.refresh_egress`/`resolve_egress` avec des sémantiques de repli différentes |
| `wire_fp` | `free/wire_profile.fingerprint` / `free/ip_registry/adapters/probe.py:_template_fingerprint` | 2 constructions incompatibles (P0-2) |
| Vérification « 200 avec contenu » | `free/profile_registry.classify_evidence:67` / `opencode._canary_verdict:24237` / `free/egress_validation.classify_outcome:96` | 3 implémentations, seule la 3ᵉ (catalogue) ne vérifie **pas** le contenu (P1-8) |
| Health WARP | `WarpRotator._wait_healthy:618` (poll Docker) / `scripts/warp_recreate_loopback._wait_healthy:45` / `ops/providers/warp.probe:141` | 3 implémentations |
| `block_for_ip` / `block_for_ledger_key` | `free/warp_rotation.py:158,170` | double indirection vers `quota_ledger.quota_block` |

---

## 7. Violations constitution

| Principe | Violation | Preuve |
|---|---|---|
| **XVIII** (IPv4 WARP = capacité nulle) | `unique_buckets` compte les `/32` IPv4 | `free/pool/accounting.py:31-41` vs `free/provider_capabilities.py:92-98` ; mesure 17 vs 12 (P1-1) |
| **XVIII** (une IP = un format) | `wire_fp` client ≠ `wire_fp` probe ; `classify_429` ignore le mismatch | `free/wire_profile.py:169` vs `free/ip_registry/adapters/probe.py:291` ; `free/classify_429.py:181-182` (2 occurrences = signature+docstring) (P0-2) |
| **XVIII** (429 sur IP non prouvée ≠ quota) | `ip_proven` toujours `False` pour WARP (`ip_state` sans provider warp) | `opencode.py:16031` ; `SELECT COUNT(*) FROM ip_state WHERE provider='warp'` = 0 (P0-3) |
| **VII** (aucune preuve, aucun succès) | Le catalogue promeut `usable` sur un 200 sans inspecter le corps | `free/egress_validation.py:96-115` → `free/egress_catalog.py:402-404` (P1-8) |
| **VII** | Le probe post-rotation renvoie `False` sur une erreur d'I/O, comptant une IP saine comme épuisée | `opencode.py:24380-24386` + `warp_rotation.py:465-467` (P0-1) |
| **XVI** (exécution silencieuse) | **Respecté** : `warp_rotation.py:33,612` et `warp_provisioner.py:24,72` utilisent `creationflags=CREATE_NO_WINDOW` ; `egress_probe.py:22,95` idem | `scripts/platform_audit.py` ne scanne pas `scripts/` (exemption documentée) |
| **V** (une IP = une station) | Les événements `ip_state_events` utilisent `socks5:WARP 3 IPv6` comme « IP » | requête live : `provider='warp'`, `ip='socks5:WARP 3 IPv6'` |
| **XVII** (maintenabilité) | `free/egress_store.py` = 1078 l., `free/warp_rotation.py` = 705 l. (sous 1500) ; `opencode.py` = 33 618 l. (**hors limite**) | `wc -l` |
| **IV** (reset fournisseur = barrière dure) | `parse_retry_after` ne fabrique pas de reset local (conforme) ; mais `quota_ledger.note_result:457` utilise `next_utc_midnight(ts)` quand `Retry-After` est absent — estimation locale | `free/quota_ledger.py:442-457` |

---

## 8. Performance

| Point | Fichier:ligne | Coût |
|---|---|---|
| Watchdog 30 s + log non throttlé | `opencode.py:3179` ; `warp_rotation.py:243` | 1884 lignes de log/jour (P1-2) |
| `_ROTATE_LOCK` global autour d'un probe réseau | `warp_rotation.py:277,326` | Blocage multi-minutes de toutes les rotations (P1-3) |
| 4 tentatives × (rm+run+health 90 s+egress 30 s) | `warp_rotation.py:28,29,42` | ~6-8 min de churn Docker par rotation refusée (P2-12) |
| `reconcile_warp_ipv4` réécrit `config.yaml` | `pool:4990-4995` | I/O disque toutes les 300 s si un changement (P1-6) |
| `rotate_exhausted_managed` synchrone dans la boucle | `pool:2962-3111` | O(endpoints × modèles) sur l'event loop (P2-7) |
| `_maintenance_loop` dort 24 h avant la 1ʳᵉ purge | `egress_runtime.py:173` | `egress_observations` non purgé (P2-8) |
| `_expire_recreated` : ~474 `store.apply` par cycle | `egress_adapters/warp.py:171-189` | **175 560 lignes écrites** (96 % des obs WARP), cause principale des 167 Mo de `registry.db` (P2-10) |
| `rotation_jobs` sans index sur `started_at` seul | `warp_rotation.py:92-94` | index `(label, started_at)` seulement ; `allow()` filtre sur `label=?` (OK) mais la purge future en aurait besoin |

---

## 9. Fiabilité / idempotence

| Point | Constat |
|---|---|
| Idempotence `_recreate` | `docker rm -f` puis `docker run` — non idempotent si le conteneur existe déjà (le `rm -f` échoue silencieusement, `run` échoue avec « name in use » → `RuntimeError` capturée en `docker:` reason) |
| État au crash | `INFLIGHT`/`LAST_ROTATION`/`ROTATION_TIMES` sont **en mémoire** (`warp_rotation.py:34-36`) : un redémarrage les perd, permettant une rafale de rotations au boot. Le `budget_store` persiste, mais `INFLIGHT` non → 2 rotations concurrentes possibles après restart |
| `registry.db` vs JSON vs config | 3 sources : `registry.db` (`egress_options`, `rotation_jobs`), `config.yaml` (`socks5_proxies.egress_ip/block`), `logs/free_pool.json`. `_persist_socks5_egress` écrit `config.yaml`, `_seed_warp_ledger_from_catalog` lit `registry.db` — pas de réconciliation croisée |
| Containers zombies | Vérifié : aucun conteneur orphelin `warp-restore*` ; mais `warp-restore1` **existe dans le debug.log** (144 lignes, dernière 2026-11-20T11:30) alors qu'aucun conteneur ni ligne config ne le référence → rotation demandée sur un endpoint fantôme (probablement un label en mémoire `_socks5_eps`) |
| Ports occupés | `config.yaml` : ports 11080-11111 pour 24 lignes ; deux lignes partagent un port (IPv4/IPv6 d'une même station) — validé par `warp_provisioner._validate:79-104`. Aucun conflit détecté |
| Races rotation/requête | `_apply` pose `endpoint.egress_ip` **pendant** qu'une requête peut lire `socks5_url` (host:port inchangé) : la requête en vol sort par l'ancien tunnel jusqu'à la reconnexion. Le grace `rotation_wait_timeout` (`pool:3276-3288`) ne s'applique qu'aux stations `status == "connecting"` (Docker), **pas** aux `Socks5Endpoint` (qui n'ont pas de `status` mis à jour par le rotator) |

---

## 10. Zones non auditées / incertitudes

- **`gluetun/` (fork Go)** : audit superficiel (structure, provenance git, non-construction). L'audit du code Go interne (`internal/dns`, `internal/control`, `internal/egress`) dépasse le périmètre Python de cette passe ; les 15 commits locaux non upstream méritent une revue dédiée si le fork est adopté.
- **`scripts/fresh_chain*.py`** : référencés en passant (`fresh_chain_supervisor.py:546` lit `qualification_probe`) mais non audités — ils touchent NordVPN, pas WARP.
- **`free/qualification/*`** : grep `-i warp` = **0 résultat** dans tout le dossier → aucune interaction WARP. Non audité plus loin.
- **`free/public_egress_worker.py` / `free/public_egress_container.py`** : hors périmètre WARP (proxys publics), non audités.
- **`app/protocol/mapping.py`** : mentionne `prewarm_after_rotate` (`:464`) en commentaire ; le mapping Anthropic↔OpenAI n'a pas été audité pour son interaction avec les profils WARP.
- **Le contenu exact de `logs/free_pool.json`** (état pool persisté) : non inspecté au-delà de la présence de `socks5:warp-restore1`/`warp-restore3` (clés ledger périmées, cf. `quota_ledger.is_logical_proxy_key:89-95` qui documente leur purge).
- **`free/classify_429.py`** : audité fonctionnellement (paramètres morts), mais la totalité de la taxonomie (`_FORMAT_HINTS`, `_SESSION_HINTS`, `_QUOTA_TYPES`) n'a pas été confrontée aux corps 429 réels au-delà de l'échantillon `ip_state_events`.
- **L'IPv6 WARP `/64` réel** : la constitution dit qu'un `/64` est un bucket. Le code applique bien `quota_block` → `/64` (`quota_ledger.py:78-82`) et `option_block` → `/64` (`egress_catalog.py:200-202`). Non vérifié : si Cloudflare attribue réellement des `/64` distincts par tunnel ou si plusieurs tunnels partagent un `/64` (l'`_refresh_siblings` suggère le contraire, mais aucune mesure live n'a été faite).
- **Le probe de catalogue `probe_proxy_free_gate`** envoie `{"model": model, "messages": [{"role":"user","content":"ping"}]}` (`egress_probes.py:49-51`) puis `_do_free_request_curl_cffi` applique `_free_wire_body` avec `force_stream=True` → le corps officiel complet est bien envoyé (conforme XVIII). Le `content: "ping"` est écrasé par la copie officielle. **Non vérifié** : si `_filter_free_tools` peut retirer un outil et produire un corps non conforme sur une IP prouvée (le commentaire `opencode.py:8329-8336` indique que `subagent_fork` est retiré).
- **Aucun test d'intégration WARP réel** n'a été exécuté dans cette passe (lecture seule) : les findings live reposent sur `logs/debug.log`, `docker inspect/ps` et les requêtes lecture seule sur `logs/registry.db` / `logs/requests.db`.
