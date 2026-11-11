# Audit — gestion des IP, VPN, WARP et proxylist

**Date** : 2026-11-11
**Périmètre** : jambe free, six stations Gluetun/NordVPN, transports
WireGuard / OpenVPN UDP / OpenVPN TCP, registre d'IP et ledger de quota,
WARP, catalogue de proxys publics, superviseur de réparation, observabilité
de la disponibilité.
**Méthode** : lecture du code et de la configuration, requêtes live sur
`http://127.0.0.1:4000`, lecture de `logs/fresh_chain_supervisor.log`,
`logs/debug.log`, `logs/registry.db`, `logs/station_slo.jsonl`, et audit croisé
des modules `vpn/`, `free/`, `scripts/`, `ops/`, `dashboard/`.
**Règle** : chaque conclusion sur une IP vient d'un test de cette IP. Le quota
free est **par IP**, jamais global ni « account-wide ».

---

## 1. Résumé exécutif

Le proxy n'est pas cassé. À l'heure de l'audit :

- `GET /v1/models` répond **200** ; le processus `opencode.py --gui` tourne
  (PID 30404).
- `deepseek-v4.1-flash-free` répond **200 en 5 s** et
  `glm-5.3-flash-free` répond **200 en 59 s** sur une requête réelle.
- Le pool élargi (WARP + proxies) expose **9/16 endpoints utilisables**.
- Mais les **six stations NordVPN sont à 0/6 `client_usable`**, avec des pauses
  AUTH de **718 à 894 s** et des chaînes qui tournent 20 min sans gagner.

Autrement dit : le service continue grâce aux egress de secours, mais l'objectif
« six stations utilisables en permanence » n'est pas tenu. Le problème est un
**ensemble de boucles de réparation**, pas une panne du proxy.

Les trois causes dominantes, vérifiées dans les logs et le code :

1. **Un gagnant fraîchement prouvé est détruit par le nettoyage des pins.**
   `_clear_stale_pins` compare le pin `.env` au host live et l'efface dès qu'ils
   diffèrent — ce qui est toujours le cas avant que le manager ait recréé le
   conteneur. Preuve : `11:43:28 station 1 stale pin al46 cleared (live=al73)`
   puis `11:45:37 station 1 still broken 128s after winner al46...`.
2. **Une pause AUTH globale de 900 s gèle tous les transports, y compris
   WireGuard.** `GlobalAuthGuard` est construit avec ses valeurs par défaut
   (300/900 s), ignore `auth_global_pause_s: 30`, ne décroît jamais et n'est
   remis à zéro que par `_auth_repair_succeeded()`, qui n'est pas appelé quand
   `station_repair.enabled: false` et `repair_owner: supervisor`.
3. **Deux réparateurs et des chaînes trop coûteuses.** Le pool rotate encore sur
   429 (`on_429_action: rotate`) pendant que le superviseur répare la même
   station ; chaque candidat consomme jusqu'à ~22 requêtes réelles (10 modèles
   de secours + seconde confirmation), ce qui brûle le quota de l'IP avant même
   qu'elle serve. Deux slots seulement, 20 min par chaîne, 5 min de cooldown.

Le reste de l'audit détaille les mécanismes, les preuves et les correctifs
priorisés.

---

## 2. Constat live (2026-11-11, ~12:00 locale)

| Élément | Valeur observée | Preuve |
|---|---|---|
| Proxy | up, PID 30404 | `Get-CimInstance Win32_Process` |
| `/v1/models` | 200 | requête HTTP |
| `deepseek-v4.1-flash-free` | 200, 5 s | requête réelle |
| `glm-5.3-flash-free` | 200, 59 s | requête réelle |
| Stations `client_usable` | **0/6** | `/api/pool-status` |
| Pause AUTH | 718–894 s | `logs/fresh_chain_supervisor.log` |
| Chaînes actives | 2 (`running=[1,5]`) | log superviseur |
| Capacité élargie | 9/16 endpoints | `/api/free-health` |
| Incidents flotte | `client_usable_low` ouvert | `registry.db` |

