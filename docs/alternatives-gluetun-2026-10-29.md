# Alternatives à Gluetun pour les stations VPN — recherche large

> 29/10/2026. Périmètre : clients VPN Docker, orchestrateurs de flottes VPN,
> tunnels/proxies comparables, et leviers adjacents (fournisseurs VPN, proxies
> commerciaux). Méthode : métadonnées GitHub, README/docs officielles et projets
> voisins. **Pas de benchmark réseau live** : les chiffres de débit doivent être
> mesurés sur ta machine, avec le même serveur et le même modèle.

## 1. Réponse courte

**Gluetun reste le meilleur choix pour ce projet.** Aucun projet trouvé ne
combine aujourd'hui, en un seul conteneur :

- NordVPN en OpenVPN **et** WireGuard ;
- proxy HTTP **et** SOCKS5 intégrés (utilisés par les stations) ;
- serveur de contrôle HTTP avec authentification ;
- kill switch, DNS-over-TLS, healthcheck egress ;
- sélection de serveur par pays/hostname et API de serveurs ;
- maintenance active, licence MIT, image ~43 Mo.

Les alternatives intéressantes sont **complémentaires ou expérimentales**, pas des
remplacements directs :

1. **azinchen/nordvpn** et **azinchen/nordvpn-wg** : les plus proches côté
   NordVPN, mais **pas de proxy HTTP/SOCKS ni d'API de contrôle** ; ils routent
   des conteneurs via `network_mode: service:...`. Il faudrait déplacer le proxy
   dans le namespace VPN de chaque station.
2. **itsnocny/sas** : alternative minimale NordVPN WireGuard, ~24 Mo,
   <20 Mo de RAM, API de contrôle inspirée de Gluetun, rotation dynamique. Très
   prometteuse sur le papier, mais **1 étoile, très jeune, pas de proxy HTTP,
   pas d'OpenVPN** : prototype, pas production.
3. **GlueCannon** : orchestre plusieurs Gluetun derrière HAProxy ; bon modèle
   d'architecture, mais pas de qualité/rotation fine.
4. **tunnelhop** : un proxy local + Gluetun + API `/rotate` ; architecture la
   plus proche de la tienne, mais ProtonVPN et projet très jeune.
5. **sing-box / mihomo** : un seul processus peut gérer plusieurs sorties
   WireGuard avec santé/load-balancing et proxy HTTP/SOCKS ; mais **pas
   d'OpenVPN, pas d'API fournisseur NordVPN**, et une refonte importante.
6. **gost / HAProxy** : utiles devant les stations pour le failover et la santé,
   pas pour remplacer le tunnel VPN.

> Recherche complémentaire spécifique WireGuard (sélection d'IP, hostname,
> alternatives GitHub) : [alternatives-wireguard-github-2026-10-29.md](alternatives-wireguard-github-2026-10-29.md).

**Le levier le plus rentable n'est probablement pas de remplacer Gluetun**, mais
de changer de fournisseur VPN si le plafond de 4 sessions ou le débit te bloque :
Gluetun supporte déjà Mullvad, ProtonVPN, Surfshark, PIA, etc. Les proxies
commerciaux (Bright Data, Oxylabs, IPRoyal, Smartproxy, Webshare) sont une autre
piste si le besoin est la rotation d'IP à haut débit, avec un coût et un risque
de blocage différents.

## 2. Contraintes réelles du projet

Le remplaçant doit rester compatible avec :

- **Windows + Docker Desktop** (conteneurs Linux via WSL2) ;
- **NordVPN** (OpenVPN et/ou WireGuard/NordLynx) ;
- **4 tunnels simultanés** utiles (plafond observé du compte) ;
- **proxy HTTP/SOCKS par station** (`VPN_PROXY_URL`, `VPN_SOCKS5_URL`) ;
- **API de contrôle** pour l'état, le serveur, l'IP publique ;
- **pin par hostname** pour la présélection qualité (Gluetun accepte aussi le
  filtre hostname en WireGuard ; notre wrapper le désactive actuellement) ;
