# Data Model — Audit global 2026-11-20

**Feature** : `024-audit-optimisation-globale`
État mesuré au 2026-11-20 sur `logs/registry.db` (159,4 Mo, 52 tables,
96 index, `schema_migrations` max = 3) et `logs/requests.db` (159,2 Mo).

---

## 1. `registry.db` — inventaire et problèmes

### 1.1 Propriété du schéma : six déclarants, aucune autorité

| Déclarant | Tables créées | Dans `schema.sql` ? |
|---|---|---|
| `rotation/infrastructure/db/schema.sql` (538 l.) | **30** | — |
| `free/qualification/store.py:29-255` | 12 | 12/12 **dupliquées** |
| `free/egress_store.py:30-157` | 9 | 0 |
| `free/catalog_claims.py:41-68` | 3 | 0 |
| `rotation/infrastructure/db/claims.py:13-39` | 3 | copie **octet pour octet** |
| `free/scan_audit.py:44-79` | 2 | 2/2 dupliquées |
| `free/ip_registry/registry_db.py:29-41` | 2 | 0 |
| `free/ip_state.py:49,74` | 2 | 0 |
| `free/client_reserve.py:74` + `reserve_settings.py:42` | 2 | 0 |
| `free/control_plane/telemetry.py:19,36` | 2 | 0 (autre fichier) |
| `free/probe_budget.py:23` | 1 | 0 |
| `free/warp_rotation.py:71` | 1 | 0 |
| `free/repair_queue.py` | 1 | 0 |
| `free/ip_registry/sqlite.py:17-130` | 6 | 0 (autre fichier) |
| `observability/db.py` | 6 | 0 |

**Mesure** : 52 tables vivantes, **30** dans `schema.sql`, **22 hors schéma**.
`only-in-schema: []` → aucune table déclarée n'est absente, mais la
convergence est un **effet de bord**, pas une migration.

### 1.2 Tables déclarées plusieurs fois (conflits silencieux)

| Table | Déclarants | Divergence |
|---|---|---|
| `ip_state` | `free/ip_state.py:49`, `free/ip_registry/sqlite.py:58` | **colonnes incompatibles** : `(provider, ip, bucket_key, wire_fp, burned_at…)` vs `(egress_ip PK, hostname, country, score, latency_ms…)` |
| `station_state` | `free/qualification/store.py`, `schema.sql` | `CHECK (state IN (8 valeurs))` + `NOT NULL` **présents** dans `schema.sql`, **absents** dans `store.py` |
| `server_transports` | `free/qualification/store.py`, `schema.sql` | DDL divergentes |
| `incidents` | `free/qualification/store.py`, `schema.sql`, `scripts/station_slo_watch.py` | **3 déclarants** |
| `repair_jobs` | `free/qualification/store.py`, `schema.sql` | DDL divergentes |

**Le `IF NOT EXISTS` rend chaque conflit silencieux** : le premier module
exécuté crée la table, les autres sont des no-ops. Le schéma effectif dépend
de **l'ordre d'initialisation**.

### 1.3 Intégrité référentielle : déclarée mais non appliquée

```
PRAGMA foreign_key_check → 15 179 violations
  toutes : server_transports.hostname → servers.hostname
```

**5 des 6 connexions écrivantes omettent `PRAGMA foreign_keys=ON`** — le
pragma est **par connexion**, non persistant.

Cause : `store.upsert_server_transports` insère des hostnames sans ligne
`servers` correspondante.

### 1.4 Volume et rétention

| Table | Lignes | Purgée par `retention.py` ? |
|---|---|---|
| `egress_observations` | **326 812** | ❌ non |
| `egress_threat_intel` | 174 163 | ❌ non |
| `scan_attempts` | 61 027 | ❌ non |
| `qualification_attempts` | 41 166 | ❌ non (purge codée, jamais appelée) |
| `incidents` | 35 249 | ✅ oui |
| `egress_options` | 31 873 | ❌ non |
| `qualification_epochs` | **21 230** | ❌ non |
| `server_transports` | 20 945 | ❌ non |
| `catalog` / `claims` | 9 788 | ❌ non |
| `quota_buckets` | 8 519 | ❌ **non** |
| `observations` | 8 657 | ✅ oui |
| `rotation_jobs` | 3 808 | ❌ non |

**La purge couvre 5 tables, dont 3 vides.** Les 7 tables les plus grosses
(95 % des lignes) ne sont **jamais** purgées.

### 1.5 Compteurs faux

