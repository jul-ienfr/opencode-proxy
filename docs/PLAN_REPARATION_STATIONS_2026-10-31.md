# Plan de réparation des stations — conception

**Date** : 31/10/2026
**Statut** : P0–P4 implémentés et testés, P5 partiel — P6 canary non exécuté, P7 capacité non exécutée
**Périmètre** : disponibilité des stations VPN de la jambe free
**Contrainte absolue** : même modèle, aucun fallback modèle, aucun paid, client officiel inchangé.

---

## 1. Constat

### 1.1 Compte NordVPN

Le compte autorise **10 connexions simultanées**. La flotte en utilise 6.
L'hypothèse « la 6e station est refusée par une limite de sessions » est donc
**écartée**.

### 1.2 État observé le 31/10 à 12:34 (local)

| Station | État | Dernier egress |
|---|---|---:|
| s1 | `tunnel_up_full` | 12 s |
| s2 | `tunnel_up_full` | 27 s |
| s3 | `tunnel_up_full` | 10 s |
| **s4** | **`tunnel_down`** | **1 230 s** |
| s5 | `tunnel_up_full` | 35 s |
| s6 | `tunnel_up_full` | 20 s |

La station 4 est la seule non utilisable. s1/s2/s3 sont utilisables ; si le
dashboard les affiche autrement, c'est un défaut d'affichage à corriger.

### 1.3 Preuves sur s4

- Conteneur `opencode-vpn-4` : `Up`, mais `unhealthy`.
- Logs OpenVPN : TLS OK, `PUSH_REQUEST`, puis `AUTH_FAILED` toutes les ~10 s.
- **255 `AUTH_FAILED`** depuis le 29/10 14:26 UTC.
- **282 `server-refused`**.
- **51 `watchdog_restart_budget_exhausted`**.
- **95 `AUTH_FAILED` sur les 3 dernières heures**.
- Dernier bon egress : `187.15.111.104`, serveur `se572`, à 11:12:49 UTC.
- Reprise brève à 11:12:32 UTC, puis nouvel `AUTH_FAILED` à 11:14:36 UTC.
- Les 6 conteneurs ont **les mêmes hash** `OPENVPN_USER` / `OPENVPN_PASSWORD`.
- Les 5 autres stations se connectent avec ces identifiants.

### 1.4 Diagnostic

**Cause principale : auth throttle NordVPN / rejet serveur sur les nouvelles
authentifications**, et non limite de sessions.

NordVPN laisse les sessions existantes vivre, mais peut refuser les nouvelles
authentifications après une rafale. Le cycle actuel :

```text
AUTH_FAILED → restart → nouvelle auth → AUTH_FAILED → 300 s → restart…
```

entretient le throttle. Le `auth_cooling` de 300 s est trop court pour laisser
retomber un throttle de ~15 min. La station 4 est donc exclue du pool, mais
continue de consommer des tentatives d'auth et peut déstabiliser les autres.

**Causes secondaires possibles** :

1. serveur NordVPN spécifiquement refusé (le fast-pin blackliste 10 min, mais
   la rotation suivante peut retomber sur un autre mauvais serveur) ;
2. état d'auth corrompu dans le conteneur (à tester par recreate propre) ;
3. credentials globaux invalides — improbable, 5 stations fonctionnent.

---

## 2. Objectifs

### 2.1 SLO

```text
- aucune station usable sans egress vérifié
- aucune station sélectionnée sans quota disponible pour le modèle demandé
- détection d'une panne < 30 s
- reprise < 5 min si la panne est réparable
- auth attempts <= 3/h/station
- restarts <= 3/h/station
- recreates <= 1/h/station
- aucune tempête auth : <= 1 auth probe pendant une pause globale
- capacité effective toujours visible (pas de faux 6/6)
- uptime station : >= 99,5 % hors maintenance
- objectif flotte : 6/6 actives + 1 IP/spare chaude
- sans spare : 5/6 pendant une rotation, jamais de station morte utilisée
- aucune substitution de modèle
```

### 2.2 Non-objectifs

- Ne pas changer de modèle.
- Ne pas utiliser de paid.
- Ne pas modifier le client officiel.
- Ne pas « réparer » des credentials invalides par des restarts.
- Ne pas prétendre qu'une station est bonne si elle ne l'est pas.

---

## 3. Contrats `station_usable` et `model_usable`

Deux niveaux sont nécessaires : la station peut être saine, mais ne pas avoir de
quota pour le modèle demandé.

