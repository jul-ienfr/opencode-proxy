# Research — plan de contrôle Gluetun

**Date** : 2026-11-11
**Feature** : `016-gluetun-control-plane`
**Méthode** : lecture du code upstream `passteque/gluetun` (master), comparaison
GitHub du fork `jul-ienfr/gluetun`, lecture du code Python
`opencode-proxy-one`, audit de l'écosystème public, mesures réseau locales.
**Règle** : chaque affirmation sur une IP vient d'un test de cette IP ; le quota
free est **par IP**, jamais global ni compte.

---

## 1. État du fork et de l'upstream

| Élément | Valeur | Preuve |
|---|---|---|
| Fork | `jul-ienfr/gluetun` | GitHub |
| Upstream | `passteque/gluetun` (ex-`qdm12/gluetun`) | GitHub |
| Comparaison | `identical`, 0 commit d'avance, 0 de retard | GitHub compare, 2026-11-11 |
| Branche | `master` | GitHub |
| Langage | Go 1.26 | `go.mod` |
| Dernier release | `v3.41.3` (2026-07-30) | GitHub releases |
| Dernier push master | 2026-11-11 | GitHub API |
| Étoiles | ~16 050 | GitHub |

**Conséquence** : il n'y a aucune adaptation locale à préserver. Toute extension
doit être créée dans le fork ; le plan doit minimiser les divergences pour
faciliter les rebases trimestriels.

### Recensement des forks (2026-11-11)

| Requête | Résultat |
|---|---|
| `GET /repos/passteque/gluetun` | 16 053 étoiles, 647 forks, dernier push 2026-11-11 |
| Forks triés par étoiles (top 15) | maximum 7 étoiles ; le premier est un fork PIA de 2018 |
| Fork avec plan de contrôle, ladder DNS/transport ou preuve d'egress | aucun |
| `jul-ienfr/gluetun` | 0 étoile, `identical`, push aligné sur l'upstream |

**Conséquence** : le fork n'est pas en retard et il n'existe pas, dans les 647
forks, une variante à récupérer. Les forks à 5–7 étoiles sont des éditions
fournisseur (`gluetun-airvpn-edition`) ou des dépôts abandonnés, pas des plans
de contrôle. Toute extension 016 reste à créer dans `jul-ienfr/gluetun`.

### Carte du code upstream pertinente

```text
cmd/gluetun/main.go
internal/
├── configuration/settings/
│   ├── dns.go                  # DNS struct + validation
│   ├── serverselection.go      # ServerSelection (pays, villes, ISP, tiers…)
│   ├── vpn.go                  # VPN settings
│   └── wireguardselection.go
├── dns/
│   ├── run.go                  # boucle de démarrage du serveur DNS
│   ├── settings.go             # buildServerSettings (dialers, middlewares)
│   ├── state/                  # SetSettings
│   └── status.go
├── publicip/
│   ├── loop.go                 # boucle de rafraîchissement
│   └── api/
│       ├── resilient.go        # fetchers, ban 30 j, getMostPopularResult
│       ├── multi.go            # fetch multi-IP
│       └── {cloudflare,ipinfo,ip2location,echoip}.go
├── server/
│   ├── handler.go              # montage v0/v1 + middlewares
│   ├── handlerv1.go            # /version, /vpn, /dns, /publicip, /updater, /portforward
│   ├── vpn.go                  # GET/PUT status, GET/PUT settings
│   ├── dns.go                  # GET/PUT dns
│   ├── publicip.go
│   ├── middlewares/auth/       # apikey/basic/none, rôles par route
│   └── middlewares/log/
├── vpn/
│   ├── loop.go                 # boucle VPN, re-sélection
│   ├── state/vpn.go            # SetSettings → Stop puis Run
│   └── openvpn.go, wireguard.go
├── provider/nordvpn/           # catalogue + updater
└── models/server.go            # Server, ServerSelection
```

## 2. Inventaire des capacités upstream

### 2.1 Sélection statique

`inner/configuration/settings/serverselection.go` expose :

```text
VPN, Mode (random|ordered), Countries, Categories, Regions, Cities, ISPs,
Names, Numbers, Hostnames, OwnedOnly, FreeOnly, PremiumOnly, StreamOnly,
MultiHopOnly, PortForwardOnly, SecureCoreOnly, TorOnly,
OpenVPNSelection, WireguardSelection
```

