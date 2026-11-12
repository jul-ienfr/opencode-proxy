# Audit — fork Gluetun et intégration au plan de contrôle

**Date** : 2026-11-11
**Périmètre** : fork `jul-ienfr/gluetun`, upstream `passteque/gluetun`
(`master`, Go 1.26), serveur de contrôle, DNS, vérification d'IP publique,
catalogue serveurs, boucle VPN, et intégration Python `opencode-proxy-one`.
**Méthode** : lecture du code, comparaison GitHub, lecture de la configuration et
des adapters du projet, revue de l'écosystème public.
**Règle** : une IP n'est réputée bonne qu'après un test de cette IP ; le quota
free est par IP, jamais global ni compte.

---

## 1. Résumé exécutif

Le fork est **strictement identique** à l'upstream : aucune adaptation locale,
aucun commit propre. L'upstream est déjà un excellent **agent de tunnel** :
sélection statique riche, catalogue provider, DNS DoT/DoH/plain, IP publique,
firewall, proxies, port forwarding, healthcheck, Prometheus, serveur de contrôle
authentifié.

Mais il n'a **pas de plan de contrôle** :

1. Aucune API de rotation ou de changement d'IP ; la rotation n'existe que comme
   effet de bord d'un redémarrage interne.
2. `PUT /v1/vpn/settings` fait un stop/start brutal, sans préflight, sans
   rollback, sans preuve, et répond en texte brut.
3. Les settings sont éphémères : un redémarrage réapplique Compose.
4. Le DNS est mono-étage : si DoT est bloqué, le resolver boucle au lieu de
   basculer vers plain.
5. Le consensus d'IP publique porte sur la géolocalisation, pas sur la valeur
   d'IP ; l'IP de l'hôte peut être acceptée pendant une fenêtre de tunnel down.
6. Aucun contrat d'orchestration : le projet parse `running`, fait des
   `docker exec`, blackliste des hostnames côté Python et recrée le conteneur en
   dernier recours.
7. Aucun ladder de transports : un seul `VPN_TYPE` est actif ; passer de
   WireGuard à OpenVPN UDP/TCP exige un stop/start, sans breaker, sans preuve
   par transport et sans retour automatique au premier.

Le plan 016 ajoute les cinq primitives manquantes (SwitchJob, rotation, DNS
ladder, egress verification, transport ladder) et un contrat HTTP stable, sans
déplacer le quota, le claim ou le score hors de `registry.db`.

## 2. État du fork

| Élément | Constat | Preuve |
|---|---|---|
| Fork | `jul-ienfr/gluetun` | GitHub |
| Upstream | `passteque/gluetun:master` | GitHub |
| Comparaison | `identical`, 0 ahead / 0 behind | GitHub compare, 2026-11-11 |
| Commits propres | 0 | `git log` upstream == fork |
| Langage | Go 1.26 | `go.mod` |
| Release | `v3.41.3` (2026-07-30) | releases |
| Dernier push | 2026-11-11 | API |

**Conclusion** : toute extension doit être créée dans le fork ; il n'y a rien à
préserver localement. Le risque principal est la divergence lors des merges
upstream, à contenir par des modules isolés et des tests de contrat.

## 3. Matrice de capacités