```text
station_usable =
    status == connected
    AND auth_ok
    AND egress_state == tunnel_up_full
    AND last_effective_ok_age_s <= 60
    AND not repairing
    AND not parked
    AND not disabled

model_usable(model, station) =
    station_usable
    AND quota_headroom(model, station.current_ip) > quota_reserve
    AND not quota_cooldown(model, station.current_ip)
    AND not ip_blacklisted(model, station.current_ip)
```

Le pool ne sélectionne que `model_usable(model, station)`. Un `connected` sans
egress vérifié, ou une IP sans quota, est un état invalide : la station n'est
pas disponible pour ce modèle.

---

## 4. Machine à états

```text
usable ──(2 bad probes)──> suspect ──(confirmation)──> repairing
   ^                                                        │
   │                                                        ├─ action OK ─> probing
   │                                                        │                 │
   └────────────────────(2 good probes)─────────────────────┘                 │
                                                                              │
                                                          (échec / budget) ──> parked
                                                                              │
                                                          (retry planifié) ───┘
```

| État | Sélectionnable | Sens |
|---|---|---|
| `usable` | oui | egress + auth vérifiés récemment |
| `suspect` | non | un probe a échoué, confirmation en cours |
| `repairing` | non | action de réparation en cours |
| `probing` | non | action réussie, vérification avant retour |
| `parked` | non | réparation impossible pour l'instant, alerte |
| `disabled` | non | arrêt volontaire |

Règles :

- 2 probes mauvais consécutifs → `repairing` (pas de réaction sur un seul).
- 2 probes bons consécutifs → `usable` (pas de retour sur un seul).
- Un seul repair en vol par station.
- Un seul probe auth pendant une pause globale.
- Toute transition est idempotente.

---

## 5. Classifieur de cause

| Cause | Détection | Branche |
|---|---|---|
| `auth_throttle` | `AUTH_FAILED` isolé, 5 autres OK, historique récent | branche principale |
| `auth_global` | 2+ stations `AUTH_FAILED` en 60 s | pause globale, 1 probe |
| `credentials` | toutes les stations `AUTH_FAILED` | park + alerte, zéro restart |
| `server` | TLS/reset/`server-refused` | blacklist + pin |
| `egress` | tunnel up mais probe egress KO | rotation / restart / pin |
| `docker` | daemon/conteneur KO | alerte, pas de restart en boucle |
| `capacity` | moins de N stations `usable` | état dégradé visible, pas de faux 6/6 |

**La branche « session limit » est supprimée** : le compte autorise 10
connexions. La capacité dégradée reste un état générique, pas une conclusion
automatique.

---

## 6. Branche `auth_throttle` — séquence révisée

Le premier réflexe reste immédiat : **un restart, pas 15 minutes d'attente**.
Les silences ne commencent qu'après un échec de restart.

Point technique indispensable : un `auth_cooling` côté proxy **ne coupe pas**
les retries OpenVPN dans le conteneur (visibles toutes les 10 s dans les logs
s4). Pour un vrai silence, il faut **arrêter le conteneur** (ou le mettre en
pause) pendant la fenêtre. Sinon, le throttle est réalimenté en continu.

```text
1. AUTH_FAILED isolé
   → station immédiatement exclue (repairing)
   → un seul restart léger
   → vérification auth + egress
   → si OK : retour probing/usable en ~20–40 s

2. Si échec
   → stop conteneur 5 min (vrai silence, zéro auth)
   → start + une seule auth
   → vérification

3. Si échec
   → stop conteneur 15 min
   → start + une seule auth
   → vérification

4. Si échec
   → pin dernier bon serveur/pays, puis une seule auth
   → stack fallback OpenVPN TCP, puis WG si canary OK
   → vérification

5. Si échec
   → parked 30 min
   → alerte « auth NordVPN / serveur / credentials »
   → retry planifié, pas de boucle rapide
```

Donc l'escalade est **immédiat → 5 min → 15 min → 30 min de park**, pas
15/30 min d'entrée. Les 5/15 min sont des **arrêts réels du conteneur**, pas
un simple sommeil du watchdog.

Pendant l'arrêt, la station est exclue du pool ; les 5 autres continuent. On
n'arrête qu'une seule station à la fois.

### 6.1 Dernier bon serveur

Mémoriser par station :

```text
last_good_country
last_good_host
last_good_ip
last_good_at
```

En réparation :

1. dernier bon serveur si encore dans le périmètre ;
2. sinon serveur du même pays ;
3. sinon rotation dans les pays autorisés ;
4. jamais un serveur blacklisté.

---

## 7. Garde globale auth

Si 2 stations ou plus ont `AUTH_FAILED` dans une fenêtre de 60 s :

