# Feature Specification: Audit des dépôts tiers et parité de la jambe free

**Feature Branch**: `004-repo-parity-audit`  
**Created**: 2026-11-06  
**Status**: Draft (documentation)  
**Input**: Demande opérateur « audit complet, documentation de tous les dépôts, de tout ce qu'on a modifié, de l'état actuel et des divergences restantes entre les dépôts et le proxy ».  
**Complète**: `specs/003-official-client-parity/` (parité wire) sans le remplacer.

## Contexte

Neuf dépôts tiers ont été fournis entre le 2026-11-05 et le 2026-11-06 :

1. `xljiulang/opencode-proxy` — passerelle Rust, identité 2.2.5, gate et TLS.
2. `rianilham28/opencode2api` — passerelle Rust free-only, quatre dialectes.
3. `xd88dx/afree-proxy` — passerelle Go multi-upstreams, pool d'egress.
4. `alztrk/opencode-ip-rotator` — proxy FastAPI + rotateur Cloudflare WARP.
5. `beastbroak30/opencode-zen-proxy` — proxy FastAPI avec backend `opencode serve`.
6. `nahdd123uosackr/opencode_proxy` — relais Node/Deno + pont uTLS Go.
7. `xueweijian/opencode-edge-proxy` — edge proxy US, geo-gate, patch SSE.
8. `developingchet/zengate` — gateway Node sur vrai `opencode serve`.
9. `ebouther/opencode-unlimited-proxy` — pool WARP par bloc, gate probe.

Ils documentent, mesurent ou implémentent des parties du même problème : passer le
gate free d'OpenCode, survivre au quota par IP, et rester utilisables par des
clients hétérogènes. Certains faits sont contradictoires (nombre d'outils du
gate, durée de vie de la session, version TLS). Le proxy du projet est, lui,
encore partiellement hybride : corps et ordre 2.2.5, identité 1.19.5.

Le dépôt n'a **aucun commit** pour ces travaux : l'état de référence est le
`git diff` local. Une documentation datée est donc nécessaire pour que l'état
actuel et les écarts soient reproductibles.

## Objectifs

1. **Documenter chaque dépôt** avec ses faits de code (pas seulement son
   README), la liste des fichiers lus et la qualité de la preuve.
2. **Documenter chaque modification** faite pendant l'audit : fichier,
   fonction, avant/après, raison, test.
3. **Documenter l'état actuel** : proxy, IP, stations, WARP, config, tests,
   fichiers non commités.
4. **Documenter les divergences restantes** entre chaque dépôt et le proxy,
   avec un statut : aligné, intentionnel, ouvert, risqué.
5. **Rendre l'audit reproductible** : commandes de vérification, contrats,
   mesures, et points de rollback.
6. **Ne pas exposer de secret** : aucun token, credential ou valeur
   d'authentification dans les documents.

## Non-objectifs

- Corriger toutes les divergences ouvertes dans cette itération.
- Modifier la jambe payante.
- Choisir un nouveau fournisseur d'egress ou contourner le quota.
- Rejouer les tests de shadow-ban de session sur l'IP directe (risque de la
  perdre pour la journée).
- Committer ou pousser les changements.

## User Scenarios & Testing *(mandatory)*

### User Story 1 — Lire l'audit d'un dépôt (Priority: P1)

Un mainteneur ouvre `research.md`, choisit un dépôt et trouve : ce qu'il fait,
les fichiers de code lus, les faits mesurés, ce qui a été adopté, ce qui a été
rejeté et pourquoi, et l'écart avec le proxy.

**Independent Test**: pour chaque dépôt, la fiche contient au moins une
référence de fichier de code (pas seulement le README) et un verdict explicite.

### User Story 2 — Reconstituer une modification (Priority: P1)

Un mainteneur ouvre `plan.md` et retrouve, pour chaque changement de cette
session : le fichier, la fonction, le comportement avant/après, la raison, le
test associé et le contrat concerné.

**Independent Test**: chaque ligne du journal des changements pointe vers un
fichier existant et un test existant, ou vers une tâche ouverte de `tasks.md`.

### User Story 3 — Vérifier l'état courant (Priority: P1)

Un opérateur ouvre `measurements.md` et `quickstart.md` et peut vérifier sans
ambiguïté : proxy en écoute, IP d'egress, mode, stations, WARP, tests passants,
et quelles mesures sont datées.

