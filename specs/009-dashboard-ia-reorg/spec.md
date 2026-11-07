# Feature Specification: Réorganisation des onglets Modèles Gratuits et Configuration

**Feature Branch**: `009-dashboard-ia-reorg`

**Created**: 2026-11-07

**Status**: Draft

**Input**: Demande opérateur : les onglets « Modèles Gratuits » et
« Configuration » sont devenus un grand bazar. Prévoir une réorganisation
complète, avec des sous-onglets si nécessaire, pour que chaque réglage ait une
place claire et qu'on n'ait plus à faire défiler une page interminable.

## User Scenarios & Testing *(mandatory)*

### User Story 1 - Naviguer dans Modèles Gratuits par sous-onglets (Priority: P1)

L'opérateur ouvre « Modèles Gratuits » et voit une barre de sous-onglets :
**Vue d'ensemble**, **Egress & providers**, **Routage free**, **Qualité &
probes**, **Avancé**. Chaque section est accessible en un clic, sans scroll
interminable.

**Why this priority**: c'est la page la plus utilisée et la plus chargée
(inventaire + statut + routage + serveurs + avancé).

**Independent Test**: cliquer chaque sous-onglet, vérifier que la section
correspondante s'affiche, que l'URL change et qu'aucune section n'est perdue.

**Acceptance Scenarios**:

1. **Given** la page Modèles Gratuits, **When** elle s'ouvre, **Then** le
   sous-onglet « Vue d'ensemble » est actif par défaut.
2. **Given** un sous-onglet, **When** l'opérateur clique, **Then** seule la
   section correspondante est visible et l'URL contient le sous-onglet.
3. **Given** toutes les sections actuelles, **When** la réorganisation est
   terminée, **Then** chacune est accessible en au plus deux clics.
4. **Given** un rechargement de page, **When** l'URL contient un sous-onglet,
   **Then** ce sous-onglet est restauré.

---

### User Story 2 - Naviguer dans Configuration par domaines (Priority: P1)

L'opérateur ouvre « Configuration » et voit des sous-onglets par domaine :
**Serveur**, **Modèles & routage**, **Clés & accès**, **Outils web**,
**Maintenance**.

**Why this priority**: la page Configuration est un fichier de 44 Ko avec
serveur, mapping, routes, web search/fetch, clés API et modèles.

**Independent Test**: cliquer chaque sous-onglet, vérifier que les réglages
correspondants sont présents, qu'aucun réglage n'est dupliqué et que les
sauvegardes fonctionnent.

**Acceptance Scenarios**:

1. **Given** la page Configuration, **When** elle s'ouvre, **Then** le
   sous-onglet « Serveur » est actif par défaut.
2. **Given** un réglage existant, **When** la réorganisation est terminée,
   **Then** il apparaît dans **un seul** sous-onglet.
3. **Given** un réglage sauvegardé, **When** l'opérateur change de sous-onglet,
   **Then** la valeur est conservée.
4. **Given** un sous-onglet, **When** l'opérateur recharge, **Then** le
   sous-onglet est restauré depuis l'URL.

---

### User Story 3 - Anciens liens et deep links (Priority: P2)

Les anciens liens (`#config`, `#free-models`, `#vpn`) continuent de
fonctionner ou redirigent vers le bon sous-onglet. Les nouveaux liens profonds
sont partageables.

**Why this priority**: éviter de casser les habitudes et les liens existants.

**Independent Test**: ouvrir chaque ancien hash, vérifier la redirection vers
le bon sous-onglet ; copier un lien de sous-onglet et l'ouvrir dans un nouvel
onglet.

**Acceptance Scenarios**:

1. **Given** `#config`, **When** la page s'ouvre, **Then** « Serveur » est
   actif.
2. **Given** `#free-models`, **When** la page s'ouvre, **Then** « Vue
   d'ensemble » est actif.
3. **Given** un lien `#config/models`, **When** il est ouvert, **Then** le
   sous-onglet « Modèles & routage » est actif.
4. **Given** un hash inconnu, **When** la page s'ouvre, **Then** le sous-onglet
   par défaut est affiché sans erreur.

---

### User Story 4 - Trouver un réglage et protéger les changements (Priority: P2)

L'opérateur peut rechercher un réglage par nom et sauter directement au bon
sous-onglet. S'il quitte un formulaire modifié, une confirmation l'avertit.

**Why this priority**: la réorganisation doit réduire le temps de recherche,
pas seulement déplacer les blocs.

**Independent Test**: rechercher « web search », vérifier le saut vers
« Outils web » ; modifier un champ et changer de sous-onglet, vérifier
l'avertissement.

**Acceptance Scenarios**:

1. **Given** une recherche « web search », **When** l'opérateur valide,
   **Then** le sous-onglet « Outils web » s'ouvre et le champ est mis en
   évidence.
2. **Given** un formulaire modifié, **When** l'opérateur change de sous-onglet,
   **Then** une confirmation propose de sauvegarder, annuler ou quitter.
3. **Given** un formulaire non modifié, **When** l'opérateur change de
   sous-onglet, **Then** aucun avertissement n'apparaît.

---

### User Story 5 - Utilisable sur mobile et accessible (Priority: P3)

Les sous-onglets restent lisibles sur petit écran, accessibles au clavier et
traduits en français et en anglais.

**Why this priority**: la réorganisation ne doit pas créer une nouvelle dette
d'accessibilité ou de responsive.