| Capacité requise | Upstream | Écart | Priorité |
|---|---|---|---|
| Sélection pays/région/ville/ISP/hostname/numéro/catégorie/tier | Oui (`ServerSelection`) | API de lecture/facettes absente | P1 |
| IP d'endpoint explicite | Oui (`*_ENDPOINT_IP`) | Validation d'appartenance au hostname absente | P1 |
| IP de sortie attendue | Non | Champ et vérification absents | P1 |
| Rotation pilotée | Non (effet de bord) | Aucun job, historique, politique | P1 |
| Bascule transactionnelle | Non | Stop/start nu, pas de rollback | P1 |
| Préflight avant coupure | Non | Aucune validation de joignabilité | P1 |
| Drain des connexions | Non | Aucun drain | P2 |
| Idempotence | Non | Aucune clé, aucun journal | P2 |
| Persistance d'état | Non | Settings en mémoire | P1 |
| DNS multi-étages | Non (un seul type) | Ladder + santé absents | P1 |
| Ladder de transports WG/UDP/TCP | Non (un seul `VPN_TYPE`) | Aucun ordre, breaker, backoff ni retour au premier | P1 |
| DNS fail-closed | Partiel | Pas de health par étage | P1 |
| Consensus sur la valeur d'IP | Non (géoloc) | Consensus IP absent | P1 |
| Exclusion de l'IP d'hôte | Non | Capture underlay absente | P1 |
| Canary 200 avec contenu | Non | Aucun canary | P1 |
| Catalogue API/facettes | Non | Storage interne seulement | P2 |
| Événements/SSE | Non | Aucun bus | P2 |
| Scopes lecture/écriture | Non | Rôles par route seulement | P2 |
| Masquage des secrets | Non | `GET settings` expose tout | P1 |
| Métriques par job/tier/source | Partiel | Métriques DNS/IP existantes, pas par job | P2 |
| Rate limit / taille de corps | Non | Aucun | P2 |
| Rétro-compatibilité | Oui | À préserver | P0 |

## 4. Findings détaillés

### A-01 — Aucune API de rotation (P0)

**Constat** : la rotation est un effet de bord de `internal/vpn/loop.go` : après
un redémarrage, la sélection est relue et un serveur est choisi. Il n'existe ni
`POST /v1/vpn/rotate`, ni job, ni historique, ni budget, ni preuve.

**Impact** : l'orchestrateur doit recréer le conteneur ou manipuler les settings,
ce qui provoque des coupures et des courses. Impossible de garantir « tourner
jusqu'à une IP prouvée ».

**Correctif** : `POST /v1/vpn/rotate` + exécuteur de candidats (Phase 4).

### A-02 — Bascule non transactionnelle (P0)

**Constat** : `internal/vpn/state/vpn.go` :

```go
settingsUnchanged := reflect.DeepEqual(s.vpn, vpn)
...
s.vpn = vpn
_, _ = s.statusApplier.ApplyStatus(ctx, constants.Stopped)
outcome, _ = s.statusApplier.ApplyStatus(ctx, constants.Running)
```

Aucun préflight, aucun snapshot persistant, aucun rollback, aucune attente de
tunnel, aucune vérification d'egress. Les erreurs d'arrêt sont ignorées.

**Impact** : une cible invalide coupe le tunnel ; un serveur mort laisse la
station down ; l'ancien état n'est pas restauré.

**Correctif** : SwitchJob avec phases et rollback (Phase 3).

### A-03 — Settings éphémères (P0)

**Constat** : les settings vivent en mémoire dans `internal/vpn/state`. Un
redémarrage de conteneur réapplique l'environnement Compose.

**Impact** : toute rotation appliquée par l'API est perdue au restart ; le pin
pays/hostname de l'orchestrateur est écrasé.

**Correctif** : `DesiredState` atomique + `CONTROL_STATE_MODE` (Phase 3).

### A-04 — Pas de sélection par IP de sortie (P1)

**Constat** : `*_ENDPOINT_IP` désigne l'IP du serveur VPN. Rien ne permet
d'exiger une IP de sortie, de vérifier qu'elle a changé, ni d'exclure un /24.

**Impact** : la rotation peut « réussir » sans changer l'IP de sortie ; le
dashboard et `registry.db` ne peuvent pas prouver le changement.

**Correctif** : `expected_egress_ip`, `require_new_ip`, `require_new_subnet`,
vérification consensus (Phases 1 et 4).

### A-05 — DNS mono-étage (P0)

**Constat** : `settings.DNS.UpstreamType` est unique (`dot`, `doh` ou `plain`).
`buildServerSettings` construit un seul dialer. Aucune santé par fournisseur ni
fallback inter-types. `qdm12/dns/v2` choisit une adresse dans une liste du même
type, pas un étage différent.