C'est déjà riche : pays, région, ville, ISP, nom, numéro, hostname, catégorie,
tier, owned/free/premium/stream/multihop/port-forward/secure-core/tor. Le fork
n'a donc **pas** à réinventer le catalogue ; il doit l'exposer en lecture, le
valider et accepter une IP d'endpoint explicite.

### 2.2 IP d'endpoint

`OPENVPN_ENDPOINT_IP` et `WIREGUARD_ENDPOINT_IP` fixent l'IP **du serveur VPN**,
pas l'IP de sortie. Il n'existe aucun champ `expected_egress_ip`. C'est une
confusion fréquente dans les demandes de rotation : « changer d'IP » signifie
changer de serveur, donc d'IP de sortie observée.

### 2.3 Rotation

La rotation upstream est un **effet de bord** : la boucle VPN relit les settings
à chaque itération et re-sélectionne un serveur après un redémarrage interne.
Aucun job, aucun historique, aucune politique, aucune preuve d'IP, aucun budget
de candidats.

### 2.4 DNS

`internal/configuration/settings/dns.go` :

```go
const (
    DNSUpstreamTypeDot   = "dot"
    DNSUpstreamTypeDoh   = "doh"
    DNSUpstreamTypePlain = "plain"
)

type DNS struct {
    ServerEnabled *bool
    UpstreamType  string
    Providers     []string
    Caching       *bool
    UpstreamPlainAddresses []netip.AddrPort
    PublicNamesAsLocal []string
    PublicNameserverCIDRsAsLocal []netip.Prefix
    ...
}
```

Un **seul** `UpstreamType` par instance. `Providers` est une liste de
fournisseurs du même type. `buildServerSettings` construit un `server.Dialer`
unique (DoT, DoH ou plain) et l'entoure des middlewares cache/filter/localdns.
`qdm12/dns/v2` fournit des dialers avec un picker parmi les adresses, mais pas de
fallback inter-types ni de santé par étage. Si DoT est bloqué, la boucle échoue
et redémarre avec backoff ; elle ne bascule pas vers plain.

### 2.5 IP publique

`internal/publicip/api/resilient.go` :

- `ResilientFetcher` interroge en parallèle tous les fetchers non bannis.
- Ban 30 jours sur `429` (`const banDuration = 30 * 24 * time.Hour`).
- `getMostPopularResult` filtre successivement par **pays**, **région**, puis
  **ville** (avec distance de Levenshtein ≤ 1 sur les noms), puis choisit le
  résultat le plus populaire.
- **Aucun consensus sur la valeur d'IP** ; les autres champs sont ignorés dans
  la comparaison.
- Un fetcher qui `CanFetchAnyIP()` peut renvoyer l'IP de l'hôte si le tunnel est
  down ; rien ne rejette cette valeur.

C'est le point le plus dangereux pour la contrainte « aucune preuve, aucun
succès » (constitution VII) : une IP d'hôte peut être acceptée comme IP de
sortie.

### 2.6 Serveur de contrôle

Routes v1 : `/version`, `/v1/vpn/*`, `/v1/openvpn/*`, `/v1/dns/*`,
`/v1/updater/*`, `/v1/publicip/*`, `/v1/portforward/*`.

`internal/server/vpn.go` :

- `GET /v1/vpn/status` → `{"status":"running"}`.
- `PUT /v1/vpn/status` → applique `running|stopped`.
- `GET /v1/vpn/settings` → sérialise **tous** les settings, credentials inclus.
- `PUT /v1/vpn/settings` → décode un `settings.VPN`, `OverrideWith`, `Validate`,
  puis `SetSettings`, et répond en **texte brut** (`outcome`).
- `internal/vpn/state/vpn.go` : `SetSettings` compare avec `reflect.DeepEqual`,
  remplace les settings, puis `ApplyStatus(Stopped)` et `ApplyStatus(Running)`.
  Pas de transaction, pas de préflight, pas de rollback, pas d'attente de preuve.

Auth : rôles par route (`apikey`, `basic`, `none`), liste `validRoutes`,
middleware qui renvoie 401/404/405. Pas de notion de scope lecture/écriture :
un rôle qui peut lire les settings peut aussi les modifier si la route lui est
accordée.

