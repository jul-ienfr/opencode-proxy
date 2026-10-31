# WireGuard : sélection d'IP/serveur et alternatives — recherche GitHub

> 29/10/2026. Recherche centrée GitHub (dépôts, README, code source, wiki),
> complétée par les docs officielles. Aucun benchmark réseau live : les débits
> annoncés ne sont pas comparables. Les étoiles et dates sont des signaux de
> santé, pas des mesures de qualité.

## 1. Réponse courte

**Gluetun sait filtrer les serveurs WireGuard par hostname ; le transport
fonctionne à condition de forcer `WIREGUARD_MTU=1280`. Le pin runtime reste
fragile, mais les alternatives WireGuard sont viables.**

Gluetun lit bien `SERVER_HOSTNAMES` et son API de contrôle accepte un patch
`provider.server_selection.hostnames`. C'est **notre wrapper** qui supprime
volontairement les hostnames quand la station est en WireGuard :

```python
# vpn/manager.py:5114
# WireGuard: never pin hostnames (gluetun picks internally)
if getattr(self, "_stack_effective", None) == "wireguard":
    hostnames = []
```

Le test live du 29/10/2026 a montré qu'un couple pays/hostname invalide met le
looper Gluetun en état `crashed`, avec un HTTP 200 trompeur. Le canary
**au démarrage** applique bien `SERVER_HOSTNAMES=fr1004.nordvpn.com` ; sans MTU
explicite, le tunnel ne passe pas le healthcheck, mais avec
`WIREGUARD_MTU=1280`, l'IP publique arrive en ~2 s et la station est healthy.
Le facteur bloquant est donc le **MTU**, pas la sélection du serveur.

Pour une **IP exacte**, le provider `custom` reste la piste ; les alternatives
WireGuard-only sont viables avec `WIREGUARD_MTU=1280` :

- **alternative générale** : **sing-box** (ou **mihomo**) pour un remplacement
  générique tunnel + proxy, avec configuration WireGuard explicite ;
- **alternative WireGuard/IP** : **itsnocny/sas** (endpoint IP épinglé + liste +
  API de rotation) ou **azinchen/nordvpn-wg** (sélection par hostname, plus
  simple, mais sans proxy ni API de contrôle).

Aucun des deux n'est un drop-in : il faut déplacer le proxy dans le namespace du
VPN ou ajouter un sidecar HTTP/SOCKS.

## 2. Ce que Gluetun sait vraiment faire en WireGuard

### 2.1 Sélection par hostname

Le code de Gluetun lit bien `SERVER_HOSTNAMES` pour les deux types de VPN :

- `internal/configuration/settings/serverselection.go` :
  `ss.Hostnames = r.CSV("SERVER_HOSTNAMES", ...)` ;
- `internal/provider/utils/tier.go` : le filtre `selection.Hostnames` compare
  `server.Hostname` ;
- `internal/provider/nordvpn/updater/servers.go` : NordVPN crée une entrée
  `vpn.Wireguard` avec `Hostname`, `IPs` et `WgPubKey`.

Autrement dit, à la création du conteneur, `SERVER_HOSTNAMES=fr957.nordvpn.com`
devrait sélectionner ce serveur, y compris en WireGuard, puis utiliser l'IP
d'endpoint et la clé publique associées.

### 2.2 Changement à chaud

Le serveur de contrôle expose `PUT /v1/vpn/settings` ; le handler
`internal/server/vpn.go` décode un `settings.VPN`, applique `OverrideWith`, puis
valide et reconnecte. Le patch utilisé par notre code est de la forme :

```json
{"provider":{"server_selection":{"countries":["France"],"hostnames":["fr957.nordvpn.com"]}}}
```

La validation de `ServerSelection` accepte les hostnames. Il faut donc tester le
comportement réel de la version embarquée (v3.41.3) : succès, rejection
`choices/hostnames`, ou reconnexion sur un autre serveur.

### 2.3 Test live du pin runtime (29/10/2026)

Test effectué sur la station 6 (WireGuard, Gluetun v3.41.3), avec l'API de
contrôle :

1. `PUT /v1/vpn/settings` avec `countries=["France"]` et
   `hostnames=["de1217.nordvpn.com"]` → **HTTP 200**, corps `crashed` ;
   logs : `no server found: ... country France; hostname de1217...`.
   `de1217` est un serveur allemand, pas français : le couple pays/hostname
   était incohérent.