**Impact** : sur le réseau de l'opérateur, DoH est bloqué ; si DoH est configuré,
la station échoue en boucle. Un fournisseur DoT tombé dégrade tout l'étage.

**Correctif** : DNS ladder (Phase 2), défaut flotte `dot,plain`.

### A-06 — Consensus d'IP publique inadapté (P0)

**Constat** : `internal/publicip/api/resilient.go::getMostPopularResult` filtre
par pays, région, ville (Levenshtein ≤ 1) puis choisit le plus populaire. Les
autres champs, dont l'IP, sont ignorés.

**Impact** : deux IP différentes dans la même ville passent le « consensus » ;
une source menteuse ou l'IP de l'hôte peut être retenue. Violation de la
constitution VII.

**Correctif** : consensus k-of-n sur la valeur d'IP (Phase 1).

### A-07 — IP d'hôte acceptée (P0)

**Constat** : `ResilientFetcher` accepte les fetchers `CanFetchAnyIP()` ; si le
tunnel est down, ils renvoient l'IP de l'hôte. Aucune capture d'underlay au boot,
aucune exclusion.

**Impact** : une station peut être déclarée « vérifiée » avec l'IP de l'hôte,
c'est-à-dire sans tunnel. C'est le faux positif le plus dangereux.

**Correctif** : capture de l'IP d'hôte via les interfaces + rejet dur (Phase 1).

### A-08 — Secrets exposés et scopes absents (P1)

**Constat** : `GET /v1/vpn/settings` sérialise tous les settings, credentials
inclus. Les rôles sont par route (`apikey`/`basic`/`none`) sans distinction
lecture/écriture ; `validRoutes` ne connaît pas de notion de scope.

**Impact** : un rôle lecteur peut lire les credentials ; l'API de contrôle
augmente la surface sans granularité.

**Correctif** : masquage par défaut, `reveal=true` admin, scopes
`reader/controller/admin` (Phase 5).

### A-09 — Aucun journal, idempotence ou événement (P1)

**Constat** : aucune clé d'idempotence, aucun journal de jobs, aucun flux
d'événements. `PUT settings` répond en texte brut.

**Impact** : les retries de l'orchestrateur créent des bascules multiples ; le
dashboard et `registry.db` ne peuvent pas suivre les transitions ; pas de reprise
après crash.

**Correctif** : journal de jobs, idempotence 24 h, SSE (Phases 3 et 5).

### A-10 — Pas de préflight ni de drain (P1)

**Constat** : aucune résolution/joignabilité avant coupure ; aucun drain des
connexions proxy avant arrêt.

**Impact** : coupure sur un hostname inexistant ou un endpoint injoignable ;
requêtes en cours interrompues brutalement.

**Correctif** : préflight complet (Phase 3), drain borné optionnel (Phase 3).

### A-11 — Catalogue non exposé (P2)

**Constat** : `models.Server` et le storage provider contiennent les facettes,
mais aucune route `/v1/servers` ni validation d'IP d'endpoint par hostname.

**Impact** : l'orchestrateur doit lire le catalogue autrement ; les erreurs de
cible ne sont détectées qu'après la coupure.

**Correctif** : API catalogue + facettes + validation (Phase 4).

### A-12 — Chemin chaud côté projet fragile (P0)

**Constat** : `vpn/manager.py` utilise `_control_exec` avec `docker exec wget`,
parse le corps texte `running`, blackliste les hostnames côté Python, et recrée
le conteneur Compose en dernier recours
(`free/ip_registry/adapters/gluetun_control.py`).

**Impact** : couplage fort, dépendance à Docker, parsing fragile, doubles
décisions. C'est la source des courses avec le superviseur.

**Correctif** : client HTTP typé + feature flag + shadow (Phase 6).

### A-13 — Double propriété de la rotation (P0)

