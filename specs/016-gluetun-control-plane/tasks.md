# Tasks — plan de contrôle Gluetun

**Feature** : `016-gluetun-control-plane`
**Input** : `spec.md`, `plan.md`, `research.md`, `audit.md`, `contracts/`
**Date** : 2026-11-11

Légende : `[P]` = parallélisable avec les autres tâches de la même phase ;
`P0` = bloquant la release ; `P1` = requis pour la cible ; `P2` = qualité et
robustesse.

Chaque tâche indique : fichiers, action, acceptation, test. Les fichiers du fork
sont relatifs au dépôt `jul-ienfr/gluetun` ; les fichiers Python sont relatifs à
`opencode-proxy-one`.

---

## Phase 0 — Baseline, flags et harnais (J0–J3)

### T001 — Geler la baseline upstream (P0)
- **Fichiers** : `go.mod`, `Dockerfile`, `.github/workflows/ci.yml`.
- **Action** : épingler le commit upstream, créer la branche `016-control-plane`,
  documenter le point de rebase, vérifier `git diff upstream/master` vide.
- **Acceptation** : build reproductible ; 0 diff avant ajout.
- **Test** : `git diff --exit-code upstream/master -- .` sur la baseline.

### T002 — Feature flags et réglages (P0)
- **Fichiers** : `internal/configuration/settings/control.go`,
  `internal/configuration/settings/settings.go`, `cmd/gluetun/main.go`.
- **Action** : ajouter `CONTROL_PLANE_ENABLED`, `CONTROL_STATE_DIR`,
  `CONTROL_STATE_MODE`, `CONTROL_EGRESS_*`, `DNS_LADDER`, `DNS_FAIL_CLOSED`,
  avec défauts sûrs ; aucune route montée si `off`.
- **Acceptation** : `off` = comportement upstream strict ; `on` = routes
  présentes.
- **Test** : `internal/configuration/settings/control_test.go` (défauts,
  validation, inconnues).

### T003 — Harnais de contrat upstream (P0) [P]
- **Fichiers** : `tests/contract/upstream_test.go`.
- **Action** : rejouer `/version`, `/v1/vpn/status`, `/v1/vpn/settings`,
  `/v1/dns/status`, `/v1/publicip/ip`, `/v1/portforward`.
- **Acceptation** : les réponses et codes sont identiques avant/après.
- **Test** : `go test ./internal/server -run Upstream`.

### T004 — Harnais réseau local (P0) [P]
- **Fichiers** : `tests/integration/harness/` (faux DoT, faux DNS plain, échos
  HTTP, `vpn-wg-test`), `tests/integration/docker-compose.yml`.
- **Action** : fournir des services déterministes pour l'egress et le DNS.
- **Acceptation** : une vérification complète fonctionne sans Internet.
- **Test** : `docker compose -f tests/integration/docker-compose.yml up -d --wait`
  puis `docker compose -f tests/integration/docker-compose.yml run --rm check`.

### T005 — CI et image (P0) [P]
- **Fichiers** : `.github/workflows/control-plane-ci.yml`, `Dockerfile`.
- **Action** : `go test`, `go vet`, `golangci-lint`, build multi-arch, scan,
  publication GHCR, digest en sortie.
- **Acceptation** : pipeline vert, image épinglable par digest.
- **Test** : `docker buildx build --platform linux/amd64,linux/arm64 .`

### T006 — Squelette API et modèle d'erreur (P0)
- **Fichiers** : `internal/control/api/{router.go,errors.go}`,
  `internal/server/handler.go`.
- **Action** : monter `/v1/control`, `/v1/vpn/{state,jobs,switch,rotate,
  rollback}`, `/v1/egress`, `/v1/dns`, `/v1/events` ; enveloppe d'erreur
  commune ; 404/405/415.
- **Acceptation** : routes répondent `501` tant que non implémentées, sans
  panique.
- **Test** : `internal/control/api/router_test.go`.

### T007 — Scopes et mapping des routes (P0)
- **Fichiers** : `internal/control/auth/scopes.go`,
  `internal/server/middlewares/auth/settings.go` (lecture seule).
- **Action** : `reader/controller/admin`, table route→scope, refus `403
  forbidden_scope`, journalisation sans secret.
- **Acceptation** : une clé reader ne peut pas `POST switch`.
- **Test** : `internal/control/auth/scopes_test.go`.

### T008 — Observabilité de base (P1) [P]
- **Fichiers** : `internal/control/observability/metrics.go`,
  `internal/control/observability/log.go`.
- **Action** : logger structuré avec `job_id`, compteurs de routes, histogrammes
  de latence ; scrub des secrets.
