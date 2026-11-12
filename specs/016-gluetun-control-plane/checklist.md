# Checklist — qualité des exigences : plan de contrôle Gluetun

**Purpose** : vérifier que la spec 016 est complète, non ambiguë, testable et
alignée sur la constitution et les contraintes projet avant implémentation.
**Created** : 2026-11-11
**Feature** : [spec.md](./spec.md)

**Note** : checklist de revue des exigences. `[x]` signifie que le critère de
qualité a été revu et satisfait, pas que le code est écrit.

---

## Périmètre et frontière

- [ ] CHK001 Le périmètre distingue explicitement ce qui appartient au fork (tunnel, settings, DNS, preuve d'egress) et ce qui reste à `registry.db` (IP, lease, quota, score, SLO).
- [ ] CHK002 Le hors-périmètre exclut sans ambiguïté le payant, le second compte, l'attente, la 7e station et la 7e session.
- [ ] CHK003 Le make-before-break inter-stations est attribué à la feature 015, pas à 016.
- [ ] CHK004 L'absence de double tunnel dans un même conteneur est justifiée et reliée à un risque.
- [ ] CHK005 La spec ne demande jamais au fork de re-scorer ou de re-trier une liste de candidats.
- [ ] CHK006 Les dépendances (fork, upstream, `qdm12/dns/v2`, 015) sont nommées avec leur version ou leur commit.

## Sélection et cible

- [ ] CHK007 Les facettes de sélection (pays, région, ville, ISP, catégorie, tier, hostname, numéro, IP d'endpoint) sont énumérées.
- [ ] CHK008 La distinction `endpoint_ip` / `expected_egress_ip` est explicite dans la spec et le contrat.
- [ ] CHK009 La validation d'une cible contre le catalogue est obligatoire avant toute coupure.
- [ ] CHK010 Le comportement sur cible inconnue est défini (`422`, aucun tunnel coupé).
- [ ] CHK011 La validation d'appartenance d'une IP d'endpoint à un hostname est spécifiée.
- [ ] CHK012 L'historique `hostname → IPs de sortie` est consultatif, avec TTL et provenance, jamais un claim.

## Bascule et transaction

- [ ] CHK013 Les phases du SwitchJob sont ordonnées et chacune a une condition d'entrée/sortie.
- [ ] CHK014 Le préflight couvre credentials, résolution, appartenance IP, TCP/UDP et transport.
- [ ] CHK015 Un snapshot est pris avant application et persisté atomiquement.
- [ ] CHK016 Le rollback réapplique le snapshot et **re-vérifie** l'ancienne IP.
- [ ] CHK017 Le cas « rollback impossible » mène à `failed_degraded` terminal, sans boucle.
- [ ] CHK018 L'idempotence a une fenêtre, une règle de conflit et un comportement défini.
- [ ] CHK019 Un seul job actif par station ; le conflit renvoie `409` avec `active_job_id`.
- [ ] CHK020 `dry_run` ne coupe pas le tunnel et retourne un plan vérifiable.
- [ ] CHK021 Le drain est optionnel, borné et émet un événement en cas de timeout.
- [ ] CHK022 L'annulation ne laisse jamais le tunnel dans un état inconnu.

## Rotation

- [ ] CHK023 La rotation accepte une liste ordonnée **ou** une politique, pas les deux.
- [ ] CHK024 La liste reçue n'est ni re-scorée ni re-triée.
- [ ] CHK025 Les exclusions hostnames/IPs/sous-réseaux et `forbidden_ips` sont spécifiées.
- [ ] CHK026 `require_new_ip`, `require_new_subnet`, `expected_egress_ip` sont des portes dures.
- [ ] CHK027 Un budget (`max_candidates`, `max_attempts`, `timeout_s`) borne la rotation.
- [ ] CHK028 Un cooldown par hostname est appliqué après échec.
- [ ] CHK029 `exhausted` contient la raison de chaque candidat et ne désactive jamais la station.
- [ ] CHK030 Aucun état global « aucune IP disponible » n'est écrit, dans aucun cas.
- [ ] CHK031 La rotation ne crée ni station ni session supplémentaire.

## DNS

- [ ] CHK032 Le ladder est ordonné et configurable (`dot,doh,plain`), avec au moins deux étages si redondance exigée.
- [ ] CHK033 Chaque étage supporte plusieurs fournisseurs/adresses.
- [ ] CHK034 Le fallback ne se déclenche que sur timeout/SERVFAIL/REFUSED/erreur réseau, jamais sur NXDOMAIN.
- [ ] CHK035 TC déclenche un retry TCP sur le même étage avant fallback.
- [ ] CHK036 La santé active et passive par étage/endpoint est définie (seuils, cooldown, récupération).
- [ ] CHK037 `fail_closed=true` interdit les résolveurs de l'hôte et retourne SERVFAIL.
- [ ] CHK038 Le cache, le filtre et le DNS local restent au niveau du listener, pas dupliqués par étage.
- [ ] CHK039 Le défaut flotte `dot,plain` est justifié par une mesure (DoH bloqué).
- [ ] CHK040 L'état DNS est exposé (`/state`, `/tiers`) et une sonde active existe.

## Vérification d'egress

- [ ] CHK041 Au moins trois sources indépendantes sont exigées par défaut.
- [ ] CHK042 Le consensus porte sur la valeur exacte d'IP, pas sur la géolocalisation.
- [ ] CHK043 k-of-n, dissidents, confiance et tie-break sont définis.
- [ ] CHK044 L'IP d'hôte est capturée au boot et rejetée en tant que preuve.
- [ ] CHK045 Les plages privées/loopback/link-local/CGNAT/multicast/unspecified/documentation sont rejetées.
- [ ] CHK046 Le canary exige 200 + corps non vide (+ hash optionnel) et un 200 vide échoue.
- [ ] CHK047 Une vérification non prouvée n'est jamais convertie en succès.
- [ ] CHK048 Le cache a un TTL, un single-flight et une option `fresh`.
- [ ] CHK049 Les sondes n'appellent jamais l'API modèle et ne consomment pas le quota.
- [ ] CHK050 La provenance, la latence et la santé de chaque source sont exposées.

## État, événements, sécurité

- [ ] CHK051 L'état désiré est versionné, atomique, avec checksum et mode 0600.
- [ ] CHK052 Les modes `state|env|merge` et le cas `state.invalid` sont spécifiés.
- [ ] CHK053 Les secrets ne sont pas persistés en clair ; le chiffrement conditionnel est défini.
- [ ] CHK054 `GET /v1/vpn/settings` masque les secrets par défaut ; `reveal` exige admin.
- [ ] CHK055 Les scopes `reader/controller/admin` couvrent toutes les routes nouvelles.
- [ ] CHK056 Les jobs et événements sont append-only, bornés, avec rétention.
- [ ] CHK057 SSE supporte `Last-Event-ID`, `seq` monotone et heartbeat.
- [ ] CHK058 Aucun secret, clé ou token n'est journalisé.
- [ ] CHK059 Les limites de corps, de taux et de timeout sont définies.

## Ladder de transports

- [ ] CHK089 Le ladder est ordonné et configurable (`wireguard,openvpn-udp,openvpn-tcp`) avec `VPN_TYPE` explicite prioritaire.
- [ ] CHK090 Le premier transport qui fournit tunnel + preuve d'egress est committé ; l'échec passe au suivant.
- [ ] CHK091 Un seul tunnel actif : teardown explicite, verrou de boucle, aucun double namespace.
- [ ] CHK092 Après un cycle complet, retour à `policy[0]` avec backoff exponentiel et jitter.
- [ ] CHK093 Un breaker par transport (`closed/open/half_open`) avec seuil, cooldown et sonde.
- [ ] CHK094 Les erreurs sont typées (`auth`, `udp_blocked`, `tcp_refused`, `tls`, `handshake_timeout`, `no_egress`, `interface_down`).
- [ ] CHK095 `auth` ne boucle jamais indéfiniment ; escalation `tunnel.repair_needed` après budget.
- [ ] CHK096 Le ladder ne change jamais de serveur, d'IP ou de hostname.
- [ ] CHK097 Le watchdog même-serveur distingue tunnel mort et transport bloqué, et retente avant le ladder.
- [ ] CHK098 Firewall/kill switch et MTU/PMTUD réappliqués avant de déclarer un transport `up`.
- [ ] CHK099 `GET /v1/vpn/transport` et `POST /v1/vpn/transport/reset` sont spécifiés avec scopes.
- [ ] CHK100 Les événements/métriques par transport existent, sans secret.
- [ ] CHK101 Le ladder Python 015 passe en shadow puis est décommissionné, sans double pilotage.

## Compatibilité et intégration

- [ ] CHK060 Les routes upstream gardent leur comportement ; les nouveautés sont additives.
- [ ] CHK061 `PUT /v1/vpn/settings` reste compatible et le routage conditionnel est clair.
- [ ] CHK062 Le client Python n'utilise ni `docker exec`, ni `docker cp`, ni parsing texte dans le chemin chaud.
- [ ] CHK063 Le mode `legacy|shadow|active` est défini avec ses critères de passage.
- [ ] CHK064 Le shadow ne coupe jamais le tunnel et ne crée aucun claim.
- [ ] CHK065 Les événements sont ingérés de façon idempotente dans `registry.db`.
- [ ] CHK066 Le fallback legacy est déclenché uniquement sur 5xx/timeout, jamais sur 422/403/409 métier.
- [ ] CHK067 Un timeout côté client ne crée pas un second job (idempotence).
- [ ] CHK068 Les critères de décommissionnement du chemin chaud sont mesurables (≥ 95 %).

## Performance et fiabilité

- [ ] CHK069 Les cibles p95/p99 sont chiffrées pour l'état, la bascule, le DNS, l'egress et les transports.
- [ ] CHK070 Aucun job ne peut rester `applying` après un crash (SC-001).
- [ ] CHK071 Toute écriture critique est atomique et un crash laisse un journal rejouable.
- [ ] CHK072 Les transitions sont idempotentes et les états terminaux explicites.
- [ ] CHK073 Les six stations restent `client_usable` pendant le rollout (SC-007).
- [ ] CHK074 Aucune régression des routes upstream n'est acceptée (SC-010).

## Tests et recette

- [ ] CHK075 Chaque user story a un test indépendant décrit.
- [ ] CHK076 Les cas de chaos sont listés (source menteuse, IP d'hôte, corps vide, DNS bloqué, state corrompu, rollback impossible).
- [ ] CHK077 Les tests de contrat couvrent les codes 400/401/403/404/409/422/429/500.
- [ ] CHK078 La parité shadow est mesurée sur 100 bascules.
- [ ] CHK079 Un soak 24 h est prévu avec des critères de sortie.
- [ ] CHK080 La recette inclut la non-régression 015 et la mesure `client_usable`.
- [ ] CHK081 Le runbook opérateur couvre switch/rotate/rollback/DNS/egress/incidents.

## Documentation et décisions

- [ ] CHK082 Les décisions D1–D13 sont documentées avec alternative rejetée.
- [ ] CHK083 Chaque variable `CONTROL_*`/`DNS_*`/`TRANSPORT_*` a un consommateur, un défaut et un test.
- [ ] CHK084 Les limites et capacités sont exposées par `GET /v1/control/capabilities`.
- [ ] CHK085 Les risques R-01..R-12 ont une mitigation et un propriétaire.
- [ ] CHK086 Le plan de rollback (digest, state mode, flag Python) est explicite.
- [ ] CHK087 Les références upstream (`internal/dns`, `publicip`, `state/vpn.go`) sont exactes.
- [ ] CHK088 Aucune exigence ne contredit la constitution I–XVII.

## Notes

- Les cases restent décochées tant que la revue n'a pas confirmé le critère.
- Les findings de revue peuvent être ajoutés en ligne sous l'item concerné.
- `checklists/requirements.md` garde son cycle de vie séparé (specify/clarify).
