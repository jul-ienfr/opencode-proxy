# Plan d'audit — Pourquoi 1 seule station sur 6 est routable

Date : 2026-09-08 | Périmètre : `opencode-proxy-one`, flotte de 6 tunnels gluetun/NordVPN alimentant le pool free
Statut : plan d'audit complet + plan de correction — prêt à exécuter, chaque étape a sa commande et son critère d'interprétation.

---

## 0. Réponse courte (TL;DR)

« Une seule station routable » ne vient **jamais** d'une seule cause : c'est l'effet cumulé de 4 mécanismes qui retirent des stations du pool indépendamment, jusqu'à ce qu'il n'en reste qu'une. D'après l'état capturé dans `pool_snapshot.json` :

| Station | État observé | Cause probable |
|---|---|---|
| 1 | `error` | `AUTH_FAILED - identifiants NordVPN rejetés` + `gluetun non sain après redémarrage` |
| 2 | `connected` ✅ | — |
| 3 | `connected` | dernier essai : `gluetun non sain après redémarrage` (instable, à la limite de retomber) |
| 4 | `error` | `Serveur VPN injoignable - échec négociation TLS (liste serveurs obsolète ?)` |
| 5 | `connected` ✅ | — |
| 6 | `error` | `Serveur VPN injoignable - échec négociation TLS (liste serveurs obsolète ?)` |

Et même les stations `connected` peuvent être exclues du service par les marqueurs internes (`bad_until`, cooldown latence, fenêtre de rotation) — le code le sait lui-même : `free/rotation.py:611` documente que l'escalade soft→hard peut faire « tomber le pool à 1/N routable ».

**Objectif « toutes routables tout le temps » :** littéralement impossible (une rotation VPN = micro-coupure, NordVPN rate-limite). L'objectif correct et atteignable : **au moins `N-2` stations éligibles ≥ 99 % du temps, jamais moins d'1 servable à un instant donné, MTTR < 2 min par station**. Ce plan audite puis corrige pour y arriver.

---

## 0bis. Objectif final (exigence utilisateur — 2026-09-08)

Le but n'est pas « réparer 1/6 » mais un système déterministe :

- **O1 — Zéro configuration morte.** Chaque clé de `config.yaml` choisie par l'opérateur DOIT être lue par le code ET changer observablement le comportement. Toute clé ignorée est un bug. Preuve de violation déjà constatée dans les logs : `watchdog_max_restarts_per_hour: 3` (config.yaml:297) vs **22 restarts/30 min appliqués** (F1) — la clé existe, elle n'est pas appliquée sur cette voie.
- **O2 — N stations, toutes connectées et utilisables en permanence**, N au choix de l'opérateur (6 aujourd'hui). « Utilisable » = éligible au sélecteur ; une station en rotation reste servable sur son ancienne IP (cut-over sans trou). Littéral « jamais une coupure » n'existe pas en VPN : l'invariant garanti est **jamais moins de N−2 utilisables, jamais 0**, dégradé inclus.
- **O3 — La stack suit le mode choisi, exactement.**
  - `vpn_stack: auto` (défaut) → les **3 stacks simultanées** sur la flotte (WG, OpenVPN UDP, OpenVPN TCP réparties station par station, ex. `n mod 3`), avec répartition compensatoire si une stack tombe.
  - `vpn_stack: wireguard` → **toutes** les stations en WireGuard, jamais de flip silencieux vers OpenVPN.
  - `vpn_stack: openvpn` → **toutes** en OpenVPN ; `ovpn_protocol: tcp|udp` (et overrides `OPENVPN_PROTOCOL_STATIONn`) strictement respectés.
  - Aujourd'hui constaté dans le code : l'hétérogénéité n'existe qu'au boot et seulement pour 2 stations (`opencode.py:1866-1883`, `auto_hetero_boot`, WG + OV-UDP), seulement en `auto`, et désactivée (`auto_hetero_boot: false`). Le mode mixte 3-stacks sur N stations **n'existe pas encore** — c'est un travail à construire, pas à réparer.
- **O4 — Fiabilité mesurable** : SLO par station ≥ 99 % de minutes « utilisables » (hors fenêtre de rotation normalisée), MTTR < 2 min, qualité = egress pays conforme au périmètre `server_countries`, perf = budget latence par stack.

Chaque lot de l'audit et du plan de correction doit impérativement citer l'objectif (O1–O4) qu'il sert.

---

## 1. Définition exacte de « routable » dans ce code

Une station est éligible au service seulement si TOUS les filtres passent :

1. **Statut tunnel** : `StationStatus.is_up()` = `connected` **ou** `degraded` — `vpn/manager.py:506-520`.
2. **Pas de bad-mark** : `bad_until` expiré — `free/pool.py` `_station_usable` (l. 446).
3. **Pas de cooldown latence** : cooldown soft/hard expiré (sauf passe de secours `ignore_latency_cool`) — `free/pool.py:460-580`, `free/rotation.py:549-818`.
4. **Pas au seuil de quota** (passe préférée) : `requests_this_ip < rotation_threshold` (~490 avec stagger).
5. **Sélection** : `_best_station` choisit parmi les `usable` — round-robin strict/hedge/load-balance — `free/pool.py:562-621`.

Diagnostic déjà instrumenté : `free/pool.py:500-557` (`_non_routable_reason`) sait répondre « pourquoi cette station n'est pas routable » et le snapshot expose `non_routable_reason` par station. **L'audit s'appuie dessus au lieu de deviner.**

---

## 2. Chaîne de routabilité — les 8 maillons où une station peut tomber

```
Hôte réseau ── Docker/gluetun ── Tunnel (WG/OV) ── Egress réelle
     │              │                 │                │
  (DNS,MTU)   (image,healthcheck, (auth NordVPN,  (probe SOCKS5,
               firewall, volumes) serveur, quota)  ipify OK)
                    │
             Manager/superviseur ── Pool free ── Consommateurs
              (watchdog, rotation,   (bad-mark,     (on_request,
               canary, boot)          cooldowns)     custom_routes, geo)
```

Un seul maillon en échec = station non routable. L'audit vérifie **chaque maillon pour chaque station** (6 × 8 points de contrôle).

---

## 3. Hypothèses de causes racines (classées par probabilité × preuves existantes)

> **2026-09-08 — exigence appliquée : hypothèses relues dans le code, verdicts figés ci-dessous (§3.1).** Le tableau initial reste comme historique des soupçons ; les certitudes sont au §3.1.

### 3.1 Verdicts après lecture du code — hypothèses ⇒ certitudes

