# Audit 2026-11-20 — 05 · Proxys publics / egress publics

Branche : `019-audit-optimisation-flotte-free`
Périmètre : le 3ᵉ chemin d'egress (proxys publics) — découverte, validation, claim, bind, probe, serve, retrait — et le catalogue d'egress qui l'alimente.
Méthode : lecture seule sur le code (aucun fichier modifié), preuves live (`logs/registry.db` et `logs/debug.log` en lecture seule, API `:4000`, `docker ps`), 10 requêtes HEAD de 5 s sur les sources. Aucun secret divulgué (noms de clés uniquement).

---

## 1. Résumé exécutif

1. **Le gate public ne valide jamais le contenu.** `probe_public_candidates` ne lit que `status_code` (`free/egress_adapters/public.py:142`) : un `200` sans contenu ni `event: error` est promu `usable` — violation directe du principe VII. Le chemin client, lui, valide (`opencode.py:11292`), donc le catalogue peut promouvoir une IP que le serveur refusera ensuite.
2. **Le catalogue jette les credentials des proxys authentifiés.** `normalize_proxy` les conserve (`free/egress_sources.py:113-116`), mais `new_option`/`EgressOption` n'ont pas de champ user/pass et `rows_from_options` reconstruit l'URL sans auth (`free/egress_rotation.py:96-100`). Prouvé : `http://user:pa55@1.2.3.4:8080` → `http://1.2.3.4:8080`. Tout proxy à authentification est promu puis échoue systématiquement en production.
3. **Un candidat non testé est enregistré `dead` pendant 7 jours.** `probe_public_candidates` n'écrit un résultat que pour les tâches terminées ; le cycle de production est systématiquement partiel (`cycle partiel: 41 candidat(s) non testé(s)`, `catalog_runs` live). Le candidat absent de `probed` tombe dans la branche `outcome is None` → `OptionStatus.DEAD` (`public.py:558-566`) avec `ttl_dead_s = 604800`. Mesuré : **26 474 des 30 647 options `public` sont `dead` avec `egress_ip=''`** — jamais joignables, donc jamais testables.
4. **Le cycle public ne progresse pas.** 6 runs consécutifs : ~440-460 candidats, 0-14 promus, 368-410 `dead`, tous `partial` (`catalog_runs` live). Le budget de 500 candidats (`cycle_candidate_budget`) est amputé d'environ 10 % par cycle par le `deadline_s` du worker (`public.py:528`).
5. **Une IP en `dead` sans egress n'est jamais re-testée avant 7 jours** et n'entre pas dans `blocked_endpoints` (qui ne filtre que `reset_at > now` / `auth` / `quota`). Le pool reste alimenté par ~176 options `usable` sur 1 415 (dont 1 239 ont un TTL déjà expiré) — la marge est mince.
6. **`_is_denied` reconstruit les réseaux et interroge le threat-intel par candidat** (`free/egress_runtime.py:107-128`, 1,4 ms/appel mesuré) : appelé sur chaque option dans `public_rows`, `block_indicator` (× 5 000) et `sync_public_pool`. Coût non borné, sans cache.
7. **`free/public_proxies.py` ne garde que 5 % de son contenu utile** : 238 lignes dont seuls `is_untrusted_proxy`, `sanitize_free_headers`, `assert_secure_endpoint`, `redact_proxy_url` sont appelés (tous depuis `opencode.py`). Le module n'est **pas** celui décrit par `PLAN_PROXIES_PUBLICS_2026-11-09.md` (§1.1 « dataclass, parse, score, TTL, cap /24 ») : ce rôle a migré vers `free/egress_sources.py` + `free/pool/public_pool.py`.
8. **Le worker de probe (process) contourne l'isolation conteneur du pool.** Le pool sert en `isolation: container` (`config.yaml:262`), mais chaque cycle de catalogue lance un `start_worker_process` (`free/egress_adapters/public.py:69-76`) avec le Python de l'hôte, sans `--cap-drop`/`--read-only`. L'isolation forte ne couvre que le trafic client, pas les ~450 connexions de probe par cycle.
9. **La station d'identité free casse au premier appel après démarrage.** `_station_identity_runtime` renvoie un runtime dont `profile` est `None` au premier appel du process, car `registry.verified()` rafraîchit les statuts en place et les deux profils de `free/profiles/verified_profiles.json` sont expirés depuis le 2026-11-13. `opencode.py:11107` lève alors `RuntimeError("free station identity: aucun profil vérifié")`. Reproduit : `call1 profile=None would_raise=True`, `call2 profile=oc-2.2.5-cli-13`. `free.station_identity: true` (`config.yaml:1051`).
10. **Le fichier de capture 1.19.5 est supprimé mais toujours référencé** : `free/profiles/verified_profiles.json:26` → `free/captures/1.19.5.json` (absent, `git status: D`). `_capture_body` dégrade silencieusement (`runtime_identity.py:123-126`), donc la moitié des profils sert un corps templaté sans trace.
11. **`StationProbe.__init__` lit un fichier gitignoré** : `free/official_payload.json.bak` (`free/ip_registry/adapters/probe.py:88-90`), absent d'un checkout neuf. Prouvé : `FileNotFoundError`. Le module de sondage d'IP ne peut pas être construit dans un environnement propre.
12. **`psutil` est optionnel mais non déclaré** (`requirements.txt` ne le liste pas) : `cleanup_orphan_worker_processes` retourne 0 sans lui (`public_egress_worker.py:436-439`), donc les workers de probe orphelins s'accumulent silencieusement (le commentaire documente ~30 orphelins en 15 h).

---

## 2. Carte des modules

| Module | LOC | Rôle | Appelants réels (production) |
|---|---|---|---|
| `free/egress_adapters/public.py` | 699 | Adaptateur catalogue public : sources → probe isolé → gate free → `store.apply` | `free/egress_runtime.py:19,402` |
| `free/public_proxies.py` | 238 | Garde-fous proxy non fiable (headers, endpoint, redaction) | `opencode.py:10862,10872,10883,14547` |
| `free/public_egress_worker.py` | 559 | Worker CONNECT isolé (process) + `cleanup_orphan_worker_processes` | `free/pool/__init__.py:4390`, `free/egress_adapters/public.py:69`, `opencode.py:3570` |
| `free/public_egress_container.py` | 239 | Même worker en conteneur Docker durci + `cleanup_orphan_workers` | `free/pool/__init__.py:4386`, `opencode.py:3559` |
| `free/egress_sources.py` | 424 | Sources déclaratives, parseurs, garde SSRF, `fetch_text` | `free/egress_runtime.py:30`, `free/egress_adapters/public.py:28`, `free/threat_intel.py:19`, `dashboard/routers/egress_catalog_mutations.py:110` |
| `free/egress_catalog.py` | 440 | Domaine pur : `EgressOption`, `transition`, `score_option`, TTL | 9 modules prod + dashboard |
| `free/egress_store.py` | 1078 | SQLite `registry.db` (`egress_*`) | `free/egress_runtime.py:361`, routeurs dashboard |
| `free/egress_runtime.py` | 479 | Composition root : store + adapters + scheduler + `sync_public_pool` | `opencode.py:4208` |
| `free/egress_scheduler.py` | 185 | Une boucle asyncio par provider, single-flight, jitter | `free/egress_runtime.py:449` |
| `free/egress_rotation.py` | 145 | Sélection (`select_many`), `rows_from_options` | `free/egress_runtime.py:28,217` |
| `free/egress_validation.py` | 143 | `ProbeOutcome`, `classify_outcome`, `parse_retry_after` | `free/egress_adapters/public.py:37` |
| `free/threat_intel.py` | 211 | 5 feeds plain-text → `egress_threat_intel` | `free/egress_runtime.py:32,368`, dashboard, `opencode.py:5281` |
| `free/catalog_claims.py` | 534 | Claims partagés (hostname ↔ station) + budget rebind | **script-only** : `scripts/fresh_chain.py:1969` |
| `free/scan_audit.py` | 297 | Journal SQLite des runs de scan | **script-only** : `scripts/fresh_chain.py:1999`, `fresh_chain_supervisor.py:3262` |
| `free/qualification/qualifiers.py` | 598 | Qualifieurs dédiés (stations 7-10) | `opencode.py:4232`, `dashboard/routers/qualification_routes.py:53,96` |
| `free/qualification/sweep.py` | 439 | Balayage de qualification (NordVPN, pas de partie publique) | `free/qualification/qualifiers.py:27`, `runtime.py:21` |
| `free/ip_registry/adapters/probe.py` | 499 | `StationProbe` (NordVPN/WARP), `classify_429_scope` | `free/qualification/station_adapter.py:161,209`, scripts |
| `free/ip_registry/sources/*` | 584 | Catalogue NordVPN multi-sources (API, cache, gluetun) | `free/ip_registry/catalog.py:25`, `status.py:18,323` |
| `free/pool/public_pool.py` | 111 | `public_subnet`, `select_public_rows` (cap /24, alternance v4/v6) | `free/pool/__init__.py:70-71` |
| `free/pool/__init__.py` | 8362 | Pool : `set_public_proxies`, `revalidate_public_proxies`, worker | `opencode.py:3533`, partout |
| `config/egress_sources.yaml` | 247 | **26 sources** déclaratives (23 actives, 3 désactivées) | `free/egress_runtime.py:408` |