Persistance : les settings vivent **en mémoire**. Un redémarrage de conteneur
réapplique l'environnement Compose et perd tout réglage appliqué par l'API.

### 2.7 Catalogue provider

`internal/provider/nordvpn/updater/*` télécharge et stocke les serveurs.
`models.Server` porte hostname, nom, numéro, pays, région, ville, ISP,
catégories, tier, IPs, protocoles, clé WireGuard. Le catalogue est donc
disponible localement ; il manque seulement une API de lecture/facettes et une
validation d'IP d'endpoint par hostname.

### 2.8 Ladder de transports

`internal/vpn/state/vpn.go` applique **un seul** `VPNType` (`openvpn` ou
`wireguard`) et un `OpenVPN.Protocol` (`udp` ou `tcp`). `SetSettings` avec un
autre type arrête puis redémarre la boucle ; il n'existe :

- aucune notion de **liste ordonnée** de transports ;
- aucun **breaker** par transport, aucun backoff typé ;
- aucune **preuve d'egress** attachée au transport ;
- aucun **retour automatique** au premier transport après un cycle ;
- aucun **watchdog local** distinguant « tunnel mort » de « transport bloqué ».

Côté projet, `free/transport_ladder.py` implémente déjà l'idée mais dans
Python : il pilote `docker exec`/`PUT settings`, recrée parfois Compose, et ne
peut pas prouver finement l'état de l'interface. Le déplacer dans le fork
supprime ces allers-retours, garantit « un seul tunnel » par verrou local et
permet une bascule en secondes.

## 3. Audit de l'écosystème

| Projet | Ce qu'il apporte | Ce qu'on retient | Ce qu'on ne reprend pas |
|---|---|---|---|
| `Sir-Scuzza/gluetun-webui` | Monitoring multi-instances, statut, logs | Vue flotte, events, boutons d'action | UI lourde ; le dashboard existe déjà. |
| `NoelisTired/tunnelhop` | Rotation jusqu'à changement de /24, API key, historique | Contrainte `require_new_subnet`, boucle jusqu'à preuve | Rotation qui s'arrête sans budget ni `exhausted`. |
| `zerofelx/gluetun-ip-rotator` | Rotation périodique simple | Idée de planification | Pas de preuve ni de rollback. |
| `PilaScat/gluetun-rotate` | Rotation par redémarrage de conteneur | Simplicité | Recréation de conteneur = coupure et perte d'état. |
| `laurentpellegrino/tundler` | Contrat d'IP de sortie, drain, `/rotate`, watchdog | Test de contrat egress, drain borné, `/rotate` | Dépendance à un orchestrateur externe. |
| `quaywin/hermit` | Orchestrateur multi-tunnels en netns, profils entrants/sortants, DNS de filtrage | Séparation profils/instances, DNS par tunnel | Pas d'auth fournisseur grand public, pas de rotation ni de quota, pas de preuve d'egress ; 11 étoiles. |
| `lasermaze/vpn-rotation-service` | Rotation par provisioning de VPS et bascule DNS | Rotation sans changer la configuration client | Rotation d'infrastructure auto-hébergée (Xray) ; ne pilote pas des IP de sortie de fournisseur VPN. |
| `92jackson/WireGuard-...-Failover-and-Auto-Rotation` | Failover WireGuard sur OpenWrt | Watchdog de lien | Niveau routeur, mono-transport, sans catalogue fournisseur. |
| `wg-easy` | UI, 2FA, OIDC, gestion de clients | Modèle de scopes et de secrets | Hors périmètre VPN provider. |
| `vopono` | Namespaces réseau par application | Isolation par processus | Ne s'applique pas à un conteneur unique. |
| `MetaCubeX/mihomo` | Fallback/load-balance, DNS multi-upstream, health | Ladder DNS, health checks, groupes de fallback | Trop large ; on garde un ladder borné. |
| `AdGuardHome` | Upstreams multiples, fallback, load balancing | Plusieurs résolveurs par étage, santé | Serveur DNS complet non nécessaire. |
| `dnscrypt-proxy` | Fallback, load balancing, cache, sources | Sélection par requête, cache négatif | Dépendance externe si le ladder suffit. |
| `smartdns` | Groupes d'upstreams, fastest-IP | Mesure de latence par upstream | Modification de réponse hors périmètre. |