- **healthcheck egress** (pas seulement « VPN running ») ;
- **rotation rapide** et **logs exploitables** (hostname, IP de peering) ;
- budget mémoire/CPU raisonnable (512 Mo / 0,5 CPU par station aujourd'hui).

Un remplaçant qui ne fournit pas le proxy HTTP/SOCKS ou l'API de contrôle impose
une refonte du proxy, pas un simple changement d'image Docker.

> **Tests du 29/10/2026** : le pin hostname WireGuard fonctionne au démarrage ;
> sans `WIREGUARD_MTU` explicite le healthcheck échoue, avec **1280** il passe en
> ~2 s. Le provider **custom** (IP exacte) est validé. **sing-box** fonctionne
> aussi (proxy HTTP/SOCKS + IP publique) à condition d'exclure l'IP de l'endpoint
> du tunnel via une route `direct`. Détails :
> [alternatives-wireguard-github-2026-10-29.md](alternatives-wireguard-github-2026-10-29.md).

## 3. Panorama par catégories

### A. Clients VPN Docker « provider-aware »

| Projet | Fournisseurs | Protocoles | Proxy intégré | Contrôle/rotation | État |
|---|---|---|---|---|---|
| **Gluetun** (`passteque/gluetun`, ex-`qdm12`) | NordVPN, Mullvad, Proton, Surfshark, PIA, etc. | OpenVPN + WireGuard | HTTP, SOCKS5, Shadowsocks | API HTTP, auth, settings, IP publique | 15,9 k★, MIT, actif |
| **azinchen/nordvpn** | NordVPN | OpenVPN | Non (namespace) | Sélection serveur, reconnexion, `status.json`, healthcheck | 126★, AGPL, actif |
| **azinchen/nordvpn-wg** | NordVPN | WireGuard/NordLynx | Non (namespace) | Idem, token, serveur/hostname, load balancing | 5★, AGPL, actif |
| **itsnocny/sas** | NordVPN | WireGuard | Non (namespace) | API type Gluetun, rotation dynamique, liste épinglée | 1★, MIT, très jeune |
| **bubuntux/nordvpn** | NordVPN | OpenVPN | Non | Ancien | **archivé** |
| **bubuntux/nordlynx** | NordVPN | WireGuard | Non | Ancien | **archivé** |
| **jordanpotter/docker-wireguard** | Générique (config) | WireGuard | Non | Aucun | 163★, MIT, actif |
| **dperson/openvpn-client** | Générique (config) | OpenVPN | Non | Aucun | 1,1 k★, 2024 |
| **linuxserver/docker-wireguard** | Générique | WireGuard | Non | **Serveur**, pas client | 3,6 k★, actif |

### B. Orchestration / rotation de flottes

| Projet | Ce qu'il fait | Intérêt pour toi | Limite |
|---|---|---|---|
| **GlueCannon** | Génère plusieurs Gluetun + un HAProxy devant | Modèle de frontal et de flotte | 9★, pas de qualité/rotation fine |
| **tunnelhop** | 1 proxy local, Gluetun, API `POST /rotate` | Architecture très proche | ProtonVPN, 2★, très jeune |
| **wg-proxy-farm** | N namespaces WireGuard + tinyproxy | Idée de ferme de proxies isolés | 2★, pas d'API fournisseur |
| **mullvad-proxy** | Pool de Gluetun Mullvad | Référence de pool | 0★, Mullvad uniquement |
| **gluetun-ip-rotator** | API qui redémarre un Gluetun pour changer d'IP | Brique de rotation | 2★, sans auth, à ne pas exposer |
| **gluetun-webui** | UI multi-Gluetun | Inspiration UI | 5★, README/téléchargement douteux, à auditer avant usage |

### C. Tunnels/proxies génériques (refonte possible)

| Projet | Forces | Limites pour ce projet |
|---|---|---|
| **sing-box** | Un processus, plusieurs sorties WireGuard, HTTP/SOCKS, routage, santé | Pas d'OpenVPN ni d'API NordVPN ; configuration à générer ; 39 k★ |
| **mihomo** (Clash Meta) | Groupes `url-test`/`fallback`/`load-balance`, HTTP/SOCKS, API REST, WireGuard | Pas d'OpenVPN ni d'API NordVPN ; config WireGuard manuelle ; 35 k★ |
| **gost** | Load-balancing, health checks, Web API, proxy multi-protocole | N'est pas un client VPN ; à placer devant les Gluetun |
| **vopono** | Namespaces réseau, plusieurs fournisseurs, Rust | **Linux uniquement**, pas Docker/Windows ; 1,4 k★ |
| **Tailscale / NetBird / wg-easy** | Maillage ou serveur WireGuard | Pas des sorties VPN commerciales tournantes ; hors sujet |

## 4. Analyse des alternatives les plus pertinentes

### 4.1 azinchen/nordvpn et nordvpn-wg — le plus proche de NordVPN

**Points forts**

- Sélection de serveur par pays, ville, groupe ou **hostname** ;
- tri par charge, reconnexion automatique, healthcheck Docker ;
- token NordVPN, mode gateway, kill switch fail-closed, `status.json` ;
- version OpenVPN (XOR) et version WireGuard (NordLynx) ;
- actif et spécialisé NordVPN.

**Ce qui manque pour ton architecture**

- Pas de proxy HTTP/SOCKS intégré : il faut que le proxy tourne **dans** le
  namespace du conteneur VPN (`network_mode: service:...`) ou ajouter un
  sidecar proxy.
- Pas d'API de contrôle équivalente à Gluetun (pas de `GET /v1/publicip/ip`,
  pas de changement de serveur à chaud par HTTP).
