# Plan de correction — récurrence « 6/6 connectées mais 0/6 utilisables »

**Date : 2026-11-03** | **Statut : P0 implémenté, quota free = limite dure** | **Objectif : plus jamais 0/6 client-usable, service continu.**

> **Implémentation (2026-11-03, 18:30–19:00 UTC)** — voir §9 pour le journal
> détaillé. Le superviseur est auto-démarré et surveillé, la rotation
> proactive tourne toutes les 60 s, l'AUTH bascule en WireGuard
> automatiquement, et les pins morts sont nettoyés. **Limite mesurée : seul
> `87.120.102.0/24` rend des 200 ; les autres plages sont 429 jusqu'au reset
> UTC (~00:10).** Le 6/6 permanent en free n'est pas garanti sans la clé
> payante ou un egress résidentiel (§5 P2).

---

## 1. Ce qui s'est passé

- 6/6 stations **connectées**.
- **0/6 client-usable** : toutes les IPs courantes étaient `daily-bucket-exhausted` (429 upstream).
- Les 6 stations étaient concentrées sur le **même pool** (`87.120.102.x` / serveurs albanais legacy).
- Le superviseur de réparation était **arrêté**.
- `on_429_action: cooldown` : le proxy marquait l'IP et passait à une autre, **sans rotation**.
- Aucun **warm pool** d'IPs fraîches validées : quand le pool a expiré, il n'y avait rien à promouvoir.
- Le switch AUTH→WG avait corrigé l'AUTH, mais **pas le quota**.

**Cause racine : épuisement corrélé du quota + absence de réserve chaude + rotation non automatique.**

---

## 2. Objectif mesurable

| Métrique | Cible |
|---|---|
| `client_usable` | **6/6 en permanence** |
| `client_usable` minimum | ≥ 5/6 |
| `warm_pool` validé | ≥ 6 IPs |
| Part du top /24 | ≤ 50 % |
| `auth_failures_30m` | ≤ 2 |
| Taux de succès client | ≥ 99 % |
| Durée sans 0/6 | 24 h/24 |

---

## 3. Corrections P0 — empêcher la récidive

### 3.1 Superviseur toujours actif

- Le `fresh_chain_supervisor` doit **démarrer avec le proxy** (service/tray), pas à la main.
- Throttle : **2 réparations parallèles / 2 rebinds/min**.
- Ne jamais l'arrêter pendant une pause AUTH : le switch WG doit être tenté d'abord.

### 3.2 Rotation automatique sur quota

- `on_429_action: both` (cooldown + rotation).
- `auto_ov_fail_threshold: 1` (déjà fait).
- `auto_flip_cooldown_min: 5` (déjà fait).
- `station_max_rotations_per_hour: 10`, `rotation_storm_cooldown_s: 600` pour éviter l'emballement.

### 3.3 Rotation **préventive** (avant le 429)

- Le lease `IpQuotaRegistry` passe en `DRAINING` quand `headroom < rotation_threshold_pct` (20 %).
- Ajouter un déclencheur : dès qu'une station est `DRAINING`, **promouvoir un WARM** ou lancer une rotation, sans attendre le 429.
- Le quota ne doit jamais être consommé jusqu'à zéro.

### 3.4 Warm pool validé

- Maintenir **≥ 6 IPs validées** (`last_status=200`, `reset_at`, `last_ok_at`) dans `ip_state`.
- Le superviseur remplit le pool quand une station tourne.
- Les IPs 429 passent en `COOLING` avec leur `reset_at` ; elles sont re-probées après reset.
- Le parser `scripts/recover_hostnames.py` enrichit le mapping `host ↔ egress` en continu.

### 3.5 Diversité obligatoire

- **Interdire** que les 6 stations soient sur le même /24 ou le même pays.
- Contrainte : `max_stations_per_24 = 2`, `max_stations_per_country = 2`.
- Répartir sur au moins **3 /24 distincts** (idéalement 3 pays/régions).
- C'est la correction directe de l'épuisement corrélé.

### 3.6 Métrique honnête + alertes

- `client_usable = station_usable && model_usable` (déjà codé).
- Dashboard : `client_usable`, `warm_pool`, `top /24 share`, `auth30`.
- Alertes : `client_usable < 2`, `warm_pool < 3`, `auth30 > 5`, top /24 > 50 %.

---

## 4. Corrections P1 — fiabilité structurelle