**Independent Test**: les commandes de `quickstart.md` rendent les mêmes
valeurs que `measurements.md` (au PID et à l'IP près).

### User Story 4 — Voir ce qui reste divergent (Priority: P1)

Un mainteneur ouvre la matrice de divergences et voit, pour chaque dimension
(UA, TLS, gate, session, quota, egress, outils, endpoint), la valeur de chaque
dépôt, la valeur du proxy et le statut.

**Independent Test**: la matrice couvre les neuf dépôts et au moins douze
dimensions, et chaque cellule « ouvert » ou « risqué » a une tâche associée.

### User Story 5 — Ne pas casser la jambe free en lisant (Priority: P2)

Un opérateur suit `quickstart.md` et ne déclenche aucun test destructif : pas
de test de session collante sur l'IP directe, pas de rotation WARP, pas de
redémarrage des six stations.

**Independent Test**: `quickstart.md` contient une section « Ne pas faire » et
les commandes de test sont en lecture seule ou sur une IP sacrifiable.

## Functional Requirements

- **FR-001** `research.md` DOIT contenir une fiche par dépôt (neuf) avec URL,
  langage, architecture, fichiers de code lus, faits, verdict.
- **FR-002** Chaque fait DOIT être classé : mesuré par le dépôt, mesuré par
  nous, documenté seulement, ou inféré.
- **FR-003** `plan.md` DOIT contenir un journal des changements de la session
  avec avant/après/raison/test.
- **FR-004** `plan.md` DOIT distinguer les changements de cette session des
  travaux antérieurs non commités (specs 001–003).
- **FR-005** `measurements.md` DOIT contenir les tests live avec date, IP,
  modèle, statut, latence et note.
- **FR-006** `measurements.md` DOIT contenir l'état runtime (PID, port, mode,
  stations, WARP) et le résultat de la suite de tests.
- **FR-007** La matrice de divergences DOIT couvrir les neuf dépôts et les
  dimensions : identité, UA, TLS, en-têtes, corps/gate, session, trace,
  project, quota, 429, egress, endpoint, outils, streaming, erreurs.
- **FR-008** Chaque divergence ouverte DOIT avoir une tâche dans `tasks.md`.
- **FR-009** `contracts/` DOIT contenir les invariants exécutables : wire,
  `<env>`, cooldown, alias d'outils.
- **FR-010** `quickstart.md` DOIT permettre de reproduire les vérifications
  sans secret et sans action destructive.
- **FR-011** Aucun document NE DOIT contenir de token, clé, mot de passe ou
  identifiant de workspace réel.
- **FR-012** L'audit DOIT nommer la divergence principale restante : l'identité
  free est encore 1.19.5 alors que le corps et l'ordre sont 2.2.5.
- **FR-013** Le pool DOIT pouvoir distinguer deux egress partageant
  `host:port` par un `label`, et forcer `IPRESOLVE=6` sur une entrée
  `ipv6: true` sans polluer la session IPv4.
- **FR-014** Le ledger DOIT keyer l'IPv6 réel au `/64` et l'IPv4 exact.

## Acceptance Criteria

1. **AC-001** Les neuf fiches de `research.md` existent et citent au moins un
   fichier de code par dépôt.
2. **AC-002** Le journal des changements couvre les sept zones modifiées :
   placeholders, garde `<env>`, `tool_choice`, plafond station, plafond ledger,
   config, tests.
3. **AC-003** La matrice contient au moins douze dimensions et neuf colonnes
   dépôts.
4. **AC-004** Chaque divergence ouverte a un identifiant `T-...` dans
   `tasks.md`.
5. **AC-005** Les commandes de `quickstart.md` sont cohérentes avec
   `measurements.md` (proxy 4000, IP directe, WARP 11080).
6. **AC-006** Aucun secret n'apparaît dans `specs/004-repo-parity-audit/` ni
   dans `docs/AUDIT_REPOS_2026-11-06.md`.
7. **AC-007** Le document consolidé `docs/AUDIT_REPOS_2026-11-06.md` résume
   l'ensemble et pointe vers le paquet Spec Kit.
8. **AC-008** Six requêtes distinctes via le pool rendent **200** et
   `requests.db` montre les trois egress : IPv6 réel `2a09:…`, `warp-ipv4`,
   `direct`.

## Key Entities

- **Dépôt audité** : nom, URL, langage, architecture, fichiers lus, faits.
- **Fait** : énoncé, source (fichier/ligne), qualité de preuve, statut
  (adopté/rejeté/ouvert).
- **Changement** : fichier, fonction, avant, après, raison, test.
- **Divergence** : dimension, valeur dépôt, valeur proxy, statut, tâche.
- **Mesure** : date, IP, modèle, chemin, statut, latence, note.
- **Risque** : description, probabilité, impact, mitigation, tâche.

## Success Criteria

- Un lecteur qui ne connaît pas les dépôts peut, après lecture, expliquer
  pourquoi le proxy passe le gate et pourquoi il reçoit encore des 429.
- Un mainteneur peut retrouver chaque modification de la session sans
  `git diff`.
- Les divergences ouvertes sont classées par impact et ont un plan.
- L'état runtime est vérifiable en moins de cinq minutes.

## Assumptions

- Les captures `free/captures/2.2.5.json` et `free/official_payload.json` sont
  la référence du wire 2.2.5.
- L'IP directe `37.69.230.71` est fraîche au moment de l'audit.
- Les dépôts tiers peuvent évoluer ; les faits sont datés et ne sont pas des
  contrats permanents.
- Le proxy tourne en `proxy_mode: direct` pendant l'audit.
