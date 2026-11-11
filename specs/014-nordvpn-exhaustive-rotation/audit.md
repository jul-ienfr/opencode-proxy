# Audit — control plane NordVPN, couverture et rotation

**Date** : 2026-11-09
**Périmètre** : jambe free, catalogue NordVPN, six stations Gluetun, lifecycle,
watchdog, découverte, réparation, claims, scanner `fresh_chain`, catalogue
d'egress, ledger de quota, ordonnancement.
**Méthode** : lecture du code et de la configuration (`ops/lifecycle.py`,
`ops/reconciler.py`, `ops/supervisor.py`, `free/station_health.py`,
`free/repair_queue.py`, `free/rotation_planner.py`, `scripts/fresh_chain*`),
requêtes sur `logs/registry.db` (`catalog`, `servers`, `egress_options`,
`observations`, `scan_runs`, `scan_attempts`, `claims`, `egress_ips`,
`quota_buckets`, `station_state`, `incidents`, `repair_jobs`), inspection de
`logs/nordvpn_api.json`, `logs/gluetun_nordvpn_servers.json`,
`logs/gluetun_nordvpn_embedded.json` et `logs/station_slo.jsonl`.
**Règle** : chaque conclusion sur une IP ou un serveur vient d'un test de cette
IP ou de ce serveur. Le quota free est **par IP**, pas par compte ; les 429
observés qualifient les IP testées, jamais l'ensemble du pool.

---

## 1. Résumé exécutif

Le système ne manque pas de serveurs. Il ne sait pas **qualifier** le catalogue
qu'il possède, et il ne sait pas **continuer** après avoir trouvé une IP qui
répond 200.

À l'heure de l'audit :

- Le catalogue amont contient **7 070 serveurs en ligne**, dont **6 932
  compatibles WireGuard**, dans **150 pays** ; la charge médiane est de 10 %,
  le maximum de 84 %.
- Le catalogue d'egress ne contient que **6 options NordVPN**, toutes en
  `quota`. Il est alimenté par les **six stations actives** (`/api/vpn/fleet`),
  pas par le catalogue de 7 070 serveurs.
- Le scanner a bien touché **6 817 hostnames distincts** sur 26 045 tentatives,
  mais **2 008 de ces hostnames ne sont plus dans l'API** : le catalogue live
  n'a été tenté qu'à **4 809 / 7 070 (68,0 %)** et **2 261 serveurs en ligne
  n'ont jamais été tentés**. Seuls **74 tentatives (0,28 %) ont atteint un
  200**, **3 667 (14,1 %) un 429**, et **22 150 (85,0 %) se sont arrêtées en
  `no_egress`**. « Tenté » n'est pas « testé ».
- Les **4 hostnames** qui portent les 94 observations 200 (`al41`, `al74`,
  `al46`, `it372`) **ne sont pas dans l'API courante**. Le catalogue live de
  7 070 serveurs n'a **aucun 200 observé** ; les seuls succès sont des hôtes
  legacy.
- Les observations ne couvrent que **1 373 serveurs live (19,4 %)** ; **aucun
  serveur live n'a de 200**. Les **94 observations 200** portent sur **4
  hostnames legacy**.
- La couche de claims a parqué **9 770 des 9 788 hostnames** : 5 407 en
  `country_backoff`, 4 242 en `no_egress`, 121 en `bind_error` ; les 18 autres
  sont 9 `429`, 6 `claimed` et 3 `abandoned`. Presque tout le catalogue a été
  **écarté par une heuristique de pays ou par un filtre de candidat**, sans
  test de quota.
- Le scanner est un **réparateur** : 285 runs, tous `reason='repair'`, dont
  **75 `recovered`** (arrêt au premier 200), **70 `superseded`**, **107
  `interrupted`** (37,5 %), **33 `max_candidates`**. Il ne balaie pas le
  catalogue ; il cherche une IP utilisable et s'arrête.
- L'ordonnancement réel du scanner est
  `ORDER BY fail_count, catalog.priority, hostname` dans
  `free/catalog_claims.py:150-154`. Le score composite construit par
  `scripts/fresh_chain.py:1086-1121` sert à filtrer et à choisir les transports,
  **pas à choisir l'ordre des hostnames**. Le « meilleur d'abord » n'est pas
  appliqué.
- Trois scores coexistent et ne mesurent pas la même chose :
  `egress_catalog.score_option` (40/25/20/10/5),
  `fresh_chain` (mêmes poids mais données différentes), et
  `ip_quota.IpQuotaLease.score_key` (headroom × succès, puis latence). Aucun
  n'utilise le ping mesuré depuis l'opérateur.
- Le watchdog et la découverte sont des **îlots** : `station_state` est vide,
  `repair_jobs`/`repair_requests` sont vides, `free/repair_queue.py` vit dans
  `logs/repair_queue.db`, les 162 incidents sont tous `scope='fleet'` (157
  `client_usable_low`, 3 `sweep_stalled`, 2 `warm_pool_low`), aucun n'est
  `scope='station'`, et
  `scripts/ip_registry_discover.py` est une découverte ciblée, pas un
  scheduler. `ops/supervisor.py` annonce un watchdog non branché.
- Le chemin catalogue complet n'est pas branché : `scripts/ip_registry_observe.py`
  écrit dans `logs/ip_registry.db` (fichier de **0 octet**), tandis que le
  runtime lit `ip_registry.registry_db_path = logs/registry.db`
  (`free/egress_runtime.py:331-335`). `server_transports` est vide,
  `sweep_cursor` est vide, `servers` ne contient que **2 066** lignes (1 509
  `fresh-chain`, 557 `legacy-ip-registry`, toutes sans `status`), et
  `ip_registry.allocator.mode` vaut `off`
  (`config.yaml:1208-1209`).

