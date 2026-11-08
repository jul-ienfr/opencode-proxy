# Research — 011 Station fleet usability

**Feature** : `specs/011-station-fleet-usability/`  
**Date** : 2026-11-07  
**Sources** : `docs/AUDIT_STATIONS_NORDVPN_WARP_2026-11-07.md`, `docs/AUDIT_STATIONS_IP_2026-10-26.md`, `docs/PLAN_REPARATION_STATIONS_2026-10-31.md`, `docs/ALLOCATOR_ALL_STATIONS.md`, `docs/quota-identity.md`, `specs/001`–`010`, code `vpn/`, `free/`, `ops/`, `rotation/`, `scripts/`.

## D-01 — Un seul desired state, avec génération

**Décision** : créer un objet `DesiredStationSpec` par station et par endpoint, persisté dans `registry.db`, avec un numéro de génération monotone. Toute création/recréation/suppression est dérivée de cet objet. Le `.env`, le compose, la mémoire du manager et le registre lisent la même génération.

**Rationale** : l'audit live montre `.env` = wireguard, conteneur = openvpn, mémoire = variable, et un reconcile qui supprime les conteneurs. Trois writers produisent trois vérités. Une génération unique rend l'idempotence vérifiable : un conteneur est conforme si `(name, stack, transport, host, port, generation)` correspond.

**Alternatives rejetées** :
- Garder `.env` comme source : impossible, il est déjà contredit par les conteneurs.
- Garder la mémoire du manager : perdue au redémarrage, non partageable entre processus.
- Utiliser les labels Docker seuls : utiles comme cache, insuffisants pour l'historique et les leases.

## D-02 — Reconcile non destructeur

**Décision** : le reconcile compare desired et actual et n'agit que sur une différence prouvée. Un conteneur dont le stack et la génération correspondent n'est jamais supprimé. Une divergence de stack déclenche **un** recreate planifié, pas une boucle.

**Rationale** : `vpn/manager.py:11284-11324` supprime les conteneurs sur simple divergence `.env` vs inspect, et `_compose_env` (`:5441-5444`) réimpose le stack mémoire. La boucle observée toutes les ~10 min est la cause directe du 0/6.

**Alternatives rejetées** :
- Désactiver complètement le reconcile : laisse les orphelins et la dérive.
- Reconcile uniquement au boot : ne corrige pas une dérive en cours de vie.

## D-03 — Un seul propriétaire de la réparation

**Décision** : le superviseur (`scripts/fresh_chain_supervisor.py` + `rotation/application/repair.py`) est le seul à créer, recréer, parker et réparer. Le watchdog `vpn/manager.py` devient un **observateur** qui signale et laisse le superviseur agir ; il ne recrée plus. `station_repair.enabled` devient `true` et `repair_owner` est appliqué.

**Rationale** : `station_repair.enabled: false` (`config.yaml:425`) alors que le watchdog a fait 31 restarts en 10 min (`logs/debug.log`) et que le superviseur s'arrête (`fresh_chain_supervisor.py:1236-1241`). Constitution VIII : un seul réparateur.

**Alternatives rejetées** :
- Laisser le watchdog réparer : il ne connaît ni les claims ni les leases, et il boucle sur un host mort.
- Laisser les deux : course et churn, déjà observés.

## D-04 — La maintenance est indépendante de la sélection trafic

**Décision** : un provider non sélectionné dans `egress_providers` reste maintenu, surveillé et réparé. La sélection ne contrôle que l'usage client, pas le cycle de vie.

**Rationale** : `egress_providers: [warp]` (`config.yaml:140-142`) mais six stations NordVPN tournent ; le superviseur s'arrête complètement (`fresh_chain_supervisor.py:1236-1241`), donc les stations NordVPN ne sont jamais réparées. L'objectif « toutes les stations utilisables » exige que la maintenance soit orthogonale au routage.