Les six stations : s1 `connected` mais quota épuisé, s2 `error`, s3 `error`,
s4 `error`, s5 `connected` quota épuisé, s6 `connected` quota épuisé.

---

## 3. Findings — stations, chaînes et superviseur

| ID | Sév. | Finding | Preuve | Impact |
|---|---|---|---|---|
| A-01 | P0 | `_clear_stale_pins` efface un pin gagnant sans vérifier l'âge du winner ni l'IP prouvée. | `scripts/fresh_chain_supervisor.py:1314-1343` ; log `11:43:28` | Le 200 fraîchement obtenu est jeté ; la station repart en `still broken`. |
| A-02 | P0 | `_winner_for` lit le state file du transport « préféré » ; une chaîne lancée via `_TRANSPORT_OVERRIDE` écrit un autre fichier. | `scripts/fresh_chain_supervisor.py:1505-1510` ; `config.yaml:1275` | Winner invisible, chaîne qui tourne jusqu'au cap 20 min. |
| A-03 | P0 | Le gagnant n'est pas adopté atomiquement : pin, winner JSON, lease et état manager sont écrits par des acteurs différents. | `fresh_chain.py:2174-2198` ; `supervisor:296-329` | Fenêtre de course ; un autre acteur peut invalider le winner. |
| A-04 | P1 | Deux slots seulement pour six stations ; une chaîne qui échoue monopolise 20 min puis 5 min de cooldown. | `config.yaml:1263` ; `MAX_CHAIN_AGE_S=1200` | Les stations 2/4/6 attendent pendant que 1/5 échouent. |
| A-05 | P1 | Le budget de chaîne est temporel, pas fondé sur la progression (nombre de binds sans 200). | `fresh_chain_supervisor.py:1246-1252` | Une chaîne peut tourner 20 min sur un slice mort. |
| A-06 | P1 | Backoff `no_egress` plafonné sous la base : `max=60 < base=120`. | `config.yaml:1270-1271` ; `catalog_claims.py:34-36` | Les hosts morts reviennent toutes les 120 s, sans escalade. |
| A-07 | P1 | `auth_fallback_transport: ''` rend l'override de transport inerte. | `config.yaml:1275` ; `supervisor:542-575` | Aucune bascule OVPN → WireGuard après deux échecs. |
| A-08 | P1 | `stop_after_429: true` + `fast_discovery: true` : un 429 passe au host suivant sans parcourir les autres transports. | `config.yaml:1277-1279` ; `fresh_chain.py:2109-2119` | Le cycle tri-transports demandé n'est pas systématique. |
| A-09 | P1 | `_active_hosts`/`_active_egress_ips` renvoient ∅ si `/api/vpn-status` échoue (fail-open). | `supervisor:563-576`, `1240-1253` | Deux stations peuvent binder le même host/IP. |
| A-10 | P1 | `acquire_lock` n'est pas atomique et `_stop_chain` supprime le lock sans vérifier le propriétaire. | `chain_repair.py:77-80` ; `supervisor:1275-1278` | Course de PID, relance en boucle, lock volé. |
| A-11 | P2 | Le probe de chaîne essaie jusqu'à 10 modèles de secours puis une seconde passe de confirmation. | `fresh_chain.py:1628-1644`, `2034-2064` | ~22 requêtes réelles par candidat ; quota brûlé. |
| A-12 | P2 | Le signal `client_usable` peut être optimiste : en mode superviseur, `refresh_status` pose `tunnel_up_full` sur control+IP sans probe data. | `vpn/manager.py:3241-3250` ; `ops/providers/nordvpn.py:85-99` | Uptime affiché sans utilisabilité réelle. |
| A-13 | P2 | Le test de `_supervise()` n'existe pas ; les courses winner/pin et la fairness des slots ne sont pas couvertes. | `tests/test_station_allocator_supervisor.py` | Les régressions P0 ne sont pas détectées. |

---

