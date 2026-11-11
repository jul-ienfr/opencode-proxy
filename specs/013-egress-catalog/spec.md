# Feature Specification: Catalogue d'egress et rotation disponible

**Feature Branch**: `013-egress-catalog`

**Created**: 2026-11-09

**Status**: Draft

**Input**: « Le but n'est pas forcément d'avoir plus de stations de chacun mais
plus de rotation possible disponible pour chacun. » — public, WARP, NordVPN.

## Contexte

Aujourd'hui, chaque famille d'egress a sa propre mécanique :

- **public** : listes récupérées par `scripts/fetch_proxy_lists.py`, testées
  par `probe_proxy_pool.py` + `test_proxy_pool.py`, puis importées dans
  `public_proxies` ;
- **WARP** : conteneurs provisionnés par `free/warp_provisioner.py`, egress
  observé par `resolve_egress`, rotation par `free/warp_rotation.py` ;
- **NordVPN** : stations Gluetun, rotation de serveur/IP par
  `vpn/manager.py` et `fresh_chain`, quotas dans `logs/registry.db`.

Le problème n'est pas le nombre de stations actives. Le problème est le
**nombre d'options de rotation réellement disponibles** : une IP publique
vivante, un `/64` WARP frais, un serveur/IP NordVPN non épuisé. Il faut un
catalogue commun, alimenté régulièrement, qui sait ce qui est **utilisable**
et ce qui ne l'est pas, sans confondre « joignable » et « quota free encore
disponible ».

## Objectifs

1. Un catalogue persistant, provider-agnostique, de **toutes les options de
   rotation connues** (pas seulement les endpoints actifs).
2. Une découverte régulière et extensible : ajouter une source publique sans
   toucher au moteur.
3. Une validation réelle avant promotion : liveness, egress, puis gate free
   `200`/`429`. Un 429 est un bucket épuisé, pas un endpoint mort.
4. Une rotation qui pioche dans le catalogue selon le provider, le quota,
   la fraîcheur et la santé — sans multiplier les stations actives.
5. Une observabilité honnête : combien d'options par provider, lesquelles
   sont utilisables, quand elles ont été vues pour la dernière fois.

## Qualité, fiabilité, performance, maintenabilité

Ces exigences sont aussi contraignantes que les exigences fonctionnelles.

### Fiabilité

- Aucun cycle de catalogue ne doit vider le pool actif : un échec conserve
  l'état précédent.
- Les cycles sont **idempotents** : un redémarrage au milieu d'un run ne crée
  ni doublon ni option sans observation.
- Une source morte, un DNS cassé ou un worker arrêté ne doivent jamais
  interrompre le trafic free ; le fallback provider reste disponible.
- Toute décision automatique est journalisée avec sa cause et sa preuve
  (constitution VIII).
- Le système doit survivre à un redémarrage de l'OS (Windows, Linux, macOS) :
  catalogue, TTL, `reset_at` et slots sont persistés dans `registry.db`.
- Un cycle échoué, une source morte ou un worker arrêté ne doit jamais
  produire une fenêtre, un prompt ou un blocage interactif.

### Multiplateforme

- Le catalogue, le scheduler, les adapters et le CLI tournent sur **Windows,
  Linux et macOS** ; aucun chemin de code n'est réservé à un OS.
- Aucune API spécifique (`winreg`, `msvcrt`, `fcntl`, `signal.SIGBREAK`)
  n'entre dans le domaine ; les différences passent par un adapter de
  plateforme.
- Les chemins sont `pathlib`, les exécutables sont résolus par
  `sys.executable`/`shutil.which` ; aucun `C:\`, `\` ou `.exe` en dur.
- Les commandes système sont des listes d'arguments ; `shell=True`,
  `os.system` et `os.popen` sont interdits dans le service.
- La gate non-Docker tourne sur Windows et Linux ; un test spécifique à un OS
  est marqué et possède une contrepartie ou une justification.

### Exécution silencieuse

- Aucun processus lancé par le scheduler, le worker, le probe ou une rotation
  n'ouvre de fenêtre ni ne vole le focus.
- Sur Windows, tout lancement non interactif passe par
  `core.process.run_hidden`/`popen_hidden` (`CREATE_NO_WINDOW`) ; sur POSIX,
  les enfants détachés utilisent `start_new_session=True` et
  `stdin=DEVNULL`.
- Aucun `cmd.exe`, `start`, `powershell.exe` visible ou TTY interactif.
- `scripts/platform_audit.py` échoue si un module runtime viole la règle ;
  une exception est justifiée sur la ligne par
  `# platform-audit: allow <raison>`.