**Verdict** : le goulot n'est ni le nombre d'IP ni le quota global. Le goulot
est un pipeline de qualification incomplet, un ordre de claim statique, des
heuristiques de pays qui parquent le catalogue, et un scanner qui s'arrête au
premier succès. La spec 014 doit transformer ce réparateur en **balayage
exhaustif, ordonné par potentiel, qui consomme le quota de chaque IP et passe
automatiquement à la suivante sur 429**.

---

## 2. Ce que le système fait aujourd'hui

### 2.1 Trois chemins qui ne se parlent pas

| Chemin | Entrée | Sélection | Sortie | Limite |
|---|---|---|---|---|
| Catalogue d'egress | `/api/vpn/fleet` (6 stations) | `select_option` sur `egress_options` | options `usable` | 6 stations, 10 options |
| Scanner `fresh_chain` | `catalog` (9 788 hostnames) + API | `claim_next` : `fail_count`, `priority`, `hostname` | un 200, puis arrêt | 500 candidats par slice |
| Allocateur `ip_registry` | catalogue API | `eligibility` + `scoring` | claims/leases | `mode: off` |

Les trois chemins partagent `registry.db`, mais pas la même unité de travail :
le catalogue d'egress raisonne en **option = IP observée**, le scanner en
**hostname**, l'allocateur en **serveur → transport → IP**. Aucun des trois ne
maintient une file unique « hostname à qualifier → IP observée → option de
rotation ».

### 2.2 Le catalogue d'egress ne voit que six stations

`free/egress_runtime.py:275-300` construit la liste NordVPN à partir de
`GET /api/vpn/fleet` : six lignes, avec `station`, `server`, `egress_ip`,
`status`. `free/egress_runtime.py:406-417` passe cette liste à
`NordVpnEgressAdapter` avec `max_active_stations=6`.

L'adaptateur applique ensuite deux plafonds :

- `free/egress_adapters/nordvpn.py:56` :
  `rows[: max(1, int(max_candidates or policy.max_options or 10))]` ;
- `free/egress_adapters/nordvpn.py:123-142` : au-delà de
  `max_active_stations`, l'option est seulement observée, aucune session n'est
  ouverte.

La configuration confirme les plafonds (`config.yaml:1154-1163`) :

```yaml
nordvpn:
  max_active: 6
  max_options: 10
  max_active_stations: 6
```

Conséquence mesurée : `egress_options` contient **7 026** lignes, dont
**6 NordVPN**, toutes `quota` :

```text
('nordvpn', 'quota', 6)
('public',  'dead', 6367)
('public',  'quota', 536)
('public',  'reachable', 52)
('public',  'usable', 7)
('warp',    'dead', 45)
('warp',    'quota', 7)
('warp',    'usable', 6)
```

Le catalogue d'egress n'a donc aucune ligne `candidate` NordVPN à qualifier. Il
ne peut pas ordonner ce qu'il ne connaît pas.

### 2.3 Le scanner est un réparateur, pas un balayeur

`scripts/fresh_chain_supervisor.py:326-350` lance `fresh_chain.py --claims`
avec un slice de `max_candidates_per_chain` (500 dans `config.yaml:1216`). Le
scanner :

1. claim un hostname via `free/catalog_claims.py:133-174` ;
2. vérifie qu'il est dans `by_host` (candidats du run) ;
3. si absent, enregistre `no_egress` avec `error='filtered-candidate'` et
   **saute sans tester** (`scripts/fresh_chain.py:1848-1853`) ;
4. si présent, bind, probe, et au premier 200 :
   `_close_run("recovered", host, ip); return 0`
   (`scripts/fresh_chain.py:2142-2143`).

Le run est donc terminé au premier succès. Les 75 runs `recovered` ont une
durée moyenne de 850 s ; les 107 runs `interrupted` ont une durée moyenne de
32 037 s ; la durée moyenne globale est de 14 911 s, la médiane de 3 853 s, le
maximum de 96 794 s. Un run peut durer 27 heures et être interrompu sans avoir
fini son slice. Chaque run construit en moyenne **9 786 candidats**
(`scan_runs.candidates`), mais n'effectue que **91 tentatives** (26 045 / 285) :
la liste est un catalogue de travail, pas un plan de balayage.

Les 285 runs ont tous `reason='repair'` ; aucun run n'a `reason='sweep'` ou
`reason='discovery'`. `rotation.application.sweep.SweepCursor` existe, mais
`sweep_cursor` est vide : aucun curseur de balayage n'est utilisé.

### 2.4 La couche de claims parque le catalogue

`free/catalog_claims.py:143-158` sélectionne le prochain hostname avec :

```sql
ORDER BY COALESCE(cl.fail_count, 0), c.priority, c.hostname
```

`c.priority` vient de la table `catalog` (9 788 lignes `hostname, country,
priority`). C'est une priorité **statique**, pas un score mesuré. Le tri
composite de `fresh_chain` (`scripts/fresh_chain.py:1086-1121`) n'est pas
utilisé pour l'ordre des claims.

Les statuts de `claims` au moment de l'audit :

| Statut | Lignes | Effet |
|---|---:|---|
| `country_backoff` | 5 407 | hostname écarté 30 min avec tout son pays |
| `no_egress` | 4 242 | backoff exponentiel 5 min → 1 h |
| `bind_error` | 121 | backoff exponentiel |
| `429` | 9 | cooldown court (600 s par défaut) |
| `claimed` | 6 | lease actif |
| `abandoned` | 3 | reclaimable |