## 4. Findings — VPN et transports

| ID | Sév. | Finding | Preuve | Impact |
|---|---|---|---|---|
| B-01 | P0 | Pause AUTH globale 900 s, non décroissante, déclenchée par 2 stations en 60 s ; elle gèle aussi WireGuard. | `station_repair.py:290-345` ; `vpn/manager.py:8483-8495` | 0/6 prolongé de 15 min à chaque orage AUTH. |
| B-02 | P0 | `auth_global_pause_s: 30` est ignoré : le guard est instancié sans config. | `vpn/manager.py:103` ; `config.yaml:676` | La valeur documentée n'a aucun effet. |
| B-03 | P0 | `_auth_repair_succeeded()` n'est appelé que par le chemin de réparation du manager, désactivé (`station_repair.enabled: false`). | `vpn/manager.py:8352`, `8382` ; `config.yaml:686` | Le level du guard ne redescend jamais ; 900 s à chaque fois. |
| B-04 | P1 | En production `vpn_stack: wireguard`, `auto_mixed_stacks: false` : le manager ne bascule jamais WG → OVPN. | `config.yaml:90,715` ; `vpn/manager.py:7070`, `7251-7255` | La cascade tri-transports n'existe que dans le scanner. |
| B-05 | P1 | `transport_cycle_timeout_s: 45` et `transport_auth_backoff_s: 90` ne sont consommés nulle part. | `config.yaml:697-698` | Pas de budget par transport ; les binds s'empilent. |
| B-06 | P1 | La limite de six sessions NordVPN est dépassée par les binds temporaires, le canary WG et les recreates. | `PLAN_FREE_6OF6_2026-11-03.md:214,253` ; `vpn/manager.py:7325-7405` | AUTH_FAILED en cascade, attribué à tort aux identifiants. |
| B-07 | P1 | Le binder de chaîne ne partage pas le `_auth_gate` du manager (processus différents). | `vpn/auth.py:32-99` ; `free/catalog_claims.py:500-519` | Les binds concurrents ne sont pas sérialisés globalement. |
| B-08 | P1 | Une seule clé WireGuard partagée par les six stations ; le canary est une septième session. | `vpn/manager.py:7325-7405` ; `docker-compose.yml:563-613` | Pas d'identité WG par station ; limite de sessions. |
| B-09 | P2 | `bind_error` conflate compose, hostname invalide et no-egress. | `compose_binder.py:161-184` ; `gluetun_control.py:73-97` | Impossible de choisir la bonne escalade. |
| B-10 | P2 | Le healthcheck Docker dépend de l'image ; sans `HEALTHCHECK`, WARP brûle 4×90 s. | `free/warp_rotation.py:618-634` | Rotation WARP bloquée jusqu'à 6 min. |

---

## 5. Findings — IP, quota, registre, allocateur