### Performance

- Aucun cycle long dans le chemin de requête : fetch, probe, gate et écritures
  SQLite lourdes s'exécutent hors boucle de requête.
- API catalogue : p95 < 500 ms pour 1 000 options ; UI p95 < 2 s.
- Cycle public de 1 000 candidats : < 5 min en mode échantillon ; le scan
  complet 20 000 est un job de fond borné.
- Mémoire et disque bornés : dédup par egress, batchs, rétention, pas de
  chargement intégral des 20 000 lignes dans le dashboard.

### Maintenabilité

- Modules < 1 500 lignes ; domaine pur sans I/O ; adapters isolés.
- Chaque provider a un contrat de testé indépendamment, sans Docker pour les
  tests unitaires.
- Toute option de configuration ou de GUI a un contrat
  `surface → clé → consommateur → effet observable → test` (constitution XII).
- Une option sans consommateur est supprimée ou explicitement marquée
  « affichage seul » ; elle ne reste pas silencieusement inerte.
- `config_audit.py` doit rester à 0 dead / 0 missing ; un audit GUI équivalent
  doit être ajouté.

### Modularité

- Ajouter une source publique = ajouter une ligne de configuration, pas un
  module.
- Ajouter un provider = implémenter l'interface `EgressAdapter` sans modifier
  le scheduler ni le store.
- Le pool ne dépend pas des détails du catalogue : il reçoit des options
  prêtes à servir via `egress_rotation`.
- Les exports JSON sont remplaçables ; `registry.db` reste l'autorité.

### Observabilité et qualité des données

- Chaque option expose `last_checked_at`, `expires_at`, `last_status` et au
  moins une observation brute.
- Les métriques distinguent `reachable`, `usable`, `quota`, `auth`, `dead`.
- Un dashboard sans preuve est un bug : aucune option ne s'affiche sans âge de
  dernière vérification.
- Les erreurs de source sont bornées, classées et consultables par run.

## Non-objectifs

- Acheter des IP, ouvrir un second compte ou attendre comme solution.
- Maintenir toutes les options actives en permanence.
- Garantir un 200 si **tous** les buckets sont épuisés.
- Remplacer les six stations WARP/NordVPN comme socle de service.
- Router du trafic payant par un egress public.

## User Scenarios & Testing *(mandatory)*

### User Story 1 — Un catalogue unique, pas trois listes (Priority: P1)

L'opérateur ouvre le dashboard et voit, par provider, le nombre d'options
connues, utilisables, en cooldown et mortes. Chaque option a une preuve
(dernier test, egress, bloc, expiration).

**Why this priority**: sans source de vérité commune, les trois mécaniques
divergent et les mêmes IP sont re-testées ou oubliées.

**Independent Test**: injecter 3 options publiques, 2 WARP et 2 NordVPN dans
un catalogue vide ; l'API retourne 7 options avec leur état et leur provider.

**Acceptance Scenarios**:

1. **Given** un catalogue vide, **When** un cycle de découverte insère des
   candidats, **Then** chaque candidat a `provider`, `egress`, `status`,
   `last_seen_at` et `expires_at`.
2. **Given** deux sources qui publient le même `host:port`, **When** le
   catalogue ingère, **Then** une seule option est créée et les sources sont
   fusionnées.

---

### User Story 2 — Le public tourne régulièrement et trouve de nouvelles IP (Priority: P1)

Un scheduler récupère le catalogue de sources publiques, normalise, déduplique
par egress, teste la liveness puis le gate free. Seules les options `200`
deviennent éligibles à la rotation ; les `429` restent en quarantaine courte
avec leur reset ; les mortes expirent.

