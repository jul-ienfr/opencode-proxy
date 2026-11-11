# Research — Catalogue d'egress et rotation

**Date**: 2026-11-09

## 1. État mesuré avant la feature

| Élément | Valeur |
|---|---:|
| Candidats publics collectés | 20 045 |
| Hôtes publics uniques | 10 994 |
| Échantillon public testé | 600 |
| Vivants (ipify) | 149 (24,8 %) |
| Free 200 au premier gate | 29 |
| Free 200 après quelques heures | 2 |
| 429 au dernier gate | 27 |
| Conteneurs WARP provisionnés | 12 (6 → 12 le 2026-11-09) |
| Blocs WARP `/64` uniques | 12 |
| Stations NordVPN actives | 6 |
| Services NordVPN compose | 10 |
| IP NordVPN `usable` (dernier check) | 0 (buckets journaliers épuisés) |

Constats :

- le rendement public est faible et périssable : 29 → 2 en quelques heures ;
- un 429 n'est pas un proxy mort, c'est un bucket épuisé ;
- WARP ajoute des **blocs `/64`**, pas des « stations » utiles ;
- NordVPN a plus de services compose que de sessions saines ; la limite de
  sessions est la contrainte réelle ;
- le pool actif n'a pas besoin de tout garder actif : il a besoin de savoir
  quoi activer lors d'une rotation.

## 2. Décisions

### D1 — SQLite `registry.db`, pas un JSON

La constitution III impose `registry.db` comme autorité. Le catalogue y ajoute
des tables versionnées, des index et des observations append-only. Les exports
JSON (`logs/egress_catalog.json`) sont en lecture seule pour le dashboard.

### D2 — Une option = un bucket de quota

La clé canonique est le **bloc d'egress** :

- public : IP de sortie (pas host:port) ;
- WARP : `/64` IPv6 observé (pas le conteneur) ;
- NordVPN : IP observée (pas le serveur).

Deux endpoints derrière la même IP ne créent qu'une option ; deux conteneurs
WARP qui partagent un `/64` ne créent qu'un bloc.

### D3 — `reachable` ≠ `usable`

- `reachable` : liveness/ipify OK ;
- `usable` : gate free `200` récent avec contenu.

Le catalogue ne promeut jamais un `reachable` seul. Cela évite les faux
positifs des proxys publics qui répondent à ipify mais sont bloqués/429 par
l'API.

### D4 — 429 = quota, avec `reset_at`

Le `Retry-After` ou le reset explicite du fournisseur est la seule source du
`reset_at`. Aucune IP n'est re-testée avant. Un 429 sans succès antérieur peut
être une mauvaise IP : le catalogue le maintient en `quota` court puis `dead`
si les échecs persistent, selon la politique du provider.

### D5 — Le public est validé via le worker isolé

La sonde publique ne doit jamais connecter le processus principal au proxy
public. Elle utilise :

- `free/public_egress_worker.py` (process ou conteneur) ;
- le wire free 2.2.5 réel ;
- `Authorization: Bearer public` ;
- TLS destination vérifié.

### D6 — WARP : catalogue de blocs, pas de stations

Le provisioning WARP reste un outil séparé. Le catalogue :

- lit les conteneurs et les `egress_ip` observés ;
- historise chaque `/64` avec TTL ;
- expire un bloc si le conteneur est recréé avec un autre `/64` ;
- ne propose que des blocs `usable` à la rotation.

### D7 — NordVPN : catalogue d'IP, pas de sessions

Le catalogue lit `vpn_status`/`registry.db` et historise chaque IP observée.
Une station AUTH_FAILED marque toutes ses IP `auth` ; une IP 429 marque
`quota` avec reset. La rotation peut changer de serveur **et** d'IP, mais ne
doit pas ouvrir plus de sessions que la capacité saine mesurée (6 par défaut,
10 services compose disponibles).

### D8 — Scheduler asyncio, single-flight, fail-soft

La boucle vit dans le processus proxy, démarre avec le lifespan, s'arrête avec
`close()`. Un cycle par provider à la fois. Un cycle échoué conserve le
catalogue et le pool précédents.

### D9 — Sources déclaratives

Une source est un enregistrement :

```yaml
- id: proxifly-https
  type: public_text
  url: https://raw.githubusercontent.com/...
  scheme: https
  interval_s: 21600
  enabled: true
```

Ajouter une source = ajouter une ligne, pas un module. Les parsers sont
enregistrés par `type` (`public_text`, `public_json`, `geonode_json`, `warp`,
`nordvpn`).

### D10 — Pas d'autoscaler en boucle

`scripts/autoscale_max_ips.py` a servi de transition one-shot. Il ne doit pas
être planifié : le scheduler du catalogue le remplace. Le catalogue ne crée
pas de stations NordVPN en boucle et ne dépasse pas la capacité de session.

## 3. Alternatives rejetées

| Alternative | Pourquoi rejetée |
|---|---|
| Un gros JSON unique | Pas de contraintes, pas d'index, pas d'observations append-only, corruption au crash |
| Re-tester tout le catalogue à chaque requête | Latence et quota ; le catalogue est le cache de preuve |
| Activer tous les endpoints publics trouvés | 27/29 étaient 429 ; le pool doit choisir, pas tout brancher |
| Ajouter 10 stations NordVPN | Limite de sessions ; AUTH_FAILED observé à 6 + test |
| Créer 24 conteneurs WARP | Coût RAM inutile ; le catalogue historise les `/64` et rotationne |
| Compter `host:port` | Deux ports derrière la même IP = un seul bucket |

## 4. Questions résolues

- **Combien d'options garder ?** Public : 64 actives, tout le catalogue
  jusqu'à 20k candidats. WARP : 12 blocs connus. NordVPN : 6 IP utilisables
  par défaut, 10 connues.
- **Quand expirer ?** `usable` 12 h, `quota` jusqu'au `reset_at` (plafond
  24 h), `dead` 7 j, `auth` jusqu'à réparation.
- **Qui répare ?** Le superviseur existant ; le catalogue observe et propose.
- **Où sont les secrets ?** Nulle part dans le catalogue ; les URLs publiques
  sans auth dans les sources, le worker isolé pour les tests.
- **Que faire si une source meurt ?** Garder les options des autres sources
  jusqu'à TTL ; un run échoué ne vide pas le catalogue.
