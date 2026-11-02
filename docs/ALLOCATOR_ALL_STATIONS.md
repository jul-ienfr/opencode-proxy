# Allocateur toutes-stations — comportement et exploitation

> 2026-11-02. Implémentation du contrat opérateur : chaque station doit devenir
> fonctionnelle indépendamment, avec sa propre IP, un catalogue partagé et une
> règle « tester puis conserver ».

## 1. Contrat

1. **Toutes les stations sont dans la boucle** (`station_count`, moins les
   stations explicitement désactivées par l’opérateur).
2. Une station non fonctionnelle déclenche une **rotation d’IP** : recherche
   d’un hostname, bind, vérification d’egress, probe officiel.
3. **Dès qu’un probe retourne 200**, l’IP est écrite dans le registre partagé
   (`ip_state` + `assignments` + `observations`) et **conservée** sur la station.
4. **Une IP n’appartient qu’à une station** : claims exclusifs par hostname et
   index d’unicité en base.
5. **Catalogue partagé** : `servers` (9948 entrées), `catalog_claims.db`
   (qui a testé quoi), `ip_registry.db` (observations et assignations).
6. Les stations réparent **en parallèle**, avec une borne :
   `max_parallel_repairs` chaînes simultanées et un **budget global de rebind**
   (`rebind_limit_per_min`) pour ne pas faire tomber le compte NordVPN.

## 2. Composants

| Composant | Rôle |
|---|---|
| `scripts/fresh_chain_supervisor.py` | boucle toutes-stations, détecte les besoins, lance/arrête les chaînes, réconcilie le registre |
| `scripts/fresh_chain.py --claims` | chaîne par station : claim → bind → egress → probe → 200/429/erreur |
| `free/catalog_claims.py` | catalogue partagé, claims exclusifs, reset des 429, budget de rebind global |
| `free/ip_registry/adapters/compose_binder.py` | bind Compose par station, **override file par station** |
| `free/ip_registry/sqlite_registry.py` | `assign_observed()` : 200 → claim → record → assign ; `release_station()` |
| `free/ip_registry/status.py` | état live : sources, `stale`, `no-adapter`, attribution, qualité |

## 3. Détection du besoin

Une station est réparée si :

- `station_usable=false` (tunnel, santé, egress, auth) ;
- `model_usable=false` avec `daily-bucket-exhausted`, `bad-ip`,
  `station-unusable`, `quota`, `tunnel-down` ;
- `non_routable_reason` commence par `quota=`, `bad_until`, `egress=`,
  `status=`, `health=`, `auth=`, `no-ip` ;
- `vpn_status` est `error`, `disconnected`, `connecting`, `stopped` ;
- aucune IP courante.

Un simple cooldown modèle transitoire (`model-cooldown`) ne déclenche pas de
rotation : elle brûlerait du quota sans changer l’IP.

## 4. Cycle d’une chaîne

```text
claim_next(station)          # hostname exclusif, priorité pays autorisé + load
  → compose up --force-recreate (override propre à la station)
  → mesure de l'egress réel
  → probe officiel free (modèle principal puis buckets de repli)
     200  → assign_observed(ip, host, station) + winner + stop
     429  → claim 429 + reset_at, candidat suivant
     err  → claim no_egress/bind_error, candidat suivant
```

Après `--force-recreate`, le binder attend `settle_s` puis re-sonde l’egress
jusqu’à `egress_wait_s` (20 s, poll 3 s) avant de conclure `no-egress` : un
tunnel WireGuard lent n’est plus condamné sur un unique essai à froid.

Une IP 429 sur `deepseek-v4.1-flash-free` peut être 200 sur
`glm-5.3-flash-free` ou `mimo-v2.6-flash-free` : la chaîne teste les buckets de
repli avant de condamner l’IP.

