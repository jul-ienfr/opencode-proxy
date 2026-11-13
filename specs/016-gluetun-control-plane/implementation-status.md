# Statut d'implémentation — 016

**Date** : 2026-11-13
**Dépôt** : `gluetun/` (fork `jul-ienfr/gluetun`), branche `016-control-plane`
**Baseline** : `8d925146ab36fdc538bcb35b3427518fd478e273`, identique à
`passteque/gluetun@master`.

## Phase 0 — fait

| Tâche | Statut | Preuve |
|---|---|---|
| T001 — baseline gelée | fait | `git diff upstream/master` vide ; branche créée ; commit documenté dans `docs/adr-control-plane.md`. |
| T002 — flags et réglages | fait | `internal/configuration/settings/control.go`, branché dans `Settings` (read/defaults/copy/override/validate/String). |
| T006 — squelette API | fait | `internal/control/api/{router,errors}.go` ; toutes les routes répondent `501 not_implemented`, `capabilities` répond `200`. |
| T009 — validation | fait | `Control.validate` + tests : quorum > sources, ladder à un étage, mode d'état, timeouts, IP d'hôte. |
| T010 — ADR et capabilities | fait | `docs/adr-control-plane.md`, `GET /v1/control/capabilities` reflète les réglages effectifs. |
| T003 — contrat upstream | fait | `internal/server/upstream_contract_test.go` : `/v1/version`, status VPN/DNS, settings, public IP, portforward, montage conditionnel du plan de contrôle. |
| T007 — scopes | fait | `internal/control/auth/scopes.go` : `reader`/`controller`/`admin`, table route→scope, `403 forbidden_scope`, journalisation sans secret ; tests de middleware. |
| T008 — observabilité | fait | `internal/control/observability/{scrub,log,metrics}.go` : scrub récursif (Bearer, `api_key`, userinfo d'URL), logger JSON `job_id`/`event`, compteurs et histogrammes de latence, export Prometheus ; `scrub_test.go`, `log_test.go`, `metrics_test.go`. |
| T004 — harnais réseau local | fait | `tests/integration/harness` (binaire Go `dns`, `echo`, `wg`, `check`), `docker-compose.yml` (plain, DoT, trois échos, écho défaillant, `vpn-wg-test`), `Dockerfile` ; `up --wait` puis `run --rm check` : DNS plain/DoT, SERVFAIL, trois échos, configuration WireGuard, tunnel réel via `10.66.0.1:8080` et écho WG. |
| T005 — CI | fait | `.github/workflows/control-plane-ci.yml` : verify, tests Go en conteneur `NET_ADMIN`/TUN, golangci-lint v2.14.0, job harnais Compose, buildx `linux/amd64,linux/arm64`, push GHCR, scan Trivy, artefact et résumé de digest. Workflow séparé pour préserver les rebases upstream. |

## Phase 1 — Egress verification (fait)

| Tâche | Statut | Preuve |
|---|---|---|
| T020 — interface Source et source HTTP | fait | `internal/egress/source/{source.go,build.go,http.go}` : `kind:name` (`ipify`, `ifconfig`, `icanhazip`), timeout strict, parse JSON/texte, erreurs typées (`ErrTimeout`, `ErrHTTPStatus`, `ErrMalformed`, `ErrEmpty`, `ErrBanned`) ; `http_test.go`. |
| T021 — sources DNS et publicip | fait | `internal/egress/source/{dns.go,publicip.go}` : whoami/OpenDNS TXT via resolver, `publicip` interne ; `publicip:internal` ne suffit jamais seule au consensus ; `dns_test.go`. |
| T022 — capture de l'IP d'hôte | fait | `internal/egress/source/host.go` : `HostIPs(auto, configured)` au boot (v4/v6), rejet `host_ip` ; `host_test.go`. |
| T023 — règles de rejet | fait | `internal/egress/consensus/reject.go` : private, loopback, link-local, CGNAT, multicast, unspecified, documentation (sauf `allow_test_nets`), host, forbidden, malformed, wrong_family ; table exhaustive dans `reject_test.go`. |
| T024 — consensus k-of-n | fait | `internal/egress/consensus/consensus.go` : groupement par IP exacte, quorum, `min_sources`, dissidents, tie-break DNS puis IP la plus petite, confiance, familles séparées ; `consensus_test.go`. |
| T025 — canary HTTP | fait | `internal/egress/canary/canary.go` : statut exact, corps minimal, SHA-256 optionnel, timeout, redirections non suivies, `checked_at` ; `canary_test.go`. |
| T026 — cache et single-flight | fait | `internal/egress/verify/{service.go,cache.go}` : TTL (`Verification.TTLS`), `fresh`, single-flight, backoff par source, timeout global `ErrTimeout`, historique borné ; `service_test.go`. |
| T027 — routes egress | fait | `internal/control/api/{egress.go,interfaces.go}` et `internal/server/egress.go` : `GET /v1/egress` (cache), `/verify` (frais par défaut, `fresh`/`quorum`/`min_sources`/`canary`/`timeout_s`), `/canary`, `/history` (`limit`/`since`) ; `422 insufficient_sources`, `503 not_ready` + `retry_after_ms`, `400 invalid_request` (egress ou canary désactivé) ; `egress_consensus=true` dans `capabilities` ; tests `internal/control/api/egress_test.go`. |
| T028 — métriques egress | fait | `internal/control/observability/metrics.go` : `gluetun_egress_verifications_total{result}`, `..._source_requests_total{source,result}`, `..._source_latency_seconds{source}`, `..._source_banned{source}`, `..._quorum`, `..._dissenters`, `..._canary_ok`, `..._cache_hits_total` ; `metrics_test.go`. |
| T029 — chaos egress | fait | `tests/integration/egress_chaos_test.go` : EG-AT-01..08 plus source lente ; aucune source menteuse ni IP d'hôte ne produit de faux `verified`. |

### Détails de la phase 1

- Les sources sont construites dans `internal/server/egress.go` avec un client
  HTTP dédié à timeout fixe, `net.DefaultResolver`, le provider `publicip` du
  fork et les IP d'hôte capturées ; le service est `nil` quand
  `CONTROL_EGRESS_ENABLED=false`, et l'API répond alors
  `400 invalid_request` sans paniquer.
- `GET /v1/egress` sert le cache sauf `?fresh=true` ; `GET /v1/egress/verify`
  collecte fraîchement par défaut.
- Le canary n'est jamais exécuté depuis l'hôte : il passe par le client du
  conteneur, donc par le tunnel dès qu'il est monté.
- Le cache n'est jamais réutilisé pour un futur `committed` de job : les
  endpoints de job exigeront `fresh=true`.
- `ObserveEgressVerification` alimente le registre de métriques ; son montage
  HTTP suivra le même chemin que les métriques de routes de T008.

## Phase 3 — État, reprise, préflight, jobs (fait)

| Tâche | Statut | Preuve |
|---|---|---|
| T030 — schéma DesiredState | fait | `internal/control/state/desired.go` : schéma v1, `TunnelSettings` non secret (références `env:NAME`), checksum SHA-256 du JSON canonique, validation (schéma, station, mode, settings, snapshots, anneau d'observations), `Seal`, `Equal`, `mergedWith` ; `desired_test.go`. |
| T031 — écriture atomique et journal | fait | `internal/control/state/{atomic.go,journal.go,store.go}` : `tmp` + `Sync` + `rename`, mode 0600, `syncDirectory` tolérant Windows, journal append-only avec fsync, rotation par taille (`maxBytes`) et rétention (`maxFiles`), `Store.Save`/`Load` (`state.json`, `state.journal.jsonl`) ; `atomic_test.go`, `journal_test.go`, `store_test.go` (dont échec d'écriture et ancien état conservé). |
| T032 — snapshot et reprise | fait | Types de snapshot dans `desired.go` (`TunnelSnapshot`, `DNSState`, `ResolverTier`, `TransportDetail`) et `recovery.go` : `Recover` modes `state\|env\|merge`, décisions `state.absent\|recovered\|drift\|merged\|invalid`, état invalide jamais réécrit, station étrangère traitée en `drift` ; `recovery_test.go`. L'exécuteur capture désormais le snapshot avant chaque switch et `Rollback` le restaure. |
| T033 — préflight | fait | `internal/control/jobs/preflight.go` : interfaces étroites `Catalog`/`Resolver`/`Prober`, validation de la cible et du transport, credentials `OpenVPN`/`WireGuard`, protocoles du catalogue, résolution DNS, appartenance de l'`endpoint_ip` (résolution ∪ catalogue), sonde réseau ; erreurs typées `invalid_target`, `unknown_target`, `endpoint_ip_mismatch`, `transport_unsupported`, `missing_credentials`, `resolution_failed`, `endpoint_unreachable` ; timeout 10 s par défaut ; `preflight_test.go`. |
| T034 — machine à états | fait | `internal/control/jobs/{job.go,machine.go}` : `Job`/`Attempt`/`Timings` conformes au data model, 15 états, transitions exhaustives, invariants `committed`/`rolled_back` prouvés par une vérification d'egress, `Cancel` immédiat avant `applying` puis `cancel_requested`, `Interrupt` au redémarrage, timings par phase, `SetVerification` ; `machine_test.go` (transitions valides/invalides, invariants, annulation, reprise). |
| T035 — idempotence | fait | `internal/control/jobs/idempotency.go` : fenêtre 24 h par défaut, SHA-256 hexadécimal du corps, clé ≤ 128 caractères, `Reserve` atomique (même clé + même corps → job existant, corps différent → `ErrIdempotencyConflict`), purge à chaque accès, `Lookup`, `Forget` ; `idempotency_test.go` (rejeu, conflit, expiration, concurrence). |
| T036 — drain borné | fait | `internal/control/jobs/drain.go` : polling du compteur de connexions proxy, intervalle borné (timeout/20, 1 ms à 100 ms), attente plafonnée au deadline, `ErrDrainTimeout` + constante d'événement `drain.timeout`, annulation par contexte ; `drain_test.go` (fermeture, timeout exact, annulation, horloge injectée). |
| T037 — rollback et failed_degraded | fait | `internal/control/jobs/rollback.go` : ré-application du snapshot avant, attente du tunnel, une seule vérification egress fraîche, égalité stricte avec l'IP prouvée du snapshot, terminal `rolled_back` avec `Verification` enregistrée ou `failed_degraded` avec événement critique, aucune boucle ; `rollback_test.go` (succès depuis applying/awaiting/verifying/rolling_back, échec apply/await/verify, IP absente ou différente, snapshot absent, état de départ invalide, contexte annulé). |
| T038 — routes switch/jobs | fait | `internal/control/api/{switch,jobs,rollback,rotate}.go` et `internal/server/control.go` : `POST /v1/vpn/switch` (`202` + `Location`, `200` si `wait=true`), `GET /v1/jobs/{id}`, annulation, rotation et rollback ; jobs détachés du contexte HTTP ; `jobs.Manager` réel construit avec `Executor`, `Preflight`, catalogue et vérificateur egress ; `switch_test.go`, `rotate_test.go`. |
| T039 — compatibilité `PUT /v1/vpn/settings` | fait | `internal/control/api/settings_compat.go` : détection du corps contrôlé, validation synchrone (`422 invalid_target` pour policy/cible invalide), délégation au chemin upstream pour les réglages legacy ; `settings_compat_test.go`, `internal/server/settings_control_test.go`. |

### Détails de la phase 3

- `Store.Save` scelle le state avant écriture : un state invalide est refusé
  avant toute modification du fichier précédent.
- `Store.Recover` journalise chaque décision ; le state invalide est journalisé
  `state.invalid` sans checksum et le fichier d'origine reste intact.
- `Recover` prend `stationID` en argument : `Store` ne connaît pas l'identité
  de la station.
- La validation d'une IP d'endpoint réutilise les rejets durs de
  `internal/egress/consensus` avec `AllowTestNets=true` (INV-08 : private,
  loopback, link-local, CGNAT, multicast, unspecified refusés ; les plages de
  documentation restent acceptées pour les snapshots de test).
- `snapshot.go` n'a pas été créé : les types de snapshot sont définis dans
  `desired.go` pour garder le schéma et sa validation dans un seul fichier.
- Le préflight ne coupe jamais le tunnel : il valide d'abord la cible, les
  credentials et le catalogue, puis résout et sonde l'endpoint. Le catalogue
  réel (T050) et le câblage dans le SwitchJob (T034/T038) sont faits ; les
  interfaces `Catalog`/`Resolver`/`Prober` isolent ces dépendances.
- Sonde réseau : `openvpn-tcp` fait un dial TCP réel ; `wireguard` et
  `openvpn-udp` vérifient la disponibilité d'une socket UDP vers l'endpoint.
  Sonder en TCP le port UDP d'OpenVPN produirait un faux négatif ; le handshake
  du tunnel reste la preuve réelle pour UDP.
- La machine à états est volontairement pure : `Transition` ne fait aucune I/O et
  refuse `committed`/`rolled_back` sans `Verification.Verified`. `Rollback` est
  la seule brique qui pilote `rolling_back` → `rolled_back|failed_degraded` et
  elle n'essaie jamais deux fois.
- L'idempotence est un index en mémoire, borné par la fenêtre de 24 h et purgé à
  chaque `Reserve`/`Lookup`. La persistance de l'index suivra l'index de jobs du
  `DesiredState` (T038) ; le fork ne réutilise jamais un job d'une autre station.
- Le drain est optionnel et désactivé par défaut (`drain.enabled=false`). Il
  reçoit un compteur de connexions actives injecté et ne bloque jamais au-delà
  du timeout : le dernier sommeil est tronqué au temps restant.
- `Rollback` exige une IP de sortie prouvée dans le snapshot avant de toucher au
  tunnel ; sans elle, le job passe directement en `failed_degraded` plutôt que
  d'appliquer une cible non prouvable.
- Les événements `drain.timeout`, `job.rolled_back` et `job.failed_degraded` sont
  exposés comme constantes et publiés sur le bus (T060/T061) ainsi que dans le
  journal `events.jsonl` (T062).

## Phase 2 — DNS ladder (fait, sauf T049)

| Tâche | Statut | Preuve |
|---|---|---|
| T040 — modèle Tier/Endpoint | fait | `internal/dns/ladder/{tier,endpoint,state}.go` : types de tier `dot\|doh\|plain`, endpoints avec adresse, provider et latence EWMA, disponibilité par endpoint et par tier ; `tier_test.go`. |
| T041 — algorithme de résolution | fait | `internal/dns/ladder/{ladder,exchange}.go` : essai des tiers dans l'ordre, timeout et nombre de tentatives par étage, bascule d'endpoint dans un même tier, cache et single-flight conservés par le middleware ; `ladder_test.go`. |
| T042 — santé active/passive | fait | `internal/dns/ladder/health.go` : `FailuresToDown=3`, `SuccessesToRecover=2`, cooldown 5 s doublé jusqu'à 5 min, tier up si au moins un endpoint est up ; `health_test.go`. |
| T043 — intégration `internal/dns` | fait | `internal/dns/ladder_middleware.go` : le ladder est le middleware le plus interne, un seul listener, cache/filter/localdns/fail-closed conservés ; `ladder_integration_test.go`. |
| T044 — configuration et défauts | fait | `settings.Control.DNS` : `LADDER` défaut `dot,plain`, `fail_closed=true`, timeout 800 ms, 2 tentatives, providers par étage, sondes 30 s, domaine `example.com` ; `control_test.go`. |
| T045 — routes DNS | fait | `internal/control/api/dns.go` et `internal/server/dns.go` : `GET /v1/dns` (snapshot), `POST /v1/dns/probe`, `GET /v1/dns/tiers`, `422 unknown_tier` ; `dns_test.go`. |
| T046 — métriques et événements DNS | fait | Compteurs de tentatives/échecs par tier et endpoint dans `internal/dns/ladder`, événements de bascule/remontée exposés par le snapshot ; `state.go`, `snapshot_test.go`. |
| T047 — fail-closed | fait | `internal/dns/ladder_middleware.go` : ladder épuisé + `fail_closed=true` ⇒ SERVFAIL sans fuite ; `fail_closed=false` ⇒ délégation au handler upstream ; `ladder_integration_test.go`. |
| T048 — NXDOMAIN et TC | fait | `internal/dns/ladder/{ladder,exchange}.go` : NXDOMAIN terminal, réponse tronquée rejouée en TCP, retour à `dot` après récupération ; `ladder_test.go`. |
| T049 — mesure live DoH bloqué | script prêt, mesure à faire | `gluetun/tests/integration/dns_live_block.sh` bloque TCP/853 et les DoH connus, mesure le p95 (seuil 2 s) et restaure les règles via `trap`. Syntaxe vérifiée (`sh -n`) ; l'exécution demande une station réellement filtrée, absente du banc local. |

## Phase 2b — Ladder de transports (fait, sauf validation tunnel réel T099)

| Tâche | Statut | Preuve |
|---|---|---|
| T090 — Policy/Attempt/State/Breaker | fait | `internal/vpn/transport/{policy,state,breaker}.go` : ladder normalisée (`wireguard,openvpn-udp,openvpn-tcp`), rejet ladder vide/inconnu, déduplication avec warnings, breaker `closed/open/half_open` ; `policy_test.go`, `breaker_test.go`. |
| T091 — boucle du ladder | fait | `internal/vpn/transport/ladder.go` : preflight → teardown → apply → tunnel → egress → commit/échec → suivant, un seul `Run` (`ErrAlreadyRunning`), `teardown_current` explicite, un seul tunnel actif ; `ladder_test.go`. |
| T092 — preflight et erreurs typées | fait | `internal/vpn/transport/{preflight,errors}.go` : résolution, dial TCP/UDP, credentials, mapping `auth`, `udp_blocked`, `tcp_refused`, `tls`, `handshake_timeout`, `no_egress`, `interface_down`, `config_invalid` ; `errors_test.go`, `preflight_test.go`. |
| T093 — breaker et backoff | fait | `internal/vpn/transport/breaker.go` et `ladder.go` : seuils, cooldown doublé/plafonné, backoff de cycle avec jitter, `max_cycles` ; `breaker_test.go`. |
| T094 — watchdog même-serveur | fait | `internal/vpn/transport/watchdog.go` : détection interface/handshake/statut, intervalle 1 s, stale 30 s, 3 reconnexions maximum puis `tunnel.repair_needed` ; `watchdog_test.go`. |
| T095 — intégration boucle VPN, firewall, MTU | fait | `internal/vpn/{ladder,transport_settings,run,tunnelup,cleanup}.go` branche le ladder dans la boucle : activation seulement si `TRANSPORT_ENABLED`, type non explicite, type ≠ `amneziawg`, pas d'`OpenVPN.ConfFile` et hostname pinné ; sinon upstream. Le driver applique firewall/MTU puis `VerifyEgress` avant `up`, le watchdog répare sur place puis escalade au transport suivant, et `applyTunnelUp` réutilise le chemin `onTunnelUp` avec `prepared=true`. `integration_test.go`, `transport_test.go`. |
| T096 — endpoints transport | fait, attaché | `internal/control/api/transport.go` : `GET /v1/vpn/transport`, `POST /v1/vpn/transport/reset` (admin), reset sans transport = `all`, transport inconnu ⇒ `422 invalid_target` ; `transport_test.go`. `newControlHandler` appelle `SetTransport` quand le service existe, donc `capabilities.transport_ladder` reflète l'état réel. |
| T097 — événements et métriques transport | fait | `internal/vpn/transport/metrics.go` : compteurs par transport, breaker, cycles, latence, exportés sur l'endpoint Prometheus global via `metricsprometheus.NewTextGatherer(vpnLooper.TransportMetrics().WritePrometheus)` ; `internal/control/events/transport.go` mappe chaque événement ladder sur le bus et `events.jsonl`, avec sévérité warning pour `failed`/`breaker_open`/`cycle_exhausted`/`repair_needed` ; `metrics_test.go`, `text_test.go`, `setup_test.go`. |
| T098 — `transport_policy` switch/rotate | fait | `internal/control/jobs/{switch.go,executor.go,rotation.go}` et `internal/control/api/switch.go` : `auto`, transport unique ou liste ordonnée, validation synchrone, policy restreinte sans changer la cible serveur/IP, policy invalide ⇒ `422 invalid_target` ; `transport_policy_test.go`. |
| T099 — tests ladder et shadow Python | partiel (live restant) | `tests/integration/transport_ladder_test.go` couvre UDP bloqué → TCP, TCP refusé, auth invalide, tunnel mort, second cycle, reset breaker ; `internal/vpn/transport/ladder_test.go`, `integration_test.go` et `internal/vpn/ladder.go` couvrent le reste. Parité shadow Python : `TransportShadowObserver` lit `GET /v1/vpn/transport` (aucun POST, aucune policy, aucun `PUT /v1/vpn/settings`) et écrit `control_plane_transport_shadow` dans `telemetry.db` ; `tests/test_transport_ladder_shadow.py` (4 cas). `TR-AT-08`/`TR-AT-09` demandent encore un tunnel réel (bascule et réparation observées). |

### Mise en œuvre T095

Le ladder runtime est construit dans `cmd/gluetun/main.go` après le service
egress partagé : `SetEgressVerifier` puis `SetTransportLadder`. La boucle
`internal/vpn/run.go` choisit le chemin ladder par `shouldRunLadder` ; le
driver `internal/vpn/ladder.go` pinne le hostname/IP de `ServerSelection`,
applique le transport, attend le tunnel, réapplique firewall/MTU et ne déclare
`up` qu'après `VerifyEgress`. Le même driver fournit `Health`/`Reconnect` au
watchdog, qui répare sur le même serveur avant de rendre `ErrRepairNeeded` ;
`Integration.Run` rapporte alors l'échec au breaker et relance le transport
suivant.

Le comportement upstream reste intact dans les cas suivants, documentés par
`shouldRunLadder` :

- `TRANSPORT_ENABLED=false` ou ladder non construit ;
- `VPN_TYPE` explicitement fourni (toutes les stations du compose actuel) ;
- type `amneziawg` ou `OpenVPN.ConfFile` personnalisé ;
- aucun hostname pinné.

La policy du ladder runtime vient de `TRANSPORT_*` ; `transport_policy` d'un
job continue de piloter la cible du job (T098), pas le choix interne du
transport. Les tests d'intégration avec un vrai tunnel (kill, MTU réduit,
firewall) restent à exécuter sur station : ils ne peuvent pas l'être sur le
banc local sans interface VPN.

## Phase 4 — Catalogue et rotation (fait, sauf mesure live T049)

| Tâche | Statut | Preuve |
|---|---|---|
| T050 — lecture catalogue et facettes | fait | `internal/control/catalog/{api,query,facets}.go` : catalogue sur `Storage.GetServers`, facettes sur tout le catalogue, pagination curseur opaque, `limit` défaut 50/max 500 ; `query_test.go`, `facets_test.go`. |
| T051 — détail serveur et stats | fait | `internal/control/catalog/{detail,stats}.go` et `GET /v1/servers/{hostname}` ; `detail_test.go`. |
| T052 — validation d'IP d'endpoint | fait | `internal/control/catalog/validate.go` : appartenance au catalogue/résolution, rejets durs d'egress, `allow_test_nets` pour les tests ; `validate_test.go`. |
| T053 — route rotation | fait | `POST /v1/vpn/rotate` : `candidates` ordonnés jamais re-triés, `policy` résolue dans l'ordre du catalogue, `candidates` + `policy` ⇒ `422 invalid_target`, `dry_run`/`supersede` refusés ; `rotate_test.go`. |
| T054 — exclusions et gates | fait | `internal/control/jobs/{gates,executor}.go` : `RequireNewIP`, `RequireNewSubnet`, `ExcludeIPs`, `ExcludeSubnets`, `ForbiddenIPs` ; `gates_test.go`. |
| T055 — cooldown et exhausted | fait | `internal/control/catalog/cooldown.go` et `internal/control/jobs/rotation.go` : base 30 s doublée/plafonnée à 5 min par hostname, succès efface l'état, candidats en cooldown `skipped`, épuisement ⇒ `station_preserved=true`, `global_state_written=false` ; `exhausted_test.go`. |
| T056 — refresh catalogue | fait | `POST /v1/servers/refresh`, rate limit 1 min, `ErrRefreshUnsupported` ⇒ `501` ; `servers.go`. |
| T057 — tests rotation 3 candidats | fait | `tests/integration/rotation_test.go` : trois candidats puis trois échecs ; `internal/control/jobs/rotation_test.go`. |

## Phase 5 - Événements, sécurité, observabilité (fait)

| Tâche | Statut | Preuve |
|---|---|---|
| T060 — bus et séquence | fait | `internal/control/events/{bus,seq}.go` : séquence monotone, buffer de replay borné, drop policy « newest dropped + gap signalé », `Recent` avec `since` inclusif, `Subscribe`/SSE avec `Last-Event-ID` exclusif ; `bus_test.go`. |
| T061 — SSE | fait | `GET /v1/events` (`since`, `type`, `limit` défaut 100/max 500) et `GET /v1/events/stream` (heartbeat 15 s, `Last-Event-ID`) ; `events.go`, `sse_test.go`. |
| T062 — store et rétention | fait | `internal/control/events/store.go` : JSONL append-only, checksum SHA-256 par enregistrement, rotation `events.jsonl` → `events.N.jsonl`, budget 1 MiB × 3, `Load(limit)` ignore les lignes corrompues et restaure le dernier seq ; `store_test.go`. |
| T063 — masquage des secrets | fait | `GET /v1/vpn/settings` masqué par défaut, `reveal=true` réservé au scope admin, `MaskedVPNSettings` via `observability.ScrubMap` ; `secrets_test.go`. |
| T064 — scopes complets | fait | `internal/control/auth/scopes.go` et middleware : `reader`/`controller`/`admin`, table route→scope, `403 forbidden_scope`, journalisation sans secret ; tests de middleware. |
| T065 — limites de corps et rate | fait | corps max 1 MiB ⇒ `413 payload_too_large`, `/vpn/switch` et `/vpn/rotate` partagent 6/min ⇒ `429 rate_limited` + `Retry-After` ; `limits_test.go`. |
| T066 — capabilities effectives | fait | `Handler.capabilities()` : `Switch/Rotate/Rollback/DryRun/Drain` selon `h.jobs`, `DNSLadder` selon `h.dns`, `TransportLadder` selon `h.transport`, `EgressConsensus` selon `h.egress`, `EventsSSE` selon le bus, `StatePersistence` selon `StateDir` ; `capabilities_test.go`. |
| T067 — métriques de jobs | fait | `internal/control/observability/metrics.go` : `gluetun_control_jobs_total{kind,state}`, `..._job_duration_seconds_{count,sum,min,max}{kind}`, `..._job_phase_duration_seconds_{total,count}{phase}`, `..._job_attempts_total{kind,result}` ; labels bornés (`other` pour toute valeur inconnue), aucun hostname/job_id. Alimenté par `eventSink` sur les événements terminaux (`committed`, `rejected`, `cancelled`, `rolled_back`, `failed_degraded`, `switch.noop`) et exporté sur l'endpoint Prometheus global via `metricsprometheus.NewTextGatherer(controlMetrics.WritePrometheus)` ; `jobs_metrics_test.go`, `internal/server/control_metrics_test.go`, `internal/metrics/prometheus/text_test.go`. |
| T068 — scrub des logs | fait | `internal/control/observability/{scrub,log}.go` : scrub récursif (Bearer, `api_key`, userinfo), logger JSON `job_id`/`event` sans secret ; `scrub_test.go`, `log_test.go`. |
| T069 — tests de sécurité | fait | `tests/contract/security_test.go` démarre le serveur HTTP réel (auth TOML, état temporaire) et couvre : 401 sans clé, 403 `forbidden_scope` pour `reader` sur `POST /v1/vpn/switch`, `reveal=true` réservé admin, masquage de tous les secrets par défaut, corps > 1 MiB ⇒ `413 payload_too_large` sans toucher au tunnel, scrub des clés API/tokens dans les logs d'accès, scrub des événements avant SSE et `events.jsonl`, mode `0600` du fichier d'état et `0700` du répertoire, et erreurs qui ne reflètent jamais les valeurs secrètes. Le routage `/v1` retire désormais la query avant dispatch (`/v1/vpn/settings?reveal=true`) et le handler settings renvoie `application/json`. |

### `/v1/vpn/state` et câblage runtime (fait)

- `GET /v1/vpn/state` est servi depuis la mémoire par `api.StateService` :
  `state.TunnelSnapshot` (`Transport` + `TransportDetail`), `DesiredState`,
  `LastCommitted`, job actif et dernier seq d'événements. Aucune lecture disque
  par requête ; le cache `catalog.Get` fournit country/city.
- `internal/server/control.go` construit le vrai `jobs.Manager` (préflight,
  catalogue, exécuteur, cooldown, vérificateur egress, bus, journal) et le
  passe à `api.New`. Les jobs sont détachés du contexte HTTP.
- `tunnelAdapter.ApplySnapshot` appelle `SetSettings` depuis la goroutine du job
  (jamais depuis `vpn.Loop`) puis vérifie que le statut est `running` ou
  `starting` ; `snapshotApplied` compare type, protocole OpenVPN, hostname et
  endpoint IP.
- `controlRuntimeState` persiste `DesiredState`, `Current`, `LastCommitted`,
  `Jobs.Recent` et `Events.LastSeq` ; `jobs.Manager` restaure `LastCommitted`
  au démarrage et appelle `OnCommit(job, snapshot)` avant de fermer `done`.
- `eventSink` publie sur le bus et dans `events.FileLog`, avec sévérité warn
  pour rejet, annulation, rollback, `failed_degraded` et drain timeout.

## Phase 6 — Intégration orchestrateur (T070–T079 faits, T078/T079 outillés)

| Tâche | Statut | Preuve |
|---|---|---|
| T070 — client Python | fait | `free/control_plane/{client,models,errors}.py` : méthodes du contrat (`capabilities`, `state`, `switch`, `rotate`, `rollback`, `job`, `cancel`, `egress`, `dns_state`, `transport_state`, `transport_reset`, `events`), dataclasses typées, retries bornés (`429/502/503/504` + connexion/timeout), `Idempotency-Key` obligatoire sur switch/rotate, erreurs `ActiveJob`/`UnknownTarget`/`VerificationFailed`/`Exhausted`/`ControlPlaneUnavailable`, aucun `docker exec`. Adaptateur `free/ip_registry/adapters/gluetun_control_v2.py` (binder async, clé `action-station-cycle_id`, aucun re-score) ; `tests/test_gluetun_control_v2.py` (24 cas, faux serveur `httpx.MockTransport`). |
| T071 — configuration Python | fait | `free/control_plane/config.py` : `mode` (`legacy|shadow|active`), `base_url` avec `{station}`, `api_key_env` résolue à l'appel et jamais rendue, timeouts, retry, shadow, events, `transport_owner` (`python|fork`) ; `config.yaml` porte le bloc `control_plane` (mode `legacy` par défaut) ; `tests/test_control_plane_config.py`. |
| T072 — shadow mode | fait | `free/control_plane/{shadow,telemetry}.py` : `ShadowObserver` n'émet que des `switch(dry_run=true, wait=false)` et n'est jamais autoritaire ; `ShadowPlan`/`ShadowComparison` comparent cible/IP legacy avec l'état et l'egress du fork ; toute erreur du plan de contrôle est capturée dans la ligne (`control-plane-error`) et aucune exception de télémétrie ne remonte ; `ShadowTelemetry` écrit une table additive `control_plane_shadow` dans `telemetry.db` (jamais `registry.db`) ; `tests/test_control_plane_shadow.py` (8 cas). |
| T073 — ingestion d'événements | fait | `free/control_plane/events.py` : `EventIngestor.poll` lit `GET /v1/events?since=<curseur>`, applique chaque `(station, seq)` une seule fois et persiste le curseur ; `free/ip_registry/registry_db.py` ajoute `control_cursor` et `control_events` (migration additive) et applique le lot dans une transaction `BEGIN IMMEDIATE` (un échec de mapping ne saute jamais un événement). Mapping : `job.committed`/`egress.verified`/`transport.*` → `observations`, `job.failed_degraded`/`dns.*`/`state.*` → `incidents`, `job.exhausted`/`transport.cycle_exhausted`/`tunnel.repair_needed` → `repair_requests` (jamais de désactivation de station), `job.rolled_back` répété → repair ; `tests/test_control_plane_events.py` (9 cas). |
| T074 — adaptateur rotation | fait | `GluetunControlV2Binder.rotate_outcome` envoie la liste ordonnée verbatim (aucun score/quota, seuls les hostnames vides sont retirés), transmet exclusions et budget (`max_candidates`/`max_attempts`) et normalise `committed`/`exhausted`/`rolled_back` en `RotationOutcome` sans exception ; `exhausted`/`rolled_back` conservent la station et ses tentatives ; `tests/test_rotation_contract.py` (7 cas). |
| T075 — adaptateur egress | fait | `StationEgress` accepte `control_plane=` et consulte `/v1/egress` en priorité (preuve k-of-n du fork) dans `__call__` et `control_only` ; une preuve non vérifiée, une IP interdite ou une erreur du fork retombe sur les cinq couches froides inchangées ; `last_source=control_plane:/v1/egress` ; `tests/test_egress_adapter.py` (6 cas) + `tests/test_ip_registry_egress.py`. |
| T076 — dashboard | fait | `dashboard/routers/station_control_routes.py` ajoute `GET .../control/{state,jobs/{id},events}` et `POST .../control/{switch,rotate,rollback,transport/reset}` : lectures `/v1/vpn/state`/jobs/événements, actions qui créent un job et renvoient `202` + suivi par id (ou `200` en `wait`/`dry_run`), aucune commande Docker ; `dashboard/api.py` câble `control_getter` avec un client mis en cache ; `tests/test_dashboard_station_control.py` (6 cas). |
| T077 — fallback legacy | fait | `legacy_fallback_reason` n'autorise que timeout/`ControlPlaneUnavailable`/5xx après retries (jamais 403/409/422/429) ; `GluetunControlClient.wait_for_active_job` suit un 409 sans créer de second job ; le binder sonde `state.active_job_id` avant tout fallback et trace la raison via `on_fallback` ; `legacy=` rejoue le chemin froid documenté ; `tests/test_control_plane_fallback.py` (7 cas). |
| T078 — activation 6→1 | fait (outillage) | `scripts/rollout_control_plane.py` : ordre 6→1 borné à six stations, `--dry-run` sans contact, `probe_station` exige `vpn_status=running` + egress vérifié + IP valide/non-hôte, arrêt au premier échec ; `ops/runbooks/control-plane.md` documente activation, rollback et incidents ; `tests/test_rollout_control_plane.py` (3 cas). L'exécution réelle sur les six stations reste une action opérateur. |
| T079 — chemin chaud sans Docker | fait (test) | `tests/test_no_docker_exec_hot_path.py` inspecte l'AST des modules chauds (`free/control_plane/*`, binder v2, routes dashboard, rollout) : aucun import `subprocess`/`shlex`, aucun `os.system`/`os.popen`, aucun littéral `docker …` ; le binder v2 n'importe pas le wrapper legacy ; `gluetun_control.py` est explicitement marqué « cold path only » et reste le secours `LegacyFallback`. Le ratio SC-009 ≥ 95 % se mesure après T078. |

Le client est volontairement inerte tant que `control_plane.mode=legacy` : la
rotation automatique reste legacy, le dashboard n'expose le plan de contrôle
que sur des routes explicites et `scripts/rollout_control_plane.py` ne modifie
pas la configuration. Le passage en `shadow` puis `active` reste une décision
de rollout (T078), station par station.

## Vérifications

```text
go build ./...                                                               -> ok
docker build -t gluetun-016:local gluetun                                    -> ok (image 46.7 MB)
pilote station 6 (gluetun-016:local) : capabilities, state, transport,
events, servers, settings masqués -> ok ; cible injoignable ->
422 invalid_target/resolution_failed ; switch réel br143 -> br150 :
state=committed, egress vérifié 187.15.85.191 (quorum 3), proxies
HTTP/SOCKS5 -> ok
go test ./internal/control/... ./internal/server ./internal/configuration/settings ./internal/vpn/transport -count=1 -> ok
go test ./internal/configuration/settings ./internal/control/... ./internal/server ./tests/integration/harness -> ok
go test ./internal/egress/... ./internal/control/... ./internal/server ./internal/configuration/settings -> ok
go test ./internal/control/state -count=1                                     -> ok
go test ./internal/control/jobs -count=1                                      -> ok
go test ./tests/integration -run Test_EgressChaos                             -> ok
go test ./internal/server/middlewares/auth -run 'Test_authHandler_ServeHTTP|Test_Role|Test_Settings' -> ok
go test ./tests/contract -run Security -count=1                              -> ok
go test ./internal/control/events ./internal/server/... ./internal/control/state ./internal/control/api ./tests/contract -count=1 -> ok
python -m pytest tests/test_gluetun_control_v2.py tests/test_control_plane_config.py tests/test_control_plane_shadow.py tests/test_control_plane_events.py tests/test_rotation_contract.py tests/test_egress_adapter.py tests/test_dashboard_station_control.py tests/test_control_plane_fallback.py tests/test_no_docker_exec_hot_path.py tests/test_rollout_control_plane.py tests/test_transport_ladder_shadow.py -q -> 79 passed
python -m pytest tests/test_station_control_routes.py tests/test_ip_registry_egress.py -q -> 11 passed
python -m ruff check free/control_plane free/ip_registry/registry_db.py free/ip_registry/adapters/gluetun_control_v2.py free/ip_registry/adapters/gluetun_control.py free/ip_registry/adapters/egress.py dashboard/routers/station_control_routes.py scripts/rollout_control_plane.py tests/test_rotation_contract.py tests/test_egress_adapter.py tests/test_dashboard_station_control.py tests/test_control_plane_fallback.py tests/test_no_docker_exec_hot_path.py tests/test_rollout_control_plane.py -> ok
python -m ruff format --check free/control_plane free/ip_registry/adapters/gluetun_control_v2.py free/ip_registry/adapters/gluetun_control.py free/ip_registry/adapters/egress.py dashboard/routers/station_control_routes.py scripts/rollout_control_plane.py tests/test_rotation_contract.py tests/test_egress_adapter.py tests/test_dashboard_station_control.py tests/test_control_plane_fallback.py tests/test_no_docker_exec_hot_path.py tests/test_rollout_control_plane.py -> ok
python scripts/rollout_control_plane.py --dry-run -> ok (plan 6 -> 1, aucun contact)
go vet ./internal/control/... ./internal/server/... ./internal/configuration/settings/... ./tests/integration/harness -> ok
GOOS=linux GOARCH=amd64 go build ./...                                       -> ok
golangci-lint v2.14.0 (harness, control, settings, server, egress, tests, cmd) -> 0 issues
golangci-lint v2.14.0 ./internal/control/state/...                            -> 0 issues
golangci-lint v2.14.0 ./internal/control/jobs/...                             -> 0 issues
golangci-lint fmt --diff ./internal/control/state/...                         -> no diff
actionlint control-plane-ci.yml                                              -> ok
docker compose -f tests/integration/docker-compose.yml up -d --wait ...      -> all healthy
docker compose -f tests/integration/docker-compose.yml run --rm check        -> all harness services are ready
```

`markdownlint-cli2` et `golangci-lint` ne sont pas installés dans
l'environnement courant ; les contrôles Markdown et lint Go n'ont donc pas été
exécutés. Commandes à lancer une fois disponibles :
`markdownlint-cli2 "**/*.md"` et `golangci-lint run`.

`internal/server/middlewares/auth` ferme désormais son fichier de configuration
après lecture : le nettoyage `TempDir` sous Windows ne bloque plus.
`go test ./...` conserve des échecs Windows/network sans rapport avec ces
changements (`internal/storage` avec `airvpn.json` absent, `internal/dns`,
`internal/natpmp`, `internal/netlink`, `internal/portforward`,
`internal/pmtud/tcp`, `internal/configuration/sources/files`).

## Écarts assumés

- T004/T005 ne modifient pas les workflows upstream : le harnais et la CI du
  plan de contrôle vivent dans `tests/integration` et
  `.github/workflows/control-plane-ci.yml` pour que `git rebase upstream/master`
  reste sans conflit.
- Le test de contrat upstream est dans `internal/server` et non `tests/contract`,
  car il a besoin du câblage interne `newHandler` et des mocks générés du paquet
  `server`. La couverture demandée par T003 est identique.
- Les tests des routes egress sont dans `internal/control/api/egress_test.go` et
  non `tests/contract/egress_test.go`, pour la même raison : ils exercent le
  `Handler` réel (routage, parsing, mapping d'erreurs) sans dupliquer le serveur.
- EG-AT-01 du contrat annonce `confidence=high` tout en décrivant un dissident ;
  la règle §4.2 (un dissident ⇒ `medium`) fait foi dans l'implémentation et
  dans le test de chaos.
- La validation multi-arch complète (buildx `amd64`+`arm64`) et le scan Trivy
  sont délégués à la CI GitHub : l'environnement local ne permet pas de
  reproduire le push GHCR ni l'authentification du runner.
- T095 est branché dans la boucle VPN ; il reste inactif tant que `VPN_TYPE`
  est explicite, ce qui est le cas de toutes les stations du compose actuel.
  `GET /v1/vpn/transport` et `capabilities.transport_ladder` reflètent le
  service réellement attaché.
- La flotte live tourne encore l'image upstream `qmcgaw/gluetun` épinglée par
  digest, sauf la station 6 déployée en pilote avec `gluetun-016:local`
  (plan de contrôle actif, `CONTROL_STATE_MODE=env`). Les stations 1-5 restent
  en upstream : T078 exige de les migrer une par une, puis d'activer
  `CONTROL_PLANE_ENABLED`. Le pilote 6 valide désormais un switch complet :
  `br143 → br150` en WireGuard, job `committed`, egress vérifié
  (`187.15.85.191`, quorum 3), proxies HTTP/SOCKS5 fonctionnels. Trois
  correctifs ont été nécessaires : les filtres pays/région/ville sont effacés
  quand un hostname est pinné, la vérification d'egress est retentée après la
  bascule, et un rollback sans baseline egress restaure quand même le snapshot
  puis prouve l'egress à neuf. Le superviseur (`scripts/fresh_chain.py`) ajoute
  l'override du plan de contrôle à ses recreates, sinon chaque rotation
  retombait sur l'image upstream. Les endpoints WG brésiliens restent
  fluctuants : `br101`, `br127`, `br71`, `br72`, `br149` ne passaient pas de
  trafic au moment du test, tandis que `br53`, `br143` et `br150` répondaient.
- T049 (mesure live DoH/TCP 853 bloqués) reste ouvert : le script existe et sa
  syntaxe est vérifiée, mais il demande un réseau réellement filtré.
- T099 est partiel : les tests Go du ladder et de l'intégration passent, la
  parité shadow Python est couverte par `TransportShadowObserver`
  (`tests/test_transport_ladder_shadow.py`) ; `TR-AT-08`/`TR-AT-09` demandent
  encore un tunnel réel pour observer bascule et réparation.

## Activation

`CONTROL_PLANE_ENABLED=false` ne monte aucune route 016 et laisse les réglages
inertes. Les valeurs par défaut sont celles de l'ADR. `features` n'annonce que
les capacités effectivement attachées :

- `switch`, `rotate`, `rollback`, `dry_run`, `drain` : `true` dès qu'un
  `jobs.Manager` réel est construit (egress verification active et catalogue
  disponible) ;
- `egress_consensus` : `true` quand `CONTROL_EGRESS_ENABLED=true` (défaut) et
  que le service egress est monté ;
- `dns_ladder` : `true` quand le looper DNS expose `api.DNSService` ;
- `events_sse` : `true` quand le bus d'événements est monté ;
- `state_persistence` : `true` quand un `StateDir` est configuré et que
  `/v1/vpn/state` est attaché ;
- `secret_reveal` : `true` (le middleware auth admin reste seul juge de
  `reveal=true`) ;
- `transport_ladder` : `true` quand le service runtime est attaché
  (`TRANSPORT_ENABLED=true`) ; `false` sinon. Le service peut être attaché sans
  piloter un run donné si `shouldRunLadder` est faux (`VPN_TYPE` explicite,
  `amneziawg`, `OpenVPN.ConfFile`, hostname non pinné).