- **Acceptation** : aucune valeur sensible dans les logs de test.
- **Test** : `internal/control/observability/scrub_test.go`.

### T009 — Validation de configuration (P0)
- **Fichiers** : `internal/configuration/settings/control.go`,
  `internal/dns/ladder/config.go`.
- **Action** : refuser `CONTROL_EGRESS_QUORUM > MIN_SOURCES`, un ladder avec un
  seul étage si redondance exigée, un état dir non inscriptible, un timeout nul.
- **Acceptation** : démarrage refusé avec message explicite, pas de panique.
- **Test** : `internal/configuration/settings/control_test.go::TestInvalid`.

### T010 — ADR et capabilities (P1) [P]
- **Fichiers** : `docs/adr-control-plane.md`,
  `internal/control/api/capabilities.go`.
- **Action** : documenter D1–D13, exposer version/features/limites.
- **Acceptation** : `GET /v1/control/capabilities` reflète la config effective.
- **Test** : `internal/control/api/capabilities_test.go`.

---

## Phase 1 — Egress verification (J3–J8)

### T020 — Interface Source et source HTTP (P0)
- **Fichiers** : `internal/egress/source/{interfaces.go,build.go,http.go}`.
- **Action** : interface, timeout, parse JSON/texte, erreurs typées.
- **Acceptation** : une source HTTP lente n'excède pas son timeout.
- **Test** : `internal/egress/source/http_test.go`.

### T021 — Sources DNS whoami et publicip (P0) [P]
- **Fichiers** : `internal/egress/source/{dns.go,publicip.go}`.
- **Action** : whoami via le resolver du tunnel ; lecture `publicip` interne.
- **Acceptation** : la source DNS prouve aussi la résolution ; publicip marquée
  non suffisante seule.
- **Test** : `internal/egress/source/dns_test.go`.

### T022 — Capture de l'IP d'hôte (P0)
- **Fichiers** : `internal/egress/source/host.go`.
- **Action** : lire les interfaces au boot, conserver v4/v6, recharger sur
  changement d'interface.
- **Acceptation** : l'IP d'underlay est connue avant le tunnel.
- **Test** : `internal/egress/source/host_test.go` (interfaces factices).

### T023 — Règles de rejet (P0)
- **Fichiers** : `internal/egress/consensus/reject.go`.
- **Action** : privé, loopback, link-local, CGNAT, multicast, unspecified,
  documentation (hors test), host, forbidden, malformed, wrong_family.
- **Acceptation** : table de vérité exhaustive.
- **Test** : `internal/egress/consensus/reject_test.go` (≥ 40 cas).

### T024 — Consensus k-of-n (P0)
- **Fichiers** : `internal/egress/consensus/consensus.go`.
- **Action** : groupement par IP exacte, quorum, dissidents, tie-break
  déterministe, confiance, familles séparées.
- **Acceptation** : EG-AT-01/02/06/08.
- **Test** : `internal/egress/consensus/consensus_test.go`.

### T025 — Canary HTTP (P0) [P]
- **Fichiers** : `internal/egress/canary/canary.go`.
- **Action** : statut attendu, corps minimal, SHA-256 optionnel, timeout, pas de
  redirection.
- **Acceptation** : 200 vide = échec (`empty_body`).
- **Test** : `internal/egress/canary/canary_test.go`.

### T026 — Cache et single-flight (P0)
- **Fichiers** : `internal/egress/verify/{service.go,cache.go}`.
- **Action** : TTL 10 s, `fresh`, single-flight, timeout global.
- **Acceptation** : 10 appels concurrents → une collecte.
- **Test** : `internal/egress/verify/service_test.go`.

### T027 — Routes egress (P0)
- **Fichiers** : `internal/control/api/{egress.go,router.go}`.
- **Action** : `GET /v1/egress`, `/verify`, `/canary`, `/history` ; schémas du
  contrat.
- **Acceptation** : réponses conformes, `422 insufficient_sources`.
- **Test** : `internal/control/api/egress_test.go`.

### T028 — Métriques egress (P2) [P]
- **Fichiers** : `internal/control/observability/metrics.go`.
- **Action** : compteurs par source/résultat, latence, quorum, dissidents,
  cache.
- **Acceptation** : métriques exposées et nommées comme au contrat.
- **Test** : `internal/control/observability/metrics_test.go`.

### T029 — Chaos egress (P0)
- **Fichiers** : `tests/integration/egress_chaos_test.go`.
- **Action** : source menteuse, IP d'hôte, corps vide, 429, timeout, une seule
  source.