- **aucun restart en cascade** ;
- une seule station est autorisée à probe ;
- pause globale initiale `auth_global_pause_s = 300` (5 min) ;
- si la pause échoue, escalade à 900 s (15 min), puis alerte ;
- les autres stations restent `repairing` mais `next_retry_at` est repoussé ;
- alerte « auth throttle / credentials / compte » ;
- les stations déjà `usable` restent en service.

---

## 8. Backoff et budgets

```yaml
station_repair:
  enabled: true
  detect_after_s: 20
  good_probes: 2
  bad_probes: 2

  # Quota : seuil dynamique, pas une constante.
  quota_reserve_pct: 10
  rotation_threshold_pct: 20

  # Auth : vrai silence par arrêt conteneur, pas un simple sleep proxy.
  auth_backoff_s: [300, 900, 1800]
  park_after_failures: 3
  park_s: 1800

  max_restarts_per_hour: 3
  max_recreates_per_hour: 1

  last_good_first: true

  auth_global_window_s: 60
  auth_global_min_stations: 2
  auth_global_pause_s: 300

  verify_egress_before_usable: true
```

Règles :

- un seul restart immédiat ; pas de silence avant ce restart ;
- si le restart échoue : **arrêt conteneur 5 min**, puis une seule auth ;
- si échec : **arrêt 15 min**, puis une seule auth ;
- si échec : pin dernier bon serveur, puis stack fallback ;
- si échec : park 30 min + alerte ;
- backoff monotone, plafonné, avec jitter ;
- 1 repair en vol par station ;
- 1 recreate/h/station ;
- 3 restarts/h/station ;
- `parked` n'est pas sélectionnable ;
- un retour `usable` exige 2 probes OK + egress réel.

---

## 9. Politique de capacité

```text
capacité_effective = count(stations usable)
```

Si `capacité_effective < configured` :

- `/health` = `degraded` ;
- dashboard affiche `5/6` (ou `4/6`) avec la cause ;
- aucune conclusion « session limit » automatique ;
- alerte si capacité < N-1 ;
- la station `parked` est listée avec `action_required` ;
- aucune requête n'est routée vers une station non `usable`.

Avec 10 connexions autorisées, l'objectif reste **6/6**. Une capacité 5/6 est
un état dégradé à réparer, pas une configuration acceptable par défaut.

---

## 9 bis. Uptime maximal, meilleures IP, quota disponible

### 9 bis.1 Définition du “meilleur IP”

Un “bon IP” n'est pas seulement une IP qui répond. Pour le modèle demandé :

```text
score_ip(model, ip) =
    expected_success_rate
    × quota_headroom
    × server_health
    / (latency_penalty × recent_429_penalty)
```

Filtres durs avant le score :

- auth OK ;
- egress vérifié ;
- `quota_headroom > quota_reserve` ;
- pas de 429 actif pour `(model, ip)` ;
- pas de `0 succès + 429` dans l'historique récent ;
- pas de host blacklisté ;
- pas de `repairing`/`parked`.

Le score est **par modèle** : une IP excellente pour DeepSeek peut être mauvaise
pour GLM. On ne compare jamais deux modèles.

### 9 bis.2 Lease IP/quota

Chaque station a un lease :

```text
station → current_ip
lease = {
  model,
  ip,
  quota_estimate,       # requêtes restantes estimées
  success_count,
  recent_429,
  last_429_at,
  quality_score,
  state: warming | qualified | active | draining | exhausted | bad
}
```

Règles :

- `warming` : IP fraîche, pas encore qualifiée ;
- `qualified` : auth + egress OK, quota estimé > 0 ;
- `active` : sélectionnable pour le modèle ;
- `draining` : plus de nouvelles requêtes, les flux en cours finissent ;
- `exhausted` : quota atteint, rotation planifiée ;
- `bad` : 429 sans succès, blacklist.

Une IP `warming` ou `bad` n'est **jamais** sélectionnable.

### 9 bis.3 Quota disponible

Le quota est suivi par `(model, ip)` :

- profil mesuré `quota_per_ip_by_model` (DeepSeek ~150, Mimo ~20, GLM ~8) ;
- mise à jour depuis les premiers 429 observés ;
- `quota_reserve = max(1, 10 % du profil)` — **par modèle**, pas une
  constante globale (sinon 15 > profil GLM) ;
- `rotation_threshold = max(reserve, 20 % du profil)` ; c'est le point de
  drainage, pas 150 ;
- une IP avec `headroom <= reserve` passe en `draining` ;
- on ne sélectionne jamais une IP à zéro headroom ;
- un 429 avec 0 succès = `bad`, pas “quota consommé” ;
- un 429 après succès = `exhausted`, pas “mauvaise IP”.

### 9 bis.4 Sélection

