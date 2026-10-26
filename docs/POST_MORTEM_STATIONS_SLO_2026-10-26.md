# Post-mortem — stations VPN, stacks et rotation IP

**Date** : 2026-10-26  
**Périmètre** : 6 stations gluetun, rotation d'IP free, API dashboard, watchdog.  
**Statut** : correctifs déployés, flotte stable 6/6, observation SLO 24 h en cours.

## 1. Résumé exécutif

La flotte a subi une tempête de reconnexions lors de bascules **globales** de
stack (WireGuard ↔ OpenVPN) : toutes les stations étaient recréées en même
temps, ce qui déclenchait des `AUTH_FAILED` NordVPN en série et des
healthchecks WireGuard en boucle à cause de timeouts DNS. Le contrôleur
`/api/vpn-status` attendait en plus jusqu'à 6 scans Docker et répondait
`stale` / timeout.

Depuis les correctifs et la remise en état du 2026-10-26 13:45 (local) :

- **6/6 stations healthy et routables**, `boot_error: null` ;
- `/api/vpn-status` répond en **p50 14 ms / p95 54 ms** (10 appels), sans
  timeout, `stale: false` en régime stable ;
- les bascules sont désormais **par station** (`stations=[sid]`) et ne
  recréent plus la flotte entière ;
- les holds anti-flap (60 s voie secondaire, 30 s degraded) et le throttling
  des logs de churn réduisent le bruit et les oscillations ;
- une observation SLO de 24 h tourne via `scripts/station_slo_watch.py`.

## 2. Impact

| Symptôme | Mesure / preuve |
|---|---|
| `AUTH_FAILED` en rafale | 1 224 lignes `AUTH_FAILED` dans `logs/debug.log` sur la fenêtre 11:00–12:00 ; `auth_fail_30min` monté à 7–9 sur s5/s6 |
| Healthchecks WG en boucle | 51 `Serveur VPN injoignable`, 33 `healthcheck-restart churn`, resets VPN répétés |
| Stations non routables | `boot_error` observé de `1/6` à `5/6`, `healthy`/`routable` entre 1 et 5 |
| Dashboard stale | `refresh timeout after 2.0s/5.0s`, réponses `stale: true` à chaque poll |
| Voie secondaire bruitée | 511 logs `resets voie secondaire` sur la fenêtre |

Aucune perte de données : le pool a continué à router sur les stations
disponibles. L'impact est une dégradation de capacité et de latence de
contrôle, pas une indisponibilité totale.

## 3. Chronologie (UTC)

| Heure | Événement |
|---|---|
| 11:15 | Timeouts DNS dans les conteneurs WG (`lookup api.ipify.org`, `github.com` sur `127.0.0.1:53`), gluetun redémarre le VPN en boucle |
| 11:19 | Seuil de churn atteint ; resets de voie secondaire et `degraded` répétés |
| 11:23 | s4 (WG) échoue ses healthchecks DNS ; `connect` station timeout |
| 11:27 | Bascule globale vers OpenVPN (`vpn_stack: openvpn`) : `AUTH_FAILED` sur plusieurs stations |
| 11:28 | `AUTH_FAILED` sur `ch515`/`fr968` ; cooldown auth 300 s sur s5/s6 |
| 11:36 | s4 repasse en WG par station, nouvelle IP |
| 11:47 | Bascule per-station de s5/s6 ; s6 timeout côté API mais applique ensuite |
| 11:54 | Flotte stabilisée en mixte (s1–s3 OV, s4–s6 WG), 6/6 |
| 12:04 | s4 recréée en OV au boot (divergence `.env`/état) → `AUTH_FAILED` |
| 12:07 | s4 repassée en WG par l'API per-station, IP `186.247.161.52` |
| 12:18 | Redémarrage (charge SWR + correctifs) |
| 12:19 | Watcher SLO (fenêtre intermédiaire) |
| 12:27 | Auto-flip WG→OV de s5 sur churn healthcheck (comportement attendu) |
| 12:32 | s6 guérie sur reset ancien, mais le compteur `_pin_resets_1h` gonflait encore (6-7/h artificiels) |
| 12:41 | Dédup compteur + healing, suite de tests verte, redémarrage |
| 12:42 | Boucle AUTH s2 sur `ch488` (7 échecs/30 min), auth cooling |
| 12:44 | s2 bascule en WireGuard par station → 6/6 |
| 12:45 | Watcher SLO (fenêtre intermédiaire) ; 6/6, `stale: false` |
| 13:00 | Watcher qualité/upstream (fenêtre intermédiaire) |
| 13:20 | Mesure TTFB corrigée (non-streaming exclu), sélecteur cold-start déterministe, persistance stack manuel ; tests verts |
| 13:30 | Redémarrage, s2 restaurée en WG depuis l'état (`stack_manual`), fenêtre SLO intermédiaire |
| 13:47 | Bruit heal réduit (INFO 1× puis debug), redémarrage final, 6/6, fenêtre SLO finale |