- Licence **AGPL-3.0** : à vérifier si tu distribues l'image modifiée.
- `nordvpn-wg` est très jeune (5★) ; `nordvpn` OpenVPN est plus établi (126★).

**Verdict** : bon candidat pour un **canary** si tu acceptes de déplacer le
proxy dans le namespace VPN. Pas un drop-in.

### 4.2 itsnocny/sas — le « Gluetun minimal » NordVPN WireGuard

**Points forts**

- Image ~24 Mo, <20 Mo de RAM annoncés ;
- WireGuard kernel via netlink, kill switch nftables fail-closed ;
- API de contrôle « même forme » que Gluetun ;
- modes dynamique (API NordVPN), épinglé ou liste de serveurs ;
- rotation/failover et liste `WIREGUARD_SERVERS_FILE`.

**Limites**

- **1 étoile**, projet très jeune, peu de retours ;
- **WireGuard uniquement**, pas d'OpenVPN ;
- **pas de proxy HTTP/SOCKS** : `network_mode: service:sas` ou sidecar ;
- pas de sélection par hostname aussi riche que Gluetun ;
- pas de proxy intégré ni de healthcheck egress documenté au même niveau.

**Verdict** : le plus intéressant pour **réduire l'empreinte** et garder une API
proche, mais à considérer comme **prototype**. À tester sur une seule station,
jamais en production sans audit.

### 4.3 GlueCannon — orchestrateur Gluetun + HAProxy

- Génère un `docker-compose` et une config HAProxy pour plusieurs Gluetun ;
- 9★, Python, MIT, actif ;
- Bonne référence pour un **frontal HAProxy** avec health checks ;
- Ne fait pas la présélection qualité, la rotation fine ni la mesure tok/s ;
- Ton proxy fait déjà plus ; GlueCannon peut inspirer la partie « un seul point
  d'entrée + santé », pas remplacer la logique.

### 4.4 tunnelhop — architecture la plus proche

- Un proxy HTTP local + Gluetun + API `POST /rotate` ;
- change de serveur et vérifie que l'IP et le /24 ont changé ;
- MIT, 2★, ProtonVPN ;
- Très bon **modèle de conception** pour la rotation et la vérification d'IP ;
- Pas NordVPN, pas de qualité tok/s, projet très jeune.

### 4.5 sing-box / mihomo — la refonte « un processus, N tunnels »

Ils peuvent gérer plusieurs sorties WireGuard et exposer HTTP/SOCKS, avec des
groupes de santé/load-balancing. Mais :