**Synthèse** : les patterns utiles sont (a) contrat d'IP de sortie prouvé,
(b) rotation jusqu'à changement de sous-réseau avec budget, (c) DNS multi-étages
avec santé, (d) drain borné, (e) événements consommables. Les patterns à éviter
sont la recréation de conteneur comme mécanisme de rotation et la rotation sans
preuve.

**Verdict du recensement** : au 2026-11-11, aucun projet ni fork identifié ne
couvre mieux le besoin. `tundler` est le plus proche sur le contrat de rotation
(API `/rotate`, drain, IP de sortie, multi-comptes) mais n'a ni DNS multi-étages,
ni consensus egress multi-sources, ni ladder de transports, ni sélection
ville/hostname/IP ; 3 étoiles, 0 fork. `sing-box` et `mihomo` ont le failover et
le DNS mais pas l'authentification fournisseur, la rotation ni la preuve
d'egress. `hermit` orchestra des tunnels et un DNS de filtrage mais reste au
niveau netns, sans catalogue fournisseur. Les autres projets sont des plugins de
rotation par redémarrage, et les 647 forks de Gluetun n'en contiennent aucun avec
un plan de contrôle. Gluetun reste la seule base qui possède déjà le catalogue
serveurs, la sélection par ville/hostname, WireGuard + OpenVPN, un serveur de
contrôle et une communauté active ; 016 se construit donc dans le fork, en
s'inspirant des contrats de `tundler` et `tunnelhop`.

## 4. Audit de l'intégration côté projet

| Élément | État | Problème |
|---|---|---|
| `vpn/manager.py` (~591 Ko) | Rotation, pin pays, watchdog, cascade transports | `_control_exec` via `docker exec wget` ; parsing du corps texte `running` ; blacklist hostnames côté Python ; recréation Compose en dernier recours. |
| `free/rotation.py` | Sélection/scoring | Décide déjà quel candidat ; le fork ne doit pas re-scorer. |
| `free/station_health.py` | Santé, `client_usable` | Autorité du verdict ; à ne pas dupliquer. |
| `free/ip_registry/adapters/gluetun_control.py` | `PUT /v1/vpn/settings` via `docker cp` + `docker exec` | Chemin chaud fragile, lent, non transactionnel. |
| `free/ip_registry/adapters/egress.py` | 5 couches de détection d'IP | Utile comme source de secours ; à brancher sur `/v1/egress/verify`. |
| `dashboard/routers/station_control_routes.py` | `/api/vpn/station/{sid}/...` | Doit consommer les jobs et événements, pas `docker exec`. |
| `config.yaml` | `station_count: 6`, `target_slots: 6` | Contrainte dure à préserver. |
| `docker-compose.yml` | 6 actifs, 4 dormants (`7..10`) | Ne pas réactiver ; ne pas compter les dormants. |

## 5. Mesures et contraintes locales

- **DoH bloqué** sur le réseau de l'opérateur : `dns.google`,
  `cloudflare-dns`, `dns.quad9` expirent.
- **DoT et plain validés live**.
- **Décision** : ladder par défaut de la flotte `dot,plain` ; DoH disponible
  mais non prioritaire (`DNS_LADDER` configurable).
- **Canary local** : `vpn-wg-test` et le canary WireGuard fournissent un banc
  d'essai sans credentials réels.
- **Six stations** : `vpn-gluetun`..`vpn-gluetun-6` ; `7..10` restent dormants.
- **Quota** : free par IP ; les sondes du fork ne doivent pas appeler l'API
  modèle ; le canary est un endpoint neutre configurable.

## 6. Décisions de recherche

### D1 — Frontière fork/orchestrateur

**Décision** : le fork exécute une liste ordonnée de candidats et prouve
l'egress ; l'orchestrateur choisit, score, claim et quota.

**Raison** : constitution III (une vérité par cycle de vie) et IX (le score
ordonne, les portes dures décident). Un second classement dans Go créerait deux
autorités et des divergences impossibles à déboguer.

**Alternative rejetée** : porter le score composite dans le fork. Rejetée :
duplication de `registry.db`, dérive des poids GUI, double maintenance.

### D2 — Pas de double tunnel intra-conteneur