| ID | Sév. | Finding | Preuve | Impact |
|---|---|---|---|---|
| C-01 | P0 | `assign_observed` met `reset_at=NULL` et `state='assigned'` sans exiger une preuve de 200 fraîche. | `free/ip_registry/registry_db.py:145-155` | Une IP épuisée peut être réassignée et repasser « fresh ». |
| C-02 | P0 | `_record_registry_winner` fait un check-then-insert non transactionnel. | `fresh_chain.py:1452-1528` | Deux chaînes peuvent claim la même IP ; `IntegrityError` après écriture partielle. |
| C-03 | P0 | Au boot, des états basés sur `time.monotonic()` sont restaurés tels quels (`blocked_until`, `parked_until`, `last_ok_at`). | `free/ip_quota.py:475` ; `free/station_health.py:347` | Après reboot, une IP ou une station peut rester « bloquée » des heures. |
| C-04 | P0 | Le code traite ≥3 429 sur des IP distinctes comme un épuisement de compte et refroidit toutes les stations. | `opencode.py:15400-15419` ; `free/pool/__init__.py:5927-5964` | Contredit la règle « quota par IP » ; gèle des IP saines. |
| C-05 | P1 | Le ledger plafonne `Retry-After` à `max_block_s` (1200 s) et reteste avant le reset fournisseur. | `free/quota_ledger.py:309-316` | Une IP encore épuisée est retestée, puis re-429. |
| C-06 | P1 | Un 429 sans corps connu est classé `unknown` puis marqué quota (`FreeUsageLimitError`), parké jusqu'à minuit. | `fresh_chain.py:1599-1607` ; `opencode.py:24906-24913` | Une IP saine peut être condamnée pour la journée. |
| C-07 | P1 | Le warm pool n'est pas un standby : jamais lu par le superviseur, jamais réapprovisionné en fond, TTL 6 h. | `free/warm_pool.py` (usage tests) ; `fresh_chain.py:502-560` | Aucune IP prête au moment de la coupure. |
| C-08 | P1 | La rotation proactive est désactivée (`rotation_planner.enabled: false`, `spare_station: 0`). | `config.yaml:287-289` ; `pool:1888-1932` | On rotate seulement quand le 429 est déjà arrivé. |
| C-09 | P1 | Le chemin requête attend 5 s une rotation déléguée au superviseur, qui poll toutes les 20 s. | `pool:6327-6362` ; `supervisor:42` | Échec client avant même le début de la réparation. |
| C-10 | P1 | Le pool rotate encore lui-même sur 429 (`on_429_action: rotate`) en plus de la file de réparation. | `config.yaml:232` ; `pool:6600-6629` | Deux rebinders pour la même station. |
| C-11 | P2 | Le /24 n'est pas modélisé ; seul un tri de préférence existe. | `fresh_chain.py:1030-1038` | Corrélation de quota ignorée ; rafales sur le même /24. |
| C-12 | P2 | `daemon_max_hosts: 40` alors que la dette D-014-10 est marquée résolue avec 1. | `config.yaml:1186` ; `DEBT.md` | Écart config/dette ; charge de qualification incontrôlée. |

---

## 6. Findings — WARP et proxylist

| ID | Sév. | Finding | Preuve | Impact |
|---|---|---|---|---|
| D-01 | P0 | Le catalogue WARP est un snapshot statique ; les rotations ne sont pas vues avant un restart. | `free/egress_runtime.py:258-277,405` ; `free/egress_adapters/warp.py:31-42` | Des egress morts restent « healthy ». |
| D-02 | P0 | Le probe WARP ne force pas la famille IPv4/IPv6 : une ligne IPv6 peut être promue par un egress IPv4. | `opencode.py:3094-3140` | Fausse preuve ; l'IPv6 échoue ensuite. |
| D-03 | P1 | Une panne locale (worker, Docker, timeout) écrit `dead` avec TTL 7 jours. | `free/egress_adapters/public.py:74-83,511-517` ; `warp.py:99-121` | Un incident local évince des proxys sains une semaine. |
| D-04 | P1 | La denylist filtre le host, pas l'IP d'egress découverte. | `public.py:474-481` | Un proxy interdit peut être promu via une autre IP. |
| D-05 | P1 | Les erreurs de source ne sont pas persistées (`mark_source_run` absent). | `public.py:354-364` | Sources mortes invisibles. |
| D-06 | P1 | Le probe de revalidation consomme ~48 requêtes/jour/IP sur des egress déjà qualifiés. | `public.py:411-428` | Le quota est brûlé par la surveillance. |
| D-07 | P1 | Le worker public redémarre quand l'ordre des lignes change. | `pool:4258-4284` | Churn de conteneurs, coupures. |
| D-08 | P1 | Aucune réserve de standby : WARP/proxies sont consommés par le trafic normal (`reserve_policy: shared`). | `config.yaml:137-139` ; `pool:4903-5068` | Plus de secours au moment où NordVPN tombe. |
| D-09 | P2 | Rotation WARP sous un lock global, 4 tentatives × 90 s + 30 s. | `free/warp_rotation.py:277,618-634` | Une rotation bloque les autres. |
| D-10 | P2 | `success_count` utilise `max()`, un proxy une fois bon garde 1.0 indéfiniment. | `free/egress_store.py:520-524` | Score sans décroissance ; mauvais ordre. |