- il faut **générer les configs NordLynx** (clé privée, endpoint, adresse) ;
- pas d'API NordVPN pour choisir/renouveler les serveurs ;
- pas d'OpenVPN ; le filtre hostname existe aussi dans Gluetun WireGuard, mais
  notre wrapper le désactive aujourd'hui (voir la recherche WireGuard dédiée) ;
- la rotation, les métriques et l'API de contrôle seraient à réécrire ;
- un seul processus = un seul point de défaillance.

**Verdict** : expérimentation possible pour réduire le nombre de conteneurs, mais
pas recommandé pour ce projet maintenant.

### 4.6 gost / HAProxy — le front, pas le tunnel

- **gost** : load-balancing, health checks, Web API, proxy multi-protocole ;
- **HAProxy** : health checks TCP/HTTP, retries, load-balancing ;
- Ils n'apportent **pas** de nouvelle IP de sortie ; ils améliorent la
  disponibilité et le failover entre stations ;
- Ton pool Python fait déjà la sélection ; un front ajouterait un saut et de la
  latence. À n'envisager que si le failover applicatif est insuffisant.

### 4.7 vopono — bon sur Linux, hors sujet ici

- Namespaces réseau, plusieurs fournisseurs, Rust, actif ;
- **Linux uniquement**, pas Docker Desktop/Windows ;
- Pas de proxy HTTP intégré pour ton architecture ;
- À garder si le proxy migre un jour sur un hôte Linux.

### 4.8 jordanpotter/docker-wireguard et dperson/openvpn-client

- Simples, légers, kill switch pour le premier ;
- **Aucune API fournisseur, aucun contrôle, aucun proxy** ;
- Il faudrait générer les configs, gérer la rotation et le proxy soi-même ;
- Utiles comme briques, pas comme remplacement.

## 5. Pourquoi Gluetun reste le bon choix

Gluetun cumule des fonctions que les alternatives n'ont pas ensemble :

1. **Proxy HTTP/SOCKS/Shadowsocks intégrés** — c'est le contrat actuel des
   stations (`VPN_PROXY_URL`, `VPN_SOCKS5_URL`).
2. **Serveur de contrôle HTTP authentifié** — état VPN, settings, IP publique.
3. **NordVPN OpenVPN + WireGuard** et sélection par hostname.
4. **Kill switch et DNS-over-TLS**.
5. **Maintenance active**, MIT, 15,9 k★, image ~43 Mo.
6. **Multi-fournisseurs** : changer de fournisseur ne demande pas de changer
   d'image.

Ses faiblesses sont réelles : un tunnel par conteneur, pas de load-balancing
multi-serveurs natif, pin hostname WireGuard **runtime** désactivé dans notre
wrapper (le pin au démarrage est validé avec MTU 1280), logs parfois difficiles à
parser. Mais ces faiblesses sont **déjà compensées par ton proxy** (qualité par
IP, rotation, présélection, healthcheck egress). Les remplacer par un projet
moins mature ferait perdre les briques de contrôle sans gain mesuré.

## 6. Le levier souvent plus rentable : le fournisseur

Le plafond observé de **4 sessions** est un problème de compte NordVPN, pas de
Gluetun. Gluetun supporte déjà :

- **Mullvad** (WireGuard/OpenVPN, SOCKS5) ;
- **ProtonVPN** (WireGuard/OpenVPN) ;
- **Surfshark**, **PIA**, **IVPN**, **Windscribe**, etc.

Pistes à tester, dans cet ordre :

1. **Mullvad** : peu de serveurs mais bonne stabilité, API claire, nombre
   d'appareils limité (vérifier l'offre actuelle) ;
2. **ProtonVPN** : plus d'appareils selon l'offre, WireGuard, bonne latence EU ;
3. **Surfshark/PIA** : plus de sessions simultanées, à vérifier sur ton offre ;
4. **Proxies commerciaux** (Bright Data, Oxylabs, IPRoyal, Smartproxy,
   Webshare) : rotation par requête/session, pas de plafond de 4 tunnels, mais
   coût au Go, IP datacenter/résidentielles et risque de blocage upstream.