Modules > 1 500 lignes dans la zone : **`free/pool/__init__.py` (8 362)**, **`free/egress_store.py` (1 078)** — seul le premier dépasse le seuil ; il est hors périmètre strict mais porte toute la logique publique.

### Flux réel : découverte → validation → claim → bind → probe → serve → retrait

```
DÉCOUVERTE (toutes les 21600 s, providers.public.discovery_interval_s)
  free/egress_runtime.py:449 EgressScheduler → PublicEgressAdapter.refresh (public.py:327)
    ├─ _reload_sources (public.py:263) : config/egress_sources.yaml (mtime) + egress_sources (DB)
    ├─ read_source_text (egress_sources.py:389) → fetch_text (370) [garde SSRF validate_source_url:325]
    │     └─ parse_payload (254) → normalize_proxy (86) [rejette privé/loopback/broadcast]
    ├─ interleave_by_source (288) + dedupe_candidates (264) → fenêtre de `cycle_candidate_budget`=500 (public.py:392-412)
    └─ revalidation : les options `usable` (max_active=64) + `reachable` (≥8) sont re-testées à CHAQUE cycle (public.py:425-438)

VALIDATION (une seule passe : liveness PUIS free gate)
  probe_public_candidates (public.py:52)
    ├─ start_worker_process (public_egress_worker.py:458) — 1 process par cycle, env scrubbé, python -I
    ├─ _read_egress (public.py:204) : GET https://api64.ipify.org via le worker → egress_ip
    ├─ _do_free_request_curl_cffi (opencode.py:11050) : wire free réel, 1 modèle "ping"
    └─ semaphore 12 (public.py:88) — `providers.public.concurrency: 48` est ÉCRASÉ par min(12, 48)
  → store.apply(Observation(FREE_GATE)) (public.py:614) → transition (egress_catalog.py:272)
     200 → usable (TTL 43200 s) | 429 → quota (+reset_at) | 401/407 → auth | 0/403/5xx → dead (TTL 604800 s)

CLAIM (pas de claim : le public n'a pas de station)
  Le pool public est alimenté par sync_public_pool (egress_runtime.py:219) → pool.set_public_proxies (pool:4324)
  Le claim exclusif (principe V) n'existe que côté NordVPN (free/catalog_claims.py, script-only)

BIND
  pool.set_public_proxies (pool:4324) : select_public_rows (cap max_active + max_per_24)
    → start_worker_container (container) ou start_worker_process (process)  ← config.yaml:262 = container
    → transport_url = handle.local_proxy_url(f"p{index}") (pool:4416) → HttpsProxyEndpoint.transport_url (pool:345)
    → _rebuild_https_list (pool:4277) → _rebuild_static_eps (pool:4737) → Socks5Endpoint/HttpsProxyEndpoint

PROBE (revalidation in-pool, distincte du catalogue)
  opencode.py:11308 _revalidate_public_proxies → _do_free_request_curl_cffi par endpoint
    → pool.revalidate_public_proxies (pool:4563) : 200/429 conservés, le reste retiré
    → ticker `refresh_interval_s` = 21600 (pool:4661) + POST /api/vpn/public-proxies/revalidate

SERVE
  opencode.py:15634 provider "public"/"static" → bucket "proxies"
  pool._socks5_client_usable (pool:3885) : egress_ip requis + non-managed ⇒ preuve d'egress suffit
  _do_free_request_curl_cffi (opencode.py:11122) : _guard_free_wire_headers → Bearer public
  _assert_secure_free_endpoint (opencode.py:11208) : https + *.opencode.ai obligatoires

RETRAIT
  clear_public_proxies (pool:4303) : arrêt différé du worker (300 s de grâce)
  purge (egress_store.py:930) : observations > retention_days=7 ; options `dead` > 30 j ; runs > 30 j
  _maintenance_loop (egress_runtime.py:169) : une passe par 24 h
```

---

## 3. Findings

### P0

#### P0-01 — Le gate public promeut un `200` sans contenu (violation VII)

**Preuve.** `free/egress_adapters/public.py:142-144` :
```python
free_status = int(getattr(response, "status_code", 0) or 0)
headers = dict(getattr(response, "headers", None) or {})
body = str(getattr(response, "text", "") or "")[:2_000]
```
`body` est lu **uniquement** pour `parse_retry_after` sur 429 (`:145-150`). Aucun contrôle de contenu, aucune détection de `event: error`. `CandidateProbe` n'a pas de champ `valid`/`content_ok`. Le probe ne lit même pas le corps en streaming : `_do_free_request_curl_cffi` est appelé avec `stream` non forcé (`timeout=(free_timeout, 60.0)`, `:138`) puis `response.text` est lu une fois.

**Contraste.** Le chemin client valide : `opencode.py:11292` rejette un SSE vide (`"free: empty SSE collection"`, 502) et `opencode.py:21830` intercepte `event: error`. Le collecteur accepte même un `200` avec seulement un `finish_reason` (vérifié : `_collect_chat_completion(['data: {"choices":[{"delta":{},"finish_reason":"stop"}]}'])` → `content: ""`, statut 200).

**Impact.** Constitution VII : « un `200` ne compte que si le flux a produit du contenu et n'a pas émis de trame `event: error` ». Le catalogue peut marquer `usable` une IP qui ne sert rien ; `select_many` la retient (score 40 % taux de 200 calculé sur un 200 creux) et `_seed_public_ledger_proofs` (`pool:4461-4503`) inscrit une **preuve ledger** (`note_model_ip(model, endpoint, 200)`) sur cette base — la preuve devient un faux positif propagé au bucket `proxies`. Mesuré : 3 125 observations `free_gate/usable` côté public contre 6 818 tous providers.

**Recommandation.** Ajouter un `content_ok`/`error_frame` au `CandidateProbe` (réutiliser `_validate_sse_frame`, `free/ip_registry/adapters/probe.py:330-395`, déjà écrit et testé) et refuser la promotion `usable` sans contenu. `classify_outcome` doit produire `dead` (ou un statut `empty`) pour un 200 creux. Effort : **M**.

---

#### P0-02 — Le catalogue jette les credentials des proxys authentifiés

**Preuve.** Chaîne complète, reproduite en exécutant le code réel :
```
normalize_proxy("http://user:pa55@1.2.3.4:8080") → url "http://user:pa55@1.2.3.4:8080"   (egress_sources.py:113-116)
new_option(host="1.2.3.4", port=8080, ...)       → EgressOption  (pas de champ username/password)
rows_from_options([opt])                         → url "http://1.2.3.4:8080"              (egress_rotation.py:96-100)
```
`EgressOption` (`free/egress_catalog.py:132-154`) n'a que `host`, `port`, `egress_ip`, `block` — aucune colonne credentials dans `egress_options` (`free/egress_store.py:48-70`). Le worker, lui, sait les porter : `Upstream.parse` lit `parsed.username`/`parsed.password` et construit `Proxy-Authorization` (`free/public_egress_worker.py:108-117`), et `pool.set_public_proxies` sait aussi les extraire (`pool:4405-4406`) — mais il ne les reçoit jamais du catalogue.