`country_backoff` est posé par `scripts/fresh_chain.py:1817-1843` et
`:2053-2080` : après 3 vrais 429 dans un pays, ou 5 échecs de bind/no-egress,
**tout le pays** est mis en pause (30 min ou 10 min). Chaque hostname du pays
reçoit ensuite `country_backoff` et un lease de 1 800 s
(`free/catalog_claims.py:316-320`). C'est un **quota par pays déguisé**,
interdit par la constitution VI (« aucune contrainte de répartition
géographique ; la seule contrainte dure est l'unicité de l'IP d'egress »).

Le résultat est mécanique : 5 407 hostnames sont parqués par pays, et 4 242 par
des échecs de bind dont une partie sont des **skips de filtre** qui n'ont jamais
atteint la porte de quota.

### 2.5 Les stations sont couplées et les rotations sérialisées

Les six stations ne sont pas réellement indépendantes dans l'exécution :

- `free/rotation_planner.py:1-11` pose comme règle « **jamais deux rotations
  planifiées simultanément** » et « une seule station à la fois ». C'est
  l'inverse d'une gestion parallèle : une rotation attend qu'une autre finisse.
- `config.yaml:1259-1263` définit des pauses **globales** :
  `global_429_threshold: 100000`, `global_429_pause_s: 900`,
  `exhausted_threshold: 5000`, `exhausted_pause_s: 1800`. Une rafale de 429 ou
  un seuil d'épuisement peut donc arrêter les six stations, même si une seule
  IP est concernée.
- `free/catalog_claims.py` partage un ordre de claim et un `country_backoff`
  entre toutes les stations : un pays mis en pause par une station parque les
  hostnames pour les cinq autres.
- `scripts/fresh_chain.py` rebinde la station **avant** de prouver la nouvelle
  IP ; `_restore_previous_host` n'intervient qu'en cas d'abandon. Pendant la
  tentative, la station ne sert plus l'ancienne IP : c'est une coupure non
  mesurée, pas un make-before-break.
- `ip_registry.allocator.mode` vaut `off` (`config.yaml:1208-1209`) : le
  planificateur proactif (drain/spare) n'est pas utilisé. `station_state` est
  vide ; `warm_pool` contient 38 lignes, mais aucune garantie de standby **par
  station**.

La conséquence est mesurable dans `logs/station_slo.jsonl`. Sur les **2 871**
échantillons des dernières 24 h (2026-11-08 20:43 → 2026-11-09 20:43) :

| Indicateur | Valeur |
|---|---:|
| Échantillons `6/6 healthy` | 1 430 (49,8 %) |
| Échantillons `0/6 healthy` | 1 102 (38,4 %) |
| Station `connected` (moyenne) | ~50 % du temps |
| Station `disconnected` (moyenne) | ~40 % du temps |
| Transitions de statut par station | 62 à 163 par 24 h |

Chaque station est donc indisponible ou dégradée une grande partie du temps,
et les rotations ne sont ni parallèles ni sans coupure. L'objectif « toutes les
stations au maximum utilisables, avec le minimum de coupure, voire zéro » n'est
pas tenu par l'architecture actuelle.

### 2.6 La stratégie auto n'utilise pas réellement tous les transports

`config.yaml:1226-1227` active `fast_discovery: true` avec
`fast_discovery_transport: wireguard`. Dans ce mode,
`scripts/fresh_chain.py:1903-1917` réduit la liste des transports à **un seul** :

```python
if fast_discovery:
    transports = [discovery_pref or discovery_transport]
```

Le `transport_order` par défaut (`wireguard`, `openvpn-udp`, `openvpn-tcp`,
`scripts/fresh_chain.py:650-663`) n'est donc pas parcouru. En cas de 429,
`scripts/fresh_chain.py:2018-2028` passe au **hostname suivant** au lieu
d'essayer les autres transports, en supposant que « the other two transports
on the same hostname usually lands in the same burned NAT range ». Cette
supposition n'est pas mesurée.

Les tentatives réelles par transport :

| Transport | Tentatives | 200 | 429 | `no_egress` |
|---|---:|---:|---:|---:|
| `wireguard` | 6 797 | **73** | 982 | 5 717 |
| `openvpn-udp` | 14 498 | **0** | 2 076 | 12 378 |
| `openvpn-tcp` | 1 547 | **0** | 7 | 1 531 |
| `auto` | 3 203 | 1 | 602 | 2 524 |

**Tous les 200 viennent de WireGuard.** OpenVPN UDP et TCP cumulent **16 045
tentatives et zéro 200** : le mode est utilisé, mais il ne qualifie pas. Soit
le chemin OpenVPN est cassé (bind, AUTH, probe, DNS), soit toutes les IP
OpenVPN testées sont réellement épuisées ; l'audit ne permet pas de trancher,
et c'est précisément pourquoi il faut mesurer la santé **par transport** avant
de déclarer le fallback valide. `server_transports` est vide, donc aucune
capacité par hostname n'est connue.

### 2.7 Le watchdog, la gestion et la découverte sont des îlots

Le control plane NordVPN existe déjà, mais en morceaux non reliés :

| Composant | Rôle actuel | Angle mort |
|---|---|---|
| `ops/lifecycle.py` | `SlotState` (`created` → … → `client_usable`), `LifecycleEngine`, `decide_failure`, `reconcile` | N'est pas branché à la file de qualification ; aucune boucle de décision par station. |
| `ops/reconciler.py` | Décide desired/actual, un recreate planifié, sans effet de bord | Pas de lien avec `fresh_chain`/qualification ; décision non exécutée par le watchdog. |
| `ops/supervisor.py` | `StationSupervisor` par station, breaker, warm-up | L'en-tête dit « Lot 2 branchera watchdog/restart ici » : le watchdog n'est pas branché. |
| `free/station_health.py` | Machine à états pure (`usable`, `suspect`, `repairing`, `probing`, `parked`) | `station_state` est **vide** : l'état n'est pas persisté. |
| `free/repair_queue.py` | File de réparation par station | Vit dans `logs/repair_queue.db`, pas `registry.db` ; `repair_jobs` et `repair_requests` dans `registry.db` = **0**. |
| `scripts/fresh_chain_supervisor.py` | Watchdog de la chaîne de réparation | `incidents` = 162 lignes, **toutes** `scope='fleet'` (157 `client_usable_low`, 3 `sweep_stalled`, 2 `warm_pool_low`) ; aucune ligne `scope='station'`. |
| `scripts/ip_registry_discover.py` | Découverte ciblée (petit nombre de serveurs, une station) | N'est pas un scheduler de découverte complet ; pas de run persistant. |
| `free/ip_registry/catalog.py` + `sources/*` | Catalogue API → cache → embedded → manifeste | `catalog_sources.server_count=0`, `servers` 2 066, `server_transports` 0 ; la découverte ne remplit pas le registre. |
| `ops/providers/nordvpn.py` | Adapter Compose/credentials/AUTH/session/transport | Mécanisme présent, politique non branchée au balayage. |

Conséquence : le watchdog surveille la **chaîne de réparation**, pas la
disponibilité de chaque station. La découverte est un script ciblé, pas un
scheduler. La gestion de station (`lifecycle`, `reconciler`, `supervisor`)
n'est pas reliée à la file. Le plan 014 doit couvrir **tout le control plane
NordVPN**, pas seulement la qualification.

### 2.8 Baseline qualité

Le dépôt a de bons garde-fous, mais la base legacy ne satisfait pas encore les
seuils de modularité/maintenabilité visés :

| Indicateur | Mesure | Cible |
|---|---|---:|
| Plus gros modules | `opencode.py` 31 347 lignes ; `vpn/manager.py` 11 385 ; `free/pool.py` 8 190 ; `dashboard/api.py` 6 845 | < 1 500 pour tout nouveau module |
| Modules du control plane | `scripts/fresh_chain.py` 2 535 ; `fresh_chain_supervisor.py` 1 934 | < 1 500 |
| Modules egress existants | `egress_store.py` 1 048 ; `ip_registry/sqlite_registry.py` 1 039 ; `egress_catalog.py` 440 | < 1 500 (OK) |
| Couverture (dernier `.coverage`) | 38 % total ; `vpn/manager.py` 38 % | ≥ 85 % sur le nouveau domaine/application |
| `config_audit` | 0 dead / 0 missing | 0 / 0 |
| `platform_audit --strict` | 0 violation | 0 |
| Dette ouverte (`debt_status.json`) | D-02 (P1 monolithes), D-04 (P1 GUI), D-16 (P2 exceptions silencieuses), D-20 (P2 dashboard lourd) | 0 P0, P1 planifiée, 0 non listée |
| Tests | 365 fichiers de test | couverture des chemins critiques |

La conclusion est nette : les garde-fous (`config_audit`, `platform_audit`,
tests) sont bons, mais les monolithes legacy et la couverture restent des
dettes. La feature 014 ne doit **pas ajouter de dette** : nouveau code < 1 500
lignes, aucun module legacy touché ne doit grossir, couverture du nouveau code
≥ 85 %, et les monolithes sont **découpés progressivement** par strangler
(US12), avec parité, shadow, canary et rollback ; D-02/D-20 sont closes à la
fin.

---

## 3. Mesures

### 3.1 Catalogue amont

`logs/nordvpn_api.json` (snapshot du 2026-11-09 08:39) :

- **7 070** serveurs, tous `status='online'` ;
- **6 932** `wireguard_udp`, **6 933** `openvpn_tcp`, **6 932** `openvpn_udp` ;
- **150 pays** ; top : United States 2 485, United Kingdom 821, Canada 413,
  Germany 309, France 255, Australia 237, Japan 207, Netherlands 199 ;
- charge : min 0, médiane **10**, max **84** ;
- 7 070 hostnames distincts, aucun doublon.

`logs/gluetun_nordvpn_servers.json` contient **19 573** entrées (catalogue
Gluetun fusionné) ; `logs/gluetun_nordvpn_embedded.json` en contient **19 569**.
Ces catalogues sont plus larges que l'API, mais ils ne sont pas la source de
vérité du runtime : l'API reste primaire, les autres complètent.

### 3.2 Couverture réelle

`scan_attempts` (26 045 tentatives, 285 runs) :

| Statut | Tentatives | Part |
|---|---:|---:|
| `no_egress` | 22 150 | 85,0 % |
| `429` | 3 667 | 14,1 % |
| `interrupted` | 149 | 0,6 % |
| `200` | 74 | 0,28 % |
| `intent` | 5 | 0,02 % |

Hostnames distincts touchés : **6 817**, dont **4 809 dans l'API courante**
(68,0 % des 7 070 serveurs live) et **2 008 hors API** (29,5 % des hostnames
tentés). **2 261 serveurs live (32,0 %) n'ont jamais été tentés.**

**Couverture du catalogue live** (intersection avec `logs/nordvpn_api.json`) :

| Mesure | Hostnames | Part |
|---|---:|---:|
| Serveurs live | 7 070 | 100 % |
| Tentés au moins une fois | 4 809 | 68,0 % |
| Jamais tentés | 2 261 | 32,0 % |
| Ont atteint la porte (429) | 1 372 | 19,4 % |
| Ont obtenu un 200 | 0 | 0 % |
| Ont un `no_egress` | 4 776 | 67,5 % |
| N'ont **que** des `no_egress` | 3 378 | 47,8 % |

Sur l'ensemble live + legacy :

- hostnames avec au moins un 200 : **4** ;
- hostnames avec au moins un 429 : **1 587** ;
- hostnames avec **uniquement** des `no_egress` : **5 143** ;
- hostnames avec un mélange `no_egress` + autre : **1 618**.

Autrement dit, le scanner a tenté 6 817 hostnames — 96,4 % du **nombre** de
serveurs live — mais **4 809 seulement sont live (68,0 %)** et **2 008 sont des
hôtes legacy**. Seules 3 741 tentatives (14,4 %) ont atteint la porte de quota,
sur **1 591 hostnames distincts (22,5 %) — dont 1 372 live (19,4 %)**. **5 226
hostnames n'ont jamais atteint cette porte** ; 5 143 d'entre eux n'ont **que**
des `no_egress`. Ils ne sont pas « testés » au sens de la constitution IV/IX :
aucune observation de quota n'existe pour eux.

Les **4 hostnames** qui portent les 94 observations 200 (`al41`, `al74`,
`al46`, `it372`) **ne sont pas dans l'API courante**. Le catalogue live de
7 070 serveurs n'a **aucun 200 observé** ; les seuls succès sont des hôtes
legacy. Le scanner a donc passé 29,5 % de son effort sur des hôtes qui ne sont
plus annoncés, et n'a qualifié que 19,4 % du catalogue live.

`observations` (3 770 lignes) confirme la famine :

- 94 lignes `200`, 3 676 lignes `429` ;
- **1 592 hostnames** distincts, dont **1 373 live (19,4 %)** ;
- **4 hostnames** seulement portent les 94 succès ;
- la colonne `provider` est vide pour **toutes** les lignes : la provenance
  n'est pas enregistrée.

### 3.3 Quota

`quota_buckets` : **4 871** lignes, **21 786** requêtes utilisées,
**5 180** succès, **21 202** échecs. Les `reset_at` vont de 1 793 946 073 à
1 794 257 037 (fenêtre de plusieurs jours). Le ledger est bien par IP, mais :

- le champ `provider` est vide pour toutes les lignes ;
- le rapprochement avec `egress_options` n'est pas fait : une option `quota`
  peut avoir un `reset_at` fournisseur, tandis que le ledger a une autre ligne
  pour la même IP ;
- le scanner utilise `host_429_reset = now + min(retry_after, 600 s)`
  (`free/catalog_claims.py:285-296`) et non le `reset_at` réel de l'IP. C'est
  volontaire — un même hostname peut rendre une autre NAT egress — mais cela
  signifie que le cooldown **hostname** ne dit rien du quota **IP**. Seul
  `egress_ips`/`quota_buckets` fait foi.

`egress_ips` : **4 739** `fresh`, **200** `exhausted` ; `ip_server_map` :
**4 697** lignes ; `leases` : 92 ; `warm_pool` : 38. Il y a donc de la matière
pour qualifier, mais elle n'est pas convertie en options de rotation.

### 3.4 Ce que « testé » veut dire

Trois définitions circulent et sont confondues :

1. **Tenté** : un claim + un bind ont été lancés (26 045 tentatives, 6 817
   hostnames).
2. **Atteint la porte free** : le probe a reçu un statut HTTP (200 ou 429) :
   1 591 hostnames (dont 1 372 live), 94 + 3 667 tentatives ; **0 hostname
   live n'a obtenu 200**.
3. **Qualifié** : une observation 200 récente et une option `usable` :
   4 hostnames legacy, 0 hostname live ; 6 options NordVPN au total (toutes
   `quota` aujourd'hui).

L'audit doit parler en définition 2 ou 3. La définition 1 est un compteur
d'activité, pas une couverture.

---

## 4. Causes racines

### RC-1 — Le catalogue d'egress est alimenté par la flotte, pas par le catalogue

`free/egress_runtime.py:275-300` ne lit que `/api/vpn/fleet`. Les 7 070
serveurs de `logs/nordvpn_api.json` ne sont jamais transformés en options
`candidate`. Le plafond `max_options: 10` (`config.yaml:1158`) et
`max_active_stations: 6` (`:1159`) s'appliquent à une liste qui n'a au mieux
que six lignes.

**Preuve** : `egress_options` = 6 lignes NordVPN, toutes `quota`.

### RC-2 — Aucune file de qualification

`EgressStore.selectable` (`free/egress_store.py:670-688`) ne retourne que
`status='usable'`. Une option `candidate` n'est jamais ordonnée, jamais
claimée, jamais promue par le catalogue. `select_option`
(`free/egress_rotation.py:32`) demande `max(8, max_active * 4)` candidats
`usable` ; sans `usable`, il retourne `None`.

Il n'existe donc aucun état « hostname connu, pas encore testé » dans
`egress_options`. La seule file est `catalog` + `claims`, et elle est statique
et parquée.

### RC-3 — Le chemin catalogue complet n'est pas branché

- `scripts/ip_registry_observe.py:29` écrit dans
  `cfg["db_path"]`, par défaut `logs/ip_registry.db`. Ce fichier fait **0
  octet**.
- `free/egress_runtime.py:331-335` lit
  `ip_registry.registry_db_path`, qui vaut `logs/registry.db`
  (`config.yaml:1170`).
- `servers` dans `registry.db` ne contient que **2 066** lignes (1 509
  `fresh-chain`, 557 `legacy-ip-registry`), toutes sans `status`.
- `catalog` contient **9 788** lignes `hostname, country, priority`, sans
  charge, sans transport, sans fraîcheur. **2 008 hostnames tentés ne sont pas
  dans l'API** : le scanner claim donc des fantômes, les skip, et les marque
  `no_egress`.
- `server_transports` est **vide** : aucune capacité de transport par serveur.
- `sweep_cursor` est **vide** : aucun curseur de balayage.

Le catalogue complet existe donc sur disque, mais il n'est ni synchronisé dans
la bonne base, ni enrichi (transports, charge, RTT), ni consommé par le
runtime.

### RC-4 — L'ordre réel est statique et ne mesure pas le potentiel

`free/catalog_claims.py:150-154` :

```sql
ORDER BY COALESCE(cl.fail_count, 0), c.priority, c.hostname
```

`c.priority` est une colonne statique de `catalog`. Elle ne contient ni la
charge API, ni le RTT opérateur, ni le quota restant, ni le taux de 200. Le
score composite de `fresh_chain` n'est pas utilisé pour l'ordre des claims.
`claim_next` peut donc choisir un hostname absent du catalogue live ; le
scanner le skip et le marque `no_egress` (`filtered-candidate`), ce qui
augmente son `fail_count` et le rétrograde encore.

### RC-5 — Le pays est une porte dure déguisée

`scripts/fresh_chain.py:1817-1843` et `:2053-2080` mettent un pays entier en
pause après 3 × 429 ou 5 × bind/no-egress. `free/catalog_claims.py:316-320`
traduit cela en `country_backoff` + lease 1 800 s pour chaque hostname du pays.
5 407 hostnames sont dans cet état.

Cela viole la constitution VI et, surtout, **empêche de découvrir les IP
fraîches du pays** : un pays peut contenir 2 485 serveurs (United States) et
être parqué sur la base de trois 429. Une série d'échecs sur quelques IP ne
prouve rien sur les autres.

### RC-6 — Le scanner s'arrête au premier 200

`scripts/fresh_chain.py:2142-2143` : `_close_run("recovered"); return 0`. Les
75 runs `recovered` ont trouvé une IP et se sont arrêtés. Le reste du slice
n'est pas testé. Le superviseur relance un run de réparation quand une station
a besoin d'une IP, pas pour finir la couverture.

Il n'y a donc aucune notion d'**époque de balayage** : « tous les hostnames
ont été vus au moins une fois, avec un résultat de quota ou une raison de
skip ». `sweep_cursor` vide confirme l'absence de reprise.

### RC-7 — 85 % des tentatives échouent avant le quota

Sur 26 045 tentatives, 22 150 sont `no_egress`. Une partie sont des skips de
filtre (`filtered-candidate`), une partie des binds qui n'obtiennent pas
d'egress. Le résultat est le même : le hostname n'a pas atteint la porte free,
mais il est enregistré comme un échec de hostname. La distinction
constitutionnelle II (panne de tunnel ≠ IP condamnée) n'est pas respectée dans
la pratique.

### RC-8 — Trois scores, aucun ping opérateur

- `free/egress_catalog.py:363-385` : 40 % succès, 25 % latence, 20 % débit,
  10 % stabilité, 5 % fraîcheur. Une option jamais testée vaut **50,0**
  (succès 0, latence 1,0, débit 0,5, stabilité 1,0, fraîcheur 1,0), ce qui
  n'est pas un score de « potentiel ».
- `scripts/fresh_chain.py:967-973` : mêmes poids, mais les valeurs par défaut
  d'un hostname inconnu sont 0,5/0,5/0/0,5/0,5 → **0,40** sur 1.
- `free/ip_quota.py:243-257` : `(headroom × success_rate, -latence, -inflight,
  -station)`, un ordre lexicographique qui privilégie le quota restant.

Aucun n'utilise `PreflightProber` (`free/ip_registry/preflight.py:23-43`) pour
mesurer le DNS/connect/handshake **depuis l'opérateur**, ni la charge API comme
facteur (elle n'est qu'un tiebreaker dans `fresh_chain`), ni le quota restant
dans la file de qualification.

### RC-9 — L'allocateur est éteint et les preuves sont minces

`config.yaml:1208-1209` : `allocator.mode: 'off'`. `server_transports` vide,
`station_state` vide, `sweep_cursor` vide. `observations.provider` vide.
`catalog_sources` ne contient que trois lignes `legacy`, `fresh-chain`,
`runtime`, avec `server_count=0`.

Le système a donc des briques (allocateur, scoring, preflight, sweep cursor)
mais ne les branche pas. La spec 014 doit brancher ces briques ou les remplacer
explicitement, pas ajouter une quatrième mécanique parallèle.

### RC-10 — Les seuls 200 viennent d'hôtes absents de l'API

Les 94 observations 200 et les 74 tentatives 200 portent sur `al41`, `al74`,
`al46` et `it372`, tous absents de `logs/nordvpn_api.json`. Le scanner réussit
donc sur des hôtes legacy, s'arrête au premier 200 (`recovered`), et ne qualifie
**aucun** serveur du catalogue live. Les 2 261 serveurs live jamais tentés et
les 4 809 tentés sans 200 sont la conséquence directe de cet ordre de claim et
de cet arrêt précoce.

### RC-11 — Les stations sont couplées, les rotations sérialisées et les coupures non mesurées

`free/rotation_planner.py:1-11` interdit deux rotations simultanées ;
`config.yaml:1259-1263` autorise des pauses globales ; `catalog_claims` partage
le `country_backoff` ; `fresh_chain` rebinde avant de prouver la nouvelle IP.
Il n'existe ni standby garanti par station, ni make-before-break, ni mesure de
la coupure. Les 2 871 échantillons SLO des dernières 24 h montrent 49,8 % de
`6/6 healthy` et 38,4 % de `0/6` : la disponibilité n'est pas pilotée par
station, elle subit les rotations et les réparations globales.

### RC-12 — La stratégie auto ne parcourt pas tous les transports et ne mesure pas leur santé

`fast_discovery: true` réduit la liste à un transport
(`scripts/fresh_chain.py:1903-1917`), et un 429 passe au hostname suivant au
lieu d'essayer les autres transports (`:2018-2028`). Le `transport_order`
existe mais n'est pas appliqué. `server_transports` est vide. Résultat mesuré :
WireGuard porte **tous les 200** (73), tandis qu'OpenVPN UDP/TCP cumulent
**16 045 tentatives et 0 200**. Le fallback OpenVPN n'est ni exercé
systématiquement ni diagnostiqué par transport ; un blocage UDP WireGuard peut
donc être compté à tort comme un échec du hostname.

### RC-13 — Le watchdog et la découverte ne sont pas reliés au pipeline

`ops/supervisor.py` annonce un watchdog non branché, `free/station_health.py`
n'est pas persisté (`station_state` vide), `free/repair_queue.py` vit dans une
base séparée, `repair_jobs`/`repair_requests` sont vides, et les 162 incidents
sont tous `scope='fleet'` (157 `client_usable_low`, 3 `sweep_stalled`, 2
`warm_pool_low`) : aucun incident par station. La découverte
(`scripts/ip_registry_discover.py`, `free/ip_registry/catalog.py`) est un
script ciblé, pas un scheduler ; `catalog_sources.server_count` vaut 0 et
`server_transports` est vide. Le control plane n'a donc pas de boucle
« découverte → gestion → watchdog → réparation → qualification → rotation »
pour NordVPN.

### RC-14 — La base legacy ne satisfait pas les seuils de modularité et de maintenabilité

Les plus gros modules dépassent largement la règle des 1 500 lignes :
`opencode.py` 31 347, `vpn/manager.py` 11 385, `free/pool.py` 8 190,
`dashboard/api.py` 6 845, `scripts/fresh_chain.py` 2 535,
`fresh_chain_supervisor.py` 1 934. Le dernier rapport `.coverage` donne 38 % de
couverture totale. Quatre dettes sont ouvertes : D-02 (P1 monolithes), D-04
(P1 GUI), D-16 (P2 exceptions silencieuses), D-20 (P2 dashboard lourd). Les
garde-fous sont bons (`config_audit` 0/0, `platform_audit` 0 violation, 365
fichiers de test), mais la feature 014 doit appliquer un **strangler avec
ratchet** et un **démantèlement planifié** : aucun nouveau module > 1 500
lignes, aucun module legacy touché ne grossit, couverture du nouveau code
≥ 85 %, et les monolithes sont découpés par slices (manager, pool, fresh_chain,
dashboard, opencode) avec parité, shadow, canary et rollback.

---

## 5. Ordre actuel vs ordre demandé

| Signal | Demandé | Présent dans `egress_catalog` | Présent dans `fresh_chain` | Présent dans `claim_next` |
|---|---|---|---|---|
| Taux de 200 mesuré | oui | oui (success_count) | oui (observations 7 j) | non |
| Latence / ping opérateur | oui | partiel (`last_latency_ms`) | partiel (latence des 200) | non |
| Charge du serveur | oui | non | tiebreaker seulement | non |
| Quota restant par IP | oui | non | non | non |
| Fraîcheur | oui | oui | oui | non |
| Pays | interdit comme facteur | non | non (métadonnée) | **oui, porte dure** |
| Transport | préférence | non | oui (filtre) | non |
| Preflight DNS/connect | oui | non | non | non |

L'ordre demandé est donc **absent** : le seul ordre effectif est
`fail_count`, `priority` statique, `hostname`.

---

## 6. Conséquences et tensions avec la constitution

- **II (trois ressources)** : le scanner condamne un hostname sur un
  `no_egress` de station/transport. Une panne de tunnel ne doit pas condamner
  l'IP ; elle doit changer de station ou de transport.
- **III (une source de vérité)** : `catalog` (9 788), `servers` (2 066),
  `egress_options` (7 026), `claims` (9 788) et `catalog` de l'API (7 070)
  divergent. `registry.db` doit être l'autorité unique, avec une file et des
  observations.
- **IV (reset fournisseur)** : le `reset_at` de l'IP est respecté dans
  `egress_catalog`, mais le scanner utilise un cooldown hostname de 600 s. Le
  ledger IP n'est pas consulté avant un nouveau bind.
- **VI (couverture mondiale, pas de quota pays)** : `country_backoff` viole
  directement le principe. 5 407 hostnames sont parqués par pays.
- **VII (aucune preuve, aucun succès)** : 94 observations 200 sur 4 hostnames
  ne suffisent pas à déclarer le catalogue « testé ».
- **IX (le score ordonne, les portes dures décident)** : le score n'ordonne
  pas les claims. Les portes dures (reset, unicité) sont respectées dans
  `egress_catalog`, pas dans le scanner.
- **X (dégradation explicite, reste free)** : l'absence de couverture est
  affichée comme un manque d'options, pas comme un pipeline de qualification
  incomplet. La coupure d'une rotation n'est ni mesurée ni exposée.
- **II/VIII (indépendance, un seul réparateur)** : les pauses globales
  (`global_429_pause_s`, `exhausted_pause_s`) et la règle « jamais deux
  rotations simultanées » couplent les stations. Une station doit pouvoir
  servir, qualifier ou se réparer sans attendre les cinq autres.
- **XII (toute option GUI a un effet)** : `max_candidates_per_chain`,
  `stop_after_429`, `global_429_threshold` et `exhausted_threshold` n'ont pas
  d'effet observable sur le balayage ; ils doivent avoir un contrat ou être
  retirés. Une pause globale sans preuve par station est un mensonge
  d'observabilité.
- **III/VIII (source de vérité, un seul réparateur)** : `station_state` vide,
  `repair_jobs`/`repair_requests` vides, file de réparation dans
  `logs/repair_queue.db` et incidents `fleet` : le watchdog n'a pas d'état
  persistant par station. La réparation doit être un objet de `registry.db`,
  avec un seul propriétaire.

---

## 7. Ce qu'un bon design doit produire

1. **Une file unique par hostname** : chaque serveur en ligne du catalogue
   (7 070) entre une fois dans une file `qualification`, avec pays, ville,
   charge API, transports, RTT opérateur, quota connu, score potentiel.
2. **Un score de potentiel** distinct du score mesuré : taux de 200
   (Laplace), RTT opérateur (DNS + connect + handshake), quota restant par IP,
   charge API, fraîcheur. Pas de pays, pas de source.
3. **Un ordre explicite** : `potential_score DESC`, puis quota restant, RTT,
   charge, hostname. La file est reprenable et versionnée par époque.
4. **Un balayage qui continue après 200** : le 200 promeut l'IP en `usable`
   pour le service ; le balayage passe au hostname suivant. Il ne s'arrête que
   lorsque l'époque est complète ou que le budget est atteint.
5. **Une rotation sur 429** : l'IP consommée passe en `quota` avec son
   `reset_at` fournisseur ; la station claim immédiatement le meilleur
   candidat suivant. Le quota de chaque IP est consommé, puis l'IP revient à
   son reset.
6. **Une classification honnête** : `200`, `429`, `no_egress`/`bind_error`
   (infrastructure, retry), `auth` (station, pas host), `dead` (host
   réellement mort), `skipped` (raison explicite). Aucun pays en porte dure.
7. **Une couverture mesurable** : époque, testés / total, 200 / 429 /
   infra / skip, par pays et par transport, prochains candidats, âge du
   dernier test.
8. **Un seul propriétaire** : le superviseur/allocateur reste le seul
   réparateur ; le balayage est un job de fond, jamais dans le chemin de
   requête.
9. **Six workers indépendants** : une station = un worker, un état, un
   curseur ; les claims sont atomiques ; aucune rotation globale n'est
   sérialisée.
10. **Make-before-break** : un standby validé est préparé avant de toucher la
    station ; l'ancienne IP sert jusqu'au 200 de la nouvelle ; rollback si le
    nouveau bind échoue ; la coupure est mesurée.
11. **Arrêt par station, jamais global** : un 429, un AUTH_FAILED ou un
    épuisement ne pause que la station/IP concernée ; les cinq autres
    continuent de servir et de qualifier.
12. **Disponibilité par station** : objectif 99 % `client_usable` quand une IP
    200 existe ; dashboard par station, standby, cutover, raison de coupure ;
    aucune station n'est sacrifiée au balayage.
13. **Un control plane unifié** : découverte, gestion de station, watchdog,
    réparation, qualification, rotation et quota partagent `registry.db` et
    une seule politique. Le watchdog surveille chaque station, pas seulement
    la chaîne ; la découverte est un scheduler, pas un script ciblé ; le
    manager existant reste l'adapteur, sans fork.
14. **Un démantèlement planifié des monolithes** : `vpn/manager.py`,
    `free/pool.py`, `scripts/fresh_chain.py`, `dashboard/api.py` et
    `opencode.py` sont découpés par seams, avec tests de caractérisation,
    shadow, canary, rollback et parité p95 ≤ +5 %. Objectif : < 1 500 lignes
    par module ou entrée fine, D-02/D-20 closes.

---

## 8. Annexes

### A. Requêtes de vérification

```sql
-- Couverture par statut de tentative
SELECT status, COUNT(*) FROM scan_attempts GROUP BY status ORDER BY 2 DESC;

-- Hostnames réellement qualifiés
SELECT COUNT(DISTINCT hostname) FROM observations WHERE status = 200;

-- Claims par état
SELECT status, COUNT(*) FROM claims GROUP BY status ORDER BY 2 DESC;

-- Options d'egress par provider
SELECT provider, status, COUNT(*) FROM egress_options GROUP BY provider, status;
```

### B. Citations

| Sujet | Fichier:ligne |
|---|---|
| Flotte → stations | `free/egress_runtime.py:275-300` |
| Cap adaptateur | `free/egress_adapters/nordvpn.py:56`, `:123-142` |
| Plafonds config | `config.yaml:1154-1163` |
| Sélection `usable` seulement | `free/egress_store.py:670-688` |
| Score rotation | `free/egress_catalog.py:363-385` |
| Ordre des claims | `free/catalog_claims.py:150-154` |
| Backoff pays | `free/catalog_claims.py:316-320` |
| Sérialisation des rotations | `free/rotation_planner.py:1-11` |
| Pauses globales | `config.yaml:1259-1263` |
| Stratégie `fast_discovery` | `config.yaml:1226-1227` |
| Transports réduits à un | `scripts/fresh_chain.py:1903-1917` |
| 429 → hostname suivant | `scripts/fresh_chain.py:2018-2028` |
| Ordre des transports | `scripts/fresh_chain.py:650-663` |
| Cutover avant preuve | `scripts/fresh_chain.py:2142-2143`, `_restore_previous_host` |
| Watchdog non branché | `ops/supervisor.py:1-14` |
| État station non persisté | `free/station_health.py:1-15` |
| File de réparation séparée | `free/repair_queue.py:14-20` |
| Incidents fleet only | `logs/registry.db:incidents` |
| Découverte ciblée | `scripts/ip_registry_discover.py:1-13` |
| Déclencheur pays | `scripts/fresh_chain.py:1817-1843`, `:2053-2080` |
| Arrêt au premier 200 | `scripts/fresh_chain.py:2142-2143` |
| Skip filtre → `no_egress` | `scripts/fresh_chain.py:1848-1853` |
| Slice superviseur | `scripts/fresh_chain_supervisor.py:326-350` |
| `--max` par défaut | `scripts/fresh_chain.py:2522` |
| Preflight opérateur | `free/ip_registry/preflight.py:23-43` |
| Score quota | `free/ip_quota.py:243-257` |
| Allocateur off | `config.yaml:1208-1209` |
| DB du catalogue observé | `scripts/ip_registry_observe.py:29` |
| DB lue par le runtime | `free/egress_runtime.py:331-335` |

### C. Glossaire

- **Serveur / hostname** : entrée du catalogue NordVPN (`xx123.nordvpn.com`).
- **IP d'egress** : IP publique observée derrière un hostname et un transport ;
  c'est l'unité de quota et de rotation.
- **Qualification** : fait d'atteindre la porte free et d'obtenir un 200 ou un
  429 pour une IP.
- **Époque** : un passage complet sur tous les hostnames en ligne, avec reprise
  et état terminal par hostname.
- **Potentiel** : score a priori utilisé pour ordonner la file avant la
  première mesure.
- **Mesuré** : score a posteriori (constitution IX) utilisé pour la rotation.
