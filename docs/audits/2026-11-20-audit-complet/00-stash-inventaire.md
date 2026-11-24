# Inventaire du `git stash` — livrables de la spec 019 jamais commités

Le `git stash@{0}` (`spec19-wip-2026-11-14`, commits d'untracked `bb0e80d`)
contient **35 fichiers / 4 691 lignes** produits le 2026-11-14 et **jamais
commités**. La branche courante s'appelle `019-audit-optimisation-flotte-free`
mais l'arbre de travail n'en contenait aucun.

## 1. Documents — RESTAURÉS par l'audit

Les 5 fichiers de spécification ont été restaurés dans l'arbre de travail
(`git checkout bb0e80d -- specs/019-audit-optimisation-flotte-free/`) :

| Fichier | Lignes | Contenu |
|---|---|---|
| `spec.md` | 200 | objectif 6/6, 5 causes racines, contraintes dures, US1–US3 |
| `plan.md` | 218 | décisions D1–D6, architecture cible, phases 0–3 |
| `audit.md` | 755 | audit SRE du 2026-11-14 (mesures, 5 P0, 10 P1, 6 P2) |
| `tasks.md` | 259 | T301–T3xx |
| `checklist.md` | 114 | revue de qualité |

**Raison de la restauration** : ce sont des documents, sans effet
d'exécution. La branche porte leur nom ; leur absence rendait la branche
incohérente et privait tout repreneur de l'audit de référence.

## 2. Code et tests — NON RESTAURÉS (décision de l'opérateur requise)

**17 fichiers** du stash n'existent toujours pas dans l'arbre de travail :

### 2.1 Modules d'observabilité (5)

| Fichier | Rôle attendu (spec 019) |
|---|---|
| `observability/auth_pause.py` (200 l.) | T314 — un seul écrivain de la pause AUTH |
| `observability/fleet_slo.py` (274 l.) | T302/T313 — SLO de flotte, « 6/6 même cycle » |
| `observability/pins.py` (237 l.) | T306 — table `pins`, `.env` généré |
| `observability/station_state.py` (232 l.) | T305 — `station_state` comme read-model |
| `observability/crashguard.py` | *(déjà présent)* |

### 2.2 Scripts d'exploitation (6)

| Fichier | Rôle attendu |
|---|---|
| `scripts/maintenance_purge.py` (199 l.) | T304 — purge `registry.db` |
| `scripts/preflight.py` (74 l.) | T311 — préflight avant bind |
| `scripts/slo_monitor.py` (112 l.) | T313 — mesure continue 6/6 |
| `scripts/warp_bound_fleet.py` (252 l.) | T310 — flotte WARP bornée |
| `scripts/diagnostics/*.py` (5 fichiers) | outils de diagnostic |
| `scripts/run_app_supervisor.ps1` (9 l.) | lanceur superviseur |

### 2.3 Tests (11)

| Fichier | Ce qu'il vérifie |
|---|---|
| `tests/test_state_authority.py` (131 l.) | **T308** — non-divergence API / `station_state` / fork |
| `tests/test_pins.py` (75 l.) | T306 — un seul écrivain de pin |
| `tests/test_reset_at.py` (143 l.) | T312 — respect strict de `reset_at` |
| `tests/test_warm_pool.py` (94 l.) | T310 — consommation du warm pool |
| `tests/test_quota_verdict.py` (80 l.) | T309 — `ours_used` vs `upstream_verdict` |
| `tests/test_auth_pause.py` (81 l.) | T314 — un seul écrivain de pause |
| `tests/test_rotation_queue.py` (70 l.) | T-rotation — file visible |
| `tests/test_preflight.py` (42 l.) | T311 — préflight |
| `tests/test_slo_monitor.py` (51 l.) | T313 — SLO |
| `tests/test_warp_cap.py` (143 l.) | P1-1 — capacité WARP IPv4 = 0 |
| `tests/test_no_visible_spawn.py` | *(déjà présent)* |

**Pourquoi ne pas les restaurer automatiquement** :

1. Ils écrivent/attendent des modules (`observability/pins.py`, etc.) qui
   n'existent pas — les restaurer seuls **casserait la collecte pytest**
   (`ImportError`) et donc la gate.
2. Ils touchent le chemin de sélection (`free/pool`) alors que **le proxy
   tourne en ce moment** sur cette machine (CLAUDE.md : « restart = tuer le
   process pythonw et relancer »). Restaurer du code non validé dans un
   processus vivant est une décision d'exploitation, pas d'audit.
3. Leur contenu n'a **jamais été relu ni testé** contre l'arbre actuel
   (l'arbre a changé depuis le 14/11 : +276 lignes dans `free/pool`).

**Recommandation pour le plan** : traiter ce bloc comme une **entrée de
travail à examiner**, pas comme du code à adopter. Chaque fichier doit être
relu, confronté à l'état actuel, puis soit adopté (avec ses tests), soit
abandonné explicitement. Le point de départ le plus rentable est
`tests/test_state_authority.py` — c'est le garde-fou T308 qui manque
aujourd'hui (V-22 : l'état persisté contredit l'état live, aucun test ne le
détecte).

## 3. Comment consulter un fichier du stash sans le restaurer

```bash
git show bb0e80d:observability/pins.py
git show bb0e80d:tests/test_state_authority.py
```

## 4. Leçon

Un travail d'audit de 755 lignes et 35 fichiers de correctifs a été produit
le 2026-11-14, puis **laissé dans un stash pendant 6 jours** pendant que la
branche qui porte son nom restait sans son dossier `specs/`. C'est la raison
mécanique pour laquelle les 5 P0 de cet audit sont encore ouverts
aujourd'hui : ils n'ont jamais eu de trace versionnée.

**Règle à retenir** : un livrable d'audit non commité n'existe pas.