**Why this priority**: les listes publiques changent en quelques heures ; un
import one-shot est périmé le lendemain.

**Independent Test**: lancer un cycle avec une source factice contenant 3
proxys (1 vivant 200, 1 vivant 429, 1 mort) ; le catalogue contient 1
`usable`, 1 `quota`, 1 `dead`, et le pool actif ne reçoit que le 200.

**Acceptance Scenarios**:

1. **Given** une source publique nouvelle, **When** le scheduler s'exécute,
   **Then** elle est ajoutée au catalogue sans redémarrage du proxy.
2. **Given** une option publique déjà connue et encore `200`, **When** le
   cycle se répète, **Then** son TTL est rafraîchi sans doublon.
3. **Given** une option `429`, **When** le reset est atteint, **Then** elle
   redevient candidate et est re-testée, sans être considérée comme morte.
4. **Given** toutes les options publiques épuisées, **When** une requête
   arrive, **Then** le pool ne sélectionne aucune option morte et le fallback
   provider reste possible.

---

### User Story 3 — WARP : plus de /64 de rotation, pas plus de stations (Priority: P1)

Le catalogue connaît les conteneurs WARP et les `/64` observés. Une rotation
WARP peut viser un conteneur existant ou un nouveau conteneur, mais le
catalogue garde l'historique des blocs vus, leur fraîcheur et leur état 200/429.
Un `/64` épuisé n'est pas proposé ; un `/64` frais est prioritaire.

**Why this priority**: chaque `/64` est un bucket ; le but est d'avoir
suffisamment d'options de rotation, pas de faire tourner 12 stations en
permanence.

**Independent Test**: injecter 2 conteneurs WARP avec 2 blocs `/64` chacun,
dont un `429` ; la sélection ne propose que les blocs non épuisés.

**Acceptance Scenarios**:

1. **Given** un conteneur WARP sain, **When** son egress est mesuré, **Then**
   le `/64` est enregistré comme option avec un TTL et un état.
2. **Given** un `/64` marqué `429`, **When** une rotation WARP est demandée,
   **Then** il n'est pas choisi tant que son reset n'est pas atteint.
3. **Given** aucun `/64` frais, **When** la rotation est demandée, **Then** le
   système le signale sans brûler une IP au hasard.

---

### User Story 4 — NordVPN : mettre à jour la liste des IP utilisables (Priority: P1)

Le catalogue rafraîchit régulièrement les serveurs/IP NordVPN connus, vérifie
la session/AUTH et l'egress, puis marque chaque IP `usable`, `quota`, `auth`
ou `dead`. La rotation choisit une IP fraîche dans ce catalogue ; elle ne
dépend plus de la seule station courante.

**Why this priority**: une station connectée peut être sur une IP déjà épuisée ;
changer de serveur sans changer d'IP ne sert à rien.

**Independent Test**: injecter 3 IP NordVPN (200, 429, AUTH_FAILED) ; la
rotation ne propose que la 200 et n'essaie pas l'AUTH_FAILED.

**Acceptance Scenarios**:

1. **Given** une IP NordVPN en `429 FreeUsageLimitError`, **When** le catalogue
   est consulté, **Then** elle est `quota` avec `reset_at`.
2. **Given** une station en `AUTH_FAILED`, **When** le catalogue est consulté,
   **Then** ses IP sont `auth` et aucune rotation ne les propose.
3. **Given** une nouvelle IP sur une station existante, **When** elle répond
   200, **Then** elle est ajoutée comme option de rotation réutilisable.

---

### User Story 5 — Ajouter une source sans redéployer (Priority: P2)

L'opérateur ajoute une URL de liste publique, une source WARP ou un catalogue
NordVPN dans la configuration. Le prochain cycle l'ingère ; aucun code n'est
modifié.

**Independent Test**: ajouter une source JSON/texte factice à la config ;
relancer le cycle ; l'API montre ses candidats avec `source_id`.

---

### User Story 6 — Voir la rotation disponible et son état (Priority: P2)

