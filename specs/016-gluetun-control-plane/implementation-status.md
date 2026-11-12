# Statut d'implémentation — 016

**Date** : 2026-11-12
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

## Phase 3 — État, reprise, préflight, jobs (partiel)

| Tâche | Statut | Preuve |
|---|---|---|
| T030 — schéma DesiredState | fait | `internal/control/state/desired.go` : schéma v1, `TunnelSettings` non secret (références `env:NAME`), checksum SHA-256 du JSON canonique, validation (schéma, station, mode, settings, snapshots, anneau d'observations), `Seal`, `Equal`, `mergedWith` ; `desired_test.go`. |
| T031 — écriture atomique et journal | fait | `internal/control/state/{atomic.go,journal.go,store.go}` : `tmp` + `Sync` + `rename`, mode 0600, `syncDirectory` tolérant Windows, journal append-only avec fsync, rotation par taille (`maxBytes`) et rétention (`maxFiles`), `Store.Save`/`Load` (`state.json`, `state.journal.jsonl`) ; `atomic_test.go`, `journal_test.go`, `store_test.go` (dont échec d'écriture et ancien état conservé). |
| T032 — snapshot et reprise | partiel | Types de snapshot dans `desired.go` (`TunnelSnapshot`, `DNSState`, `ResolverTier`) et `recovery.go` : `Recover` modes `state\|env\|merge`, décisions `state.absent\|recovered\|drift\|merged\|invalid`, état invalide jamais réécrit, station étrangère traitée en `drift` ; `recovery_test.go`. La restauration du snapshot est maintenant utilisée par `Rollback` ; la capture avant switch reste à câbler dans l'exécuteur (T038+). |
| T033 — préflight | fait | `internal/control/jobs/preflight.go` : interfaces étroites `Catalog`/`Resolver`/`Prober`, validation de la cible et du transport, credentials `OpenVPN`/`WireGuard`, protocoles du catalogue, résolution DNS, appartenance de l'`endpoint_ip` (résolution ∪ catalogue), sonde réseau ; erreurs typées `invalid_target`, `unknown_target`, `endpoint_ip_mismatch`, `transport_unsupported`, `missing_credentials`, `resolution_failed`, `endpoint_unreachable` ; timeout 10 s par défaut ; `preflight_test.go`. |
| T034 — machine à états | fait | `internal/control/jobs/{job.go,machine.go}` : `Job`/`Attempt`/`Timings` conformes au data model, 15 états, transitions exhaustives, invariants `committed`/`rolled_back` prouvés par une vérification d'egress, `Cancel` immédiat avant `applying` puis `cancel_requested`, `Interrupt` au redémarrage, timings par phase, `SetVerification` ; `machine_test.go` (transitions valides/invalides, invariants, annulation, reprise). |
| T035 — idempotence | fait | `internal/control/jobs/idempotency.go` : fenêtre 24 h par défaut, SHA-256 hexadécimal du corps, clé ≤ 128 caractères, `Reserve` atomique (même clé + même corps → job existant, corps différent → `ErrIdempotencyConflict`), purge à chaque accès, `Lookup`, `Forget` ; `idempotency_test.go` (rejeu, conflit, expiration, concurrence). |
| T036 — drain borné | fait | `internal/control/jobs/drain.go` : polling du compteur de connexions proxy, intervalle borné (timeout/20, 1 ms à 100 ms), attente plafonnée au deadline, `ErrDrainTimeout` + constante d'événement `drain.timeout`, annulation par contexte ; `drain_test.go` (fermeture, timeout exact, annulation, horloge injectée). |
| T037 — rollback et failed_degraded | fait | `internal/control/jobs/rollback.go` : ré-application du snapshot avant, attente du tunnel, une seule vérification egress fraîche, égalité stricte avec l'IP prouvée du snapshot, terminal `rolled_back` avec `Verification` enregistrée ou `failed_degraded` avec événement critique, aucune boucle ; `rollback_test.go` (succès depuis applying/awaiting/verifying/rolling_back, échec apply/await/verify, IP absente ou différente, snapshot absent, état de départ invalide, contexte annulé). |

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
  credentials et le catalogue, puis résout et sonde l'endpoint. Le vrai
  catalogue (T050) et le câblage dans le SwitchJob (T034/T038) restent à faire ;
  les interfaces `Catalog`/`Resolver`/`Prober` isolent ces dépendances.
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
  exposés comme constantes ; leur publication SSE sera branchée avec T063–T066.

## Vérifications

```text
go test ./internal/configuration/settings ./internal/control/... ./internal/server ./tests/integration/harness -> ok
go test ./internal/egress/... ./internal/control/... ./internal/server ./internal/configuration/settings -> ok
go test ./internal/control/state -count=1                                     -> ok
go test ./internal/control/jobs -count=1                                      -> ok
go test ./tests/integration -run Test_EgressChaos                             -> ok
go test ./internal/server/middlewares/auth -run 'Test_authHandler_ServeHTTP|Test_Role|Test_Settings' -> ok
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

`markdownlint-cli2` n'est pas installé dans l'environnement courant ; le
contrôle Markdown n'a donc pas été exécuté. Commande à lancer une fois
disponible : `markdownlint-cli2 "**/*.md"`.

`internal/server/middlewares/auth` passe mais échoue au nettoyage `TempDir` sous
Windows (fichier encore ouvert) : limitation de l'environnement, sans rapport
avec les changements.

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

## Activation

`CONTROL_PLANE_ENABLED=false` ne monte aucune route 016 et laisse les réglages
inertes. Les valeurs par défaut sont celles de l'ADR. `features` n'annonce que
les capacités implémentées : `egress_consensus` est `true` quand
`CONTROL_EGRESS_ENABLED=true` (défaut), les autres restent `false` jusqu'à leurs
phases.