**Independent Test**: réduire la fenêtre à 375 px, naviguer au clavier,
basculer FR/EN.

**Acceptance Scenarios**:

1. **Given** un écran de 375 px, **When** la page s'ouvre, **Then** les
   sous-onglets sont scrollables horizontalement ou en menu déroulant.
2. **Given** le focus clavier, **When** Tab/Entrée, **Then** les sous-onglets
   sont atteignables et activables.
3. **Given** la langue FR ou EN, **When** la page s'affiche, **Then** tous les
   libellés des sous-onglets sont traduits.

---

### Edge Cases

- Un ancien hash pointe vers une section supprimée ou renommée.
- Deux sous-onglets contiennent le même réglage (doublon).
- Un formulaire est modifié puis l'onglet est fermé.
- Un sous-onglet contient un composant lourd (VpnAdvanced) et ralentit le
  premier rendu.
- L'URL contient un sous-onglet invalide.
- Le localStorage est vidé.
- Le mode sombre/clair change pendant la navigation.
- Un utilisateur arrive avec un lien profond depuis une ancienne version.

## Requirements *(mandatory)*

### Functional Requirements

- **FR-001** « Modèles Gratuits » DOIT avoir des sous-onglets : Vue
  d'ensemble, Egress & providers, Routage free, Qualité & probes, Avancé.
- **FR-002** « Configuration » DOIT avoir des sous-onglets : Serveur, Modèles &
  routage, Clés & accès, Outils web, Maintenance.
- **FR-003** Chaque section/réglage existant DOIT apparaître dans **un seul**
  sous-onglet ; aucun doublon.
- **FR-004** Le sous-onglet actif DOIT être reflété dans l'URL et restauré au
  reload.
- **FR-005** Les anciens hashes (`#config`, `#free-models`, `#vpn`) DOIVENT
  continuer à fonctionner ou rediriger vers le sous-onglet par défaut.
- **FR-006** Un hash inconnu NE DOIT PAS produire d'erreur ; le défaut
  s'applique.
- **FR-007** La navigation entre sous-onglets DOIT être accessible au clavier
  (rôles ARIA, focus visible).
- **FR-008** Les sous-onglets DOIVENT être traduits FR/EN.
- **FR-009** Les sous-onglets DOIVENT rester utilisables à 375 px de large.
- **FR-010** Un formulaire modifié DOIT déclencher une confirmation avant de
  quitter le sous-onglet.
- **FR-011** Une recherche de réglage DOIT ouvrir le bon sous-onglet et
  mettre en évidence le champ.
- **FR-012** Les composants lourds DOIVENT être chargés à la demande
  (lazy) pour ne pas ralentir le premier rendu.
- **FR-013** La réorganisation NE DOIT PAS changer les API, les clés de config
  ni le comportement runtime.
- **FR-014** Les tests existants du dashboard DOIVENT rester verts.
- **FR-015** Chaque option GUI DOIT conserver son contrat
  `option → clé → consommateur → effet → test`.
- **FR-016** Un plan de migration des anciens liens DOIT être documenté.
- **FR-017** Les sous-onglets DOIVENT avoir des `data-testid` stables pour les
  tests.
- **FR-018** La page NE DOIT PAS afficher deux fois le même titre de section.
- **FR-019** Le temps de premier rendu NE DOIT PAS augmenter de plus de 20 %
  par rapport à l'existant.
- **FR-020** Un mode « affichage seul » DOIT être explicite si un réglage n'a
  pas de consommateur runtime.

### Key Entities

- **NavigationGroup** : onglet principal (Modèles Gratuits, Configuration).
- **NavigationTab** : sous-onglet (id, label FR/EN, ordre, composant,
  route, lazy).
- **RouteAlias** : ancien hash → nouveau sous-onglet.
- **SettingOwnership** : réglage → sous-onglet unique → composant → test.
- **DirtyForm** : formulaire modifié, sous-onglet, actions.

## Success Criteria *(mandatory)*

### Measurable Outcomes

- **SC-001** Toutes les sections existantes sont accessibles en **≤ 2 clics**
  depuis leur onglet principal.
- **SC-002** **0** réglage dupliqué entre deux sous-onglets.
- **SC-003** **100 %** des anciens hashes testés ouvrent le bon sous-onglet.
- **SC-004** Le sous-onglet actif est restauré après reload dans **100 %** des
  cas.
- **SC-005** Navigation clavier complète : **0** piège de focus.
- **SC-006** Rendu utilisable à **375 px** sans scroll horizontal de page.
- **SC-007** FR/EN : **100 %** des libellés de sous-onglets traduits.
- **SC-008** Premier rendu ≤ **+20 %** par rapport à l'existant.
- **SC-009** Tests dashboard : **0** régression.
- **SC-010** Un formulaire modifié déclenche une confirmation dans **100 %**
  des cas de sortie.

## Assumptions

- Réorganisation **UI uniquement** : aucune API, aucun schéma de config, aucun
  comportement runtime ne change.
- Les composants existants (`VpnStatus`, `VpnRouting`, `VpnServers`,
  `VpnAdvanced`, `AllocatorStatus`, `Config`) sont réutilisés et déplacés, pas
  réécrits.
- Les sous-onglets utilisent le routeur/hash existant (`dashboard-ui/src/lib/router.svelte`).
- Les tests UI existants seront adaptés aux nouveaux `data-testid`.
- Aucun nouveau framework ni dépendance.