Le dashboard expose le catalogue par provider : total, usable, quota, auth,
dead, blocs uniques, âge du dernier test, prochaine expiration, et un bouton
« rafraîchir maintenant ».

---

### User Story 7 — Aucune option GUI/config décorative (Priority: P1)

L'opérateur modifie une option dans l'UI ou `config.yaml` et observe un effet
réel : le scheduler change de cadence, une source est activée, un cap est
appliqué, une option est rejetée. Chaque contrôle a un contrat et un test.

**Why this priority**: une option inerte est un mensonge opérationnel ; elle
fait croire que le système est configurable alors qu'il ne l'est pas.

**Independent Test**: pour chaque ligne de `contracts/option-map.md`, modifier
la valeur, recharger, vérifier l'effet observable et le test associé.

**Acceptance Scenarios**:

1. **Given** une option `egress_catalog.providers.public.max_active`,
   **When** l'opérateur la passe de 64 à 8, **Then** le pool actif tombe à 8
   au cycle suivant et l'API affiche la valeur effective.
2. **Given** un bouton « Rafraîchir » du dashboard, **When** il est cliqué,
   **Then** un `CatalogRun` est créé et son état est visible.
3. **Given** une clé de config sans consommateur, **When** le gate tourne,
   **Then** le build échoue avec le nom de la clé.

---

### Edge Cases

- Une source renvoie 20 000 lignes dont 90 % de doublons : la déduplication
  par egress doit borner la mémoire et le disque.
- Un proxy public répond à `ipify` mais pas au gate free : il reste
  `candidate`, jamais `usable`.
- Un 429 transitoire (surcharge) ne doit pas être confondu avec un quota
  journalier.
- Un `/64` WARP change après recréation du conteneur : l'ancien bloc doit être
  expiré, pas réutilisé.
- Une station NordVPN change d'IP sans changer de nom : l'ancienne IP garde
  son état, la nouvelle est ajoutée.
- Le scheduler tombe pendant un cycle : le catalogue précédent reste lisible
  et aucune option n'est promue sans validation.
- Toutes les sources sont indisponibles : le catalogue garde les dernières
  options valides jusqu'à leur TTL.

## Requirements *(mandatory)*

### Functional Requirements

- **FR-001**: Le système MUST maintenir un catalogue persistant d'options
  d'egress, indépendant du provider.
- **FR-002**: Chaque option MUST porter `provider`, `kind`, `egress_ip` ou
  `block`, `host`/`port` ou identifiant de station, `status`, `last_seen_at`,
  `last_checked_at`, `expires_at`, `source_ids` et un compteur de preuves.
- **FR-003**: Le système MUST accepter des sources déclaratives (URL texte,
  JSON, catalogue WARP, inventaire NordVPN) et permettre d'en ajouter sans
  modification du moteur.
- **FR-004**: Le scheduler MUST lancer la découverte publique à intervalle
  configurable (défaut 6 h) et la revalidation à intervalle plus court
  (défaut 30 min pour les options actives).
- **FR-005**: Le pipeline public MUST : télécharger → normaliser → dédupliquer
  par egress → tester la liveness → tester le gate free → promouvoir ou
  quarantiner.
- **FR-006**: Un `200` free MUST être la seule preuve qui rend une option
  publique `usable`.
- **FR-007**: Un `429 FreeUsageLimitError` MUST marquer `quota` avec
  `reset_at`, pas `dead`.
- **FR-008**: Un timeout, refus, TLS cassé ou 403 MUST marquer `dead` avec un
  TTL court et ne jamais être sélectionné.
- **FR-009**: WARP MUST enregistrer chaque `/64` observé, son conteneur, son
  état et son TTL ; un conteneur recréé MUST expirer l'ancien bloc s'il a
  changé.
- **FR-010**: NordVPN MUST enregistrer chaque IP observée, son serveur, son
  état AUTH/quota/200 et son `reset_at` ; une IP AUTH_FAILED MUST être exclue.
- **FR-011**: La sélection de rotation MUST consulter le catalogue et choisir
  une option `usable`, fraîche, avec un bloc distinct, dans la limite du
  provider.