**Constat** : le projet rotate déjà (`free/rotation.py`, superviseur) et le fork
re-sélectionne au redémarrage. Deux propriétaires, deux horloges.

**Impact** : une rotation décidée par le Python peut être annulée par un
redémarrage interne du fork, et inversement.

**Correctif** : frontière D1/D12 ; le fork exécute, l'orchestrateur décide ;
décommissionnement explicite du chemin chaud (Phase 6).

### A-14 — Aucune limite de corps/rate (P2)

**Constat** : le serveur de contrôle ne limite ni la taille des corps, ni le taux
de `PUT settings`, ni les timeouts de handlers au-delà du serveur HTTP.

**Impact** : surface DoS interne, boucles de bascules.

**Correctif** : limites et rate-limit (Phase 5).

### A-15 — Tests upstream centrés sur le tunnel (P2)

**Constat** : les tests couvrent settings, DNS, public IP, serveur ; il n'existe
aucun test de job, de rotation, de consensus IP ou de failover DNS multi-étages.

**Impact** : toute régression du plan de contrôle serait invisible.

**Correctif** : tests unitaires/contrat/intégration listés dans `tasks.md`.

### A-16 — Aucun ladder de transports (P1)

**Constat** : `settings.VPN.Type` vaut `openvpn` **ou** `wireguard` ;
`OpenVPN.Protocol` vaut `udp` **ou** `tcp`. Changer de transport passe par
`SetSettings` → `ApplyStatus(Stopped)` → `ApplyStatus(Running)`, sans breaker,
sans budget, sans raison typée, sans preuve d'egress par transport et sans
retour automatique au premier. Le ladder existant (`free/transport_ladder.py`)
vit côté Python et pilote Docker.

**Impact** : un réseau qui bloque UDP laisse la station en échec jusqu'à une
intervention ; la bascule coûte des dizaines de secondes et parfois une
recréation de conteneur ; deux autorités peuvent piloter le tunnel.

**Correctif** : `internal/vpn/transport` (Phase 2b) : ordre
`wireguard,openvpn-udp,openvpn-tcp`, un seul tunnel, breaker/backoff, retour à
`policy[0]`, événements et endpoints `GET /v1/vpn/transport` /
`POST /v1/vpn/transport/reset`.

## 5. Audit de l'écosystème (synthèse)

| Projet | Pattern retenu | Décision |
|---|---|---|
| `tundler` | Contrat d'IP de sortie, drain, `/rotate`, watchdog | Reprendre le contrat et le drain borné. |
| `tunnelhop` | Rotation jusqu'à changement de /24, historique | Reprendre `require_new_subnet` et le budget. |
| `gluetun-webui` | Monitoring multi-instances | Le dashboard existant suffit ; consommer les événements. |
| `gluetun-ip-rotator` / `gluetun-rotate` | Rotation périodique | Ne pas reprendre la recréation de conteneur. |
| `wg-easy` | UI, 2FA, OIDC | Reprendre le modèle de scopes, pas l'UI. |
| `vopono` | Namespaces par app | Hors périmètre. |
| `mihomo` | Fallback/load-balance, DNS health | Reprendre le ladder DNS et transport, la santé. |
| `AdGuardHome` / `dnscrypt-proxy` / `smartdns` | Upstreams multiples, fallback | Reprendre la sélection par requête et le cache négatif. |

Détail complet : `research.md` §3.

## 6. Audit de l'intégration Python

