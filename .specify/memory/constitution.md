# Constitution — opencode-proxy-one

**Version** : 1.2.1 | **Ratifiée** : 2026-11-04 | **Dernière modification** : 2026-11-10

Périmètre : la jambe gratuite (`opencode` free), les stations VPN, le registre
des serveurs/IP, l'allocateur, la rotation et le catalogue d'egress. Le reste du
proxy conserve ses règles existantes.

## Principes fondamentaux

### I. La disponibilité se mesure, elle ne se déclare pas

Toute affirmation de disponibilité — par station ou par flotte — doit être
calculée depuis un journal d'incidents persistant et des sondes synthétiques
horodatées. Un chiffre affiché sans échantillons bruts associés est un bug, pas
une métrique. Les fenêtres de calcul sont 1 h, 24 h, 7 j et 30 j, et le verdict
`ok` / `degraded` / `breached` suit les seuils de la spec. `connected` n'est
jamais synonyme de `client_usable`.

### II. Trois ressources, trois cycles de vie

Une **station** (slot : conteneur, tunnel, port SOCKS), une **session**
(connexion NordVPN, ressource rare et bornée par le compte) et une **IP**
(egress public, ressource périssable soumise au quota amont) sont trois objets
distincts. Le cooldown appartient à l'IP, jamais à la station ; une panne de
tunnel ne condamne jamais l'IP. Chaque objet a une machine à états unique et un
seul propriétaire.

### III. Une seule source de vérité par cycle de vie

`registry.db` est l'autorité pour les serveurs, transports, endpoints, IP,
quotas, leases, observations, scores, pool chaud, balayage, jobs, fallbacks et
incidents. Les fichiers JSON (`free_pool.json`, `fresh_chain_state*.json`,
`shared_rotation.json`, `latency_engine.json`) sont des exports en lecture
seule, jamais une seconde vérité. La télémétrie volumineuse vit dans
`telemetry.db`, avec sa propre rétention.

### IV. Le reset fournisseur est une barrière dure

Aucune IP n'est sondée ni utilisée avant son `reset_at`. Ce timestamp vient
exclusivement du fournisseur (`Retry-After`, corps de la 429) ; une estimation
locale est interdite. Après reset, un jitter empêche la ruée simultanée. Une
429 sans succès antérieur est une mauvaise IP, pas un quota consommé : le
traitement diffère.

### V. Une IP d'egress n'appartient qu'à une station

Claim exclusif par IP, index d'unicité en base, libération explicite à la
réaffectation. Deux stations ne partagent jamais un egress, même si les
hostnames diffèrent. Un bind qui échoue (`no-egress`, `bind_error`) n'est
jamais enregistré comme gagnant. Aucune IP n'est réutilisée avant libération.

### VI. Indépendance des stations et couverture mondiale

Chaque station est indépendante : elle peut utiliser n'importe quel pays, sans
quota par pays ni par continent. Il n'existe **aucune** contrainte de
répartition géographique. La seule contrainte dure est l'unicité de l'IP
d'egress. Une préférence de diversité peut exister comme option GUI, désactivée
par défaut ; elle ne devient jamais une porte dure sans décision explicite de
l'opérateur. Un incident corrélé (pays, /24) est surveillé et exposé, pas
masqué par un cantonnement.

**Transports** : chaque station peut utiliser indépendamment **WireGuard**,
**OpenVPN UDP** et **OpenVPN TCP**. Aucun bail global de transport, aucune
clé partagée et aucun verrou inter-stations ne limite l'usage simultané d'un
transport par plusieurs stations. La seule exclusivité globale reste l'IP
d'egress (principe V). Le cycle de qualification d'un hostname essaie les
trois transports, puis revient à WireGuard ; une station peut être réparée
sur n'importe lequel des trois.

### VII. Aucune preuve, aucun succès

Un `200` ne compte que si le flux a produit du contenu et n'a pas émis de trame
`event: error`. Un bind sans egress réel n'est pas un succès. Une IP issue de
l'inventaire historique n'est pas sélectionnable avant re-validation. Une
mesure non conservée est une opinion.