**Alternatives rejetées** :
- Supprimer les stations non sélectionnées : perte de capacité et de bascule.
- Ne maintenir que le provider actif : rend le fallback inutilisable.

## D-05 — Santé par étapes et preuve client

**Décision** : machine à états `created → starting → tunnel_up → dns_ok → egress_ok → quota_ok → client_usable`, avec `degraded`, `parked`, `failed`. Le healthcheck Docker n'est qu'un signal parmi d'autres. `client_usable` exige une preuve de contenu horodatée.

**Rationale** : `docker-compose.yml:278-283` teste `wget api.ipify.org` à travers le tunnel ; un tunnel mort et un quota épuisé produisent le même `unhealthy`. Constitution I et VII : la disponibilité se mesure, un 200 sans contenu ne compte pas.

**Alternatives rejetées** :
- Garder `healthy` Docker comme vérité : ne distingue pas DNS, auth, quota, serveur mort.
- Sonde uniquement au moment de la requête : ne permet pas la maintenance proactive.

## D-06 — Unicité par bucket, pas par endpoint

**Décision** : la capacité compte les **buckets uniques** (`/32` IPv4, `/64` IPv6). Un endpoint WARP supplémentaire dans un `/64` déjà utilisé n'ajoute pas de capacité. Le rapport expose `configured`, `created`, `running`, `healthy`, `egress_ok`, `quota_ok`, `client_usable`, `unique_buckets`.

**Rationale** : l'audit live trouve 6 endpoints WARP mais 5 `/64` (deux endpoints dans `2a09:bac1:27a0:2060::/64`), alors que `dashboard/api.py:1599-1609` et `free/pool.py:4198-4220` comptent 6. Le conseiller 010 peut donc dire `ok` à tort. Constitution V : une IP d'egress n'appartient qu'à une station.

**Alternatives rejetées** :
- Compter les endpoints : faux, déjà observé.
- Compter les conteneurs : faux, un conteneur peut changer d'IP.

## D-07 — Réconciliation live de l'egress WARP

**Décision** : à chaque cycle (ou sur événement Docker), mesurer l'egress de chaque endpoint WARP, comparer à `egress_ip`/`block`, et écrire la vérité dans le registre, la config et le ledger. Un changement non planifié est traité comme une dérive, pas comme un succès silencieux.

**Rationale** : 5/6 entrées `config.yaml` sont périmées ; `_persist_socks5_egress` n'est appelé que par le rotator (`free/pool.py:2938-2952`). Un redémarrage externe change l'IP sans mise à jour. La détection de doublons et le ledger deviennent faux.

**Alternatives rejetées** :
- Persister uniquement à la rotation : ne couvre pas les redémarrages, crashs, `docker restart`.
- Faire confiance à `config.yaml` : contredit le live.

## D-08 — Rotation avec budget persisté

**Décision** : la rotation NordVPN et WARP partage un budget par station et par heure, persisté dans `registry.db`, rechargé au démarrage. Les budgets sont alignés sur `station_max_rotations_per_hour` et un backoff par cause.

**Rationale** : `free/warp_rotation.py:31-38` garde `LAST_ROTATION` en mémoire et ne lit pas `station_max_rotations_per_hour` ; un restart remet le compteur à zéro. La rotation storm est un risque réel.

**Alternatives rejetées** :
- Budget uniquement en mémoire : perdu au restart.
- Budget global : une station peut consommer tout le budget.

## D-09 — Reset fournisseur = barrière dure

**Décision** : aucune IP n'est sondée ou utilisée avant `reset_at`. Le timestamp vient du fournisseur (`Retry-After`, corps 429). Le ledger local est un cache, pas une autorité. Un 429 sans succès antérieur est classé « mauvaise IP », pas « quota consommé ».

**Rationale** : `docs/quota-identity.md` montre que le reset est propre à l'IP et mesuré entre 00:01 et 00:53 UTC ; la spec 007 et le ledger existent déjà. Les logs `fresh_chain_*` montrent des 429 immédiats sur des IP fraîches, ce qui suggère des IP déjà brûlées ou une sélection trop naïve.