- **Acceptation** : zéro faux `verified` ; EG-AT-01..08.
- **Test** : `go test ./tests/integration -run EgressChaos`.

---

## Phase 2 — DNS ladder (J5–J11)

### T040 — Modèle Tier/Endpoint (P0)
- **Fichiers** : `internal/dns/ladder/{tier.go,endpoint.go,state.go}`.
- **Action** : types, états, compteurs, EWMA, cooldown.
- **Acceptation** : modèles sérialisables pour `/v1/dns/tiers`.
- **Test** : `internal/dns/ladder/tier_test.go`.

### T041 — Algorithme de résolution (P0)
- **Fichiers** : `internal/dns/ladder/ladder.go`.
- **Action** : ordre, attempts, fallback typé, NXDOMAIN terminal, TC retry TCP,
  endpoints multiples.
- **Acceptation** : DNS-AT-01..06.
- **Test** : `internal/dns/ladder/ladder_test.go` (table de vérité).

### T042 — Santé active/passive (P0)
- **Fichiers** : `internal/dns/ladder/health.go`,
  `internal/dns/ladder/probe.go`.
- **Action** : seuils, backoff, half-open, probe périodique, événements.
- **Acceptation** : récupération après 2 succès ; down après 3 échecs.
- **Test** : `internal/dns/ladder/health_test.go`.

### T043 — Intégration `internal/dns` (P0)
- **Fichiers** : `internal/dns/{run.go,settings.go,loop.go}`.
- **Action** : brancher le ladder comme resolver amont ; conserver cache,
  filtre, localdns, `publicNamesAsLocal` au-dessus.
- **Acceptation** : un seul listener ; healthcheck ne redémarre plus le tunnel
  sur panne d'étage.
- **Test** : `internal/dns/ladder_integration_test.go`.

### T044 — Configuration et défauts (P0)
- **Fichiers** : `internal/configuration/settings/dns.go`,
  `internal/dns/ladder/config.go`.
- **Action** : `DNS_LADDER`, providers par étage, timeouts, santé ; défaut flotte
  `dot,plain`.
- **Acceptation** : `dot,doh,plain` accepté ; `doh` seul avertit.
- **Test** : `internal/configuration/settings/dns_test.go`.

### T045 — Routes DNS (P0)
- **Fichiers** : `internal/control/api/dns.go`.
- **Action** : `/v1/dns/state`, `/tiers`, `POST /probe`.
- **Acceptation** : schémas du contrat, `403` sur probe sans scope.
- **Test** : `tests/contract/dns_test.go`.

### T046 — Métriques et événements DNS (P1) [P]
- **Fichiers** : `internal/control/observability/metrics.go`,
  `internal/control/events/bus.go`.
- **Action** : métriques par étage/endpoint, événements `dns.*`.
- **Acceptation** : noms conformes au contrat.
- **Test** : `internal/dns/ladder/metrics_test.go`.

### T047 — Fail-closed (P0)
- **Fichiers** : `internal/dns/ladder/ladder.go`,
  `tests/integration/dns_failclosed_test.go`.
- **Action** : tous les étages down → SERVFAIL, aucune requête hôte,
  `fail_closed=true`.
- **Acceptation** : capture tcpdump/iptables = zéro paquet hors tunnel.
- **Test** : `go test ./tests/integration -run DNSFailClosed`.

### T048 — NXDOMAIN et TC (P0) [P]
- **Fichiers** : `internal/dns/ladder/ladder_test.go`.
- **Action** : vérifier qu'NXDOMAIN ne déclenche pas de fallback ; TC retry TCP.
- **Acceptation** : DNS-AT-04/05.
- **Test** : table de vérité.

### T049 — Mesure live DoH bloqué (P1)
- **Fichiers** : `specs/016-gluetun-control-plane/quickstart.md` (section 5).
- **Action** : rejouer le blocage TCP/853 et DoH sur une station de test,
  consigner les latences.
- **Acceptation** : bascule plain ≤ 2 s p95.
- **Test** : script `tests/integration/dns_live_block.sh`.

---

## Phase 2b — Ladder de transports (parallèle P2/P3)

### T090 — Modèle Policy/Attempt/State/Breaker (P0)
- **Fichiers** : `internal/vpn/transport/{policy.go,state.go,breaker.go}`.
- **Action** : types `Transport`, `Policy`, `Attempt`, `State`, `Breaker`, raisons
  typées, validation de configuration.
- **Acceptation** : ladder inconnu ou vide rejeté au démarrage ; doublons ignorés
  avec warning.
- **Test** : `internal/vpn/transport/policy_test.go`.