| # | Verdict | Preuve code (fichier:ligne) |
|---|---|---|
| **H1 ✔ CONFIRMÉ** | AUTH_FAILED massif, cause opérationnelle dominante. Log confirmé (310/h). Mécanisme : le *fast-pin* saute les hôtes blacklistés mais réessaie quand même dans la fenêtre ; les quelques hôtes « top-load » (fr954, fr1001, de691 = 57× chacun/heure) absorbent toutes les tentatives. | logs fenêtre ; `vpn/manager.py` `_record_auth_failure` (6283+), `blacklist fast-pin` |
| **H2 ✔ CONFIRMÉ** | Échec TLS = liste de serveurs périmée. Le refresh applicatif (`vpn/manager.py:7418`) ne s'exécute QUE si `_server_issue or _auth_failed` — aucune maintenance périodique de la liste paid à part l'`UPDATER_PERIOD: 24h` de gluetun. Stations st4/st6 restent coincées sur des serveurs morts. | `docker-compose.yml:40` ; `vpn/manager.py:7412-7418` |
| **H3 ✔ CONFIRMÉ + amplification** | Healthcheck compose = `/v1/vpn/status` local ≠ egress (compose:22-27). Le restart watchdog « egress dead » poste à 22×/30 min → vient F1 : **`watchdog_max_restarts_per_hour` n'est JAMAIS appliqué** car `self._watchdog_restarts_1h` n'est **jamais initialisé** dans le code de production (grep `_watchdog_restarts_1h\s*=` ne match que les **tests**). `getattr(..., None)` → store None → `[ ]` permanent → `_watchdog_budget_exhausted()` toujours False → restarts illimités → boucle « restart → gluetun non sain → restart ». **Violation directe de O1 : une clé de config (1446 caractères de log/jour) est morte.** | `vpn/manager.py:6250-6281, 7310, 7421` ; `opencode.py` — aucune init |
| **H4 ✔ CONFIRMÉ partiellement** | L'escalade soft→hard et sa purge existent bien (`free/rotation.py:605-618`) — code OK, mais tests live requis : le GC `_soft_history` ne purge qu'à l'expiration, donc une IP re-servie pendant sa fenêtre re-received hard directement. Affiner avec observabilité `total_hard` (Prometheus §12.2.7 non câblé). | `free/rotation.py:571-618` |
| **H5 ✔ CONFIRMÉ** | Bad-mark **uniforme** : `station_bad_ttl_s` (défaut 60 s) appliqué tel quel à toutes les causes — code `free/pool.py:215-225` (charge la clé) et 7 sites d'écriture `bad_until = now + self._bad_ttl` (1360, 1381, 1473, 1492, 1538, 1569, 1839). Un seul paramètre pour 429, timeout, AUTH_FAILED, egress. | `free/pool.py:215-225, 1360…1839` |
| **H6 ✔ CONFIRMÉ** | `is_up()` = connected|degraded uniquement → **pendant toute rotation (status `connecting`) la station sort du pool**. Aucune disposition « servir l'ancienne IP pendant rotation » n'existe dans le sélecteur. | `vpn/manager.py:510-520` ; `free/pool.py:446-460` |
| **H7 ✔ CONFIRMÉ en direct** | Canary `Container opencode-wg-test Creating` → bring-up échoué ×3 dans l'heure → verdict SANS EGRESS → flip annulé ET `remove_wait=600` bloque 10 min : le système ne peut jamais prouver WG. Cause : `docker compose up` sur conteneur existant sans `rm -f` préalable (PLAN_STABILISATION §1.2). | logs 08:52/08:53/09:03 ; `vpn/manager.py` canary |
| **H8 ✔ CONFIRMÉ** | (a) **Clé morte avérée** : `geo_probe_url` (config.yaml:107) — aucune lecture dans tout le code hors docs/tests → violation O1. (b) `dual_station` : encore lu (`config/loader.py:83`, shadowed par `station_count`) + `vpn_proxy_port_2`/`socks5_proxy_port_2`/`state_file_2`/`compose_service_2` hérités → 5 clés legacy à retirer. (c) 5 pays pour 6 stations × offsets 14+17×(n-1) → collision possible par modulo. (d) `control_api_key` en clair config.yaml:187. | config.yaml:107,200,262-275,187 |
| **H9 ⚠ NON CONFIRMÉ comme cause primaire** | DNS plain/MTU déjà posés (compose:47-53) et stables ; probes échouent en rafale **pendant les fenêtres post-restart** → symptôme de H3, pas cause autonome. Garder comme contrôle, pas comme correctif prio. | compose:43-53 ; logs rafales post-restart |
| **H10 ✔ CONFIRMÉ ET AGGRAVÉ (F5 des logs)** | 3×429 en 12 s sur 3 IPs de sortie différentes → le rate-limit free suit le **compte/modèle**, pas l'IP : la diversité des 6 tunnels ne sert à rien pour ce modèle. Toutes les `custom_routes` (config.yaml:325-377) convergent vers `muse-spark-1.3-contributor`. | `config.yaml:325-377` ; logs `[free-400-stream]` 09:01 |

**Ce qui reste à mesurer, pas à deviner** : (i) le plafond `watchdog_max_restarts_per_hour` doit d'abord être câblé — avant, aucune mesure de « restart loop corrigée » n'est fiable ; (ii) `total_soft/total_hard` (latence) non instrumentés → ajouter compteur avant de juger H4 en prod.