2. Nouvel essai avec `fr1004.nordvpn.com` + France → corps `already crashed` :
   le looper VPN était déjà en état de crash, le PUT n'a pas relancé la
   connexion.
3. `docker restart` de la station → retour à un tunnel sain après quelques
   dizaines de secondes (IP publique Suède).
4. Essai **valide** avec `countries=["Germany"]` et
   `hostnames=["de1217.nordvpn.com"]` → corps `running`. Gluetun a bien
   sélectionné `de1217`, puis le watchdog a basculé la station en OpenVPN
   (certificat `de1217.nordvpn.com`) et elle est revenue **healthy** avec une IP
   allemande.
5. Remise en état : `POST /api/vpn/station/6/stack {"stack":"wireguard"}` →
   retour en WireGuard, station **healthy**, IP publique Pays-Bas.

**Conclusion** : Gluetun **accepte** le champ hostname et sait filtrer, mais le
pin runtime est **fragile** : un couple pays/hostname invalide met le looper en
échec, et l'API renvoie `crashed` au lieu d'un rejet propre. Un couple valide
fonctionne, mais peut déclencher un **flip de stack** si WireGuard échoue au
healthcheck. Il ne faut donc pas supprimer le garde-fou `hostnames = []` sans
validation préalable et sans fallback pays. La piste la plus sûre est le **pin
au démarrage** avec un hostname valide, ou le provider `custom` avec une IP
exacte.

### 2.4 Canary boot-time (29/10/2026)

Test complémentaire avec le service canary `vpn-wg-test`, sans toucher aux
stations de production :

- s6 arrêtée pour libérer un slot NordVPN ;
- canary lancé avec `SERVER_COUNTRIES=France` et
  `SERVER_HOSTNAMES=fr1004.nordvpn.com` ;
- la configuration Gluetun confirme bien `Hostnames: fr1004.nordvpn.com` ;
- endpoint WireGuard : `109.69.110.140:51820` ;
- **premier essai sans `WIREGUARD_MTU`** : aucune IP publique, healthcheck egress
  en échec (`lookup github.com: i/o timeout`), redémarrages toutes les ~12 s ;
- **second essai avec `WIREGUARD_MTU=1280`** : IP publique
  `109.69.110.159` (France, Paris) en ~2 s, station **healthy** ;
- test avec `WIREGUARD_GSO=on` + MTU 1280 : également healthy. Le facteur
  déclenchant est donc le **MTU**, pas GSO.

**Conclusion** : le filtre hostname fonctionne bien **au démarrage** et le
transport WireGuard fonctionne quand le MTU est forcé à **1280**. Sans MTU
explicite, Gluetun peut auto-découvrir un MTU trop haut et casser DNS/egress.
Il faut donc garder `WIREGUARD_MTU=1280` sur les stations WG et vérifier ce
réglage avant d'incriminer NordVPN, Docker Desktop ou une alternative.

### 2.5 La vraie limite : le provider `custom`

Pour un endpoint WireGuard **arbitraire**, Gluetun exige une **IP**, pas un nom
de domaine :

```yaml
VPN_SERVICE_PROVIDER=custom
VPN_TYPE=wireguard
WIREGUARD_ENDPOINT_IP=1.2.3.4
WIREGUARD_ENDPOINT_PORT=51820
WIREGUARD_PUBLIC_KEY=...
WIREGUARD_PRIVATE_KEY=...
WIREGUARD_ADDRESSES=10.64.222.21/32
```

C'est la limite documentée dans le wiki (`setup/providers/custom.md`) :
`WIREGUARD_ENDPOINT_IP` doit être une IP, les domaines ne sont pas supportés.
Mais avec le provider **NordVPN**, on peut sélectionner un serveur par hostname ;
avec le provider **custom**, on peut épingler une IP exacte, au prix de la gestion
manuelle de la config et de la rotation.

**Test du 29/10/2026** : custom provider avec `fr1004` (endpoint
`109.69.110.140`, clé publique NordLynx, `WIREGUARD_MTU=1280`) → healthy en
~1 s, IP publique `109.69.110.159` (France). C'est la voie la plus fiable pour
une **IP exacte** avec le proxy intégré ; il reste à gérer la liste de serveurs
et la rotation côté wrapper.