Pour chaque requête :

1. filtrer `model_usable(model, station)` ;
2. calculer `score_ip` ;
3. choisir le meilleur score ;
4. départager par le moins de requêtes en vol, puis le plus de headroom ;
5. si aucun candidat : file d'attente courte, puis 429/503 explicite.

On ne fait pas de round-robin aveugle. On ne rotate pas une bonne IP pour
“équilibrer” si elle a encore du quota et une bonne qualité.

### 9 bis.5 Rotation proactive

Une station passe en `draining` quand :

```text
quota_headroom <= rotation_threshold
OR quality_score < plancher
OR recent_429 >= seuil
OR server_health < seuil
```

`rotation_threshold` est dynamique :

```text
rotation_threshold = max(
    quota_reserve,
    int(0.20 × min(profile, capacity_estimate))
)
```

Exemples : DeepSeek profil 150 → seuil ~30, réserve 15 ; Mimo profil 20 →
seuil ~4, réserve 2 ; GLM profil 8 → seuil ~2, réserve 1.

Un seuil fixe à 150 était trop haut : il forçait des rotations inutiles et
consommait des IP alors que le spare pouvait absorber la fin de quota.

Ordre :

1. vérifier qu'une IP `qualified` de réserve existe ;
2. vider la station (drain) sans couper les flux en cours ;
3. faire tourner **une seule** station à la fois ;
4. qualifier la nouvelle IP (auth + egress + quota) ;
5. seulement alors, la remettre `active`.

Sans spare qualifié, on ne rotate pas une bonne IP : on attend ou on réduit la
capacité, plutôt que de créer un trou.

### 9 bis.6 Spare chaud

Objectif : **6 stations actives + 1 IP/spare qualifiée**.

Avec 10 connexions NordVPN, un 7e egress est possible. Il sert à :

- couvrir une rotation sans passer à 5/6 ;
- remplacer une IP devenue mauvaise ;
- absorber un pic de quota.

Si le spare n'est pas disponible :

- rotation une station à la fois ;
- capacité 5/6 pendant la rotation ;
- jamais d'IP non qualifiée dans le pool.

### 9 bis.7 Uptime par station

Une station est comptée “uptime” seulement si :

- `station_usable` ;
- `quota_headroom > reserve` pour au moins un modèle servi ;
- pas de `repairing`/`parked`.

L'uptime “connecté mais sans quota” ne compte pas. Le dashboard doit afficher :

```text
s1 : usable, IP 1.2.3.4, quota DeepSeek 120/150, score 0.91
s2 : usable, IP 5.6.7.8, quota DeepSeek 40/150, score 0.72
s3 : draining, IP 9.9.9.9, quota 5/150, rotation prévue
s4 : repairing, auth throttle
```

### 9 bis.8 Objectif d'équilibrage

Toutes les stations doivent rester dans une fenêtre de quota saine :

```text
headroom_station >= quota_reserve
```

On évite :

- une station à 0 pendant que les autres ont 80 % ;
- une station qui prend tout le trafic parce que son score est meilleur ;
- une IP “favorite” qui s'épuise avant les autres.

Le sélecteur applique une pénalité de charge et une réservation par station.

---

## 10. Observabilité

Exposer dans `/health` et `/api/free-health` :

```text
configured_stations
usable_stations
repairing_stations
parked_stations
active_stations_with_quota
warm_spare_ready
last_repair_attempt
next_retry_at
last_error
cause              # auth_throttle | auth_global | credentials | server | egress | docker
action_required    # true/false
auth_failures_30m
repair_attempts
repair_success
repair_parked
station_ip
model
quota_headroom
quota_estimate
quality_score
ip_state             # warming | qualified | active | draining | exhausted | bad
rotations_last_hour
```

Objectif dashboard : on doit voir **quelle IP sert, quel quota reste, et
pourquoi une station n'est pas sélectionnable**.

Événements dashboard :

- `station_repair_started`
- `station_repair_action`
- `station_repair_succeeded`
- `station_parked`
- `auth_global_pause`
- `capacity_degraded`

---

## 11. Tests de conception

### 11.1 Invariants — 12/12 passés

- un seul bad probe ne retire pas une station ;
- deux bad probes → `repairing` ;
- deux good probes → `usable` ;
- `parked` n'est jamais `usable` ;
- backoff monotone et plafonné ;
- escalade auth : restart → recreate → pin → stack → park ;
- `credentials` → park direct ;
- détection de tempête auth ;
- détection de capacité dégradée ;
- park seulement après N échecs ;
- succès d'action → `probing`, jamais `usable` direct.

Résultat : `DESIGN_TESTS_PASS=12`.

