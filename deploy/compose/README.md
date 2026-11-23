# Overlays Compose — control plane des stations

Ces 6 fichiers portent **la configuration qui fait fonctionner la flotte
VPN**. Ils étaient auparavant dans `logs/` (donc **ignorés par git**), ce qui
signifiait qu'un `git clone` neuf ne reproduisait pas une flotte
fonctionnelle, et qu'une purge de `logs/` détruisait le control plane.

**Sortis de `logs/` le 2026-11-20** (finding V-33 de l'audit).

---

## Ce qu'ils apportent

Chaque overlay surcharge **une** station du `docker-compose.yml` principal
avec :

| Clé | Valeur | Rôle |
|---|---|---|
| `image` | `gluetun-016:local` | **le fork**, seul à porter l'API de contrôle v2 |
| `pull_policy` | `never` | interdit tout retour à l'image upstream |
| `ports` | `127.0.0.1:800N:8000` | expose le control plane (8001…8006) |
| `CONTROL_PLANE_ENABLED` | `"true"` | active `/v1/vpn/*` |
| `CONTROL_EGRESS_SOURCES` | 5 sources | mesure d'egress multi-sources (principe XI) |
| `DNS_LADDER` | `doh,plain,dot` | chaîne de repli DNS |
| `TRANSPORT_*` | 100 s / 1 essai | budget de transport borné |

## Pourquoi `pull_policy: never`

`docker-compose.yml` déclare `qmcgaw/gluetun@sha256:4af0c804…` (l'image
**upstream**) pour 5 des 6 services. **Seul `vpn-gluetun-4` déclare
`gluetun-016:local`.**

L'image upstream **n'a pas** l'API de contrôle v2 : les rotations tombent en
**404**. `free/ip_registry/adapters/compose_binder.py:121` l'affirme
explicitement :

```python
if not tag.startswith("gluetun-016:"):
    # A legacy image cannot serve the control plane; migrate it on
    # the next bind, one station at a time.
    return True
```

`pull_policy: never` empêche Compose de remplacer le fork par l'upstream lors
d'une recréation.

## Comment les utiliser

```bash
# station 1 seule
docker compose -f docker-compose.yml -f deploy/compose/compose-control-plane-1.yml up -d vpn-gluetun

# les 6 stations
for n in 1 2 3 4 5 6; do
  docker compose -f docker-compose.yml \
                 -f deploy/compose/compose-control-plane-$n.yml \
                 up -d vpn-gluetun${n:+-$n}
done
```

**Attention** : le nom du service est `vpn-gluetun` pour la station 1, et
`vpn-gluetun-N` pour les autres.

## Ce qui reste à faire (T005, suite)

**Le code ne référence pas encore ces overlays.** `vpn/manager.py` invoque
`docker compose` à **8 sites** avec un seul `-f` et **zéro `--profile`**
(vérifié : `grep -c -- "--profile" vpn/manager.py` → 0). Le paramètre
`extra_compose_files` existe dans `compose_binder.py:36` mais **n'a aucun
appelant de production**.

**Tant que ce n'est pas fait** :

- un `docker compose up -d` lancé **sans** ces overlays ramène les stations
  sur l'image upstream ;
- l'auto-update (`vpn/manager.py:9423`, `image = "qmcgaw/gluetun"`) surveille
  et *pull* l'image upstream pendant que la flotte tourne le fork.

**Prochaine étape** : brancher `extra_compose_files` dans `vpn/manager.py` et
faire du compose principal la source unique qui inclut ces overlays.

## Historique

| Date | Événement |
|---|---|
| 2026-11-14 08:45 | création des 6 overlays dans `logs/` |
| 2026-11-20 | **déplacés ici et versionnés** (finding V-33) |
| 2026-11-20 | `docker-compose.yml` corrigé pour déclarer le fork sur les 6 services (T005) |

## Voir aussi

- `docs/audits/2026-11-20-audit-complet/00-verifications-croisees.md` §V-29, V-33, V-35
- `specs/024-audit-optimisation-globale/tasks.md` — tâche T005
- `ops/runbooks/control-plane.md` — prérequis et déploiement
