# Découverte ciblée — 2026-11-01

But : relier le catalogue NordVPN au registre `ip_state` en bindant des
serveurs réels, en vérifiant l’egress et en enregistrant le résultat free.

## Commande

```bash
python scripts/ip_registry_discover.py \
  --station 1 --country Albania --limit 3 --restore
```

## Résultats

| Serveur | Egress | Free | Reset |
|---|---|---|---|
| `al109.nordvpn.com` | `186.247.44.49` | **429** (`Retry-After=38684`) | `2026-11-02T00:21:34Z` |
| `al113.nordvpn.com` | aucun | bind rejeté (`no-egress`) | — |
| `al117.nordvpn.com` | aucun | bind rejeté (`no-egress`) | — |

## Ce que ça valide

- Le catalogue → binder → egress → probe → `ip_state` fonctionne de bout en
  bout sur un serveur réel.
- Un 429 est enregistré avec l’hostname, l’egress, le `Retry-After` et la date
  de reset exacte.
- Les serveurs sans egress sont rejetés avant tout probe.
- La station 1 a été restaurée avec la configuration Compose normale après le
  test.

## Enseignement

Les serveurs `al109..al130` du catalogue pointent vers le pool
`186.247.44.x`, qui est déjà épuisé (429) ou dont certains endpoints sont
morts. Les 200 observés précédemment (`87.120.102.53`, `.13`, `.76`) viennent
d’un autre pool, non exposé directement par le catalogue public. La découverte
doit donc être plus large (ou utiliser les observations déjà connues) pour
retrouver des egress 200.

## Limite majeure découverte

Le catalogue utilisé par gluetun (`/gluetun/servers/nordvpn.json`) contient
**14 132 entrées** (7 229 uniques côté API publique). Pour l’Albanie :

- 128 entrées (OpenVPN + WireGuard) ;
- hostnames `al109..` → IPs `186.247.44.x` ;
- **aucune entrée `87.120.102.x`**.

Or les seuls egress 200 observés (`.53`, `.13`, `.76`) appartiennent au pool
`87.120.102.x`, qui n’est **pas exposé** dans le catalogue. Il s’agit
probablement d’un pool NAT de serveurs plus récents, absents du catalogue
public/gluetun.

Conséquence : le binding par hostname est fiable **pour les serveurs du
catalogue**, mais il ne permet pas de reproduire un egress NAT inconnu. Pour ces
derniers, il faut :

1. les garder **stickies** tant que la station reste connectée ;
2. les marquer `rebindable=false` dans le registre ;
3. si la station tombe, revenir à une découverte par pays (loterie) ou
   identifier le hostname via les logs gluetun debug / l’API interne du
   provider.

C’est un point à intégrer au registre : une IP observée n’est pas forcément
re-assignable par hostname.

## Percée — catalogue embarqué gluetun

Le binaire gluetun (`/gluetun-entrypoint`) embarque un **catalogue NordVPN
complet** de **17 103 entrées**, dont les anciens serveurs albanais
`al36..al75` avec leur clé publique WireGuard :

```text
al41.nordvpn.com  wireguard  87.120.102.83  wgpubkey=0sAxvVg+...
al75.nordvpn.com  wireguard  87.120.102.67  wgpubkey=0sAxvVg+...
```

Le manifeste sur disque (`/gluetun/servers/nordvpn.json`, 14 132 entrées) ne
les contient pas, et c’est lui qui gagne : gluetun rejette
`SERVER_HOSTNAMES=al41` (« not a valid choice »).

Correctif :

1. `scripts/merge_gluetun_catalog.py` extrait le catalogue embarqué et
   l’écrit dans `/gluetun/servers/nordvpn.json` (backup inclus).
2. La station 1 est recréée avec `SERVER_HOSTNAMES=al41.nordvpn.com`.
3. Egress observé : **87.120.102.90**.
4. Probe officiel : **200**.
5. Proxy : **200**, DB `free_model_ip=87.120.102.90`, `station=1`.

Le serveur `al41` est donc **rebindable** : on peut le réassigner par hostname,
et le pool `87.120.102.x` n’est plus une loterie NAT inconnue.

Le compose de la station 1 est persisté avec :

```yaml
- SERVER_HOSTNAMES=${SERVER_HOSTNAMES_STATION1:-al41.nordvpn.com}
```

et `.env` contient `SERVER_HOSTNAMES_STATION1=al41.nordvpn.com`.

## Quatre stations validées

Après fusion du catalogue (embarqué + API) dans chaque volume gluetun :

| Station | Serveur | Egress | Free |
|---|---|---|---|
| 1 | `al41.nordvpn.com` | `87.120.102.90` | **200** |
| 2 | `al75.nordvpn.com` | `87.120.102.77` | **200** |
| 3 | `al74.nordvpn.com` | `87.120.102.59` | **200** |
| 4 | `al36.nordvpn.com` | `87.120.102.9` | **200** |

Le proxy voit les quatre stations `connected`, `station_usable=true`,
`model_usable=true`, et sert en **200** (DB : `.59`/station 3, `.90`/station 1).