1. **Scheduler quota-aware** (`ip_quota` persistant) :
   - états `ACTIVE` / `WARM` / `COOLING` / `DEAD` ;
   - sélection `ACTIVE > WARM > COOLING(reset passé)` ;
   - re-probe à `reset_at + jitter` ;
   - promotion WARM dès qu'une ACTIVE faiblit.
2. **Auto-start du superviseur** dans le lifespan `opencode.py` ou le service Windows.
3. **Watchdog du superviseur** : s'il meurt, le relancer automatiquement.
4. **Découverte ciblée** : `87.120.102.x` d'abord, puis autres plages ; pas de scan full-catalogue continu.
5. **Mapping lazy** : parser les logs `fresh_chain` après chaque chaîne, mettre à jour `ip_state`.

---

## 5. Corrections P2 — garantie 24/7

- **Clé API OpenCode valide** (la clé actuelle est expirée) : supprime la dépendance au free.
- **Egress résidentiel/mobile** : seule voie free réellement fiable si le datacenter est flaggé.
- **Fallback payant** : 1 clé en secours pour les trous de quota.
- **2e compte/provider** si une 7e station de validation est nécessaire (la limite pratique du compte refuse la 7e session).

---

## 6. Runbook automatique

Quand une station devient `model_usable=false` :

1. **Si AUTH_FAILED** → switch OpenVPN → WireGuard (déjà automatique, seuil 1).
2. **Si quota 429** → passer l'IP en `COOLING` avec `reset_at` ; promouvoir un `WARM`.
3. **Si aucun WARM** → lancer une chaîne `fresh_chain` (2 parallèles max) sur un hostname **non actif**.
4. **Si 0/6 usable** → le superviseur répare en priorité les stations ; ne pas ajouter de 7e station.
5. **Si le pool entier est 429** → chercher un autre /24/pays ; si aucun, alerter et basculer paid/résidentiel.

---

## 7. Critères d'acceptation

- [ ] 6/6 client-usable pendant 24 h consécutives. *(bloqué par le quota free — §9.4)*
- [ ] Aucun épisode 0/6. *(le superviseur répare, mais le pool entier peut être 429)*
- [ ] `warm_pool ≥ 6` en permanence. *(partiel : `verified_spares` alimenté)*
- [ ] Top /24 ≤ 50 % des stations. *(assoupli : seul `87.120.102.0/24` rend des 200)*
- [ ] `auth30 ≤ 2`. *(AUTH OpenVPN intermittent, bascule WG automatique)*
- [x] Superviseur auto-démarré et supervisé.
- [x] Rotation préventive déclenchée à `DRAINING` (pas à 429).
- [ ] Alerte si `client_usable < 2`. *(le superviseur logge et répare)*

---

## 8. Ordre d'application

1. `on_429_action: both` + rotation préventive `DRAINING`.
2. Auto-start + watchdog du superviseur.
3. Warm pool ≥ 6 + diversité /24.
4. Scheduler `ip_quota` persistant.
5. Clé payante / résidentiel pour la garantie.

**Sans les points 1-3, l'épisode 0/6 se reproduira au prochain épuisement du pool.**

---

## 9. Journal d'implémentation (2026-11-03, 18:30–19:00 UTC)

### 9.1 Ce qui est en place