Le changement de fournisseur est plus simple qu'un changement de client VPN :
mêmes conteneurs Gluetun, mêmes ports, mêmes API, seul le `.env` change.

## 7. Plan d'expérimentation recommandé

1. **Garder Gluetun en production** ; ne pas migrer toute la flotte.
2. **Canary 1 station** avec `azinchen/nordvpn-wg` ou `itsnocny/sas`, même
   serveur NordVPN, même modèle, pendant 24 h.
3. Mesurer : temps de connexion, temps de rotation, tok/s réel, TTFB, RAM/CPU,
   taux d'échec, visibilité du hostname, qualité de l'IP publique.
4. **Essai fournisseur** : 4 tunnels Mullvad/Proton, 24 h, mêmes prompts ;
   comparer tok/s et taux de blocage.
5. **Ne pas dépasser 4 stations actives** sur NordVPN ; le 5e tunnel peut
   provoquer des AUTH_FAILED.
6. Si un front est testé, préférer **gost** (léger, API) à HAProxy pour un
   failover applicatif ; mesurer la latence ajoutée.
7. Ne pas exposer l'API de contrôle Gluetun sans authentification ; les
   alternatives `gluetun-ip-rotator` et `sas` n'ont pas toutes ce niveau de
   sécurité.

## 8. Sources principales

- Gluetun : https://github.com/passteque/gluetun — wiki :
  https://github.com/qdm12/gluetun-wiki
- Contrôle Gluetun :
  https://github.com/qdm12/gluetun-wiki/blob/main/setup/advanced/control-server.md
- NordVPN dans Gluetun :
  https://github.com/qdm12/gluetun-wiki/blob/main/setup/providers/nordvpn.md
- azinchen/nordvpn : https://github.com/azinchen/nordvpn
- azinchen/nordvpn-wg : https://github.com/azinchen/nordvpn-wg
- itsnocny/sas : https://github.com/itsnocny/sas
- GlueCannon : https://github.com/IAmStoxe/gluecannon
- tunnelhop : https://github.com/NoelisTired/tunnelhop
- wg-proxy-farm : https://github.com/sudoliyang/wg-proxy-farm
- gluetun-ip-rotator : https://github.com/zerofelx/gluetun-ip-rotator
- mullvad-proxy : https://github.com/kub0-ai/mullvad-proxy
- sing-box : https://github.com/SagerNet/sing-box
- mihomo : https://github.com/MetaCubeX/mihomo
- gost : https://github.com/go-gost/gost
- vopono : https://github.com/jamesmcm/vopono
- jordanpotter/docker-wireguard : https://github.com/jordanpotter/docker-wireguard
- dperson/openvpn-client : https://github.com/dperson/openvpn-client
- linuxserver/docker-wireguard : https://github.com/linuxserver/docker-wireguard
- bubuntux/nordvpn (archivé) : https://github.com/bubuntux/nordvpn
- Mullvad : https://mullvad.net — ProtonVPN : https://protonvpn.com
- Surfshark : https://surfshark.com — PIA : https://www.privateinternetaccess.com
- Bright Data : https://brightdata.com — Oxylabs : https://oxylabs.io
- IPRoyal : https://iproyal.com — Smartproxy : https://www.smartproxy.com
- Webshare : https://www.webshare.io

## 9. Limites de cette recherche

- Les étoiles et dates GitHub sont des **signaux de santé**, pas des benchmarks.
- Les débits annoncés par les README ne sont pas comparables ; il faut mesurer
  sur ta machine, ton FAI, tes serveurs et ton modèle.
- Les projets très jeunes (`sas`, `tunnelhop`, `wg-proxy-farm`) peuvent changer
  ou disparaître ; ne pas les mettre en production sans audit.
- Les licences **AGPL/GPL** de certaines alternatives peuvent avoir des
  implications si tu redistribues.
- Les proxies commerciaux et le changement de fournisseur doivent respecter les
  conditions d'utilisation de l'upstream et du fournisseur.