Serveurs du pool testés mais sans egress WG : `al37`, `al45`, `al46`, `al73`.
`al36` fonctionne en WireGuard (`al36` était initialement instable, puis a
produit `.9`) et reste aussi joignable en HTTPS proxy (`:89`, egress `.14`)
dont le quota est épuisé (429).

Persistance : Compose pin `al41`/`al75`/`al74`/`al36` pour les stations 1/2/3/4
via `SERVER_HOSTNAMES_STATION1..4`, et `.env` contient les mêmes valeurs. Un
recreate de station ne perd donc plus le serveur.

**Attention** : le hostname fige le **serveur**, pas l’egress NAT. `al41` a
produit `87.120.102.90` puis `87.120.102.86` après un redémarrage ; l’IP peut
donc changer (fraîche ou déjà épuisée). Chaque egress observé doit être
enregistré dans le registre, et une rotation pinned reste utile pour chasser
une IP fraîche.

### L’egress NAT n’est pas fixe

Mesures du 2026-11-01 après usage :

```text
al41  .90 → 429 (quota consommé)   puis redémarrage → .86 → 429
al74  .59 → 429
al75  .77 → 200, puis redémarrage → .75 → 200
al36  .9  → indisponible, .14 → connexion fermée
```

Deux conséquences :

1. une IP « fraîche » peut s’épuiser à l’usage ; le ledger `(IP, reset_at)`
   doit suivre chaque observation ;
2. redémarrer le **même serveur** peut rendre une autre IP du pool NAT, parfois
   fraîche. La rotation des stations pinnées est donc utile, et le garde
   anti-doublon reste indispensable.

### Correctif santé station 4

Le 200 de la station 4 était masqué par `health=probing` : `mark_station_ok()`
nourrissait les bons probes **avant** `repair_succeeded()`, qui remettait
`good_probes=0` et l'état à `probing`. La station restait donc exclue malgré
un tunnel sain. L'ordre est désormais réparation → probes → `usable`, et le
mark 200 efface aussi `last_rotation_error`, `_repairing` et `_parked_until`.

## Catalogue fusionné (embarqué + API)

Remplacer le manifeste par le seul catalogue embarqué était une régression :
il ne contient pas les serveurs créés après la build gluetun. `ch524` (créé le
2026-10-28) était rejeté (`not one of the possible choices`), alors que
l’ancien manifeste téléchargé le connaissait.

`scripts/merge_gluetun_catalog.py` construit maintenant l’**union** :

1. catalogue embarqué (17 103 entrées, serveurs anciens dont `al36..al75`),
2. manifeste courant du conteneur,
3. API NordVPN live (7 228 serveurs, clés WireGuard comprises).

Priorité : API > manifeste courant > embarqué, clé `(hostname, vpn)`.

```text
embedded: 17 103 serveurs (8 552 wireguard)
api:      14 128 entrées (7 064 wireguard, WG + OpenVPN)
merged:   19 569 entrées (9 785 wireguard)
```

L’union est écrite dans `/gluetun/servers/nordvpn.json` et exportée vers
`logs/gluetun_nordvpn_embedded.json` (gitignored). `NordVpnCatalog` charge cet
export par défaut et le fusionne avec l’API ; un checkout neuf fonctionne sans
le fichier, il suffit de lancer le script pour retrouver les serveurs anciens.

## Proxy HTTPS NordVPN

Le proxy `:89` fonctionne **aussi via l’IP** :

```text
https://USER:PASS@87.120.102.3:89   → 200, egress 87.120.102.14
https://USER:PASS@37.120.213.83:89  → 200, egress 187.15.173.90
```

Mais `verify=True` échoue : le certificat ne correspond ni au hostname ni à
l’IP. Il faut `verify=False` ou épingler le certificat. L’egress HTTPS est
**distinct** de l’egress WireGuard du même serveur :

```text
al41   WG .90  → 200     HTTPS .94  → 429
ch296  WG .93  → 429     HTTPS .90  → 429
ch296  WG .93  → 429     (nouveau tunnel testé, manifeste fusionné OK)
```

Le port 89 n’est pas actif partout : sur 297 anciens serveurs résolus, seuls
`al41`, `ie106` et `us11640` ont produit un egress, tous les trois en 429. Un
scan de 300 serveurs `proxy_ssl` publics (14 pays) n’a produit **aucun** egress
fonctionnel. Un scan de 120 anciens serveurs WG (Autriche, Tchéquie, Bulgarie,
Irlande, Israël, Corée, Australie) n’a produit que 3 egress (`cz200`, `kr182`,
`kr190`), tous en 429. La piste HTTPS reste donc un transport possible mais ne
fournit pas d’IP fraîche à elle seule.

### 3ᵉ voie implémentée

`HttpsProxyEndpoint` est maintenant disponible dans le pool : proxy CONNECT
TLS, sans Docker, config `ip_rotation.https_proxies` et
`https_fallback_enabled` (off par défaut). Il n’est utilisé que si aucune
station Docker n’est routable. La vérification TLS du proxy est désactivée
(`CurlOpt.PROXY_SSL_VERIFYPEER/VERIFYHOST`), l’origine reste vérifiée.

## Suite

1. Étendre la découverte aux pays/pools qui ont déjà produit des 200.
2. Alimenter `ip_state` avec ces candidats.
3. Rejouer le canary allocateur sur un candidat vivant.