Le superviseur réconcilie ensuite l’état live : si une station est
`station_usable` **et** `model_usable`, son IP/host courants sont enregistrés
dans `ip_state`/`assignments` même si la chaîne a été arrêtée juste après le
probe.

## 5. Concurrence et garde-fous

- `max_parallel_repairs=2` par défaut : deux stations avancent réellement en
  parallèle ; les autres sont dans la file. Le compte NordVPN supporte mal
  plusieurs ré-authentifications simultanées.
- `rebind_limit_per_min=4` : budget global partagé par toutes les chaînes
  (`catalog_claims.rebind_budget`).
- `winner_grace_s=20` : le superviseur laisse la chaîne finir son probe avant
  de l’arrêter, pour ne pas perdre le hostname gagnant.
- `failure_cooldown_s=300` : une chaîne qui épuise le catalogue sans 200 ne
  redémarre pas en boucle serrée.
- `winner_cooldown_s=120` : une station encore malade juste après un winner
  laisse au proxy le temps de publier son état avant une nouvelle recherche.
- **Override Compose par station** : le bug du fichier unique
  `logs/compose-hostname.yml` faisait écrire deux stations dans le même
  override ; les binds partaient sur le mauvais hostname. Chaque station a
  maintenant `logs/compose-hostname-<n>.yml`.
- **Cache candidats** : `fresh_chain` réutilisait
  `logs/fresh_chain_candidates.json` sans limite, donc l’ancien ordre
  (public mondial avant `al*`) survivait aux corrections. Le cache expire
  maintenant en 30 min et le superviseur passe `--refresh` à chaque chaîne.
- **Watchdog prioritaire** : l’allocateur ne recrée pas une station que le
  manager est déjà en train de réparer (`repairing`/`probing` < 10 min). Un
  park **auth/credentials** est respecté ; un park **egress/deadline** est au
  contraire réparable par une rotation d’IP, donc l’allocateur reprend la main.

## 6. Exploitation

Statut :

```bash
curl -s http://127.0.0.1:4000/api/free/allocator/status | jq
curl -s http://127.0.0.1:4000/api/free/ip-registry/status | jq
```

La page **Free Models** du dashboard contient aussi la carte
**« Réparation des stations »** (`AllocatorStatus.svelte`) : mode, chaînes,
dernier statut, winner, claims 429, prochain reset et assignations ouvertes.

`/api/free/allocator/status` expose les chaînes actives, leur PID/âge, le
nombre de candidats testés, le dernier statut, le winner et les claims
(`catalog`, `429`, `bind_error`, `next_reset_in_s`). `/api/free/ip-registry/status`
expose la provenance, les assignations ouvertes et la qualité.

Le payload contient :

- `enabled`, `allocator_mode`, `binder_mode` ;
- `db.servers` (total, actifs, stale, par source) ;
- `db.ip_state` (`assigned`, `candidate`, `exhausted`, `error`) ;
- `no_adapter` (sources déclarées sans adaptateur) ;
- `host_attribution` (requêtes free VPN avec/sans hostname) ;
- `quality` (entrées, hostnames, échantillons tok/s).

Logs :

```text
logs/fresh_chain_supervisor.log   # décisions, winners, réconciliation
logs/fresh_chain_<n>.log          # détail des candidats par station
logs/fresh_chain_state_<n>.json   # résultats et winner par station
```

Vérifier les assignations :

```bash
sqlite3 logs/ip_registry.db \
  "SELECT station, egress_ip, hostname FROM assignments WHERE released_at IS NULL;"
sqlite3 logs/catalog_claims.db \
  "SELECT status, COUNT(*) FROM claims GROUP BY status;"
```

## 7. Preuve live du 2026-11-02

- Station 3 : `al143.nordvpn.com` → egress `186.247.44.119`, probe
  **200**, enregistrée `assigned`.
- Station 6 : `al73.nordvpn.com` → egress `87.120.102.39`, probe **200**,
  enregistrée `assigned` (pool albanais `87.120.102.x`).
