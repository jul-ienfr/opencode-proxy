# Audit présélection IP, quota et tests — 2026-11-02

Statut : audit exécuté, correctifs appliqués  
Périmètre : `free/ip_registry`, `scripts/ip_preselection.py`, schéma
`requests`, attribution hostname, quota ledger, suite de tests complète.

## 1. Résumé exécutif

L’audit a trouvé **une lacune fonctionnelle réelle** et **sept problèmes de
tests** (fixtures obsolètes, dépendances d’ordre, flottants). Tout est corrigé :

| Sévérité | Trouvaille | État |
|---|---|---|
| **P1** | `requests.server_host` n’existait pas en base, alors que la phase 0 de présélection le documentait comme prérequis | corrigé : schéma + migration + INSERT + attribution |
| **P1** | `scripts/ip_preselection.py` joignait l’IP d’**entrée** de l’API à l’egress mesuré : 0/50 en live | corrigé : jointure `hostname` d’abord, IP en legacy seulement |
| P2 | 8 clés `ip_registry.*` mortes dans `config.yaml` | corrigées : 6 branchées (score, timeout, rétention), 2 supprimées (`max_test_concurrency`, `preselect.max_tests_per_hour`) |
| P2 | Tests `forced_pool` : `MagicMock._pinned_hostname` truthy → rotation sautée | fixture corrigée |
| P2 | Tests disconnect-retry : deux stations avec la même IP → invariant IP distincte | fixture corrigée |
| P2 | Tests préflight : `_vpn_manager` absent → mode `direct` → préflight court-circuité | fixture corrigée |
| P2 | Tests direct free : chemins restés sur httpx alors que le runtime est passé à curl_cffi | tests mis à jour |
| P3 | Deux tests dépendants de l’ordre (UA vaultée, flottant `7200.000000000004`) | isolation + tolérance |

Suite complète : **4452 tests passés, 5 skipped, 1 xpassed, 0 échec**
(`-k "not docker"`, 34 tests Docker désélectionnés). Audit config :
**406 clés, 0 dead, 0 missing**.

## 2. Méthode

Commandes exécutées :

```bash
python -m pytest tests -q -k "not docker"
python scripts/config_audit.py --json
python scripts/ip_preselection.py --hours 72 --top 10
```

Vérifications ciblées :

```bash
python -m pytest tests -k "ip_registry or nordvpn_sources or quota" -q
python -m pytest tests/test_config_audit.py tests/test_phase0_contracts.py -q
sqlite3 logs/requests.db "PRAGMA table_info(requests);"
```

## 3. Présélection IP

### 3.1 Ce qui fonctionne

- `ip_rotation.quality_preselection` est actif : tri A/B/C par tok/s mesuré et
  `load` API, plafonds `tier_b_quota`/`tier_c_quota` appliqués dans
  `_rotation_threshold`.
- `quality.host_quality()` et `_record_station_quality()` attribuent déjà la
  qualité au hostname quand il est connu.
- `quality_rotation` est implémenté avec les garde-fous documentés :
  `min_requests`, `min_samples`, plafond horaire, cooldown 600 s par station,
  pause si la flotte est globalement lente, indépendance vis-à-vis du moteur
  latence.
- `post_rotation_probe` reste volontairement inactif (phase 4 non branchée).

### 3.2 Lacune corrigée : `server_host`

La documentation affirmait la phase 0 faite, mais `PRAGMA table_info(requests)`
ne contenait **pas** `server_host`. Conséquence : la qualité mesurée ne pouvait
pas être reliée au hostname rebindable ; `ip_preselection.py` retombait sur
l’IP d’entrée de l’API, qui n’est **jamais** l’egress (0/50 en live).

Correctif :

1. `observability/db.py` : colonne `server_host TEXT`, migration idempotente,
   `_INSERT_REQUESTS_SQL` à 36 colonnes.
2. `opencode.py` : `_save_request` / `_save_and_log_request` acceptent
   `server_host`, inféré depuis le manager de la station ou passé explicitement
   par le chemin stream.
3. `scripts/ip_preselection.py` : qualité par `server_host` d’abord ; l’IP
   d’entrée n’est plus utilisée que comme repli explicitement étiqueté
   « legacy, non fiable ».

Limite honnête : les lignes historiques n’ont pas de hostname et ne peuvent pas
être reconstruites de façon fiable. Le rapport live du 2026-11-02 montre
**0 hostname sur 811 IP et 11 059 requêtes** : l’attribution démarrera avec le
prochain redémarrage du proxy.

