# Contrat — Rotation automatique des egress WARP

**Statut**: implémenté et vérifié live le 2026-11-06.  
**Périmètre**: stations `caomingjun/warp` déclarées `managed: true`.

## 1. Modèle

Une entrée `socks5_proxies` managée porte :

```yaml
- host: 127.0.0.1
  port: 11103
  label: warp-restore1
  ipv6: true
  egress_ip: 2a09:bac1:27c0:2060::40e:4
  block: 2a09:bac1:27c0:2060::/64
  managed: true
  container: warp-restore1
  image: caomingjun/warp:latest
  enabled: true
```

Un proxy sans `managed: true` n'est **jamais** recréé.

## 2. Déclencheurs

| Déclencheur | Rôle |
|---|---|
| `429` de quota dans `_ledger_note_result` | rotation immédiate de la station |
| `on_quota_exhausted` (mode socks5) | rotation même si le bad-mark C1 est sauté |
| `rotate_exhausted_managed()` avant sélection | rattrapage quand toutes les stations sont bloquées |
| watchdog lifespan (30 s) | rotation sans requête en cours |
| `POST /api/vpn/socks5/rotate-egress` | déclenchement manuel contrôlé |

## 3. Algorithme

```
pour tentative 1..2 :
  si tentative == 1 : docker restart <container>
  sinon             : docker rm -f <container> ; docker run ... <image>
  attendre health == healthy
  résoudre l'egress via le SOCKS5 local (curl_cffi puis curl)
  calculer le bloc (/64 IPv6, /32 IPv4)
  si bloc == ancien bloc ou bloc déjà utilisé par une autre station : réessayer
  sinon : succès
```

Après succès :

1. `endpoint.egress_ip`, `endpoint.current_ip`, `endpoint.block` mis à jour ;
2. `bad_until`/`consec_429` effacés ;
3. la ligne `socks5_proxies` est mise à jour et `config.yaml` est persisté ;
4. le résultat est journalisé.

## 4. Invariants

- **WR-01** Single-flight par label.
- **WR-02** Aucun proxy non `managed` modifié.
- **WR-03** Un nouveau bloc ne doit pas être identique à l'ancien ni à celui
  d'une autre station.
- **WR-04** Le conteneur n'est recréé qu'après échec du redémarrage ou bloc
  inchangé.
- **WR-05** La config n'est écrite qu'après résolution d'un egress valide.
- **WR-06** Un échec laisse l'ancien endpoint en place et journalise la cause.
- **WR-07** La rotation ne dépend pas d'une requête entrante (watchdog).
- **WR-08** Le nouvel egress est vérifié avant d'être appliqué.

## 5. Tests

`tests/test_warp_rotation.py` :

- `/64` et `/32` ;
- champs `managed`/`container`/`image` ;
- restart puis nouveau bloc ;
- recréation si même bloc ;
- échec health ;
- proxy non managé ignoré ;
- application endpoint/pool/config ;
- `rotate_exhausted_managed` ne prend que les managés.

## 6. Preuve live

```bash
POST /api/vpn/socks5/rotate-egress
{"ok":true,"started":2}
```

Les six stations managées ont été recréées et ont obtenu six blocs distincts :

| Label | Nouvel egress | Nouveau bloc |
|---|---|---|
| `warp-ipv6` | `2a09:bac5:32d3:150f::219:3e` | `2a09:bac5:32d3:150f::/64` |
| `warp-ipv6-2` | `2a09:bac5:32d2:14dc::214:b5` | `2a09:bac5:32d2:14dc::/64` |
| `warp-ipv6-3` | `2a09:bac1:27e0:2060::3db:3e` | `2a09:bac1:27e0:2060::/64` |
| `warp-ipv6-4` | `2a09:bac5:32d2:150f::219:6c` | `2a09:bac5:32d2:150f::/64` |
| `warp-restore1` | `2a09:bac5:32d1:1519::21a:5b` | `2a09:bac5:32d1:1519::/64` |
| `warp-restore3` | `2a09:bac1:2780:2060::40e:2a` | `2a09:bac1:2780:2060::/64` |

La config `socks5_proxies` a été mise à jour avec ces valeurs.