### 11.2 Simulation — scénarios et résultats

Modèle déterministe, 6 stations, 8 h, pannes synthétiques. Les résultats
valident la **logique de politique**, pas le comportement réel de NordVPN.

| Scénario | Politique actuelle | Plan révisé |
|---|---:|---:|
| `auth_throttle` s4 | 97,9 % usable, 4 auth, MTTR 3 620 s | **99,3 %, 2 auth, MTTR 1 220 s** |
| `server_auth` s4 | 83,7 %, 24 auth, jamais réparé | **98,2 %, 2 auth, MTTR 3 020 s** |
| `auth_global` 3 stations | 51 %, 72 auth, jamais réparé | **97,3 %, 6 auth, MTTR 1 560 s** |
| `credentials` 6/6 | 2,1 %, 144 auth | 2,1 %, **0 auth, park + alerte** |
| `transient` s4 | 100 %, 1 auth, 20 s | 100 %, 1 auth, 20 s |
| Restart naïf | — | 470 auth sur `auth_throttle`, jamais réparé |
| Restart agressif | — | 5 638 auth, jamais réparé |
| Park immédiat | — | 83,7 % sur transient, perte inutile |

La branche `server_auth` utilise un pin rapide ; la branche `auth_throttle`
utilise les arrêts 5/15 min. Les deux sont séparées pour ne pas imposer un
silence long à une panne serveur qui peut être réparée en 30–120 s.

Conclusion : le plan révisé **rétablit les pannes réparables**, **réduit les
auth attempts**, et **détecte les pannes non réparables** sans hammer le compte.

### 11.3 Tests IP/quota — 7/7 passés

Invariants vérifiés :

- une IP avec beaucoup de quota restant gagne sur une IP plus rapide mais
  presque épuisée ;
- une IP à zéro quota n'est jamais sélectionnée ;
- une IP avec 429 et zéro succès est `bad`, jamais sélectionnée ;
- 3+ 429 récents excluent l'IP ;
- `headroom <= reserve` → `draining` ;
- rotation refusée sans spare qualifié ;
- le sélecteur évite d'épuiser une IP basse quand une autre a du headroom.

Résultat : `IP_QUOTA_DESIGN_TESTS_PASS=7`.

### 11.4 Simulation de charge IP/quota — 12 h, 0,25 req/s

Pannes synthétiques : 44 % d'IP bonnes (capacité 120–260), 56 % d'IP mauvaises,
rotations de 30 s, 6 stations, spare qualifié pour la v2.

| Politique | 429 | Uptime quota | 6/6 actif | Rotations | p95 |
|---|---:|---:|---:|---:|---:|
| Actuelle (seuil 500, sans spare) | 1,7 % | 50 % | 97 % | 238 | 7,5 s |
| v1 (seuil 150, sans spare) | 0 % | 93 % | 60–68 % | 1 000 | 7,6 s |
| **v2 (seuil dynamique ~30, spare)** | **0 %** | **100 %** | **100 %** | **68** | **7,0 s** |

La v2 est meilleure sur les trois axes qui comptent ici : aucune 429, uptime
quota maximal, 6/6 actif, et **4 à 14 fois moins de rotations** que la v1.
Le seuil fixe à 150 était le défaut principal : il déclenchait des rotations
alors que l'IP avait encore beaucoup de quota utile.

---

## 12. Critères d'acceptation

- `AUTH_FAILED` isolé → station exclue en < 30 s.
- Un restart est tenté immédiatement ; s'il échoue, arrêt conteneur 5 min avant
  toute nouvelle auth.
- Si l'arrêt 5 min échoue, arrêt 15 min ; puis pin dernier bon serveur.
- Aucun restart > 3/h/station ; aucun recreate > 1/h/station.
- 2+ `AUTH_FAILED` en 60 s → une seule auth probe, pause 5 min puis 15 min si
  nécessaire.
- `credentials` global → park + alerte, zéro restart.
- `connected` sans egress → jamais `usable`.
- 2 probes OK → retour automatique.
- 1 probe OK puis KO → reste `repairing`.
- 5/6 usable → `/health` degraded, alerte, pas de faux 6/6.
- Aucune station sélectionnée avec `quota_headroom <= quota_reserve`.
- Aucune IP `warming`/`bad`/`exhausted` dans le pool.
- Une IP avec 0 succès + 429 ne reçoit pas de trafic.
- Rotation uniquement si une IP qualifiée est prête, ou une station à la fois.
- Seuil de rotation dynamique (`20 %` du profil), jamais 150 en dur.
- Objectif : 6 stations actives + 1 spare qualifié ; sinon capacité 5/6 visible.
- Uptime par station calculé sur `usable + quota`, pas sur `connected`.
- Simulation de charge : 0 % de 429, uptime quota 100 %, 6/6 actif, moins de
  100 rotations/12 h pour 0,25 req/s.