## 4. Causes racines

1. **Bascule globale = orage d'AUTH.** `_apply_stack(stations=None)` recréait
   toutes les stations dans une seule commande compose. Chaque recréation
   refait une vraie authentification NordVPN ; six AUTH simultanées sur un
   compte déjà sollicité déclenchent des `AUTH_FAILED` en cascade et des
   cooldowns. Le flip global est maintenant réservé aux cas exceptionnels.
2. **Healthcheck WG dépendant du DNS.** Le healthcheck gluetun résout
   `github.com`/`cloudflare.com` via le DNS interne (`127.0.0.1:53`). Au boot
   à froid, ces lookups expirent alors que le tunnel peut déjà porter du
   trafic. Gluetun interprète cela comme un échec VPN et redémarre le tunnel :
   boucle de healthcheck, faux `error`, churn.
3. **Divergence état mémoire / `.env` / conteneur.** Le reconcile lisait le
   `VPN_TYPE` réel du conteneur, mais l'état restauré pouvait désigner une
   autre stack. s4 a ainsi été recréée en OpenVPN au boot alors qu'elle
   tournait en WireGuard, avec `AUTH_FAILED` à la clé.
4. **Endpoint per-station incomplet.** La première version utilisait
   `set_stack(propagate=False)`, qui ne faisait que **simuler** le changement
   d'état sans recréer le conteneur. Résultat : un `ok: true` trompeur et un
   état mémoire faux. Corrigé avec `stations=[sid]` + `force=True`.
5. **Contrôle bloquant.** `/api/vpn-status` attendait le refresh de 6
   stations (inspect + logs + control). Le timeout de 2 s (puis 5 s) expirait
   et la réponse partait `stale`, sans revalidation fiable en arrière-plan.
6. **TTFB pollué par les réponses non-streaming.** Pour un non-streaming, la
   durée totale du completion était injectée dans l'EWMA de latence. Une
   réponse longue (mais tunnel rapide) faisait chuter le score de la station :
   s6 affichait 16 s d'EWMA / 45,9 s de p95 alors que sa probe était à 0,9 s.
   Le sélecteur évitait donc une station correcte pour la mauvaise raison.
7. **Choix manuel de stack non persisté.** Sous `vpn_stack: auto`, l'état
   `stack=wireguard` était ignoré au reload (garde anti-verrouillage). Chaque
   redémarrage recréait s2 en OpenVPN → nouvelle AUTH, boucle `ch488`,
   indisponibilité. Distinct d'un ancien état sans drapeau : `stack_manual`
   marque désormais un choix utilisateur explicite.

## 5. Correctifs déployés