| Fichier | Rôle actuel | Risque | Cible 016 |
|---|---|---|---|
| `vpn/manager.py` | Rotation, pin, watchdog, `_control_exec` | Parsing texte, `docker exec`, recréation | Client HTTP, jobs, événements. |
| `free/transport_ladder.py` | Cascade WG/UDP/TCP côté Python | Chemin chaud Docker, deux autorités, pas de preuve d'interface | Shadow puis décommissionné ; ladder dans le fork. |
| `free/rotation.py` | Sélection/scoring | Décide sans preuve locale | Fournit une liste ordonnée au fork. |
| `free/station_health.py` | `client_usable` | Verdict dupliqué si le fork en produit un | Reste l'autorité. |
| `free/ip_registry/adapters/gluetun_control.py` | `PUT settings` via Docker | Chemin chaud fragile | Legacy froid ; remplacé par `gluetun_control_v2.py`. |
| `free/ip_registry/adapters/egress.py` | 5 couches de détection | Lent, non consensus | Utilise `/v1/egress/verify` + garde ses couches comme secours. |
| `dashboard/routers/station_control_routes.py` | API station | Actions directes | Consomme jobs/événements. |
| `config.yaml` | 6 stations, DNS, probes | À préserver | Ajoute `control_plane.*`, `DNS_LADDER`. |
| `docker-compose.yml` | 6 actifs, 4 dormants | Ne pas réactiver | Change l'image/digest et les variables. |

## 7. Backlog priorisé

### P0 — sans quoi la feature n'a pas de sens

| ID | Finding | Tâche |
|---|---|---|
| B-01 | Bascule transactionnelle + rollback | T030–T039 |
| B-02 | Preuve d'egress consensus + canary | T020–T029 |
| B-03 | DNS ladder fail-closed | T040–T049 |
| B-04 | État désiré persistant | T031–T033 |
| B-05 | Rotation jusqu'à preuve, `exhausted` local | T050–T057 |
| B-06 | Chemin chaud Python migré | T070–T079 |

### P1 — cible de la feature

| ID | Finding | Tâche |
|---|---|---|
| B-07 | Catalogue API/facettes | T051–T053 |
| B-08 | Idempotence, jobs, cancel | T034–T036 |
| B-09 | Scopes + masquage | T060–T062 |
| B-10 | Événements/SSE et ingestion | T063–T066 |
| B-11 | Préflight et drain | T032, T037 |
| B-12 | Métriques et capabilities | T067–T068 |
| B-17 | Ladder de transports WG→UDP→TCP, breaker, retour au premier | T090–T099 |

### P2 — robustesse

| ID | Finding | Tâche |
|---|---|---|
| B-13 | Rate limit / taille de corps | T069 |
| B-14 | Rétention et rotation des journaux | T064 |
| B-15 | Tests de chaos et soak | T080–T083 |
| B-16 | Documentation et runbooks | T084–T085 |

## 8. Risques transverses

| ID | Risque | Mitigation |
|---|---|---|
| R-01 | Divergence upstream | Modules isolés, patchs minimaux, contrats, rebase trimestriel. |
| R-02 | Limites de `qdm12/dns/v2` | Ladder au-dessus de `server.Dialer` ; fork local testé si nécessaire. |
| R-03 | Latence de failover DNS | Timeout 800 ms/étage, health passive, cache négatif. |
| R-04 | Rollback vers serveur mort | Préflight du snapshot ; `failed_degraded` sinon. |
| R-05 | State corrompu | Écriture atomique, checksum, fallback env. |
| R-06 | Fuite de secrets | Masquage, `reveal` admin, tests de sérialisation. |
| R-07 | Double propriété | Frontière D1/D12, shadow, décommissionnement. |
| R-08 | Régression 015 | Rollout 1→6, parité, rollback digest. |
| R-09 | Ladder qui boucle ou double le tunnel | Verrou de boucle, teardown explicite, `max_cycles`, test `ip link`, backoff borné. |

## 9. Conclusion

Le fork n'est pas à réécrire : il est à **compléter**. Les briques existantes
(serveur, catalogue, tunnel, DNS, public IP) servent de fondation ; les ajouts
sont des modules isolés et un contrat HTTP. Le gain attendu est double :
supprimer le chemin chaud `docker exec`/parsing côté Python, et rendre la
rotation/changement d'IP **prouvé, réversible et observable** sur les six
stations, sans jamais sortir du free ni créer une septième session.
