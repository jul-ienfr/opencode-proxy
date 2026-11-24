# Audit 2026-11-20 — 01 · Cœur de la jambe free

Zone : `free/` (152 fichiers Python, 45 246 lignes), priorité `free/pool/__init__.py`
(8 362 l.), plus les modules cités dans la commande d'audit.
Méthode : lecture intégrale par tranches + `grep` de preuve d'usage sur tout le
repo (hors `Lib/`, `_research/`, `.audit-wt/`, `.audit-logs-wt/`, `node_modules/`,
`logs/`). Aucune modification de code. Mesures sur la base de production en
lecture seule (`file:logs/registry.db?mode=ro`) et sur le state persisté
`chain_state/free_pool`.

---

## 1. Résumé exécutif

1. **Trois P0** : (a) `repair_owner: supervisor` (config livrée) désarme toute
   rotation autonome du pool sans vérifier que le superviseur est vivant ;
   (b) des valeurs `time.monotonic()` (`station_health.parked_until`,
   `ip_quota.blocked_until`) sont persistées puis restaurées au boot suivant —
   après un reboot OS elles peuvent bloquer des stations pendant des jours ;
   (c) `_draining_stations` n'est nettoyé que par la fin d'une rotation réelle :
   quand `_launch_rotation` sort sans enfiler (cas (a) notamment), la station est
   retirée **définitivement** de la sélection, sans que le dashboard le voie.
2. Le monolithe `free/pool/__init__.py` (8 362 l.) viole le principe XIV
   (≤ 1500 l.) ; le strangler `free/pool/*.py` n'a extrait que 604 lignes.
3. `QuotaLedger._save_sqlite` écrit dans `egress_ips` (source de vérité du
   registre) des clés de quota **tronquées** (`server_ip_key`) et des labels
   `socks5:*`, étiquetées `provider='warp'` par défaut → incohérence III/V.
4. 634 `except Exception` dans `free/`, dont 231 `pass` : un bug de code devient
   un comportement silencieux dans le chemin de sélection.
5. `free/fallback.py` : 5 des 8 clés de `ip_rotation.fallback` n'ont aucun effet
   observable ; `wire_admission.py` (81 l.) n'a **aucun** appelant en production.
6. `free/ip_registry/` (≈ 4 700 l. avec les sous-paquets) n'est appelé en
   production que par `scripts/fresh_chain*.py` et deux vues dashboard ; le
   cœur du pool ne l'utilise pas — deux registres d'IP coexistent (`registry.db`
   et `logs/ip_registry.db`, ce dernier 0 octet).
7. Performance : le ledger réécrit **toutes** ses lignes SQLite (8 519 buckets)
   à chaque flush ; `_persist_socks5_egress` réécrit `config.yaml` entier sur le
   chemin de rotation WARP ; `get_status()` construit 20+ champs par station à
   chaque rafraîchissement dashboard.
8. Fiabilité : `registry.db` = 167 Mo avec 326 812 lignes `egress_observations`
   et 8 519 `quota_buckets` ; la table `quota_buckets` n'est **jamais purgée**
   par `rotation/infrastructure/db/retention.py` (seules `observations`,
   `fallback_events`, `incidents`, `repair_jobs`, `config_snapshots` le sont).
9. Dead code significatif : `EgressBlock`/`QuotaObservation` (007), `wire_admission`,
   `_TRANSIENT_HINTS`, `IpQuotaRegistry.prune`, `station_health.disable/enable/
   repair_failed`, `Classification.is_format`, `SlotWaitStats` partiellement,
   `pick_two`, `_socks5_provider_available`, `public_pool_rows`, `provider_status`.
10. Config morte : `ip_registry.db_path` (pointe vers un fichier vide),
    `ip_registry.selection.*` (warm_pool_size, unique_ip, allow/deny_countries),
    `ip_rotation.fallback.{order,probe_interval_s,max_block_s,fail_closed,...}`
    majoritairement inertes, `egress_capacity.reserve_policy` consommé seulement
    par le dashboard.

---

## 2. Carte des modules de la zone

### 2.1 Cœur chaud (chemin de requête client)

| Module | LOC | Rôle réel | Appelé par |
|---|---|---|---|
| `free/pool/__init__.py` | 8362 | `FreeIPPool` : sélection, quota, rotations, SOCKS5/WARP, statut | `opencode.py` (via `free_ip_pool` shim + `shared_state`), `dashboard/routers/*` |
| `free/pool/selection.py` | 174 | tri/dédup purs (egress_rank, dedupe_by_ip, prefer_healthy…) | `free/pool/__init__.py` |
| `free/pool/quota.py` | 91 | `ledger_ip`, `apply_quota_config` | `free/pool/__init__.py` |
| `free/pool/accounting.py` | 116 | compteurs provider/endpoint (dashboard) | `free/pool/__init__.py`, `dashboard/api.py` |
| `free/pool/rotation.py` | 47 | raisons de blocage de rotation (pur) | `free/pool/__init__.py` |
| `free/pool/config.py` | 65 | `clamp_seconds` | `free/pool/__init__.py` |
| `free/pool/public_pool.py` | 111 | sélection/normalisation des proxys publics | `free/pool/__init__.py` |
| `free/quota_ledger.py` | 864 | ledger `(IP, jour UTC, bucket)` — autorité quota journalier | `free/pool/__init__.py`, `opencode.py`, `free/warp_rotation.py`, scripts |
| `free/ip_quota.py` | 516 | leases `(modèle, IP)` : headroom, quarantaine | `free/pool/__init__.py`, `free/best_ip.py` |
| `free/best_ip.py` | 185 | sélecteur lexicographique quota/latence | `free/pool/__init__.py` |
| `free/selector.py` | 198 | `StationSelector` par score qualité | `free/pool/__init__.py` |
| `free/quality.py` | 633 | registre qualité `(station, IP)` : EWMA, tok/s, slow lane | `free/pool/__init__.py`, `free/probe.py`, `opencode.py`, dashboard |
| `free/station_health.py` | 422 | machine à états santé station + `station_contract` | `free/pool/__init__.py`, `free/best_ip.py` |
| `free/rotation.py` | 952 | registre partagé IP/identité/pays + moteur latence-adaptive | `free/pool/__init__.py`, `vpn/manager.py`, `opencode.py` |
| `free/rotation_planner.py` | 145 | décision de rotation proactive (drain/spare) | `free/pool/__init__.py` |
| `free/reliability_canary.py` | 94 | canary du sélecteur (routage déterministe) | `free/pool/__init__.py` |
| `free/adaptive_weights.py` | 102 | poids egress modulés par les 429 récents | `free/pool/__init__.py` |
| `free/auto_egress.py` | 167 | normalisation providers/poids + SWRR | `free/pool/__init__.py`, `free/capacity.py`, dashboard |
| `free/classify_429.py` | 268 | classification 429 (format/quota/session/upstream) | `opencode.py`, `free/ip_registry/adapters/probe.py` |
| `free/ip_state.py` | 470 | machine à états IP dans `registry.db` | `free/ip_registry/adapters/probe.py`, `scripts/fresh_chain*.py`, `opencode.py` |
| `free/probe_budget.py` | 177 | budget de sondes par bucket | `free/ip_registry/adapters/probe.py`, `free/ip_state.py` |
| `free/client_reserve.py` | 391 | réserve d'IP prouvées 200 (022) | `free/pool/__init__.py`, `dashboard/routers/free_routes.py`, scripts |
| `free/reserve_settings.py` | 177 | réglages GUI de la réserve (table `client_reserve_settings`) | `free/client_reserve.py`, `free/pool/__init__.py`, dashboard |
| `free/provider_capabilities.py` | 100 | capacités par provider (023) | `opencode.py`, `free/ip_registry/adapters/probe.py` |
| `free/wire_profile.py` | 241 | empreinte du wire (fingerprint) | `opencode.py` |
| `free/egress_provider.py` | 114 | helpers bucket/famille d'egress | `free/pool/__init__.py`, `free/warp_rotation.py` |

### 2.2 Egress (WARP / proxys publics / NordVPN)

| Module | LOC | Rôle réel | Appelé par |
|---|---|---|---|
| `free/warp_rotation.py` | 705 | recréation des conteneurs WARP + budget horaire | `free/pool/__init__.py`, `opencode.py` (watchdog) |
| `free/egress_reconcile.py` | 130 | mesure de l'egress live et correction endpoint/config | `free/pool/__init__.py`, `free/warp_rotation.py` |
| `free/egress_probe.py` | 85 | `resolve_egress` via curl_cffi/curl | `free/pool/__init__.py`, `free/warp_rotation.py`, `ops/providers/warp.py` |
| `free/egress_probes.py` | 95 | probe free-gate sur tunnel managé | `opencode.py` |
| `free/egress_validation.py` | 143 | `ProbeOutcome` + parsing reset | `free/egress_adapters/*`, `free/egress_probes.py` |
| `free/egress_catalog.py` | 440 | domaine pur du catalogue (options/observations/policies) | `free/egress_store.py`, `free/egress_adapters/*`, `free/egress_runtime.py` |
| `free/egress_store.py` | 1078 | store SQLite `egress_*` (options, observations, threat intel) | `free/egress_runtime.py`, adapters |
| `free/egress_runtime.py` | 479 | composition root du catalogue + maintenance | `opencode.py` (lifespan) |
| `free/egress_rotation.py` | 145 | sélection d'options (score) | `free/egress_runtime.py`, dashboard |
| `free/egress_sources.py` | 424 | définitions de sources (fichiers/URLs) | `free/egress_adapters/public.py`, `free/egress_runtime.py` |
| `free/egress_scheduler.py` | 185 | boucle de refresh par provider | `free/egress_runtime.py` |
| `free/egress_adapters/{public,nordvpn,warp,base}.py` | 699+217+~90+42 | probes par provider | `free/egress_runtime.py`, scripts |
| `free/public_egress_worker.py` | 559 | worker HTTP isolé (process) pour proxys publics | `free/pool/__init__.py`, `opencode.py` |
| `free/public_egress_container.py` | 239 | variante conteneur du worker | `free/pool/__init__.py` |
| `free/public_proxies.py` | 238 | garde-fous secrets/proxy non fiable | `opencode.py` (chemin requête) |
| `free/warp_provisioner.py` | 218 | provisionnement d'endpoints WARP | `ops/providers/warp.py`, scripts |
| `free/threat_intel.py` | 211 | feeds de réputation IP | `free/egress_runtime.py` |
| `free/host_attribution.py` | 65 | métrique de couverture `server_host` | `opencode.py`, `free/ip_registry/status.py` |
| `free/fallback.py` | 323 | politique de repli egress (007) | `free/pool/__init__.py` (partiel) |
| `free/scan_audit.py` | 297 | journal d'audit des scans chaîne | `scripts/fresh_chain*.py` |

### 2.3 Identités / profils

| Module | LOC | Rôle réel | Appelé par |
|---|---|---|---|
| `free/identities.py` | 776 | vault d'identités par IP (version/UA/ses_) | `free/pool/__init__.py`, `opencode.py` |
| `free/station_identity.py` | 783 | identité par station, prompt templaté, audit | `opencode.py`, `free/runtime_identity.py`, `free/profile_live.py` |
| `free/identity_generator.py` | 235 | génération de profils | `opencode.py`, `free/profile_generator.py`, `free/ip_registry/adapters/probe.py` |
| `free/profile_registry.py` | 604 | profils vérifiés (preuves, TTL) | `free/station_identity.py`, `free/runtime_identity.py`, `free/profile_*` |
| `free/profile_generator.py` | 85 | enveloppe filtrée | (tests + `free/__init__`) |
| `free/profile_live.py` | 121 | requête de vérification P0/P0.5 | `opencode.py` |
| `free/profile_transport.py` | 75 | transport curl_cffi | `opencode.py` |
| `free/profile_verifier.py` | 143 | vérificateur de profil | (tests seulement) |
| `free/runtime_identity.py` | 202 | résolution d'identité runtime | `opencode.py` |
| `free/impersonation.py` | 89 | specs d'impersonation TLS/UA | `opencode.py`, `free/identities.py`, `free/ip_registry/adapters/probe.py` |
| `free/console_auth.py` | 55 | auth console | `opencode.py`, `free/identities.py`, `free/runtime_identity.py` |

### 2.4 Sous-paquets