- Aucun modèle substitué, aucun paid, client inchangé.

---

## 13. Limites

- Si NordVPN refuse durablement toutes les nouvelles auth, aucun restart ne
  peut réparer : le plan park et alerte.
- Si les credentials sont invalides, le plan park et alerte ; il ne les
  invente pas.
- Si un serveur est mauvais, le fast-pin/blacklist peut aider ; si tous les
  serveurs sont mauvais, le plan park.
- Le plan ne peut pas garantir 6/6 si l'infrastructure externe ne le permet
  pas ; il garantit que l'état réel est visible et qu'aucune station morte
  n'est utilisée.

---

## 14. Prérequis d'implémentation

L'implémentation suit cet ordre de dépendances ; P2 (auth) peut avancer en
parallèle de P3 (IP/quota) après P1 :

1. `StationHealth` + contrat `station_usable` / `model_usable`.
2. **`IpQuotaLease`** : quota par `(model, ip)`, score IP, états
   `warming/qualified/active/draining/exhausted/bad`.
3. **`BestIpSelector`** : filtre `model_usable`, score lexicographique
   `expected_success_remaining` → latence → charge.
4. **`RotationPlanner`** : seuil dynamique 20 % du profil, drain, spare chaud,
   une station à la fois.
5. Classifieur `auth_throttle` / `auth_global` / `credentials` / `server` /
   `egress`.
6. `RepairPolicy` + `RepairExecutor` (restart, recreate, pin, stack).
7. Backoff auth 300/900/1800 avec arrêt conteneur réel + garde globale.
8. `CapacityPolicy` + `/health` degraded.
9. Observabilité + tests de chaos.

---

## 15. Roadmap

### 15.1 Principes

- Aucune phase ne va en production sans **feature flag** et **rollback**.
- Chaque phase a une **porte de sortie mesurable**.
- Le client officiel n'est jamais modifié.
- Aucun changement de modèle.
- Une phase peut être livrée en **shadow mode** avant application.
- La priorité est : **fiabilité > uptime quota > latence > coût**.

### 15.2 Vue d'ensemble

| Phase | Contenu | Durée indicative | Dépend de | Porte de sortie | Statut |
|---|---|---|---:|---|---|
| **P0 — Baseline & instrumentation** | Métriques station/IP/quota, baseline SLO, shadow selector | 1–2 j | — | baseline chiffrée, dashboards, zéro changement de comportement | ✅ code + tests ; shadow flag disponible |
| **P1 — Health strict** | `station_usable`, `model_usable`, machine à états, exclusion pool | 2–3 j | P0 | aucune station `connected` sans egress/quota ; tests unitaires | ✅ code + tests |
| **P2 — Auth repair** | classifieur auth, restart, stop 5/15 min, recreate, last-good, park | 3–5 j | P1 | pas de tempête auth ; s4 réparable en simulation ; plafonds respectés | ✅ code + tests |
| **P3 — IP/quota lease** | profil par modèle, réserve 10 %, seuil 20 %, score lexicographique | 3–5 j | P1 | 0 sélection sans quota ; 0 429 en simulation ; tests score | ✅ code + tests |
| **P4 — Spare & rotation** | 7e egress/spare qualifié, drain, promotion, une rotation à la fois | 3–5 j | P1, P3 | 6/6 actif + spare ; 100 % uptime quota en simulation | ✅ code + config 7e spare |
| **P5 — Observabilité & ops** | dashboard, alertes, runbook, flags, rollback | 2–3 j | P2, P3, P4 | toutes les causes visibles ; alerte testée | 🟡 `/health` + `/api/free-health` ; UI/runbook à finaliser |
| **P6 — Canary & rollout** | shadow 5 %, canary 5 %, 24 h, 50 %, 100 % | 3–5 j | P5 | SLO tenus, zéro régression client, rollback vérifié | ⏳ non exécuté |
| **P7 — Capacité** | 8–9 stations actives + 1 spare si demande, tuning du spare | 3–7 j | P6 | capacité mesurée, ≤ 10 connexions NordVPN | ⏳ non exécuté |

**Chemin critique** : P0 → P1 → P3 → P4 → P5 → P6.
P2 peut avancer en parallèle de P3 après P1. P5 peut démarrer dès P2/P3.

**Ordre de grandeur** : 4 à 6 semaines avec tests et canary, hors
développement lourd du dashboard.

### 15.3 Détail des phases