- **FR-012**: Le système MUST ne jamais promouvoir une option non validée dans
  le pool actif.
- **FR-013**: Le système MUST conserver un historique borné des observations
  (au moins les 24 dernières heures) pour expliquer une décision.
- **FR-014**: Les sondes publiques MUST passer par le worker isolé et ne
  jamais envoyer de clé, cookie ou header de compte.
- **FR-015**: Le scheduler MUST être fail-soft : un cycle en échec conserve le
  catalogue et le pool précédents.
- **FR-016**: L'API MUST exposer le catalogue par provider et un état
  agrégé (connus, usable, quota, auth, dead, blocs uniques, âge).
- **FR-017**: L'UI MUST afficher le catalogue, les sources, le dernier cycle
  et un bouton de rafraîchissement manuel.
- **FR-018**: Le système MUST respecter les limites de session NordVPN et ne
  pas ouvrir plus de stations que la capacité saine mesurée.
- **FR-019**: Le système MUST continuer à fonctionner si une source unique
  disparaît ; les autres sources et le cache restent valides.
- **FR-020**: Le catalogue MUST être reconstructible depuis les observations
  et ne doit pas être la seule copie d'un état critique.
- **FR-021**: Aucun trafic payant, aucune clé payante et aucun second compte
  ne MUST être utilisé par le catalogue.
- **FR-022**: Les métriques MUST distinguer `reachable` (liveness) de
  `usable` (free 200).

- **FR-023**: Toute option `egress_catalog.*` MUST avoir un consommateur
  runtime identifié et un test d'effet.
- **FR-024**: Toute option GUI du catalogue MUST afficher la valeur effective
  et déclencher un rechargement observable, jamais seulement l'écriture du
  YAML.
- **FR-025**: Le dépôt MUST maintenir `contracts/option-map.md` : une ligne par
  surface (config/GUI/API), clé, consommateur, effet, test, statut.
- **FR-026**: Un gate MUST échouer si une clé `egress_catalog.*` ou un contrôle
  GUI du catalogue n'a pas de ligne dans l'option-map.
- **FR-027**: Le scheduler MUST être idempotent et reprendre après crash sans
  doublon ; un run interrompu est marqué `failed` mais ne corrompt pas le
  catalogue.
- **FR-028**: Le catalogue MUST être borné en mémoire/disque (batch, rétention,
  index) et ne jamais charger les 20 000 candidats dans l'UI.
- **FR-029**: Chaque adapter provider MUST être testable sans Docker, avec un
  runner/store factice.
- **FR-030**: Toute nouvelle source/provider MUST être ajoutable sans modifier
  le scheduler, le store ou le pool.
- **FR-031**: Le catalogue, le scheduler, les adapters et le CLI MUST
  fonctionner sur Windows, Linux et macOS ; aucune fonctionnalité ne MUST
  dépendre d'un seul OS.
- **FR-032**: Aucun processus lancé par le service MUST ouvrir de fenêtre ou
  de console visible ; les lancements non interactifs MUST passer par
  `core.process.run_hidden`/`popen_hidden` (constitution XVI).
- **FR-033**: `scripts/platform_audit.py --strict` MUST échouer si un module
  runtime utilise `os.system`, `os.popen`, `shell=True` ou un lancement sans
  politique silencieuse.
- **FR-034**: Les budgets de performance MUST être testés : API catalogue p95
  < 500 ms pour 1 000 options, UI p95 < 2 s, cycle de 1 000 candidats < 5 min
  en mode échantillon.
- **FR-035**: Le système MUST filtrer les candidats contre une denylist
  statique et des flux de threat-intel (IP et CIDR) **avant** toute connexion
  réseau.
- **FR-036**: Un flux de threat-intel indisponible MUST conserver les
  indicateurs précédents (fail-soft) et exposer son erreur ; il ne MUST jamais
  vider la denylist.

### Key Entities

- **SourceDefinition** : id, type (`public_text`, `public_json`, `warp`,
  `nordvpn`), URL/chemin, format, actif, intervalle, parser.