### 3.3 Preuves live reproductibles

Rapport initial (2026-11-02, première exécution) :

```text
API: 50 serveurs online, load min=9 median=11 max=19
DB 72h: 811 IP mesurées, 0 hostnames, 11 059 requêtes
Appariement hostname->DB: 0/50 serveurs
Appariement IP API->DB (legacy, non fiable): 0/50
Top IP publiques: 152,5 / 116,8 / 113,2 tok/s
```

Reproduction machine du 2026-11-02T09:10Z (fenêtre glissante, donc les
compteurs bougent légèrement) :

| Fenêtre | IP mesurées | hostnames | requêtes | appariement hostname | legacy IP | tiers A/B/C |
|---|---:|---:|---:|---:|---:|---|
| 24 h | 26 | 0 | 1 974 | 0/50 | 0/50 | 0 / 50 / 0 |
| 72 h | 795 | 0 | 10 783 | 0/50 | 0/50 | 0 / 50 / 0 |
| 168 h | 1 269 | 0 | 46 883 | 0/50 | 0/50 | 0 / 50 / 0 |

Fichiers de preuve (JSON, UTF-8, avec `generated_at`, seuils, notes) :

- [`evidence/ip-preselection-2026-11-02-24h.json`](evidence/ip-preselection-2026-11-02-24h.json)
- [`evidence/ip-preselection-2026-11-02-72h.json`](evidence/ip-preselection-2026-11-02-72h.json)
- [`evidence/ip-preselection-2026-11-02-168h.json`](evidence/ip-preselection-2026-11-02-168h.json)

Après redémarrage (migration `server_host` + rebuild qualité) :

| Fenêtre post-restart | IP | hostnames | requêtes | appariement hostname |
|---|---:|---:|---:|---:|
| 1 h | 3 | 2 | 19 | 1/50 |
| 24 h | 26 | 2 | 1 974 | 1/50 |

Preuves : [`evidence/ip-preselection-2026-11-02-post-restart.json`](evidence/ip-preselection-2026-11-02-post-restart.json)
et [`...-post-restart-24h.json`](evidence/ip-preselection-2026-11-02-post-restart-24h.json).
En base, au dernier contrôle (2026-11-02T09:59Z) : **13 lignes attribuées,
3 hostnames distincts** (`al109`, `al138`, `al169`). La couverture runtime est
exposée par `GET /api/free/ip-registry/status` (`host_attribution`) :
**1/1 requête free VPN attribuée, couverture 1,0** juste après redémarrage,
et `quality` contient 8 entrées dont 3 hostnames reconstruits.

Commande de reproduction :

```bash
python scripts/ip_preselection.py --hours 72 --top 15 \
  --output docs/evidence/ip-preselection-2026-11-02-72h.json
```

Le classement A/B/C reste dominé par le `load` tant que `server_host` est vide :
c'est attendu, pas une régression.

### 3.4 Annotations explicites

- **Attribution hostname** : avant le redémarrage, `0 hostname` ; après
  migration + rebuild, **6 lignes attribuées / 3 hostnames distincts**
  (`al109`, `al138`, `al169`) et une couverture runtime exposée par
  `host_attribution`. Les lignes historiques restent non reconstructibles :
  l’IP d’entrée API n’est pas l’egress, et on ne les joint pas.
- **Registre runtime** : `ip_registry.enabled=true`, `allocator.mode=enforce`.
  Le superviseur répare toutes les stations (2 chaînes en parallèle, budget de
  rebind global) et persiste les winners dans `ip_state`/`assignments`.
- **Nordcache** : `nordcache_packed` est déclaré dans le manifeste mais n’a pas
  de décodeur. Si ce fallback est nécessaire, la résolution rapporte
  explicitement `no-adapter:nordcache_packed` — aucun fallback silencieux.

## 4. Quota

- Le ledger persistant `(IP, jour, bucket)` est la source de vérité ; le
  `Retry-After` est honoré par IP, avec repli `next_utc_midnight` seulement
  quand il manque.
- `RuntimeResult` / `record_runtime` conservent la règle
  `reset_at = max(existant, observation + Retry-After)` : pas de régression.
- Les tests quota (`test_quota_ledger`, `test_quota_bucket_pool`,
  `test_quota_audit_p0`, `test_quota_parse_p0`, `test_free_quota_vs_transient`,
  `test_trial_dead_quota`) passent.
- Aucun chemin de rotation n’est déclenché sur un 429 transitoire
  (`quota=False`), et une station pinnée ne s’auto-rotationne plus : le
  fresh-chain possède le rebind.