**Alternatives rejetées** :
- Reset local à minuit : faux, contredit les mesures.
- Réessayer avant reset : brûle le quota et fausse les métriques.

## D-10 — Provisioning WARP déclaratif

**Décision** : créer un `WarpProvisioner` idempotent (plan/apply) qui crée, met à jour et supprime les conteneurs WARP à partir de `DesiredEndpointSpec` : nom, port, image, `ipv6`, `managed`, `provider`, `block`, `generation`. Il vérifie l'unicité du `/64` avant création, publie sur `127.0.0.1`, et rollback en cas d'échec.

**Rationale** : la création WARP est aujourd'hui un `docker run` dans `free/warp_rotation.py:315-333`, sans plan, sans idempotence, sans unicité de bloc, sans bind loopback. La spec 005 a un contrat conceptuel (`contracts/egress-provisioning.md`) mais aucune implémentation ; les tâches T-530–T-537 sont ouvertes.

**Alternatives rejetées** :
- Garder `docker run` dans le rotator : mélange rotation et provisioning.
- Ajouter WARP dans `docker-compose.yml` : possible, mais les ports et le nombre d'endpoints sont dynamiques ; un provisioner dédié est plus simple et testable.

## D-11 — Quota : modèle conservateur, mesure réelle

**Décision** : distinguer trois notions : `provider_reset_at` (vérité), `quota_per_ip` (plafond local conservateur), `ledger_used` (observé). Le seuil de rotation est un pourcentage du plafond, pas une vérité provider. La limite de sessions NordVPN est mesurée par une campagne contrôlée avant de figer `station_count` au-delà de 6.

**Rationale** : les docs donnent 500, ~200/5 h, 150/20/8 par modèle, et `ledger_daily_limit: 0`. `dailyRequests` n'est pas connu. La seule mesure contrôlée est « 6 stations + 1 test = AUTH_FAILED » (`PLAN_RETABLISSEMENT_FREE_2026-11-05.md:88-96`). Il faut mesurer avant de dimensionner.

**Alternatives rejetées** :
- Prendre 500 comme vérité : non prouvé.
- Prendre 4 sessions : non prouvé.
- Ignorer la limite : AUTH_FAILED en cascade.

## D-12 — Capacité cible et spare

**Décision** : `target_slots = 6`, `reserve_policy = full` par défaut. `spare_station = 0` tant que la limite de sessions n'est pas mesurée. Le spare devient une option explicite (`spare_station = 1`) après la campagne de mesure et un canary 24 h.

**Rationale** : la config actuelle est 6 sans spare ; le PLAN_REPARATION proposait 6+1 mais n'a jamais exécuté le canary P6 ; la spec 001 dit « pas de 7e sans décision ». L'objectif immédiat est 6/6, pas 7.

**Alternatives rejetées** :
- Forcer 6+1 : risque AUTH_FAILED et complexité.
- Réduire à 4 : sous-utilise la capacité observée.

## D-13 — Observabilité dans le registre

**Décision** : `station_state`, `repair_jobs`, `leases`, `egress_ips`, `quota_buckets` et une nouvelle table `rotation_jobs` sont écrits à chaque transition. Les JSON `logs/*.json` deviennent des exports en lecture seule. L'API `/api/stations` lit le registre, pas la mémoire du processus.

**Rationale** : `registry.db` a `endpoints=0`, `station_state=0`, `server_transports=0`, `repair_jobs=0` ; le statut JSON date du 2026-11-02. Constitution III et XIII.

**Alternatives rejetées** :
- Continuer avec les JSON : pas de transaction, pas d'historique, pas de reprise.
- Écrire les deux : double vérité, déjà observée.

## D-14 — Sécurité par défaut