---

## 7. Findings — mesure, SLO, alertes

| ID | Sév. | Finding | Preuve | Impact |
|---|---|---|---|---|
| E-01 | P0 | Il n'existe pas de métrique d'uptime par station ; la dispo est un booléen instantané. | `free/qualification/readmodel.py:527,688-741` ; `scripts/station_slo_watch.py:180-213` | Un 99,99 % n'est pas calculable ni vérifiable. |
| E-02 | P0 | Quatre définitions concurrentes de `client_usable` (pool, watchdog, sampler, rapport « connected+degraded »). | `pool:7660-7664` ; `qualification/watchdog.py:161-169` ; `station_slo_report.py:98-104` | Le dashboard peut afficher 0/6 et le proxy servir en même temps. |
| E-03 | P0 | La cible est 99 %, pas 99,99 %, et les fenêtres 7 j/30 j n'existent pas. | `dashboard/routers/qualification_routes.py:412` ; `QualificationStatus.svelte:217` | L'objectif utilisateur n'est pas représenté. |
| E-04 | P1 | `availability_1h_pct` n'est pas une fenêtre : 100 % à la promotion, 0 % sinon, et moyenne des valeurs >0. | `free/qualification/station_runtime.py:81-86` ; `readmodel:700-710` | Une station à 100 % masque cinq stations à 0 %. |
| E-05 | P1 | Deux journaux d'incidents divergent (`registry.db` et `station_incidents.db`). | `readmodel.py:558-574` ; `scripts/station_slo_watch.py:45-59` | Deux vérités, aucune fiable. |
| E-06 | P1 | Les alertes sont opt-in, et `pool_floor` ne correspond pas à `pool_usable_floor`. | `docker-compose.yml:684-704` ; `vpn_alerts.py:114-119` | Aucune alerte 0/6 en pratique. |
| E-07 | P1 | `max_effective_age_s: 900` dépasse le budget 99,99 % (~4,3 min/mois). | `config.yaml:663` | La cible est invérifiable avec une fenêtre de 15 min. |
| E-08 | P2 | Le canary tourne en shadow et ne prouve pas le cutover réel. | `config.yaml:1157,1188` ; `canary.py` | Faux vert. |

---

## 8. Chaîne de causalité du 0/6 observé

1. Une chaîne trouve un 200 et écrit le winner + le pin.
2. Le manager n'a pas encore recréé le conteneur : `live_host` est encore
   l'ancien.
3. `_clear_stale_pins` efface le pin du gagnant (`A-01`).
4. Le winner JSON devient incohérent ; `_winner_for` peut ne pas le voir (`A-02`).
5. La station reste `still broken` au-delà de `winner_grace` (10 s) ; le
   superviseur relance une chaîne (`A-03`).
6. Pendant ce temps, un bind OpenVPN échoue ; deux stations en 60 s arment le
   guard global 900 s (`B-01`, `B-03`).
7. Le superviseur passe à une seule probe, espace les starts de 60 s ; les deux
   slots tournent 20 min sur des slices brûlés (`A-04`, `A-05`).
8. Chaque candidat consomme ~22 requêtes ; le quota des IP neuves est brûlé
   avant le cutover (`A-11`).
9. Le pool rotate aussi de son côté sur 429 (`C-10`) ; deux rebinders se
   marchent dessus.
10. Le ledger reteste des IP avant `reset_at` (`C-05`) et condamne des IP sur un
    429 inconnu (`C-06`).
11. Résultat : 0/6 pendant que WARP/proxies servent, et le dashboard montre
    deux vérités (`E-02`).

---

## 9. Ce qui n'est pas le problème

- Le proxy HTTP et le routage des modèles : 200 sur les deux modèles free.
- Le nombre de serveurs NordVPN disponibles : le catalogue contient des
  milliers de serveurs ; le problème est la qualification et la rotation.