**Impact.** Tout proxy authentifié (une part notable des listes publiques : `zloi`/`hideip.me`, ProxyScrape, Geonode) est parsé, testé avec succès via le worker (qui a l'URL complète), promu `usable`, puis **re-servi sans auth** — 407 upstream systématique. Le catalogue accumule des options `usable` structurellement inutilisables, et le pool les publie. C'est aussi une asymétrie de sécurité : l'URL complète circule dans le worker mais la version catalogue est tronquée, donc aucun log ne peut expliquer l'échec.

**Recommandation.** Ajouter `username`/`password` (ou `auth`) à `EgressOption` + colonne `egress_options`, les propager dans `rows_from_options`, et les rédiger systématiquement en sortie API/log (le mécanisme existe : `redact_proxy_url`, `free/public_proxies.py:209`). Effort : **M**.

---

#### P0-03 — Un candidat non testé est marqué `dead` 7 jours (famine auto-entretenue)

**Preuve.** `free/egress_adapters/public.py:546-567` : la boucle itère sur **tous** les `candidates` ; pour ceux absents de `probed` (`outcome is None`) elle écrit une observation `liveness` avec `result=dead` :
```python
if outcome is None or not outcome.egress_ip:
    ...
    self._record(store, option, CandidateProbe(url=..., error="no probe"),
                 policy, run_id, phase=ObservationPhase.LIVENESS)
    result.dead += 1
```
`transition` (`free/egress_catalog.py:313-318`) mappe tout résultat non reconnu sur `DEAD` avec `expires = now + policy.ttl_dead_s`. Le `deadline_s` du probe (`public.py:528` : `max(10, cycle_timeout_s - 10)`) annule les tâches en retard (`:180-183`), donc `probed` est presque toujours plus petit que `candidates`.

Prouvé par exécution : `new_option(...)` puis `Observation(phase=LIVENESS, status=0, result="candidate")` → `status='dead'`, `expires_at - now = 604800 s`.

**Mesures live.**
| Mesure | Valeur |
|---|---|
| options `public` en base | 30 647 |
| dont `dead` avec `egress_ip=''` | **26 474** (86 %) |
| observations `liveness/candidate` | **127 405** (le plus gros volume du catalogue) |
| runs publics consécutifs | 6, tous `partial`, `cycle partiel: 38-41 candidat(s) non testé(s)` |
| promotions par cycle | 0, 4, 6, 9, 13, 14 |

Le cycle consomme ~440-460 candidats, en promeut ~10, et **gèle 7 jours** les ~40 non testés — qui reviennent dans la fenêtre de 500 au cycle suivant (le curseur `public_cursor` avance de `limit` sur une liste de 20 000). Le catalogue ne converge pas.

**Impact.** Constitution I (« la disponibilité se mesure, elle ne se déclare pas ») : une absence de mesure est convertie en condamnation. Le ratio promotions/candidats reste sous 3 % alors que 1 415 options sont déjà `usable` — le catalogue sature de lignes `dead` non informatives, et le pool ne dispose que de ~176 options fraîches (voir P1-01).

**Recommandation.** Ne jamais écrire `dead` sur une absence de résultat : soit ne rien écrire (le candidat reste `candidate`, TTL 300 s, `public.py:242`), soit un statut dédié `untested` sans pénalité. Réserver `dead` au refus explicite (timeout liveness confirmé, 403, 5xx). Effort : **S**.

---

#### P0-04 — Le worker de probe contourne l'isolation conteneur

**Preuve.** `config.yaml:262` : `isolation: container` (vérifié live : `/api/vpn/public-proxies` → `"isolation":"container"`, `"kind":"ContainerWorkerHandle"`). Mais le catalogue utilise **toujours** le chemin process :
```python
# free/egress_adapters/public.py:69-76
from free.public_egress_worker import start_worker_process
handle = await asyncio.to_thread(start_worker_process, upstreams)
```
`probe_public_candidates` n'a aucun paramètre d'isolation ; `PublicEgressAdapter.__init__` (`:223-261`) n'en reçoit pas non plus ; `egress_runtime.py:402-411` ne le transmet pas. Le chemin conteneur n'est câblé que dans `pool.set_public_proxies` (`pool:4385-4392`).

**Impact.** Chaque cycle (~450 candidats, 6 runs/heure) ouvre jusqu'à 450 connexions sortantes vers des proxys arbitraires depuis un process Python **de l'hôte**, lancé avec `worker_env()` (`public_egress_worker.py:68-72`) qui conserve `HOME`, `USERPROFILE`, `TEMP`, `PATH` et le cwd du dépôt (`cwd=str(tmpdir)` mais `sys.executable` de l'hôte). Le durcissement documenté (pas de montage, `--read-only`, `--cap-drop=ALL`, uid 65534, `--pids-limit`) ne s'applique pas au trafic le plus exposé : celui qui parle à des proxys publics inconnus. Le `KEEP_ENV` est volontairement minimal mais reste le Python système sans sandbox.

**Recommandation.** Transmettre `isolation`/`container_image` jusqu'à `probe_public_candidates` (paramètre ou fabrique injectée) et utiliser `start_worker_container` quand `isolation == container`. À défaut, documenter explicitement que la probe est hors isolation. Effort : **M**.

---

### P1

#### P1-01 — 1 239 options `usable` ont un TTL expiré et restent dans la fenêtre de revalidation

**Preuve.** Live : `SELECT COUNT(*) ... status='usable' AND expires_at<=now` → **1 239**. `is_selectable` les refuse (`egress_catalog.py:352`) et `summary()` les reclasse en `expired` (`egress_store.py:706-714`) — la logique est correcte. Mais la **revalidation** les reprend : `public.py:425-431` liste `status=USABLE, limit=policy.max_active (64), oldest_first=True`. Sur 1 415 `usable`, 64 par cycle sont re-testées ; à 6 cycles/heure, un tour complet prend ~3,7 h pour un TTL de 12 h. Les 1 239 expirées consomment donc le quota de revalidation et étouffent les preuves fraîches.

**Impact.** Le pool est alimenté par `select_many` sur les seules non-expirées : `public_rows()` retourne 64 + jusqu'à 64 IPv6 (`egress_runtime.py:192-206`), dont 176 réellement `usable` fraîches. La fenêtre `oldest_first` re-teste d'abord les plus périmées — dont beaucoup ne reviendront jamais (`dead`/`quota`) — au détriment des preuves qui expirent bientôt.

**Recommandation.** Séparer la fenêtre : re-tester d'abord les `usable` dont `expires_at - now < revalidate_interval_s` (les « bientôt périmées »), puis purger/expirer les autres au lieu de les re-prober. Effort : **S**.

---

#### P1-02 — `_is_denied` reconstruit les réseaux et balaie 6 369 réseaux threat-intel par appel

**Preuve.** `free/egress_runtime.py:107-128` :
```python
def _is_denied(self, option) -> bool:
    networks = []
    for raw in self.denylist:                      # reparsé à CHAQUE appel
        networks.append(ipaddress.ip_network(...))
    ...
    if self.threat_intel is not None:
        for value in (option.host, option.egress_ip):
            if value and self.threat_intel.matches(str(value)):   # 6 369 réseaux linéaires
```
Mesuré sur la base live : `len(ti._networks) = 6 369`, `len(ti._exact) = 142 838`, un appel = **1,399 ms**. Appelé par option dans `public_rows()` (`:183-186, 202`), dans `sync_public_pool` (via `public_rows`) et **5 000 fois** dans `block_indicator` (`:138-158`) — soit ~7 s pour un blocage manuel.

**Impact.** `sync_public_pool` tourne après **chaque** cycle public (6×/h, `egress_runtime.py:465-475`) ; `block_indicator` est un endpoint dashboard synchrone (`egress_catalog_mutations.py:76-79`). Le coût croît avec les feeds (ipsum seul = 141 785 indicateurs) et n'est jamais caché alors que `ThreatIntel` a un `_loaded_at` — c'est la **boucle d'appartenance** qui n'est pas indexée.

**Recommandation.** Mémoïser `networks` dans `EgressRuntime` (invalidation sur `block_indicator`), et remplacer le balayage linéaire par un tri par préfixe + `bisect` sur les adresses réseau. Effort : **M**.

---

#### P1-03 — `StationProbe.__init__` exige un fichier gitignoré

**Preuve.** `free/ip_registry/adapters/probe.py:88-90` :
```python
self._body_v1, self._body_raw_v1 = self._load_payload(str(PAYLOAD_PATH) + ".bak")
```
`free/official_payload.json.bak` est ignoré (`gitignore:91` `free/*.json.bak`) et non suivi (`git ls-files` → erreur). Prouvé : renommage du fichier → `StationProbe() RAISES: FileNotFoundError`. Aucun `try` : `_load_payload` (`:397-411`) ouvre directement.

**Impact.** Un checkout neuf, un conteneur Docker (`Dockerfile:58` `COPY free/ ./free/` — mais `.dockerignore` ne l'exclut pas explicitement, il n'est simplement pas dans git) ou une CI casse la qualification et `scripts/fresh_chain.py:2888`, qui construisent `StationProbe` sans garde. Le `.bak` est produit par `scripts/opencode_client_sync.py:598` (`shutil.copyfile`), donc son existence dépend d'une action manuelle sur la machine.

**Recommandation.** Rendre le `.bak` optionnel (fallback sur `PAYLOAD_PATH`) et tracer la dégradation, ou versionner une capture 1.19.5 légitime. Effort : **S**.

---

#### P1-04 — `psutil` non déclaré ⇒ les workers de probe orphelins ne sont jamais nettoyés

**Preuve.** `free/public_egress_worker.py:436-439` :
```python
try:
    import psutil
except Exception:  # noqa: BLE001 - psutil is optional on this host
    return 0
```
`requirements.txt` ne contient pas `psutil` (vérifié : grep vide). Le commentaire du même fichier (`:427-434`) documente le problème qu'il résout : « the workers keep listening on loopback forever (one per cycle, ~30 after 15 h) ». `opencode.py:3570` appelle ce nettoyage à chaque boot.

**Impact.** Sur toute installation construite depuis `requirements.txt`, chaque crash/redémarrage laisse les workers de probe accumulés. Ils écoutent sur loopback avec des URLs de proxys publics en argv (`--config <tmpdir>/config.json`, fichier supprimé mais le chemin reste). Le nettoyage conteneur (`cleanup_orphan_workers`, `public_egress_container.py:100`) utilise Docker et fonctionne — l'asymétrie est précisément sur le mode process.

**Recommandation.** Ajouter `psutil` à `requirements.txt` (déjà installé sur cette machine : 7.2.2) ou remplacer par une lecture `/proc` + `tasklist` sans dépendance. Effort : **S**.

---

#### P1-05 — La station d'identité free lève au premier appel après démarrage

**Preuve.** Reproduit en exécutant le code réel :
```
call1: profile=None    would_raise=True
call2: profile=oc-2.2.5-cli-13  would_raise=False
```
Cause : `ProfileRegistry.verified()` (`free/profile_registry.py:408-414`) appelle `profile.refresh_status(now)` qui **mute** le statut en place (`:242-252`) ; les deux profils du registre ont `expires_at` au **2026-11-13** (vérifié : `EXPIRED`), donc le premier `verified()` retourne `[]`. `select_for_station` retourne `None`, puis le fallback `canonical()` (`:416-423`) — appelé **après** que `verified()` a re-renouvelé les statuts (`passes >= MIN_PASSES=2` → `status=verified`, `expires_at = now + ttl_s`) — réussit. D'où le succès au 2ᵉ appel.

`opencode.py:11105-11108` :
```python
if _station_identity_enabled() and station is not None:
    _station_runtime = _station_identity_runtime(station)
    if _station_runtime is not None and _station_runtime.profile is None:
        raise RuntimeError("free station identity: aucun profil vérifié")
```
`free.station_identity: true` (`config.yaml:1051`, vérifié via `yaml_get`). Le même motif existe en `opencode.py:14089-14091`.

**Impact.** La **première** requête free après chaque démarrage du proxy (ou après chaque `mtime` changé sur `verified_profiles.json`, qui réinitialise `_STATION_PROFILE_REGISTRY`, `opencode.py:10831`) lève un `RuntimeError`. Sur le chemin stream, `opencode.py:14312-14318` le convertit en `_FreeTunnelFailure("station identity: pas de fallback direct")` — donc pas de repli direct, la requête échoue. Le registre se répare ensuite tout seul, ce qui explique l'absence de trace durable : 0 occurrence de « aucun profil » dans `logs/debug.log` (le log est `_debug`-gated).

**Recommandation.** Rendre `verified()` non destructif (calculer le statut effectif sans muter, ou rafraîchir le registre au chargement via `refresh_status` avant toute lecture), et re-tester les profils expirés au boot plutôt que de dépendre du 2ᵉ appel. Effort : **S**.

---

#### P1-06 — Capture 1.19.5 supprimée mais toujours référencée

**Preuve.** `free/profiles/verified_profiles.json:26` → `"body_capture": "free/captures/1.19.5.json"`. Le fichier n'existe pas (`git status` : ` D free/captures/1.19.5.json` ; `ls` ne montre que `2.2.5.json`, `2.3.0.json`). `_capture_body` (`free/runtime_identity.py:116-128`) avale l'erreur :
```python
except Exception:
    return None
```
puis `build_body` (`:182-186`) retombe sur `template_official_body`.

**Impact.** `oc-1.19.5-desktop-11` est l'un des deux profils `verified` et celui que `select_for_station` retient dans la majorité des tirages (`runtime_profile_versions` → `('2.2.5','1.19.5')`). Les stations qui reçoivent ce profil servent un corps **templaté** au lieu de la capture, ce qui est exactement le « wire mismatch » que le commentaire de `probe.py:83-87` décrit comme donnant un faux 429. Aucun log, aucun compteur : la dégradation est invisible. (Déjà signalé côté WARP — `04-warp-egress.md` §1.10 ; l'impact est ici sur **tous** les chemins, y compris le public via `_FREE_CANONICAL_VERSION`.)

**Recommandation.** Soit restaurer la capture 1.19.5, soit retirer/remapper le profil, soit logger la dégradation dans `_capture_body`. Effort : **S**.

---

#### P1-07 — Un proxy `http://` en clair est promu `usable` et peut entrer au pool

**Preuve.** `normalize_proxy` accepte `ALLOWED_SCHEMES = ("http", "https", "socks5", "socks5h")` (`free/egress_sources.py:21`). Live : `SELECT kind, COUNT(*) ... status='usable'` → `http: 112`, `https: 1 281`, `socks5: 22`. `select_public_rows` accepte les quatre schémas (`free/pool/public_pool.py:13`). Le hop **proxy** est alors en clair ; le TLS vers `opencode.ai` reste vérifié (`_assert_secure_free_endpoint`), donc le corps est protégé, mais l'URL cible (`CONNECT opencode.ai:443`) et les métadonnées sont lisibles par un intermédiaire.

**Impact.** Un `http://` non authentifié sur un réseau hostile est un vecteur d'injection de `407`/`502` forgés et, combiné à P0-01 (aucune validation de contenu), un moyen de faire promouvoir une IP contrôlée par l'attaquant. `free/captures/README.md` et `PLAN §2.4` ne mentionnent pas de restriction de schéma ; `docs/EGRESS_SOURCES.md` liste des sources `http` comme « veille ».

**Recommandation.** Exclure `http` du pool servi (ou l'exiger seulement pour la découverte), et documenter la règle dans `PLAN`/`EGRESS_SOURCES.md`. Effort : **S**.

---

#### P1-08 — Le cycle est structurellement partiel (budget vs deadline)

**Preuve.** `cycle_candidate_budget: 500` (`config.yaml:1110`), `cycle_timeout_s: 600` (`:1111`), mais `probe_public_candidates` reçoit `deadline_s = max(10, cycle_timeout_s - 10) = 590` (`public.py:528`) et le budget réel par candidat est de 2 requêtes séquentielles (ipify puis gate free, `:99-141`) bornées à `12 s + (free_timeout + 5)`. Avec `Semaphore(12)` (`:88` — le `concurrency: 48` de `config.yaml:1140` est ignoré), le débit maximal théorique est ~12 candidats/2 s, soit ~3 500 candidats sur 590 s ; en pratique les proxys publics répondent en dizaines de secondes (mesuré : `last_latency_ms` 15 703 ms sur une option `reachable`).

**Mesures live (6 runs consécutifs)** : 454/457/438/446/406/446 candidats, durées 357/333/344/355/308/347 s, **tous** `partial` avec « 38-41 candidat(s) non testé(s) ». Le cycle s'arrête sur `deadline_s`, pas sur le budget.

**Impact.** Combiné à P0-03, chaque cycle gèle ~40 candidats pour 7 jours. Le catalogue ne peut pas dépasser ~10 promotions par cycle, et la dette de `dead` croît de ~40 par cycle (~240/h).

**Recommandation.** Aligner le budget sur la capacité réelle (mesurer, puis baisser `cycle_candidate_budget` à ~150) ou augmenter `cycle_timeout_s` ; surtout, ne pas convertir les non-testés en `dead` (P0-03). Effort : **S**.

---

#### P1-09 — `rows_from_options` force `last_status = 200` par défaut

**Preuve.** `free/egress_rotation.py:105` :
```python
"last_status": int(option.last_status or 200),
```
Une option dont `last_status` vaut 0 (jamais observée) est publiée comme `200`. Cette valeur alimente `_seed_public_ledger_proofs` (`pool:4473-4479` : filtre `last_status == 200`) qui appelle `note_model_ip(model, endpoint, 200)` — donc une **preuve ledger** est créée depuis une valeur par défaut.

**Impact.** En pratique `select_many` ne retient que des `usable` (dont `last_status` est 200, vérifié : 0 ligne `usable` avec `last_status != 200`), donc l'impact est latent. Mais le défaut est un « 200 par défaut » dans un système dont la constitution (VII) interdit précisément cela, et il masque un futur bug de provenance (une option `reachable` promue par erreur publierait `200`).

**Recommandation.** Remplacer par `int(option.last_status or 0)` et laisser `_seed_public_ledger_proofs` refuser 0. Effort : **S**.

---

#### P1-10 — `local_text` accepte une traversée de répertoire

**Preuve.** `SourceDefinition.from_config` ne valide l'URL que si `source_type != "local_text"` (`free/egress_sources.py:48`), et `read_source_text` fait `Path(source.url)` sans contrainte (`:395-399`). Reproduit :
```
SourceDefinition.from_config({"id":"x","type":"local_text","url":"../../../Windows/win.ini"})
  → accepté
read_source_text(s) → lit C:\Users\julie\Downloads\opencode-proxy-one\..\..\..\Windows\win.ini
```
Le garde SSRF existe et fonctionne pour les sources distantes (vérifié : `http://127.0.0.1:8080/x` → `ValueError: hôte de source interdit: '127.0.0.1'`).

**Impact.** `POST /api/egress/catalog/sources` (`dashboard/routers/egress_catalog_mutations.py:100-116`) accepte un corps arbitraire et appelle `SourceDefinition.from_config(body)` — la validation de type passe, puis `store.upsert_source` persiste. Le prochain cycle lit le fichier désigné et en parse les lignes comme des candidats `host:port`. Un opérateur authentifié (ou un CSRF sur le dashboard, atténué par `DashboardTrustMiddleware`) peut donc faire lire un fichier arbitraire de la machine ; le contenu ne revient pas à l'attaquant (il est parsé comme des proxys), donc l'exfiltration est indirecte — mais la primitive d'accès est réelle, et `block_indicator`/les logs peuvent en révéler des fragments.

**Recommandation.** Restreindre `local_text` à un répertoire racine (`config/`) et rejeter `..` ; exiger que le chemin résolu soit sous `ROOT`. Effort : **S**.

---

#### P1-11 — Les workers de probe orphelins portent les URLs des proxys en argv

**Preuve.** `start_worker_process` (`free/public_egress_worker.py:458-524`) crée `config.json` avec `"upstreams": upstreams` (les URLs publiques complètes) puis lance `python -I public_egress_worker.py --config <tmpdir>/config.json`. Le fichier est supprimé **après** démarrage (`:522-523`), mais `cleanup_orphan_worker_processes` (`:445`) identifie les orphelins par `"oc-public-egress-" not in cmdline` — la commande **reste** lisible dans la liste des processus de l'hôte pendant toute la durée de vie du worker.

**Impact.** Sur une machine multi-utilisateurs, `ps`/`Get-CimInstance` expose la liste des proxys publics utilisés. `redact_proxy_url` n'est jamais appliqué ici. Les credentials (si P0-02 est corrigé) seraient exposés de la même façon.

**Recommandation.** Passer le token et les upstreams par un descripteur de fichier hérité ou stdin plutôt que par un chemin en argv, ou effacer l'argv (`setproctitle`) ; à défaut, documenter. Effort : **M**.

---

### P2

#### P2-01 — `/health` du worker non authentifié

`free/public_egress_worker.py:340-349` : `if data.startswith(b"GET /health ")` répond `200 {"ok":true}` **avant** le contrôle `Proxy-Authorization` (`:351-355`). Le worker écoute sur `127.0.0.1` uniquement (`listen_host="127.0.0.1"`, `:474`) — sauf en mode conteneur où `listen_host="0.0.0.0"` mais le port est publié sur `127.0.0.1` (`-p 127.0.0.1:{host_port}:{internal}`, `public_egress_container.py:191`). Impact limité à un oracle de vivacité local. Recommandation : exiger le token aussi sur `/health`. Effort : **S**.

#### P2-02 — `_read_egress` accepte n'importe quel texte ≤ 64 octets comme IP

`free/egress_adapters/public.py:215-217` : `if status == 200 and text and len(text) <= 64: return text, status`. Aucun `ipaddress.ip_address()`. Un proxy qui répond `"<html>blocked</html>"` (≤ 64) est accepté comme egress. `new_option` normalise ensuite via `_normalize_ip` (`egress_catalog.py:248-255`) qui **retourne la valeur brute** en cas d'échec — donc `egress_ip="blocked"` entre au catalogue, `option_block` le reprend (`:203`), `public_subnet` retourne `""` (`public_pool.py:20-25`) et le cap /24 est contourné. Live : 0 valeur non-IP actuellement (vérifié), donc défaut latent. Recommandation : valider avec `ipaddress.ip_address`. Effort : **S**.

#### P2-03 — Aucune vérification que l'egress public diffère de l'IP de l'hôte

Le chemin NordVPN/WARP compare l'egress à `host_public_ip()` (`free/ip_registry/adapters/egress.py:44-52`, `forbidden_ips` câblé en `fresh_chain.py:2876-2886`) : « A station that reports the host's own address is not tunnelled […] and must never be accepted as a fresh egress. » Le chemin public **n'a aucun équivalent** (grep `forbidden|host_public_ip|host_ip` sur les 5 modules publics → aucun résultat). Un proxy public configuré sur `127.0.0.1`/l'IP de l'hôte, ou un proxy qui reflète l'adresse du client, serait promu et servirait avec l'IP réelle de la machine — exactement le scénario que le garde NordVPN existe pour empêcher. `_public_host` (`egress_sources.py:126-144`) rejette bien loopback/privé pour la cible du proxy, mais pas l'egress observé. Recommandation : injecter `host_public_ip()` comme `forbidden` dans `probe_public_candidates`. Effort : **S**.

#### P2-04 — `deadline_s` du probe ignore les candidats non lancés

`free/egress_adapters/public.py:170-183` crée **toutes** les tâches immédiatement puis `asyncio.wait(timeout=deadline_s)` annule les restantes. Avec ~450 candidats et un sémaphore à 12, la majorité n'a jamais commencé : `_one` est annulé avant d'acquérir le sémaphore, et le `finally` de `_one` n'existe pas — l'annulation est propre, mais ces candidats deviennent `dead` (P0-03). Un ordonnancement par vagues avec `as_completed` + deadline glissante éviterait de compter les non-lancés comme testés. Effort : **M**.

#### P2-05 — `sync_public_pool` peut vider le pool sur un catalogue vide

`free/egress_runtime.py:465-475` :
```python
await asyncio.to_thread(runtime.sync_public_pool, None, clear_if_empty=not result.errors)
```
`clear_if_empty=True` dès qu'il n'y a **aucune** erreur. Or un cycle sans erreur mais sans candidat (`public.py:511-513` produit une erreur « aucun candidat public hors quarantaine », donc protégé) ou un cycle où tous les candidats sont `dead` **sans** erreur (P0-03 en produit 400 par cycle, sans `result.errors`) déclenche `pool.clear_public_proxies()` → les 3 endpoints actifs sont retirés et le worker arrêté avec 300 s de grâce. Le docstring promet l'inverse (« A failed/empty catalog never clears the pool »). Recommandation : `clear_if_empty=False` ou n'effacer que si le catalogue a effectivement perdu ses `usable`. Effort : **S**.

#### P2-06 — Les sources retirées du YAML ne sont jamais purgées de la base

`free/egress_adapters/public.py:285-302` relit `store.list_sources()` et **remplace** `self.sources` par ce que contient la base. `_store_sources` (`:304-325`) ne fait qu'`upsert`. `EgressStore.delete_source` (`egress_store.py:279`) n'a **aucun appelant** (grep repo-wide : définition seule). Une source ajoutée par l'API puis supprimée du YAML reste active indéfiniment. Live : `db-only ids: []`, `yaml-only ids: []` — cohérent aujourd'hui, mais aucun mécanisme ne le garantit. Recommandation : purger les ids absents du fichier lors du reload, ou ajouter un endpoint de suppression. Effort : **S**.

#### P2-07 — `local_text` (liste manuelle) est vide

`config/egress_proxies.txt` ne contient que des commentaires (11 lignes, 0 entrée). Live : `manual_proxies` → `candidates_count = 0`, `usable_count = 0`. La source est prioritaire (`priority: 110`, `config/egress_sources.yaml:19`) et rechargée toutes les 300 s — coût nul mais un canal documenté comme « source de vérité opérateur » qui n'apporte rien. Recommandation : soit l'alimenter, soit la désactiver pour clarifier l'inventaire. Effort : **S**.

#### P2-08 — `total` des observations publiques : 127 405 lignes `liveness/candidate` non informatives

`egress_observations` contient 326 371 lignes dont **127 405** `liveness/candidate` pour le seul provider public (P0-03), plus 97 001 `egress/dead` (majoritairement WARP, cf. `04-warp-egress.md` §1.5). `registry.db` = 167 Mo. La purge (`egress_store.py:930-947`) n'efface que les observations > `retention_days=7` ; live : **24 327 observations plus vieilles que 7 jours** subsistent (la maintenance ne tourne qu'une fois par 24 h, `egress_runtime.py:173`). Recommandation : après P0-03, élaguer et vérifier que la maintenance tourne réellement (aucune trace de `purge` dans les logs). Effort : **S**.

#### P2-09 — Le token du worker transite dans l'URL du proxy

`local_proxy_url` construit `http://<upstream_id>:<token>@127.0.0.1:<port>` (`public_egress_worker.py:321-326`, `public_egress_container.py:74-77`). Le token (24 octets urlsafe) apparaît dans `transport_url`, donc dans les objets endpoint du pool. Les routes dashboard le filtrent explicitement (`tests/test_egress_mode_routes.py:86`, `test_vpn_endpoints.py:807`) et `egress_catalog_routes.py` sérialise `asdict(option)` — qui ne contient pas `transport_url` (champ du pool, pas du catalogue), donc la fuite est contenue. Défaut de conception acceptable (Basic proxy auth), mais toute future route qui sérialiserait un endpoint doit être vérifiée. Recommandation : documenter l'invariant + un test de non-régression global. Effort : **S**.

#### P2-10 — `assert_untrusted_proxy_safe` n'est jamais appelé avec de vrais en-têtes ailleurs qu'en interne

`free/public_proxies.py:140` est exporté et appelé uniquement depuis `sanitize_free_headers` (`:180,185`) et les tests. `opencode.py` importe `is_untrusted_proxy`/`sanitize_free_headers`/`assert_secure_endpoint`/`redact_proxy_url` — jamais `assert_untrusted_proxy_safe` directement. Ce n'est pas un bug (l'appel interne couvre les deux passes) mais l'API publique suggère une défense en profondeur qui n'existe pas au point d'envoi. Recommandation : soit le retirer de `__all__`, soit l'appeler en dernier recours juste avant `sess.post`. Effort : **S**.

