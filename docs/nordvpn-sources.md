# Sources NordVPN — registre, audit et plan de test

> 2026-11-02. Audit de la chaîne de découverte des serveurs/IP NordVPN.
> Périmètre : sources fournies par l'opérateur, sources officielles,
> miroirs communautaires, données réseau/ASN, et intégration au registre
> `ip_registry`. Le registre machine lisible est dans
> [`docs/nordvpn_sources.yaml`](nordvpn_sources.yaml).

## 0. Réponse courte

Le projet ne s'appuie pas sur **une** base : il distingue trois plans qui ne
doivent jamais être confondus.

| Plan | Question | Source de vérité | Stockage projet |
|---|---|---|---|
| **Catalogue** | Quels serveurs existent, où, avec quelle charge et quelle clé WireGuard ? | API publique NordVPN + catalogue Gluetun | `logs/ip_registry.db` table `servers`, `logs/gluetun_nordvpn_embedded.json` |
| **Egress** | Quelle IP publique l'upstream voit-il réellement ? | **Aucune source** : uniquement une mesure depuis la station | `logs/requests.db` (`free_model_ip`), `ip_state`, `observations` |
| **Réseau** | À quel ASN / quelles plages appartient une IP ? | Netify, X4BNet, RIPE Stat, PeeringDB, bases ASN | pas stocké aujourd'hui |

Les quatre sources fournies le 2026-11-02 sont enregistrées dans
[`docs/nordvpn_sources.yaml`](nordvpn_sources.yaml) :

1. `https://wg-nord.pages.dev/` — frontend WireGuard, dérivé de l'API officielle.
2. `https://nordgen.selfhoster.win/` — catalogue packé, dérivé de `nordcache`.
3. `https://github.com/ip-address-list/nordvpn` — proxys HTTPS `:89`, hebdomadaire.
4. `https://www.netify.ai/resources/vpns/nordvpn` — réseaux/ASN, données payantes.

**Aucune des quatre n'est un catalogue indépendant de serveurs.** Deux sont des
miroirs de l'API officielle, une est une liste de proxys, une est une source
réseau. La seule vraie source de découverte reste l'API NordVPN, complétée par
le catalogue embarqué Gluetun pour les serveurs que l'API omet.

## 1. Ce que le code utilise déjà

| Fichier | Rôle | Source |
|---|---|---|
| [`free/ip_registry/catalog.py`](../free/ip_registry/catalog.py) | `NordVpnCatalog` : snapshot mémoire, TTL 6 h | `https://api.nordvpn.com/v1/servers?limit=0` + `logs/gluetun_nordvpn_embedded.json` |
| [`free/ip_registry/sources/`](../free/ip_registry/sources/) | Résolution multi-sources : `OfficialApiSource`, `JsonCatalogSource`, manifeste YAML, `SourceRegistry` (primaire → fallbacks → complétude), `source_id`/`stale` | API, manifeste/embarqué gluetun, cache `logs/nordvpn_api.json` |
| [`vpn/manager.py`](../vpn/manager.py) | least-loaded, pin pays, carte IP d'entrée → hostname | `/v1/servers/recommendations`, `/v1/servers?limit=8000` |
| [`scripts/merge_gluetun_catalog.py`](../scripts/merge_gluetun_catalog.py) | union API + manifeste conteneur + catalogue embarqué | trois sources, API prioritaire |
| [`scripts/fresh_chain.py`](../scripts/fresh_chain.py) | cache brut et chaîne de fraîcheur | `logs/nordvpn_api.json` |
| [`free/ip_registry/sqlite_registry.py`](../free/ip_registry/sqlite_registry.py) | persistance catalogue/observations/assignations | `logs/ip_registry.db` |
| [`scripts/ip_preselection.py`](../scripts/ip_preselection.py) | rapport de présélection (recherche seule) | API + `logs/requests.db` |