## 3. Matrice des alternatives GitHub

| Projet | Sélection WireGuard | Proxy HTTP/SOCKS | Contrôle / rotation | NordVPN API | État | Verdict |
|---|---|---|---|---|---|---|
| **Gluetun** (`passteque/gluetun`) | hostname/pays via `SERVER_HOSTNAMES` ; IP exacte via provider `custom` | **HTTP + SOCKS5 + Shadowsocks** | API HTTP authentifiée, settings, IP publique | Oui | 15,9 k★, MIT, actif | **Référence ; à corriger plutôt qu'à remplacer** |
| **itsnocny/sas** | `VPN_ENDPOINT_IP` + `WIREGUARD_PUBLIC_KEY` (épinglé) ou `WIREGUARD_SERVERS_FILE` (liste) | Non (namespace) | API type Gluetun, `POST /v1/vpn/rotate`, failover | Oui (NordLynx) | 1★, MIT, **créé et poussé le 05/07/2026, 0 fork** | **Meilleur candidat IP WireGuard**, prototype non maintenu |
| **azinchen/nordvpn-wg** | hostname/pays/ville via `COUNTRY`, load balancing | Non (namespace) | reconnexion, healthcheck, `status.json` ; pas d'API settings | Oui (token) | 5★, AGPL, créé 2021, actif | Bon **NordVPN WireGuard**, pas de proxy/contrôle |
| **sing-box** | endpoint WireGuard explicite (IP/port/clé) | **HTTP/SOCKS/mixed** | routage, urltest, health ; pas d'API fournisseur | Non (config à générer) | ~39 k★, GPL-3.0, très actif | **Alternative générale** si refonte acceptable |
| **mihomo** (Clash Meta) | endpoint WireGuard explicite | **HTTP/SOCKS/mixed** | groupes `url-test`/`fallback`/`load-balance`, API REST | Non (config à générer) | ~35 k★, MIT, actif | Alternative générale orientée proxy groups |
| **wg-proxy-farm** | fichiers `.conf` (IP exacte) | **HTTP** (tinyproxy par namespace) | health checks, restart ; pas d'API de settings | Non | 2★, Apache-2.0 | Bonne **architecture ferme de proxies**, jeune |
| **edgd1er/nordlynx-transmission** | serveur NordVPN recommandé/sélectionné ; WireGuard ou client NordVPN | **HTTP + SOCKS5** | supervisor, healthcheck ; pas d'API de rotation | Oui (client) | 9★, pas de licence claire | Intéressant mais pas un client général |
| **edgd1er/nordlynx-proxy** | via client NordVPN, sélection `CONNECT` | **SOCKS5** | limité | Oui (client) | 49★, pas de licence claire | Ancien, pas de contrôle moderne |
| **jordanpotter/docker-wireguard** | `wg0.conf` (IP exacte) | Non | Aucun | Non | 163★, MIT | Brique simple, pas un remplacement |
| **Gluetun + provider custom** | `WIREGUARD_ENDPOINT_IP` (IP exacte) | HTTP/SOCKS intégrés | API Gluetun, mais config statique | Non (config manuelle) | stable | **Meilleur compromis IP exacte + proxy** |

## 4. Les deux recommandations demandées

### 4.1 Alternative générale : sing-box (ou mihomo)

**Pourquoi** : c'est le seul type de projet qui peut remplacer à la fois le tunnel
WireGuard et le proxy HTTP/SOCKS, avec plusieurs sorties et de la santé/load
balancing, sans dépendre d'un fournisseur.

**Ce qu'il faut accepter** :

- générer les configs NordLynx (endpoint IP, clé publique, clé privée, adresse) ;
- réécrire la sélection, la rotation, les métriques et l'API de contrôle ;
- pas d'OpenVPN ni d'API NordVPN : la liste de serveurs et la qualité par IP
  deviennent notre responsabilité ;
- un seul processus pour N tunnels = point de défaillance unique (ou N processus).

**Verdict** : bonne alternative **générale** si tu veux sortir de Gluetun et
garder un seul binaire ; mauvaise idée pour une migration immédiate. À tester sur
une station, pas sur les quatre.