| # | Hypothèse | Preuves déjà en main | Vérification (phase) |
|---|---|---|---|
| H1 | **AUTH_FAILED NordVPN massif** — un seul compte, 6 stations tentent en parallèle → rate-limit NordVPN (pas un mauvais mot de passe). | Snapshot st1 ; `auth_failed_window_len: 36` ; PLAN_STABILISATION §1.1 (476 occurrences) ; docs/country backoff | B, G |
| H2 | **Liste de serveurs périmée / serveur mort** → négocie TLS impossible côté WG/OV. | st4 & st6 `échec négociation TLS (liste serveurs obsolète ?)` ; `UPDATER_PERIOD: 24h` compose vs doc 480h | C |
| H3 | **Après restart watchdog : « gluetun non sain »** → boucle restart↔erreur. Le healthcheck compose interroge `/v1/vpn/status` **local** (connecté ≠ egress réelle). | st1/st3 `gluetun non sain après redémarrage` ; compose:22-27, 513-518 ; PLAN §3.2 | C, D |
| H4 | **Escalade latence soft→hard** sur IP re-desservie après rotation → exclusions en rafale, « pool tombe à 1/N » (documenté `free/rotation.py:605-618`, purge censée exister — reste à confirmer effective en prod). | Commentaire code ; règle de purge présente | E |
| H5 | **Bad-mark uniforme** (60 s quelle que soit la cause) + possible marquage en cascade → fenêtre où toutes les stations sauf une sont exclues. | PLAN_STABILISATION §2.2 ; `bad_ttl: 10`, `station_bad_ttl_s: 60` config | E |
| H6 | **Fenêtre de rotation** : `status=connecting` → station exclue ; si tout le reste est exclu pour une autre raison, la seule routable s'effondre. | PLAN §2.3 ; `_station_usable` exclude_approaching | E |
| H7 | **Canary WG cassé** (`docker compose up vpn-wg-test` : conteneur déjà existant) → verdict « SANS EGRESS » → flips annulés en boucle, churn. | PLAN §1.2 ; `wg_canary_fail_ttl` snapshot | D |
| H8 | **Configuration dérivée/legacy** : `dual_station: true` (l.200) coexiste avec `station_count: 6` ; `server_countries` = 5 pays pour 6 stations (offsets `14 + 17×(n-1)`) ; `control_api_key` en clair dans `config.yaml:187` vs rôle dans `credentials.env` (risque de mismatch → pins pays 401). | config.yaml ; compose `${SERVER_COUNTRIES:?}` ; audit F-H5/F-H6 | F |
| H9 | **DNS/MTU dans le tunnel** flanchent par intermittence (DoT désactivé → plain 1.1.1.1 ok, MTU 1280 posé, à valider effectif) → probes en erreur → egress flipped. | compose:43-53 ; docs/gluetun-station.md §DNS/MTU | C |
| H10 | **Concentration apparente, pas panne** : `custom_routes` redirige ~10 modèles vers `muse-spark-1.3-contributor` (geo US/CA/UK...) → tout le trafic réel peut se concentrer sur la station qui sert ce modèle même quand 6 sont routables. « 1 sur 6 » peut être un artefact de routage modèle. | config.yaml:325-377 custom_routes ; geo policies `meta-muse-spark` | G |

> Observation annexe du snapshot : l'historique d'IPs de la station 4 montre des sorties **brésiliennes** (186.247.164.x, 187.15.111.x) alors que `server_countries` = de/nl/fr/se/ch — signal d'un dépassement de périmètre pays (fallback cascade) à inscrire dans l'audit (H8/H2).

---

## 4. Plan d'audit exécutable — 6 phases

### Phase A — Photographie complète à l'instant T (10 min, sans action)

```bash
# 1. État docker
docker ps --format "{{.Names}}\t{{.Status}}" | grep -i vpn
docker inspect --format '{{.Name}} healthy={{.State.Health.Status}} restarts={{.RestartCount}}' opencode-vpn opencode-vpn-2 opencode-vpn-3 opencode-vpn-4 opencode-vpn-5 opencode-vpn-6

# 2. Statut tunnel vu par chaque control server (attendu: {"status":"running"})
for i in 1 2 3 4 5 6; do docker exec opencode-vpn$( [ $i -eq 1 ] || echo "-$i" ) wget -q -O - --header="X-API-Key: $VPN_CONTROL_API_KEY" http://127.0.0.1:8000/v1/vpn/status; echo; done

# 3. Egress RÉELLE par station (ce que le healthcheck ne fait PAS aujourd'hui)
for p in 1080 1081 1082 1083 1084 1085; do echo "sock $p :"; curl -s --max-time 8 --socks5-hostname 127.0.0.1:$p https://api.ipify.org; echo; done
# sortie depuis le conteneur proxy (le vrai chemin applicatif) :
docker exec opencode-proxy sh -c 'for u in "$VPN_SOCKS5_URL $VPN_SOCKS5_URL_2 ... 6"; do wget -q -O- --timeout=8 -e use_proxy=yes -e socks5_proxy=$u https://api.ipify.org; done'

# 4. Vue applicative
curl -s http://127.0.0.1:4000/api/pool-status   # N/M routable + non_routable_reason par station
curl -s http://127.0.0.1:4000/api/vpn-status    # statuts managers, watchdog, canary

# 5. Etats persistés
for f in logs/vpn_state*.json logs/shared_rotation.json logs/paused_keys.yaml; do echo "== $f"; cat "$f"; done
```

**Critère** : produire un tableau 6 stations × {healthy?, tunnel?, egress réelle?, statut manager, bad_until, cooldown, dernière IP, pays}. Toute incohérence entre colonnes (ex : healthy + egress KO) = un finding.

### Phase B — Couche NordVPN / authentification (H1)

```bash
docker logs opencode-vpn   --since 6h 2>&1 | grep -iE "auth|AUTH_FAILED|bad credentials" | tail -50
# ×6 conteneurs ; compter AUTH_FAILED par heure et par station
grep -c AUTH_FAILED logs/debug.log ; grep AUTH_FAILED logs/debug.log | tail -20
```

À vérifier :
- AUTH_FAILED corrélés à des **rafales simultanées** (6 connexions < 2 s au boot/rotation) → confirme H1.
- `auth_failed_oldest_age_s` / `auth_failed_window_len` par station dans le snapshot (fenêtre 30-60 min).
- Double source d'identifiants : `credentials.env` vs `vpn_configs/credentials.txt` (`credentials_file` encore défini config) — divergence possible.
- Stack de chaque station (`.env` `VPN_TYPE_STATIONn`) : WG silencieux vs OV bavard (docs/gluetun-station.md §2).
- Compte NordVPN : support multi-sessions (6 tunnels simultanés) actif ? Vérifier sur le portail NordVPN.

**Critère succès** : AUTH_FAILED < 5/h au total ; sinon H1 confirmée.

### Phase C — Couche gluetun / tunnel (H2, H3, H9)

```bash
# version & serveurs
docker exec opencode-vpn-4 sh -c 'grep -c . /tmp/gluetun/servers.json 2>/dev/null; gluetun version'
# updater forcé sur une station saine puis re-pin pays :
docker exec opencode-vpn-2 wget -qO- --post-data='{"status":"running"}' --header="X-API-Key: $VPN_CONTROL_API_KEY" --header="Content-Type: application/json" http://127.0.0.1:8000/v1/updater/status
# MTU effectif vs attendu 1280 :
for c in opencode-vpn opencode-vpn-2 opencode-vpn-3 opencode-vpn-4 opencode-vpn-5 opencode-vpn-6; do echo -n "$c tun0 MTU: "; docker exec $c cat /sys/class/net/tun0/mtu 2>/dev/null || echo "(pas de tun0)"; done
# DNS dans le tunnel :
docker exec opencode-vpn-4 nslookup api.ipify.org 127.0.0.1 2>&1 | head -5
# firewall hôte / sortie :
curl -s https://api.ipify.org   # l'hôte sort-il ?
```