**Décision** : tous les endpoints WARP sont publiés sur `127.0.0.1` ; le dashboard exige un token si `server.host` n'est pas loopback ; l'image WARP est épinglée par digest ; les secrets ne sont jamais loggés.

**Rationale** : `docker ps` montre `0.0.0.0:11080-11105` pour WARP ; `DASHBOARD_TOKEN not set while host is 0.0.0.0` dans les logs. `caomingjun/warp:latest` est mutable. Constitution XII et XIII.

**Alternatives rejetées** :
- Laisser `0.0.0.0` : exposition LAN du quota.
- Token optionnel : insuffisant.

## D-15 — Migration strangler, pas big-bang

**Décision** : le nouveau chemin `DesiredState + Reconciler + StationHealth` est activé par un flag `station_lifecycle.mode = shadow|enforce` (défaut `shadow`) ; l'ancien chemin reste en secours. On passe à `enforce` après 24 h de shadow sans divergence.

**Rationale** : la base est un monolithe (`vpn/manager.py` > 11 000 lignes) ; un remplacement direct est risqué. La constitution XIV impose le strangler, `shadow` puis canary puis décommissionnement.

**Alternatives rejetées** :
- Réécrire le manager : trop gros, trop risqué.
- Activer directement : pas de retour arrière.

## D-16 — Un seul writer `.env`, atomique

**Décision** : toutes les écritures de `.env` passent par une primitive unique,
atomique (`tmp` unique + `fsync` + `os.replace`) protégée par un verrou
inter-processus. Le superviseur et les chaînes ne réécrivent plus le fichier
entier en concurrence.

**Rationale** : `vpn/manager.py:90` est un `threading.Lock` de process ;
`scripts/fresh_chain_supervisor.py:1104-1121` écrit en `write_text` non
atomique ; `scripts/fresh_chain.py:1263-1290` utilise un `.env.tmp` fixe
partagé par jusqu'à six chaînes parallèles. Une station peut être recréée sans
pin ou avec un pin perdu.

**Alternatives rejetées** :
- Faire confiance au lock de process : ne couvre pas les processus séparés.
- Écrire `.env` depuis le registre uniquement au boot : ne couvre pas les pins
  dynamiques.

## D-17 — Upscale transactionnel

**Décision** : un upscale est une transaction : préflight des ports et du
compose, création, connexion, et rollback complet (`station_count`, registre,
conteneur partiel) si une étape échoue.

**Rationale** : `opencode.py:2586-2595` logge l'échec de `connect()` et
continue ; `station_count` est persisté (`:2706-2712`). Le superviseur traite
ensuite la station fantôme et consomme des rebinds.

**Alternatives rejetées** :
- Laisser la station partielle : elle pollue la capacité et les budgets.
- Retirer seulement le manager : le conteneur et les ports restent.

## D-18 — Démarrage non bloquant du proxy

**Décision** : le proxy démarre même si la station 1 est down. La dépendance
Compose passe de `service_healthy` à `service_started` + healthcheck
applicatif ; le mode dégradé est exposé.

**Rationale** : `docker-compose.yml:181-183` bloque le proxy si la station 1
est AUTH_FAILED, même si les stations 2–6 sont saines. C'est un SPOF de
déploiement.

**Alternatives rejetées** :
- Garder `service_healthy` : un provider en panne empêche tout le service.
- Supprimer la dépendance : le proxy peut démarrer avant le réseau ; le
  healthcheck applicatif gère.

## D-19 — Toutes les clés de politique effectives

**Décision** : chaque clé `station_repair` est lue par le propriétaire, et
`repair_owner` est modifiable à chaud. Les clés inertes sont supprimées ou
branchées.

**Rationale** : `max_recreates_per_hour`, `park_after_failures`,
`verify_egress_before_usable`, `last_good_first` ne sont pas lues ;
`repair_owner` n'a pas de handler `update_config`. Une option GUI sans effet
viole la constitution XII.

