# Implementation Plan: Audit global et optimisation

**Branch** : `024-audit-optimisation-globale` | **Date** : 2026-11-20
**Spec** : [spec.md](./spec.md) | **Research** : [research.md](./research.md) |
**Data model** : [data-model.md](./data-model.md)
**Audit source** : `docs/audits/2026-11-20-audit-complet/` (10 rapports +
4 documents de preuves vérifiées + index, **12 479 lignes**)

## Summary

L'audit du 2026-11-20 établit que le système a de **bonnes fondations** (suite
de tests 6 379/6 385, aucune injection, aucun XSS, principes VIII et XVI
respectés, `requests.db` conforme à XIII sur 6/8) et des **défauts localisés
mais convergents** : **34 P0** qui se ramènent à **quatre familles causales**.

Le plan attaque les familles, pas les symptômes, dans un ordre imposé par les
preuves : d'abord **arrêter de perdre des données et de mentir sur l'état**
(P0), ensuite **restaurer la capacité de la flotte** (P1), puis **borner les
données et découper les monolithes** (P2), enfin **trancher la question du
langage sur des faits** (P3).

**Volumétrie de l'audit** : 10 rapports, **12 479 lignes**, **34 P0 · 102 P1 ·
98 P2 · ~120 P3**. Les P0 et P1 sont couverts à **100 %** par une tâche
(vérifié automatiquement) ; les P2 sont traités **par familles** avec une
règle de non-perte explicite.

Aucune phase ne touche la jambe payante.

## Technical Context

**Language/Version** : Python 3.12.10
**Primary Dependencies** : FastAPI 0.141.1, curl_cffi 0.16.1 (empreinte TLS
JA3 exacte), SQLite WAL, Docker Compose + fork Gluetun `016-control-plane`
(Go, 110 580 l.)
**Storage** : `registry.db` (52 tables, 159,4 Mo), `requests.db` (159,2 Mo),
`free_pool.json` (1,9 Mo), `logs/` (15 Go)
**Testing** : pytest (6 385 tests, 8 min 48), ruff, mypy, `platform_audit.py`,
`config_audit.py --strict`, `ci_contracts.py`
**Target Platform** : Windows 11 (poste opérateur), Linux (CI)
**Project Type** : service web unique + scripts d'exploitation
**Performance Goals** : surcoût proxy p50 < 30 ms (**déjà tenu : 14-15 ms**) ;
TTFB non dégradé ; UI p95 < 2 s
**Constraints** : free only, 6 stations, quota par IP, aucune bascule payante,
proxy en cours d'exécution sur la machine de l'opérateur
**Scale/Scope** : 6 stations, 12 conteneurs WARP, ~30 k proxys publics,
8+ clients, 143 k lignes Python de production

## Constitution Check

*GATE : doit passer avant la Phase 0 et être revérifié après chaque phase.*