**Test du 29/10/2026** : sing-box `1.14.4` démarre bien un inbound `mixed`
HTTP/SOCKS. Premier essai : handshake WireGuard en échec, car le trafic vers
l'endpoint partait dans le tunnel (boucle de routage). Avec un outbound `direct`
et une règle pour l'IP de l'endpoint, le handshake réussit et l'IP publique
`109.69.110.157` est obtenue :

```json
{
  "outbounds": [{"type": "direct", "tag": "direct"}],
  "route": {
    "rules": [
      {"ip_cidr": ["109.69.110.140/32"], "outbound": "direct"}
    ],
    "final": "wg-ep"
  }
}
```

C'est donc une alternative **viable**, mais pas un drop-in : il faut gérer cette
exception de routage, les clés, les endpoints et la rotation.

Recette minimale validée (clés à remplacer, ne pas committer) :

```json
{
  "log": {"level": "info"},
  "inbounds": [
    {"type": "mixed", "tag": "in", "listen": "0.0.0.0", "listen_port": 8899}
  ],
  "endpoints": [
    {
      "type": "wireguard",
      "tag": "wg-ep",
      "mtu": 1280,
      "address": ["10.5.0.2/32"],
      "private_key": "<NORDLYNX_PRIVATE_KEY>",
      "peers": [
        {
          "address": "109.69.110.140",
          "port": 51820,
          "public_key": "<NORDLYNX_PUBLIC_KEY>",
          "allowed_ips": ["0.0.0.0/0", "::/0"],
          "persistent_keepalive_interval": 25
        }
      ]
    }
  ],
  "outbounds": [{"type": "direct", "tag": "direct"}],
  "route": {
    "rules": [
      {"ip_cidr": ["109.69.110.140/32"], "outbound": "direct"}
    ],
    "final": "wg-ep"
  }
}
```

### 4.2 Alternative WireGuard/IP : itsnocny/sas (prototype) ou azinchen/nordvpn-wg

**itsnocny/sas** est le plus proche du besoin « je veux choisir l'IP » :

- mode **pinned** : `VPN_ENDPOINT_IP` + `WIREGUARD_PUBLIC_KEY` = une IP exacte ;
- mode **pinned list** : `WIREGUARD_SERVERS_FILE` = plusieurs endpoints, rotation ;
- mode **dynamic** : `SERVER_COUNTRIES` via l'API NordVPN ;
- API `POST /v1/vpn/rotate`, failover, kill switch nftables, image ~24 Mo.

Mais : **1 étoile, 0 fork, créé le 05/07/2026 et aucun commit depuis**, WireGuard
uniquement, **pas de proxy HTTP/SOCKS**, pas de healthcheck egress au niveau de
Gluetun. C'est un **prototype non maintenu**, pas une base de production. Pour
l'utiliser avec ton proxy, il faut soit faire tourner le proxy dans son
namespace, soit ajouter un sidecar HTTP/SOCKS (par exemple `gost`).

**azinchen/nordvpn-wg** est plus simple et plus « production » :