#### P2-11 — `probe_public_candidates` ne journalise pas les échecs de worker

`free/egress_adapters/public.py:77-84` : si `start_worker_process` échoue, **tous** les candidats reçoivent `error="worker: ..."` et deviennent `dead` (via `_record` avec `liveness_only=True`). Aucun `result.errors` n'est ajouté par ce chemin — l'échec du worker (port occupé, Python cassé) est indistinguable d'un cycle normal. Recommandation : remonter l'erreur dans `RefreshResult.errors` pour que `clear_if_empty=not result.errors` (P2-05) protège le pool. Effort : **S**.

#### P2-12 — `concurrency: 48` est un réglage mort

`config.yaml:1140` déclare `concurrency: 48` ; `free/egress_adapters/public.py:88` clampe à `min(12, ...)` — la valeur effective est toujours 12. Aucun avertissement. Combiné à P1-08 (cycle partiel), l'opérateur croit avoir 4× plus de parallélisme. Recommandation : soit honorer la valeur, soit clamper la config au chargement et le tracer. Effort : **S**.

---

### P3

#### P3-01 — `_last_discovery_at` est un attribut mort
`free/egress_adapters/public.py:256` initialise `self._last_discovery_at = 0.0`, jamais relu ni écrit ailleurs (grep : 1 occurrence). Reliquat d'une cadence de découverte abandonnée au profit de `_source_last` (`:257`). Supprimer. Effort : **S**.

