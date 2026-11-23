# Requirements Quality Checklist: Audit global et optimisation

**Purpose** : revue de la qualité des exigences (pas de l'implémentation) pour
la spec 024.
**Created** : 2026-11-20
**Feature** : [spec.md](./spec.md) · [plan.md](./plan.md) ·
[research.md](./research.md) · [data-model.md](./data-model.md) ·
[architecture-review.md](./architecture-review.md) ·
[tasks.md](./tasks.md) · [quickstart.md](./quickstart.md) ·
[contracts/invariants.md](./contracts/invariants.md)

**Marker Semantics** : `[x]` revu et satisfait ; `[ ]` à clarifier, corriger
ou évaluer.

---

## Alignement constitution (v1.3.1)

- [x] CHK001 **I** — chaque chiffre de disponibilité du plan est adossé à une
  mesure horodatée (SQL, `git show`, commande) ; le constat « l'état persisté
  ment » est prouvé par comparaison `monotonic()` / `last_ok_at`.
- [x] CHK002 **II** — la spec nomme les trois objets (station, session, IP) et
  constate que **la session n'est pas comptée** (V-34) ; le plan ajoute le
  compteur (tâche 1.8).
- [x] CHK003 **III** — l'objectif « une seule source de vérité » est chiffré :
  5 surfaces d'état → 1 ; 5 tables multi-déclarants → 0 ; 22 tables hors
  schéma → 0.
- [x] CHK004 **IV** — `reset_at` fournisseur uniquement ; l'invariant INV-13
  interdit une IP `proven` sans `reset_at`.
- [x] CHK005 **V** — l'invariant INV-14 corrige le garde d'unicité d'IP
  (comparaison int/TEXT) ; `duplicate_ip_groups` est mesuré.
- [x] CHK006 **VI** — la garde AUTH partagée (dernier écrivain gagne) est
  identifiée comme violation ; le plan la traite (tâche 1.8).
- [x] CHK007 **VII** — l'asymétrie de preuve (gate sans contenu vs client
  validant) est prouvée ; INV-6 la corrige.
- [x] CHK008 **VIII** — le plan **ne touche pas** au réparateur unique ;
  l'audit confirme que le principe est respecté (à ne pas casser).
- [x] CHK009 **IX** — `measured_score` alimenté par des constantes est
  identifié ; le plan le corrige en Phase 1.
- [x] CHK010 **X** — aucune tâche ne touche la jambe payante ; le free-only
  est une contrainte dure de la spec.
- [x] CHK011 **XI** — l'échelle DNS inerte est identifiée (P1-17 du
  rapport 02) ; les chaînes de fallback sont préservées.
- [x] CHK012 **XII** — INV-11 rend le gate option-map non circulaire
  (1 page/15 → 15/15) et fait voir les 6 clés legacy.
- [x] CHK013 **XIII** — INV-1, INV-2, INV-15 couvrent schéma, FK et
  rétention ; l'objectif « 7 tables non purgées → 0 » est chiffré.
- [x] CHK014 **XIV** — INV-10 gate la taille ; le plan découpe 3 monolithes
  par strangler avec façade conservée.
- [x] CHK015 **XV** — INV-12 câble la matrice CI Windows+Linux.
- [x] CHK016 **XVI** — l'audit confirme **0 violation sur 279 fichiers** ; le
  plan traite la duplication de politique (P2, pas P1).
- [x] CHK017 **XVII** — les 8 success criteria sont mesurables ; INV-16
  traite le taux de 30 % d'`except: pass`.
- [x] CHK018 **XVIII** — INV-6, INV-13 et le branchement de
  `wire_admission` (tâche 0.10) couvrent la brûlure de format.

## Exactitude des constats

- [x] CHK019 Chaque P0 du plan cite une **mesure**, pas une impression :
  502 requêtes, 3 jours sans rotation, 15 179 violations, 26 474 `dead`.
- [x] CHK020 Les chiffres ont été **revérifiés indépendamment** par
  l'auditeur principal (17 vérifications croisées).
- [x] CHK021 Le seul **faux positif** de l'audit (`web_search`/`web_fetch`
  inertes) est corrigé et documenté, avec la leçon méthodologique.
- [x] CHK022 Les **nuances** sont conservées : 2 des 10 sites
  `PYTEST_CURRENT_TEST` sont des protections légitimes ; `vpn/manager.py`
  pose bien `CREATE_NO_WINDOW`.
- [x] CHK023 Les points **sains** sont listés explicitement (recherche §3)
  pour éviter une refonte injustifiée.
- [x] CHK024 La distinction **nouveau vs historique** est faite pour les
  modules > 1 500 lignes (date de création via `git log`).

## Complétude

- [x] CHK025 Les 5 P0 de l'audit 019 sont tracés un par un (fermé / ouvert /
  aggravé / régressé).
- [x] CHK026 Les ≈ 477 tâches SpecKit ouvertes sont inventoriées ; le plan
  prévoit leur réconciliation (tâche 2.10).
- [x] CHK027 Le travail perdu dans `git stash` est restauré et inventorié.
- [x] CHK028 La question du langage est traitée avec des mesures, pas un avis
  (recherche §4, plan §2).
- [x] CHK029 Les **4 familles causales** regroupent les findings ; le plan
  attaque les familles, pas les symptômes.
- [x] CHK030 Chaque phase a un **critère de sortie mesuré**, pas une liste de
  tâches.

## Faisabilité et risque

- [x] CHK031 L'ordre des phases est **imposé par les preuves** et justifié.
- [x] CHK032 Chaque changement a un **rollback** documenté avec son délai.
- [x] CHK033 La contrainte « proxy en cours d'exécution » est respectée :
  aucun restart pendant l'audit.