| Table | Mesure | Réalité |
|---|---|---|
| `qualification_epochs` | `SUM(tested)` = **847 383** | 41 166 tentatives réelles → **×20,6** |
| `qualification_epochs` | 6 769 epochs sur le même `total=7118` | **même ensemble re-mesuré** |
| `warm_pool` | 742 lignes | **2 vivantes** ; 683 `transport` vides, 742 `score_ip=0` |
| `ip_state` | 283 lignes `proven`/`unproven` | **0** `spent`/`burned` malgré 7 570 buckets en 429 |
| `egress_options` public | 26 474 `dead` avec `egress_ip=''` | **86,4 %** morts sans preuve de test |

### 1.5.1 Deux bugs de type SQLite (aucune erreur levée)

SQLite est **typé dynamiquement** : une comparaison entre types incompatibles
ne lève **rien**, elle retourne simplement faux. Deux cas mesurés :

**a) `egress_ip_in_use` — comparaison entier/TEXT**
(`free/qualification/store.py:1141`)

```python
WHERE egress_ip=? AND status='usable' AND station<>?
(ip, str(int(station)))          # lie '1', '2'…
```

Or la colonne `station` est de type **TEXT** et contient `'warp-1'`,
`'station-2'`. En SQLite, `'warp-1' <> '1'` est **toujours vrai** → la
fonction retourne `True` pour toute IP déjà présente, **y compris détenue par
la même station**. Le garde d'unicité du principe V est **inopérant**.

**b) `time.monotonic()` persisté puis comparé** (V-22)

Les valeurs sont écrites et relues sans unité ni référence d'époque. Après un
reboot, `762 626` (ancien boot) est comparé à `269 939` (boot courant) —
aucune erreur, mais un park de 30 min devient « dans 30 min + uptime
précédent ».

**Règle d'invariant** : les colonnes comparées à des entiers sont déclarées
`INTEGER` avec un `CHECK` de type ; les durées sont persistées en **secondes
restantes**, jamais en valeur d'horloge monotone.

### 1.5.2 Config morte — un gate permissif sur 62 % des clés

`scripts/config_audit.py:266` déclare une clé « vivante » dès que son **nom
de feuille** apparaît entre guillemets n'importe où :

```python
if re.search(rf"[\"']{re.escape(feuille)}[\"']", source):
    continue        # ← "vivante" par coïncidence de nom
```

Mesure : **392 des 625 clés (62 %)** ne sont jamais lues par un `yaml_get`
littéral. La chaîne `"enabled"` apparaît **1 110 fois** dans le code — donc
toute clé nommée `enabled` est déclarée vivante.

**`STRICT_SECTIONS = {"ip_registry"}`** ne couvre qu'**une** section sur 45.

Conséquence : la gate CI bloquante `config_audit --strict` (vert) **ne prouve
rien sur 62 % des clés**, et les 6 clés legacy auto-déclarées à retirer
depuis le 2026-09-08 sont dans une liste « informative » qui **ne peut pas
échouer**.

### 1.6 Objectifs cibles (spec 024)

| Métrique | Actuel | Cible |
|---|---|---|
| Taille `registry.db` | 159,4 Mo | **< 50 Mo** |
| WAL | 5,25 Mo | < 4 Mo |
| Tables hors schéma | 22 | **0** |
| Tables à déclarants multiples | 5 | **0** |
| Violations FK | 15 179 | **0** |
| Tables non purgées (parmi les 10 plus grosses) | 7 | **0** |
| `SUM(tested)` / tentatives réelles | ×20,6 | **≈ 1,0** |
| `warm_pool` vivantes | 2 | **≥ 6** |

---

## 2. `requests.db` — inventaire

| Mesure | Valeur | Problème |
|---|---|---|
| Taille | 159,2 Mo | — |
| `page_count` | 40 766 | — |
| **`freelist_count`** | **25 499 (99,6 Mo)** | **62,6 % du fichier est de l'espace mort** |
| `auto_vacuum` | 0 | jamais de récupération |
| Lignes | 42 784 | — |
| > 7 jours | 25 401 | rétention non appliquée aux lignes |
| `free_status` rempli | **26,0 %** | télémétrie lacunaire |
| `ttfb_ms` / `stop_reason` remplis | 38,4 % | idem |
| `winner_station` | **0 %** (jamais lu ni écrit) | colonne morte |
| `hedged` | 0 sur 42 787 | feature jamais active |

### 2.1 Colonnes de télémétrie — état de remplissage

