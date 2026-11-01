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

## Suite

1. Étendre la découverte aux pays/pools qui ont déjà produit des 200.
2. Alimenter `ip_state` avec ces candidats.
3. Rejouer le canary allocateur sur un candidat vivant.