### T091 — Boucle du ladder et tunnel unique (P0)
- **Fichiers** : `internal/vpn/transport/{ladder.go,teardown.go}`.
- **Action** : ordre du ladder, preflight → teardown → apply → tunnel → egress →
  commit/échec → suivant ; verrou de boucle ; `teardown_current` explicite.
- **Acceptation** : un seul transport actif ; jamais deux interfaces VPN `up`.
- **Test** : `internal/vpn/transport/ladder_test.go` + `ip link` en intégration.

### T092 — Preflight et classification des erreurs (P0)
- **Fichiers** : `internal/vpn/transport/{preflight.go,errors.go}`.
- **Action** : résolution, port TCP/UDP, credentials, settings par transport ;
  mapper vers `auth`, `udp_blocked`, `tcp_refused`, `tls`, `handshake_timeout`,
  `no_egress`, `interface_down`, `config_invalid`.
- **Acceptation** : aucune coupure si le preflight échoue ; raison toujours typée.
- **Test** : `internal/vpn/transport/errors_test.go` (table par code).

### T093 — Breaker et backoff/jitter (P0)
- **Fichiers** : `internal/vpn/transport/breaker.go`.
- **Action** : `closed→open→half_open`, seuils, cooldown doublé et plafonné,
  backoff de cycle avec jitter, `max_cycles`.
- **Acceptation** : transport `open` ignoré jusqu'à `retry_at` ; retour à
  `policy[0]` après backoff ; aucune boucle serrée.
- **Test** : `internal/vpn/transport/breaker_test.go` (horloge injectée).

### T094 — Watchdog même-serveur (P0)
- **Fichiers** : `internal/vpn/transport/{watchdog.go,reconnect.go}`.
- **Action** : détection interface absente, handshake stale, statut non
  `running`, DNS local injoignable ; reconnexion même transport/serveur.
- **Acceptation** : détection ≤ 2 s ; `local_reconnect_max`, puis transport
  suivant ; `tunnel.repair_needed` après budget.
- **Test** : `internal/vpn/transport/watchdog_test.go` + intégration kill tunnel.

### T095 — Intégration boucle VPN, firewall et MTU (P0)
- **Fichiers** : `internal/vpn/loop.go`, `internal/vpn/transport/integration.go`,
  `internal/firewall/*` (appel).
- **Action** : brancher le ladder sur la boucle existante ; réappliquer firewall
  et recalculer MTU/PMTUD avant de déclarer `up` ; `VPN_TYPE` explicite désactive
  le ladder.
- **Acceptation** : comportement upstream inchangé si `VPN_TYPE` est explicite.
- **Test** : `internal/vpn/transport/integration_test.go`.

### T096 — Endpoints transport (P0)
- **Fichiers** : `internal/control/api/transport.go`,
  `internal/server/handlerv1.go`.
- **Action** : `GET /v1/vpn/transport` (reader) et
  `POST /v1/vpn/transport/reset` (admin) ; schémas du contrat.
- **Acceptation** : 200/403 conformes ; reset ne coupe pas le tunnel actif.
- **Test** : `tests/contract/transport_test.go`.

### T097 — Événements et métriques transport (P1)
- **Fichiers** : `internal/vpn/transport/metrics.go`,
  `internal/control/events/transport.go`.
- **Action** : `transport.attempt`, `connected`, `failed`, `breaker_open`,
  `breaker_reset`, `cycle_exhausted`, `tunnel.repair_needed` ; métriques
  Prometheus par transport.
- **Acceptation** : chaque tentative produit exactement un événement ; aucune
  donnée secrète.
- **Test** : `internal/vpn/transport/metrics_test.go`.

### T098 — `transport_policy` dans switch/rotate (P1)
- **Fichiers** : `internal/control/jobs/{switch.go,rotate.go}`,
  `internal/control/api/switch.go`.
- **Action** : accepter `auto`, un transport unique ou une liste ordonnée ;
  valider ; propager au ladder sans toucher à la cible serveur/IP.
- **Acceptation** : une policy restreinte ne change jamais le serveur ; policy
  invalide ⇒ `422 invalid_target`.
- **Test** : `internal/control/jobs/transport_policy_test.go`.

### T099 — Tests ladder et shadow Python (P0)
- **Fichiers** : `tests/integration/transport_ladder_test.go`,
  `tests/test_transport_ladder_shadow.py` (côté projet).
- **Action** : scénarios UDP bloqué, TCP refusé, auth invalide, tunnel qui meurt,
  cycle complet, reset breaker ; parité shadow Python/fork.
- **Acceptation** : `TR-AT-01`→`TR-AT-10` et `ORCH-AT-09` verts ; aucun double
  tunnel ; zéro recréation Compose.
