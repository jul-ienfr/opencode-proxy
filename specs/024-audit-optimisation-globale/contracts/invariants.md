# Contrats d'invariant — spec 024

Chaque contrat est un **test automatisé** qui échoue aujourd'hui et doit
passer après le plan. C'est la traduction directe des constats de l'audit en
gates opposables (principe XVII : « chaque exigence fonctionnelle a un test
automatisé »).

Format : `INV-<n>` — énoncé, preuve du besoin (constat de l'audit), test.

---

## INV-1 — Une table, un déclarant

**Énoncé** : aucun `CREATE TABLE IF NOT EXISTS` n'existe en dehors de
`schema.sql` (ou d'un module de migration unique).

**Preuve du besoin** : `registry.db` a **52 tables**, dont **22 hors
`schema.sql`**, créées par **15 modules**. **5 tables** ont des déclarants
multiples avec des DDL divergentes : `ip_state` (colonnes incompatibles),
`station_state` (`CHECK` présent dans un cas, absent dans l'autre),
`server_transports`, `incidents` (3 déclarants), `repair_jobs`.

**Test** : un test AST scanne tout `.py` du dépôt et échoue sur tout
`CREATE TABLE` hors de la liste blanche. Le test doit aussi vérifier que les
DDL de la liste blanche sont **identiques** entre elles.

**Effort** : S (le test) · M (la fusion des 5 tables).

---

## INV-2 — L'intégrité référentielle est appliquée

**Énoncé** : `PRAGMA foreign_key_check` retourne 0 sur `registry.db`.

**Preuve du besoin** : **15 179 violations** mesurées, toutes
`server_transports.hostname → servers.hostname`. **5 des 6 connexions
écrivantes** omettent `PRAGMA foreign_keys=ON`.

**Test** : un test ouvre la base de test, exécute les écritures de chaque
connexion, puis `PRAGMA foreign_key_check` → 0.

**Effort** : S (le pragma) · M (corriger les insertions orphelines).

---

## INV-3 — Aucune valeur `time.monotonic()` persistée

**Énoncé** : aucun état persisté ne contient de valeur issue de
`time.monotonic()`. On persiste des **durées restantes**.

**Preuve du besoin** : `free_pool.json` contient `last_ok_at = 762 626.406`
alors que `time.monotonic()` vaut `269 939` — **2,8× l'horloge courante**.
Les 6 stations portent la **valeur identique au millième**. Après un reboot,
un park de 30 min devient « dans 30 min + uptime précédent ».

**Test** : un test sérialise l'état, simule un reboot (monotonic remis à 0),
restaure, et vérifie que les durées restantes sont **préservées** et que
toute valeur incohérente (négative ou > TTL max) est purgée.

**Effort** : S (2 sites : `station_health`, `ip_quota`).

---

## INV-4 — Un compteur mesure un fait, pas une répétition

**Énoncé** : la somme des compteurs d'une passe de qualification reste du
même ordre que le nombre de tentatives réelles.

**Preuve du besoin** : `SUM(qualification_epochs.tested)` = **847 383** pour
**41 166** tentatives réelles → **×20,6**. **6 769 epochs** portent le même
`total=7118` — le même ensemble re-mesuré. **1 215 epochs/jour**.

**Test** : après un cycle de qualification, `SUM(tested)` ≤ 2 ×
`COUNT(qualification_attempts)`.

**Effort** : M (corriger la boucle) · S (le test).

---

## INV-5 — Un `dead` exige une preuve de test

**Énoncé** : un candidat jamais sondé n'est **pas** marqué `dead`.

**Preuve du besoin** : **26 474 options `public` sur 30 647 (86,4 %)** sont
`dead` avec `egress_ip` vide. Le code (`public.py:558-566`) fait
`result.dead += 1` quand `outcome` est `None` (« no probe ») comme quand il
est en erreur. **129 805 observations** en phase `liveness`.

**Test** : un candidat sans sonde aboutie conserve un état distinct
(`unprobed`), et n'est pas compté dans `dead`.

**Effort** : S.

---

## INV-6 — Un `200` ne compte que s'il a produit du contenu

**Énoncé** : la promotion d'une option exige que la réponse ait produit du
contenu, pas seulement un code HTTP 200.

**Preuve du besoin** : le gate catalogue (`public.py:142`) promeut sur
`free_status == 200` sans lire le corps (sauf en 429), alors que le chemin
**client** (`opencode.py:11292`) rejette un SSE vide en 502 avec le
commentaire « *SSE vide (tunnel tronqué ?) → proxy suivant* ». **Asymétrie de
preuve.**

**Test** : un gate qui reçoit un `200` avec un corps vide ne promeut pas
l'option. Le test doit utiliser **le même** validateur que le chemin client.

**Effort** : S.

---

## INV-7 — Toute surface d'état a un test de non-divergence

**Énoncé** : `/api/vpn-status`, `station_state`, `free_pool.json` et les pins
décrivent le même état pour les 6 stations.

**Preuve du besoin** : `station_state` dit station 2 `REPAIRING/client_usable=0`
avec l'IP `1.2.3.4` ; l'API l'a servie `client_usable=true`. `free_pool.json`
déclare **6 stations `usable`** alors que l'API en voit **1**. **58 stations
fantômes** dans `station_health`, dont 46 `usable` avec `good_probes=0`.

**Test** : toutes les 30 s pendant 24 h, comparer `(station, client_usable,
current_ip)` entre les surfaces ; **zéro divergence tolérée**.

**Effort** : M.

---

## INV-8 — Aucun artefact requis n'est absent du dépôt

**Énoncé** : tout chemin d'artefact référencé par la configuration ou le code
existe, et est versionné ou généré par un script versionné.

**Preuve du besoin** : trois cas mesurés —
`logs/compose-control-plane-{1..6}.yml` (**gitignoré**, porte l'image du
fork), `free/official_payload.json.bak` (**gitignoré**, requis par
`probe.py:88-90`), `free/captures/1.19.5.json` (**supprimé**, référencé par
`verified_profiles.json:26`).

**Test** : un test parcourt les chemins d'artefacts déclarés (profils, captures,
overlays, payloads) et échoue sur tout fichier absent. Un second test vérifie
que chaque fichier requis est **suivi par git** ou produit par un script
versionné.

**Effort** : S (le test) · M (versionner les artefacts).

---

## INV-9 — Le chemin testé est le chemin livré

**Énoncé** : aucun chemin de production n'est conditionné par
`PYTEST_CURRENT_TEST`.

**Preuve du besoin** : **10 sites** dans le code de production. Deux sont des
protections légitimes (`quota_ledger.py:335` empêche d'écrire dans la base
réelle, `opencode.py:4575` supprime un log). **Cinq sautent des chemins de
production** : réutilisation de preuve (`probe.py:109`), persistance du 429
(`probe.py:179`), transition `proven` (`probe.py:252`), `classify_429` +
`ip_state` (`opencode.py:16020`).

**Corrélation** : `ip_state` ne contient **que** `unproven`/`proven`,
**jamais** `spent`/`burned` — précisément les transitions désactivées en test.

**Test** : un test AST échoue sur tout `PYTEST_CURRENT_TEST` dans un chemin
de production. Les cas légitimes passent par un **flag de configuration
explicite**.

**Effort** : S (le test) · M (remplacer par des flags).

---

## INV-10 — Aucun module créé après la constitution ne dépasse 1 500 lignes

**Énoncé** : les modules créés après le 2026-11-04 respectent la borne XIV.

**Preuve du besoin** : trois modules créés **après** la ratification
dépassent la borne — `free/pool/__init__.py` (créé le 2026-11-11, **8 362 l.**,
**+276 lignes en 8 commits** sans extraction), `scripts/fresh_chain_supervisor.py`
et `scripts/fresh_chain.py` (créés le 2026-11-01, 3 294 et 3 242 l.).

**Test** : un test lit la date de création via `git log --diff-filter=A` et
échoue si un module post-constitution dépasse 1 500 lignes.

**Effort** : S (le test) · L (les découpages).

---

## INV-11 — Toute option a un consommateur, tout consommateur a sa clé

**Énoncé** : le gate option-map couvre **toutes** les pages GUI, et aucune
clé de config n'est morte.

**Preuve du besoin** : le gate `tests/test_option_map.py:102-106` filtre les
pages sur la présence du marqueur qu'il vérifie — **contrôle circulaire**.
Mesure : **184 contrôles interactifs**, **17 marqueurs**, **1 page sur 15**
inspectée. Six clés `legacy_a_retirer` sont dans une liste « informative »
donc **ne peuvent pas faire échouer le gate**.

**Test** : `PAGES.rglob("*.svelte")` sans filtre (avec liste d'exclusion
justifiée) + assertion `controls_with_marker >= 0.9 * total_controls`.
`config_audit --strict` doit voir les clés legacy.

**Effort** : M (produire les ~171 contrats).

---

## INV-12 — Les 8 gates de la constitution sont câblées

**Énoncé** : chaque gate nommée par la constitution est invoquée par un
pipeline (CI ou `gate.ps1`).

**Preuve du besoin** : `platform_audit --strict` (cité par la constitution)
**n'est invoqué par aucun pipeline**. La CI tourne sur **Linux seulement**
(`runs-on: ubuntu-latest`, aucune matrice) alors que le principe XV exige
Windows **et** Linux. `gate.ps1:44` utilise `-k "not docker"` au lieu de
`-m` → **46 tests désélectionnés**.

**Test** : un test lit `ci.yml` et `gate.ps1` et vérifie que chaque gate
nommée y figure ; un test vérifie la présence d'une `matrix` d'OS.

**Effort** : S (CI) · M (matrice Windows).

---

## INV-13 — Une IP `proven` porte un `reset_at` ou une preuve récente

**Énoncé** : aucune IP n'est marquée `proven` sans `reset_at` ni preuve
horodatée récente.

**Preuve du besoin** : **116 IP `proven`**, dont **102 sans `reset_at`**,
**116 avec `probe_count=0`**, toutes âgées de **48 à 59 heures**. Aucune
n'est jamais passée en `spent` ni `burned` malgré **7 570 buckets en 429**.

**Test** : toute insertion `proven` exige `reset_at` non nul **ou**
`proven_at` < TTL ; sinon la ligne est `unproven`.

**Effort** : S.

---

## INV-14 — Le garde d'unicité d'IP compare des types compatibles

**Énoncé** : `egress_ip_in_use` retourne `True` uniquement quand une
**autre** station détient réellement l'IP.

**Preuve du besoin** : `free/qualification/store.py:1141` lie
`str(int(station))` (soit `'1'`) à une colonne `station` de type **TEXT**
contenant `'warp-1'`, `'station-2'`. En SQLite, `'warp-1' <> '1'` est
**toujours vrai** → la fonction retourne `True` pour toute IP déjà présente,
y compris détenue par la même station.

**Corrélation** : explique `duplicate_ip_groups=1` et
`capacity_reason = "1 station(s) partagent une IP"`.

**Test** : deux options avec la **même** station et la même IP →
`egress_ip_in_use` retourne `False` ; deux stations différentes → `True`.

**Effort** : S.

---

## INV-15 — Toute table volumineuse est couverte par la rétention

**Énoncé** : chaque table de plus de 10 000 lignes figure dans la politique
de rétention.

**Preuve du besoin** : la purge couvre **5 tables**, dont **3 vides**. Les
**7 tables les plus grosses** (95 % des lignes) ne sont jamais purgées :
`egress_observations` (326 812), `egress_threat_intel` (174 163),
`scan_attempts` (61 027), `qualification_attempts` (41 166),
`egress_options` (31 873), `qualification_epochs` (21 230),
`server_transports` (20 945), `quota_buckets` (8 519).

**Test** : un test énumère les tables > 10 000 lignes et échoue si l'une
n'apparaît pas dans la politique de rétention.

**Effort** : S (le test) · M (la purge).

---

## INV-16 — Aucune exception de programmation n'est avalée silencieusement

**Énoncé** : un `except Exception` qui capture une erreur de programmation
(`NameError`, `AttributeError`, `UnboundLocalError`, `TypeError`) la
**signale** (compteur + log non-DEBUG).

**Preuve du besoin** : **367 `except …: pass` nus sur 1 225 (30 %)** dans
`opencode.py`. C'est ce taux qui a rendu un `NameError` invisible pendant deux
mois — **502 requêtes perdues** mesurées.

**Test** : un compteur `swallowed_programming_errors` est exposé dans
`/metrics` ; un test vérifie qu'il reste à 0 sur un parcours de référence.

**Effort** : L (mécanique, à faire lot par lot).

---

## INV-17 — Un `compose up` ne dégrade aucune station

**Énoncé** : après un `docker compose up -d`, les 6 stations tournent
l'image du fork avec leurs variables de contrôle.

**Preuve du besoin** : `docker-compose.yml` déclare
`qmcgaw/gluetun@sha256:4af0c804…` pour **5 des 6** services ; seul
`vpn-gluetun-4` déclare `gluetun-016:local`. Les 6 conteneurs **vivants**
tournent `gluetun-016:local`, maintenu par des overlays **gitignorés**.
`extra_compose_files` (le rattrapage code) n'a **aucun appelant de
production**.

**Test** : `docker compose config` doit lister `gluetun-016:local` et les
variables `CONTROL_*` pour chaque service station.

**Effort** : S.

---

## INV-18 — Aucune surface de contrôle n'est exposée sans authentification

**Énoncé** : hors loopback, toute route de contrôle exige un token.

**Preuve du besoin** : `config.yaml:46` `require_token: false`,
`:47` `client_auth.mode: none`, écoute `0.0.0.0:4000`. Vérifié :
`GET /api/stats` → **200 sans token**.

**Test** : `curl` sans token sur `/api/stats`, `/api/vpn-status`,
`/api/proxy/restart` → 401.

**Effort** : S.

---

## Tableau récapitulatif

| Contrat | Famille | Effort test | Effort correctif | Constat source |
|---|---|---|---|---|
| INV-1 | C (autorité) | S | M | V-28, V-42 |
| INV-2 | B (données) | S | M | V-36 |
| INV-3 | B (état) | S | S | V-22 |
| INV-4 | B (mesure) | S | M | V-37 |
| INV-5 | B (mesure) | S | S | V-40 |
| INV-6 | A (preuve) | S | S | V-39 |
| INV-7 | B (état) | M | M | V-22, §1 |
| INV-8 | C (dépôt) | S | M | V-15, V-33, V-41 |
| INV-9 | D (gates) | S | M | V-43 |
| INV-10 | D (gates) | S | L | §17 |
| INV-11 | D (gates) | M | M | §4.1 du rapport 08 |
| INV-12 | D (gates) | S | M | V-7, V-8, V-9 |
| INV-13 | A (preuve) | S | S | §6.7 |
| INV-14 | B (type) | S | S | V-44 |
| INV-15 | B (données) | S | M | V-24 |
| INV-16 | A (silence) | M | L | V-17 |
| INV-17 | C (dépôt) | S | S | V-29, V-33 |
| INV-18 | sécurité | S | S | §6.5 |

**18 contrats.** Les plus rentables à écrire en premier : **INV-6, INV-8,
INV-9, INV-14, INV-17, INV-18** — chacun est un test court qui attrape un
défaut mesuré.