**Décision** : la bascule intra-station est un stop/start rapide, précédé d'un
préflight et suivi d'une preuve, avec rollback. Le make-before-break
inter-stations reste la feature 015 (standby/warm pool).

**Raison** : un conteneur Gluetun a un seul namespace réseau, un seul tunnel et
un firewall qui suppose une interface unique. Deux tunnels simultanés cassent
les routes, les proxies et le kill switch.

**Alternative rejetée** : lancer deux instances WireGuard/OpenVPN dans le même
conteneur. Rejetée : refonte réseau majeure, hors périmètre, risque de fuite.

### D3 — Machine à états et un seul job

**Décision** : un job actif par station, états explicites, journal append-only,
`supersede` réservé admin.

**Raison** : un seul tunnel ⇒ une seule opération de settings à la fois. Les
courses produisent des états inconnus et des rollbacks qui écrasent le mauvais
snapshot.

**Alternative rejetée** : jobs concurrents avec verrous par ressource. Rejetée :
complexité pour un bénéfice nul sur un tunnel unique.

### D4 — Persistance sans secrets en clair

**Décision** : `state.json` contient les settings non secrets, le serveur
effectif et les références d'environnement. Les secrets modifiés via l'API sont
chiffrés AES-256-GCM avec `CONTROL_SECRET_KEY` ; sans clé, la requête échoue en
`409 secret_persistence_unavailable`.

**Raison** : survivre au redémarrage sans écrire de credentials en clair ;
respecter NFR-003.

**Alternative rejetée** : persister tout le JSON de settings. Rejetée : fuite de
credentials sur disque et dans les backups.

### D5 — DNS ladder par requête

**Décision** : un listener, des étages ordonnés, sélection par requête, santé
active/passive, cache/filter au-dessus.

**Raison** : le réseau bloque DoH ; un mono-étage transforme un blocage en boucle
de healthcheck. Le cache et le filtre doivent rester uniques pour éviter les
incohérences.

**Alternative rejetée** : redémarrer le resolver avec un autre type. Rejetée :
coupure de 10–30 s, perte du cache, boucle de healthcheck.

### D6 — Consensus sur la valeur d'IP

**Décision** : k-of-n sur la valeur d'IP, avec dissidents, confiance et
exclusions.

**Raison** : le consensus upstream porte sur la géolocalisation ; deux IP
différentes dans la même ville passent. La constitution VII exige une preuve
d'IP réelle.

**Alternative rejetée** : conserver le consensus géographique et ajouter un
simple log. Rejetée : ne prouve pas l'IP de sortie.

### D7 — IP d'hôte capturée au boot

**Décision** : lire les interfaces au démarrage, conserver l'IP d'underlay et la
comparer à chaque observation.

**Raison** : un écho externo avant le tunnel renvoie l'IP d'hôte ; il faut la
connaître sans dépendre d'un service externe.

**Alternative rejetée** : capturer l'IP d'hôte via un écho public au boot.
Rejetée : dépendance réseau et faux positif si l'écho est biaisé.

### D8 — `exhausted` local

**Décision** : un budget épuisé renvoie `exhausted` avec la raison de chaque
candidat ; aucun état global « aucune IP disponible », aucune désactivation.

**Raison** : contrainte projet permanente ; la station reste réparable et les
autres stations ne sont pas contaminées.

**Alternative rejetée** : marquer la flotte indisponible. Rejetée : gèle les six
stations et viole la constitution X.

### D9 — Idempotence et événements

**Décision** : `Idempotency-Key` 24 h, journal de jobs, SSE avec `seq` et
`Last-Event-ID`.

**Raison** : les retries HTTP de l'orchestrateur ne doivent pas déclencher deux
bascules ; le dashboard et `registry.db` ont besoin d'un flux ordonné.

**Alternative rejetée** : pas d'idempotence, réponse uniquement synchrone.
Rejetée : doubles bascules sur timeout et impossible de suivre un job long.

### D10 — Scopes et masquage

**Décision** : `reader`, `controller`, `admin` par route ; `GET
/v1/vpn/settings` masque les secrets par défaut ; `reveal=true` admin.

**Raison** : la route existante expose déjà les credentials ; l'API de contrôle
augmente la surface. Le projet interdit déjà de l'appeler.