| Principe | Gate | Statut actuel | Après ce plan |
|---|---|---|---|
| I — disponibilité mesurée | preuves horodatées, `client_usable` calculé | ❌ **violé** (58 stations fantômes, `free_pool.json` menteur, `SUM(tested)` ×20) | PASS |
| II — 3 objets, 3 cycles | station/session/IP distincts, cooldown sur l'IP | ⚠️ session **non comptée** (V-34) | PASS |
| III — une source de vérité | `registry.db` seule autorité | ❌ **violé** (5 tables multi-déclarants, 5 surfaces d'état) | PASS |
| IV — reset barrière dure | `reset_at` fournisseur uniquement | ⚠️ 102 IP `proven` sans `reset_at` | PASS |
| V — unicité d'IP | claim exclusif | ⚠️ `duplicate_ip_groups=1`, `egress_ip_in_use` compare entier/TEXT | PASS |
| VI — indépendance stations | aucun verrou inter-stations | ⚠️ garde AUTH partagée, dernier écrivain gagne | PASS |
| VII — aucune preuve, aucun succès | 200 avec contenu, même `wire_fp` | ❌ **violé** (gate public sans contenu, `get_public_ip` auto-déclarée) | PASS |
| VIII — un seul réparateur | superviseur propriétaire | ✅ respecté | PASS |
| IX — score ordonne, portes décident | 40/25/20/10/5 | ⚠️ `measured_score` alimenté par des constantes | PASS |
| X — dégradation explicite | 503/429 honnêtes | ✅ respecté | PASS |
| XI — fallbacks multiples | chaîne ordonnée | ⚠️ échelle DNS inerte en config livrée | PASS |
| XII — toute option a un effet | contrat option→consommateur→test | ❌ **violé** (gate 1 page/15, 6 clés legacy, `web_search.on_error`) | PASS |
| XIII — données traçables | PK/FK/contraintes/migrations/rétention | ❌ **violé** (15 179 FK, 22 tables hors schéma, 7 tables non purgées) | PASS |
| XIV — modulaire, strangler | ≤ 1 500 l./module | ❌ **violé** (9 modules, dont 3 créés après la constitution) | PASS |
| XV — multiplateforme | matrice CI Win+Linux | ❌ **violé** (CI mono-OS, 348 l. Windows dans `manager.py`) | PASS |
| XVI — silencieux | `run_hidden` partout | ⚠️ fond respecté (0 violation), forme non (18 sites directs) | PASS |
| XVII — fiabilité/perf/qualité | gates mesurables | ❌ **violé** (30 % `except: pass`, 5 assertions tautologiques, dette non à jour) | PASS |
| XVIII — brûlage de format | IP = 1 format, sonde exacte | ❌ **violé** (`wire_admission` mort, 2 `wire_fp`, 86,4 % `dead` sans preuve) | PASS |

**11 principes violés ou en risque, 2 respectés (VIII, X).** Aucune dérogation
demandée : le plan vise à les rendre tous PASS.

## Project Structure

### Documentation (cette feature)

```text
specs/024-audit-optimisation-globale/
├── spec.md              # ce qui doit être vrai
├── plan.md              # ce fichier
├── research.md          # les 4 familles causales + analyse du langage
├── data-model.md        # inventaire des données et objectifs
├── contracts/           # contrats d'invariant (schéma, état, preuve)
├── tasks.md             # tâches exécutables
└── checklist.md         # revue qualité
```

### Code source (structure cible)

```text
core/
└── process.py            # wrapper unique subprocess (existant, à généraliser)

observability/
├── db.py                 # schéma requests.db (existant)
├── state_authority.py    # NOUVEAU — une seule surface d'état
└── retention.py          # NOUVEAU — purge unifiée des 2 bases

free/
├── pool/                 # découper __init__.py (8 362 l.) en 8 modules
├── ip_state.py           # machine à états unique (fusionner avec le legacy)
├── wire_admission.py     # À BRANCHER (garde-fou XVIII)
├── artifact_check.py     # NOUVEAU — vérifie que tout artefact déclaré existe
└── providers/            # NOUVEAU — un adapter par provider

server/
├── request_preamble.py   # NOUVEAU — prologue commun aux 3 handlers
├── handlers/             # NOUVEAU — messages, chat, responses, systemone
└── lifespan.py           # NOUVEAU — boot hors monolithe

deploy/compose/           # NOUVEAU — overlays versionnés (sortis de logs/)
```

**Structure Decision** : strangler. Chaque lot extrait du code **et** le
supprime de la source. La façade `opencode.<nom>` est conservée par ré-export
pour ne casser aucun des 145 fichiers de tests qui référencent `oc.*`.

## 1. Stratégie — quatre phases, ordre imposé par les preuves

L'ordre n'est pas négociable : chaque phase **débloque la suivante**.

### Phase 0 — Arrêter de perdre et de mentir (P0, ~2 jours)

Les correctifs les moins risqués et les plus rentables. **Aucun refactor.**

| # | Correctif | Preuve du besoin | Effort |
|---|---|---|---|
| 0.1 | Déplacer `_resp_json_hors_boucle` avant le bloc `__main__` | **502 requêtes perdues** | S |
| 0.2 | `sys.modules.setdefault("opencode", sys.modules["__main__"])` | 2 connexions SQLite, télémétrie géo à 0 | S |
| 0.3 | Fusionner les deux `elif _geo_tunnel` (contournement géo) | `require_vpn: true` non appliqué | S |
| 0.4 | Versionner les overlays compose hors de `logs/` | un `compose up` casse 5/6 stations | S |
| 0.5 | Versionner/générer `official_payload.json.bak` | `StationProbe.__init__` → `FileNotFoundError` | S |
| 0.6 | Probe post-rotation : lire `body_raw` + `endpoint` | **3 jours sans rotation** | S |
| 0.7 | `repair_owner` : vérifier le superviseur avant de déléguer | refus client permanent | S |
| 0.8 | `_draining_stations` : n'ajouter que si la rotation est enfilée | station exclue à vie | S |
| 0.9 | Persister des durées restantes, pas `time.monotonic()` | stations bloquées des jours après reboot | S |
| 0.10 | Brancher `wire_admission` en mode `observe` | garde-fou XVIII mort | M |

**Sortie** : 0 requête perdue par exception avalée sur 24 h ; 0 station
fantôme ; un `compose up` ne casse rien ; le probe post-rotation réussit.

### Phase 1 — Restaurer la capacité de la flotte (P1, ~3 jours)

| # | Correctif | Cible mesurable |
|---|---|---|
| 1.1 | Une seule surface d'état (`registry.db`) + test de non-divergence | 0 divergence sur 24 h |
| 1.2 | Fusionner les 5 tables multi-déclarants dans `schema.sql` | 1 déclarant par table |
| 1.3 | `PRAGMA foreign_keys=ON` sur les 6 connexions écrivantes | `foreign_key_check` = 0 |
| 1.4 | Séparer `ours_used` de `upstream_verdict` | 6/6 possible par construction |
| 1.5 | Consommer le warm pool avant de qualifier | `warm_pool` vivant ≥ 6 |
| 1.6 | Préflight avant bind (DNS + port + egress, 5 s) | `bind_error` < 20 % |
| 1.7 | Budget de sondes séparé (`probe_budget`) réellement appliqué | 0 sonde sur la réserve client |
| 1.8 | Compteur de sessions NordVPN | ≤ limite du compte |
| 1.9 | `PYTEST_CURRENT_TEST` → flag de config explicite | chemin testé = chemin livré |
| 1.10 | Gate public : valider le contenu, pas seulement le code HTTP | 0 promotion sans preuve |

**Sortie** : 6/6 `client_usable` sur un cycle de 120 s tenu 30 min ;
taux de rotation WARP > 50 % ; taux de 200 > 30 %.

### Phase 2 — Borner, découper, outiller (P2, ~5 jours)

| # | Chantier | Cible |
|---|---|---|
| 2.1 | Purge unifiée des 2 bases (toutes les tables volumineuses) | `registry.db` < 50 Mo, `logs/` < 2 Go |
| 2.2 | Rétention des sauvegardes + `integrity_check` sur la copie | `logs/backups/` < 1 Go |
| 2.3 | `requests.db` : `auto_vacuum=INCREMENTAL` + `VACUUM` | `freelist_count` < 5 % |
| 2.4 | Télémétrie complète (`free_status` à 100 %) | 100 % des requêtes |
| 2.5 | Découpage `opencode.py` — lots L1–L17 | < 5 000 l. après L5, façade après L16 |
| 2.6 | Découpage `vpn/manager.py` — 15 modules | ≤ 1 500 l. chacun |
| 2.7 | Découpage `free/pool/__init__.py` — 8 modules | ≤ 1 500 l. chacun |
| 2.8 | Suppression du code mort (62 symboles inventoriés) | 0 symbole sans appelant |
| 2.9 | Nettoyage des 6 clés legacy + config morte | `config_audit --strict` à 0 |
| 2.10 | Réconciliation des ≈ 477 tâches SpecKit ouvertes | specs closes ou fusionnées |

**Sortie** : aucun module créé après le 2026-11-04 ne dépasse 1 500 lignes ;
`logs/` < 2 Go ; 0 option morte.

### Phase 3 — Gates et langage (P3, ~2 jours)

| # | Chantier | Cible |
|---|---|---|
| 3.1 | Gate option-map : 15 pages / 15 | 184 contrôles couverts |
| 3.2 | Matrice CI Windows + Linux | principe XV tenu |
| 3.3 | `platform_audit --strict` en CI | gate nommée exécutée |
| 3.4 | `gate.ps1` : `-m` au lieu de `-k` | 46 tests récupérés |
| 3.5 | Gate de taille de module | 0 nouveau module > 1 500 l. |
| 3.6 | Supprimer les 5 assertions tautologiques | 0 `or True` |
| 3.7 | Gate « artefact réel » : tout chemin déclaré existe | 0 dépendance non versionnée |
| 3.8 | `debt.md` à jour + gate de dette | 0 P0 ouverte |
| 3.9 | **Document d'architecture + décision de langage** | décision argumentée |

**Sortie** : les 8 gates de la constitution sont câblées et passent ; la
question du langage est tranchée sur des mesures.

## 2. La question du langage — recommandation

### 2.1 Ce que les mesures disent

| Mesure | Valeur |
|---|---|
| Plancher du proxy (hors génération) | **14-15 ms** |
| Latence médiane bout en bout | **1 203 ms** |
| Génération du modèle (deepseek) | **16,6 s** |
| Coût d'un `503` / `429` | **19,7 s** / **11,5 s** |
| Gain maximal d'un portage | **1,2 %** de la médiane |
| Go déjà écrit et fonctionnel | **110 580 lignes** (`gluetun/`) |
| Python de production à porter | ≈ **143 000 lignes** |
| Tests Python à réécrire | **107 377 lignes** |
| Bloqueur | `curl_cffi` (**281** usages, JA3 `1523504b…`) |

### 2.2 Recommandation : **ne pas porter**

Trois raisons mesurées :

1. **Le gain est de 1,2 %** sur la métrique qui compte. Le temps est dans la
   génération du modèle et dans les retries d'échec — deux postes qu'un
   changement de langage **ne touche pas**.
2. **Le coût est de ~250 000 lignes** à réécrire, avec un bloqueur non
   négociable : l'empreinte TLS JA3 exacte. Une erreur d'empreinte **brûle une
   IP** (principe XVIII).
3. **Aucun des 5 P0 n'est un problème de langage.** Ce sont des défauts de
   logique, d'autorité d'état et de gates. Les porter en Go les reproduirait
   à l'identique, avec un coût de réécriture en plus.

### 2.3 Ce qu'il faut faire à la place

- **Extraire, pas traduire.** Les plans de découpage (17 lots pour
  `opencode.py`, 15 pour `vpn/manager.py`, 8 pour `free/pool`) rendent chaque
  module **portable indépendamment** si le besoin se confirme.
- **Go là où c'est déjà le cas.** Le fork Gluetun (110 580 l.) est en Go et
  fonctionne : c'est le langage du composant le plus critique. Si un composant
  doit rejoindre Go, c'est le **chemin chaud de sélection** — pas tout le proxy.
- **Mesurer avant de décider.** Après le découpage, si un module précis
  (sélection, budget de sondes) montre un **coût CPU mesuré**, il devient un
  candidat de portage **isolé**, avec un test de parité — jamais une réécriture
  big-bang.

### 2.4 Ce qui changerait la recommandation

| Si… | alors |
|---|---|
| le proxy devait servir > 1 000 requêtes/s | la concurrence (GIL) deviendrait le goulot → Go |
| un module montrait > 50 % de CPU en profilage | portage isolé de ce module |
| `curl_cffi` était remplacé par un besoin d'empreinte plus simple | le bloqueur principal tombe |
| la latence médiane tombait sous 100 ms (modèles locaux) | les 14 ms deviendraient significatifs |

**Aucune de ces conditions n'est vraie aujourd'hui.**

## 3. Migration et rollback

| Changement | Rollback | Délai |
|---|---|---|
| Correctifs P0 (Phase 0) | revert du commit (correctifs isolés) | < 5 min |
| Overlays versionnés | revenir aux fichiers `logs/` (conservés) | < 2 min |
| Surface d'état unique | flag `STATE_SOURCE=legacy` | < 1 min |
| Schéma unifié | backup `VACUUM INTO` avant migration | < 10 min |
| Purge de données | backup vérifié avant purge | < 30 min |
| Découpage (chaque lot) | revert du lot (façade conservée) | < 5 min |

Chaque rollback est testé **avant** application, jamais sur les 6 stations
simultanément.

## 4. Risques et parades

| Risque | Parade |
|---|---|
| Un correctif P0 casse la flotte en cours | tester sur 1 station, jamais 6 ; flag de désactivation |
| La purge détruit une donnée utile | `VACUUM INTO` vérifié avant, rétention configurable |
| Le découpage casse les 145 fichiers de tests | façade `oc.*` conservée par ré-export, un lot = un commit |
| Le changement de schéma casse une écriture | migration additive, jamais de `DROP`, backup préalable |
| Le proxy tombe pendant l'application | correctifs appliqués par lots, proxy relancé entre chaque |
| Le plan lui-même devient un 478ᵉ backlog | chaque phase a un critère de sortie **mesuré**, pas une liste |

## 5. Definition of Done

- [ ] SC-001 : 6/6 `client_usable` sur un cycle de 120 s, tenu 30 min.
- [ ] SC-002 : 0 requête perdue par exception avalée sur 24 h.
- [ ] SC-003 : `logs/` < 2 Go, `registry.db` < 50 Mo.
- [ ] SC-004 : taux de rotation WARP > 50 %.
- [ ] SC-005 : 0 divergence entre surfaces d'état sur 24 h.
- [ ] SC-006 : aucun module post-constitution > 1 500 lignes.
- [ ] SC-007 : les 8 gates de la constitution câblées et vertes.
- [ ] SC-008 : un `git clone` neuf produit une flotte fonctionnelle.
- [ ] Les 11 principes violés passent à PASS.
- [ ] La décision de langage est documentée et argumentée sur les mesures.
- [ ] `docs/DEBT.md` reflète l'état réel, 0 P0 ouverte.

## 6. État d'exécution

**Phase 0 — démarrée le 2026-11-20.** Les 3 correctifs P0 de `opencode.py`
sont **livrés et vérifiés** :

| Tâche | Correctif | Vérification |
|---|---|---|
| **T001** | `_resp_json_hors_boucle` déplacée avant le bloc `__main__` | AST : définition index **818** < main **892** ; `test_a11` 2/2 |
| **T002** | Alias `sys.modules` (mono-namespace) | Exécution : alias **identique** à `__main__`, import ultérieur = **même objet**, **0** module chargé en plus |
| **T003** | Fusion des `elif _geo_tunnel` (contournement géo) | AST : **7 blocs**, **0 lecture non assignée** de `_actual_model`, **7/7** appellent `_open_via_pool` |

**Validation** :

| Contrôle | Résultat |
|---|---|
| Suite complète (`-k "not docker"`) | **exit 0** — **6 334 passed**, 7 skipped, **0 failed**, 0 error |
| `ruff check opencode.py` | **All checks passed** (10 erreurs avant → 0 après) |
| `config_audit --strict` | 0 dead / 0 missing |
| `platform_audit --strict` | 0 violation / 279 fichiers |
| `docker compose config` | OK — **11/11** services VPN sur le fork |

**Tâches P0 traitées : T001, T002, T003, T004 (résolu par T003), T005.**

**Deux découvertes en exécutant** :

- **V-66** — `scripts/fresh_chain.py` garde l'image upstream **légitimement**
  (`docker run --rm --entrypoint sh`, un shell jetable). Le test
  `test_manifest_pinning.py` protégeait une bonne intention avec un critère
  obsolète ; mis à jour sans perdre son intention.
- **V-67** — `official_payload.json.bak` **n'est pas** la capture 1.19.5 que
  le code prétend (`_body_sha256` **identique** au principal). Le correctif
  du 2026-11-14 n'a pas résolu le problème de format visé ; T006 est corrigé
  en conséquence (retirer le profil 1.19.5 plutôt que versionner le `.bak`).

**Reste à valider en fenêtre d'exploitation** (le proxy tourne sur le poste de
l'opérateur, jamais redémarré pendant l'audit) :

- un seul `SQLite connection established` au prochain démarrage (T002) ;
- un appel réel à `/v1/responses` non-stream → 200 au lieu de 502 (T001) ;
- une requête géo-restreinte sort par le tunnel (T003).

**Prochaines tâches recommandées** : **T005** (versionner les overlays
compose — protège la flotte et débloque la purge de `logs/`) puis **T154**
(auditer les 2 modules de classification d'échec avant la Phase 1).