À vérifier : âge de la liste de serveurs (volume `gluetunN`, `UPDATER_PERIOD`), erreurs `no such host`, `i/o timeout`, MTU réel ≠ 1280 (PMTUD), 401 en rafale → rôle control server (ADR-005).

### Phase D — Manager / superviseur / watchdog (H3, H7)

```bash
grep -E "watchdog|restart|canary|flip|reconcile" logs/debug.log | tail -100 ; cat logs/cs_debug.log 2>/dev/null | tail -40
docker logs opencode-proxy --since 6h 2>&1 | grep -iE "rotation|watchdog|supervisor|canary|worker" | tail -80
```

Points de contrôle : escalades watchdog (restarts/h/station vs `watchdog_max_restarts_per_hour: 3`), verdicts canary (TTL `wg_canary_fail_ttl_s: 90` qui martèle), boot reconcile qui lit `VPN_TYPE_STATIONn` du `.env` (source de vérité, docs/gluetun-station.md:16), workers rotation ≤ 2 in-flight, `StationSupervisor.restart` sans lock double (finding plan-perf Lot 2).

### Phase E — Pool free / sélecteur (H4, H5, H6)

- Rejouer l'état : pour chaque station, calculer `_station_usable` à la main depuis le snapshot (`vpn_status`, `bad_until`, cooldown latence, approaching quota) → vérifier que `pool-status` dit la même chose. Tout écart = bug de sélection.
- Audit ciblé des compteurs : `free/rotation.py` `_soft_history`, `_cooldowns`, `total_soft/total_hard`, `_global_log`, `_global_paused_until` — si `total_hard` croît sans slow réel confirmé → H4.
- Vérifier que pendant une rotation la station **garde son ancienne IP servable** (PLAN §2.3) : chercher ce comportement en code et en tests ; s'il n'existe pas, c'est un correctif à écrire.

### Phase F — Cohérence configuration (H8, H10)

| Contrôle | Attendu | Commando |
|---|---|---|
| `station_count: 6` vs legacy `dual_station: true` | supprimer dual_station et les clés `*_2` orphelines | config.yaml:200-275 |
| 5 pays pour 6 stations + curseur `offset 14, stride 17` | jamais 2 stations sur le même pays | simuler le curseur `shared_rotation.json` |
| Clé control server identique proxy ↔ containers | **strictement égales** | comparer `config.yaml:187` et rôle dans `credentials.env` ; si ≠ → tous les `PUT /v1/vpn/settings` renvoient 401 → pins pays échouent → stations errantes (explique aussi le Brésil st4) |
| `OPENVPN_PROTOCOL` unique source | `.env` seul (pas double défaut udp) | PLAN §4.1 |
| Image gluetun épinglée | digest, pas `:latest` | compose:6,127 |
| Geo/custom_routes : quelle station sert `muse-spark-*` ? | vérifier H10 : concentration apparente | greps logs `upstream` par station |

### Phase G — Couverture configuration (O1 — la phase reine)

But : produire un tableau **clé → consommée où → effet observable → test**. Aucune clé ne peut rester à blanc.

```bash
# 1. Inventaire exhaustif des clés (à outiller : scripts/config_coverage.py)
python - <<'EOF'
import yaml, json
def flat(d, p=''):
    for k, v in (d or {}).items():
        k2 = f'{p}.{k}' if p else k
        yield from flat(v, k2) if isinstance(v, dict) else [(k2, v)]
print('\n'.join(k for k, v in flat(yaml.safe_load(open('config.yaml')))))
EOF
# 2. Pour chaque clé : grep dans le code hors tests/docs/vendeur
rg -n "\bwatchdog_max_restarts_per_hour\b" --type py   # → constat F1 : présent ? appliqué ?
# 3. Pour chaque clé : effet observable — changer la valeur, relancer, vérifier que le comportement change.
```

| Statut | Définition | Action |
|---|---|---|
| ✅ consommée | lu + effet vérifiable | garder + 1 test de comportement |
| ⚠️ partielle | lue mais un chemin ne l'applique pas (ex. F1 : plafond watchdog contourné par la voie `egress dead`) | corriger le chemin fautif, ajouter test de régression |
| ✗ morte | aucune lecture dans le code | implémenter l'effet **ou** supprimer la clé (ADR) — jamais de zombie |
| ↻ legacy | seconde source obsolète (`dual_station`, `*_2`, `credentials_file` vs `credentials.env`) | plan de retrait documenté |

Candidates déjà repérées à classer : `dual_station`, `state_file_2`, `vpn_proxy_port_2`, `socks5_proxy_port_2`, `compose_service_2`, `credentials_file`, `wg_churn_fallback`, `wait_healthy_require_socks`, `socks_catchup_s`, `update_*` (fenêtre de MAJ non vérifiée), `egress_failure_tick_interval`, `watchdog_backoff_*`, `pin_widen_*`, `upstream_event_*`, `identity_*`, plus chaque clé de `latency_rotation` vs `supervisor` (les deux font du cooldown latence — chevauchement à clarifier). **Livrable : tableau complet joint au rapport d'audit ; toute nouvelle clé ajoutée au `config.yaml` doit dorénavant livrer son test d'effet dans la même PR.**

---

## 5. Matrice de décision (cause → preuve → correctif)

| Cause confirmée | Correctif ciblé | Fichier |
|---|---|---|
| H1 AUTH_FAILED | boot/rotation **staggered 30 s**, max 2 connexions simultanées, cooldown global 120 s après 3 AUTH_FAILED ; supprimer `credentials.txt` | `vpn/manager.py` `_connect_next_impl` ; PLAN §1.1 |
| H2 serveurs périmés/TLS | updater forcé, blacklist hostname 24 h, `UPDATER_PERIOD` cohérent, élargir pays avant abandon | compose + `vpn/manager.py` |
| H3 restart loop | healthcheck compose = **egress réelle** (wget via SOCKS5 interne, PLAN §4.3) ; escalade progressive probe→re-pin→restart ; jamais de restart avec streams actifs | compose:healthcheck, `_watchdog_escalate` |
| H4 escalade latence | confirmer purge `_soft_history` effective + test de régression « IP re-servie après rotation » | `free/rotation.py:605-618`, tests |
| H5 bad-mark uniforme | durée par cause (429=60 s, timeout=30 s, AUTH_FAILED=0 marquage) ; **jamais plus de N-2 stations marquées** ; EWMA fiabilité | `free/pool.py` `notify_connection_failure` |
| H6 fenêtre rotation | station sert l'ancienne IP jusqu'à confirmation (rotation non-bloquante), `on_request` attend ≤ 5 s | `free/pool.py` |
| H7 canary cassé | `docker rm -f` du canary stale avant up, timeout verdict 10 s | `_compose_up`/canary |
| H8 config dérivée | nettoyage dual_station, pays ≥ stations, clé control en secret unique, image épinglée | config.yaml, compose |