- Le quota « global » : il n'existe pas. Les 429 observés qualifient des IP
  testées, pas l'ensemble du pool.
- L'absence d'IP : des centaines d'IP et de /24 restent disponibles ; il faut
  les qualifier sans brûler leur quota.
- Le besoin d'un compte payant, d'un second compte ou d'attendre : hors
  périmètre et interdit par la constitution.

---

## 10. Baseline chiffrée à figer avant correctifs

| Métrique | Valeur de départ | Source |
|---|---|---|
| Stations `client_usable` | 0/6 | `/api/pool-status` |
| Durée du 0/6 courant | > 60 min | logs |
| Pause AUTH | 718–894 s | log superviseur |
| Chaînes utiles / heure | ~0–1 | log |
| Coût probe / candidat | jusqu'à ~22 requêtes | code |
| Rotation p95 observée | > 5 min (worst case) | code + logs |
| 6/6 sur 24 h (audit 014) | 49,8 % | `specs/014/audit.md` |
| 0/6 sur 24 h (audit 014) | 38,4 % | `specs/014/audit.md` |

---

## 11. Conclusion

Le système a déjà les briques : registre, ledger, claims, transports,
superviseur, WARP, catalogue de proxys, qualification. Le défaut est
**l'ordonnancement et la cohérence entre ces briques** : un gagnant est détruit,
une panne locale devient globale, un quota local devient un gel de flotte, et
la disponibilité n'est pas mesurée.

Le plan `015-egress-uptime-slo` attaque ces trois propriétés dans l'ordre :
1. **Cohérence** : un seul gagnant, un seul réparateur, une seule vérité.
2. **Indépendance** : une station, un transport et une IP ne peuvent plus geler
   les autres.
3. **Vitesse** : standby préqualifié, cutover make-before-break, mesure du SLO.

---

## 12. Addendum — contrainte opérateur du 2026-11-11 : six stations exactement

L'opérateur fixe le dimensionnement : **six stations, pas une de plus**. Chaque
station doit être réparée et rotée automatiquement dès qu'elle n'est plus
utilisable. Le warm pool est une réserve d'**IP d'egress** pour ces six
stations, pas un moyen d'ajouter des stations.

Écarts constatés dans le code/config actuel :

| ID | Sév. | Écart | Preuve | Effet |
|---|---|---|---|---|
| F-01 | P0 | `egress_capacity.target_slots: 7` alors que `station_count: 6`. | `config.yaml:138-139,661` | La capacité est déclarée dégradée même à 6/6 ; le 7e slot est un objectif fantôme. |
| F-02 | P0 | Le canary WireGuard `vpn-wg-test` est une septième session NordVPN. | `docker-compose.yml:563-613` ; `vpn/manager.py:7325-7405` | Consomme une session rare et peut provoquer des AUTH_FAILED sur les six vraies stations. |
| F-03 | P1 | Le warm pool est décrit comme `station_count + 2` sans préciser que ce sont des IP. | `spec.md` initial ; `free/warm_pool.py` | Risque de confondre réserve d'IP et stations supplémentaires. |
| F-04 | P1 | Le superviseur ne vérifie pas explicitement que `sid <= station_count`. | `scripts/fresh_chain_supervisor.py:1346-1358` | Une config ou un état ancien peut faire réparer une station 7. |

Décisions opposables :
- `station_count = 6`, `target_slots = 6`, aucune station 7 dans
  `/api/pool-status`, dans le superviseur ou dans les tests.
- Le canary est supprimé comme session NordVPN ; un probe de santé sans session
  peut le remplacer.
- Le warm pool est un pool d'IP : `reserve(n)` réserve des IP, pas des stations.
- `six_of_six` reste le seul verdict de flotte ; WARP/proxys ne le modifient
  jamais.
- Toute rotation est **par station** : une station down déclenche sa propre
  rotation, sans attendre les cinq autres et sans créer de station 7.