| Correctif | Fichier / clé |
|---|---|
| Stacks par station, pas de flip global par défaut | `vpn/manager.py` (`stations`, `force`), `config.yaml` (`auto_mixed_stacks: false`) |
| Endpoint `POST /api/vpn/station/{sid}/stack` réellement per-station | `dashboard/api.py` |
| Stale-while-revalidate non bloquant + single-flight | `dashboard/api.py` (`asyncio.ensure_future`, cache 5 s) |
| Seuil `stale` distinct du refresh (15 s / 3× cache) | `dashboard/api.py` |
| Holds anti-flap voie secondaire / degraded | `ops/health.py` (`secondary_path_hold_s: 60`, `degraded_hold_s: 30`), `vpn/manager.py` |
| Guérison des resets anciens + dédup compteur 1 h | `vpn/manager.py` (`_stale_issue`, `_last_pin_reset_event_at`) |
| TTFB non-streaming exclu de l'EWMA (`latency=False`) | `free/quality.py`, `opencode.py` |
| Sélecteur cold-start déterministe (sans exploration avant probe) | `free/selector.py` |
| Persistance du stack manuel par station (`stack_manual`) | `vpn/manager.py` |
| Tests non-régression per-station, resets, TTFB, persistance | `tests/test_vpn_stack_nstation.py`, `tests/test_graceful_aurora.py`, `tests/test_stations_roadmap.py` |
| Throttling logs churn (5 min) et resets secondaires en debug | `vpn/manager.py` |
| Heal reset ancien : INFO au 1er événement, debug sur les relectures | `vpn/manager.py` |
| Réconciliation `.env` ↔ conteneurs | `scripts/station_env_reconcile.py --apply` |
| Observation SLO 24 h + rapport | `scripts/station_slo_watch.py`, `scripts/station_slo_report.py` |
| Secret `control_api_key` hors `config.yaml` | `.env` + `scripts/migrate_control_key.py` (valeur non exposée) |

Décision conservée : image gluetun `:latest` pour bénéficier des hotfixes
amont. Le risque de régression est accepté et surveillé par le watchdog.

## 6. Validation live (2026-10-26 13:49Z)

| Indicateur | Valeur |
|---|---|
| Stations | 6/6 `healthy`, 6/6 `routable` |
| `boot_error` | `null` |
| `stale` / `refresh_error` | `false` / `null` en régime stable |
| Latence API (10 appels) | p50 **14 ms**, p95 **54 ms**, max 179 ms |
| Latence watcher | 47 ms sur l'échantillon final ; 15–63 ms en régime |
| Qualité | `quality.enabled: true`, entrées présentes pour les 6 stations |
| Flap max par station | 1 hors incident (s2=3, s5=1 après le dernier restart) |
| Rotations sur la fenêtre | 0 |

Stack live : **s1/s3/s4 OpenVPN, s2/s5/s6 WireGuard**. L'auto-policy peut
basculer une station WG→OV sur churn healthcheck ; `.env` est réaligné après
chaque bascule (`scripts/station_env_reconcile.py --apply`).

### Qualité mesurée (après correctif, 13:49Z)

| Indicateur | Valeur |
|---|---|
| Probe TTFB (6 stations) | 0,8–1,8 s (p95 1,8 s) |
| TTFB réel (EWMA) | **mesure corrigée** : les non-streaming (durée totale du completion) ne polluent plus l'EWMA |
| Taux de succès (snapshots) | 100 % |
| 429 récents | 0 |
| Fallback payant | 0 |
| Scores qualité | re-mesure en cours sur la fenêtre finale ; la probe reste 1,2–1,8 s |

Avant correctif, s6 affichait 16 s d'EWMA / 45,9 s de p95 : c'était la durée
de complétion de réponses non-streaming, pas la latence du tunnel. Aucun
tuning de `rotate_floor` n'est fait pendant la fenêtre 24 h ; si le TTFB
corrigé reste élevé, l'action prévue est un canari `rotate_floor: 0.55` +
`latency_ceiling_ms: 10000`, puis re-mesure avant généralisation.

## 7. SLO et cibles 24 h

Fenêtre : **2026-10-26T13:49Z → 2026-10-27T13:50Z**.

| SLI | Cible | Base 13:49Z |
|---|---|---|
| Flotte complète (`healthy == total` et `routable == total`) | ≥ 99 % | 100 % |
| Stations routables | ≥ 99 % | 100 % |
| Latence API p95 | < 300 ms | 16–63 ms |
| Réponses `stale` | < 1 % | 0 % |
| Âge max de l'état (`status_age_s`) | ≤ 120 s | 13–60 s |
| `refresh_error` | < 0,5 % | 0 % |
| `boot_error` après boot | 0 | 0 |
| `flap_count_1h` par station | ≤ 2 en régime stable | 1 |
| `AUTH_FAILED` par station | ≤ 1 / 24 h | burst s2 avant bascule WG ; cible 24 h à confirmer |
| Probe TTFB p95 | < 2,5 s | 1,8 s |
| TTFB réel (EWMA) p95 par station | < 5 s | à re-mesurer (mesure corrigée) |
| Taux de succès free | ≥ 99 % | 100 % |
| 429 récents | 0 | 0 |
| Fallback payant | 0 | 0 |

