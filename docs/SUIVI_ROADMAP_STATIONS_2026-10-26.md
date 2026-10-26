# Suivi d'exécution — roadmap stations / IP

Dernière mise à jour : 2026-10-26 14:50 (local).  
Case cochée = code + test présents. Les tests sont verts hors Docker :
`python -m pytest -k "not docker" -q` → **suite complète OK** (dernier run du
2026-10-26). Le proxy a été **redémarré à 13:41** pour charger les derniers
correctifs ; flotte live **6/6 healthy/routable**, `boot_error: null`,
`stale: false`, p95 API **54 ms**.

Référence : [ROADMAP_STATIONS_IP_2026-10-26.md](ROADMAP_STATIONS_IP_2026-10-26.md)

## Phase 0 — Baseline, métriques, garde-fous

- [x] R0.1 Script de snapshot read-only (`scripts/station_snapshot.py`)
- [x] R0.2 Métriques stations/rotation/probes/log-scan
- [x] R0.3 Flags de sécurité + hot-reload (`stations.health`, `stations.quality`)
- [x] R0.4 Fixtures/tests de base (`tests/test_stations_roadmap.py`)

## Phase 1 — Fiabilité immédiate

- [x] R1.1 Classification par stack effective — voie secondaire isolée,
      `secondary_path_issue` exposé, plus de faux `error` sur resets OV d'une
      station WG
- [x] R1.2 Frontière de guérison (`last_effective_ok_at`)
- [x] R1.3 Fenêtre de churn bornée + reset frontière
- [x] R1.4 Anti-flap (hystérésis, transitions stables, `flap_count_1h`)
- [x] R1.5 Stacks `.env` par station + reconcile boot/upscale
- [x] R1.6 `boot_error` dynamique
- [x] R1.7 Persistance quota périodique (30 s) + flush shutdown + garde IP
- [x] R1.8 Nettoyage state (validation IP au chargement, plus de pollution
      `1.1.1.1` appliquée à une IP réelle)
- [x] R1.9 Guérison des resets anciens + dédup `_pin_resets_1h` (un log
      ancien relu à chaque refresh ne ré-arme plus le degraded/widen ;
      test `test_same_old_reset_counted_once_across_refreshes`)
- [x] R1.10 Persistance du stack manuel par station (`stack_manual`) :
      un choix explicite WG/OV survit au reload même sous `vpn_stack: auto`
      (les anciens états sans drapeau restent ignorés ; tests dédiés)

## Phase 2 — Gestion de flotte complète

- [x] R2.1 `ops/fleet.py` + état désiré
- [x] R2.2 Opérations start/stop/restart/connect/disconnect par station
- [x] R2.3 Endpoints flotte + jobs (`/api/vpn/fleet/*`, `/api/vpn/jobs/*`)
- [x] R2.4 Correction toggle/connect/disconnect/heal + boutons UI par station
- [x] R2.5 Updater de flotte unique (`ops/updater.py`, seul station 1 pull)
- [x] R2.6 Secret : `control_api_key` retiré de `config.yaml`, migré dans
      `.env` (script `scripts/migrate_control_key.py`). Image `latest` conservée
      volontairement pour les hotfixes gluetun (décision documentée, pas un oubli).

## Phase 3 — Performance

- [x] R3.1 Logs bornés (`--tail`) sur refresh, auth/TLS, churn, pin
- [x] R3.2 Refresh sérialisé par station + cache 5 s + événements Docker
- [x] R3.3 Pin scan 2 s (au lieu de 0,5 s)
- [x] R3.4 Jobs longs 202 + statut consultable
- [x] R3.5 Pré-chauffage après rotation (`_post_rotation_prewarm_cb`)
- [x] R3.6 Suppression du double `rm -f` au downscale

## Phase 4 — Sélection optimale

- [x] R4.1 `StationQuality` + registre borné (`free/quality.py`)
- [x] R4.2 Sonde active (`free/probe.py`, GET statique, zéro quota)
- [x] R4.3 Sélecteur par score (`free/selector.py`) intégré au pool
- [x] R4.4 Décision de rotation proactive (`should_rotate`) + cut-over par
      bascule de station (le cut-over « même station » n'est pas possible
      pendant un stop/start gluetun ; le pool bascule sur une autre station)
- [x] R4.5 Short-list : un seul host enregistré (plus tout le top-5) ;
      charge réelle mémorisée
- [x] R4.6 Tests synthétiques de sélection (100/300/600 ms, 429, starvation)
- [x] R4.7 Mesure TTFB assainie : les réponses non-streaming (durée totale)
      n'alimentent plus l'EWMA de latence (`latency=False`) ; sélecteur
      cold-start déterministe (pas d'exploration avant la première probe)

## Phase 5 — Maintenabilité

- [x] R5.1 Sélecteur unique
- [x] R5.2 Breaker superviseur réellement alimenté et consulté
- [x] R5.3 Purge état latence/qualité au downscale
- [x] R5.4 Config morte : `warmup_excluded_requests` et
      `prewarm_after_rotate` désormais consommés
- [x] R5.5 Extraction modules (`ops/health`, `ops/fleet`, `ops/jobs`,
      `ops/updater`, `free/quality`, `free/probe`, `free/selector`)
- [x] R5.6 Tests : nouvelle suite roadmap + suite complète verte (`-k "not docker"`)
- [x] R5.7 Docs/runbook : audit + roadmap + ce suivi + README mis à jour

## Phase 6 — Validation

- [x] R6.1 Validation synthétique (sélecteur, flapping, stacks, flotte)
- [x] R6.2 Validation live après redémarrage du proxy :
      - 6/6 `healthy` et `routable`, `boot_error: null`, `stale: false` ;
      - `quality.enabled: true`, entrées qualité présentes pour les 6 stations ;
      - latence API p50 14 ms / p95 54 ms (10 appels) ; watcher 47 ms sur
        l'échantillon final (15–63 ms en régime) ;
      - s6 guérie (reset ancien), s2 restaurée en WG après boucle AUTH ;
      - observation 24 h instrumentée démarrée le **2026-10-26T13:49Z**
        (`scripts/station_slo_watch.py`, PID 38476,
        `logs/station_slo_final.jsonl`, échantillon 60 s) avec qualité/TTFB,
        succès, 429, rotations et fallback payant.
- [x] R6.3 Post-mortem initial + cibles SLO publiés :
      → [POST_MORTEM_STATIONS_SLO_2026-10-26.md](POST_MORTEM_STATIONS_SLO_2026-10-26.md)
- [ ] R6.3b Rapport SLO final sur la fenêtre 24 h
      (échéance **2026-10-27T13:50Z**) — **génération automatique planifiée** :
      finaliseur PID **32100** (`scripts/station_slo_finalize.py`) →
      [POST_MORTEM_SLO_FINAL_2026-10-27.md](POST_MORTEM_SLO_FINAL_2026-10-27.md).
      Vérification manuelle possible :
      `python scripts\station_slo_report.py --in logs\station_slo_final.jsonl --since 2026-10-26T13:49:28Z --until 2026-10-27T13:50:28Z --markdown`.
      Le rapport inclut flotte + qualité/TTFB + succès + 429 + rotations +
      fallback payant. Puis compléter la section 7 du post-mortem.

> Il ne reste que la clôture temporelle du SLO 24 h : la collecte tourne, le
> rapport est automatisé. Tout le code et la validation live immédiate sont
> terminés ; ne relancer aucun flip global de stack.