**Alternatives rejetées** :
- Documenter les clés comme « affichage seul » : contraire à XII.
- Laisser le doublon `max_restarts_per_hour` : ambiguïté de propriétaire.

## D-20 — Credentials et updater cohérents

**Décision** : le refresh des credentials écrit `OPENVPN_*` **et** la clé
NordLynx dans `vpn_configs/wireguard.env`, de façon atomique, puis redémarre
les stations concernées avec preuve. L'updater raisonne sur le digest réel de
l'image, pas sur un tag divergent.

**Rationale** : `scripts/refresh_nord_credentials.py:47-98` ignore la clé WG ;
les stations WireGuard gardent l'ancienne clé. `vpn/manager.py:9308-9493`
compare un tag alors que le compose est digest-pinné, et `--pull never` ne peut
pas avancer.

**Alternatives rejetées** :
- Changer le compose en `:latest` : contraire à la reproductibilité.
- Laisser le refresh manuel : les stations WG échouent silencieusement.



## D-21 — Consolidation par noyau commun + adapters, pas par fusion

**Décision** : un noyau commun (`ops/lifecycle.py`, `ops/lifecycle_policy.py`,
`ops/lifecycle_evidence.py`) porte l'identité de slot, la machine à états, la
santé, la réparation, la rotation, les budgets, les preuves et le statut. Deux
adapters (`NordVpnProvider`, `WarpProvider`) portent uniquement les mécanismes
irréductibles : compose/credentials/AUTH/session pour NordVPN, docker
run/registration/`/64`/options de conteneur pour WARP.

**Rationale** : l'audit montre des duplications coûteuses (deux normalisations
de bloc, deux résolveurs d'egress, deux réparations, deux statuts, deux logs) et
des divergences de comportement. Mais fusionner les mécanismes créerait un
monolithe impossible à tester : Gluetun et WARP n'ont ni les mêmes credentials,
ni le même cycle de registration, ni les mêmes options. La bonne frontière est
« politique commune, mécanique spécifique ».

**Alternatives rejetées** :
- Tout fusionner dans `vpn/manager.py` : monolithe, contraire à XIV.
- Tout laisser séparé : chaque correctif doit être appliqué deux fois, et les
  comportements divergent déjà.
- Un simple helper partagé : ne règle ni le statut, ni les budgets, ni les
  preuves, ni la réparation.

## Points ouverts à mesurer (pas des ambiguïtés de spec)

1. **Limite de sessions NordVPN** : campagne contrôlée 4/5/6/7/8 avec 1 station à la fois, 10 min entre les tentatives. Décision après mesure.
2. **Plafond de quota réel par bucket** : 500 local vs ~200/5 h observé ; mesure par modèle et par bucket.
3. **Spare** : 0 ou 1 après la mesure de session et un canary 24 h.
4. **Latence de rotation** : budget 240 s ; mesurer p95 sur 50 rotations.
5. **WARP IPv4** : reste désactivé ; ne compte pas comme bucket.

## Références code

- `vpn/manager.py:5394-5485` — `_compose_env`.
- `vpn/manager.py:11190-11324` — reconcile de boot.
- `vpn/manager.py:4302-4458` — commit IP.
- `vpn/manager.py:6372-6471` — fast-pin.
- `free/warp_rotation.py:31-38, 179-333` — rotation WARP.
- `free/pool.py:2938-2952` — persistance egress WARP.
- `free/pool.py:3946-3991` — `set_socks5_proxies`.
- `free/pool.py:4198-4220` — comptage endpoints.
- `free/capacity.py:10-11, 155-200` — modèle capacité.
- `dashboard/api.py:1549-1610` — comptage capacité.
- `scripts/fresh_chain_supervisor.py:1236-1241` — idle NordVPN.
- `config.yaml:136-142, 403, 409-432` — capacité, providers, réparation.
- `docker-compose.yml:185-283` — station 1, healthcheck.
- `rotation/infrastructure/db/schema.sql` — schéma registre.
