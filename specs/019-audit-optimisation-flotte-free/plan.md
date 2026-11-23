# Implementation Plan: Audit et optimisation de la flotte free

**Branch**: `019-audit-optimisation-flotte-free` | **Date**: 2026-11-14
**Spec**: [spec.md](./spec.md) | **Audit**: [audit.md](./audit.md)
**Input**: `specs/017-fork-soft-migration/decisions.md` (ADR-017-01),
`specs/018-autonomous-six-station-recovery/{spec,plan}.md`, code fork 016,
`scripts/fresh_chain_supervisor.py`, `free/pool/__init__.py`.

## Summary

L'audit du 2026-11-14 montre que la flotte n'échoue pas faute d'IP (15 vivent
dans le warm pool) mais par **défaut de modèle de quota** et **absence
d'autorité unique de l'état**. Le plan corrige d'abord la mesure et la
propriété, puis l'approvisionnement, puis la fiabilité et la performance. Il
ne touche ni au protocole, ni à `tool_aliases.py`, ni aux réglages WARP.

Principe directeur : **le fork possède le tunnel, le registre possède les
faits, le superviseur possède les décisions, l'app possède la preuve client.**

## Technical Context

- **Langage** : Python 3.12 (superviseur, app, pool) ; Go (fork Gluetun 016).
- **Stockage** : SQLite WAL (`logs/registry.db` v3, `logs/requests.db`).
- **Orchestration** : `docker compose` (stations, WARP), processus hôte
  (`opencode.py`, superviseur).
- **Tests** : pytest (`tests/`, 468 entrées), Go `testing` dans le fork,
  `ruff` + `mypy`, CI `.github/workflows/ci.yml`.
- **Plateforme** : Windows 11 hôte, conteneurs Linux.
- **Contraintes** : six stations, free, une seule image par station, migration
  douce, superviseur jamais gelé.

## Décisions d'architecture

### D1 — Une autorité par objet (remplace la propriété implicite actuelle)

| Objet | Autorité | Mécanisme |
|---|---|---|
| IP, transport, pays | fork | `/v1/vpn/state` recopié à chaque cycle |
| Pin hostname | `registry.db` | table `pins`, `.env` généré |
| Lease/quota | `registry.db` | `quota_buckets` + `leases` |
| Décision de rotation | superviseur | seul appelant de `/v1/vpn/rotate` |
| `client_usable` | app | probe → `station_state` |
| Warm pool | superviseur | `_refresh_warm_pool` |
| SLO/incidents | job dédié | lecture registre, écriture agrégée |

Conséquence : `station_state` devient un **read-model** de l'app. L'écriture
actuelle depuis `free/qualification/store.py:702` est supprimée ou passée en
`shadow` derrière un flag, puis retirée après 24 h sans divergence.

### D2 — Modèle de quota à deux compteurs

```
ip_state(ip) = f(upstream_verdict, ours_used, last_429_at, last_200_at)
upstream_verdict ∈ {unknown, hot, cooling, dead}
cooling_until = min(retry_after_at, last_429_at + ledger_max_block_s)
```

- `ours_used` ne décide **jamais** de l'éligibilité.
- `hot` = 200 vu < 10 min → éligible immédiatement.
- `cooling` = 429 vu → éligible à `cooling_until`, jamais avant.
- `dead` = 3 échecs de transport → retiré du pool pour 24 h.
- Une IP ne sert qu'une station à la fois (lease exclusif).

### D3 — Warm pool comme file d'attente principale

- `_refresh_warm_pool` garantit `warm_pool_min=6` (au lieu de 3).
- L'affectation (`free/pool`) lit le warm pool **avant** de qualifier.
- Un candidat consommé est retiré (`DELETE`), pas seulement marqué.
- TTL 6 h, re-probe à la consommation si `verified_at` > 1 h.

### D4 — App supervisée

- Lanceur : NSSM (Windows service) ou Tâche planifiée « au démarrage ».
- `restart=always`, délai 5 s, capture `stdout`/`stderr` horodatée.
- `sys.excepthook`, `faulthandler.enable()`, `atexit` pour flush.
- Heartbeat `/health` toutes les 10 s ; alerte si absent 30 s.

### D5 — Rotation asynchrone et bornée

- L'appel rend un `job_id` ; le superviseur interroge `/v1/vpn/jobs/{id}`.
- `rotate_timeout_s = rotation_max_duration + 30` (270 s).
- La file est exposée (`position`, `eta_s`) pour que l'opérateur sache
  pourquoi une station attend.

### D6 — Données bornées

- `registry.db` : purge TTL (`warm_pool` expirés, `claims` > 7 j,
  `incidents` > 7 j), checkpoint WAL > 4 Mio, `integrity_check` quotidien.
- `requests.db` : suppression des lignes > 7 j, `auto_vacuum=INCREMENTAL`,
  plafond 500 Mio, `VACUUM` hors pointe, `.bak` supprimé après contrôle.

## Architecture cible

```
app :4000 (supervisée)
  ├─ probe → station_state (écrit)
  ├─ sélection ← warm_pool / pool (lit)
  ├─ quota (lit registry, cache TTL 30 s)
  └─ /metrics, /api/vpn-status (lit station_state)

superviseur (seul décideur)
  ├─ _refresh_warm_pool → warm_pool (écrit)
  ├─ _auth_gate (unique écrivain pause)
  ├─ rotate(job_id) → fork
  └─ incidents/SLO → registry

fork :800N (seul propriétaire tunnel)
  ├─ /v1/vpn/rotate, /v1/vpn/state, /v1/vpn/jobs
  └─ transports, DNS, egress

registry.db (seule vérité des faits)
```