| Paquet | LOC totales | Rôle réel | Appelé par (prod) |
|---|---|---|---|
| `free/ip_registry/` (+ adapters, sources) | ~4 700 | registre d'IP legacy (`ip_state`, `assignments`), binders, probes | `scripts/fresh_chain*.py`, `scripts/ip_registry_*.py`, `dashboard/routers/free_routes.py` (lecture), `free/qualification/station_adapter.py` |
| `free/qualification/` | ~7 000 | qualification de hosts NordVPN (sweep, watchdogs, rotation batch) | `opencode.py` (lifespan, `QualifierManager`), `dashboard/routers/qualification_routes.py` |
| `free/control_plane/` | ~2 000 | client gluetun control-plane + shadow | `dashboard/api.py`, `dashboard/routers/station_control_routes.py`, `free/ip_registry/adapters/gluetun_control_v2.py` |
| `free/clients/` | ~450 | familles wire des clients (anthropic/openai) | `opencode.py`, `free/wire_profile.py`, `scripts/wire_ab.py` |
| `free/egress_adapters/` | ~1 000 | probes par provider | `free/egress_runtime.py`, `scripts/egress_catalog.py` |

---

## 3. Findings

### P0

#### P0-01 — `repair_owner: supervisor` désarme les rotations sans vérifier que le superviseur tourne

- **Preuve** : `free/pool/__init__.py:6827-6833`
  ```python
  if getattr(self, "_repair_owner", "manager") == "supervisor":
      self._request_repair(station, "rotation-delegated")
      logger.debug(...)
      return
  ```
  `_repair_owner` vient de `_rel_cfg.get("repair_owner", "manager")`
  (`free/pool/__init__.py:770-772`). `config.yaml:289` livre
  `repair_owner: supervisor`. Le pool écrit alors une demande dans
  `logs/registry.db::repair_requests` et **ne rotationne jamais lui-même**.
- **Preuve du risque** : `scripts/fresh_chain_supervisor.py` est un process
  séparé ; le pool sait déjà détecter sa mort (`_fresh_chain_supervisor_alive()`,
  `free/pool/__init__.py:119-152`, utilisée uniquement pour les pins Compose
  l. 6188) mais ne l'interroge **pas** dans `_launch_rotation`.
- **Impact** : superviseur arrêté (crash, poste redémarré, runbook oublié) →
  toute la flotte reste sur ses IP épuisées ; les 429 se répètent, le pool ne
  fait que bad-marker, et `strict_free: true` (`config.yaml:701`) refuse la
  jambe payante → refus client permanent. C'est exactement le scénario « 0/6 »
  documenté.
- **Recommandation** : dans `_launch_rotation`, avant de déléguer, exiger
  `_fresh_chain_supervisor_alive() and not _fresh_chain_supervisor_paused()` ;
  sinon retomber sur la rotation pool (et logguer `rotation_delegated_but_owner_dead`).
  Ajouter un test de non-régression.
- **Effort** : S.

#### P0-02 — Valeurs `time.monotonic()` persistées puis restaurées (parks/cooldowns incohérents après reboot)

- **Preuve** : `free/station_health.py:189` `self.parked_until = ts + seconds` où
  `ts = _now(now)` et `_now` = `time.monotonic()` (l. 33-34) ;
  `export_state` (l. 307-324) persiste `parked_until` / `next_retry_at` ;
  `restore_state` (l. 326-351) les recharge **tels quels** ;
  `free/pool/__init__.py:7720` appelle `self._station_health.restore_state(raw.get("health"))`
  au boot. Idem `free/ip_quota.py:147` `blocked_until`, `_now` monotonic
  (l. 36-37), `export_state` l. 418-437, `restore_state` l. 475,
  restauré par `free/pool/__init__.py:7722`.
- **Preuve sur la prod** : `chain_state/free_pool` contient
  `health["3"]["last_ok_at"] = 96793.921` et `health["2"]["last_ok_at"] = 267636.765`
  alors que `time.monotonic()` valait **269429.5** au moment de la mesure : des
  epochs monotonic d'un boot antérieur, rechargés dans le boot courant. Le
  fichier `free_pool` pèse 1 390 840 octets (état complet, dont 4 096 leases).
- **Impact** : après un reboot (monotonic repart à ~0), un `parked_until` de
  30 min devient « dans 30 min + uptime précédent » — station invisible pendant
  des heures/jours, `station_contract` renvoyant `health=parked` ou
  `health=repairing` ; même effet pour `blocked_until` (`model_usable` →
  `quota-blocked`). Le principe I (disponibilité mesurée) est violé : la
  disponibilité est déclarée par un état périmé.
- **Recommandation** : persister des **durées restantes** (`parked_remaining_s`,
  `blocked_remaining_s`) et les réappliquer à `monotonic()` courant au boot —
  le pattern est déjà utilisé pour le ledger (`retry_in_s`) et pour
  `latency_engine` (`remaining`, `free/rotation.py:722`). Purger au passage
  toute entrée dont la durée restante est incohérente (négative ou > TTL max).
- **Effort** : S (2 sites).

#### P0-03 — `_draining_stations` fuit quand `_launch_rotation` sort sans enfiler → station exclue à vie

- **Preuve** : trois sites ajoutent à l'ensemble **avant** d'appeler
  `_launch_rotation` :
  `free/pool/__init__.py:1852` (`_plan_duplicate_rotation`),
  `l.1957` (`_plan_proactive_rotation`),
  `l.5631` (dual-clutch « approaching quota »).
  Le seul nettoyage est `l.7027` `self._draining_stations.discard(int(sid))`
  dans le `finally` de `_rotate_station` — donc **uniquement si une rotation a
  réellement été exécutée**. Or `_launch_rotation` peut sortir sans rien
  enfiler : `l.6827-6833` (délégation superviseur), `l.6849-6863` (`_blocked` :
  `FROZEN` événement amont, `UNKNOWN` station retirée, `PENDING`/`RUNNING`).
  `_selection_stations` (`l.1985-1995`) exclut alors la station de la sélection
  pour toujours.
- **Impact** : avec `repair_owner: supervisor` (config livrée), le chemin est le
  **cas nominal** : chaque 429 retire la station de la sélection jusqu'au
  prochain `update_config` (qui fait `self._draining_stations.clear()`,
  `l.7502`). Sur une flotte de 6, quelques 429 suffisent à vider la sélection
  alors que les tunnels sont sains. Le dashboard, lui, ne montre pas ce
  drapeau : `get_status()` n'expose pas `_draining_stations` (aucune occurrence
  dans la sortie de `get_status`, l. 8091-8187).
- **Recommandation** : faire retourner un booléen à `_launch_rotation`
  (« enfilée ») et n'ajouter à `_draining_stations` que si `True` ; sinon ne pas
  marquer. Exposer `draining_stations` dans `get_status()`.
- **Effort** : S.

### P1

#### P1-01 — `QuotaLedger._save_sqlite` pollue `egress_ips` avec des clés tronquées et `socks5:*`

- **Preuve** : `free/quota_ledger.py:722-774` — `_save_sqlite` fait, pour chaque
  `BucketState`, un `INSERT OR IGNORE INTO egress_ips(ip, first_seen_at,
  last_seen_at, state) VALUES(?,?,?,'fresh')` où `state.ip` est la clé
  `server_ip_key()` (tronquée : IPv6 → 4 premiers groupes, `quota_ledger.py:39-62`),
  puis un upsert `quota_buckets`.
- **Preuve sur la prod** : `SELECT COUNT(*) FROM egress_ips` = 6 557 dont
  `('socks5:127.0.0.1:11080','fresh')`, `('socks5:warp-ipv6-3','fresh')`,
  `('socks5:direct','fresh')` (10 lignes `socks5:*`) et des IPv6 tronquées
  (`2a09:bac5:32d4:be`, `240e:314:1f24:1248`, `2a0c:5d00:2002:`), dont la plus
  récente a 0,6 jour. Le schéma canonique
  (`rotation/infrastructure/db/schema.sql:155`) déclare
  `quota_buckets.ip REFERENCES egress_ips(ip) ON DELETE CASCADE` : ces lignes
  entrent dans la table de référence par le ledger.
- **Impact** : violation du principe III (une seule source de vérité ; les
  clés tronquées ne sont pas des IP) et V (une IP d'egress n'appartient qu'à
  une station — ici des labels logiques deviennent des « IP »). Le
  `DELETE FROM quota_buckets WHERE ip LIKE 'socks5:%'` (l. 673) purge le ledger
  mais **pas** `egress_ips` : 10 lignes y subsistent. Risque de collision de
  clé : `2a0c:5d00:2002:` (préfixe tronqué avec un `:` final) peut ne
  correspondre à aucune IP réelle et fausser `assigned_for_station` /
  `release_station` (`free/ip_registry/registry_db.py:127` met à jour
  `egress_ips.state` par `ip=?`).
- **Recommandation** : ne jamais écrire dans `egress_ips` depuis le ledger
  (la table appartient au registre) ; si le ledger a besoin d'une FK, créer une
  table dédiée (`quota_ips`) ou écrire la clé de bucket non tronquée
  (`quota_block`), et purger les 10 lignes `socks5:*` + les clés tronquées en
  migration.
- **Effort** : M.

#### P1-02 — `_launch_rotation` délégué au superviseur ne persiste aucun fallback ; boucle de réparation non bornée côté pool

- **Preuve** : `free/pool/__init__.py:6014-6041` `_request_repair` ouvre un
  `RepairQueue` **par appel** (`queue = RepairQueue()` l. 6029) — chaque appel
  ouvre une connexion SQLite sur `logs/registry.db`, avec `_conn` conservé
  jusqu'au garbage collector (`free/repair_queue.py:24-28`, pas de `close()`
  appelé). `_request_repair` est appelé à chaque 429 quota
  (`on_quota_exhausted`, l. 6130) et à chaque `_launch_rotation` délégué
  (l. 6828).
- **Impact** : sous rafale de 429 (cas réel : `quota_buckets` = 7 570 lignes
  `last_status=429` en base), des dizaines de connexions SQLite concurrentes
  sur la même base, non fermées ; risque de `database is locked` (timeout 10 s,
  `repair_queue.py:26`) et de fuite de descripteurs. Les `request()` sont
  idempotents (PK station), donc pas de doublon, mais le coût I/O est réel.
- **Recommandation** : instancier `RepairQueue` une fois par pool
  (`self._repair_queue_store = RepairQueue()` dans `__init__`), la fermer dans
  `close()` ; ne pas rouvrir une connexion par 429.
- **Effort** : S.

#### P1-03 — `QuotaLedger._save_sqlite` réécrit tous les buckets à chaque flush (coût O(n) par écriture)

- **Preuve** : `free/quota_ledger.py:722-774` — `states = list(self._items.values())`
  puis une boucle `INSERT OR IGNORE` + `INSERT ... ON CONFLICT DO UPDATE` par
  état, sur une connexion neuve. `_maybe_save` est appelé à **chaque**
  `note_request` (l. 401, intervalle 2 s, `save_interval_s=2.0` l. 308) et à
  chaque `note_result` (l. 469). Mesure prod : `quota_buckets` = **8 519**
  lignes, et `_items` en mémoire est borné à `max_entries=8192` (l. 306) →
  jusqu'à ~16 000 instructions SQL toutes les 2 secondes en charge.
- **Impact** : contention SQLite sur `registry.db` (167 Mo, partagée avec le
  catalogue egress, la qualification et les scripts chaîne), latence du chemin
  de requête (le flush est synchrone dans le thread appelant), WAL qui gonfle
  (`registry.db-wal` = 5,5 Mo à l'arrêt mesuré).
- **Recommandation** : n'écrire que les buckets **sales** (marquer les clés
  modifiées dans `note_request`/`note_result`), ou passer à un flush
  asynchrone par lot ; à défaut, augmenter `save_interval_s` et flusher sur
  `close()`/arrêt.
- **Effort** : M.

#### P1-04 — Aucun `flush()` du ledger au shutdown du pool

- **Preuve** : `free/pool/__init__.py:6657-6724` `close()` annule workers,
  tâches publiques, pin-escapes et vide la file — il **ne touche pas**
  `self._ledger`. `grep -rn "ledger.flush\|\.flush()" free/ opencode.py` ne
  trouve que `stream.flush()` (`opencode.py:33446`) ; les seuls appels à
  `ledger.flush()` sont dans `scripts/migrate_quota_ledger.py` et des scripts
  de test.
- **Impact** : jusqu'à `save_interval_s` (2 s) de pertes — faible en volume,
  mais le dernier `note_result` d'un 429 peut ne jamais être persisté si le
  process meurt dans la fenêtre, et un 200 « force » (l. 468) est sauf. Le
  risque réel est un arrêt brutal pendant une rotation/rafale : quelques
  buckets redeviennent `unproven` au boot suivant et une IP épuisée est
  re-testée.
- **Recommandation** : `self._get_ledger().flush()` (fail-soft) dans `close()`
  et dans le lifespan opencode après `save_pool_state(True)`
  (`opencode.py:4333`).