**Alternative rejetée** : réutiliser les rôles sans scopes. Rejetée : impossible
de donner un accès lecture seule au dashboard.

### D11 — Compatibilité additive

**Décision** : conserver `PUT /v1/vpn/settings` ; router vers SwitchJob si
`dry_run`, `wait` ou `require_verified` sont présents.

**Raison** : les clients existants et les tests upstream doivent continuer à
fonctionner ; la migration se fait par flag.

**Alternative rejetée** : remplacer la route. Rejetée : casse les clients, les
scripts et les tests de contrat.

### D12 — Intégration strangler

**Décision** : `control_plane.mode=legacy|shadow|active`, par station ; shadow
compare sans décider ; active bascule le chemin chaud.

**Raison** : permet un rollout 1→6 avec retour arrière immédiat et mesure de
parité. Le chemin legacy reste un secours froid.

**Alternative rejetée** : big-bang. Rejetée : risque sur les six stations et le
SLO 015.

### D13 — Ladder de transports dans le fork

**Décision** : `internal/vpn/transport` exécute un ladder ordonné
(`wireguard,openvpn-udp,openvpn-tcp`), un seul tunnel actif, breaker par
transport, backoff/jitter, retour à `policy[0]` après un cycle épuisé. Le ladder
ne change jamais de serveur, d'IP ou de hostname.

**Raison** : la bascule devient locale et rapide (ms/s au lieu de dizaines de
secondes), sans recréation de conteneur ; la station reste autonome si
l'orchestrateur est indisponible (NFR-009). Le choix de la cible reste à
l'orchestrateur (constitution IX), donc aucune double autorité.

**Alternative rejetée** : garder le ladder Python. Rejetée : chemin chaud
Docker, deux autorités, pas de preuve d'interface, et violation implicite de
« un seul tunnel » lors des recréations.

## 7. Questions résolues

| Question | Réponse |
|---|---|
| Le fork a-t-il des modifications locales ? | Non, identique à l'upstream. |
| Faut-il créer une API ou modifier Compose ? | Créer l'API dans le fork ; Compose ne change que l'image/digest et les variables. |
| Le fork doit-il choisir le pays/ville/IP ? | Il valide et exécute une cible ; l'orchestrateur choisit les candidats et leur ordre. |
| Peut-on faire deux tunnels dans un conteneur ? | Non (D2) ; le standby inter-stations est la feature 015. |
| Le DNS peut-il basculer sans redémarrer ? | Oui, au-dessus du listener, par requête (D5). |
| DoH doit-il être prioritaire ? | Non ; défaut `dot,plain`, DoH configurable. |
| Le fork doit-il connaître le quota ? | Non ; il reçoit un budget/lease borné et ne sonde pas l'API modèle. |
| Que faire si le rollback échoue ? | `failed_degraded`, événement critique, arrêt ; pas de boucle (D8, R-04). |
| Le fork doit-il choisir entre WireGuard, OpenVPN UDP et TCP ? | Oui, localement, via le ladder (D13) ; il ne choisit que le transport, jamais le serveur. |
| Que faire quand tous les transports échouent ? | `transport.cycle_exhausted`, backoff, reprise à `policy[0]` ; l'orchestrateur décide d'une rotation. |
| Peut-on garder le ladder Python en secours ? | Oui en `shadow` (observation), jamais en pilotage simultané ; puis décommissionné du chemin chaud. |

## 8. Références

- `https://github.com/passteque/gluetun` (master, Go 1.26, `v3.41.3`).
- `https://github.com/jul-ienfr/gluetun` (fork, identical).
- `github.com/qdm12/dns/v2` (pseudo-version
  `v2.0.0-rc9.0.20260909224732-d83961363ca2`).
- Projet : `specs/015-egress-uptime-slo/`, `.specify/memory/constitution.md`,
  `vpn/manager.py`, `free/ip_registry/*`, `config.yaml`, `docker-compose.yml`.
- Écosystème : `gluetun-webui`, `tunnelhop`, `gluetun-ip-rotator`,
  `gluetun-rotate`, `tundler`, `hermit`, `lasermaze/vpn-rotation-service`,
  `wg-easy`, `vopono`, `mihomo`, `sing-box`, `AdGuardHome`,
  `dnscrypt-proxy`, `smartdns`.
