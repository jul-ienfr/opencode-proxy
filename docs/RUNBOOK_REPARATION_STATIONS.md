# Runbook — réparation et exploitation des stations free

**Date** : 31/10/2026
**Périmètre** : jambe free uniquement. Le client officiel et le modèle demandé
ne sont jamais modifiés.

---

## 1. Commandes de diagnostic

```powershell
# Santé globale + capacité free
Invoke-WebRequest -UseBasicParsing http://127.0.0.1:4000/health | Select-Object -Expand Content

# Détail stations / quota / spare
Invoke-WebRequest -UseBasicParsing http://127.0.0.1:4000/api/pool-status | Select-Object -Expand Content

# Santé par station + leases IP/quota
Invoke-WebRequest -UseBasicParsing http://127.0.0.1:4000/api/free-health | Select-Object -Expand Content

# Conteneurs VPN
docker ps --format "table {{.Names}}\t{{.Status}}\t{{.Ports}}"

# Derniers logs d'une station
docker logs --tail 120 opencode-vpn-4
```

Champs à lire en premier :

| Champ | Sens |
|---|---|
| `configured_stations` | stations actives attendues (hors spare) |
| `usable_stations` | stations réellement sélectionnables |
| `active_stations_with_quota` | stations avec quota restant |
| `warm_spare_ready` | spare qualifié, prêt à couvrir une rotation |
| `spare_promoted` | spare utilisé en secours |
| `capacity_degraded` | capacité < attendu, `/health` = `degraded` |
| `parked_stations` | stations en park, action opérateur |
| `repairing_stations` | réparation/probing en cours |
| `ip_state` | `warming/qualified/active/draining/exhausted/bad` |
| `quota_headroom` | requêtes restantes estimées sur l'IP |

---

## 2. Lecture rapide des états

### `capacity_degraded: true`

1. Regarder `stations[]` et `non_routable_reason`.
2. Si une station est `parked` : lire `cause` (`auth_throttle`, `credentials`,
   `egress`, `server`, `docker`).
3. Si une station est `repairing` : attendre la fin de la séquence auth
   (restart → stop 5 min → stop 15 min → pin → recreate → stack → park).
4. Ne jamais forcer un restart manuel pendant `repairing` : cela double les
   AUTH et peut aggraver le throttle NordVPN.

### `ip_state: draining`

La station a encore du quota mais approche du seuil de rotation (20 % du
profil). Le planificateur va la drainer et promouvoir le spare si
`warm_spare_ready=true`. Ne pas relancer de rotation manuelle.

### `ip_state: exhausted`

Le quota de l'IP est atteint. La rotation est attendue. Si aucune rotation
n'est en cours et que `rotate_pending` est vide, vérifier le watchdog.

### `ip_state: bad`

429 sans aucun succès sur cette IP. Elle est en quarantaine longue
(`bad_ip_ttl_s`). Ne pas la réutiliser : laisser la rotation ou le TTL.

### `warm_spare_ready: false`

Le 7e egress est configuré mais pas qualifié (auth, egress, quota ou health).
1. Vérifier `docker ps` : `opencode-vpn-7` doit être `healthy`.
2. Vérifier `spare_station` et `spare_promoted`.
3. Si le spare est `repairing`, attendre ; ne pas le mettre dans le pool actif.

## 2 bis. Récupération egress — objectif secondes à 4 minutes

Séquence automatique :

```text
0–30 s   : fast-pin / restart (le fast-pin peut corriger en quelques secondes)
30 s     : plafond d'attente auth_cooling pour l'egress
60 s     : bascule de stack OV↔WG
120 s    : recreate conteneur
240 s    : park + alerte
```

Champs utiles :

```text
vpn.watchdog.egress_dead_elapsed_s
vpn.watchdog.egress_restart_budget_used
vpn.watchdog.egress_flip_done
vpn.watchdog.egress_recreate_done
```

Si une station dépasse **240 s** en `tunnel_down`, elle doit être `parked` avec
`action_required=true`; sinon le watchdog ne fait pas son travail.

Le budget auth `3/h` ne doit **pas** bloquer cette séquence : le budget egress
est séparé (`6/h`). Si tu vois `budget-exhausted` sur un egress dead, c'est un
bug de politique, pas une limite NordVPN.

---

## 3. Tempête AUTH

Symptômes :

- `auth_failures_30m` élevé sur plusieurs stations ;
- `auth_global_pause_remaining_s > 0` ;
- `cause: auth_throttle` ou `auth_global` ;
- logs `AUTH_FAILED` répétés.

Actions :

1. **Ne pas** redémarrer les stations en boucle.
2. Laisser la garde globale faire son travail : une seule probe autorisée,
   pause 5 min puis 15 min.
3. Vérifier que les conteneurs des stations en pause sont bien arrêtés
   (pas seulement `sleep` proxy).
4. Si `cause: credentials` : le plan park directement, zéro restart.
   Vérifier les credentials NordVPN hors proxy, puis redémarrer le proxy.
5. Si toutes les stations sont parkées : arrêter la flotte, attendre au moins
   30 min, puis redémarrer une seule station et vérifier l'egress avant de
   remonter les autres.

---

## 4. Rollback

Les flags sont dans `config.yaml`, section `ip_rotation`.

```yaml
station_health:
  strict: false          # retour à l'ancien filtre de routabilité
ip_quota:
  enabled: false         # ancien seuil et ancien sélecteur
rotation_planner:
  enabled: false         # plus de rotation proactive
station_repair:
  enabled: false         # stations exclues, plus d'arrêt automatique
station_count: 6         # retire le 7e egress du runtime
```

Après modification :

```powershell
# le proxy relit config.yaml à chaud pour ces blocs ; sinon :
Stop-Process -Id <pid> -Force
python opencode.py --no-gui
```

Rollback complet :

1. `station_repair.enabled: false`
2. `rotation_planner.enabled: false`
3. `ip_quota.enabled: false`
4. `station_health.strict: false`
5. `station_count: 6`
6. redémarrer le proxy
7. vérifier `/health` et `/api/pool-status`

---

## 5. Alertes

Lancer :

```powershell
python scripts/vpn_alerts.py --base http://127.0.0.1:4000 --once
```

Alertes couvertes :

- capacité free dégradée ;
- spare froid ;
- station parkée ou `action_required` ;
- backoff modèle global ;
- lease IP `bad` ;
- SLO `breached` ;
- canary `failing`.

Brancher le webhook via `VPN_ALERT_WEBHOOK`.

---

## 6. Canary P6 (à exécuter après stabilisation)

1. Vérifier `pytest`, `ci_contracts`, `config_audit`.
2. Activer le mode shadow : `station_health.shadow: true`. Comparer
   `last_selection` avec le routage réel.
3. Passer 5 % du trafic sur le nouveau sélecteur (flag canary dédié).
4. Surveiller 24 h : 429, p95 TTFB, capacité, rotations.
5. Passer 50 %, puis 100 %.
6. Rollback immédiat si 429 > 0,5 %, p95 > 15 s ou capacité < N-1.

**Ne pas** activer le canary pendant une tempête AUTH ou si le spare est froid.

---

## 7. Ce qu'il ne faut jamais faire

- Modifier `~/.config/opencode/opencode.jsonc`.
- Changer de modèle ou utiliser un paid fallback.
- Redémarrer toutes les stations en même temps.
- Servir une IP `bad`, `warming`, `exhausted` ou sans quota.
- Forcer `6/6` dans le dashboard alors que `/health` dit `degraded`.
- Lancer une rotation manuelle pendant `repairing`.