#### P3-02 — Commentaires trompeurs sur le gate public
`free/egress_adapters/public.py:60-64` (« The free gate reuses the exact production wire ») et `:3-5` (« No candidate is promoted from liveness alone ») sont vrais pour le **statut** mais faux pour le **contenu** (P0-01). `free/pool/__init__.py:4566` dit « Le validateur est fourni par opencode (wire free réel, capture 2.2.5) » alors que `_revalidate_public_proxies` (`opencode.py:11308-11355`) ne lit que `status_code`. Effort : **S**.

#### P3-03 — Duplication `public_pool_rows` / `EgressRuntime.public_rows`
`free/egress_rotation.py:73-88` (`public_pool_rows`) est un quasi-doublon de `free/egress_runtime.py:179-217` (`public_rows`), sans appelant en production (`logs/inspect_select.py:34` et `tests/test_egress_rotation.py:71` seulement). `public_rows` ajoute la denylist et l'équilibrage IPv6. Recommandation : supprimer `public_pool_rows` ou le faire déléguer. Effort : **S**.

#### P3-04 — `interleave_by_source` + `dedupe_candidates` sur le plafond catalogue, pas le budget
`free/egress_adapters/public.py:399-401` : les deux passes utilisent `catalog_cap = max_options (20 000)` alors que la fenêtre utile est `limit` (500). Le tri/round-robin porte donc sur 20 000 lignes à chaque cycle pour n'en garder que 500. Mesuré : `egress_sources` cumule ~80 000 candidats/cycle (`ercin_socks5` 30 530, `openproxylist_socks5` 8 957…). Recommandation : passer `limit=limit`. Effort : **S**.