## 5. Tests

### 5.1 Causes racines des échecs de la baseline

1. **Guard `_pinned_hostname`** ajouté le 2026-11-01 : les doubles `MagicMock`
   exposent un attribut truthy, la rotation quota était sautée. Les tests
   devaient modéliser une station non pinnée (`_pinned_hostname = None`).
2. **Invariant IP distincte** : deux `_StubStation` partageaient `1.1.1.1` ;
   la station 2 était refusée par le pool. IP unique par station dans la fixture.
3. **`_free_proxy_mode()`** retourne `direct` sans `_vpn_manager` : les tests
   préflight/VPN doivent poser un manager ou forcer le mode.
4. **Chemin direct free** déplacé de httpx vers curl_cffi (2026-11-01) : les
   tests qui espionnaient `_client.stream()` testaient un chemin supprimé.
5. **UA par IP via vault** : un test d’égalité UA héritait d’une IP directe
   cachée par un test précédent ; isolation explicite.
6. **Flottant** : `time.monotonic() + 7200` relu peut dépasser 7200 de quelques
   nanosecondes ; tolérance de 1 s.
7. **Layout DB figé** : trois tests (`test_db_offload`, `test_boot_phase8_db`,
   `test_phase0_contracts`) figeaient 35 colonnes ; le passage à
   `server_host` porte le tuple à 36 et les DDL de test ont été alignées.

### 5.2 Ce qui n’est pas couvert

- Tests Docker live (`-k "not docker"`, 34 tests) : non exécutés dans cet audit.
- Canary allocateur réel : non relancé (aucun changement de binder).
- `nordcache` : adaptateur déclaré mais non implémenté (format packé non
  versionné) ; la résolution rapporte `no-adapter:nordcache_packed`.

## 6. Registre multi-sources (implémenté dans le même passage)

- Schéma SQLite v2 : `servers.source_id`, `servers.stale`,
  `servers.catalog_fetched_at`, journal `catalog_snapshots`, index
  `idx_servers_source`.
- Package `free/ip_registry/sources/` : `OfficialApiSource`,
  `JsonCatalogSource`, manifeste YAML validé, `SourceRegistry` avec hiérarchie
  primaire → fallbacks → complétude.
- Règle : l’API gagne sur un doublon ; une source de complétude ne remplace
  jamais le vainqueur ; un fallback marque la résolution **et chaque ligne**
  `stale=True` ; un snapshot partiel ne désactive jamais de serveurs.
- Tests dédiés : `tests/test_ip_registry_sources.py`, migration v1 → v2 dans
  `tests/test_ip_registry_sqlite.py`, provenance dans
  `tests/test_ip_registry_sqlite_registry.py`.

Validation live du 2026-11-02 : `ip_registry_observe.py` a importé
**9 948 serveurs** (7 229 API frais + 2 719 embarqués `stale=1`), journalisé
un snapshot `nordvpn_api_servers` et migré `logs/ip_registry.db` en v2 sans
perte.

## 7. Risques résiduels

| Risque | Impact | Mitigation |
|---|---|---|
| `server_host` vide sur l’historique | qualité non attribuable rétroactivement | attendre les nouvelles lignes ; ne pas joindre l’IP d’entrée |
| `ip_registry` activé | réparations parallèles, charge Docker/AUTH | 2 chaînes max + budget rebind 4/min + backoff |
| `nordcache` non décodé | fallback compact indisponible | implémenter le décodeur sur fixture avant activation |
| Egress NAT non rebindable | un 200 n’est pas reproductible | marquer `rebindable=false`, garder sticky |
| Tests Docker exclus | régressions Docker non vues | job CI séparé avec daemon |

## 8. Recommandations

1. Redémarrer le proxy pour appliquer la migration `server_host`, puis vérifier
   `SELECT COUNT(*) FROM requests WHERE server_host IS NOT NULL`.
2. Laisser `quality_rotation.enabled=false` jusqu’à disposer de 3 mesures par
   hostname ; activer ensuite avec `max_per_hour=6`.
3. Activer `ip_registry` en `observe` et comparer `ip_state` à la sélection
   actuelle pendant 24 h avant l’allocateur.
4. Ajouter le canary `nordcache` au nightly quand le décodeur sera prêt.

Plan de correction détaillé (P0 → P5, KPI et critères de sortie) :
[`PLAN_CORRECTION_2026-11-02.md`](PLAN_CORRECTION_2026-11-02.md).