---

## 5b. Analyse des logs — dernière heure (2026-09-08, 08:38→09:10 UTC)

Fenêtre : `logs/debug.log.1` (10,4 Mo ; la journée entière = 45 340 lignes). Dédupliqué : 2 033 brutes → 1 089 uniques (logs doublés, voir F8).

### 5b.1 Chiffres de l'heure vs seuils configurés

| Métrique | Fenêtre 08:38–09:10Z | Journée | Seuil config | Verdict |
|---|---|---|---|---|
| `AUTH_FAILED` | **310** (rafales 64/30 min) | **9 965** | — | ✗ critique (H1) |
| Restarts watchdog (`egress dead detected`) | **42** — s5 ×22, s4 ×10, s6 ×10 | **318** | `watchdog_max_restarts_per_hour: 3` | ✗ **plafond non appliqué, violé ×7** |
| `gluetun non sain après redémarrage` | 6 | — | — | boucle restart→unhealthy (H3) |
| Probes egress « failed on all 4 endpoints » | 3 rafales (08:51, 08:53, 09:07) | — | — | H(egress) confirmé |
| TLS « Serveur injoignable — liste obsolète ? » | 23 (cluster 09:01:16→09:01:50) | — | — | st4/st6 coincées (H2) |
| Canary `bring-up échoué: Container opencode-wg-test Creating` | 3 (08:52, 08:53, 09:03) → flip annulé | — | — | H7 confirmé en direct |
| 429 free `muse-spark-1.3-contributor-free` | 3 en 12 s, 3 IPs de sortie différentes | — | — | F5 ci-dessous |

### 5b.2 Nouveaux findings issus des logs (ajoutés aux H1–H10)