### VIII. Autonomie complète, contrôle total, un seul réparateur

Le système démarre, se surveille, se répare, découvre, valide et promeut sans
intervention de l'opérateur. Le **superviseur est le seul propriétaire de la
réparation** ; le manager/watchdog ne recrée jamais une station que le
superviseur répare. Chaque décision automatique est journalisée avec sa cause
et sa preuve. Un interrupteur unique arrête l'automatisation sans perdre
l'état ni corrompre les assignations.

### IX. Le score ordonne, les portes dures décident

Le classement est un score composite calculé sur des observations récentes :
**40 % taux de 200, 25 % latence, 20 % débit, 10 % stabilité, 5 % fraîcheur**
(poids ajustables GUI). Le pays et la source ne sont pas des facteurs de score ;
la source ne départage qu'à score égal. Le score ne contourne jamais une porte
dure : `reset_at`, unicité d'IP, preuve de contenu. Une IP performante épuisée
sort du classement jusqu'à `reset_at`, puis redevient éligible et est réutilisée
à la prochaine rotation qui en a besoin, sans bascule proactive.

### X. La dégradation est explicite et reste free

Quand le SLO ne peut pas être tenu — pool épuisé, limite de sessions, 429
global — le système expose l'état dégradé et sa raison. Il ne fabrique jamais
un 6/6. La jambe free ne bascule **jamais** vers le payant, un second compte ou
le résidentiel comme substitut silencieux : elle expose la capacité dégradée et
alerte. Toute action de repli est free, tracée et réversible.

### XI. Fallbacks multiples, aucune dépendance unique

Chaque dépendance externe a une chaîne de fallback ordonnée et configurable :
catalogue (API → cache → Gluetun → embarqué → registre), manifeste, DNS
(DoH/DoT/plain), site de test d'IP (SOCKS5h → SOCKS5 → HTTP → control API
Gluetun → `docker exec`), bind (Compose → control API → Docker), probe
(modèles de secours) et superviseur. Un site d'IP ou un resolver unique ne peut
pas rendre une station inutilisable. Une bascule est journalisée avec sa cause.

### XII. Toute option GUI a un effet réel

Chaque contrôle du GUI doit être décrit par un contrat
`option → clé config → consommateur runtime → effet observable → test`. Une
option sans consommateur est supprimée ou marquée « affichage seul ». Le GUI
affiche la valeur effective, pas seulement la valeur enregistrée. Aucune option
inerte n'est acceptée.

### XIII. Données traçables, migrables et sauvegardées

Chaque serveur, endpoint et IP porte sa provenance et sa fraîcheur. Le schéma a
des clés primaires, clés étrangères, contraintes, index et migrations
versionnées. Les observations sont append-only ; les scores sont matérialisés à
part. La base a une rétention, une purge, un checkpoint WAL, un backup
`VACUUM INTO` vérifié et un `integrity_check` régulier. Une migration est
testée sur copie et se rollback par backup.

### XIV. Architecture modulaire, strangler, zéro nouveau monolithe

Le domaine est pur (aucune I/O), les dépendances passent par des ports, les use
cases sont testables avec des adapters factices. La migration se fait par
strangler : nouveau chemin à côté de l'ancien, shadow, canary, puis
décommissionnement. Aucun nouveau module ne dépasse 1 500 lignes ; les
monolithes existants sont extraits par lots. La dette est suivie dans
`debt.md` : 0 P0 ouverte aux gates, P1 planifiée, pas de nouvelle dette non
listée.

### XV. Multiplateforme par construction

Le système doit fonctionner sur **Windows, Linux et macOS** sans fork ni
chemin de code parallèle non testé. Les règles sont les suivantes :

- Le domaine et les cas d'usage ne contiennent **aucune** API spécifique à un
  OS : pas de `winreg`, `msvcrt`, `fcntl`, `signal.SIGBREAK`, `os.startfile`
  hors d'un adapter de plateforme.
- Toute différence d'OS vit dans un **adapter de plateforme** unique
  (`core/platform/` ou équivalent), avec une implémentation par OS et une
  capacité déclarée (`supports_service`, `supports_symlink`, `process_group`).