## Phases

### Phase 0 — Mesure et sécurité (aucun changement de routage)

1. Lanceur supervisé de l'app + capture stderr.
2. `/metrics` (9 métriques du plan de mesure) + job SLO minute.
3. Token dashboard + `client_auth` sur l'interface non-loopback.
4. Purge `warm_pool`/`incidents` + checkpoint WAL.

**Sortie** : SLO mesurable, app relançable, dashboard fermé, registre < 50 Mio.

### Phase 1 — Autorité unique

1. `station_state` read-model ; écriture qualification en `shadow`.
2. Table `pins` ; `.env` généré ; `_clear_stale_pins` testé.
3. Test de non-divergence API/registre/fork.
4. Retirer log+rotate du chemin chaud (`free/pool/__init__.py:5134`).

**Sortie** : zéro divergence sur 24 h ; ≤ 1 ligne `warp-rotate`/min.

### Phase 2 — Quota et approvisionnement (débloque 6/6)

1. `upstream_verdict` + `cooling_until` (migration douce schéma v4).
2. Warm pool consommé avant qualification ; `warm_pool_min=6`.
3. Préflight avant bind (DNS + port + egress, 5 s).
4. Respect strict de `reset_at` + plafond 1200 s.
5. Mesure « 6/6 même cycle » en continu.

**Sortie** : 6/6 sur un cycle de 120 s, tenu 30 min ; taux de 200 > 30 %.

### Phase 3 — Fiabilité et performance

1. Un seul écrivain AUTH + tests de concurrence.
2. Timeouts alignés (270 s).
3. Rotation asynchrone + position de file.
4. Regroupement des incidents.
5. T211 DNS transitoire.

**Sortie** : 0 AUTH storm, p95 < 240 s, ≤ 5 incidents/h.

### Phase 4 — Dette et exploitation

1. Purge `requests.db` + plafond.
2. Nettoyer Compose/.env stations 7–10.
3. Borner WARP à 2–3.
4. Tests des 5 fonctions superviseur + CI bloquante.
5. Nettoyer scripts racine et `vpn_state*.json`.

**Sortie** : dépôt propre, disque < 500 Mio, CI verte.

## Migration

- **Schéma** : v3 → v4 par migration additive (`ALTER TABLE ... ADD COLUMN`),
  aucun `DROP`, rollback = laisser les colonnes inutilisées.
- **Propriété** : flag `STATION_STATE_SOURCE=app|qualification` ; bascule en
  `shadow` 24 h, puis `app` ; retour arrière par flag, sans redéploiement.
- **Warm pool** : double écriture pendant 24 h (ancien + nouveau compteur),
  comparaison, puis retrait de l'ancien.
- **App** : lanceur supervisé testé sur une copie du service, bascule une fois.
- **Fork** : aucun changement d'image nécessaire pour 019 (l'API jobs existe
  déjà ou est ajoutée dans une PR séparée) ; migration douce 6→1.

## Rollback

| Changement | Rollback | Délai |
|---|---|---|
| Lanceur app | revenir au lancement manuel documenté | < 5 min |
| `station_state` source | flag → `qualification` | < 1 min |
| Quota v4 | flag `QUOTA_MODEL=legacy` ; colonnes ignorées | < 1 min |
| Warm pool | restaurer le fichier `registry.db` (backup) | < 10 min |
| Rotation async | revenir à l'appel bloquant (timeout 270 s) | < 5 min |
| Purge données | backup avant purge, restauration | < 30 min |

Chaque rollback est testé en Phase 0 sur une station ou une copie, jamais sur
les six.

## Risques et mitigations

| Risque | Mitigation |
|---|---|
| Re-prober des IP encore chaudes en changeant `ip_state` | TTL 10 min, jamais de verdict `dead` sur un seul 429 |
| Divergence pendant la bascule | double lecture + test, pas de double écriture |
| VACUUM bloque les écritures | `auto_vacuum=INCREMENTAL`, `VACUUM` hors pointe |
| Rotation async casse les tests existants | flag, contrat conservé, tests de compatibilité |
| App service mal configuré | test de kill avant bascule, rollback manuel documenté |

## Dépendances entre phases

`Phase 0 → Phase 1 → Phase 2 → Phase 3 → Phase 4`

Seule exception : 0.4 (purge) et 4.1 (requests) peuvent avancer en parallèle
de la Phase 1. La Phase 2 ne peut pas commencer avant 1.1 (autorité de
`station_state`), sinon la mesure du 6/6 serait fausse.

## Definition of Done

- [ ] `spec.md` US1–US6 satisfaites avec preuves SQL/log.
- [ ] 6/6 même cycle tenu 30 min.
- [ ] `warm_pool_live ≥ 6` et consommé.
- [ ] 0 `code reload` / 15 min.
- [ ] App relancée en < 30 s après kill.
- [ ] 100 % des resets respectés.
- [ ] `registry.db` < 50 Mio, `requests.db` < 500 Mio.
- [ ] CI verte, tests des 5 fonctions superviseur présents.
- [ ] Aucune 7ᵉ station, aucune régression de migration douce.