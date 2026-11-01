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

## Suite

1. Étendre la découverte aux pays/pools qui ont déjà produit des 200.
2. Alimenter `ip_state` avec ces candidats.
3. Rejouer le canary allocateur sur un candidat vivant.