#### P3-05 — `source_signature` jamais appelée
`free/egress_sources.py:403-404`, exportée (`:422`), zéro appelant en production ou en test (grep : 2 occurrences = définition + `__all__`). Effort : **S**.

---

## 4. Inventaire du code mort

| Symbole | Fichier:ligne | Preuve d'absence d'appelant |
|---|---|---|
| `source_signature` | `free/egress_sources.py:403` | `grep -rn "source_signature" --include=*.py .` → 2 hits : définition + `__all__` |
| `reset_from_retry_after` | `free/egress_catalog.py:416` | `grep -rn "reset_from_retry_after"` → définition + `__all__` + `tests/test_egress_catalog.py:145-147` |
| `liveness_result` | `free/egress_validation.py:117` | `grep -rn "liveness_result"` → définition + `__all__` |
| `normalize_observation_payload` | `free/egress_validation.py:127` | `grep -rn "normalize_observation_payload"` → définition + `__all__` |
| `ttl_for` | `free/egress_catalog.py:258` | `grep -rn "\bttl_for\b"` → définition + `__all__` |
| `provider_status` | `free/egress_rotation.py:113` | `grep -rn "provider_status"` → définition + `__all__` (les autres hits sont un nom d'index SQL) |
| `public_pool_rows` | `free/egress_rotation.py:73` | seuls `logs/inspect_select.py:34` + `tests/test_egress_rotation.py:71` |
| `_last_discovery_at` | `free/egress_adapters/public.py:256` | `grep -rn "_last_discovery_at"` → 1 hit (initialisation) |
| `PROBE_VERSIONS` | `free/ip_registry/adapters/probe.py:26` | prod : 0 ; seuls `tests/test_ip_registry_probe.py:11,292` |
| `EgressStore.delete_source` | `free/egress_store.py:279` | `grep -rn "delete_source"` → définition seule |
| `EgressStore.slots` | `free/egress_store.py:920` | `grep -rn "\.slots()"` → 0 hit hors tests |
| `ScanAudit.snapshot` | `free/scan_audit.py:265` | prod : 0 ; `tests/test_scan_audit.py:25,45,64,77` |
| `CatalogClaims.stats` | `free/catalog_claims.py:525` | prod : 0 ; `tests/test_catalog_claims.py:28,344` |
| `PROBE_225` / `free/proxy_scheduler.py` | PLAN §1.1/§6 | fichiers **absents** : `free/proxy_scheduler.py`, `scripts/import_public_proxies.py` (vérifié `ls`) |
| `HttpStationProbe` | `free/qualification/station_probe.py:20` | prod : 0 ; `scripts/qualification_daemon.py:22` (script non invoqué par le serveur) |
| `SourceRegistry.capabilities/catalog_order` | `free/ip_registry/sources/registry.py:51,60` | prod : via `free/ip_registry/status.py:337-345` (affichage read-only du dashboard) |

Non-morts mais **script-only** (donc hors chemin serveur) : `free/catalog_claims.py` (534 l.) et `free/scan_audit.py` (297 l.) — appelés uniquement par `scripts/fresh_chain.py` / `fresh_chain_supervisor.py`, eux-mêmes lancés en sous-process par `opencode.py:2395`.

`TODO`/`FIXME`/`HACK`/`XXX`/`DEBUG` : **aucun** dans les 11 modules de la zone (grep ciblé, seuls des faux positifs `_template_fingerprint`). `print()` : aucun.

---

## 5. État réel des sources d'egress

Vérification : 10 requêtes HEAD, timeout 5 s, le 2026-11-20. État interne lu depuis `egress_sources` (live).

### Vivantes et productives (ont promu ≥ 1 `usable`)

| Source | HTTP | candidats | `usable` | `yield` |
|---|---:|---:|---:|---:|
| `proxifly_https` | 200 | 925 | **164** | 17,7 % |
| `zloi_https` (hideip.me) | 200 | 891 | **166** | 18,6 % |
| `thespeedx_http` | 200 | 2 960 | 73 | 2,5 % |
| `geonode_api` | 200 | 500 | 31 | 6,2 % |
| `openproxylist_https` | 200 | 6 709 | 8 | 0,1 % |
| `proxifly_socks5` | 200 | 5 567 | 6 | 0,1 % |
| `zloi_socks5` | 200 | 103 | 6 | 5,8 % |
| `monosans_socks5` | 200 | 82 | 3 | 3,7 % |
| `openproxylist_socks5` | 200 | 8 957 | 2 | 0,02 % |
| `hookzof_socks5` | 200 | 5 567 | 1 | 0,02 % |
| `thespeedx_socks5` | 200 | 2 423 | 1 | 0,04 % |
| `proxyscrape_socks5` | 200 | 914 | 1 | 0,1 % |
| `vakhov_fresh` | 200 | 555 | 0 | 0 % (priorité 95, JSON) |
| `ercin_https` / `ercin_socks5` | (non testé) | 3 255 / 30 530 | 0 / 0 | 0 % |
| `jetkai_https` / `jetkai_socks5` | (non testé) | 2 161 / 405 | 0 | 0 % |
| `proxyscrape_https` | 200 | 1 845 | 0 | 0 % |
| `roosterkid_https` / `_socks5` | 200 | 125 / 6 | 0 | 0 % |
| `shifty_https` / `shifty_socks5` | (non testé) | 13 / 279 | 0 | 0 % |
| `manual_proxies` (`local_text`) | local | **0** | 0 | liste vide |

### Mortes / douteuses

| Source | Preuve | Verdict |
|---|---|---|
| `vpslab_https` | **HTTP 404** (vérifié) | morte, `enabled: false` — correct |
| `mmpx12_https` / `mmpx12_socks5` | **HTTP 200** mais le YAML les déclare « préfixe Cloudflare error code 502/1015 » (`config/egress_sources.yaml:221-237`) | **douteuse** : le 200 renvoie probablement une page d'erreur Cloudflare, que `parse_public_text` transformerait en 0 candidat. `enabled: false` — correct, mais le motif de désactivation n'est plus vérifiable (le endpoint répond 200). |
| `ercin_socks5` (30 530 candidats) | 0 `usable` après plusieurs cycles | **douteuse** : volume énorme, rendement nul. Consomme la fenêtre de 500 candidats via `interleave_by_source`. |
| `openproxylist_socks5` (8 957) | 2 `usable` (0,02 %) | **douteuse** — ratio coût/bénéfice très défavorable. |

**Feeds threat-intel** (live `egress_threat_feeds`) : `abuse_feodo` 5, `blocklist_de` 26 003, `ipsum` 141 785, `spamhaus_drop` 1 746, `firehol_level1` 4 624 — tous `error=''`. `spamhaus.org/drop/drop.txt` a répondu 200. Aucune erreur de feed active. Total 174 163 indicateurs, 6 369 réseaux — voir P1-02 pour le coût d'appartenance.

**Politique de désactivation** : `egress_sources.yaml` est la source de vérité éditable, hot-reload par mtime (`public.py:266-284`). Un 404/format cassé est absorbé (`read_source_text` lève → `result.errors`), correct.

---

## 6. Options / config sans consommateur

| Clé | Fichier:ligne | Preuve |
|---|---|---|
| `egress_catalog.providers.public.concurrency: 48` | `config.yaml:1140` | clampé à `min(12, …)` (`public.py:88`) — la valeur n'a aucun effet (P2-12) |
| `ip_rotation.public_proxies.feed_path` | absent de `config.yaml`, lu en `pool:4711` | **code mort** tant que `_public_rows_provider` est câblé (catalogue autoritaire) ; seuls `tests/test_public_proxies_pool.py:163,314` l'exercent |
| `ip_rotation.public_proxies.free_probe_model` | absent, lu en `pool:4481,4583` | défaut `deepseek-v4.1-flash-free` en dur ; la clé n'existe pas dans `config.yaml` |
| `ip_rotation.public_proxies.free_probe_concurrency` | absent, lu en `pool:4584` | idem |
| `ip_rotation.public_proxies.max_per_24` | absent, lu en `pool:4637` | défaut 2 en dur ; jamais configurable par l'opérateur |
| `ip_rotation.public_proxies.container_image` | absent, lu en `pool:4639` | défaut `python:3.12-slim` en dur (cohérent avec `DEFAULT_IMAGE`) |
| `ip_rotation.public_proxies.refresh_interval_s` | absent, lu en `pool:4661` | défaut 21600 en dur ; `revalidate_interval_s: 1800` (`config.yaml:1137`) est la valeur réellement active via le scheduler |
| `ip_rotation.public_proxies.proxies` | absent, lu en `pool:4709` | voie legacy, inatteignable avec `_public_rows_provider` |
| `egress_catalog.providers.public.discovery_interval_s: 21600` | `config.yaml:1136` | consommé (`egress_runtime.py:328`) mais **écrasé** par `adapter.discovery_interval_s = policy.discovery_interval_s` (`:412`) — même valeur, donc sans effet observable |
| `EGRESS_CATALOG` | `config/settings.py:526` | exporté, **jamais importé** (`grep -rn "EGRESS_CATALOG" --include=*.py` → 1 hit) |
| `stations.reconciler.enabled` | `config.yaml:66` | `ops/reconciler.py` ne lit aucune config ; seul appelant `dashboard/routers/station_routes.py:132` (hors périmètre, déjà relevé) |

**Clés `egress_catalog` correctement consommées** (vérifiées) : `enabled`, `update_jitter_pct`, `retention_days`, `cycle_candidate_budget`, `cycle_timeout_s`, `denylist`, `threat_feeds.*`, `providers.public.{enabled, discovery_interval_s, revalidate_interval_s, max_active, max_options, concurrency(partiel), ttl_*, sources_file}`.

**Divergence config ↔ pool** : `egress_catalog.providers.public.max_active: 64` (catalogue) vs `ip_rotation.public_proxies.max_active: 3` (pool). `public_rows()` sélectionne 64 options (+ jusqu'à 64 IPv6), puis `select_public_rows` n'en garde **3** (`public_pool.py:108`). Les 61+ options restantes sont re-testées à chaque cycle sans jamais servir — voir P1-01.

---

## 7. Violations constitution

| Principe | Violation | Preuve |
|---|---|---|
| **VII** — Aucune preuve, aucun succès | Un `200` sans contenu est promu `usable` et génère une preuve ledger | `public.py:142` (aucun contrôle de contenu) → `pool:4501` (`note_model_ip(..., 200)`) — **P0-01** |
| **VII** | Une IP issue de l'inventaire historique est sélectionnable **sans** re-validation : `normalize_proxy` accepte, `_record` en `liveness_only` écrit `dead` mais la promotion `usable` d'un `reachable` n'est jamais requise | `public.py:425-438` re-teste `usable` **et** `reachable` ; `SELECTABLE = {USABLE}` (`egress_catalog.py:47`) — conforme en théorie, mais `_seed_public_ledger_proofs` court-circuite en pratique (P0-01) |
| **I** — La disponibilité se mesure | Un candidat non mesuré est converti en `dead` (indisponibilité déclarée) | `public.py:558-566` + `egress_catalog.py:313-318` — **P0-03** |
| **I** | `last_status = 200` par défaut | `egress_rotation.py:105` — **P1-09** |
| **XVIII** — Une IP se brûle au premier format non conforme | Le gate public n'inspecte pas le format de la réponse (pas de `error_frame`, pas de `content_ok`) alors que le gate NordVPN le fait (`probe.py:330-395`) | `public.py:142-168` vs `free/ip_registry/adapters/probe.py:331` — **P0-01** |
| **II** — Session/IP distincts, cooldown sur l'IP | `egress_ip` peut être un texte arbitraire (≤ 64 octets) ; le blocage/cap /24 devient inopérant | `public.py:215-217` + `egress_catalog.py:248-255` — **P2-02** |
| **V** — Une IP = une station, claim exclusif | Le pool public n'a **aucun** claim : `select_public_rows` déduplique par egress mais rien n'empêche deux processus (pool + catalogue + revalidation) de sonder la même IP simultanément | `pool:4321-4323`, `public.py:425-438` — **assumé** (le public est un bucket jetable, cf. PLAN §2.1) ; à documenter comme exception |
| **XVII** (fiabilité) | `clear_if_empty=not result.errors` peut vider le pool sur un cycle sans erreur et sans promotion | `egress_runtime.py:472` — **P2-05** |

---

## 8. Performance

| Chemin | Mesure / structure | Verdict |
|---|---|---|
| Cycle catalogue | 6 runs : 308-357 s pour ~440 candidats (1,3-1,6 s/candidat), sémaphore 12 | le débit est borné par la latence des proxys, pas par la concurrence — augmenter `concurrency` ne changerait rien sans P1-08 |
| `_is_denied` | **1,399 ms/appel**, 6 369 réseaux balayés linéairement | appelé × 64-128 dans `public_rows`, × 5 000 dans `block_indicator` → ~7 s par blocage manuel (**P1-02**) |
| `interleave_by_source` + `dedupe_candidates` | sur 20 000 lignes alors que la fenêtre utile est 500 | 40× le travail nécessaire (**P3-04**) |
| Écritures `egress_observations` | **127 405** lignes `liveness/candidate` + ~11 000 `egress/dead` publiques ; 326 371 lignes totales ; `registry.db` = 167 Mo | écriture amplifiée par P0-03 ; purge à 7 j non appliquée à 24 327 lignes (**P2-08**) |
| `start_worker_process` | 1 process Python par cycle, ~450 URLs en argv, `psutil` requis | coût de spawn négligeable mais P1-11 (fuite d'info) et P1-04 (orphelins) |
| Feeds threat-intel | 174 163 indicateurs, refresh 21 600 s, `replace_threat_indicators` = `DELETE` + `executemany` par feed | acceptable (1×/6 h) ; l'index est sur `(indicator, source)`, pas exploité pour l'appartenance |
| `_public_refresh_loop` | `await asyncio.sleep(21600)` puis `revalidate_public_proxies` sur 3 endpoints | coût nul, mais 2 mécanismes de revalidation coexistent (catalogue + pool) sans coordination |

---

## 9. Fiabilité / idempotence

- **Idempotence** : `_upsert_option_locked` (`egress_store.py:437`) déduplique par `provider + egress_ip` (`_resolve_key:532`) et protège une preuve `usable` d'un downgrade `candidate` (`:487-500`, `keep_proof`). Correct. Live : 0 egress dupliqué par provider (vérifié `GROUP BY egress_ip HAVING n>1` → aucun résultat).
- **Reprise après crash** : `opencode.py:3556-3578` nettoie les workers orphelins (conteneur puis process) avant de démarrer. Le nettoyage conteneur est fiable (Docker) ; le nettoyage process dépend de `psutil` (**P1-04**). `_public_worker_retired` (`pool:879`) et `close()` (`:6665-6680`) arrêtent proprement les handles — correct.
- **Cohérence `registry.db` vs JSON** : `logs/proxy_lists/public_status.json` (écrit par `_persist_public_status`, `pool:4544-4561`) duplique `last_status`/`last_checked_at` hors base. Il est relu au boot (`pool:4536-4542`) et **jamais** réconcilié avec le catalogue : une valeur périmée du JSON peut réinjecter un statut 200 sur une option devenue `dead` (`pool:4348-4354`). Risque faible (le pool ne retient que le catalogue), mais c'est une 2ᵉ source de vérité pour la preuve.
- **Rétention/purge** : `purge()` efface les observations > 7 j, les options `dead` > 30 j, les runs > 30 j (`egress_store.py:930-947`). `dead_cutoff` (30 j) > `ttl_dead_s` (7 j) : les lignes `dead` restent 23 jours de plus que leur TTL, gonflant `known`/`dead` dans `summary()` (26 716 lignes). La maintenance ne tourne qu'1×/24 h (`egress_runtime.py:173`) — live : 24 327 observations au-delà de la rétention.
- **Transactions** : `EgressStore` utilise `with self._lock, self._conn:` partout et `PRAGMA foreign_keys=ON` (`:186`). Les observations orphelines sont supprimées en cascade (`ON DELETE CASCADE`, `:85`). Correct.
- **`CatalogClaims` / `ScanAudit`** : `seed(prune=False)` en production (`fresh_chain.py:2033`) — les hostnames retirés du fournisseur restent réclamables, `claim_next` les retourne indéfiniment (le paramètre `prune=True` existe mais n'est jamais utilisé ; testé en `test_catalog_claims.py`). Hors périmètre public mais même famille de défaut que P2-06.

---

## 10. Zones non auditées / incertitudes

1. **Le worker en mode conteneur n'a pas été exercé en exécution.** `start_worker_container` est bien actif (live : `ContainerWorkerHandle`, port 60940) mais l'audit n'a pas lancé de cycle complet ; les garanties `--cap-drop`/`--read-only`/uid 65534 sont vérifiées par lecture de l'argv et par `tests/test_public_egress_container.py`, pas par observation.
2. **`free/pool/__init__.py` (8 362 l.) n'a été lu que sur la tranche publique** (`:4260-4735`, `:5140-5250`, `:6650-6690`). Les interactions avec le ledger de quota, l'adaptive-429 et la sélection sont hors périmètre — un audit dédié est nécessaire.
3. **`free/qualification/*`** : aucune partie ne traite les proxys publics (grep `public|proxy|proxies` sur `qualifiers.py`/`sweep.py` → 2 hits, tous des docstrings). La qualification est NordVPN-only ; l'affirmation « parties proxys publics » du brief ne s'applique pas.
4. **`free/ip_registry/sources/`** : intégralement dédié au catalogue **NordVPN** (API, cache, gluetun), aucun rapport avec les proxys publics. Audité par lecture, sans finding dans cette zone.
5. **`free/captures/2.3.0.json`** : présent et non suivi (`?? free/captures/2.3.0.json`). Son contenu n'a pas été différé avec `2.2.5.json` (hors périmètre, cf. `04-warp-egress.md`).
6. **Les URLs de sources non testées** (`ercin_*`, `jetkai_*`, `shifty_*`) n'ont pas reçu de HEAD pour rester dans le budget de requêtes. Leur volume de candidats (30 530 pour `ercin_socks5`) est lu depuis la base.
7. **La cause exacte du « cycle partiel »** (38-41 candidats non testés) n'a pas été instrumentée en live : l'audit l'attribue au `deadline_s` (`public.py:528`) par déduction arithmétique (durées de run 308-357 s vs deadline 590 s — l'écart suggère une sortie anticipée par `asyncio.wait` sur des tâches lentes plutôt que par le timeout). Une trace `[public-probe] pending=N` trancherait.
8. **`free/egress_probes.py`** (95 l.) : appelé uniquement pour WARP/NordVPN (`opencode.py:3242,3256,24809`), pas pour le public — mentionné pour compléter la carte, non audité en profondeur.
9. **Le sort de `public_egress_worker.py` vs `public_egress_container.py`** : les **deux** sont vivants et complémentaires (pas de doublon à supprimer). Le conteneur pour le trafic client, le process pour les probes — cette asymétrie est le finding **P0-04**, pas un code mort.