- Attribution runtime : **18/18** requêtes free VPN avec hostname
  (couverture 1,0) au dernier contrôle.
- Qualité reconstruite : 34 entrées, 3 hostnames, 10 échantillons tok/s.
- Registre catalogue : 9948 serveurs actifs, 7229 API + 2719 embarqués
  `stale=1`.
- Au dernier contrôle, les autres stations étaient encore en réparation
  manager ou en rotation : le gate free renvoyait des **429**
  (`Retry-After` ~11-14 h) sur de nombreux candidats. Le système continue ;
  il ne peut pas fabriquer un 200.

## 8. Limites honnêtes

- Une station ne peut devenir fonctionnelle que si un hostname produit un
  **vrai 200**. Si l’amont free renvoie 429 sur toutes les IP testées, le
  système continue de tourner mais ne peut pas inventer un succès.
- Les 429 sont conservés avec `reset_at` : le hostname n’est pas retesté avant
  son reset.
- Les binds `no-egress` sont des échecs de serveur, pas des 200 : ils ne sont
  jamais conservés.
- **OpenVPN testé** le 2026-11-02 sur la station 5 : le binder obtient bien un
  egress (`187.15.130.59` via `al46`), mais le gate free renvoie 429. Le
  support `--transport openvpn-tcp|openvpn-udp` et le filtrage des candidats
  par technologie existent, mais OpenVPN n’est pas activé par défaut.
- Le registre `servers` ne prouve pas l’egress ; seule la sonde le fait.
- `nordcache` reste sans décodeur : `no-adapter:nordcache_packed` est visible
  dans le statut.

### Uplinks proxy NordVPN (SOCKS5 / HTTPS)

Les proxies NordVPN ne remplacent pas les stations Docker : ce sont des
uplinks statiques, utilisés seulement quand aucune station n'est utilisable
pour le modèle demandé.

Ordre de tentative : **station Docker → SOCKS5 → HTTPS**.

- `socks5_fallback_enabled` (défaut `false`) arme l'étage SOCKS5.
- `https_fallback_enabled` (défaut `false`) arme l'étage HTTPS.
- `Socks5Endpoint.refresh_egress()` résout l'IP réelle avant le premier
  usage : le ledger de quota est indexé sur l'egress, pas sur
  `socks5:host:port`.
- `proxy_mode: socks5` reste un override global manuel ; ce n'est pas le
  mode normal.
- Le test SOCKS5 du dashboard réutilise les credentials stockés, sans les
  renvoyer au navigateur.
- HTTPS : le certificat du proxy ne couvre pas le hostname, donc la
  vérification TLS du proxy est désactivée ; l'origine reste vérifiée.
- Scan complet du 2026-11-02 : sur 67 proxies SOCKS, 2 acceptent les service
  credentials (`socks-nl1`, `socks-nl3`) et les deux renvoient 429 ; sur 100
  proxies HTTPS, 0 accepte les credentials (407). Conclusion opérationnelle :
  SOCKS5 d'abord, HTTPS reste désactivé tant que l'auth proxy n'est pas
  rétablie.

## 9. Configuration

```yaml
ip_registry:
  enabled: true
  allocator:
    mode: enforce
    max_parallel_repairs: 2
    rebind_limit_per_min: 4
    winner_cooldown_s: 120
    winner_grace_s: 20
    failure_cooldown_s: 60
    max_candidates_per_chain: 20
    probe_models:
      - deepseek-v4.1-flash-free
      - glm-5.3-flash-free
      - mimo-v2.6-flash-free
    global_429_window_s: 600
    global_429_threshold: 20
    global_429_pause_s: 900
    poll_interval_s: 20
```

Rollback : `ip_registry.allocator.mode: off` laisse le superviseur inactif ;
les stations conservent leur IP courante. `ip_registry.enabled: false` coupe
l’usage runtime du registre.