- **Effort** : S.

#### P1-05 — `free/ip_state.py` partage la table `ip_state` de `registry.db` avec le schéma legacy de `free/ip_registry/sqlite.py` (schémas incompatibles)

- **Preuve** : `free/ip_state.py:48-65` crée
  `ip_state(provider, ip, bucket_key, state, wire_fp, proven_at, serving_at,
  reset_at, burned_at, reason, evidence_ref, probe_count, updated_at,
  PRIMARY KEY (provider, ip))`. `free/ip_registry/sqlite.py:58-91` crée
  `ip_state(egress_ip PRIMARY KEY, hostname, country, state, score, last_status,
  last_tested_at, last_ok_at, reset_at, error_until, latency_ms, tests,
  successes, failures, assigned_station, ...)` + index partiels uniques. Les
  deux utilisent `registry_db_path()` / `logs/registry.db`
  (`free/ip_registry/registry_db.py:58-59`, `free/ip_state.py:173-177`).
- **Preuve sur la prod** : `PRAGMA table_info(ip_state)` renvoie les colonnes de
  **`free/ip_state.py`** (`provider, ip, bucket_key, wire_fp, …`, 283 lignes) —
  donc `SqliteIpRegistry` (legacy) échouerait sur cette base
  (`SELECT * FROM ip_state WHERE egress_ip=?` → colonne inexistante), et
  inversement si le legacy s'initialisait d'abord. `logs/ip_registry.db`
  (chemin configuré `ip_registry.db_path`) existe mais pèse **0 octet**.
- **Impact** : violation du principe III (deux définitions d'une même table dans
  une même base). Selon l'ordre d'initialisation, l'un des deux registres
  casse silencieusement (`except Exception` autour de `store.get` →
  `free/ip_registry/adapters/probe.py:110-134`, le probe passe alors sans
  vérifier le budget). `scripts/ip_registry_observe.py` et
  `scripts/ip_registry_discover.py` écrivent dans `logs/ip_registry.db`
  (0 octet) : leur travail n'est jamais lu par le proxy.
- **Recommandation** : renommer la table legacy (`ip_state_legacy`) dans
  `free/ip_registry/sqlite.py` + migration, ou déplacer le legacy dans sa
  propre base (`logs/ip_registry.db`, déjà configuré) et faire lire
  `SqliteIpRegistry` ce chemin.
- **Effort** : M.

#### P1-06 — `on_request` : un `logger.info` par requête en mode socks5/auto_egress

- **Preuve** : `free/pool/__init__.py:5388-5392`
  ```python
  logger.info("[warp-rotate] on_request enabled=%s mode=%s", ...)
  self.rotate_exhausted_managed()
  ```
  appelé à **chaque** `on_request` quand `proxy_mode in ("socks5","auto_egress")`
  — et `config.yaml` livre `proxy_mode: vpn` mais aussi des proxys publics
  actifs et `auto_egress` possible via la GUI. Le logger `free.pool` est
  attaché à `debug.log` (`opencode.py:664,668`).
- **Impact** : une ligne INFO par requête client, plus le `rotate_exhausted_managed()`
  complet (scan de tous les endpoints managés, `free/pool/__init__.py:2897-3111`)
  exécuté **sur le chemin de requête** — il fait des `logger.debug` par
  endpoint (l. 3012-3023) et des accès ledger. Sous charge, coût CPU + I/O
  disque sur le chemin critique.
- **Recommandation** : passer la ligne en `logger.debug` ; déplacer
  `rotate_exhausted_managed()` dans la boucle watchdog (elle existe déjà,
  `opencode.py:3186-3190`, appelée toutes les 30 s).
- **Effort** : S.

#### P1-07 — `_persist_socks5_egress` réécrit `config.yaml` entier depuis un thread de rotation

- **Preuve** : `free/pool/__init__.py:3117-3131` appelle
  `yaml_set("ip_rotation", "socks5_proxies", self._socks5_proxies)` ;
  `config/settings.py:430-435` `yaml_set` → `save_yaml_config()` →
  `_store.transaction(CONFIG_PATH)` + `_store.write_yaml(...)` (l. 322-356),
  qui relit le fichier disque, fusionne et réécrit **tout** `config.yaml`
  (36 725 octets). Appelé depuis `WarpRotator._run` (thread `warp-rotate-*`,
  `free/warp_rotation.py:263-269`) et depuis `reconcile_warp_ipv4`
  (`free/pool/__init__.py:4993`).