| Colonne | Remplie | Verdict |
|---|---|---|
| `aborted` | 100 % | ✅ |
| `final_status` | 77,6 % | ⚠️ |
| `station` | 71,1 % | ⚠️ |
| `attempt_no` | 67,1 % | ⚠️ |
| `ttfb_ms`, `stop_reason` | 38,4 % | ❌ |
| `free_status` | **26,0 %** | ❌ (P1-07 de l'audit 019) |
| `winner_station` | **0 %** | ❌ mort |

**Objectifs** : `free_status` à 100 %, `freelist_count` < 5 % de
`page_count`, taille < 500 Mo.

---

## 3. Fichiers d'état — cinq sources pour six stations

| Surface | Emplacement | État |
|---|---|---|
| `station_state` | `registry.db` | écrit par `qualification/watchdog.py:161`, **jamais lu par l'API** |
| `pins` | `registry.db` | 4 lignes (stations 1,3,5,6), `egress_ip` **vide**, `source='env-migration'` |
| `.env` | racine | **6** `SERVER_HOSTNAMES_STATION` — les stations 2 et 4 n'existent que là |
| `vpn_state{1..6}.json` | `logs/` | 5 fichiers figés au 18/11, 1 actif |
| `free_pool.json` | `logs/` | **1,9 Mo**, horodatages d'un autre boot, 58 stations fantômes |

### 3.1 `free_pool.json` — le cas le plus grave

| Mesure | Valeur |
|---|---|
| Taille | 1 948 129 octets |
| Clés | `per`, `health`, `quota` |
| Stations dans `health` | **64** (58 négatives `-86`…`-1`) |
| `last_ok_at` des 6 vraies stations | **`762 626.406`** (identique ×6) |
| `time.monotonic()` au moment de la mesure | **269 939** |
| Ratio | **2,8×** l'horloge courante |
| `state` déclaré | `usable` ×6 |
| `state` observé par l'API | 5/6 `repairing`, 1 `usable` |

**Trois défauts dans un seul fichier** :

1. **Périmé** — horodatages d'un boot à 8,8 jours d'uptime.
2. **Écrasé** — les 6 stations portent la valeur **identique au millième**,
   donc non mesurée par station.
3. **Contradictoire** — déclare 6 `usable`, l'API en voit 1.

### 3.2 Objectifs

| Métrique | Actuel | Cible |
|---|---|---|
| Surfaces d'état par station | **5** | **1** (`registry.db`) |
| Stations négatives | 58 | **0** |
| Valeurs `monotonic()` persistées | oui | **0** (durées restantes uniquement) |
| `vpn_state*.json` | 6 fichiers | **0** |
| Divergence API / `station_state` | constatée | **0** sur 24 h |

---

## 4. Sauvegardes

| Mesure | Valeur |
|---|---|
| `logs/backups/` | **13 Go**, 188 fichiers |
| Plus gros | `requests-20261116T174327Z.db` = **998 Mo** |
| `registry-*.db` | ~140 Mo × 2/jour depuis le 2026-11-05 |
| Rétention | **aucune** |
| `integrity_check` sur la copie | **non** (c'est la source qui est vérifiée) |

**Objectif** : rétention de 7 copies, `integrity_check` sur la copie produite,
`logs/backups/` < 1 Go.

---

## 5. Artefacts non versionnés (bloquants)

| Artefact | Requis par | Versionné ? |
|---|---|---|
| `logs/compose-control-plane-{1..6}.yml` | l'image du fork, le port 8001, les variables `CONTROL_*` | ❌ gitignoré |
| `free/official_payload.json.bak` | `free/ip_registry/adapters/probe.py:88-90` | ❌ gitignoré |
| `free/captures/1.19.5.json` | `free/profiles/verified_profiles.json:26` | ❌ supprimé |

**Objectif** : tout artefact nécessaire au runtime est versionné ou généré par
un script versionné. Un `git clone` neuf produit une flotte fonctionnelle.

---

## 6. Règles d'invariant (à faire respecter par un test)

1. **Une table = un déclarant.** Aucun `CREATE TABLE IF NOT EXISTS` en dehors
   de `schema.sql` (ou d'un module de migration unique).
2. **`foreign_keys=ON` sur toute connexion écrivante.** `PRAGMA
   foreign_key_check` doit retourner 0.
3. **Aucune valeur `time.monotonic()` persistée.** On persiste des durées
   restantes, réappliquées au boot.
4. **Un compteur mesure un fait, pas une répétition.** `SUM(tested)` doit
   rester de l'ordre des tentatives réelles.
5. **Un `dead` exige une preuve de test.** Un candidat jamais sondé n'est pas
   `dead`.
6. **Une IP sans `reset_at` n'est pas `proven`.** 102 cas aujourd'hui.
7. **Toute table volumineuse est couverte par la rétention.**
8. **Toute surface d'état a un test de non-divergence.**
