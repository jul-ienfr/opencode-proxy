# Roadmap — Fiabiliser et optimiser stations, IP et rotation

> **Statut 2026-10-26 : code des phases 0→5 livré, suite complète verte
> (`pytest -k "not docker"`). Reste l'activation live (redémarrage proxy) et
> l'observation 24 h de la phase 6. Suivi case par case :
> [SUIVI_ROADMAP_STATIONS_2026-10-26.md](SUIVI_ROADMAP_STATIONS_2026-10-26.md).**

Complément opérationnel de [l'audit stations/IP](AUDIT_STATIONS_IP_2026-10-26.md).  
Objectif : passer d'un système **fonctionnel en nominal** à un système **fiable, mesuré, rapide, maintenable et réellement optimal** sur le choix de l'IP.

---

## 1. Objectif et définition du succès

### Objectif produit

> À chaque requête free, choisir la station et l'IP qui maximisent la probabilité de succès et minimisent la latence, sous contraintes de quota, de géo et de disponibilité — sans jamais créer de coupure ni de flapping.

### North-star metrics

| Métrique | Aujourd'hui | Cible |
|---|---|---|
| Stations éligibles | 6/6 ponctuel | ≥ N−1 en continu, jamais < 2 |
| Taux de succès free | non mesuré | ≥ 95 % hors quota épuisé |
| TTFB p95 free | non mesuré par station | −20 % vs baseline |
| Rotation réussie | non mesurée (17 échecs / 23 succès sur ~3 h de logs) | ≥ 95 % |
| Durée rotation p95 | non mesurée | < 45 s |
| Flapping statut | ~1 transition/40 s sur station 1 | ≤ 1 transition/h/station hors rotation |
| Refresh statut p95 | ~0,24–0,48 s/station | < 100 ms/station |
| Compteurs quota après crash | perdus | ±1 requête |
| Config `.env` vs conteneurs | dérive confirmée | 0 dérive |
| Image gluetun | potentiellement mixte | 1 version pour toute la flotte |

### Contraintes

- Ne pas casser les contrats existants : `station_count`, `free_parallel`, `latency_rotation`, endpoints dashboard, tests actuels.
- Pas de big-bang : chaque lot derrière un flag, chemin legacy conservé jusqu'à validation.
- Pas de dépendance externe nouvelle (pas de Redis) : le proxy reste mono-process.
- Toute nouvelle clé de config doit avoir un test d'effet observable.

---

## 2. Principes de conception

1. **État désiré > actions impératives.** La flotte doit converger vers `station_count`, stacks, enabled, image ; les endpoints demandent un état, pas une suite de commandes Docker.
2. **Une seule source de vérité par décision.** Un seul `StationSelector` et un seul `StationQuality` ; les méthodes historiques délèguent.
3. **Mesurer avant d'optimiser.** Aucune sélection par score sans métriques de latence, succès, quota et rotation.
4. **Event-driven d'abord.** Les événements Docker et les transitions d'état déclenchent les refresh ; le polling reste un filet de sécurité.
5. **Coût de rotation explicite.** On ne rotate que si le gain attendu dépasse le coût (AUTH, coupure, quota, temps).
6. **Cut-over, pas cut-off.** On garde l'ancienne IP servable jusqu'à validation de la nouvelle, avec une fenêtre bornée.
7. **Idempotence et reprise.** Toute opération de flotte doit pouvoir être rejouée sans effet de bord.
8. **Observabilité d'abord.** Chaque décision doit être explicable par un champ d'état ou une métrique.

---

## 3. Architecture cible

```
config.yaml
   │
   ▼
ops/fleet.py  ── desired state ──►  reconcile()  ──►  VPNManager lifecycle
   │                                  │
   │                                  ├── ops/updater.py (image flotte)
   │                                  ├── ops/jobs.py (opérations longues, 202)
   │                                  └── docker_events.py
   │
   ├── ops/health.py  (classification, anti-flap, stack effective)
   │
   └── free/quality.py  (score, probes, historique)
           │
           ▼
   free/selector.py  (rank / choose / hedge / rotate)
           │
           ▼
   free/pool.py  (on_request, quotas, bad marks)
```

### Modules à créer / consolider

| Module | Rôle | Remplace / absorbe |
|---|---|---|
| `ops/health.py` | classification santé, frontières de logs, anti-flap, pays réel | extrait de `vpn/manager.py` |
| `ops/fleet.py` | état désiré, reconcile start/stop/restart/connect/disconnect | `_apply_station_count` |
| `ops/updater.py` | mise à jour d'image coordonnée | `_updater_loop` par manager |
| `ops/jobs.py` | jobs longs + progression SSE | endpoints bloquants |
| `free/quality.py` | score par station/IP, EWMA, succès, quota, host | nouveau |
| `free/probe.py` | sonde active légère par station | `health_check` non persisté |
| `free/selector.py` | sélection unique, hedge, cible de rotation | `_best_station`, `pick_candidates` |

Les shims existants (`station_supervisor.py`, `latency_rotation.py`, etc.) restent jusqu'à migration complète.

---

## 4. Roadmap par phases

Effort indicatif : **~34 jours-ingénieur** (7 semaines en solo, ~4 semaines à deux). Les phases 1→3 sont séquentielles ; 4 peut démarrer après 1 ; 5 en parallèle à partir de 3.

### Phase 0 — Baseline, métriques, garde-fous (2 j)

But : pouvoir prouver chaque amélioration et rollback.

| ID | Tâche | Fichiers | Tests / preuve |
|---|---|---|---|
| R0.1 | Snapshot baseline read-only : pool-status, vpn-status, inspect, `.env`, logs 1 h | `scripts/station_snapshot.py` (nouveau) | JSON horodaté |
| R0.2 | Métriques : `eligible_stations`, `flap_count_1h`, `rotation_attempts_total{result}`, `rotation_duration_s`, `probe_latency_ms`, `log_scan_duration_ms`, `quota_save_age_s`, `env_drift` | `observability/`, `dashboard/api.py` | exposition API + test |
| R0.3 | Flags de sécurité : `stations.health.effective_stack_filter`, `stations.health.flap_hysteresis_s`, `stations.quality.enabled`, `stations.reconciler.enabled` | `config.yaml`, `config/settings.py` | test de lecture/hot-reload |
| R0.4 | Fixtures de test : managers fake avec latence, horloge fake, docker fake | `tests/conftest.py`, `tests/fakes/` | suite verte |

**Critère de sortie** : baseline figée, métriques visibles, flags OFF, aucun changement de comportement.

---

### Phase 1 — Fiabilité immédiate : arrêter le flapping et la dérive (6 j)

But : supprimer les faux états et les incohérences de configuration.

| ID | Tâche | Détail | Tests |
|---|---|---|---|
| R1.1 | Classification par stack effective | `_check_auth_failed` / `_check_server_issue` reçoivent `effective_stack` ; ignorer les resets OV sur station WG ; exposer `secondary_path_issue` en info | WG + logs OV → reste `connected` ; OV + AUTH_FAILED → `error` |
| R1.2 | Frontière de guérison | `last_effective_ok_at` mis à jour sur egress OK ; ne compter que les marqueurs postérieurs | vieux reset → pas d'erreur |
| R1.3 | Churn borné | `_check_restart_churn` : fenêtre `min(window, 60 min)` ; seuil en restarts/min ; reset frontière au clear | 4 restarts/14 h → false ; 4/10 min → true |
| R1.4 | Anti-flap | `StationStatusTracker` : 2 observations consécutives, hystérésis 30–60 s, event SSE seulement sur état stable | 0 oscillation sur logs rejoués |
| R1.5 | Stacks `.env` par station | `_apply_stack` accepte `{station: stack}` ; upscale synchronise chaque manager avec sa stack ; reconcile au boot | mixte 6→8 conserve WG/OV/OV ; `.env` == conteneurs |
| R1.6 | `boot_error` dynamique | recalcul après chaque transition ; exposer `boot_error_at` | 6/6 → null |
| R1.7 | Persistance quota | dirty flag + flush 30 s + shutdown ; restauration conditionnée à l'IP | crash/restart → ±1 |
| R1.8 | Nettoyage state | retirer `1.1.1.1` et valeurs de test ; isolation pytest du fichier | state propre |

**Critère de sortie** : station 1 ne flappe plus ; `.env` aligné ; quota restauré ; tests de régression ajoutés.

---

### Phase 2 — Gestion de flotte complète (6 j)

But : une seule API/état désiré pour lancer, arrêter, reconnecter, réparer et mettre à jour toutes les stations.

| ID | Tâche | Détail | Tests |
|---|---|---|---|
| R2.1 | `ops/fleet.py` | `DesiredFleet` (count, stacks, enabled), `ObservedFleet`, `reconcile()` idempotent, verrous par station | reconcile 0→6, 6→3, 3→6 |
| R2.2 | Opérations par station | `start/stop/restart/connect/disconnect_station(sid)` ; état désiré persistant | disconnect tient 5 min |
| R2.3 | Endpoints flotte | `POST /api/vpn/fleet/{connect,disconnect,toggle}` + `POST /api/vpn/stations/{sid}/{start,stop,restart,connect,disconnect}` | 202 + job id/SSE |
| R2.4 | Corriger l'existant | `toggle` tous managers + pool ; `connect` fleet ; `heal` reconnecte les down ; UI par station | tests API + UI |
| R2.5 | Updater flotte | `ops/updater.py` : pull unique, image cible, recréation séquentielle, vérification, rollback | toutes les stations même image |
| R2.6 | Secret & image | retirer `control_api_key` du YAML tracké, régénérer, épingler gluetun | gitleaks + compose config |

**Critère de sortie** : « disconnect » ne sert plus de trafic ; « connect » remet toute la flotte ; une mise à jour laisse une flotte homogène.

---

### Phase 3 — Performance et rapidité (4 j)

But : réduire le coût des scans et les temps d'attente perçus.

| ID | Tâche | Détail | Tests |
|---|---|---|---|
| R3.1 | Logs incrémentaux | `ops/docker_logs.py` : watermark par conteneur, `--since` dernier scan − 2 s, `--tail` au boot | 1 seule lecture complète au boot |
| R3.2 | Refresh événementiel | TTL 5–10 s, verrou par station, refresh forcé uniquement sur event Docker/suspicion | p95 < 100 ms/station |
| R3.3 | Pin moins bavard | scan auth/TLS toutes les 2–3 s, control status d'abord | ≤ 30 exec/rotation |
| R3.4 | Jobs longs | `connect`/`next`/`update` en 202 + `ops/jobs.py` + SSE | client ne bloque plus |
| R3.5 | Pré-chauffage | après rotation, GET statique via SOCKS avant de marquer « prête » | 1ʳᵉ requête sans handshake |
| R3.6 | Nettoyage | supprimer le double `docker rm -f` du downscale | logs downscale propres |

**Critère de sortie** : dashboard < 2 s p95, rotation p95 < 45 s, aucun scan complet périodique.

---

### Phase 4 — Sélection optimale de l'IP (8 j)

But : choisir la meilleure station/IP pour chaque requête, pas seulement la suivante.

#### 4.1 Modèle de qualité (`free/quality.py`)

```python
@dataclass
class StationQuality:
    sid: int
    ip: str
    ewma_ttfb_ms: float | None
    p50_ms: float | None
    p95_ms: float | None
    success_rate: float
    recent_429: int
    last_429_age_s: float
    quota_used: int
    quota_limit: int
    server_load: float | None
    stability: float
    last_probe_at: float
    confidence: float
```

- EWMA log-space + p95 glissant par IP.
- Taux de succès roulant 200 / (200+429+timeout+5xx).
- Pénalité de récence 429 et de fraîcheur d'IP.
- Confiance basse au cold start ; score neutre tant que `confidence < min_samples`.
- Décroissance temporelle pour éviter qu'une vieille IP reste « bonne » éternellement.

#### 4.2 Sonde active (`free/probe.py`)

- 1 GET statique (robots.txt / URL configurée) via SOCKS5, **jamais** l'endpoint d'inférence.
- 1 sonde / 30 s / station, étalée, sémaphore 2, timeout 5 s.
- Mesure connect + TTFB ; met à jour `StationQuality` ; n'incrémente aucun quota.
- Une sonde échouée seule ne change pas le statut santé (le health owner décide).

#### 4.3 Sélecteur (`free/selector.py`)

- Hard gates : tunnel up, pas bad-mark, quota headroom, geo, breaker, pas hard-cooled.
- Score normalisé :

```
score = 0.40 * latency_score
      + 0.20 * success_score
      + 0.15 * quota_score
      + 0.15 * server_score
      + 0.10 * stability_score
```

- `choose()` : argmax + exploration ε (5 %) + part minimale pour éviter la starvation.
- `choose_hedge()` : second candidat seulement si son score est proche et que le coût est justifié.
- `choose_rotation_target()` : meilleur score attendu parmi les candidats éligibles, pas le prochain sid.
- Intégration : `_best_station`, `_best_station_excluding`, `pick_candidates` délèguent au sélecteur.

#### 4.4 Décision de rotation

- Déclencheurs : score < plancher pendant K mesures, quota restant < marge, 429 quota, échec santé.
- Coût : AUTH + coupure + temps + perte de warm-up ; ne pas rotationner si un autre candidat est meilleur.
- Cible : pays/serveur avec le meilleur score historique + `load` (pas seulement le pays suivant).
- Premier connexion : classer les pays/serveurs par historique ; après connexion, sonder ; si score sous plancher, re-rotation proactive bornée (1 fois).
- Cut-over : ancienne IP servie jusqu'à validation ; grâce = p95 de durée de pin ; état `draining`.

#### 4.5 Serveur / hostname

- Enregistrer le **hostname réellement utilisé** (`_current_hostname`), pas tout le top-5.
- Historique par hostname/ASN/pays : latence, succès, resets.
- `_least_loaded_hostnames` combine `load` NordVPN et score historique.
- WG : impossible de pinner le hostname ; compenser par le choix du pays et la qualité post-connexion. Envisager OV pour les stations où la qualité est critique si les données le justifient.

#### Tests

| Test | Attendu |
|---|---|
| 3 stations mockées 100/300/600 ms | la plus rapide choisie ≥ 90 % après warm-up |
| 429 sur la meilleure | pénalité puis bascule |
| station lente persistante | rotation déclenchée, pas de flapping |
| 6 stations, une à 0 requête | part minimale, pas de starvation |
| première connexion | classement historique + probe |
| cut-over | ancienne IP servie jusqu'à validation |

**Critère de sortie** : sélection par score active derrière flag, canary sur 1 station, métriques stables.

---

### Phase 5 — Maintenabilité et dette (5 j)

| ID | Tâche | Détail |
|---|---|---|
| R5.1 | Unifier les sélecteurs | une seule implémentation, anciennes méthodes en délégation |
| R5.2 | Breaker superviseur | soit le brancher réellement, soit le supprimer ; `build_supervisors` utilisé partout |
| R5.3 | Purge état latence | downscale → purge trackers/cooldowns/soft_history par sid |
| R5.4 | Config morte | `warmup_excluded_requests`, `prewarm_after_rotate`, clés legacy : implémenter ou supprimer |
| R5.5 | Modules | extraire `ops/health.py`, `ops/fleet.py`, `ops/updater.py`, `free/quality.py`, `free/selector.py` |
| R5.6 | Tests/CI | couverture des nouveaux modules, mypy, ruff, test config coverage |
| R5.7 | Docs | README, runbook, diagramme de décision, ADR |

**Critère de sortie** : 0 clé morte, 1 seul chemin de sélection, dette documentée.

---

### Phase 6 — Validation et déploiement progressif (3 j)

1. **Canary** : activer `quality.enabled` sur 1 station (la moins critique), comparer 24 h.
2. **Mesures** : TTFB p50/p95, taux de succès, 429, rotations, fallback payant.
3. **Gate** : si régression > 5 % sur p95 ou baisse du succès, rollback flag.
4. **Fleet** : activer sur toutes les stations, surveiller 24 h.
5. **Post-mortem** : documenter les écarts, ajuster les seuils.

---

## 5. Backlog priorisé (vue compacte)

| Priorité | Lot | Effort | Dépend de |
|---|---|---:|---|
| P0 | R0 baseline + flags | 2 j | — |
| P0 | R1.1–R1.4 flapping | 3 j | R0 |
| P0 | R1.5 stacks `.env` | 2 j | R0 |
| P0 | R1.6–R1.8 boot/quota/state | 2 j | R0 |
| P0 | R2.1–R2.4 flotte/API | 4 j | R1 |
| P1 | R2.5–R2.6 update/secret | 2 j | R2.1 |
| P1 | R3 logs/refresh/jobs | 4 j | R2 |
| P1 | R4.1–R4.3 quality/selector | 4 j | R1, R3 |
| P1 | R4.4–R4.5 rotation/cut-over | 4 j | R4.1–R4.3 |
| P2 | R5 maintenabilité | 5 j | R2–R4 |
| P2 | R6 validation/canary | 3 j | R4 |
| P2 | Nettoyages P2 audit | 2 j | R5 |

---

## 6. Feature flags et rollback

```yaml
stations:
  health:
    effective_stack_filter: true   # rollback false = ancien comportement
    flap_hysteresis_s: 45
  reconciler:
    enabled: false                 # activer après tests
  quality:
    enabled: false                 # canary station 6
    probe_interval_s: 30
    min_samples: 5
    exploration_pct: 5
    latency_weight: 0.40
    success_weight: 0.20
    quota_weight: 0.15
    server_weight: 0.15
    stability_weight: 0.10
```

- Chaque flag est lu à chaud, avec test d'effet.
- Le chemin legacy reste disponible jusqu'à la fin de la phase 6.
- Rollback = flag OFF + redémarrage éventuel, sans migration de données.

---

## 7. Risques et parades

| Risque | Probabilité | Impact | Parade |
|---|---|---|---|
| La sonde active ajoute de la charge | Moyenne | Moyen | 1/30 s/station, timeout 5 s, sémaphore 2, désactivable |
| Le score crée de la starvation | Moyenne | Moyen | part minimale + exploration + audit des parts |
| Le reconcile casse une station saine | Faible | Élevé | canary, verrous par station, dry-run, rollback |
| Le nouveau sélecteur dégrade le TTFB | Moyenne | Élevé | flag, comparaison baseline, canary 24 h |
| La mise à jour flotte interrompt le trafic | Moyenne | Moyen | fenêtre 03:00–05:00, idle, séquentiel, rollback image |
| Les tests Docker ne tournent pas en CI | Élevée | Moyen | mocks + job docker dédié, `-k "not docker"` en gate rapide |
| Dérive `.env` réapparaît | Faible | Élevé | reconcile au boot + test de dérive + CI |

---

## 8. Definition of Done

Une phase est terminée seulement si :

- code + tests unitaires + test d'intégration (fake docker) verts ;
- métrique/log observable ajouté ;
- flag de rollback documenté ;
- doc mise à jour (audit/roadmap/runbook) ;
- aucun changement de contrat API non documenté ;
- `ruff`, `mypy`, `pytest -k "not docker"` verts ;
- mesure avant/après consignée.

---

## 9. Premières 48 heures

1. Créer `scripts/station_snapshot.py` et figer la baseline.
2. Ajouter les métriques R0.2 et les flags R0.3 (OFF).
3. Écrire les tests de régression du flapping (R1.1–R1.4) avant correction.
4. Corriger R1.1 + R1.2 + R1.3 sur une branche dédiée.
5. Vérifier sur la flotte live : station 1 doit rester `connected` sans oscillation pendant 1 h.

Ensuite : R1.5 (stacks) puis R1.6–R1.8, avant de toucher à la sélection d'IP.
