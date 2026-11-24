# État des specs SpecKit — 2026-11-20

Comptage fait sur `specs/*/tasks.md` (cases `[ ]` / `[x]` / `[~]`).
Les specs sans `tasks.md` (015, 017, 018, 020, 021, 022) suivent leur état
dans `checklist.md` ou `spec.md` et sont notées séparément.

## Décompte

| Spec | Titre | tâches | faites | partielles | **restantes** |
|---|---|---|---|---|---|
| 001 | free-station-availability | 41 | 5 | 0 | **36** |
| 002 | vpn-catalog-rotation | 90 | 66 | 18 | **6** |
| 003 | official-client-parity | 89 | 18 | 6 | **65** |
| 004 | repo-parity-audit | 53 | 36 | 0 | **17** |
| 005 | free-model-continuity-and-egress-gui | 72 | 41 | 3 | **28** |
| 006 | three-egress-modes | 25 | 25 | 0 | 0 ✅ |
| 007 | warp-egress-fallback | 68 | 0 | 0 | **68** |
| 008 | auto-egress-mode | 70 | 8 | 0 | **62** |
| 009 | dashboard-ia-reorg | 46 | 0 | 0 | **46** |
| 010 | egress-station-sizing | 30 | 30 | 0 | 0 ✅ |
| 011 | station-fleet-usability | 117 | 38 | 0 | **79** |
| 012 | request-reliability | 35 | 33 | 0 | **2** |
| 013 | egress-catalog | 36 | 36 | 0 | 0 ✅ |
| 014 | nordvpn-exhaustive-rotation | 175 | 144 | 0 | **31** |
| 016 | gluetun-control-plane | 11 | 0 | 0 | **11** |
| 023 | client-usable-egress | 73 | 44 | 4 | **25** |
| **TOTAL** | | **1 032** | **524** | **31** | **≈ 477** |

Trois specs sont terminées (006, 010, 013). **Treize specs ont des tâches
ouvertes**, dont quatre avec plus de 45 restantes (003, 007, 008, 009, 011).

## Specs sans `tasks.md`

| Spec | État déclaré |
|---|---|
| 015-egress-uptime-slo | pas de tasks.md |
| 017-fork-soft-migration | ADR-017-01 seulement |
| 018-autonomous-six-station-recovery | T201–T211 dans `tasks.md` (177 l.), **T211 ouverte** selon l'audit 019 |
| 020-disable-workspace-path-impersonation | pas de tasks.md |
| 021-app-crash-resilience | P0 livré, P1 partiel ; **T028–T029, T031–T035 ouverts** (déclaré dans `spec.md`) |
| 022-zero-429-client-reserve | checklist : 4 cases ouvertes (probe budget, log de bascule, tableau de bord, soak) |

## Spec 019 — restaurée par cet audit

La branche courante est `019-audit-optimisation-flotte-free`, mais
`specs/019-audit-optimisation-flotte-free/` **n'existait pas dans l'arbre de
travail** au début de cet audit. Ses 5 fichiers étaient dans
**`git stash@{0}`** (`spec19-wip-2026-11-14`, commit d'untracked `bb0e80d`).

**Action corrective de l'audit** : les 5 documents ont été restaurés dans
l'arbre de travail et indexés :

```
$ git checkout bb0e80d -- specs/019-audit-optimisation-flotte-free/
$ ls specs/019-audit-optimisation-flotte-free/
audit.md  checklist.md  plan.md  spec.md  tasks.md
```

| Fichier | Lignes |
|---|---|
| `audit.md` | 755 |
| `tasks.md` | 259 |
| `plan.md` | 218 |
| `spec.md` | 200 |
| `checklist.md` | 114 |

Le stash contenait **35 fichiers au total** (4 691 lignes) : les 5 documents
ci-dessus, **5 modules** d'observabilité, **6 scripts** d'exploitation et
**11 fichiers de tests**. Les 17 fichiers de **code** n'ont **pas** été
restaurés — ils référencent des modules absents (casserait la collecte
pytest) et touchent le chemin de sélection d'un proxy en cours d'exécution.
Inventaire complet et recommandations : `00-stash-inventaire.md`.

Aucune des tâches T301–T3xx n'était suivie ; elles le sont désormais.

## Ce que cela signifie

Le projet a accumulé **≈ 477 tâches SpecKit ouvertes** réparties sur 13
specs, dont plusieurs se recouvrent largement :

- **007-warp-egress-fallback (68)** et **023-client-usable-egress** traitent
  tous deux du WARP comme provider de première classe ;
- **008-auto-egress-mode (62)** et **005 (28)** traitent tous deux du choix
  automatique d'egress ;
- **009-dashboard-ia-reorg (46)** et **011-station-fleet-usability (79)**
  traitent tous deux de l'IHM de flotte ;
- **003-official-client-parity (65)** recoupe la couche L0–L6 que la spec 023
  a re-spécifiée en 2026-11-17.

Un plan d'optimisation doit **d'abord réconcilier ces specs** (fusionner,
clore ou annuler) avant d'ajouter une 24ᵉ couche de tâches.