| # | Finding | Preuve (fenêtre) | Statut |
|---|---|---|---|
| **F1** | **Boucle de restarts en rafale** : `opencode-vpn-5` relancé **22 fois en 30 min** malgré `watchdog_max_restarts_per_hour: 3` — le plafond n'est pas appliqué sur cette voie → « gluetun non sain après redémarrage » permanent → station jamais routable | 22× `restarting opencode-vpn-5` entre 08:38 et 09:02 | H3 confirmée et aggravée |
| **F2** | **Décisions watchdog contradictoires au même instant** : à 08:38:55Z le journal imprime « egress dead detected — restarting opencode-vpn-5 » **et** « s5 AUTH en auth_cooling (104s) — pas de restart » — deux chemins du watchdog ne partagent pas l'état `auth_cooling` (race) | lignes à la même seconde | nouveau bug |
| **F3** | **Canary structurellement cassé** : bring-up échoue (« Container opencode-wg-test Creating »), verdict « SANS EGRESS », flip annulé, puis `remove_wait=600 s` bloque 10 min → le WG n'a jamais aucune preuve d'egress possible | 3 occurrences | H7 confirmé |
| **F4** | **Sorties hors périmètre pays** : pins/egress sur `za173.nordvpn.com` (Afrique du Sud), IPs brésiliennes (187.13.x/187.14.x/187.15.x), NZ (130.195.x), UK (109.69.x), RO (86.104.x) alors que `server_countries = de/nl/fr/se/ch` ; re-tentatives ×57 sur des hôtes déjà AUTH_FAILED avec TTL 1440 min (fr954 au flag #263) | `[free-usage] ip=…`, `AUTH_FAILED on …` | H8 confirmé, priorisé |
| **F5** | **Le 429 free suit le compte/modèle, pas l'IP** : trois 429 sur `muse-spark-1.3-contributor-free` avec 3 IPs de sortie différentes en 12 s → la diversité d'IP ne contourne pas ce rate-limit. Même 6/6 tunnels OK, le modèle concentré écrouira le service. La quasi-totalité des `custom_routes` (config.yaml:325-377) envoie vers `muse-spark-1.3-contributor` | `[free-400-stream]` 09:01:03→15 | H10 confirmé, **priorité P0 produit** |
| **F6** | ~~Curseur corrompu~~ — **RETIRÉ** : à la vérification, aucune occurrence dans `logs/debug.log.1` et aucun site `Cursor corrupted` dans le code hors logs. Allégation non reproduite → retirée de la liste des problèmes (exigence « zéro hypothèse »). | — | retiré |
| **F7** | **Le système s'autodétruit à trafic quasi nul** : 6 réponses 200 vs 3×429 dans l'heure — la non-routabilité vient des boucles internes, pas de la charge | décomptes `[free-usage]` | contexte |
| **F8** | **Lignes de log doublées ×2** (2 033 brutes / 1 089 uniques) → double handler de logging ; volumes ×2 sur un disque déjà saturé (audit F-M5) | ratio brut/unique | hygiène / indirect |
| **F9** | **`ip=192.168.31.x` dans `[free-usage]` ×6** : soit c'est l'IP **client** loguée (variable mal nommée), soit des requêtes sont sorties **sans VPN** (fallback direct) — à trancher au site d'appel | `[free-usage] … ip=192.168.31.*` | question ouverte P1 |
| **F10** | **Silence total depuis 09:09:45Z** : `debug.log` vide après rotation — proxy arrêté/planté ou logging coupé | mtime fichiers | à vérifier Phase A |
| **F11** | **Horodatages mixtes** : lignes `[vpn_manager]` en `…Z`, lignes `free-usage`/`stream-oai` sans `Z` → corrélation temporelle entre sous-systèmes faussée | formats mixtes | hygiène |

### 5b.3 Ce que l'heure de log change au diagnostic

1. **Nouvelle priorité n°1 = watchdog, pas NordVPN** : 42 restarts/heure (plafond 3 écrit mais non appliqué) entretiennent « gluetun non sain » et fabriquent la non-routabilité. Le correctif du plafond (F1) doit être cablé avant tout autre.
2. **F5 change l'objectif produit** : réparer les 6 tunnels ne suffit pas — tant que tout le trafic visa un même modèle upstream, le 429 tiendra quelles que soient les IPs. Prévoir la répartition modèle (custom_routes) EN PLUS du durcissement pool.
3. **Ignorer le bruit** : 0 `Traceback`/exception Python dans l'heure — les échecs sont externes (auth NordVPN, rate-limit) ou décisionnels (watchdog/canary), pas des crashs applicatifs.
4. Chaîne domino complète observée dans l'heure : AUTH_FAILED → station coincée → probe egress KO → watchdog restart (×22) → « gluetun non sain » (watchdog×?) → station indisponible → **1-2 stations servables** (st2/st5).

## 6. Corrections pour « toujours routables » — invariants à faire respecter

### Topologie cible — N stations, 3 stacks (O2 + O3)

> **Règle de mode (O1)** : le tableau ci-dessous vaut pour `vpn_stack: auto`. En mode explicite (`wireguard` / `openvpn`), la flotte est **uniforme** sur la stack choisie (et `ovpn_protocol` s'applique pour openvpn) — aucun flip vers une autre stack n'est autorisé.

| Station | Stack (mode auto) | Rôle flotte | Port SOCKS5 / HTTP |
|---|---|---|---|
| 1, 4, 7… | `wireguard` | rapide, rotation pays par pin control | 1080/8888, 1083/8891, … |
| 2, 5, 8… | `openvpn` **tcp 443** | robustesse NAT/firewall, logs bavards | 1081/8889, … |
| 3, 6, 9… | `openvpn` **udp 1194** | intermédiaire | 1082/8890, … |

**Sites de référence IP (egress)** : la politique est *une référence à la fois, fallback garanti* — le mécanisme existe déjà et fait exactement ça (`vpn/manager.py:2795-2832` : endpoint sticky, puis sweep borné des autres endpoints de `ip_check_urls` si le premier échoue ; reset au premier sur échec total). À faire valider par test : **le fallback doit rester muet tant qu'au moins un endpoint répond** — les rafales « all 4 endpoints failed » observées (08:51/08:53/09:07) ne doivent apparaître qu'en vrai blackout. Le parallèle **`geo_probe_url` est aujourd'hui une clé morte** (aucune lecture, 0 occurrence hors config) : soit l'implémenter (géolocalisation d'egress pilotée par cette URL + fallback), soit la supprimer.

Garanties requises par stack : healthcheck = egress réelle (wget via SOCKS5 du conteneur — §5 capture), canary **par stack** (un canary WG global ne prove pas OV-TCP), budget de restarts par station réellement appliqué, boot séquencé : max 2 connexions simultanées toutes stacks confondues, anti-correlation pays (deux stations d'une même stack ne doivent pas viser le même serveur).

Règle de non-régression flotillaire : **jamais la flotte entière sur une seule stack** (le flip WG global actuel = anti-pattern — remplacer par rééquilibrage progressif station par station, avec preuve d'egress continuelle).

### Invariants opérationnels à faire respecter

1. **Invariant pool** : le sélecteur refuse tout marquage qui amènerait le nombre de candidates < max(2, N-2) ; une exclusion qui violerait l'invariant convertit la station en « servable dégradé » — c'est le garde-fou absolu contre « 1/6 ».
2. **Invariant config (O1)** : chaque changement de `config.yaml` livré avec la preuve d'effet (test + compteur/log). Toute clé sans effet = bug bloquant de merge.
3. **Rotation non-destructive** : ancienne IP servie jusqu'à la nouvelle validée (cut-over, jamais gap).
4. **Boot séquencé** : connexions NordVPN étalées 30 s, 2 in-flight max, quotas par stack.
5. **Watchdog borné et partagé** : `max_restarts/h` appliqué sur TOUTES les voies de restart (egress, health, auth) ; état `auth_cooling` partagé entre les voies (corrige F2).
6. **SLO + monitoring** : gauges `stations_utilisables/N`, `stations_par_stack_saines`, pays_effectif ∈ `server_countries` ; alerte < N−2 pendant > 2 min ; runbook 3 commandes par pattern (AUTH_FAILED, TLS obsolète, restart loop) dans `docs/runbook-docker.md`.

### Critères de succès mesurables (1 h de trafic réel)
- `N routable / 6` ≥ 4 en continu, jamais < 2.
- AUTH_FAILED < 5/h total ; 0 restart watchdog > 3/h/station ; 0 flip annulé en boucle.
- MTTR station < 2 min ; disponibilité par station ≥ 99 % (hors rotation taguée).

---

## 7. Livrables de l'audit

1. Tableau 6 stations × 8 maillons rempli (phase A-F).
2. Preuves packagées : `docker inspect`, sorties egress, extraits `debug.log`, `vpn_state*.json`, pages `pool-status`.
3. Matrice cause → correctif validée, triée par impact (plan §5).
4. Ticket de correctifs à appliquer dans l'ordre, remis à jour après analyse des logs :
   **P0** = F1 (plafond watchdog non appliqué + contradiction auth_cooling F2), F5 (répartition modèle / 429 suit le compte), H1 (stagger boot 30 s, max 2 connexions simultanées), H8 (clé control unique + périmètre pays) ;
   **P1** = F3/H7 (canary), H3 (healthcheck = egress réelle), H5 (bad-mark par cause), H6 (rotation sans coupure), H4 (purge escalade latence), F9 (sortie directe ?) ;
   **P2** = F11 (timestamps), F8 (logs doublés), H2 (liste serveurs + updater), image épinglée, runbook, métriques SLO.

---

## 8. Plan de correction détaillé — fichier, ligne, modification

> Mode d'emploi : chaque item PC-x est autonome, cite l'objectif servi (O1–O4), la cible exacte (fichier:ligne vérifiée le 2026-09-08), la modification à écrire, et le test prouvant que la config fait effet. **Implémenté le 2026-09-08 (PC-1→PC-16 + observabilité SLO) — voir ADR-007, CHANGELOG [Unreleased], `tests/test_audit_routabilite_pc.py` (33 tests).**

### Lot P0 — stopper la boucle auto-destructrice (F1, F2, H1)

**PC-1 — Initialiser le budget de restart (cause racine F1, sert O1/O4).**
- Fichier : `vpn/manager.py`, §init états watchdog (région l.1440–1520, au niveau de `self._watchdog_last_action` / charge de `watchdog_max_restarts_per_hour` l.1490-1502).
- Modification : ajouter `self._watchdog_restarts_1h: list[float] = []`. Le getter `vpn/manager.py:6250-6281` (`_watchdog_restarts_recent`) lit alors une liste réelle et `_watchdog_budget_exhausted()` (l.6267) devient fonctionnel.
- Test : `tests/test_watchdog_budget.py` — simuler 3 restarts < 1 h → le 4ᵉ est refusé, le log émet exactement `watchdog_restart_budget_exhausted … escalade auth_cooling 300s`.

**PC-2 — Compter TOUS les restarts dans le budget (F1 complément).**
- Fichier : `vpn/manager.py` (a) voie « light restart » l.7419-7424 (`_note_watchdog_restart()` déjà appelé — garder), (b) voies compose `_compose_up(True)` l.4953-4961 : ajouter `_note_watchdog_restart()` avant recréation forcée, (c) voie heal parallèle l.7352+ : même appel.
- Modification : un restart, quelle que soit la voie (`watchdog`, `compose`, `retry`, `physique reste cble`), incrémente le même compteur.
- Test : restart via compose → le compteur `vpn_state` remonte de 1 (champ exposé dans `snapshot_info`).

**PC-3 — Une seule décision de restart par tick (F2).**
- Fichier : `vpn/manager.py:7190-7355` (corps `_watchdog_tick`).
- Modification : factoriser en `_decide_action() -> tuple[str, str]` (« restart|cooling|heal|none », raison) — une seule lecture de `_auth_cool_until`, `_auth_grace_until`, `_auth_cooldown_global`, `_egress_failures`, `_watchdog_online`, budget. Le tick exécute l'action et log **une seule ligne** `[vpn-watchdog] s{n} action=<…> reason=<…>`. Disparition des lignes contradictoires « restarting X » + « pas de restart » à la même seconde.
- Test : fixture (egress dead + auth_cooling actif) → 1 ligne de décision, 0 appel `_docker`.

**PC-4 — Boot/rotation étalé (H1, réduction AUTH_FAILED).**
- Fichier : `opencode.py:1866-1883` (création des managers) et `vpn/manager.py` cycle de connexion initiale.
- Modification : respecter `connect_retry_interval: 20` (config.yaml:236) au boot : au plus 2 appels simultanés d'AUTH NordVPN, 1 station démarre toutes les 30 s (nouvelle clé `boot_stagger_s: 30`, documentée dès son ajout — O1). Le même générateur de stagger sert `_request_reset_tick` (fausse précision TTL rn).
- Test : N=6 → sur 10 boots simulés, AUTH_FAILED/30 min ≤ 2 ; le 3ᵉ+ appel observe un délai ≥ 25 s.

**PC-5 — Le 429 free suit le modèle, pas l'IP (F5, sert O2).**
- Fichier : `opencode.py` — sites du mapping free `FREE_MODEL_MAP.get` l.5432, 8398, 9381, 10899, 11794 ; config `custom_routes` l.325-377.
- Modification : introduire `free_model_candidates` (pool de modèles free équivalents) + `free_model_spread: true` : la sélection de modèle cible devient round-robin par requête au lieu de « tout sur `muse-spark-1.3-contributor` » ; compteur `free_429_by_model{model}` ; test de routage : 2 requêtes consécutives mêmes paramètres ne touchent pas le même modèle si la liste > 1.
- Sans ce lot, P0 tunnels ne change rien au 429 (certitude §3.1-H10).

### Lot P1 — routabilité structurelle (H2, H3, H5, H6, H7/F3)

**PC-6 — Healthcheck compose = egress réelle (H3).**
- Fichier : `docker-compose.yml:22-27` (bloc `healthcheck` de l'anchor `x-gluetun-multi-base`).
- Modification : remplacer le test `wget … /v1/vpn/status` (local ≠ service) par un probe SOCKS5 interne : `test: ["CMD-SHELL", "wget -q -O - -T 10 --proxy socks5://127.0.0.1:1080 http://api.ipify.org || exit 1"]` ; garder `interval: 30s`, `start_period: 60s`. Healthy ⇒ l'IP publique répond à travers le tunnel. Corollaire : le faux « gluetun non sain » disparaît et `healthy states` colle à la réalité.
- Test : kill de la route interne (`iptables` drop dans le conteneur) → healthcheck unhealthy en ≤ 90 s ; rétablissement → healthy en ≤ 90 s.

**PC-7 — Canary réparable (F3/H7).**
- Fichier : `vpn/manager.py:5419` (`_WG_CANARY_SERVICE = "vpn-wg-test"`) et son bring-up (l.5465±).
- Modification : avant `compose up` du canary → `docker rm -f opencode-wg-test` (échec toléré si absent) ; si bring-up échoue après ce rm → verdict **indéterminé**, pas « WireGuard SANS EGRESS » ; le flip n'est annulé que sur preuve d'egress négative mesurée ; `remove_wait=600 s` ne s'arme que sur verdict négatif réel.
- Test : `docker create opencode-wg-test` fantôme → canary recouvre, verdict mesuré, aucun freeze 10 min quand bring-up échoue.

**PC-8 — Refresh périodique de la liste de serveurs (H2).**
- Fichier : `vpn/manager.py` — la fonction `_refresh_server_list()` existe (appel l.7418 uniquement si `_server_issue or _auth_failed`) ; ajouter un minuteur applicatif : `self._server_list_refresh_interval_s` (nouvelle clé, défaut 6 h) évalué dans `_watchdog_tick` ou une tâche périodique ; appeler systématiquement `_refresh_server_list()` + re-pin du pays courant. Décision unique : retirer `UPDATER_PERIOD` du compose (`docker-compose.yml:40`) OU le maintenir à 2 h — dans les deux cas **une seule** source de rafraîchissement active et loguée.
- Test : st sur un hôte simulé mort → prochain refresh ≤ 6 h → la station re-passe `connected` sans autre action opérateur.

**PC-9 — Rotation sans coupure (H6, invariant §6#3).**
- Fichier : `free/pool.py:446-460` (`_station_usable`) + `snapshot_info()`
- Modification : ajouter une fenêtre « rotation en douceur » : si `station.status == connecting` ET `rotation_started_at` < `connect_timeout_sec` ET `last_good_ip` existe → la station reste éligible et le client attend au pire `fast_connect_timeout_s` au lieu d'écoper « 0 station » ; exposer `rotation_in_progress: bool` et `last_good_ip` dans le snapshot utilisé par `/api/pool-status`.
- Test : rotation forcée (`/api/vpn/next`) pendant le fire d'une requête longue → 200 servi, aucun 500 « no stations available ».

**PC-10 — Bad-mark par cause + garde anti-N<2 (H5, invariant §6#1).**
- Fichier : `free/pool.py:215-225` (lecture des TTL) et sites d'écriture `bad_until` l.1360, 1381, 1473, 1492, 1538, 1569, 1839.
- Modification : `station_bad_ttl_s: 60` (config.yaml:225) devient `bad_ttl_by_cause: {"rate_limit": null, "timeout": 30, "auth": 0, "tls": 45}` (`null` = garde legacy 60 s) ; chaque site d'écriture reçoit la cause. Garde pool : `elegibles < max(2, N-2)` → le marquage le moins « légitime » est converti en `servable_degraded` (la station est servie malgré le marqueur), jamais moins de 1 éligible. Log `[pool] invariant_n2 gardé sur s{n} (cause=…)`.
- Tests : (a) 3 stations à 429 → les 3 marquées 60 s, config respectée ; (b) tentative qui passerait sous N-2 → dégradé forcé, pool jamais < 2.

**PC-11 — Périmètre pays coherent (H8/F4, sert O1/O3).**
- Fichier : `config/loader.py:62-83` + `vpn/manager.py` décision de pays pendant rotation/pin.
- Modification : (a) si `len(server_countries) < station_count` → au moins 2 stations partagent un pays, attribution **déterministe** (hash station → pays, log `[countries] s{n}→{pays}`) — aujourd'hui le docs/docker-stations.md pr dit « jamais deux stations le même pays » sans vérifier le préalable 6>5 ; (b) refuser d'épingler un pays hors `server_countries` sauf si `egress_allow_any_country: true` (nouveau) ; log `[POLICY] pays refusé hors périmètre` par décision. Corrige aussi les sorties Brésil/NZ/Afrique du Sud constatées dans les logs (F4).
- Tests : (a) N > len(pays) → mapping déterministe, doublons autorisés explicités ; (b) tentative pays externe → refus loggé, station re-pinée dans le périmètre.

### Lot P2 — propreté, observabilité, horizontale (O1, F8, F9, F10, F11)

**PC-12 — Épingler l'image gluetun.** `docker-compose.yml:6,127` : `qmcgaw/gluetun:latest` → `qmcgaw/gluetun@sha256:<digest>` (digest figé, changelog suivi). Test : `docker compose pull` idempotent.

**PC-13 — Logging et timestamps (F8, F11) + doublon lifespan.**
- `dashboard`/bootstrap logging : les handlers attachés deux fois (`_attach vpn log files` dans `install.py:248-252` redémarre un scheduling) → régler `propagate=False` POUR les loggers ayant un handler dédié fichier (`vpn_manager`, `free_ip_pool`), garder propagate=True sinon ; ratio brut/unique des logs doit devenir 1,0 (mesure automatisable via le mécanisme de l'audit du 2026-09-08).
- `opencode.py` helper de timestamp : uniformiser `[YYYY-MM-DD HH:MM:SS]`Z sur TOUTES les lignes (donc y compris `free-usage`, `stream-oai`, `FREE_MODEL_MAP`) — plus de lignes sans `Z`.
- Dedupe `_watchdog_online` si toujours deux positions dans le même lifecycle.

**PC-14 — Couverture configuration outillée (O1, durable).**
- Nouveau : `scripts/config_coverage.py` — parse `config.yaml`, aplatit les clés, pour chacune cherche l'usage dans `app/ core/ vpn/ free/ config/ shared_state.py opencode.py` (hors tests/docs) → rapport `CONSOMMÉE / PARTIELLE / MORTE` ; sortie table markdown jointe au rapport d'audit. CI/PR : bloque sur toute nouvelle clé MORTE.
- Actions issues du rapport : classer `dual_station`(config.yaml:200)/`vpn_proxy_port_2`/`socks5_proxy_port_2`/`state_file_2`/`compose_service_2` (config.yaml:262-275) en supprimées ou retirees avec ADR ; implémenter `geo_probe_url` (config.yaml:107) comme sonde de pays d'egress avec fallback (aligné sur le mécanisme `ip_check_urls` sticky+sweep de `vpn/manager.py:2795-2832`) ; sortir `control_api_key` de `config.yaml:187` (source unique `credentials.env`).

**PC-15 — Pas de silence (F10).**
- Fichier : boucle `_watchdog_tick` (`vpn/manager.py`) : ligne heartbeat toutes les 5 min même sans événement (ex. `[watchdog] hb stations=6 utiles=5 auth_failed_1h=0`) ; supervision externe lit la présence des lignes.
- Test: pause d'écriture volontaire → absence de heartbeat détectée en 10 min.

**PC-16 — Lever l'ambiguïté des logs d'usage (F9).**
- Fichier : site d'émission `[free-usage] … ip=…` (logs quotidiens). Lire l'implémentation ; si c'est l'IP client, renommer le champ `client_ip` ; si c'est une sortie, loguer `egress_ip` à part ; si des requêtes sortent sans tunnel, rendre `free` **fail-closed** optionnel (`ENFORCE_VPN_ONLY: true`).
- Test: chaque `[free-usage]` logue `client_ip=… egress_ip=…`.

### Ordre d'application et garde-fous

1. **P0 avant tout** : PC-1 → PC-5 (sans eux, les mesures P1/P2 seront bruitées par la boucle de restarts).
2. P1 dans l'ordre : PC-6 → PC-7 → PC-8 → PC-9 → PC-10 → PC-11.
3. P2 en parallèle par les benchs : PC-12 à PC-16.
4. Chaque PC livre : code + test prouvant *la config change le comportement* + log/métrique associée.
5. Critère d'atterrissage global (§6 critères de succès) : **N stations connectées ≥ 99 % du temps, ≥ N−2 éligibles en continu, aucune station sans coupure de rôle (rotation ininterrompue), toutes clés `config.yaml` utilisées (matrice 100 % également vérifiée par `scripts/config_coverage.py`), stacks conformes au mode choisi (auto = 3 stacks explicites sur ≥ 1 station chacune ; choice = uniforme stricte)**.

---

*Sources internes : `pool_snapshot.json`, `PLAN_STABILISATION_VPN.md`, `docs/audit-2026-08-23.md`, `docs/plan-perf-fiabilite-6stations.md`, `docs/gluetun-station.md`, `docs/docker-stations.md`, `config.yaml`, `docker-compose.yml`, `free/pool.py`, `free/rotation.py`, `vpn/manager.py`, `opencode.py`, `dashboard/display.py`, `install.py`.*