- **Test** : `go test ./tests/integration -run Transport` + `pytest -k transport_ladder`.

---

## Phase 3 — SwitchJob, état, rollback (J9–J17)

### T030 — Schéma DesiredState (P0)
- **Fichiers** : `internal/control/state/desired.go`.
- **Action** : schéma v1, checksum canonique, validation, migration future.
- **Acceptation** : un state invalide est refusé sans écraser l'ancien.
- **Test** : `internal/control/state/desired_test.go`.

### T031 — Écriture atomique et journal (P0)
- **Fichiers** : `internal/control/state/{journal.go,atomic.go,store.go}`.
- **Action** : `tmp` + `fsync` + `rename`, mode 0600, append-only, rotation,
  rétention.
- **Acceptation** : disque plein → job `failed`, ancien état conservé.
- **Test** : `internal/control/state/journal_test.go` (simulation d'erreur).

### T032 — Snapshot et reprise (P0)
- **Fichiers** : `internal/control/state/{desired.go,recovery.go}` (les types de
  snapshot vivent dans `desired.go` avec le schéma).
- **Action** : capture avant switch, restauration, modes
  `state|env|merge`, `state.invalid`.
- **Acceptation** : restart restaure le serveur désiré ou expose `drift`.
- **Test** : `internal/control/state/recovery_test.go`.

### T033 — Préflight (P0)
- **Fichiers** : `internal/control/jobs/preflight.go`.
- **Action** : validation catalogue, credentials, résolution, appartenance IP,
  TCP OpenVPN / UDP WireGuard, transport.
- **Acceptation** : préflight échoué ⇒ aucune coupure.
- **Test** : `internal/control/jobs/preflight_test.go`.

### T034 — Machine à états (P0)
- **Fichiers** : `internal/control/jobs/{job.go,machine.go,executor.go}`.
- **Action** : états/transitions du contrat, timings, attempts, cancel,
  `interrupted`.
- **Acceptation** : toutes les transitions testées ; aucun état actif après
  crash.
- **Test** : `internal/control/jobs/machine_test.go`.

### T035 — Idempotence (P0)
- **Fichiers** : `internal/control/jobs/idempotency.go`.
- **Action** : clé 24 h, hash du corps, conflit, purge.
- **Acceptation** : même clé/corps → même job ; corps différent → 409.
- **Test** : `internal/control/jobs/idempotency_test.go`.

### T036 — Drain borné (P1) [P]
- **Fichiers** : `internal/control/jobs/drain.go`.
- **Action** : attente des connexions proxy actives, timeout, event
  `drain.timeout`.
- **Acceptation** : le drain n'excède jamais son timeout.
- **Test** : `internal/control/jobs/drain_test.go`.

### T037 — Rollback et failed_degraded (P0)
- **Fichiers** : `internal/control/jobs/rollback.go`.
- **Action** : réappliquer le snapshot, re-vérifier l'ancienne IP, terminal
  `rolled_back` ou `failed_degraded`, événement critique.
- **Acceptation** : rollback prouvé ; aucun boucle.
- **Test** : `internal/control/jobs/rollback_test.go`.

### T038 — Routes switch/jobs (P0)
- **Fichiers** : `internal/control/api/{switch.go,jobs.go,rollback.go}`.
- **Action** : `POST switch`, `GET jobs`, `GET jobs/{id}`, `POST cancel`,
  `POST rollback`, `wait`, `dry_run`, `Location`.
- **Acceptation** : codes 200/202/409/422 conformes.
- **Test** : `tests/contract/switch_test.go`.

### T039 — Compatibilité `PUT /v1/vpn/settings` (P0)
- **Fichiers** : `internal/server/vpn.go`,
  `internal/control/api/settings_compat.go`.
- **Action** : legacy conservé ; routage vers SwitchJob si `dry_run`/`wait`/
  `require_verified`/`target`.
- **Acceptation** : tests upstream verts ; nouveau mode fonctionnel.
- **Test** : `tests/contract/settings_compat_test.go`.

---

## Phase 4 — Catalogue et rotation (J15–J21)

### T050 — Lecture catalogue et facettes (P1) [P]
- **Fichiers** : `internal/control/catalog/{facets.go,query.go,api.go}`.
- **Action** : `GET /v1/servers`, `/facets`, filtres, pagination.
- **Acceptation** : facettes cohérentes avec le catalogue provider.
- **Test** : `internal/control/catalog/facets_test.go`.

### T051 — Détail serveur et stats (P1)
- **Fichiers** : `internal/control/catalog/{detail.go,stats.go}`.
- **Action** : `GET /v1/servers/{hostname}`, stats runtime consultatives.
- **Acceptation** : aucun secret ; cooldown exposé.
- **Test** : `internal/control/catalog/detail_test.go`.

### T052 — Validation d'IP d'endpoint (P0)
- **Fichiers** : `internal/control/catalog/validate.go`.
- **Action** : vérifier que `endpoint_ip` appartient aux IPs du hostname ; sinon
  422 `endpoint_ip_mismatch`.
- **Acceptation** : aucune coupure sur IP incohérente.
- **Test** : `internal/control/catalog/validate_test.go`.

### T053 — Route rotation (P0)
- **Fichiers** : `internal/control/api/rotate.go`,
  `internal/control/jobs/rotation.go`.
- **Action** : candidats ordonnés ou politique, création du job `rotate`,
  réutilisation du SwitchJob interne.
- **Acceptation** : liste non re-scorée ; un seul job interne actif.
- **Test** : `internal/control/jobs/rotation_test.go`.

### T054 — Exclusions et gates (P0)
- **Fichiers** : `internal/control/jobs/gates.go`.
- **Action** : `require_new_ip`, `require_new_subnet`, `expected_egress_ip`,
  `exclude_*`, `forbidden_ips`, `same_ip`, `same_subnet`, `egress_mismatch`.
- **Acceptation** : rejet typé et candidat suivant.
- **Test** : `internal/control/jobs/gates_test.go` (≥ 20 cas).

### T055 — Cooldown et exhausted (P0)
- **Fichiers** : `internal/control/jobs/rotation.go`,
  `internal/control/catalog/cooldown.go`.
- **Action** : cooldown par hostname, backoff, budget, `exhausted` structuré,
  station préservée.
- **Acceptation** : `global_state_written=false`, `station_preserved=true`.
- **Test** : `internal/control/jobs/exhausted_test.go`.

### T056 — Refresh catalogue (P2)
- **Fichiers** : `internal/control/api/servers.go`,
  `internal/provider/nordvpn/updater`.
- **Action** : `POST /v1/servers/refresh` admin, rate-limité, idempotent.
- **Acceptation** : 1/min maximum ; pas de blocage du tunnel.
- **Test** : `internal/control/api/refresh_test.go`.

### T057 — Tests rotation 3 candidats (P0)
- **Fichiers** : `tests/integration/rotation_test.go`.
- **Action** : A échoue préflight, B échoue preuve, C réussit ; puis 3 échecs.
- **Acceptation** : US2 scénarios 1–5 ; aucune station désactivée.
- **Test** : `go test ./tests/integration -run Rotation`.

---

## Phase 5 — Événements, sécurité, observabilité (J19–J24)

### T060 — Bus et séquence (P1)
- **Fichiers** : `internal/control/events/{bus.go,seq.go}`.
- **Action** : séquence monotone, abonnés, backpressure, types normalisés.
- **Acceptation** : aucun événement perdu pour un abonné lent (buffer borné +
  drop policy documentée).
- **Test** : `internal/control/events/bus_test.go`.

### T061 — SSE (P1)
- **Fichiers** : `internal/control/api/events.go`,
  `internal/control/events/sse.go`.
- **Action** : `GET /v1/events`, `/stream`, `Last-Event-ID`, heartbeat.
- **Acceptation** : événement reçu < 1 s ; reprise correcte.
- **Test** : `tests/contract/events_test.go`.

### T062 — Store et rétention (P2) [P]
- **Fichiers** : `internal/control/events/store.go`.
- **Action** : append-only borné, rotation, purge, checksum.
- **Acceptation** : taille plafonnée ; pas de perte du dernier seq.
- **Test** : `internal/control/events/store_test.go`.

### T063 — Masquage des secrets (P0)
- **Fichiers** : `internal/server/vpn.go`,
  `internal/control/api/secrets.go`.
- **Action** : `GET settings` masqué ; `reveal=true` admin ; tests de
  sérialisation.
- **Acceptation** : aucune clé privée/password/token sans reveal.
- **Test** : `tests/contract/secrets_test.go`.

### T064 — Scopes complets (P0)
- **Fichiers** : `internal/control/auth/scopes.go`,
  `internal/server/middlewares/auth/settings.go`.
- **Action** : table route→scope, tests 403, mapping des rôles existants.
- **Acceptation** : reader ne peut ni switch ni reveal.
- **Test** : `tests/contract/scopes_test.go`.

### T065 — Limites de corps et rate (P2)
- **Fichiers** : `internal/control/api/middleware.go`.
- **Action** : 1 MiB, 6 switches/min, timeouts, `413`/`429`.
- **Acceptation** : limites conformes au contrat.
- **Test** : `internal/control/api/limits_test.go`.

### T066 — Capabilities effectives (P2) [P]
- **Fichiers** : `internal/control/api/capabilities.go`.
- **Action** : refléter flags, version, limites, ladder par défaut.
- **Acceptation** : cohérent avec la config runtime.
- **Test** : `tests/contract/capabilities_test.go`.

### T067 — Métriques de jobs (P2) [P]
- **Fichiers** : `internal/control/observability/metrics.go`.
- **Action** : `gluetun_control_job_*`, phases, résultats, latences.
- **Acceptation** : noms conformes, pas de label à cardinalité infinie.
- **Test** : `internal/control/observability/jobs_metrics_test.go`.

### T068 — Scrub des logs (P0)
- **Fichiers** : `internal/control/observability/log.go`.
- **Action** : filtre secrets/clés/tokens, tests avec valeurs pièges.
- **Acceptation** : aucune valeur piège dans la sortie.
- **Test** : `internal/control/observability/scrub_test.go`.

### T069 — Tests de sécurité (P0)
- **Fichiers** : `tests/contract/security_test.go`.
- **Action** : 401/403, reveal, body limit, logs, state 0600.
- **Acceptation** : tous les cas passent.
- **Test** : `go test ./tests/contract -run Security`.

---

## Phase 6 — Intégration orchestrateur (J22–J30)

### T070 — Client Python (P0)
- **Fichiers** : `free/ip_registry/adapters/gluetun_control_v2.py`,
  `free/control_plane/client.py`.
- **Action** : méthodes du contrat, erreurs typées, retries bornés, idempotence.
- **Acceptation** : aucun `docker exec`/`docker cp` ; tests avec faux serveur.
- **Test** : `tests/test_gluetun_control_v2.py`.

### T071 — Configuration Python (P0)
- **Fichiers** : `config.yaml`, `free/control_plane/config.py`.
- **Action** : `control_plane.*` (mode, base_url, timeouts, retry, events).
- **Acceptation** : `legacy|shadow|active` validés ; clé depuis l'environnement.
- **Test** : `tests/test_control_plane_config.py`.

### T072 — Shadow (P0)
- **Fichiers** : `free/control_plane/shadow.py`.
- **Action** : dry-run d'observation, comparaison IP/gates, télémétrie.
- **Acceptation** : zéro coupure, zéro claim, divergences tracées.
- **Test** : `tests/test_control_plane_shadow.py`.

### T073 — Ingestion des événements (P1)
- **Fichiers** : `free/control_plane/events.py`,
  `free/ip_registry/registry_db.py` (migration additive).
- **Action** : curseur `control_cursor`, idempotence `(station, seq)`, mapping
  observations/incidents.
- **Acceptation** : aucun doublon ; reprise après restart.
- **Test** : `tests/test_control_plane_events.py`.

### T074 — Adaptateur rotation (P0)
- **Fichiers** : `free/rotation.py`,
  `free/ip_registry/adapters/gluetun_control_v2.py`.
- **Action** : envoyer la liste ordonnée, exclusions et budget ; consommer
  `committed|exhausted|rolled_back`.
- **Acceptation** : le fork ne re-score pas ; `exhausted` ne désactive rien.
- **Test** : `tests/test_rotation_contract.py`.

### T075 — Adaptateur egress (P1)
- **Fichiers** : `free/ip_registry/adapters/egress.py`.
- **Action** : utiliser `/v1/egress/verify` en priorité ; garder les 5 couches en
  secours froid.
- **Acceptation** : même verdict, latence réduite.
- **Test** : `tests/test_egress_adapter.py`.

### T076 — Dashboard (P2) [P]
- **Fichiers** : `dashboard/routers/station_control_routes.py`.
- **Action** : lire `/v1/vpn/state`, jobs, événements ; remplacer les actions
  `docker exec`.
- **Acceptation** : actions déclenchent un job et suivent son état.
- **Test** : `tests/test_dashboard_station_control.py`.

### T077 — Fallback legacy (P0)
- **Fichiers** : `free/control_plane/client.py`,
  `free/ip_registry/adapters/gluetun_control.py`.
- **Action** : `LegacyFallback` sur 5xx/timeout après retries ; jamais sur 422,
  403, 409 métier.
- **Acceptation** : bascule legacy tracée, pas de double job.
- **Test** : `tests/test_control_plane_fallback.py`.

### T078 — Activation station par station (P0)
- **Fichiers** : `config.yaml`, `ops/runbooks/control-plane.md`.
- **Action** : activer 6 → 1, vérifier `client_usable`, IP et SLO avant chaque
  suivante.
- **Acceptation** : 6/6 restent utilisables ; aucune 7e session.
- **Test** : `scripts/rollout_control_plane.py --dry-run`.

### T079 — Décommissionnement du chemin chaud (P1)
- **Fichiers** : `vpn/manager.py`, `free/ip_registry/adapters/gluetun_control.py`.
- **Action** : retirer `_control_exec`/parsing du chemin normal ; garder un
  wrapper froid documenté.
- **Acceptation** : SC-009 (≥ 95 %) ; recherche `docker exec` vide dans le
  chemin chaud.
- **Test** : `tests/test_no_docker_exec_hot_path.py`.

---

## Phase 7 — Recette et release (J28–J32)

### T080 — Chaos flotte (P0)
- **Fichiers** : `tests/chaos/`.
- **Action** : kill tunnel, blocage DNS, 429 source, state corrompu, restart
  pendant job, rollback impossible.
- **Acceptation** : tous les états terminaux explicites ; aucune boucle.
- **Test** : `pytest tests/chaos -q`.

### T081 — Soak 24 h (P1)
- **Fichiers** : `ops/soak/control_plane.md`.
- **Action** : 100 rotations, 6 stations, surveillance mémoire/latence/events.
- **Acceptation** : 0 job bloqué, 0 fuite DNS, 0 secret loggé.
- **Test** : rapport de soak joint.

### T082 — Non-régression 015 (P0)
- **Fichiers** : `tests/`, `specs/015-egress-uptime-slo/`.
- **Action** : rejouer les tests 015 et la mesure `client_usable`.
- **Acceptation** : aucune régression ; SLO stable.
- **Test** : `pytest tests/ -q` + rapport SLO.

### T083 — Parité shadow 100 bascules (P0)
- **Fichiers** : `free/control_plane/shadow.py`, rapport.
- **Action** : comparer legacy/nouveau sur 100 cycles.
- **Acceptation** : 0 divergence IP/gates.
- **Test** : `pytest tests/test_control_plane_shadow.py -k 100`.

### T084 — Documentation opérateur (P1) [P]
- **Fichiers** : `specs/016-.../quickstart.md`, `ops/runbooks/`.
- **Action** : runbook switch/rotate/rollback/DNS/egress, incidents.
- **Acceptation** : un opérateur exécute les étapes sans aide.
- **Test** : revue par un pair.

### T085 — Release (P0)
- **Fichiers** : `CHANGELOG`, image, `docker-compose.yml`.
- **Action** : tag, digest, notes, matrice de compatibilité, plan de rollback.
- **Acceptation** : image épinglée, tests verts, checklist signée.
- **Test** : `docker compose config` + smoke test.

---

## Dépendances

```text
T001–T010
   ├── T020–T029 (egress)
   ├── T040–T049 (DNS)
   ├── T090–T099 (transport ladder)
   └── T030–T039 (switch) ──► T050–T057 (rotation)
                                  │
                                  ├── T060–T069 (events/security)
                                  └── T070–T079 (Python)
                                            │
                                            └── T080–T085 (recette)
```

- T091 dépend de T090 et de T020–T026 (preuve d'egress par tentative).
- T093/T094 dépendent de T091/T092.
- T095 dépend de T091–T094.
- T096/T097/T098 dépendent de T095.
- T099 dépend de T096–T098 et T072 (shadow Python).
- T033 (préflight) dépend de T052 (validation catalogue).
- T034 (machine) dépend de T030–T032 (state) et T020–T026 (preuve).
- T053 (rotation) dépend de T034/T038.
- T070 (client) peut démarrer dès T038/T045/T027 (contrats).
- T078 (activation) dépend de T072–T077.

## Definition of Done

- [ ] `go test ./...`, `go vet`, `golangci-lint` verts.
- [ ] Tests de contrat upstream et nouveaux verts.
- [ ] `pytest` complet vert, y compris 015 et Compose binder.
- [ ] Aucun secret dans les logs, l'API ou le state.
- [ ] Aucun `docker exec`/`docker cp` dans le chemin chaud.
- [ ] Six stations `client_usable` pendant le rollout ; aucune 7e session.
- [ ] `exhausted` ne désactive jamais une station et n'écrit aucun état global.
- [ ] DNS fail-closed vérifié ; zéro requête hors tunnel.
- [ ] Egress : zéro faux `verified` en chaos.
- [ ] Ladder de transports : WG→UDP→TCP→retour WG, un seul tunnel, zéro
      recréation Compose, `VPN_TYPE` explicite inchangé.
- [ ] Documentation et runbook à jour.