- **EgressOption** : clé canonique, provider, kind, host/port ou station,
  egress IP/bloc, statut, source_ids, first_seen, last_seen, last_checked,
  expires_at, reset_at, success/fail counters.
- **Observation** : horodatage, option, phase (`liveness`, `free_gate`,
  `auth`, `egress`), résultat, statut, latence, message borné.
- **RotationSlot** : provider, option active, raison, expiration, quota.
- **CatalogRun** : début/fin, sources, candidats, promus, quarantinés,
  erreurs.
- **ProviderPolicy** : intervalle, max actif, max options, TTL par statut,
  concurrence, budget de rotation.
- **OptionContract** : surface, clé, valeur par défaut, consommateur
  (`file:function`), effet observable, test, statut
  (`active`/`display-only`/`retired`).

## Success Criteria *(mandatory)*

### Measurable Outcomes

- **SC-001**: 100 % des options actives publiques ont un `200` free mesuré
  dans les 12 dernières heures.
- **SC-002**: Le catalogue expose au moins **64 options publiques**, **12
  blocs WARP** et **6 IP NordVPN** connues, avec un état explicite, après un
  cycle complet sur la machine de référence.
- **SC-003**: Un cycle de découverte publique planifié s'exécute au moins
  toutes les 6 h ; un échec de cycle ne vide jamais le pool actif.
- **SC-004**: Le taux de sélection d'une option `dead` ou `auth` est **0 %**.
- **SC-005**: Un `429` n'est jamais compté comme `dead` ; son `reset_at` est
  respecté.
- **SC-006**: Le dashboard répond en moins de 2 s pour 1 000 options et
  affiche l'âge du dernier test.
- **SC-007**: Aucun secret, cookie ou clé payante n'apparaît dans une sonde
  ou un log du catalogue.
- **SC-008**: Quand un bucket sain existe, le taux de 429 client évitables
  reste inférieur à 1 % sur une fenêtre de 1 000 requêtes.
- **SC-009**: 100 % des options `egress_catalog.*` et des contrôles GUI du
  catalogue ont une ligne `option-map` avec un test d'effet.
- **SC-010**: 0 option inerte : `config_audit` = 0 dead/0 missing et le gate
  GUI = 0 contrôle sans consommateur.
- **SC-011**: Un run interrompu puis relancé ne crée aucun doublon ; le
  catalogue reste lisible et le pool actif inchangé.
- **SC-012**: L'API catalogue répond en p95 < 500 ms pour 1 000 options et
  l'UI en p95 < 2 s.
- **SC-013**: 100 % des observateurs de statut peuvent citer la dernière
  observation brute d'une option.
- **SC-014**: Ajouter une source publique ne nécessite aucun changement de
  code ; ajouter un provider ne nécessite que son adapter.
- **SC-015**: `platform_audit` retourne 0 violation sur le dépôt et échoue sur
  un lancement non silencieux, `shell=True`, `os.system` ou un chemin Windows
  en dur non justifié.
- **SC-016**: 0 fenêtre visible ou vol de focus pendant un boot, un cycle, une
  rotation, une migration et une suite de tests complète sous Windows.
- **SC-017**: La gate non-Docker passe sur Windows et Linux ; un test
  spécifique à un OS est marqué et justifié.
- **SC-018**: 100 % des IP présentes dans la denylist ou un flux de
  threat-intel sont filtrées avant le worker ; aucune connexion n'est tentée
  vers elles.
- **SC-019**: Un flux de threat-intel en panne conserve ses indicateurs
  précédents et l'erreur est visible dans l'API/UI.

## Assumptions

- Les sources publiques sont des tiers non fiables ; le worker isolé reste
  obligatoire.
- NordVPN reste limité par les sessions du compte ; le catalogue peut
  connaître plus d'IP que de stations simultanées.
- WARP peut fournir plusieurs `/64` par conteneur au fil des rotations ; le
  catalogue doit les historiser.
- Le scheduler vit dans le processus proxy ; un cycle long est exécuté en
  tâche de fond, jamais dans le chemin de requête.