| Correction | État | Détail |
|---|---|---|
| Superviseur auto-démarré | ✅ | `opencode.py` : `_ensure_fresh_chain_supervisor()` au lifespan + watchdog 60 s. Testé en tuant le superviseur : relancé en < 75 s. |
| Watchdog superviseur | ✅ | `_fresh_chain_supervisor_watchdog()` ; vérifie le PID du lock. |
| Rotation proactive | ✅ | `free/pool.py` : `_proactive_loop()` toutes les `proactive_tick_s` (60 s), indépendante du trafic client. |
| `on_429_action` | ✅ | `both` (cooldown + rotation) dans `config.yaml`, appliqué à chaud. |
| AUTH → WireGuard | ✅ | `auto_ov_fail_threshold: 1`, `auto_flip_cooldown_min: 5`, hot-reload manager. Le superviseur bascule aussi après 2 chaînes OpenVPN échouées et suit `stack_effective`. |
| Pin mort sans superviseur | ✅ | `free/pool.py` : `_launch_pin_escape()` si le superviseur est mort ; le manager efface hostname **et** pays. |
| Stale winner / pin | ✅ | `fresh_chain.py` invalide le winner au début d'un run ; `winner_at` + TTL 5 min ; `_clear_winner()` si l'egress live diffère. |
| Grace winner | ✅ | `winner_grace_s: 30` : plus de relance de chaîne à t+0 (fin du rebind loop). |
| Diversité pays | ✅ | `max_per_country: 2` ; les pays sur-représentés sont **dépriorisés**, pas exclus. |
| Diversité /24 | ⚠️ assouplie | `max_per_24: 6` : la mesure montre que **seul `87.120.102.0/24` rend des 200**. Un max de 2 laissait 4 stations sur des plages 429. |
| Warm spares | ⚠️ partiel | `verified_spares` + `ip_state` alimentés par les chaînes ; pas encore de maintien actif de 6 IPs chaudes. |
| Mapping lazy | ✅ | `scripts/recover_hostnames.py` : 621 mappings IP→host extraits des logs ; 146 hostnames en base. |
| Backfill hostnames | ✅ | `scripts/backfill_ip_hostnames.py` : union de `requests.db`, `observations`, `assignments`, `verified_spares`, inventaire CSV, backups et logs de chaînes. **+562 IPs avec hostname, +37 hostnames sur IPs existantes, 394 pays corrigés** ; `ip_state` = 4 032 lignes, 738 avec hostname (155 hostnames distincts). |
| Pause reset-aware | ✅ | `_ledger_reset_at()` : quand tous les buckets sont épuisés, le superviseur suspend les chaînes jusqu'au reset UTC (persisté dans `allocator_state.json`, survit aux redémarrages). Fini le churn AUTH toutes les 15 min. |
| Alerte capacité | ✅ | Log `ALERT client_usable=<n>/6 (<2) — warm_pool=<n>` toutes les 5 min. |

### 9.2 Mesure qui change la stratégie

Les probes des chaînes sur l'Autriche, les Émirats, le Canada et les autres
pays rendent **429 avec `retry_after` ≈ 20 000 s** (reset UTC ~00:10), alors
que les IPs `87.120.102.x` rendent **200**. Le quota free n'est donc pas
seulement « par IP » : les plages datacenter hors Albanie sont déjà brûlées.
Conséquence : la contrainte « max 2 par /24 » **réduit** la disponibilité au
lieu de l'augmenter. Elle est conservée pour les pays, assouplie pour le /24.

### 9.3 État mesuré à 18:55 UTC

- 5/6 connectées, **3–4/6 client-usable** selon les rotations.
- Requêtes client `deepseek-v4.1-flash-free` et `deepseek-v4.1-flash` : **HTTP 200**.
- `auth30` ≈ 14 (AUTH OpenVPN intermittent sur les stations réparées).
- Les stations 1/5/6 alternent entre IPs `87.120.102.x` (200) et plages
  étrangères (429) ; le superviseur les répare en continu.

### 9.4 Conclusion honnête

L'automatisation demandée est en place : plus d'intervention manuelle pour
AUTH, pin mort, superviseur arrêté, ou rotation quota. **Mais le 6/6 permanent
en free n'est pas atteignable tant que seul un /24 rend des 200.** Les points
P2 (clé payante valide ou egress résidentiel) sont la seule garantie 24/7.
Sans eux, le système maintient le meilleur sous-ensemble disponible et
continue de réparer, mais peut rester à 3–5/6 jusqu'au reset UTC.

### 9.5 Backfill des hostnames dans `ip_state`

`scripts/backfill_ip_hostnames.py` agrège toutes les preuves locales :

| Source | Paires host↔IP |
|---|---|
| `requests.db` (`server_host` + `free_model_ip`) | 142 |
| `observations` / `assignments` / `verified_spares` | 586 |
| `usable_ips_inventory.csv` (colonne `hosts`) | 130 |
| backups `ip_registry.db.bak-*` | 242 |
| logs/state `fresh_chain` | 1 473 |

Résultat appliqué : **4 032 lignes** dans `ip_state`, **738 avec hostname**
(155 hostnames distincts), **562 nouvelles IPs** insérées, **37 hostnames**
ajoutés sur des IPs existantes, **394 pays corrigés** depuis l'API NordVPN
(la table `servers` accumulait des pays obsolètes).

Limite : les **3 294 IPs historiques** de l'inventaire restent sans hostname.
Ce sont des IPs de sortie NAT absentes de l'API NordVPN et qu'aucun log local
ne relie à un hostname. Seule une observation future (chaîne qui bind un
hostname et obtient cette IP) pourra les mapper ; le script est idempotent et
peut être relancé après chaque campagne de chaînes.