> Les compteurs `flap_count_1h` de s2/s5 incluent la tempête initiale
> (AUTH/churn) : le seuil ≤ 2 s'apprécie **hors fenêtre d'incident**, après
> expiration de la fenêtre glissante d'une heure.

**Rapport final** :

```powershell
python scripts\station_slo_report.py --in logs\station_slo_final.jsonl `
  --since 2026-10-26T13:49:28Z --until 2026-10-27T13:50:28Z --markdown
```

Un **finaliseur** tourne en tâche détachée (PID **32100**,
`scripts/station_slo_finalize.py`) : à l'échéance, il écrira automatiquement
[POST_MORTEM_SLO_FINAL_2026-10-27.md](POST_MORTEM_SLO_FINAL_2026-10-27.md).
S'il est relancé après l'échéance, il produit le rapport immédiatement.

Le watcher tourne en tâche détachée (`scripts/station_slo_watch.py`,
PID **38476**, sortie `logs/station_slo_final.jsonl`, échantillon 60 s,
avec qualité/TTFB/429/rotations/fallback). En cas de redémarrage machine,
relancer :

```powershell
Start-Process python -ArgumentList "scripts\station_slo_watch.py","--duration","86400",`
  "--out","logs\station_slo_final.jsonl" `
  -WorkingDirectory "C:\Users\julie\Downloads\opencode-proxy-one" -WindowStyle Hidden
Start-Process python -ArgumentList "scripts\station_slo_finalize.py",`
  "--since","2026-10-26T13:49:28Z","--until","2026-10-27T13:50:28Z",`
  "--in","logs\station_slo_final.jsonl" `
  -WorkingDirectory "C:\Users\julie\Downloads\opencode-proxy-one" -WindowStyle Hidden
```

## 8. Risques résiduels et actions

1. **DNS WG instable** : si un healthcheck WG repart en boucle, basculer
   **uniquement la station concernée** en OpenVPN via
   `POST /api/vpn/station/{sid}/stack` puis
   `python scripts/station_env_reconcile.py --apply`. Ne jamais relancer un
   flip global.
2. **AUTH NordVPN** : une bascule per-station refait une AUTH (acceptable).
   Éviter les bascules simultanées de plusieurs stations.
3. **Layout adaptatif** : l'auto-policy peut basculer une station WG→OV
   (churn healthcheck) ou OV→WG (AUTH/loop). Vérifier après chaque bascule
   que `.env` == conteneur réel (`station_env_reconcile.py --apply`) et que
   `secondary_path_issue` est retombé. Ne pas figer une répartition 4/2 si
   la santé impose autre chose.
4. **Rapport SLO final** : à produire après l'échéance du 2026-10-27T13:50Z
   et à joindre à ce document.
5. **Tests** : suite complète verte hors Docker
   (`python -m pytest -k "not docker" -q`). Le prochain restart ne doit pas
   être utilisé pour tester un flip global.

## 9. Annexes — commandes utiles

```powershell
# État flotte
Invoke-RestMethod http://127.0.0.1:4000/api/vpn-status
Invoke-RestMethod http://127.0.0.1:4000/api/pool-status

# Bascule d'UNE station (jamais globale)
Invoke-RestMethod -Method Post -Uri http://127.0.0.1:4000/api/vpn/station/6/stack `
  -ContentType application/json -Body '{"stack":"openvpn"}'

# Alignement .env sur les conteneurs réels
python scripts\station_env_reconcile.py --apply

# Rapport SLO (fenêtre bornée)
python scripts\station_slo_report.py --in logs\station_slo_final.jsonl `
  --since 2026-10-26T13:49:28Z --until 2026-10-27T13:50:28Z

# Finalisation automatique (si le processus détaché a été perdu)
python scripts\station_slo_finalize.py --in logs\station_slo_final.jsonl `
  --since 2026-10-26T13:49:28Z --until 2026-10-27T13:50:28Z
```