État actuel dans [`config.yaml`](../config.yaml) : `ip_registry.enabled: true`
et `allocator.mode: enforce`. Le registre catalogue et l’import multi-sources
sont opérationnels, et le superviseur répare toutes les stations en parallèle
bornée (2 chaînes, budget de rebind global). Le job de présélection périodique
reste à brancher.

### 1.1 Le catalogue, pas l'egress

Le champ `station` (et `ips[]`) de l'API est l'**IP d'entrée** du serveur, pas
l'IP publique de sortie. Exemple documenté dans
[`docs/ip-preselection-2026-10-29.md`](ip-preselection-2026-10-29.md) : la
station 1 sortait en `130.195.218.134` via `nl1050.nordvpn.com` dont l'entrée
API est `154.47.21.114`. Sur 250 serveurs API, **0** correspondance avec les
256 IP mesurées. C'est la raison d'être de `ip_state` : l'egress est **observé**,
jamais déduit du catalogue.

Corollaire : `cloud-ip-ranges.com` et `iptoolskit.com` listent des IP
d'entrée / des plages d'ASN, pas des egress. Ils ne peuvent pas remplacer une
sonde de station.

## 2. Les quatre sources fournies

### 2.1 `wg-nord.pages.dev` — frontend, pas base de données

Le HTML ne contient que l'UI. `index_script.js` interroge :

```text
https://proxy.rasimhamidi93717911.workers.dev/?target=
  https://api.nordvpn.com/v1/servers?limit=7000
  &filters[servers_technologies][identifier]=wireguard_udp
```

Le Worker Cloudflare est **tiers**. Le site expose aussi :

- `key-script.js` : `POST` vers `https://api.nordvpn.com/v1/users/services/credentials`
  avec `Authorization: token:<token>` via le même Worker ;
- `fetch-script.js` : `GET /v1/servers?limit=100&filters[...]=socks` puis
  génération de `station:1080:username:password`.

**Verdict** : ce n'est pas une source de données, c'est un client de l'API
officielle avec un proxy CORS. Utile comme référence de format et de génération
de config. **Ne pas faire transiter un token NordVPN réel par ce Worker.**

### 2.2 `nordgen.selfhoster.win` — miroir packé basse bande passante

