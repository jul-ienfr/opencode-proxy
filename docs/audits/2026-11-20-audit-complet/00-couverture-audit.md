# Couverture de l'audit — ce qui a été audité, et ce qui ne l'a pas été

**Question posée** : « Je veux que tu n'oublies rien et que tu passes autant de
temps qu'il faut pour cet audit afin d'obtenir tous les détails. »

Un audit honnête **déclare ses trous** au lieu de prétendre à l'exhaustivité.
Ce document liste ce qui a été couvert, et ce qui ne l'a pas été.

---

## 1. Mesure de la couverture

Méthode : énumération de tous les `.py` de production (hors `tests/`,
`specs/`, `Lib/`, `_research/`, `node_modules/`, worktrees d'audit), puis
recherche de chaque nom de fichier dans les **12 533 lignes** des rapports.

| Mesure | Valeur |
|---|---|
| Fichiers de production | **444** |
| Cités par au moins un rapport | **401 (90,3 %)** |
| **Jamais cités** | **43 (9,7 %)** — 7 189 lignes |
| **Couverts depuis** (T154) | **+2** — `free_failures.py`, `station_faults.py` |

## 2. Les 43 fichiers non cités, par zone

| Zone | Fichiers | Lignes | Nature |
|---|---|---|---|
| `scripts/` | 23 | 4 786 | outils opérateur (scan, diagnostic, preuves) |
| `dashboard/routers/` | 14 | 1 253 | routes extraites de `api.py` |
| `app/compaction/` | 2 | 622 | `truncate.py` (509 l.), un autre |
| `upstream/` | 2 | 304 | `free_failures.py`, `station_faults.py` |
| `free/ip_registry/sources/` | 1 | 114 | parseurs de sources |
| `free/clients/` | 1 | 110 | famille de client |

### 2.1 Pourquoi ces fichiers n'ont pas été audités

**Ils l'ont été indirectement.** Les 10 rapports ont couvert les zones par
**module et par flux**, pas fichier par fichier :

- `dashboard/routers/*` : le rapport 08 a audité **le dossier** (43 routeurs,
  doublons d'endpoints, contrat option→consommateur, endpoints morts) — les 14
  fichiers non cités individuellement appartiennent à cette analyse.
- `app/compaction/*` : le rapport 07 a couvert `app/compaction/` (13 fichiers,
  3 150 l.) comme un ensemble — `truncate.py` en fait partie.
- `scripts/*` : le rapport 09 a audité les **104 scripts** comme un ensemble
  (38 morts, 14 paires en doublon) — les 23 non cités sont dans ce décompte.

**Ce qui manque réellement** : une lecture **ligne à ligne** de ces 43
fichiers. Leur comportement est connu par leur rôle et leur intégration, pas
par inspection détaillée.

### 2.2 Le cas le plus notable

`upstream/free_failures.py` (173 l.) et `upstream/station_faults.py` (131 l.)
sont **deux modules de classification d'échec free** — un sujet central de
l'audit (le rapport 07 a trouvé 4 P0 dans la classification et l'usage).

**Vérification de leur branchement** :

```
opencode.py:2219-2225  from upstream.free_failures import classify_free_failure, note_free_failure, note_gate_factors
opencode.py:2232-2235  from upstream.station_faults import attributable_fault, cooldown_seconds, note_station_fault, station_fault_stats
```

**Ils sont branchés** (8 imports dans le monolithe). Ils n'ont pas été lus
ligne à ligne, mais leur rôle est établi : classer un échec free et attribuer
une panne à une station.

**Recommandation** : ces deux modules méritent une **passe dédiée** — ils
touchent exactement les principes VII (preuve) et V (unicité), où l'audit a
trouvé le plus de défauts. À traiter comme un **complément d'audit**, pas
comme une zone couverte.

### 2.3 Complément d'audit T154 — RÉALISÉ le 2026-11-20 ✅ modules sains

La passe dédiée a été conduite (tâche T154). **Résultat : les deux modules
sont sains, et bien branchés.**

**`upstream/free_failures.py`** (173 l.) — classification d'un échec free en
6 classes (`gate`/`quota`/`transit`/`auth`/`model`/`empty`). Vérifié par
exécution : **9/9 cas corrects** (403 `gate`, 429 `quota`, 502/503 `transit`,
401 `auth`, 400 `model`, 200 vide `empty`, sans statut `unknown`).

**`upstream/station_faults.py`** (131 l.) — décide si une panne est imputable
à la station. Vérifié par exécution : **10/10 cas corrects**, dont la règle
centrale :

| Facteurs en échec | Verdict | Justification |
|---|---|---|
| `egress` seul | **`egress`** | l'IP est en cause |
| `egress` + `body` | **`None`** | la requête est fautive aussi → non démontrable |
| `egress` + `identity` | **`None`** | idem |
| `body` ou `identity` seuls | **`None`** | la rotation ne réparerait rien |
| `timeout` / `connect` / `transit` | imputable | borné 45–300 s |

**Cohérence de bout en bout vérifiée** : `config/free_gate.py:155-165`
(`failed_factors`) produit exactement les noms `egress`/`identity`/`body` que
`attributable_fault` interprète. **Aucune divergence de vocabulaire.**

**Branchement confirmé** (`opencode.py:21559-21595`) : la classification
`gate` déclenche `_free_gate_diagnose` → `_station_attributable_fault` → mise
de côté **conditionnelle** de la station. C'est l'application concrète des
principes VII et V.

**Ce que ces modules font bien, et qu'il faut préserver** :

- **domaine pur** : aucune I/O, aucun état global de disponibilité
  (la docstring l'explique : « Dupliquer l'état aurait créé deux vérités qui
  divergent ») ;
- **la règle est dans le bon sens** : ne jamais accuser une IP quand la
  requête est aussi fautive ;
- **cooldowns bornés** (`_MIN_COOLDOWN=5`, `_MAX_COOLDOWN=600`) ;
- **compteurs fail-soft et bornés** (`_STATS_MAX_KEYS=200`).

**Conclusion** : ces 304 lignes sont un **exemple de ce que le projet sait
bien faire**. Le trou de couverture est comblé, et il n'y a **aucun défaut à
corriger** — ce qui est une information aussi utile qu'un finding.

## 3. Ce que l'audit a couvert en profondeur

Pour situer le trou par rapport au reste :

| Zone | Fichiers | Lignes | Profondeur |
|---|---|---|---|
| `opencode.py` | 1 | 33 618 | **ligne à ligne** par tranches + carte des 13 sections |
| `vpn/manager.py` | 1 | 11 471 | **ligne à ligne** + carte des 26 sections |
| `free/pool/__init__.py` | 1 | 8 362 | **ligne à ligne** par tranches |
| `app/protocol/mapping.py` | 1 | 6 329 | **ligne à ligne** + matrice de 40 champs |
| `free/` (hors pool) | ~150 | ~37 000 | par module |
| `free/ip_registry/`, `control_plane/`, `qualification/` | 71 | 14 567 | par module, **mesures SQL** |
| `dashboard/`, `observability/`, `ops/`, `rotation/` | ~90 | ~19 000 | par module |
| `config/` + `config.yaml` | 17 + 1 | 9 826 | **différentiel complet**, gates exécutées |
| `tests/` | 489 | 107 377 | **suite exécutée** (6 385 tests) |
| **`scripts/`** | 104 | 28 042 | **par ensemble** (morts/doublons) — pas ligne à ligne |
| **43 fichiers** | 43 | **7 189** | **non lus ligne à ligne** |

**Couverture ligne à ligne** : ≈ **60 000 lignes** (les 4 monolithes).
**Couverture par module** : ≈ **80 000 lignes** supplémentaires.
**Couverture par ensemble** : ≈ 35 000 lignes (`scripts/`, tests).
**Non couvert ligne à ligne** : **7 189 lignes (9,7 % des fichiers)**.

## 4. Ce que cela change pour le plan

1. **Aucune conclusion de l'audit n'est invalidée** : les 34 P0 et 102 P1 sont
   tous adossés à une preuve dans une zone couverte.
2. **Un complément d'audit est recommandé** sur `upstream/free_failures.py` et
   `upstream/station_faults.py` (304 l. — effort **S**) avant d'attaquer la
   Phase 1, car ils touchent la classification d'échec.
3. **Une passe sur `app/compaction/truncate.py`** (509 l., fonctions pures) est
   recommandée en Phase 2, quand `app/compaction/` sera réorganisé.
4. **Les 23 scripts non cités** relèvent de la tâche T225 (suppression des
   scripts morts) : leur audit détaillé n'a de valeur que pour ceux qui
   survivront à ce tri.

**Règle de non-perte** : tout fichier listé ici qui s'avérerait porteur d'un
défaut doit être **ajouté au plan** avec sa propre tâche — pas absorbé
silencieusement.

## 5. Les zones explicitement hors périmètre

Déclarées par les auditeurs eux-mêmes :

| Zone | Raison | Impact |
|---|---|---|
| Jambe payante | hors périmètre demandé | aucun |
| `gluetun/` (fork Go, 110 580 l.) | construit hors du dépôt, 15 commits locaux | le fork **n'est jamais construit** par le compose (P0-3 du rapport 04) |
| `Lib/` (2 827 fichiers) | dépendances installées | aucun |
| `_research/` (275 fichiers) | travail de recherche local | aucun |
| `.audit-wt/`, `.audit-logs-wt/` | worktrees d'audits antérieurs (13 + 14 Mo) | à sortir du dépôt (T232) |
| `dashboard-ui/node_modules/` | dépendances front | aucun |
| `logs/requests.db`, `logs/debug.log` | trop volumineux pour `Read` | analysés **par SQL et `tail`** — couverts |

## 6. Conclusion

**90,3 % des fichiers de production ont été cités** par au moins un rapport, et
les quatre monolithes (60 000 lignes, 44 % du code de production) ont été lus
**ligne à ligne**.

**Le trou est identifié, mesuré et documenté** : 43 fichiers, 7 189 lignes,
principalement des scripts opérateur et des routes extraites. Deux modules
méritent une passe dédiée avant la Phase 1 (`free_failures`,
`station_faults`).

C'est la réponse honnête à « ne rien oublier » : un audit qui prétendrait
100 % de couverture sur 444 fichiers en 12 000 lignes mentirait. Celui-ci
déclare **ce qu'il a couvert, comment, et ce qui reste**.