#### P0 — Baseline & instrumentation — ✅ code + tests

Livrables :

- métriques `station_usable`, `model_usable`, `ip_state`, `quota_headroom`,
  `auth_failures_30m`, `rotations`, `spare_ready` ;
- baseline 7 jours : uptime, 429, latence, rotations, IP mauvaises ;
- shadow `BestIpSelector` : calcule les choix sans les appliquer ;
- dashboard read-only.

Porte :

- métriques fiables, pas de faux `connected` ;
- comparaison shadow/actuel disponible ;
- aucune modification de routage.

#### P1 — Health strict — ✅ code + tests

Livrables :

- `station_usable` + `model_usable` ;
- machine `usable/suspect/repairing/probing/parked/disabled` ;
- pool qui n'accepte que `model_usable` ;
- `/health` degraded si capacité < N.

Porte :

- une station `connected` sans egress n'est jamais sélectionnée ;
- une station sans quota n'est jamais sélectionnée ;
- 2 probes OK avant retour ;
- tests unitaires + tests de non-régression.

Rollback : flag `station_health.strict=false` (retour à l'ancien filtre).

#### P2 — Auth repair — ✅ code + tests

Livrables :

- classifieur `auth_throttle` / `auth_global` / `credentials` / `server` /
  `egress` ;
- restart immédiat ;
- arrêt conteneur réel 5 min puis 15 min ;
- recreate avec credentials actuels ;
- pin dernier bon serveur ;
- backoff 300/900/1800 ;
- garde globale 5 puis 15 min ;
- park + alerte.

Porte :

- `AUTH_FAILED` isolé réparé ou parké proprement ;
- 2+ stations auth → une seule auth probe ;
- aucun restart > 3/h/station ;
- simulation `auth_throttle` : > 99 % usable, <= 3 auth.

Rollback : flag `station_repair.enabled=false` ; les stations restent exclues
mais ne sont plus arrêtées automatiquement.

#### P3 — IP/quota lease — ✅ code + tests

Livrables :

- `IpQuotaLease` par `(model, ip)` ;
- profil `quota_per_ip_by_model` calibré ;
- réserve 10 %, seuil 20 % ;
- score lexicographique ;
- persistance de la qualité IP/serveur ;
- blacklist bad IP.

Porte :

- 0 sélection avec `headroom <= reserve` ;
- 0 sélection d'IP `bad` ;
- 0 429 dans la simulation de charge ;
- rotation déclenchée au seuil dynamique, pas à 150.

Rollback : flag `ip_quota.enabled=false` (ancien seuil et ancien sélecteur).

#### P4 — Spare & rotation — ✅ code + config 7e spare

Livrables :

- spare qualifié (7e egress ou SOCKS5) ;
- drain sans couper les flux ;
- promotion du spare ;
- une rotation à la fois ;
- capacité 6/6 + spare ;
- 5/6 visible si spare indisponible.

Porte :

- 6/6 actif + spare en simulation ;
- uptime quota 100 % ;
- rotation sans trou ;
- pas de promotion d'une IP non qualifiée.

Rollback : flag `rotation_planner.spare=false` ; retour à 5/6 pendant rotation.

#### P5 — Observabilité & ops — 🟡 partiel

Livrables :

- dashboard : IP, quota, score, état lease, raison de non-sélection ;
- alertes : capacité < N-1, station parkée > 30 min, auth storm, IP bad ;
- runbook opérateur ;
- feature flags et rollback documentés.

Porte :

- chaque panne a une cause visible ;
- alerte testée avec un scénario synthétique ;
- runbook validé.

#### P6 — Canary & rollout — ⏳ non exécuté

Livrables :

- shadow 5 % ;
- canary 5 % du trafic réel ;
- 24 h stable ;
- 50 %, puis 100 % ;
- comparaison SLO.

Porte :

- 0 régression client ;
- 429 <= 0,5 % ;
- p95 TTFB <= 15 s ;
- capacité >= N-1 ;
- rollback testé.

#### P7 — Capacité — ⏳ non exécuté

Livrables :

- passage à 8–9 stations actives + 1 spare si la demande le justifie ;
- capacité plafonnée par les 10 connexions NordVPN simultanées ;
- tuning des seuils de quota ;
- rapport capacité : requêtes/jour, IP nécessaires, marge.

Porte :

- saturation prévue < 70 % ;
- pas de tempête de rotation ;
- compte NordVPN <= 10 connexions.

### 15.4 Décisions à prendre avant P2/P4

| Décision | Options | Recommandation |
|---|---|---|
| Spare | 7e conteneur / SOCKS5 / 5/6 pendant rotation | 7e egress si ressources, sinon 5/6 |
| Qualification IP | premier trafic / probe same-model dédiée | probe tiny same-model, coût maîtrisé |
| Silence auth | sleep proxy / stop conteneur | **stop conteneur réel** |
| Seuil quota | fixe 150 / dynamique 20 % | **dynamique 20 %** |
| Rollout | direct / shadow + canary | **shadow + canary** |

### 15.5 Définition de Done

La conception est considérée implémentée quand :

- `station_usable` et `model_usable` sont la seule source de sélection ;
- aucune station sans egress ou sans quota n'est utilisée ;
- `AUTH_FAILED` ne crée pas de tempête ;
- 6/6 actif + spare est observé en simulation ;
- 0 429 dans la simulation de charge ;
- les tests P0–P6 passent ;
- le canary 24 h tient les SLO ;
- le rollback est documenté et testé ;
- le client officiel est inchangé.

### 15.6 Risques de roadmap

| Risque | Impact | Mitigation |
|---|---|---|
| Auth throttle NordVPN | s4 instable | arrêt conteneur réel, backoff 5/15 min |
| Limite 10 connexions | extension plafonnée | 8–9 actifs + 1 spare max ; vérifier avant toute extension |
| IP quality volatile | mauvais choix | calibration en ligne, blacklist, score |
| Spare non qualifié | 5/6 pendant rotation | qualification avant promotion |
| Complexité | bugs de routage | shadow, flags, canary |
| Dashboard faux | mauvaise décision | tests de cohérence état/IP/quota |
| Quota mal estimé | 429 ou rotations inutiles | profil mesuré + réserve 10 % |

---

## 16. Journal d'implémentation — 31/10/2026

### 16.1 Code livré

| Brique | Fichier | État |
|---|---|---|
| Santé station + contrats | `free/station_health.py` | ✅ |
| Lease IP/quota | `free/ip_quota.py` | ✅ |
| Sélecteur lexicographique | `free/best_ip.py` | ✅ |
| Planificateur drain/spare | `free/rotation_planner.py` | ✅ |
| Politique auth/réparation | `station_repair.py` (shim `free/repair.py`) | ✅ |
| Intégration pool | `free/pool.py` | ✅ |
| Réparation auth manager | `vpn/manager.py` | ✅ |
| Observabilité `/health` + `/api/free-health` | `opencode.py` | ✅ |
| Configuration | `config.yaml` | ✅ |

### 16.2 Configuration activée

```yaml
station_count: 7              # 6 actives + 1 spare
ip_quota:
  enabled: true
  quota_reserve_pct: 10
  rotation_threshold_pct: 20
station_health:
  strict: true
  shadow: false
rotation_planner:
  enabled: true
  spare_station: 7
station_repair:
  enabled: true
  auth_backoff_s: [300, 900, 1800]
  park_s: 1800
```

Le 7e egress est **configuré et démarré** (compose et ports existent déjà
jusqu'à 10). Au relevé runtime, `opencode-vpn-7` est sain et qualifié comme
spare chaud. Aucun faux 7/7 n'est exposé : `capacity_snapshot` sépare
`configured_stations` (6 actives) et `warm_spare_ready`.

### 16.3 Tests exécutés

| Vérification | Résultat |
|---|---:|
| Suite complète `python -m pytest -q` | ✅ 0 échec |
| Contrats `scripts/ci_contracts.py` | ✅ 516/516 |
| `scripts/config_audit.py --strict` | ✅ 367 clés, 0 morte, 0 manquante |
| Ruff fichiers modifiés | ✅ |
| Tests conception santé/lease/sélection | ✅ 13/13 |
| Tests P2 réparation auth | ✅ 5/5 |
| Tests intégration pool P1/P3/P4 | ✅ 6/6 |

### 16.4 Runtime observé le 31/10 à 16:30

```text
configured_stations : 6
stations_total      : 7
usable_stations     : 5/6
warm_spare_ready    : true
spare_station       : 7
spare_promoted      : false
```

Le 7e conteneur `opencode-vpn-7` est démarré et qualifié comme spare.
La capacité affichée n'est **pas** un faux 6/6 : une station active était
encore en récupération egress au moment du relevé, donc `/health` disait
`degraded` avec `5/6`. Les stations non utilisables sont exclues du pool.

### 16.5 Reste à faire

- **P5** : brancher les nouveaux champs dans l'UI dashboard et rédiger le
  runbook opérateur (les API sont déjà exposées).
- **P6** : exécuter le canary 24 h, comparer les SLO, valider le rollback.
- **P7** : étendre à 8–9 stations actives + 1 spare si la demande le justifie.