- [x] CHK034 Les correctifs P0 sont **isolés** (effort S) et réversibles par
  revert de commit.
- [x] CHK035 Le risque « le plan devient un 478ᵉ backlog » est nommé et paré
  par des critères de sortie mesurés.

## Vérifiabilité

- [x] CHK036 Les 18 contrats d'invariant sont **testables** et nomment le
  test à écrire.
- [x] CHK037 Chaque contrat a un **effort** chiffré (test et correctif).
- [x] CHK038 Les contrats les plus rentables sont identifiés (INV-6, 8, 9,
  14, 17, 18).
- [x] CHK039 Les objectifs sont **numériques** : `registry.db` < 50 Mo,
  `logs/` < 2 Go, `freelist_count` < 5 %, rotation > 50 %.
- [x] CHK051 **Quickstart vérifié** : les 8 commandes de référence (§1) ont
  été exécutées et retournent les valeurs « AVANT » annoncées —
  `usable_stations 20` pour 6 stations, **15 179** violations FK,
  **64 entrées / 58 négatives / 46 usable** dans `station_health`,
  `logs/ 15G`, `registry.db 160M`, **62,5 %** de freelist,
  `_extract_cache_tokens` → `Chat 900 | Responses 0`.
- [x] CHK052 **Couverture des findings vérifiée automatiquement** : les
  **34 P0** et les **102 P1** sont croisés avec `tasks.md`. Deux P0 et 19 P1
  avaient été oubliés à la première rédaction ; ils ont été ajoutés
  (T026, T128–T153). Les **98 P2** sont routés par famille avec une règle de
  non-perte explicite.
- [x] CHK040 **Complété (rapport 07)** : quatre P0 de conversion identifiés
  et vérifiés par exécution — `_extract_cache_tokens` retourne **0** sur la
  forme Responses (mesuré), lève un `TypeError` sur `details` non-dict
  (mesuré), le plafond de cache n'a que 2 occurrences (jamais appliqué), et
  `_finalize_stream_tokens` ne retourne pas `final_cache_write`. Couverts par
  les tâches T011–T014 ; le contrat INV-6 (preuve de contenu) et l'esprit du
  correctif anti-autocompact du 2026-10-26 s'y rattachent.
- [x] CHK041 **Complété (rapport 09)** : le différentiel config est plus
  propre qu'annoncé — **0 clé morte prouvable** par analyse AST (les clés
  `ip_registry.*` ont des consommateurs **non branchés**, pas absents).
  En revanche le **gate est permissif sur 62 % des clés**
  (`config_audit.py:266`) et **2 clés sont réellement mortes**
  (`thinking.effort_caps.ling-3.0-flash-fin`, `muse_spark`). Couvert par
  T224 et INV-11.

## État d'exécution (2026-11-20)

- [x] CHK053 **T001 livré** : `_resp_json_hors_boucle` déplacée avant le bloc
  `__main__` (AST : index **818** < **892**). Test `test_a11` 2/2.
- [x] CHK054 **T002 livré** : alias `sys.modules` posé ; prouvé par exécution
  (alias **identique** à `__main__`, import ultérieur = **même objet**,
  **0** module chargé en plus).
- [x] CHK055 **T003 livré** : branche `elif _geo_tunnel` dupliquée supprimée ;
  prouvé par AST (**7 blocs**, **0 lecture non assignée**, **7/7** appellent
  `_open_via_pool`).
- [x] CHK056 **T005 livré** : overlays versionnés dans `deploy/compose/` ;
  `docker-compose.yml` déclare `gluetun-016:local` + `pull_policy: never` ;
  **11/11** services VPN résolvent vers le fork.
- [x] CHK057 **Découverte en exécutant T005** : `scripts/fresh_chain.py` garde
  l'image upstream **pour un usage légitime** (`docker run --rm --entrypoint
  sh`) — test `test_manifest_pinning.py` mis à jour en préservant son
  intention (V-66).
- [x] CHK058 **Validation** : suite complète exit 0, `ruff` All checks passed,
  `config_audit --strict` 0/0, `platform_audit --strict` 0 violation.

## Points ouverts (à trancher par l'opérateur)

- [ ] CHK042 Faut-il **restaurer** les 17 fichiers de code du `git stash`
  (modules `observability/pins.py`, `station_state.py`, tests
  `test_state_authority.py`…) ? Le plan les traite comme **entrée de travail
  à examiner**, pas comme code à adopter.
- [ ] CHK043 Faut-il **déployer** la matrice CI Windows (coût runner) ou
  garder la gate Windows locale (`gate.ps1`) comme seule couverture ?
- [ ] CHK044 Faut-il **borner WARP** à 2-3 conteneurs (recommandation de
  l'audit 019, jamais appliquée) alors que le fork y dépend ?
- [ ] CHK045 Faut-il **purger** `logs/` (15 Go) une fois les overlays
  versionnés (tâche 0.4) ?
- [ ] CHK046 Le **seuil de couverture CI** reste-t-il à 60 % (contre 87 %
  mesuré sur `free/qualification`) ?

## Cohérence documentaire

- [x] CHK047 Le plan met à jour la spec, la recherche, le modèle de données
  et les contrats **dans le même lot** (principe XVII).
- [x] CHK048 `docs/DEBT.md` est identifié comme **périmé** (état au
  2026-11-10, ne liste pas les P0 découverts).
- [x] CHK049 Les écarts doc ↔ réalité sont relevés
  (`docs/runbook-docker.md:21-23` décrit des boutons disparus).
- [x] CHK050 Le CHANGELOG devra être mis à jour à chaque lot (principe XVII).