- sélection par pays/ville/**hostname** dans `COUNTRY` ;
- load balancing, reconnexion, healthcheck, `status.json` ;
- token NordVPN, kill switch, mode gateway ;
- mais **pas de proxy**, **pas d'API de contrôle**, AGPL, 5★.

**Verdict** : `sas` si le critère est **IP exacte + rotation**, mais uniquement
comme prototype jetable (aucun commit depuis sa création) ; `azinchen` si le
critère est **hostname + stabilité** et que tu acceptes un sidecar proxy.
**Cependant, les deux sont WireGuard-only** : il faut vérifier leur MTU (ou
`WIREGUARD_MTU=1280` côté Gluetun) avant de conclure ; le canary a montré que
sans MTU explicite le tunnel ne passe pas le healthcheck, alors qu'avec 1280 il
fonctionne.

## 5. Plan de canary recommandé

1. **Canary A — Gluetun hostname WireGuard au démarrage** (aucun nouveau projet) :
   - recréer une station WireGuard avec `SERVER_HOSTNAMES=fr1004.nordvpn.com` et
     un `SERVER_COUNTRIES` **cohérent** (France) ;
   - vérifier les logs, l'IP publique et le hostname réellement utilisé ;
   - ne pas utiliser `PUT /v1/vpn/settings` avec hostname avant d'avoir validé ce
     mode démarrage ; garder `hostnames = []` en attendant.
2. **Canary B — Gluetun provider custom** :
   - générer une config NordLynx exacte (IP endpoint + clé publique) ;
   - vérifier que l'IP de sortie correspond bien au serveur choisi ;
   - mesurer le temps de reconnexion et la stabilité sur 24 h.
3. **Canary C — sas + sidecar proxy** :
   - `sas` en mode pinned/pinned-list, `gost` ou tinyproxy dans le même namespace ;
   - mesurer tok/s, TTFB, RAM/CPU, échecs, rotation.
4. **Canary D — sing-box** :
   - une station, un endpoint WireGuard explicite, inbound mixed HTTP/SOCKS ;
   - comparer au Gluetun actuel sur les mêmes prompts.
5. **Ne pas migrer les quatre stations** avant d'avoir les chiffres des canaris.

**Résultat du 29/10/2026** : canary A (boot-time) applique bien le hostname ;
avec `WIREGUARD_MTU=1280`, l'egress WireGuard est healthy (IP publique en ~2 s).
Canary B (provider custom) est **validé** : endpoint `109.69.110.140`, IP
publique `109.69.110.159`, healthy en ~1 s. Canary D (sing-box) est **validé
avec une exception de routage direct** pour l'IP de l'endpoint : handshake OK,
IP publique `109.69.110.157`, proxy HTTP/SOCKS fonctionnel. Canary C (`sas`) n'a
pas été exécuté (prototype 1★, build Rust non publié, pas de proxy intégré).

## 6. Points de vigilance

- **Ne pas confondre hostname et IP** : Gluetun sélectionne un hostname puis
  résout l'endpoint ; le provider `custom` épingle une IP. Une IP NordVPN peut
  changer ou être retirée ; un hostname reste plus stable.
- **Le pin runtime peut crasher le looper** si le couple pays/hostname ne
  correspond à aucun serveur : HTTP 200 avec corps `crashed`, puis boucle de
  retry. Toujours valider le couple et prévoir un fallback pays.
- **Plafond NordVPN** : 4 sessions simultanées observées ; un cinquième tunnel
  peut provoquer des `AUTH_FAILED`.
- **Licences** : `azinchen` est AGPL, `sing-box` GPL-3.0, `mihomo` MIT ;
  vérifier les implications si tu redistribues.
- **Projets jeunes** : `sas` (1★), `wg-proxy-farm` (2★), `nordlynx-transmission`
  (9★) ne sont pas des garanties de production.
- **Pas de benchmark** : les débits des README ne sont pas comparables ; il faut
  mesurer sur cette machine, avec le même serveur et le même modèle.

## 7. Sources GitHub

- Gluetun : https://github.com/passteque/gluetun
- Wiki Gluetun (provider custom, contrôle) :
  https://github.com/qdm12/gluetun-wiki/blob/main/setup/providers/custom.md
  et https://github.com/qdm12/gluetun-wiki/blob/main/setup/advanced/control-server.md
- Code Gluetun :
  https://github.com/passteque/gluetun/blob/master/internal/configuration/settings/serverselection.go
  https://github.com/passteque/gluetun/blob/master/internal/provider/utils/tier.go
  https://github.com/passteque/gluetun/blob/master/internal/provider/nordvpn/updater/servers.go
  https://github.com/passteque/gluetun/blob/master/internal/server/vpn.go
- SAS : https://github.com/itsnocny/sas
- azinchen/nordvpn-wg : https://github.com/azinchen/nordvpn-wg
- sing-box : https://github.com/SagerNet/sing-box
- mihomo : https://github.com/MetaCubeX/mihomo
- wg-proxy-farm : https://github.com/sudoliyang/wg-proxy-farm
- nordlynx-transmission : https://github.com/edgd1er/nordlynx-transmission
- nordlynx-proxy : https://github.com/edgd1er/nordlynx-proxy
- docker-wireguard : https://github.com/jordanpotter/docker-wireguard
- Générateurs NordLynx :
  https://github.com/wifiuk/NordLynx-WireGuard-Config-Extract
  https://github.com/FinleyJDev/NordLynx-WireGuard-Config-Generator
- Mise à jour d'endpoint Gluetun :
  https://github.com/ssawka/gluetun-wireguard-updater