- Les chemins passent par `pathlib` ; aucun `C:\`, `\`, `/` en dur, aucun
  `.exe` en dur. Les exécutables se résolvent par `sys.executable` ou
  `shutil.which`.
- Les commandes système sont passées en **liste d'arguments** ; `shell=True`
  est interdit pour un processus lancé par le service. Un script PowerShell
  peut être un wrapper de confort, jamais le seul chemin d'exécution.
- Chaque fonctionnalité opérationnelle (démarrage, rotation, probe, backup,
  migration, scheduler) a un point d'entrée **Python cross-platform**. Les
  tâches planifiées, services et chemins de logs ont un équivalent par OS.
- La gate non-Docker tourne sur **Windows et Linux** (matrice CI) ; un test
  spécifique à un OS est marqué et a une contrepartie ou une justification
  écrite. Aucune régression ne peut être « corrigée » en désactivant un OS.

### XVI. Aucune fenêtre intempestive, exécution silencieuse

Aucun processus lancé par le service — dashboard, superviseur, scheduler,
worker, probe, rotation, migration, tâche planifiée — ne doit ouvrir de fenêtre
visible ni voler le focus. Sur Windows, cela signifie `CREATE_NO_WINDOW` (ou
`STARTF_USESHOWWINDOW` + `SW_HIDE`) pour **tout** `subprocess.Popen`,
`subprocess.run`, `call`, `check_output` et `check_call`. Sur POSIX, les
processus détachés utilisent `start_new_session=True`, `stdin=DEVNULL` et des
logs fichier, jamais un TTY interactif.

Règles opposables :

- `os.system`, `os.popen`, `cmd /c start`, `start` et `powershell -Command`
  avec fenêtre sont interdits dans le code du service.
- Un wrapper commun (`core.process.run_hidden` / `popen_hidden`) est la seule
  voie autorisée pour lancer un processus non interactif ; il centralise
  `creationflags`, `start_new_session`, `stdin`, `cwd` et le scrub
  d'environnement.
- Les scripts purement interactifs (CLI lancée par l'opérateur dans un
  terminal) sont exemptés, mais ils doivent être explicitement listés dans
  l'audit et ne jamais être invoqués par le service.
- Un gate statique (`scripts/platform_audit.py`) échoue si un module runtime
  lance un processus sans politique silencieuse, ou utilise `shell=True`.
- Aucune fenêtre ne s'ouvre pendant un test, une migration, un cycle de
  catalogue ou une rotation, y compris en cas d'erreur ou de timeout.

### XVII. Fiabilité, performance, qualité, modularité, maintenabilité

Ces cinq propriétés sont des **gates mesurables**, pas des intentions. Une
livraison qui en viole une est refusée, même si la fonctionnalité marche.

**Fiabilité**
- Un cycle, une rotation ou une migration échoué conserve l'état précédent et
  ne vide jamais un pool ; le système est fail-soft et idempotent.
- Toute écriture critique est transactionnelle ; un crash laisse un état
  cohérent, jamais une demi-assignation.
- Les dépendances externes ont une chaîne de fallback testée ; une source
  morte ne bloque pas les autres.
- Un redémarrage de l'OS restitue l'état persisté (catalogue, TTL, `reset_at`,
  slots) sans revalidation manuelle.

**Performance**
- Aucune I/O réseau, aucun `sleep`, aucun scan lourd dans le chemin de
  requête ; ces travaux vont en tâche de fond ou en `asyncio.to_thread`.
- Budgets explicites et testés : API catalogue p95 < 500 ms pour 1 000
  options ; UI p95 < 2 s ; cycle de 1 000 candidats < 5 min en mode
  échantillon ; démarrage à froid non dégradé par le catalogue.
- Mémoire et disque bornés : déduplication, batchs, rétention, index,
  pagination. Aucune requête ne charge 20 000 lignes dans l'UI.

**Qualité**
- Chaque exigence fonctionnelle a un test automatisé ou une preuve live
  référencée ; chaque bug corrigé a un test de non-régression.
- Zéro option morte : `config_audit --strict` à 0 dead / 0 missing et
  `option-map` à 0 ligne manquante.
- Zéro test instable toléré : un test flaky est isolé, corrigé ou marqué avec
  une justification et un budget, jamais ignoré silencieusement.
- Lint, types et audit de plateforme passent avant merge.

**Modularité**
- Domaine pur sans I/O, dépendances par ports/adapters, pas de cycle
  d'import, pas d'import d'un adapter par le domaine.
- Aucun nouveau module > 1 500 lignes ; les monolithes existants sont
  extraits par strangler, pas agrandis.
- Ajouter une source = une ligne de configuration ; ajouter un provider = un
  adapter ; ajouter une option GUI = un contrat `option-map`.

**Maintenabilité**
- Toute option de configuration ou de GUI a un consommateur runtime, un effet
  observable et un test (principe XII).
- Toute migration est versionnée, testée sur copie, réversible par backup.
- Le changement met à jour dans le même lot : spec, plan, `CHANGELOG`,
  `option-map`, config et documentation. Un écart doc/config est un bug.
- La dette est suivie ; 0 P0 ouverte aux gates.

## Contraintes techniques

- Python 3.12+, SQLite WAL, Docker Compose, image Gluetun épinglée par
  version/digest.
- `registry.db` pour les décisions ; `telemetry.db` pour les requêtes
  volumineuses et leur rétention.
- Aucune dépendance externe nouvelle sans justification écrite dans le plan.
- Les seuils vivent dans `config.yaml`, pas en dur dans le code.
- **Windows, Linux et macOS** : le cœur Python est identique ; les différences
  d'OS sont isolées dans un adapter de plateforme et couvertes par la matrice
  CI. Les scripts PowerShell sont des wrappers facultatifs.
- **Processus silencieux** : tout lancement non interactif passe par
  `core.process.run_hidden`/`popen_hidden` (`CREATE_NO_WINDOW` sur Windows,
  `start_new_session` sur POSIX). Aucune fenêtre, aucun TTY, aucun prompt.
- Les chemins sont `pathlib`, les exécutables sont résolus dynamiquement ;
  aucun chemin absolu Windows ni `.exe` en dur dans le domaine.
- Les preuves live (JSON brut, logs, captures) vont dans `docs/evidence/`.
- Une IP sans hostname connu n'est pas une IP disponible : c'est une ligne de
  journal en attente de mapping.
- La jambe free ne dépend jamais de la jambe payante.


## Flux de développement

- Toute fonctionnalité passe par `/speckit.specify`, `/speckit.clarify`,
  `/speckit.plan`, `/speckit.checklist`, `/speckit.tasks`,
  `/speckit.implement`, puis `/speckit.converge`.
- Chaque exigence fonctionnelle a soit un test automatisé, soit une preuve live
  référencée ; les deux sont préférables.
- Un changement de comportement met à jour la spec, le changelog, la config et
  la carte du dashboard dans le même lot.
- Un écart entre la config et les docs est un défaut de la même gravité qu'un
  bug : il est corrigé ou justifié.
- La dette est mise à jour à chaque phase ; une phase ne se ferme pas avec une
  P0 ouverte.
- **Gates obligatoires avant merge** : `pytest -k "not docker"`,
  `config_audit --strict`, `platform_audit --strict`, `test_option_map.py`,
  `npm run check` (si UI touchée), et la matrice CI Windows/Linux.

## Gouvernance

La constitution prime sur les plans, les runbooks et la configuration. Tout
écart doit être justifié dans la revue, daté, et référencé dans la spec. Un
amendement est une entrée datée avec incrément de version ; il ne peut pas
affaiblir les principes I, IV, V, VII, X (free-only), XIV (modularité), **XV
(multiplateforme), XVI (exécution silencieuse) et XVII (gates de qualité)** sans
décision explicite de l'opérateur.

**Version** : 1.2.1 | **Ratifiée** : 2026-11-04 | **Dernière modification** : 2026-11-10