Le dépôt [`mustafachyi/NordVPN-WireGuard-Config-Generator`](https://github.com/mustafachyi/NordVPN-WireGuard-Config-Generator)
contient le Worker complet. `Web/worker/wrangler.jsonc` indique :

```text
SERVER_SOURCE_URL = https://nordcache.selfhoster.win/api/servers
cron               = */5 * * * *
route              = nordgen.selfhoster.win
```

`GET /api/servers` renvoie `[publicKeys, countries]` :

- ~93,6 Ko (vs ~29 Mo pour l'API complète) ;
- `ETag`, `Cache-Control: public, max-age=300, stale-while-revalidate=60` ;
- clés WireGuard concaténées, pays/villes, deltas IPv4, load, groupes ;
- rate limit 100 req/60 s.

Le format est documenté dans `Web/worker/api.md` et décodé par
`Web/frontend/src/composables/useServers.js`. La CLI NordGen, elle, utilise
l'API officielle (`limit=16384`, filtre WireGuard) et
`https://api.nordvpn.com/v1/helpers/ips/insights` pour la géolocalisation.

**Verdict** : excellent **fallback** si l'API officielle est indisponible ou
trop lente. Ce n'est pas une source indépendante : `nordcache` est un service
compilé dont la provenance exacte n'est pas documentée. Le décodeur est un
point de fragilité (format non standard, non versionné publiquement).

### 2.3 `github.com/ip-address-list/nordvpn` — proxys HTTPS, pas IP

Le workflow `.github/workflows/main.yml` tourne chaque **dimanche 00:00** :

```text
GET /v1/servers/recommendations
  ?filters[country_id]=<id>
  &filters[servers_technologies][identifier]=proxy_ssl
  &limit=100
```

14 pays : `au, ca, fr, de, hk, in, jp, nl, sg, se, ch, tw, uk, us`.

Deux sorties :

- `generic/<cc>.txt` : hostnames ;
- `foxyproxy/<cc>.txt` : `ssl://username:password@<host>:89` avec des
  identifiants **factices**.

**Verdict** : utile uniquement pour la voie **proxy HTTPS `:89`**, pas pour
WireGuard, pas pour les IP, pas pour l'egress. Couverture partielle (14 pays,
100 serveurs/pays). Dernier commit vérifié : 2026-11-01.

### 2.4 `netify.ai/resources/vpns/nordvpn` — réseau et ASN

Chiffres affichés le 2026-11-02 :

- 57 pays, 102 villes ;
- 1 191 plages IPv4 (3 affichées + 1 188) ;
- 119 plages IPv6 (3 + 116) ;
- 4 ASN, dont 3 nommés : `AS141039` PacketHub, `AS147049` PacketHub,
  `AS207137` Tefincom ; le 4e est masqué (« and 1 more »). L'API
  `helpers/ips/insights` renvoie `isp_asn: 136787` pour un egress NordVPN, et
  X4BNet inclut `AS136787` (TEFINCOM). C'est très probablement le 4e.

Les plages complètes ne sont pas dans le HTML : elles passent par l'API IP ou
les datasets payants de Netify. La page reste une excellente **source de
qualification** : elle permet de vérifier qu'un egress appartient à un ASN
NordVPN ou de borner les plages à scanner. Elle **ne donne pas** les hostnames,
le load, ni l'egress d'un serveur précis.

## 3. Autres sources trouvées

### 3.1 Officielles NordVPN

| Source | Donne | Limite |
|---|---|---|
| [`/v1/servers?limit=0`](https://api.nordvpn.com/v1/servers?limit=0) | 7 229 serveurs : hostname, `station`, `ips`, `load`, pays/ville, technologies, clé WG | ~29 Mo ; `station` = IP d'entrée ; pas d'egress |
| [`/v1/servers/recommendations`](https://api.nordvpn.com/v1/servers/recommendations) | top serveurs par score, filtre pays/techno | pas de mesure réelle |
| [`/v1/servers/countries`](https://api.nordvpn.com/v1/servers/countries) | `id` pays → nom | métadonnées |
| [`/v1/servers/groups`](https://api.nordvpn.com/v1/servers/groups) | groupes (standard, dedicated_ip, p2p…) | métadonnées |
| [`/v1/technologies`](https://api.nordvpn.com/v1/technologies) | technologies | **ne pas** utiliser `/v1/servers/technologies` (403) |
| [`/v1/helpers/ips/insights`](https://api.nordvpn.com/v1/helpers/ips/insights) | IP publique, pays, ville, ISP, ASN, `protected` | décrit l'appelant, pas un serveur |
| [`/v1/users/services/credentials`](https://api.nordvpn.com/v1/users/services/credentials) | clé NordLynx, user/password | token requis ; jamais via un tiers |
| [`ovpn.zip`](https://downloads.nordcdn.com/configs/archives/servers/ovpn.zip) | une config `.ovpn` par serveur (~68,7 Mo) | hostnames seulement ; Last-Modified 2026-10-30 |
| [`ovpn_tcp.zip`](https://downloads.nordcdn.com/configs/archives/servers/ovpn_tcp.zip) | variante TCP (~34,3 Mo) | idem |
| [`ovpn_udp.zip`](https://downloads.nordcdn.com/configs/archives/servers/ovpn_udp.zip) | variante UDP (~34,4 Mo) | idem |
| [`proxy_ssl`](https://api.nordvpn.com/v1/servers/recommendations?filters[servers_technologies][identifier]=proxy_ssl&limit=100) | hostnames de proxys HTTPS | port 89, auth compte |
| [`socks`](https://api.nordvpn.com/v1/servers?filters[servers_technologies][identifier]=socks&limit=100) | IP d'entrée des proxys SOCKS | port 1080, auth compte |

Les anciens endpoints `https://api.nordvpn.com/server` et
`https://nordvpn.com/api/server` renvoient **403** : ne pas les réintroduire.
La page [`nordvpn.com/servers/`](https://nordvpn.com/servers/) est marketing
(7 400+ serveurs, 118 pays), sans flux machine.

Optimisation utile : l'API accepte `fields[...]`. NordGen ne demande que
`station`, `hostname`, `load`, `technologies.metadata`, `locations` et
`groups.identifier`, ce qui réduit fortement le payload par rapport à
`limit=0` (~29 Mo). Le catalogue actuel de `NordVpnCatalog` ne l'exploite pas
encore.

### 3.2 Miroirs et listes communautaires

| Source | Fraîcheur | Ce qu'elle donne | Verdict |
|---|---|---|---|
| [`X4BNet/lists_vpn`](https://github.com/X4BNet/lists_vpn) | quotidienne (2026-11-01) | plages VPN par ASN, IPv4/IPv6, dont `AS207137`, `AS136787`, `AS9009` (M247) | **à intégrer** pour qualifier un egress ; faux positifs M247 possibles |
| [`cloud-ip-ranges.com/providers/nordvpn`](https://cloud-ip-ranges.com/providers/nordvpn) | quotidienne (2026-11-02 04:00) | 7 228 CIDR IPv4, téléchargement txt/csv/json | miroir des `station` de l'API, **pas** des egress |
| [`iptoolskit.com/organization/NordVPN`](https://iptoolskit.com/organization/NordVPN) | inconnue | 500 plages, 229 121 IP, AS136787, 33 pays, 81 villes | agrégation ASN, pas un catalogue |
| [`namakshenas/nordvpn-loc-test`](https://github.com/namakshenas/nordvpn-loc-test) | hebdomadaire (2026-11-02) | hostnames recommandés, 7 pays | petit fallback proxy |
| [`ip-address-list/nordvpn`](https://github.com/ip-address-list/nordvpn) | hebdomadaire (2026-11-01) | proxys HTTPS `:89`, 14 pays | voie HTTPS uniquement |
| [`ipnetinfo.com/data/iplist.php/nordvpn`](https://ipnetinfo.com/data/iplist.php/nordvpn) | inconnue | liste d'IP | timeout lors de l'audit ; non vérifié |
| [`sudesh0sudesh/NordVPN-IPs`](https://github.com/sudesh0sudesh/NordVPN-IPs) | 2025-01-14 | CSV d'IP + subnets | **périmé** |
| [`sandboxfan/VPN_IP`](https://github.com/sandboxfan/VPN_IP) | 2023-03-26 | liste d'IP | **périmé** |
| [`LeakForge/nordvpn-ips`](https://github.com/LeakForge/nordvpn-ips) | 2021-12 / push 2025-09 | IP via ancien `/server` | **obsolète** (endpoint 403) |
| [`Umkus/nordvpn-ips`](https://github.com/Umkus/nordvpn-ips) | 2021-12-13 | IP | **obsolète** |

Les gists `JamoCA` et `triggex` cités par les moteurs renvoient **404** au
2026-11-02 : ne pas les enregistrer comme sources actives.

### 3.3 Réseau, ASN, classification

| Source | Type | Usage |
|---|---|---|
| [`RIPE Stat AS207137`](https://stat.ripe.net/data/announced-prefixes/data.json?resource=AS207137) | API JSON | préfixes BGP annoncés, fiable et sans clé |
| [`PeeringDB AS207137`](https://www.peeringdb.com/api/net?asn=207137) | API JSON | organisation, counts, peering |
| [`BGP.Tools AS136787`](https://bgp.tools/as/136787) | HTML | visibilité BGP, préfixes ; GET OK, HEAD 403 |
| [`IPinfo AS136787`](https://ipinfo.io/AS136787) | HTML | préfixes, pays ; API JSON limitée |
| [`IPLocate`](https://github.com/iplocate/ip-address-databases) | dataset | base IP → ASN utilisée par X4BNet |
| [`Team Cymru`](https://team-cymru.com/community-services/ip-asn-mapping/) | whois/DNS | lookup IP → ASN ponctuel |
| [`MaxMind GeoLite2 ASN`](https://dev.maxmind.com/geoip/geolite2-free-geolocation-data/) | dataset local | classification ASN/pays hors ligne |
| [`ipapi.is`](https://ipapi.is/vpn-detection.html) | commercial | base d'egress VPN, CSV/MMDB, inclut NordVPN |
| [`IPASIS`](https://ipasis.com/directory/vpn-providers) | commercial | annuaire ASN et détection |
| [`AbstractAPI`](https://www.abstractapi.com/guides/vpn-checker/nord-vpn-ip-lookup) | commercial | détection VPN à la requête |

`api.bgpview.io` n'a pas résolu au moment de l'audit : ne pas en dépendre.

### 3.4 Outils, pas sources

- [`mustafachyi/NordVPN-WireGuard-Config-Generator`](https://github.com/mustafachyi/NordVPN-WireGuard-Config-Generator) :
  Worker + frontend + CLI + tests ; référence pour décoder le catalogue packé.
- [`n-thumann/wg-nord`](https://github.com/n-thumann/wg-nord) : CLI Rust de
  génération de config.
- [`nord`](https://nord.readthedocs.io/en/latest/reference/api.html) : client
  Python de l'API.
- [`sleeplessbeastie`](https://sleeplessbeastie.eu/2019/02/18/how-to-use-public-nordvpn-api/) :
  guide 2019 de l'ancien `/server`, historique uniquement.

## 4. Matrice besoin → source

| Besoin | Source recommandée | Pourquoi |
|---|---|---|
| Lister tous les serveurs, pays, villes, load, clé WG | `nordvpn_api_servers` | source officielle, complète, 6 h de cache |
| Compléter les hostnames absents de l'API | `ovpn.zip`, `gluetun_embedded_catalog` | l'API omet des anciens serveurs (`al36..al75`) |
| Choisir un serveur peu chargé | `nordvpn_api_recommendations` | `load` + score, déjà utilisé |
| Obtenir un egress réel | **sonde station** + `logs/requests.db` | aucune source ne le donne |
| Vérifier qu'un egress est NordVPN | `nordvpn_api_insights`, Netify, X4BNet, ipapi.is | ASN/ISP, plages |
| Proxy HTTPS `:89` | `nordvpn_api_proxy_ssl`, `ip-address-list` | hostnames dédiés |
| Proxy SOCKS `:1080` | `nordvpn_api_socks` | IP d'entrée, auth compte |
| Repli si l'API est down | `nordcache_packed`, `gluetun_manifest`, cache 29 Mo | formats locaux/compact |
| Borner les plages à scanner | Netify, X4BNet, RIPE, PeeringDB | réseau/ASN |
| Classifier un egress inconnu | ipapi.is (payant), X4BNet, IPLocate | base VPN/ASN |

## 5. Hiérarchie et intégration proposées

### 5.1 Ordre de résolution du catalogue

```text
1. API officielle /v1/servers?limit=0                 (source de vérité)
2. nordcache /api/servers                             (miroir 93 Ko, 5 min)
3. manifeste Gluetun /gluetun/servers/nordvpn.json    (14 132 entrées)
4. catalogue embarqué Gluetun                          (17 103 entrées)
5. cache logs/nordvpn_api.json                        (hors ligne)
6. ovpn.zip                                           (complétude hostnames)
```

Règles :

- L'API gagne toujours sur un doublon `hostname` ; le manifeste gagne sur
  l'embarqué ; l'embarqué complète les hostnames manquants.
- Un fallback ne doit jamais **remplacer** silencieusement l'API : il doit
  marquer le snapshot `stale=true` et exposer la source utilisée.
- Le catalogue ne doit jamais alimenter `ip_state.egress_ip` sans sonde.

### 5.1.1 Annotations du 2026-11-02

- `ip_registry.enabled=true`, `allocator.mode=enforce` : le superviseur répare
  toutes les stations (2 en parallèle, budget de rebind global) et persiste les
  winners dans `ip_state`/`assignments`.
- `nordcache_packed` n'a **pas** d'adaptateur : le manifeste le déclare comme
  fallback, mais la résolution rapporte `no-adapter:nordcache_packed` si l'API
  et les autres sources échouent. Ce n'est pas silencieux.
- Le cache `nordvpn_api_cache` et le manifeste gluetun ne sont enregistrés que
  si leurs fichiers existent ; une source absente n'est pas une source vide.
- Toute résolution de repli est `stale=True` et chaque ligne porte son
  `source_id` ; l'API reste prioritaire sur un doublon `hostname`.

### 5.2 Ce qui est maintenant stocké

La table `servers` porte la provenance depuis le schéma v2 :

```sql
servers(hostname, server_ip, country, city, load, technologies,
        first_seen_at, last_seen_at, active,
        source_id, stale, catalog_fetched_at)
```

Un journal `catalog_snapshots(source_id, fetched_at, etag, stale,
server_count, error)` conserve chaque refresh. Règles appliquées :

- `source_id` = source qui a fourni la ligne (`nordvpn_api_servers`,
  `gluetun_embedded_catalog`, `nordvpn_api_cache`, …) ;
- `stale=1` pour une ligne de repli, un catalogue statique ou un cache trop
  ancien ;
- un snapshot partiel (`complete=false`) ne désactive **jamais** les serveurs
  absents ;
- seuls un snapshot primaire, complet et frais peut désactiver les disparus ;
- la migration v1 → v2 est idempotente et conserve les lignes existantes.

L’API reste prioritaire sur un doublon `hostname` ; les sources de complétude
(embarqué gluetun) ne remplacent jamais le vainqueur et n’alimentent jamais
`ip_state.egress_ip` sans sonde.

### 5.3 Découpage recommandé

```text
free/ip_registry/
  sources/
    registry.py        # chargement du manifeste YAML, validation, priorités
    official_api.py    # adaptateur API NordVPN
    nordcache.py       # décodeur packé + ETag
    gluetun.py         # manifeste + embarqué
    ovpn_archive.py    # complétude hostnames
    ranges.py          # X4BNet / Netify / RIPE -> classification
  ...
```

Chaque adaptateur expose : `fetch() -> Snapshot`, `source_id`, `fetched_at`,
`etag`, `stale`, `error`. Le registre ne connaît que cette interface.

## 6. Tests et surveillance

### 6.1 Tests unitaires (CI, sans réseau)

- Le manifeste YAML est valide, les `id` sont uniques, les quatre sources
  opérateur sont présentes, les URL sont bien formées.
- Le décodeur `nordcache` est testé sur une **fixture** de 93 Ko versionnée :
  nombre de pays, villes, clés, hostnames, IP, load.
- Les adaptateurs officiels sont testés avec des réponses JSON figées :
  parsing `station`/`ips`, technologies, groupes, pays.
- La fusion Gluetun est testée sur les trois jeux : API > manifeste > embarqué,
  dédoublonnage par hostname.
- Les règles de classification X4BNet/Netify sont testées sur des IP connues
  (`87.120.102.90`, `186.247.44.61`, `2.56.190.2`) et des IP hors plage.
- Un test de non-régression vérifie que `station` n'est **jamais** écrit dans
  `ip_state.egress_ip` sans sonde.

### 6.2 Tests de contrat (quotidien ou hebdomadaire, réseau)

| Source | Assertion |
|---|---|
| API `/v1/servers?limit=0` | HTTP 200, JSON liste, ≥ 7 000 serveurs, champs `hostname/station/load/status/locations` présents |
| API `recommendations` | HTTP 200, au moins 1 serveur pour un pays connu |
| API `countries/groups/technologies` | HTTP 200, schéma attendu |
| `nordcache` | HTTP 200, ~93 Ko ± 20 %, `ETag` présent, payload `[keys, countries]` décodable |
| `ip-address-list` | dernier commit < 8 jours, 14 fichiers `generic/*.txt` non vides |
| `namakshenas` | dernier commit < 8 jours, 7 fichiers non vides |
| `X4BNet` | dernier commit < 48 h, `output/vpn/ipv4.txt` contient `186.247.44.0/23` |
| `cloud-ip-ranges` | HTTP 200, ~7 228 CIDR, dernier changement < 48 h |
| `ovpn.zip` | HTTP 200, `Content-Length` ~68 Mo, `Last-Modified` < 7 jours |
| Netify | HTTP 200, contient `AS141039`, `AS147049`, `AS207137`, 57 pays |
| RIPE Stat | HTTP 200, `data.prefixes` non vide pour AS207137 |
| PeeringDB | HTTP 200, `data[0].asn == 207137` |

### 6.3 Canary de production (toutes les 6 h)

- Refresh du catalogue officiel ; si échec, bascule sur `nordcache` puis sur le
  dernier snapshot valide.
- Sonde de disponibilité : `python scripts/nordvpn_sources_probe.py --json`
  (HEAD/GET léger, sans télécharger les archives).
- Log structuré : `source_id`, `fetched_at`, `stale`, `count`, `latency_ms`.
- Alerte si : API en échec > 1 h, snapshot > 12 h, `count` chute de > 20 %,
  `nordcache` indisponible > 30 min.
- **Ne jamais bloquer une station** parce qu'une source est down : le dernier
  snapshot valide reste utilisable, marqué `stale`.

### 6.4 Tests d'intégration (à la demande, avant activation)

1. Comparer `nordvpn_api_servers` et `nordcache` : diff des hostnames, ordre de
   grandeur attendu (quelques dizaines, pas des milliers).
2. Comparer `nordvpn_api_servers` et `cloud-ip-ranges` : les `station` doivent
   correspondre à ~99 % ; sinon la source a divergé.
3. Prendre 3 serveurs par pays, binder la station, mesurer l'egress, vérifier
   `isp_asn` via `helpers/ips/insights`, écrire dans `ip_state`.
4. Rejouer le canary de l'allocateur sur un candidat vivant.
5. Vérifier qu'un serveur `al41` (pool `87.120.102.x`) est bien rebindable et
   que son egress n'est pas confondu avec le `station` de l'API.

### 6.5 Quand ?

| Fréquence | Quoi |
|---|---|
| À chaque PR | tests unitaires du manifeste, décodeur sur fixture, fusion Gluetun |
| Toutes les 6 h | refresh catalogue + canary de production |
| Chaque nuit | tests de contrat API + X4BNet + cloud-ip-ranges |
| Chaque semaine | `ip-address-list`, `namakshenas`, `ovpn.zip`, Netify |
| Avant activation de `ip_registry` | tests d'intégration complets |
| À chaque changement de source | revalider la hiérarchie et les seuils |

## 7. Risques et limites

1. **Confusion entrée / egress.** `station`, `cloud-ip-ranges`, `iptoolskit` et
   les listes communautaires ne sont pas l'egress. Le seul egress fiable est
   mesuré. C'est la cause racine des erreurs de présélection.
2. **Miroirs non indépendants.** `wg-nord`, `nordgen`, `nordcache` et
   `ip-address-list` dérivent tous de l'API officielle. Une panne ou un
   changement de schéma se propage.
3. **Worker tiers.** `wg-nord` fait transiter le token NordVPN par
   `proxy.rasimhamidi93717911.workers.dev`. Risque de fuite de credentials.
4. **Format packé non versionné.** `nordcache` peut changer sans préavis ; le
   décodeur doit être testé sur fixture et échouer proprement.
5. **Sources périmées.** LeakForge, Umkus, sandboxfan, sudesh0sudesh : ne pas
   les utiliser pour la production. Les gists JamoCA/triggex sont morts.
6. **Port 89 / 1080.** Les proxys HTTPS/SOCKS peuvent être bloqués ou retirés ;
   tester avant de les mettre dans `https_proxies` / `socks5_proxies`.
7. **ASN partagé.** M247 (AS9009) héberge d'autres clients : X4BNet peut
   produire des faux positifs. Toujours confirmer avec `isp= NordVPN` ou
   `isp_asn` avant de conclure.
8. **Provenance en base.** Le schéma v2 stocke `source_id`, `stale` et
   `catalog_fetched_at` dans `servers`, et journalise chaque refresh dans
   `catalog_snapshots`. La résolution multi-sources refuse un fallback
   silencieux : la résolution et les lignes sont marquées `stale`.
9. **Feature active (allocateur).** `ip_registry.enabled: true` et
   `allocator.mode: enforce` depuis le 2026-11-02 : le superviseur répare toutes
   les stations (2 en parallèle, rebind global limité) et enregistre les IP
   testées. Le job de présélection périodique reste à brancher.

## 8. Actions proposées

1. **Garder** `nordvpn_api_servers` comme primaire et `nordcache` comme
   fallback compact ; ne pas promouvoir `nordcache` au rang de source de vérité.
2. **Ajouter** `source_id`, `catalog_fetched_at`, `etag` à `servers` (migration
   de schéma versionnée). **Fait** : schéma v2, `source_id`/`stale` par ligne
   et journal `catalog_snapshots`.
3. **Implémenter** les adaptateurs `sources/` avec interface commune et
   `stale` explicite. **Fait** pour l’API officielle, le manifeste/embarqué
   gluetun et le cache ; `nordcache` packé reste déclaré sans décodeur.
4. **Réduire** le payload du catalogue avec `fields[...]` (comme NordGen) au
   lieu de télécharger les ~29 Mo de `limit=0` à chaque refresh.
5. **Ajouter** le test de contrat API + `nordcache` + X4BNet au nightly.
6. **Ajouter** un probe manuel `scripts/nordvpn_sources_probe.py` qui lit le
   YAML et vérifie les URLs sans télécharger les archives complètes.
7. **Ne pas intégrer** `ip-address-list` pour WireGuard : uniquement proxy
   HTTPS. Ne pas utiliser `wg-nord` pour les tokens.
8. **Décider** du sort de `ip_registry` : décision prise le 2026-11-02 —
   `enabled: true`, `allocator.mode: enforce`, superviseur toutes-stations.
   Surveiller les winners, les 429 et le budget de rebind.

## 9. Annexe — commandes de vérification

```bash
# Sonde du registre (HEAD/GET léger, sans archives)
python scripts/nordvpn_sources_probe.py
python scripts/nordvpn_sources_probe.py --only nordvpn_api_servers --json

# Catalogue officiel (taille, count, champs)
curl -s 'https://api.nordvpn.com/v1/servers?limit=0' | jq 'length'

# Fallback packé
curl -sI 'https://nordcache.selfhoster.win/api/servers' | grep -i etag
curl -s  'https://nordcache.selfhoster.win/api/servers' | wc -c

# Proxy HTTPS hebdomadaire
curl -s 'https://raw.githubusercontent.com/ip-address-list/nordvpn/main/generic/de.txt' | head

# Plages d'egress / VPN
curl -s 'https://raw.githubusercontent.com/X4BNet/lists_vpn/main/output/vpn/ipv4.txt' | grep -E '186\.247\.44\.0/23|87\.120\.102\.'

# Plages d'entrée (miroir API)
curl -s 'https://cloud-ip-ranges.com/download/nordvpn.txt' | wc -l

# ASN
curl -s 'https://stat.ripe.net/data/announced-prefixes/data.json?resource=AS207137' | jq '.data.prefixes | length'

# Egress réel (station)
curl -s --socks5-hostname 127.0.0.1:1080 'https://api.nordvpn.com/v1/helpers/ips/insights' | jq
```