- **Impact** : verrou de config pris depuis un thread de rotation ; sous
  rotations concurrentes (jusqu'à `max_rotations_per_hour`), réécritures
  répétées du fichier de configuration partagé avec le dashboard et les
  scripts. Un crash au milieu laisse un `config.yaml` partiel (mitigé par
  tmp+replace côté `store.write_yaml`). Le commentaire de `free/pool/quota.py`
  et le docstring de `free/reserve_settings.py:1-9` documentent déjà que
  « la GUI ne peut pas écrire config.yaml : le runtime réécrit ce fichier et
  perd les clés hors schéma » — le même risque s'applique ici.
- **Recommandation** : persister l'egress résolu dans `registry.db`
  (`egress_ips.egress_ip`) plutôt que dans `config.yaml`, ou batcher les
  écritures (un seul `yaml_set` par scan, pas par endpoint).
- **Effort** : M.

#### P1-08 — `get_status()` : coût O(stations × modèles) par appel, appelé par le dashboard

- **Preuve** : `free/pool/__init__.py:7763-8187` — pour chaque station, la
  boucle `for _m in _probe_models: self.model_usable(_m, st)` (l. 7815-7820)
  puis `self.model_usable(_model, st)` (l. 7804), chacun appelant
  `_station_usable` (scan des autres stations pour la déduplication IP,
  l. 3213-3221) + `_ledger_usable` + `_quota.model_usable` + `_station_health.get`.
  `_probe_models` vient de `ip_registry.allocator.probe_models` = 3 modèles
  (`config.yaml:1316-1319`). Appelé par `dashboard/routers/pool_routes.py:86` et
  `dashboard/routers/vpn_status_routes.py:96`.
- **Impact** : ~6 stations × (1 + 3) `model_usable` × O(stations) = ~150
  vérifications par rafraîchissement, plus `_wire_snapshot_for` (lecture du
  vault identités, l. 7751-7761) par station. Le dashboard peut rafraîchir
  plusieurs fois par seconde (`status_cache_seconds: 5.0` atténue, mais
  `/api/pool-status` et `/api/vpn-status` sont deux endpoints distincts qui
  l'appellent chacun).
- **Recommandation** : mémoïser par `(station, model, ip, compteur)` avec
  invalidation sur note_*; ou déplacer le calcul `model_usable_by_model` dans
  un endpoint dédié plus rare.
- **Effort** : M.

#### P1-09 — `fallback.py` : la politique 007 est appelée sur un seul chemin et 5 clés sur 8 sont inertes

- **Preuve** : le seul appel de production à `evaluate` est
  `free/pool/__init__.py:3085-3095` (cause `exhausted` dans
  `rotate_exhausted_managed`). `note_probe` (retour vers WARP),
  `evaluate_error` (mapping taxonomie) et `active_provider` n'ont **aucun**
  appelant hors tests (`grep` : seuls `free/fallback.py` et
  `tests/test_warp_rotation.py:426`). Les clés `probe_interval_s`,
  `recovery_successes`, `cooldown_s`, `max_block_s`, `fail_closed` sont lues
  dans `FallbackConfig.from_mapping` (l. 92-108) mais ne sont consommées nulle
  part : la seule décision appliquée est `action == "fallback"` → `continue`
  (pas de rotation), sans jamais revenir ni suivre un probe.
- **Impact** : violation du principe XII (toute option GUI a un effet réel) ;
  l'opérateur qui règle `recovery_successes`/`cooldown_s` n'obtient aucun
  changement. `max_fallback_stations` est bien appliqué, mais le retour
  automatique vers WARP n'existe pas — une station passée en « fallback » ne
  revient que par un autre chemin (rotation quota/health).
- **Recommandation** : soit brancher `note_probe` sur les probes egress réels
  (la boucle existe : `opencode.py:24881+ _station_egress_probe_loop`), soit
  retirer les clés mortes de `config.yaml` et documenter la portée réelle.
- **Effort** : M.

#### P1-10 — `free/pool/__init__.py` : 8 362 lignes, monolithe non stranglé (principe XIV)

- **Preuve** : `wc -l free/pool/__init__.py` = 8 362. Les modules extraits
  (`selection`, `quota`, `accounting`, `rotation`, `config`, `public_pool`)
  totalisent 604 lignes, soit **7 %** du monolithe. La constitution
  (`.specify/memory/constitution.md:131`) plafonne à 1500 lignes.
- **Impact** : la classe `FreeIPPool` porte 8 responsabilités (sélection,
  quota/ledger, rotations, SOCKS5/WARP, public proxies, statut, persistance,
  santé) ; chaque correctif touche un fichier de 8 000 lignes, les `except
  Exception` y sont 338. Le risque de régression est maximal sur le chemin
  client.
- **Recommandation** : poursuivre le strangler dans cet ordre (dépendances les
  plus faibles d'abord) : (1) `_socks5_*` (≈ 700 l.) → `free/pool/socks5.py` ;
  (2) `_public_*`/`configure_public_proxies` (≈ 400 l.) →
  `free/pool/public_proxies_ctl.py` ; (3) `_ledger_*`/`quota_bucket_*` (≈ 200 l.)
  → `free/pool/ledger_bridge.py` ; (4) `_model_ip_*`/`_model_slot*` (≈ 400 l.)
  → `free/pool/model_health.py` ; (5) `_launch_rotation`/`_rotate_station`
  (≈ 300 l.) → `free/pool/rotation_runner.py`.
- **Effort** : L.

#### P1-11 — `registry.db` : `quota_buckets` (8 519 lignes) et `egress_observations` (326 812) jamais purgées par la rétention

- **Preuve** : `rotation/infrastructure/db/retention.py:29-33` ne purge que
  `observations`, `fallback_events`, `incidents`, `repair_jobs`,
  `config_snapshots`. `grep -c "quota_buckets" retention.py` = 0 (idem
  `egress_observations`, `egress_options`, `ip_state_events`, `probe_budget`,
  `scan_attempts`, `rotation_jobs`). `free/egress_store.py:930-944` purge ses
  propres tables (30 j pour les DEAD, retention_days pour les observations) —
  donc `egress_observations` est purgée par le store, mais **pas**
  `quota_buckets`.
- **Preuve volume** : `registry.db` = 167 133 184 octets ; 326 812
  `egress_observations` sur ~7,7 jours (min 1794507550, max 1795175591) ;
  31 900 `egress_options` ; 8 519 `quota_buckets` réparties sur ≥ 8 jours
  (20261113→20261120) ; 6 557 `egress_ips`.
- **Impact** : violation du principe XIII (données traçables **et**
  sauvegardées — ici la rétention manque). Le ledger en mémoire, lui, prune à
  `max_age_days=3` (l. 704-713), donc la base conserve des buckets que la
  mémoire ignore : divergence croissante, et `_load_sqlite` les recharge tous
  (l. 629-678) avant de les pruner — coût de boot croissant.
- **Recommandation** : ajouter `quota_buckets` (par `last_seen_at`, 3-7 j) et
  `ip_state_events`/`probe_budget`/`rotation_jobs`/`scan_attempts` à
  `retention.prune` ; aligner la purge SQL du ledger sur `max_age_days`.
- **Effort** : S.

#### P1-12 — `free/ip_registry/status.py::registry_status` charge le catalogue complet à chaque appel

- **Preuve** : `free/ip_registry/status.py:320-347` construit
  `build_default_registry(...)` (lecture manifest + embedded + cache NordVPN)
  puis `_allocator_summary` (l. 221-304) fait, **par station** (6), une
  recherche de locks et 4 `load_state` (chaque `load_state` →
  `load_json_state` → connexion SQLite `registry.db`, `free/chain_state.py:41-54`),
  soit 24 connexions + lectures par appel. `_db_summary` ouvre une seconde
  connexion `mode=ro`.
- **Impact** : endpoint `/api/free/ip-registry/status` (dashboard) coûteux ;
  les 24 lectures `chain_state` portent sur des documents de ~97 Ko
  (`chain_state` : `fresh_chain_state_3` = 96 601 octets,
  `free_pool` = 1 390 840 octets).
- **Recommandation** : ne lire que le namespace pertinent par station (un seul
  `SELECT ... WHERE namespace IN (...)`), mettre en cache 5-10 s côté route.
- **Effort** : S.

#### P1-13 — `except Exception` en cascade dans le chemin de sélection : les bugs deviennent des non-décisions silencieuses

- **Preuve** : 634 `except Exception` dans `free/`, dont 231 `pass`
  (comptage `grep -rn -A1 'except.*:$' free/ | grep -c 'pass$'`).
  Exemples à fort impact :
  `free/pool/__init__.py:3562-3584` (le sélecteur `_best_ip` échoue → repli
  silencieux sur le legacy), `l.1209-1218` (`usable_station_count` avale et
  retourne un compte partiel), `l.3199-3204` (`_station_usable` : un échec du
  contrat strict retourne `False` sans trace), `free/pool/selection.py:42-45`
  (un `sort_key` qui lève → ordre d'entrée non trié, silencieux).
- **Impact** : violation du principe XVII (fiabilité mesurable) — impossible de
  distinguer « station non utilisable » de « bug dans le calcul ». En cas de
  régression, le pool sert une station non optimale sans aucun signal.
- **Recommandation** : dans les 20 sites du chemin chaud (sélection, quota,
  ledger), remplacer `except Exception: pass` par un `except Exception` qui
  incrémente un compteur `self._selection_errors` exposé dans `get_status()` et
  loggue en `warning` throttlé.
- **Effort** : M.

#### P1-14 — `station_health.observe` sur un bon probe lève un park egress par effet de bord

- **Preuve** : `free/station_health.py:285-292`
  ```python
  def observe(self, station_id, ok, now=None, **kw):
      item = self.get(station_id)
      state = item.observe_probe(ok, now, **kw)
      if ok:
          item.clear_egress_park(now)
          state = item.state
  ```
  `clear_egress_park` (l. 233-244) appelle `repair_succeeded()` qui remet
  `good_probes=0` **et** `_transition(PROBING)`. Or `_note_health_probe` est
  appelé avec `ok=True` à chaque 200 client
  (`free/pool/__init__.py:2185`) **après** `mark_station_repaired`… mais aussi
  depuis `note_station_probe` (watchdog) et `_rotate_station`
  (`l.6960`). Un seul 200 sur une station parkée la sort du park — y compris un
  park **auth** ? Non : `clear_egress_park` refuse si la cause contient
  « auth »/« credential » (l. 225-231). Mais un park `cause="deadline 240s"`
  avec `last_error="egress"` est levé par **n'importe quel** probe OK, y
  compris un probe qui ne prouve pas l'egress réel.
- **Impact** : l'état peut osciller `parked → probing → usable` sur des probes
  partiels ; couplé à P0-02 (parked_until monotonic), l'interaction est
  difficile à raisonner. Risque : une station auth-parkée par un autre chemin
  (cause absente) est rouverte.
- **Recommandation** : exiger que `clear_egress_park` vérifie aussi
  `last_error`/`cause` explicitement egress (déjà partiel) et documenter que
  seuls les probes **egress** (pas les 200 client) peuvent lever un park ;
  ou séparer `observe(ok, kind="client"|"egress")`.
- **Effort** : S.

### P2

#### P2-01 — `_rotation_threshold` relit `_yaml_get` à chaque appel (7 sites, chemin chaud)

- **Preuve** : `free/pool/__init__.py:1781` `_profile = _yaml_get("ip_rotation",
  "quota_per_ip_by_model", {}) or {}` ; `_rotation_threshold` est appelé 7 fois
  dans le fichier (dont `on_request` l. 5541/5548, `_station_usable` l. 3339).
  `_yaml_get` fait un lookup dict sur `_yaml_data` (pas d'I/O) mais refait le
  parsing `int()`, les `try/except` et la normalisation à chaque appel, jusqu'à
  4 fois par requête.
- **Impact** : coût CPU mineur mais répété ; surtout, la valeur peut changer
  entre deux appels du même `on_request` (hot-reload concurrent) → décision
  incohérente dans la même requête (l. 5541 compare, l. 5548 recalcule).
- **Recommandation** : lire le seuil une fois par `on_request` et passer la
  valeur ; ou précalculer dans `update_config`.
- **Effort** : S.

#### P2-02 — `acquire_model_slot` retourne `None` silencieusement sur toute exception non-Timeout

- **Preuve** : `free/pool/__init__.py:2488-2489`
  ```python
  except Exception:
      return None
  ```
  Le fail-closed (P0 admission 2026-11-10) ne couvre que `TimeoutError` ; une
  `CancelledError` n'est pas attrapée (OK), mais toute autre erreur (ex.
  `_model_slot` sur un dict corrompu) retourne `None` → `release_model_slot(None)`
  no-op et **la requête passe sans limite de concurrence** (fail-open
  silencieux, exactement ce que le correctif P0 voulait interdire).
- **Impact** : sous un bug non anticipé, retour au comportement « 160 flux sur
  2 IP ».
- **Recommandation** : dans le `except Exception`, logger en `warning` et lever
  `ModelSlotTimeout` (fail-closed) plutôt que retourner `None`.
- **Effort** : S.

#### P2-03 — `note_model_ip` : le blocage modèle-global n'est pas persisté et un `return` précoce saute l'écriture

- **Preuve** : `free/pool/__init__.py:2257-2268` :
  ```python
  if len(distinct) >= max(2, min_ips):
      try:
          if getattr(self, "_stations", None) and not self.all_usable_buckets_exhausted(mid):
              return
      except Exception:
          pass
      self._model_global_until[mid] = now + max(1.0, cooldown)
  ```
  Le `return` sort de `note_model_ip` **avant** la pose du cooldown (voulu) mais
  aussi après que `st["blocked_until"]` a été posé (l. 2228) — cohérent. En
  revanche `_model_global_until` vit uniquement en mémoire
  (`l. 744`) : un redémarrage l'efface, alors que `clear_model_global` existe
  (`l. 2341`) et que le ledger, lui, persiste. `note_model_global_cooldown`
  (l. 2283) est appelé par `opencode.py` avec un `Retry-After` pouvant aller
  jusqu'à 24 h (borné l. 2297).
- **Impact** : après un restart pendant un backoff global de plusieurs heures,
  le proxy relance immédiatement des rotations/AUTH que le provider venait de
  refuser — le churn que le correctif 2026-11-01 voulait arrêter.
- **Recommandation** : persister `_model_global_until` (durées restantes) dans
  le state du pool, comme `bad_remaining`.
- **Effort** : S.

#### P2-04 — `all_usable_buckets_exhausted` appelé dans `note_model_ip` : scan complet à chaque 429

- **Preuve** : `free/pool/__init__.py:2264` appelle
  `all_usable_buckets_exhausted(mid)` (l. 1576-1613) qui parcourt toutes les
  stations + tous les endpoints SOCKS5 et interroge le ledger pour chacun. Ce
  chemin s'exécute à chaque 429 où `len(distinct) >= min_ips`.
- **Impact** : sous vague de 429 (fréquent : 7 570 buckets `last_status=429` en
  base), N × (stations + endpoints) accès ledger ; le ledger est en mémoire
  (verrou RLock) donc coût CPU, pas I/O.
- **Recommandation** : mémoïser le résultat par `(model, fenêtre 5 s)`.
- **Effort** : S.

#### P2-05 — `free/ip_state.py` : `_repair_invalid_burns` s'exécute à **chaque** instanciation de `IPStateStore`

- **Preuve** : `free/ip_state.py:181-198` — dans `__init__`, après création du
  schéma, `repaired = self._repair_invalid_burns(conn)` qui fait un `SELECT` sur
  `state='burned'` puis des `UPDATE`/`INSERT` par ligne, **à chaque** création
  d'un store. `store()` (l. 458-464) est un singleton, mais
  `scripts/fresh_chain.py:1561` et `:2446` instancient
  `IPStateStore(claims_db)` explicitement, et `free/ip_registry/adapters/probe.py:113`
  appelle `_ip_state_mod.store()` (singleton, donc une fois).
- **Impact** : sur 283 lignes `ip_state` dont 167 `unproven` (post-réparation),
  le scan est petit aujourd'hui ; mais la réparation n'est idempotente que
  parce que les raisons ciblées disparaissent — si un futur chemin réécrit
  `reason='unclassified 429…'`, chaque instanciation re-`UPDATE`. Coût de
  maintenance et surprise (effet de bord d'un constructeur).
- **Recommandation** : déplacer la réparation dans une migration explicite
  (script one-shot) ou la garder derrière une clé `egress_catalog_meta`.
- **Effort** : S.

#### P2-06 — `IPStateStore.transition` : read-modify-write non atomique entre `get()` et l'UPSERT

- **Preuve** : `free/ip_state.py:328-390` — `self._lock` (RLock **du process**)
  protège `current = self.get(...)` puis l'`INSERT ... ON CONFLICT DO UPDATE`.
  Deux process (proxy + superviseur + fresh_chain) partagent `registry.db` :
  le verrou Python ne les sérialise pas. Le schéma n'a pas de garde
  d'optimistic concurrency (pas de `WHERE state=...` dans l'UPSERT).
- **Impact** : lost update possible — ex. le superviseur écrit `spent`, le
  proxy écrit `proven` depuis un `current` lu avant → l'état final dépend de
  l'ordre. Le principe IV (reset = barrière dure) peut être contourné si un
  `proven` écrase un `spent` avec `reset_at` futur.
- **Recommandation** : ajouter `AND state=?` (état lu) à l'UPSERT et retenter
  en cas de `rowcount=0`, ou passer par `BEGIN IMMEDIATE` + relecture.
- **Effort** : M.

#### P2-07 — `free/quota_ledger.py::_load_sqlite` recharge **tous** les jours avant de pruner

- **Preuve** : `free/quota_ledger.py:629-667` : `SELECT ... FROM quota_buckets`
  **sans filtre de jour**, boucle sur toutes les lignes, puis
  `_enforce_cap_locked()` + `_prune_locked(now)`. Mesure : 8 519 lignes sur
  ≥ 8 jours ; la mémoire n'en garde que `max_age_days=3`.
- **Impact** : travail inutile au boot (8 519 constructions d'objets + dict
  insertions) pour en jeter ~60 % ; dégradation linéaire avec l'âge de la base
  (P1-11 non corrigé).
- **Recommandation** : filtrer `WHERE day >= ?` (calculé depuis `max_age_days`)
  dans la requête.
- **Effort** : S.

#### P2-08 — `_public_refresh_loop` : `asyncio.sleep(interval)` avant la première exécution, intervalle minimum 300 s non appliqué au démarrage

- **Preuve** : `free/pool/__init__.py:4655-4665` — la boucle dort
  `max(300.0, refresh_interval_s)` (défaut 21 600 s) **avant** la première
  revalidation ; le premier refresh vient donc au mieux 5 min après le boot,
  au pire 6 h.
- **Impact** : après un redémarrage, les endpoints publics restent sur leur
  preuve de la veille jusqu'à 6 h ; `_seed_public_ledger_proofs` (l. 4461) ne
  rejoue que les preuves du **jour UTC courant** (l. 4498-4499), donc après
  00:00 UTC sans refresh, tous les publics sont `unproven` → provider exclu.
  Le correctif [013] documente exactement ce verrou circulaire.
- **Recommandation** : exécuter une première passe immédiate (ou après 30 s) au
  boot avant d'entrer dans la boucle.
- **Effort** : S.

#### P2-09 — `free/egress_store.py` : `_conn` unique partagée `check_same_thread=False` sans `close()` garanti

- **Preuve** : `free/egress_store.py:177-183` — une connexion unique
  `check_same_thread=False` + `RLock`. `close()` existe (l. 218-220) et est
  appelé par `EgressRuntime.close()` (`free/egress_runtime.py:97`). En cas
  d'exception au boot entre `build_runtime` et l'enregistrement dans
  `shared_state` (`opencode.py:4208-4220`), la connexion reste ouverte.
  Surtout : `free/egress_runtime.py:446-448` ferme le store si `not providers`
  — mais si `start()` échoue ensuite, `close()` n'est jamais appelé.
- **Impact** : fuite de descripteur SQLite (1 seule, faible) ; mais surtout
  `EgressStore` est instanciée sur `registry.db` (167 Mo) et son `executescript`
  de schéma s'exécute à chaque création (`l. 185`).
- **Recommandation** : `try/finally` autour du démarrage dans `opencode.py`,
  ou context manager sur `EgressRuntime`.
- **Effort** : S.

#### P2-10 — `free/warp_rotation.py` : `INFLIGHT`/`LAST_ROTATION`/`ROTATION_TIMES` sont des globals de module (état partagé entre instances de pool)

- **Preuve** : `free/warp_rotation.py:34-38` :
  ```python
  INFLIGHT: set[str] = set()
  LAST_ROTATION: dict[str, float] = {}
  ROTATION_TIMES: dict[str, list[float]] = {}
  ```
  `request_rotation` (l. 229-262) les lit/écrit sous `_LOCK`. `INFLIGHT` est
  nettoyé dans le `finally` de `_run` (l. 348-351) ; `ROTATION_TIMES` n'est
  purgé que si `budget_store is None` (l. 236-240, 260) — avec le store (cas
  nominal, `RotationBudgetStore`), la liste n'est jamais alimentée mais
  `LAST_ROTATION` grossit d'une entrée par conteneur (borné en pratique par le
  nombre d'endpoints).
- **Impact** : faible en mémoire ; le vrai problème est l'état de module non
  réinitialisable : deux `FreeIPPool` dans le même process (tests, doubles)
  partagent les cooldowns de rotation — et `INFLIGHT` peut rester bloqué si le
  thread meurt entre `add` (l. 262) et le `finally` (l. 348) sans que
  `_run` démarre.
- **Recommandation** : déplacer l'état dans `WarpRotator` (instance), ou
  garantir la libération de `INFLIGHT` par un `try/except` autour de
  `thread.start()`.
- **Effort** : S.

#### P2-11 — `free/public_egress_worker.py::start_worker_process` bloque jusqu'à 8 s (sleep 50 ms en boucle) dans le thread appelant

- **Preuve** : `free/public_egress_worker.py:498-524` — boucle
  `while time.monotonic() < deadline: ... time.sleep(0.05)`, `timeout_s=8.0`.
  Appelé par `free/pool/__init__.py:4390` (`start_worker_process(upstreams)`)
  depuis `set_public_proxies`, lui-même appelé par `configure_public_proxies`
  → `update_config` (l. 7390-7394) qui s'exécute dans le thread de l'event
  loop (hot-reload dashboard).
- **Impact** : blocage de l'event loop jusqu'à 8 s lors d'un changement de
  config `public_proxies` ou d'une revalidation
  (`revalidate_public_proxies` → `set_public_proxies`, l. 4634) — toutes les
  requêtes en vol sont gelées.
- **Recommandation** : appeler via `asyncio.to_thread` (le chemin
  `configure_public_proxies` est déjà appelé depuis `update_config` synchrone :
  envelopper l'appel), ou rendre le démarrage asynchrone (attendre le port dans
  une tâche).
- **Effort** : S.

#### P2-12 — `free/pool/__init__.py::is_watchdog_cancelled` : scan linéaire de tous les `_per` à chaque erreur réseau

- **Preuve** : `free/pool/__init__.py:6608-6618` — `any(task in (...) for per in
  self._per.values())` ; appelé depuis le classifieur d'erreurs du chemin de
  requête (`opencode.py`, `_is_watchdog_cancelled`, cf. `.audit-logs-wt/opencode.py:6000-6011`
  pour la version antérieure ; la version courante est en `opencode.py`).
  `_per` contient une entrée par station Docker **et** par endpoint SOCKS5
  (25 endpoints configurés + 6 stations = 31+ entrées).
- **Impact** : O(31) par erreur réseau ; négligeable en valeur absolue mais
  inutilement non borné (le set `watchdog_cancelled` vit dans `_per` d'une
  seule station — il pourrait être stocké dans un `WeakSet` global du pool).
- **Recommandation** : stocker `watchdog_cancelled` au niveau du pool (un seul
  `WeakSet`) au lieu de par station.
- **Effort** : S.

#### P2-13 — `free/station_health.py::station_contract` : `egress-stale` à 900 s rend une station saine « non routable » sur flotte inactive

- **Preuve** : `free/station_health.py:397-408` — si `last_ok > 0` et
  `ts - last_ok > max_effective_age_s` → `(False, "egress-stale")`.
  `config.yaml:669` livre `max_effective_age_s: 900` et `strict: true`.
  `_max_effective_age_s` est forcé à ≥ 180 s
  (`free/pool/__init__.py:92,764-767`). Le probe de fraîcheur est la boucle
  `_station_egress_probe_loop` (`opencode.py:24881+`, intervalle
  `station_health.probe_interval_s: 120`).
- **Impact** : si la boucle de probe est en retard (>900 s, ex. event loop
  bloqué par P2-11 ou par une rotation Docker), toutes les stations basculent
  `egress-stale` **simultanément** — le pool retombe sur `None` alors que les
  tunnels répondent. Le commentaire du code reconnaît le cas (« an idle
  station whose watchdog tick is delayed past max_effective_age_s is falsely
  marked egress-stale ») mais la parade (`health.last_ok_at`) ne couvre que les
  probes réussis.
- **Recommandation** : ne pas appliquer `egress-stale` quand aucune requête
  n'est en cours (fleet idle) ou élargir la fenêtre proportionnellement à
  l'intervalle de probe (≥ 3× `probe_interval_s`).
- **Effort** : S.

#### P2-14 — `free/client_reserve.py` : cache 2 s partagé, `_CACHE` global non protégé par verrou

- **Preuve** : `free/client_reserve.py:86-87` `_CACHE: tuple[float, set[str]] | None`,
  lu/écrit sans verrou (`reserved_ips` l. 220-238, `_invalidate` l. 150-152).
  Idem `free/reserve_settings.py:49` `_CACHE`. Appelé depuis
  `_reserve_allows` (`free/pool/__init__.py:184-191`) dans `_station_usable`
  (l. 3235) — chemin chaud multi-thread (event loop + `asyncio.to_thread`).
- **Impact** : en CPython, l'affectation d'un tuple est atomique, donc pas de
  corruption ; mais une lecture peut voir l'ancien cache après invalidation
  (fenêtre 2 s), et `reserved_ips` ouvre une connexion SQLite
  (`_connect` l. 90-91) à chaque miss. Sous charge, jusqu'à 1 connexion/2 s
  depuis le chemin de sélection.
- **Recommandation** : documenter l'atomicité voulue ; ou protéger par le même
  `RLock` que le pool et précharger la réserve en tâche de fond.
- **Effort** : S.

#### P2-15 — `free/ip_quota.py::lease_for` évince l'entrée la plus ancienne par ordre d'insertion, pas par usage

- **Preuve** : `free/ip_quota.py:327-328`
  ```python
  while len(self._items) > self._max_entries:
      self._items.pop(next(iter(self._items)))
  ```
  `max_entries=4096`. Mesure : le state persisté contient **exactement 4 096**
  leases (`chain_state/free_pool` → `quota`), donc la borne est atteinte en
  production et l'éviction est active. `next(iter(dict))` est l'insertion la
  plus ancienne (Python 3.7+), pas la moins récemment utilisée : une IP
  active mais insérée tôt peut être évincée avant une IP morte insérée tard.
- **Impact** : perte du compteur `used` d'une IP active → `headroom` recalculé
  au maximum, l'IP redevient « qualified » alors qu'elle a consommé son quota
  (contournement local du ledger, qui reste autoritaire — donc mitigé — mais
  `model_usable` peut dire `True` sur une IP proche du mur).
- **Recommandation** : `OrderedDict` + `move_to_end` à chaque accès
  (le pattern existe déjà dans `free/quality.py:389-494`) ou purger par
  `last_request_at`.
- **Effort** : S.

#### P2-16 — `free/quality.py::QualityRegistry` : `_items` sans verrou, appelé depuis threads (to_thread) et l'event loop

- **Preuve** : `free/quality.py:389` `OrderedDict` nu ; `get()` fait
  `move_to_end` (l. 493) ; `observe`/`observe_completion` sont appelés depuis
  `opencode.py:15513` (thread de requête) et `free/probe.py` (via
  `asyncio.to_thread`). Aucun verrou.
- **Impact** : `OrderedDict.move_to_end` + éviction concurrents peuvent lever
  `RuntimeError: OrderedDict mutated during iteration` (l'éviction l. 490-491
  pendant une itération d'un autre thread, cf. `host_quality` l. 561 qui
  itère `self._items.values()`), ou perdre une mesure.
- **Recommandation** : `threading.RLock` autour de `get`/éviction et
  `list(self._items.values())` dans `host_quality` (déjà fait l. 561 ? non :
  `items = [i for i in self._items.values() ...]` — itération directe).
- **Effort** : S.

#### P2-17 — `free/pool/__init__.py::get_status` expose `ip_stats` complet (jusqu'à 200 × N entrées)

- **Preuve** : `free/pool/__init__.py:8173-8174`
  ```python
  "ips_used": len({ip for p in self._per.values() for ip in p["ip_stats"]}),
  "ip_stats": {ip: st for p in self._per.values() for ip, st in p["ip_stats"].items()},
  ```
  `ip_stats` est borné à 200 entrées par station (l. 5731-5736) et `_per`
  contient 31+ entrées (mesure : `per` keys = 6 stations + 28 sids négatifs) →
  jusqu'à ~6 800 entrées sérialisées en JSON à chaque `get_status()`.
- **Impact** : payload `/api/vpn-status` et `/api/pool-status` volumineux ;
  sérialisation JSON + transfert à chaque refresh dashboard.
- **Recommandation** : ne renvoyer qu'un agrégat (`ips_used`) et déplacer le
  détail dans un endpoint dédié paginé.
- **Effort** : S.

#### P2-18 — `free/pool/__init__.py::capacity_snapshot` duplique la logique de `get_status` (deux calculs de capacité divergents)

- **Preuve** : `capacity_snapshot` (l. 1249-1430) et `get_status`
  (l. 7763-8361) recalculent chacun `configured_stations`, `usable_stations`,
  `unique_usable_ips`, `active_stations_with_quota`, `duplicate_*`,
  `capacity_degraded`, `capacity_reason` — avec des variantes : `capacity_snapshot`
  utilise `self.station_usable(st)` + `model_usable(item, st)` par modèle
  (l. 1376-1396) et n'applique pas `_socks5_client_usable` ; `get_status`
  branche `_socks_capacity` (l. 8006) et recalcule via `_socks5_client_usable`
  (l. 8019).
- **Impact** : les deux vues peuvent diverger (c'est déjà documenté dans les
  commentaires : « /api/vpn-status affichait 0/6 alors que six stations WARP
  servaient le client (F-32) »). Le fix de l'un n'est pas appliqué à l'autre.
- **Recommandation** : extraire un `_capacity_view()` unique appelé par les
  deux.
- **Effort** : M.

#### P2-19 — `free/egress_store.py::list_options(limit=5_000)` charge jusqu'à 31 900 options en mémoire (`recompute_scores`, `export_json`, `block_indicator`)

- **Preuve** : `free/egress_store.py:660-676` `recompute_scores` charge
  `list_options(provider, limit=5_000)` puis un `executemany` de 5 000 UPDATE ;
  `export_json` (l. 949-963) charge `list_options(limit=5_000)` et sérialise ;
  `free/egress_runtime.py:138` `block_indicator` charge `list_options(limit=5_000)`.
  Mesure : `egress_options` = 31 900 lignes (dont beaucoup `dead`).
- **Impact** : `recompute_scores` appelé dans `EgressRuntime.start()`
  (`free/egress_runtime.py:62-63`) pour chaque policy → plusieurs passes de
  5 000 lignes au boot ; `maintenance()` (quotidien) exporte 5 000 options.
- **Recommandation** : filtrer par statut (`status != dead`) et paginer ;
  n'exporter que les options `usable`/`quota`.
- **Effort** : S.

#### P2-20 — `free/egress_adapters/public.py` : `store.record_source_promotion` / `mark_source_run` par source, dans la boucle de probe

- **Preuve** : `free/egress_adapters/public.py:377` `store.record_source_candidates(source.id, len(rows))`,
  `l. 388` `store.mark_source_run(source.id)`, `l. 634`
  `store.record_source_promotion(source_id)` — chaque appel est un
  `UPDATE`/`with self._conn` (transaction implicite) sur le store unique
  `check_same_thread=False` protégé par `RLock`. Le catalogue a `max_options`
  jusqu'à 20 000 (`config.yaml:1139`) et `concurrency: 48` (l. 1140).
- **Impact** : N sources × 3 écritures transactionnelles pendant un cycle de
  découverte ; le `RLock` unique sérialise les 48 probes.
- **Recommandation** : accumuler les compteurs et faire un flush unique en fin
  de cycle.
- **Effort** : S.

#### P2-21 — `free/ip_registry/adapters/probe.py` : le budget de probes est consommé **avant** la tentative et jamais remboursé en cas d'erreur infra

- **Preuve** : `free/ip_registry/adapters/probe.py:126-135` —
  `_decision = _budget.allow(...)`, puis `_budget.record_probe(...)`
  immédiatement, avant les `for scheme in ("socks5h","socks5")`. Si les deux
  schémas échouent pour une raison réseau locale (Docker arrêté), le budget
  est consommé pour rien. `ProbeBudget.allow` a un cooldown de 600 s et
  `per_ip=2` (`free/probe_budget.py:18-19`).
- **Impact** : une IP non testée peut être « probe budget exhausted » alors
  qu'aucune sonde n'a réellement atteint le provider ; la chaîne attend
  24 h (`WINDOW_S`, l. 20) ou un `reset` explicite
  (`free/ip_state.py:190-198` ne le fait que pour les burns réparés).
- **Recommandation** : enregistrer la sonde **après** le premier essai réseau
  réel, ou rembourser (`record_probe` négatif / `reset`) sur erreur locale.
- **Effort** : S.

#### P2-22 — `free/pool/__init__.py::on_request` : `rotate_exhausted_managed()` sur le chemin de requête (déjà P1-06) et `ensure_connected()` qui scanne toutes les stations

- **Preuve** : `free/pool/__init__.py:5381` `await self.ensure_connected()` →
  boucle sur `self._stations` avec `_station_is_up` (l. 4835-4837) ;
  puis `l. 5393` `self.rotate_exhausted_managed()`. `on_request` est appelé à
  chaque requête free.
- **Impact** : deux scans complets par requête, dont un qui peut lancer des
  threads de rotation (l. 3105 `_request_warp_rotation`).
- **Recommandation** : ne garder que le scan bon marché (`_station_is_up`) sur
  le chemin, déplacer le reste dans la boucle watchdog existante
  (`opencode.py:3186-3195`).
- **Effort** : S.

#### P2-23 — `free/pool/__init__.py::_per_station` mute `per["request_count"]` en le lisant (effet de bord sur lecture)

- **Preuve** : `free/pool/__init__.py:1076-1119` — `_per_station` détecte un
  changement d'IP (`_cur_ip != _last_ip`) et remet `request_count` à 0,
  efface `bad_until`, `bad_cause`, `degraded_override`, `post_commit_retry_count`
  et pose `_ip_reanchored_at`. Cette méthode est appelée par des **lectures**
  (`_quality_stats` l. 1142, `_station_health_snapshot`, `_degraded_rank`
  via `per_state`, `_per_station_by_sid`).
- **Impact** : une lecture de dashboard (`get_status` → `_quality_stats`)
  peut remettre à zéro un compteur de quota et **effacer un bad-mark 429**
  fraîchement posé — le commentaire du code (l. 1097-1103) documente ce bug
  exact (« Une lecture `/api/vpn-status` ou `/metrics` suffisait à réarmer une
  station épuisée ») et affirme l'avoir corrigé par l'ancre
  `last_confirmed_ip` ; mais l'ancre est mise à jour **dans le même appel**, et
  le cas `_last_ip` vide (première lecture après restore sans
  `last_confirmed_ip`) efface encore `request_count` (l. 1088-1094).
- **Recommandation** : déplacer la détection de changement d'IP dans un
  `_reanchor_station(station)` appelé explicitement par les mutations
  (`switch_ip`, `_serve_socks5`, `note_model_ip`), pas depuis un getter.
- **Effort** : M.

#### P2-24 — `free/pool/__init__.py::save_pool_state` sérialise 4 096 leases + health + per à chaque flush (debounce 25 s) en synchrone

- **Preuve** : `free/pool/__init__.py:7645-7683` — construit `data` (per
  complet), `health` (`export_state`), `quota` (`export_state` de 4 096
  leases), puis `save_json_state` (SQLite upsert d'un JSON de 1 390 840 octets
  mesuré). Appelé par la boucle `_pool_persist_loop`
  (`opencode.py:4194-4196`, `asyncio.to_thread`) toutes les 30 s, et
  **synchroniquement** dans `_apply_bad_mark` (l. 2817) et `mark_station_ok`
  (l. 1718) — donc dans le chemin de requête sur 429/200.
- **Impact** : `_apply_bad_mark` fait un JSON.dumps de ~1,4 Mo + écriture
  SQLite **synchrone** dans le thread appelant (event loop sur 429 stream).
  C'est un des rares points où le chemin de requête fait une écriture
  volumineuse.
- **Recommandation** : dans `_apply_bad_mark`, marquer `_dirty_state=True` et
  laisser la boucle dédiée flusher (le debounce 25 s existe déjà).
- **Effort** : S.

#### P2-25 — `free/pool/__init__.py::_load_bad_ttl_by_cause` accepte `None` mais `_bad_ttl_for` retombe sur `_bad_ttl` sans borne

- **Preuve** : `free/pool/__init__.py:2674-2697` : une valeur `None` est
  conservée comme `None` (`table[str(k)] = None`), documentée « legacy ». Dans
  `_bad_ttl_for` (l. 2699-2710), `None` → `self._bad_ttl`. `config.yaml`
  livre `bad_ttl_by_cause: {auth: 0, rate_limit: null, timeout: 30, tls: 45}`
  (l. ~?) — `auth: 0` donne TTL 0 (bad-mark immédiatement expiré), ce qui est
  probablement voulu (auth ne doit pas bad-mark), mais `max(0.0, float(v))`
  (l. 2707) transforme un `0` en TTL nul **silencieusement**, sans
  documentation ni test.
- **Impact** : `_apply_bad_mark(target, "auth")` (jamais appelé avec cette
  cause aujourd'hui : les causes sont `rate_limit`/`timeout`, cf. les 8 sites
  l. 6116-6983) — donc la clé `auth` est morte. `rate_limit: null` retombe sur
  `_bad_ttl` (120 s via `station_bad_ttl_s: 120`, `config.yaml:668`).
- **Recommandation** : supprimer la clé `auth` si aucune cause ne l'utilise, ou
  documenter l'intention ; ajouter un test pour `0` vs `None`.
- **Effort** : S.

#### P2-26 — `free/rotation.py::SharedRotationState._persist` écrit un JSON complet à chaque `record_ip`/`next_identity`

- **Preuve** : `free/rotation.py:448-463` sérialise `ip_events`, curseurs,
  `last_index_by_station`, `last_country_by_station` à chaque appel ;
  `record_ip` (l. 133-151), `register_station` (l. 155-161),
  `next_identity` (l. 210-211), `next_country` (l. 259-260), `set_window`
  (l. 351-352), `prune_stations` (l. 325-326) l'appellent tous. `save_json_state`
  → `_write_row` sur `registry.db` (`free/chain_state.py:57-74`), connexion
  neuve à chaque fois. Mesure : `chain_state['shared_rotation']` = 1 484
  octets, donc petit, mais l'appel vient de `vpn/manager.py:6511+`
  (`peek_next_country` est un **preview** qui ne persiste pas — OK).
- **Impact** : une connexion SQLite ouverte/fermée par rotation d'identité ou
  de pays ; acceptable en volume, mais c'est le chemin de rotation qui, à 6
  stations, peut déclencher plusieurs écritures par minute.
- **Recommandation** : batcher (dirty flag + flush périodique), comme le ledger.
- **Effort** : S.

### P3

#### P3-01 — `free/classify_429.py::_TRANSIENT_HINTS` déclaré et jamais utilisé

- **Preuve** : `free/classify_429.py:36-43` définit `_TRANSIENT_HINTS =
  ("unavailable", "overloaded", "try again", "temporarily", "timeout",
  "capacity")` ; `grep -rn "_TRANSIENT_HINTS"` ne renvoie que cette définition.
  La classification « transitoire » se fait via `_RATE_TYPES`/`is_rate_error`
  (l. 207-209) et le fallback final (l. 260-268).
- **Impact** : aucun (code mort), mais un lecteur peut croire que ces motifs
  sont classés.
- **Recommandation** : supprimer, ou s'en servir pour marquer
  `reason="transient_hint:<motif>"` dans la classification UPSTREAM.
- **Effort** : S.

#### P3-02 — `free/classify_429.py::classify_429` : paramètres `ip_proven`/`wire_fp_match` acceptés mais jamais lus

- **Preuve** : `free/classify_429.py:176-190` — signature avec `ip_proven: bool
  = False, wire_fp_match: bool = True` ; le corps (l. 190-268) ne les
  référence jamais. Les appelants les passent pourtant
  (`free/ip_registry/adapters/probe.py:187-193`, `opencode.py:16034-16040`).
- **Impact** : l'API laisse croire qu'un burn de format dépend de la preuve de
  l'IP ; le commentaire l. 211-216 explique que la distinction a été retirée
  (« Un burn exige la preuve explicite d'un rejet de forme »). Les paramètres
  sont un vestige.
- **Recommandation** : les retirer (et adapter les 2 appelants), ou les
  utiliser pour affiner `reason`.
- **Effort** : S.

#### P3-03 — `Classification.is_format` / `is_quota` : `is_format` jamais appelé

- **Preuve** : `free/classify_429.py:65-71` ; `grep -rn "is_format"`
  ne renvoie que la définition et des homonymes dans les tests
  (`test_free_error_taxonomy`, `test_free_quota_vs_transient` testent
  `_is_quota` sur `core.errors`). `is_quota` (l. 66) est utilisé dans
  `tests/test_qualification_sweep.py:281`.
- **Impact** : surface d'API morte.
- **Recommandation** : supprimer `is_format` ou l'utiliser dans
  `free/ip_registry/adapters/probe.py` (qui compare `_cls.cls == "format"` en
  dur, l. 200-205).
- **Effort** : S.

#### P3-04 — `free/quota_ledger.py::_is_free_usage_error` alias rétro-compat jamais utilisé hors du module

- **Preuve** : `free/quota_ledger.py:293-294` `_is_free_usage_error =
  is_free_usage_error` (« Rétro-compatibilité pour les appelants internes
  historiques »). `grep -rn "_is_free_usage_error"` → seuls la définition et
  l'usage interne l. 433.
- **Impact** : aucun appelant externe (l'alias public `is_free_usage_error` est
  importé par `free/pool/__init__.py:59`).
- **Recommandation** : supprimer l'alias.
- **Effort** : S.

#### P3-05 — `free/quota_ledger.py::EgressBlock` et `QuotaObservation` (feature 007) : aucune utilisation en production

- **Preuve** : `free/quota_ledger.py:191-273` ; `grep -rn "EgressBlock\|QuotaObservation"`
  → `free/quota_ledger.py` et `tests/test_egress_block.py` uniquement. Aucun
  import depuis `free/`, `opencode.py`, `dashboard/`, `scripts/`.
- **Impact** : modèles morts (~80 lignes) ; le ledger a deux représentations de
  « bloc épuisé » (`BucketState` réel, `EgressBlock` mort).
- **Recommandation** : supprimer, ou brancher sur le catalogue egress si la
  feature 007 est reprise (68 tâches restantes selon `00-specs-state.md`).
- **Effort** : S.

#### P3-06 — `free/quota_ledger.py::parked_ips` et `headroom` : aucun appelant de production

- **Preuve** : `parked_ips` (l. 573-592) : seuls les tests
  (`tests/test_quota_ledger.py:220-231`,
  `test_quota_ledger_characterization.py:117`). `headroom` (l. 525-534) :
  seuls les tests. `exhausted_ips` (l. 556-571) est utilisé
  (`free/warp_rotation.py:295-296`, `opencode.py:26082`).
- **Impact** : la logique « park plutôt que rotation » documentée dans
  `rotate_exhausted_managed` (`free/pool/__init__.py:2901-2903`) est
  implémentée **localement** avec `ledger.reset_at()` (l. 3002-3011), pas via
  `parked_ips`. Deux implémentations, une seule vivante.
- **Recommandation** : soit utiliser `parked_ips` dans `rotate_exhausted_managed`,
  soit supprimer la méthode.
- **Effort** : S.

#### P3-07 — `free/ip_quota.py` : `prune`, `prune_station` partiellement morts ; `ranking_key`, `headroom`, `state` sans appelant

- **Preuve** : `grep -rn "\.prune("` sur `free/pool/ opencode.py` → aucun appel
  à `IpQuotaRegistry.prune` (l. 492-497) ni `prune_station` (l. 499-503) hors
  `free/pool/__init__.py:7101,7199` (qui appellent `self._quota.prune_station`)
  — donc `prune_station` est vivant, `prune` est mort. `ranking_key` (l. 410-411)
  : aucun appelant (`grep` vide) ; `IpQuotaRegistry.headroom` (l. 372-374) et
  `.state` (l. 376-378) : aucun appelant (le pool utilise
  `_quota.model_usable` et `lease_for().snapshot()`).
- **Impact** : API morte ; `ranking_key` duplique `lease.score_key` utilisé par
  `free/best_ip.py:118`.
- **Recommandation** : supprimer `prune`, `ranking_key`, `headroom`, `state`.
- **Effort** : S.

#### P3-08 — `free/ip_quota.py::IpQuotaLease.blocked` (property) jamais utilisé ; `_NON_SELECTABLE` utilisé une fois

- **Preuve** : `free/ip_quota.py:167-169` `blocked` : `grep -rn "\.blocked\b"`
  ne trouve aucun appelant. `_NON_SELECTABLE` (l. 33) est utilisé l. 188 dans
  `note_request`.
- **Impact** : mineur.
- **Recommandation** : supprimer la property.
- **Effort** : S.

#### P3-09 — `free/station_health.py` : `disable`, `enable`, `repair_failed`, `prune` sans appelant de production

- **Preuve** : `grep -rn "\.disable()\|\.enable()\|repair_failed(\|\.prune("` sur
  `free/ opencode.py dashboard/ vpn/` → aucun appel (seuls les tests). Les
  états `DISABLED` restent atteignables par `restore_state`
  (`free/station_health.py:337-338`).
- **Impact** : la machine à états a des transitions (DISABLED, repair_failed)
  que rien n'actionne ; `restore_state` peut donc restaurer une station
  `disabled` qu'aucun code ne réactive.
- **Recommandation** : soit brancher (un `disabled` restauré doit être
  ré-activé au boot), soit retirer les états.
- **Effort** : S.

#### P3-10 — `free/wire_admission.py` : aucun appelant en production

- **Preuve** : `grep -rn "wire_admission\|admit_many\|MODE_COMPAT\|MODE_OBSERVE"`
  → uniquement `free/wire_admission.py` et `tests/unit/test_wire_admission.py`.
  Aucun import depuis `free/`, `opencode.py`, `dashboard/`, `scripts/`.
- **Impact** : le module 023 T007/T025 (81 l.) est mort ; la règle « un
  endpoint proven ne reçoit que son wire_fp » n'est **pas** appliquée au
  routage (seul `ip_state.is_selectable` la porte, l. 110-116, appelé par
  `free/ip_registry/adapters/probe.py` et non par le pool).
- **Recommandation** : brancher `admit()` dans la sélection SOCKS5
  (`_socks5_client_usable`, `free/pool/__init__.py:3885-3922`) ou supprimer.
- **Effort** : M (branchement) / S (suppression).

#### P3-11 — `free/pool/__init__.py::pick_two` et `_socks5_provider_available` : morts

- **Preuve** : `pick_two` (l. 3850-3853, « Back-compat alias for plan's
  pick_two ») : `grep -rn "pick_two"` → définition seule (+ `.audit-logs-wt`).
  `_socks5_provider_available` (l. 4906-4916) : `grep` → définition seule.
- **Impact** : deux chemins de sélection alternatifs non exercés.
- **Recommandation** : supprimer.
- **Effort** : S.

#### P3-12 — `free/egress_rotation.py::public_pool_rows` et `provider_status` : appelés seulement par `logs/inspect_select.py` et tests

- **Preuve** : `grep -rn "public_pool_rows\|provider_status"` → `free/egress_rotation.py`
  (déf.), `logs/inspect_select.py:34-35` (script de diagnostic), tests.
  `free/egress_runtime.py` utilise `rotation_plan` (l. 102-105) et
  `free/pool/__init__.py` utilise `free/pool/public_pool.py::select_public_rows`
  (chemin distinct).
- **Impact** : `public_pool_rows` duplique la sélection de
  `select_public_rows` sans être branché.
- **Recommandation** : supprimer, ou faire passer `set_public_proxies` par lui.
- **Effort** : S.

#### P3-13 — `free/pool/__init__.py::_egress_order_prefers_socks5` : utilisé seulement par un test

- **Preuve** : `grep -rn "_egress_order_prefers_socks5"` → définition
  (l. 5334) + `tests/test_auto_egress_config.py:193-199`.
- **Impact** : méthode morte en production ; la logique d'ordre réellement
  appliquée est dans `on_request` (l. 5421-5438).
- **Recommandation** : supprimer, ou l'utiliser dans `on_request` (le
  commentaire dit qu'elle décrit l'ordre GUI).
- **Effort** : S.

#### P3-14 — `free/fallback.py::FallbackPolicy.note_probe` / `evaluate_error` / `active_provider` / `active_count` sans appelant de production

- **Preuve** : `grep -rn "note_probe(\|evaluate_error(\|active_provider(\|active_count("`
  → `free/fallback.py` (déf.), `free/ip_state.py:392` (homonyme),
  `free/qualification/qualifiers.py:109` (homonyme). Aucun appel depuis
  `free/pool/__init__.py`.
- **Impact** : voir P1-09.
- **Recommandation** : cf. P1-09.
- **Effort** : cf. P1-09.

#### P3-15 — `free/provider_capabilities.py::normalize_ipv6_64`, `capacity`, `is_reserve_eligible` sans appelant

- **Preuve** : `grep -rn "is_reserve_eligible\|normalize_ipv6_64"` → aucun
  appelant hors définitions. `bucket_key` est utilisé par
  `free/ip_registry/adapters/probe.py` (l. 132, 191, 208) ; `capabilities()` et
  `PROVIDER_NAMES` par `opencode.py:25587-25665`.
- **Impact** : la notion de « capacité d'un bucket » (0 pour WARP IPv4) n'est
  pas appliquée dans la sélection du pool.
- **Recommandation** : brancher `capacity` dans la sélection WARP IPv4 (le
  commentaire du module dit « WARP IPv4 a une capacité nulle ») ou supprimer.
- **Effort** : S.

#### P3-16 — `free/egress_ip.py` : seul `is_global_ip` est utilisé, `agree`/`IpObservation`/`normalize` non

- **Preuve** : `grep -rn "free.egress_ip\|\.agree("` →
  `scripts/fresh_chain_supervisor.py:405` importe `is_global_ip` ;
  `agree`/`IpObservation`/`normalize` n'ont d'appelant que
  `tests/unit/test_egress_ip.py`.
- **Impact** : la règle « deux sources concordantes » (023 T030) n'est pas
  appliquée dans le chemin de rotation (seule `is_global_ip` filtre les
  sentinels).
- **Recommandation** : brancher `agree` dans `reconcile_endpoint` ou
  `_resolve_egress` (une seule source aujourd'hui), ou supprimer.
- **Effort** : S.

#### P3-17 — `free/reliability_canary.py::CanaryRouter.note_choice` / `shadow_divergences` : jamais alimentés

- **Preuve** : `grep -rn "note_choice\|shadow_divergences"` → définition +
  `tests/test_reliability_canary.py`. Le pool appelle `_canary.use_new(...)`
  (`free/pool/__init__.py:3556`) mais jamais `note_choice`.
- **Impact** : `canary.shadow_divergences` exposé dans `get_status()`
  (l. 8129) reste toujours 0 — métrique mensongère (principe I).
- **Recommandation** : alimenter `note_choice` dans `_best_station` quand les
  deux sélecteurs diffèrent, ou retirer le champ.
- **Effort** : S.

#### P3-18 — `free/pool/__init__.py::_wire_snapshot_for` appelé par station dans `get_status` (lecture vault à chaque refresh)

- **Preuve** : `free/pool/__init__.py:7751-7761` appelle
  `get_vault().snapshot(str(ip))` ; `get_vault()` (l. 745-763 de
  `free/identities.py`) fait un `configure(...)` + lecture disque potentielle.
  Appelé par station (l. 7866).
- **Impact** : I/O fichier par station et par refresh dashboard.
- **Recommandation** : mémoïser par IP avec TTL.
- **Effort** : S.

#### P3-19 — `free/hunter.py` : `_STARTED_AT`/`_PROC` globals avec `time.sleep(0.3)` dans une boucle de démarrage

- **Preuve** : `free/hunter.py:24-25` globals, `l. 103-140` `start()` avec
  `time.sleep(0.3)` (l. 135). Appelé par `dashboard/api.py:2286`
  (`ensure_started` au boot) et `opencode.py:26204` (`set_enabled` via
  `asyncio.to_thread`, OK).
- **Impact** : `ensure_started` au boot du dashboard bloque 0,3 s (acceptable) ;
  `_STARTED_AT` n'est jamais remis à 0 dans `stop()` (l. 141-174) — vérifier
  que `status()` ne rapporte pas un PID périmé (il teste `_pid_alive`, l. 88-101).
- **Recommandation** : cosmétique.
- **Effort** : S.

#### P3-20 — `free/pool/__init__.py` : `_MIN_EFFECTIVE_AGE_S = 180` commenté mais `_max_effective_age_s` vient de la config (900)

- **Preuve** : `free/pool/__init__.py:89-92` : le commentaire dit « The
  stale-egress window must exceed the watchdog cadence (20 s in config) and its
  worst-case tick delay. A 60 s window produced false "egress-stale"
  rotations » ; le code prend `max(180, config)`. `config.yaml:669` livre 900.
  Le commentaire parle d'une cadence watchdog de 20 s alors que
  `station_health.probe_interval_s` = 120.
- **Impact** : documentation périmée ; un opérateur qui baisse
  `max_effective_age_s` à 300 obtient 300 (au-dessus du plancher 180) et peut
  reproduire les fausses rotations.
- **Recommandation** : mettre à jour le commentaire et documenter le plancher
  effectif.
- **Effort** : S.

#### P3-21 — `free/quality.py::QualityRegistry.host_quality` itère `self._items.values()` sans copie

- **Preuve** : `free/quality.py:561` `items = [i for i in self._items.values() ...]`
  — voir P2-16.
- **Impact** : `RuntimeError` possible si `get()` évince pendant l'itération
  (autre thread).
- **Recommandation** : `list(self._items.values())`.
- **Effort** : S.

#### P3-22 — `free/quality.py::status()` et `slow_station_count` : `status()` appelé par le dashboard, `slow_station_count` sans appelant

- **Preuve** : `status()` (l. 592-609) appelé par
  `dashboard/routers/vpn_status_routes.py:299` ; `slow_station_count` (l. 577-580)
  : `grep` → définition seule.
- **Impact** : mineur.
- **Recommandation** : supprimer `slow_station_count`.
- **Effort** : S.

#### P3-23 — `free/ip_registry/adapters/probe.py::classify_429_scope` duplique `core.errors::free_quota_scope` et `free/classify_429.py`

- **Preuve** : `free/ip_registry/adapters/probe.py:29` définit
  `classify_429_scope(body)` (l. 29-63) ; `opencode.py:16021` utilise
  `free.classify_429.classify_429` et `core.errors.free_quota_scope`
  (`free/pool/__init__.py:1506-1514`). Trois classifieurs de 429 coexistent.
- **Impact** : duplication de logique ; le probe legacy et l'app peuvent
  classer différemment.
- **Recommandation** : faire appeler `free.classify_429` par le probe.
- **Effort** : S.

#### P3-24 — `free/pool/__init__.py` : `_quality_preselection.max_load` lu dans la config mais sans consommateur

- **Preuve** : `_QUALITY_PRESELECTION_DEFAULTS["max_load"]` (l. 537) ;
  `_seed_quality_cfg` le valide (l. 7243) ; `grep -rn '"max_load"'` → définition,
  validation, `dashboard/api.py:754`. Aucune lecture fonctionnelle (le seuil de
  charge est appliqué par `vpn/manager.py` via `_server_load`, pas par ce dict).
- **Impact** : option GUI sans effet (principe XII).
- **Recommandation** : retirer la clé ou l'appliquer dans la sélection.
- **Effort** : S.

#### P3-25 — `free/pool/__init__.py::_spare_ready` : écrit deux fois, jamais lu

- **Preuve** : `grep -rn "_spare_ready"` → `free/pool/__init__.py:776` et
  `:7503` (écritures), aucune lecture. `warm_spare_ready` (le champ exposé)
  vient de `_spare_qualified` (l. 1423, 8110).
- **Impact** : attribut mort.
- **Recommandation** : supprimer.
- **Effort** : S.

#### P3-26 — `free/pool/__init__.py::_last_selection` : écrit, jamais lu

- **Preuve** : `grep -rn "_last_selection"` → écritures l. 3572 et 3810 ;
  aucune lecture (`get_status` utilise `self._best_ip.snapshot()`, l. 8131).
- **Impact** : attribut mort.
- **Recommandation** : supprimer.
- **Effort** : S.

#### P3-27 — `free/pool/__init__.py::_station_health_snapshot` : jamais appelé

- **Preuve** : `grep -rn "_station_health_snapshot"` → définition l. 1154,
  aucun appel.
- **Impact** : code mort.
- **Recommandation** : supprimer.
- **Effort** : S.

#### P3-28 — `free/rotation.py` : `_last_index_by_station`/`_last_country_by_station` persistés dans un JSON de 1,4 Ko alors que le curseur absolu suffit

- **Preuve** : `free/rotation.py:453-460` persiste les deux dicts ; ils servent
  à la passe 1 de `next_identity`/`next_country` (l. 188, 235). Sur une flotte
  fixe de 6 stations, ces dicts ont 6 entrées.
- **Impact** : négligeable.
- **Recommandation** : RAS, note pour information.
- **Effort** : S.

#### P3-29 — `free/egress_store.py::_option_row` / `_source_row` : parsing JSON par ligne à chaque lecture (31 900 options)

- **Preuve** : `free/egress_store.py` `_option_row` (fin de fichier) fait
  `json.loads` de `source_ids` et `meta_json` par ligne ; `list_options` avec
  `limit=5_000` (l. 604-639) → 5 000 `json.loads` × 2 par appel.
- **Impact** : coût CPU sur les endpoints catalogue (dashboard, revalidation).
- **Recommandation** : colonnes normalisées ou cache par id.
- **Effort** : M.

#### P3-30 — `free/pool/__init__.py::clear_quota_buckets` ne purge pas `_model_ip_health` ni `_model_global_until`

- **Preuve** : `free/pool/__init__.py:1627-1635` délègue à `ledger.clear` ;
  `clear_model_global` (l. 2341) est une méthode séparée, appelée par
  `opencode.py:26234+` (`cleared_model_global`). Mais `_model_ip_health`
  (quarantaine par (modèle, station, IP)) n'est jamais purgée par
  `/api/free/quota/clear`.
- **Impact** : après un « clear quota » opérateur, les stations restent
  bloquées par `blocked_until` en mémoire jusqu'à expiration — l'opérateur
  croit avoir débloqué.
- **Recommandation** : purger aussi `_model_ip_health` (filtré par ip/model)
  dans `clear_quota_buckets`.
- **Effort** : S.

---

## 4. Inventaire du code mort

| Symbole | Fichier:ligne | Preuve d'absence d'usage | Verdict |
|---|---|---|---|
| `_TRANSIENT_HINTS` | free/classify_429.py:36 | `grep -rn "_TRANSIENT_HINTS"` → définition seule | mort |
| `classify_429(ip_proven=, wire_fp_match=)` | free/classify_429.py:176-183 | params jamais lus dans le corps (l. 190-268) | mort (API) |
| `Classification.is_format` | free/classify_429.py:70 | `grep -rn "is_format"` → définition seule | mort |
| `_is_free_usage_error` (alias) | free/quota_ledger.py:294 | `grep` → définition + usage interne l. 433 | mort (alias) |
| `EgressBlock` | free/quota_ledger.py:191 | `grep` → module + tests/test_egress_block.py | mort |
| `QuotaObservation` | free/quota_ledger.py:248 | idem | mort |
| `QuotaLedger.parked_ips` | free/quota_ledger.py:573 | `grep` → tests uniquement | mort |
| `QuotaLedger.headroom` | free/quota_ledger.py:525 | `grep` → tests uniquement | mort |
| `IpQuotaRegistry.prune` | free/ip_quota.py:492 | `grep "\.prune("` sur free/ opencode.py → aucun | mort |
| `IpQuotaRegistry.ranking_key` | free/ip_quota.py:410 | `grep` → définition seule | mort |
| `IpQuotaRegistry.headroom` / `.state` | free/ip_quota.py:372,376 | `grep` → définitions seules | morts |
| `IpQuotaLease.blocked` | free/ip_quota.py:167 | `grep "\.blocked\b"` → définition seule | mort |
| `StationHealth.disable` / `.enable` | free/station_health.py:206,210 | `grep "\.disable()\|\.enable()"` → aucun | morts |
| `StationHealth.repair_failed` | free/station_health.py:172 | `grep "repair_failed("` → définition seule | mort |
| `StationHealthRegistry.prune` | free/station_health.py:297 | `grep "\.prune("` → aucun sur ce registre | mort |
| `wire_admission` (module) | free/wire_admission.py:1-81 | `grep` → module + tests/unit/test_wire_admission.py | mort (module entier) |
| `FreeIPPool.pick_two` | free/pool/__init__.py:3850 | `grep "pick_two"` → définition seule | mort |
| `FreeIPPool._socks5_provider_available` | free/pool/__init__.py:4906 | `grep` → définition seule | mort |
| `FreeIPPool._egress_order_prefers_socks5` | free/pool/__init__.py:5334 | `grep` → définition + 1 test | mort (prod) |
| `FreeIPPool._spare_ready` | free/pool/__init__.py:776,7503 | `grep` → écritures seules | mort |
| `FreeIPPool._last_selection` | free/pool/__init__.py:829,3572,3810 | `grep` → écritures seules | mort |
| `FreeIPPool._station_health_snapshot` | free/pool/__init__.py:1154 | `grep` → définition seule | mort |
| `FallbackPolicy.note_probe` | free/fallback.py:247 | `grep` → définition seule (hors homonymes) | mort |
| `FallbackPolicy.evaluate_error` | free/fallback.py:231 | `grep` → définition seule | mort |
| `FallbackPolicy.active_provider` / `.active_count` | free/fallback.py:168,172 | `grep` → définitions seules | morts |
| `egress_rotation.public_pool_rows` | free/egress_rotation.py:73 | `grep` → module + logs/inspect_select.py + tests | mort (prod) |
| `egress_rotation.provider_status` | free/egress_rotation.py:113 | `grep` → définition + tests | mort (prod) |
| `provider_capabilities.capacity` | free/provider_capabilities.py:88 | `grep` → définition seule | mort |
| `provider_capabilities.is_reserve_eligible` | free/provider_capabilities.py:99 | `grep` → définition seule | mort |
| `provider_capabilities.normalize_ipv6_64` | free/provider_capabilities.py:57 | `grep` → définition seule (appelée en interne l. 77) | mort (externe) |
| `egress_ip.agree` / `IpObservation` / `normalize` | free/egress_ip.py:41,34,81 | `grep "\.agree("` → tests seulement | morts |
| `CanaryRouter.note_choice` / `shadow_divergences` | free/reliability_canary.py:75,55 | `grep` → définition + tests | mort (prod) |
| `QualityRegistry.slow_station_count` | free/quality.py:577 | `grep` → définition seule | mort |
| `_QUALITY_PRESELECTION_DEFAULTS["max_load"]` | free/pool/__init__.py:537 | `grep '"max_load"'` → validation + dashboard seulement | mort (option) |
| `free/repair.py` (shim) | free/repair.py:1-12 | `grep "free.repair\|from free import repair"` → tests/test_station_reliability.py:14 uniquement | mort (prod) |
| `free/profile_verifier.py` | free/profile_verifier.py (143 l.) | `grep` → tests + `free/__init__.py` (import docstring) | mort (prod) |
| `logs/ip_registry.db` (0 octet) | config.yaml:1254 `db_path` | 0 octet sur disque ; `SqliteIpRegistry` instancié seulement par scripts/tests | config morte |

## 5. Options / config sans consommateur

| Clé / section | Fichier:ligne | Preuve | Verdict |
|---|---|---|---|
| `ip_registry.db_path` | config.yaml:1254 | `logs/ip_registry.db` = 0 octet ; `free/ip_registry/sqlite_registry.py:74` `section.get("db_path")` lu, mais aucun process de prod n'instancie `SqliteIpRegistry` | morte |
| `ip_registry.selection.unique_ip` / `warm_pool_size` / `warm_ttl_s` / `allow_countries` / `deny_countries` | config.yaml:1273-1284 | `grep -rn "warm_pool_size\|allow_countries\|deny_countries\|unique_ip"` sur `opencode.py dashboard/ free/` → aucun (seuls `scripts/fresh_chain.py:768+` lit `selection.score_weights`) | mortes |
| `ip_rotation.fallback.probe_interval_s` | config.yaml:191 / free/fallback.py:92 | `FallbackConfig.probe_interval_s` jamais lu hors `as_dict()` ; `note_probe` sans appelant | morte |
| `ip_rotation.fallback.recovery_successes` | free/fallback.py:95 | idem | morte |
| `ip_rotation.fallback.cooldown_s` | free/fallback.py:98 | lu uniquement dans `note_probe` (mort) | morte |
| `ip_rotation.fallback.max_block_s` | free/fallback.py:101 | idem | morte |
| `ip_rotation.fallback.fail_closed` | free/fallback.py:107 | exposé dans `snapshot()` (l. 311) mais aucune décision ne le lit | morte |
| `ip_rotation.bad_ttl_by_cause.auth` | config.yaml:105 | aucune cause `"auth"` passée à `_apply_bad_mark` (8 sites : `rate_limit`/`timeout`) | morte |
| `egress_capacity.reserve_policy` (hors dashboard) | config.yaml:143-145 | `capacity_config` appelé par `dashboard/routers/{capacity,station}_routes.py` uniquement ; le pool ne lit pas `egress_capacity` | morte (runtime) |
| `ip_registry.slo.*` | config.yaml:1251-1253 | lu par `opencode.py:2456-2469` (OK) | vivante |
| `ip_rotation.public_proxies.feed_path` | free/pool/__init__.py:4711 | lu seulement si `_public_rows_provider` absent — or `EgressRuntime.start` le pose toujours (`free/egress_runtime.py:61`) | morte de fait |
| `ip_rotation.public_proxies.free_probe_model` / `free_probe_concurrency` | free/pool/__init__.py:4481,4583 | lus depuis `_public_cfg` qui vient de `config.yaml:260-263` **qui ne les définit pas** → toujours les défauts | config absente (consommateur sans clé) |
| `ip_rotation.qualification_probe` | config.yaml:149 | lu par `scripts/fresh_chain_supervisor.py:546` uniquement | vivante (script) |
| `stations.quality.*` | config.yaml:68-95 | `QualityConfig.from_yaml` lit `stations.quality` (`free/quality.py:77`) — mais `enabled: false` : tout le bloc est inerte tant que désactivé | vivante (désactivée) |

## 6. Zones non auditées / incertitudes explicites

- **`free/qualification/` (≈ 7 000 l.)** : cartographié (imports, points
  d'entrée `QualifierManager`, `build_qualification`) mais pas audité ligne à
  ligne — hors du périmètre « cœur » demandé. Ses interactions avec
  `registry.db` (table `qualification_*`, 40 891 lignes d'attempts) restent à
  auditer.
- **`free/control_plane/` (≈ 2 000 l.)** : idem ; `shadow.py`/`telemetry.py`
  sans appelant de production identifié (`grep` → `free/control_plane/__init__.py`
  + tests) — à confirmer dans un audit dédié.
- **`free/clients/`** : utilisé par `opencode.py:25638` et `free/wire_profile.py:223` ;
  les familles `family_anthropic`/`family_openai_responses` n'ont pas été
  inspectées.
- **`free/ip_registry/adapters/{compose_binder,hybrid_binder,egress}.py`** :
  vivants via `free/qualification/station_adapter.py:156-210` (binder Compose +
  StationProbe), non audités en détail.
- **`free/ip_registry/sqlite_registry.py` (1 039 l.)** : schéma `ip_state`
  conflictuel prouvé (P1-05) ; le reste de l'API (leases, assignments,
  `sync_snapshot`) non audité.
- **Ordre d'initialisation des schémas** : je n'ai pas pu déterminer dans quel
  ordre exact `free/ip_state.py` et `free/ip_registry/sqlite.py` s'exécutent en
  production (le pool n'appelle pas `SqliteIpRegistry` ; seuls les scripts le
  font). Le conflit est réel sur disque (colonnes observées = celles de
  `free/ip_state.py`) mais je n'ai pas prouvé qu'un chemin de prod casse
  aujourd'hui.
- **`_draining_stations` (P0-03)** : j'ai prouvé le chemin de fuite par lecture
  du code (3 sites d'ajout, 1 site de retrait conditionnel) mais **sans
  exécution** : je n'ai pas instrumenté un run live pour compter les stations
  drainées. La preuve d'impact reste statique.
- **`ip_quota.blocked_until` (P0-02)** : la valeur actuelle en base est 0 pour
  les 4 premiers leases inspectés, donc l'impact n'est pas observé
  aujourd'hui — il l'est après un reboot avec un blocage actif.
- **Volumétrie `registry.db` (167 Mo)** : la part attribuable à chaque table
  n'a pas été mesurée (seuls les comptes de lignes).
- **Tests** : 478 fichiers de tests (81 liés à `free/`), dernière exécution
  contrat : 513 tests, 0 échec (`.artifacts/ci3.log`). Aucun test ne couvre
  P0-01/P0-02/P0-03 (grep sur `_draining_stations` dans les tests → 4
  occurrences, toutes des manipulations directes de l'ensemble, pas le chemin
  `_launch_rotation` bloqué).
