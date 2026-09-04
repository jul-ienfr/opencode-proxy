# PLAN DE CORRECTION — Erreurs 429 + Conversions protocoles

Date : 2026-09-03 (maj 2026-09-04 — Lots A, B, C, E, G, H terminés ✅ (B/C non committés) ; Lot D terminé ✅ sauf D2 partiel voulu (non committé) ; Lot F vérifié sans changement ✅)
Périmètre : `opencode.py`, `free_ip_pool.py`, `protocol_mapping.py`, `vpn_manager.py`, `config/settings.py`, `dashboard/api.py`, `config.yaml`

---

## 1. Contexte

Le proxy gère deux familles de 429 :

| Famille | Mécanisme | Localisation |
|---|---|---|
| Clés payantes | `_KeyPauser` — pause par clé, persistance `logs/paused_keys.yaml` | `opencode.py:258-492` |
| Clés payantes (non-stream) | `_do_request_with_retry` — failover 429/401/403 sans consommer de retry | `opencode.py:6502-6620` |
| Clés payantes (stream) | `_make_stream_retry_loop` → `_handle_429` | `opencode.py:6796-6853` |
| Pause quota-aware | `_pause_key_for_quota_reset` — fetch workspace quotas | `opencode.py:506-555` |
| Modèles free (cooldown) | `_free_model_cooldowns` dict per-(model,IP), défaut 120s | `opencode.py:5405-5509` |
| Modèles free (stream) | `_on_free_429_stream` | `opencode.py:5576-5608` |
| Modèles free (non-stream) | `_try_free_model_first` | `opencode.py:5658-6250` |
| Rotation IP | `FreeIPPool.on_quota_exhausted` — single-flight | `free_ip_pool.py:1042-1100` |
| Mode strict_free | `FreeQuotaExhausted` → 429 client + Retry-After | `opencode.py:5628-5655` |
| Normalisation | 429/401/403 → 503 (éviter fenêtre auth Claude Code) | `opencode.py:4166-4178` |

Config active (`config.yaml:260-279`) :
```yaml
strict_free: true
on_429_action: both
max_free_attempts: 5
free_exception_fallback: station-first
```

### Options GUI routage/parallélisation stations free

| Option GUI | Valeurs | Persistance | Localisation runtime |
|---|---|---|---|
| Paralléliser stations free | ON/OFF | `ip_rotation.free_parallel.enabled` | `free_ip_pool.py:251` |
| Routage stations free | `round-robin` / `failover` | `ip_rotation.free_parallel.routing` | `free_ip_pool.py:252,412` |
| Mode routage | `load-balance` / `strict` / `hedge` | `ip_rotation.free_parallel.mode` | `free_ip_pool.py:253,432,440` |
| Hedge delay | 0-2000 ms | `ip_rotation.free_parallel.hedge_delay_ms` | `free_ip_pool.py:254` |
| Hedge max attempts | 1-3 | `ip_rotation.free_parallel.hedge_max_attempts` | `free_ip_pool.py:255` |
| Paid hedge after | 0-N ms (0=désactivé) | `ip_rotation.paid_hedge_after_ms` | `free_ip_pool.py:227,950-954` |

Sélection station (`free_ip_pool.py:414-457`) :
- `enabled=false` → sticky station 1
- `enabled=true + failover` → sticky 1..N (premier usable)
- `enabled=true + round-robin + load-balance` → least-loaded (min request_count)
- `enabled=true + round-robin + strict` → compteur strict 1,2,3…
- `enabled=true + round-robin + hedge` → primaire strict + course N stations (non-stream only, `opencode.py:4447-4628`)

### Fonctions de conversion protocoles (`protocol_mapping.py`)

| Fonction | Sens | Ligne |
|---|---|---|
| `anthropic_to_openai` | Request Anthropic → OpenAI chat | `:506`, wrapper `:873` |
| `openai_to_anthropic` | Response OpenAI → Anthropic | `:1014` |
| `openai_to_anthropic_request` | Request OpenAI → Anthropic | `:1077` |
| `anthropic_to_openai_response` | Response Anthropic → OpenAI chat | `:1268` |
| `anthropic_to_openai_responses` | Response Anthropic → OpenAI Responses | `:1496` |
| `_anthropic_to_responses_request` | Request Anthropic → Responses | `:1898` |
| `_responses_to_chat_response` | Response Responses → OpenAI chat | `:1905` |
| `_responses_to_anthropic_response` | Response Responses → Anthropic | `:1967` |
| `_effort_to_reasoning` | effort générique → reasoning_effort modèle | `:208` |
| `strip_synthetic_thinking` | Retire blocs thinking à signature locale | `:980` |
| `_local_signature` / `_is_local_signature` | HMAC signature thinking synthétique | `:904` / `:929` |
| `_drop_orphan_tool_messages` | Filtre role:tool orphelins | `:83` |

---

## 2. Problèmes détectés

### P1 — CRITIQUE : Bug de type `FreeQuotaExhausted.retry_after` (float vs str)

`opencode.py:5994` :
```python
raise FreeQuotaExhausted(_free_429_cooldown_seconds(""))
```
`_free_429_cooldown_seconds("")` retourne `120.0` (float), mais `FreeQuotaExhausted.__init__` (`opencode.py:5634`) déclare `retry_after: str = ""`.
En aval (`opencode.py:5641`) : `retry_after = exc.retry_after or "60"` — le float `120.0` est truthy donc passe, mais :
- le header `Retry-After` (`opencode.py:5654`) reçoit un float au lieu d'une string
- le message produit `"Retry after 120.0s."` au lieu de `"Retry after 120s."`

Impact : header HTTP potentiellement malformé selon le client, message incohérent.

---

### P2 — CRITIQUE : `strict_free` retourne True AVANT de vérifier les stations restantes

`opencode.py:5606-5607` :
```python
if IP_ROTATION.get("strict_free", False):
    return True
```
Le check `_free_stations_exhausted()` (`opencode.py:5608`) n'est jamais atteint en mode strict.
Un seul 429 sur UNE station refuse immédiatement la requête même si 5 autres stations ont encore du quota.

Impact : refus prématurés massifs en `strict_free: true`. Avec `station_count: 6`, un seul 429 bloque tout au lieu de laisser les 5 autres stations servir.

---

### P3 — CRITIQUE (GUI) : `strict_free` ignore `free_exception_fallback`

L'option GUI "échec tunnel VPN" (`free_exception_fallback`, `config.yaml:279`) n'est pas consultée dans le chemin 429 strict_free.
Elle n'est utilisée que dans le chemin tunnel-failure (`opencode.py:4881-4893`, `opencode.py:8556-8561`).

Comportement requis : en mode strict_free, la décision après 429 doit dépendre de `free_exception_fallback` :
- `"station-first"` → réessayer les autres stations d'abord, refuser seulement si toutes épuisées
- `"direct"` → tenter l'IP résidentielle directe (endpoint free toujours, pas de jambe paid)

---

### P4 — CRITIQUE (GUI) : `on_429_action` non respecté dans `_on_free_429_stream`

L'option GUI "Action sur 429" (`on_429_action`, `config.yaml:261`) accepte 3 valeurs :
- `"cooldown"` → cooldown seulement, PAS de rotation
- `"rotate"` → rotation seulement, PAS de cooldown
- `"both"` → cooldown + rotation

`FreeIPPool.on_quota_exhausted` (`free_ip_pool.py:1079`) lit bien `_on_429_action` pour décider de la rotation.
Mais `_on_free_429_stream` (`opencode.py:5596`) appelle `_set_free_cooldown` inconditionnellement — même en mode `"rotate"` seul.
Et `_try_free_model_first` (`opencode.py:5917, 6204`) fait de même.

Impact : en mode `"rotate"` seul, le cooldown est quand même appliqué → l'IP est bloquée alors que la config demande uniquement une rotation.

---

### P5 — HAUT : Duplication massive du handling 429 (~12 copies)

Le pattern `_on_free_429_stream` + réponse 429 strict_free + `_handle_429` est dupliqué dans :
- `/v1/messages` stream (Anthropic) — `opencode.py:8574-8700`
- `/v1/chat/completions` stream (OpenAI) — `opencode.py:9624-9752`
- `/v1/responses` stream (OpenAI) — `opencode.py:11060-11238`
- `/v1/messages/count_tokens` stream — `opencode.py:11970-12102`
- Non-stream partagé via `_try_free_model_first` — `opencode.py:6172-6250`

Chaque copie a ses propres variations de `_retry_after`, `_set_free_cooldown(…, 60, …)` (hardcoded 60s vs 120s par défaut en stream).

Impact : dette de maintenance, divergence déjà observable (cooldown 60s hardcoded en stream vs 120s par défaut en non-stream).

---

### P6 — HAUT : `_handle_429` (stream) ne retry que sur `attempt == 0`

`opencode.py:6825` :
```python
if attempt == 0 and len(API_KEYS) > 1 and status_code in (429, 401, 403):
```
Si la 2e clé reçoit aussi un 429, aucun retry supplémentaire. Le client reçoit un 503 alors que d'autres clés pourraient fonctionner.

---

### P7 — HAUT : `_free_model_cooldowns` sans protection concurrentielle

`opencode.py:5411` : `dict[str, float]` muté depuis plusieurs handlers async simultanés.
`_sweep_free_cooldowns` (`opencode.py:5473-5488`) itère et supprime pendant que `_set_free_cooldown` écrit.
Le GIL CPython protège contre la corruption mémoire, mais des updates peuvent être perdus.

---

### P8 — MOYEN : `threading.Lock` dans `_KeyPauser` en contexte async

`opencode.py:273` : `self._lock = threading.Lock()`. Fonctionnel car les ops sous lock sont rapides, mais non-idiomatique.

---

### P9 — MOYEN : `KEY_PAUSE_401_SEC = 3600.0` hardcoded

`opencode.py:255` : pause de 1h pour 401, non configurable via `config.yaml`.

---

### P10 — MOYEN : Incohérence de normalisation 429 → 503 vs 429

- Chemin payant : 429 upstream → 503 au client (`opencode.py:4168, 8399, 9380, 10894`)
- Chemin free strict : 429 upstream → 429 au client (`opencode.py:5644, 5650`)

---

### P11 — MOYEN : `_free_cooldown_key` utilise `current_ip` qui peut changer mid-request

`opencode.py:5440` : si une rotation background change l'IP entre la réception du 429 et `_set_free_cooldown`, la clé de cooldown ne correspond pas à l'IP qui a reçu le 429.

---

### P12 — MOYEN : `on_429_action` lu à 3 endroits avec propagation incertaine

| Source | Valeur | Localisation |
|---|---|---|
| Config YAML | `both` | `config.yaml:261` |
| FreeIPPool init | `"both"` hardcoded | `free_ip_pool.py:201` |
| FreeIPPool `update_config` | lit `cfg["on_429_action"]` | `free_ip_pool.py:1638-1641` |
| VPNManager | lit `_cfg_data.ip_rotation.on_429_action` | `vpn_manager.py:2637-2639` |
| Dashboard API | valide + persiste | `dashboard/api.py:3233-3240` |

---

### P13 — BAS : `_free_stations_exhausted` exemption rotation-in-flight trop permissive

`opencode.py:5567-5568` : si une rotation est en cours, le pool est considéré non-épuisé même si la rotation est vouée à échouer.

---

### P14 — BAS : Pas de circuit breaker global sur les 429

Chaque requête tente indépendamment → 429 → fallback. Sous forte charge, effet thundering herd.

---

### P15 — BAS : `_KeyPauser._save()` fire-and-forget

`opencode.py:336-337` : `loop.run_in_executor(None, _write_yaml)` sans await.

---

### P16 — BAS : Log verbeux sur chaque cooldown

`opencode.py:5499` : `_log(f" FREE COOLDOWN: {key} for {seconds:.0f}s")` à chaque 429.

---

### P17 — HAUT (GUI) : 429 en mode `hedge` — rotation déclenchée sur une seule station

`opencode.py:4466-4628` (`_hedged_fetch`) : en mode `hedge`, N stations sont lancées en course. Si la station primaire reçoit un 429 mais qu'une hedge gagne avec un 200, `_on_free_429_stream` n'est PAS appelé (le 429 est ignoré car la course est gagnée). Mais si TOUTES les stations hedge reçoivent un 429, `_hedged_fetch` raise et le caller appelle `_on_free_429_stream` une seule fois — or les N stations ont toutes été frappées simultanément.

Problème : `_on_free_429_stream` ne bad-mark et ne rotate qu'UNE station (`_free_attempt_station()`), pas les N stations qui ont reçu le 429 en hedge. Les autres stations restent éligibles alors que leur quota est épuisé.

Interaction : `on_429_action=rotate` + `free_parallel.mode=hedge` → une seule rotation au lieu de N.

---

### P18 — HAUT (GUI) : `paid_hedge_after_ms` incompatible avec `strict_free`

`free_ip_pool.py:950-954` : quand `paid_hedge_after_ms > 0`, après un délai sans IP fraîche, le pool rend la requête au chemin paid early.

Problème : en `strict_free: true`, ce mécanisme ne doit JAMAIS déclencher un fallback paid. Or le code ne vérifie pas `strict_free` avant de rendre la main au paid (`free_ip_pool.py:951-954`).

Impact : en `strict_free: true` + `paid_hedge_after_ms > 0`, une requête peut passer en paid au lieu d'être refusée avec 429+Retry-After.

---

### P19 — MOYEN (GUI) : `free_parallel.mode=hedge` désactivé en stream mais les 429 stream ne le savent pas

`opencode.py:4448-4450` : `_free_parallel_should_hedge` retourne `False` si `body.get("stream")` — le hedge est désactivé en streaming.

Problème : les 4 chemins stream 429 (`opencode.py:8574, 9624, 11060, 11970`) appellent `_on_free_429_stream` sans savoir si la requête était en mode hedge. Si un futur mode hedge stream est activé, le handling 429 devra bad-marker N stations, pas une.

Impact actuel : aucun (hedge stream désactivé), mais dette pour l'activation future.

---

### P20 — MOYEN (GUI) : `on_429_action=rotate` + `free_parallel.routing=failover` → rotation en cascade

Interaction : en `routing=failover`, le pool est sticky sur la station 1. Si station 1 reçoit un 429 et `on_429_action=rotate`, la rotation change l'IP de station 1. La prochaine requête revient sur station 1 (sticky) avec la nouvelle IP. Si le quota est épuisé au niveau compte (pas IP), la nouvelle IP reçoit aussi un 429 → nouvelle rotation → boucle.

Problème : `_free_stations_exhausted` (`opencode.py:5538`) ne détecte pas l'épuisement au niveau compte. Il vérifie seulement le cooldown per-(model,IP) et le bad_until de la station.

---

### P21 — HAUT (exigence) : Rotation/cooldown doit être INDÉPENDANTE par station

Exigence utilisateur : dès qu'UNE station reçoit un 429, CETTE station seule doit être traitée immédiatement — cooldown si la durée configurée est courte, sinon rotation. On ne doit PAS attendre que toutes les stations soient en 429 pour agir.

État actuel du code :
- `on_quota_exhausted` (`free_ip_pool.py:1042-1102`) bad-mark bien LA station appelante (`bad_until = now + _bad_ttl`, `:1092-1100`) — OK pour le bad-mark individuel.
- MAIS `_on_free_429_stream` (`opencode.py:5576-5608`) couple la décision à la logique globale : le cooldown per-(model,IP) est posé, puis la décision refuse/fallback dépend de `_free_stations_exhausted()` qui regarde l'état GLOBAL du pool.
- En mode `hedge`, les 429 des stations perdantes sont silencieusement ignorés si une autre station gagne la course (voir P17) → ces stations ne sont JAMAIS bad-marked individuellement.
- Le cooldown `_set_free_cooldown` est keyé par (model, IP), pas par station → si deux stations partagent la même IP de sortie, un 429 sur l'une bloque l'autre.

Comportement requis :
1. Chaque 429 → action immédiate sur LA station concernée uniquement
2. Si `on_429_action` inclut cooldown → cooldown sur cette station
3. Si `on_429_action` inclut rotate → rotation de cette station
4. Les autres stations continuent de servir normalement
5. En hedge : chaque station qui reçoit un 429 est traitée individuellement, même si une autre gagne la course

---

### P22 — HAUT (conversion) : `cache_creation_input_tokens` hardcodé à 0

`protocol_mapping.py:1069` :
```python
"cache_creation_input_tokens": 0,
```
Dans `openai_to_anthropic`, le champ `cache_creation_input_tokens` est toujours 0. Les clients Anthropic qui suivent la facturation cache écriture reçoivent une valeur incorrecte. De même, `cache_read_input_tokens` est mappé depuis `prompt_tokens_details.cached_tokens` (`:1070`) — dépend de la présence de ce champ dans la réponse OpenAI.

---

### P23 — MOYEN (conversion) : `reasoning_content` DROPPÉ en OpenAI→Anthropic request

`protocol_mapping.py:1162-1171` (Phase D.3) :
```python
# [PLAN-raisonnement Phase D.3] PAS de conversion reasoning_content →
# ...
"  [convert] DROP reasoning_content historique → upstream Anthropic "
```
Le `reasoning_content` historique est intentionnellement supprimé quand une requête OpenAI est routée vers un upstream Anthropic. Conséquence : les conversations multi-tours perdent le contexte de raisonnement quand le protocole change en cours de session.

---

### P24 — MOYEN (conversion) : `redacted_thinking` DROPPÉ pour upstreams non-Anthropic

`protocol_mapping.py:572-576` :
```python
elif btype == "redacted_thinking":
    _debug("  [convert] DROP redacted_thinking → upstream non-Anthropic")
```
Les blocs `redacted_thinking` (données chiffrées authentiques d'Anthropic) sont supprimés lors de la conversion vers OpenAI. Si la conversation revient ensuite vers Anthropic, ces données sont perdues définitivement.

---

### P25 — BAS (conversion) : `cache_control` désactivé pour glm-5.x

`protocol_mapping.py:513-514` :
```python
supports_cache_control = not model.startswith("glm-5")
```
Les modèles GLM-5.x ne reçoivent aucun `cache_control` → pas de prefix caching pour ces modèles. À vérifier si c'est une limitation upstream réelle ou un contournement temporaire.

---

### P26 — MOYEN (conversion) : Asymétrie effort/thinking entre les sens de conversion

- Anthropic→OpenAI : `thinking.budget_tokens` / `effort` → `reasoning_effort` via `_effort_to_reasoning` (`protocol_mapping.py:208, 799-826`) — fonctionne
- OpenAI→Anthropic : `reasoning_effort` n'est PAS reconverti en `thinking` (voir P23, DROP systématique)
- Responses→Anthropic : `_responses_to_anthropic_response` (`:1967`) — à vérifier que le reasoning est préservé

Impact : un client OpenAI qui demande du reasoning et dont la requête est routée vers Anthropic perd la demande de raisonnement.

---

### P27 — MOYEN (conversion) : Outils — filtre orphelins asymétrique

- `_drop_orphan_tool_messages` (`protocol_mapping.py:83`) filtre les `role:tool` orphelins côté OpenAI
- `_drop_orphan_function_outputs` (`:111`) filtre les `function_call_output` orphelins côté Responses
- MAIS : dans `anthropic_to_openai`, les `tool_use` avec nom vide sont skip (`:601`) et les `tool_result` sans `tool_use_id` sont skip (`:614-619`) — ces suppressions silencieuses peuvent casser une séquence d'outils si le client renvoie l'historique converti

---

## 3. Plan de correction

### Lot A — Bugs critiques (P1, P2, P3, P4) — TERMINÉ ✅

| # | Action | Fichier | Statut | Résultat |
|---|---|---|---|---|
| A1 | Corriger le type de `FreeQuotaExhausted.retry_after` | `opencode.py:6123` | ✅ Fait | `raise FreeQuotaExhausted(str(int(_free_429_cooldown_seconds(""))))` — string entière, plus de float dans le header `Retry-After`. Test : `test_free_quota_exhausted_retry_after_type`. |
| A2 | Restructurer `_on_free_429_stream` strict_free | `opencode.py:5699` (`_on_free_429_stream`) | ✅ Fait | En mode strict : mark stations → si `direct` → return False (jambe résidentielle) ; sinon `return _free_stations_exhausted(free_model)` — refus seulement si toutes stations épuisées. Tests : `test_strict_free_refuse_all_exhausted`, `test_strict_free_station_first`, `test_strict_free_direct`. |
| A3 | Intégrer `free_exception_fallback` en strict_free | `opencode.py:5699`, `~5894` (non-stream) | ✅ Fait | `"station-first"` → `_free_stations_exhausted()` ; `"direct"` → False (jambe directe). Non-stream : même logique + `raise FreeQuotaExhausted` seulement si épuisé. |
| A4 | Respecter `on_429_action` dans `_on_free_429_stream` | `opencode.py:5617` (`_mark_free_stations_429`) | ✅ Fait | Helper factorisé lisant `IP_ROTATION.get("on_429_action", "both")` : `rotate` → skip cooldown ; `cooldown` → skip rotation ; `both` → les deux. Tests : `test_free_429_cooldown_only`, `test_free_429_rotate_only`, `test_free_429_both`. |
| A5 | Respecter `on_429_action` dans `_try_free_model_first` (non-stream) | `opencode.py:~6340` | ✅ Fait | Même logique via `_mark_free_stations_429` (idempotent avec la boucle multi-attempt). |

Fix connexe (découvert par les tests, pré-existant) : `_do_free_request_curl_cffi` en mode direct (`proxy_url=None`) ne tentait jamais aucune requête (`proxies_to_try` vide → `raise last_exc` avec `None` → `TypeError`). Guard `proxies_to_try.append(None)` ajouté (~`opencode.py:4378`).

### Lot B — Dette structurelle (P5, P6) — TERMINÉ ✅ (non committé)

| # | Action | Fichier | Statut | Résultat |
|---|---|---|---|---|
| B1 | Extraire un handler 429 unifié | `opencode.py:7114-7202` | ✅ Fait | 4 helpers extraits : `_free_stream_refuse_bytes` (refus SSE 429), `_free_429_stream_decision` (décision retry/fallback), `_free_non429_cooldown_strict` (cooldown + refus strict), `_free_fallback_bookkeep` (bookkeeping fallback). Les 4 handlers stream (`stream`, `stream-oai`, `chat-stream`, `anthro-to-oai-stream`) utilisent ces helpers pour le chemin non-429. Le chemin 429 conserve une logique par-handler (terminaison stream spécifique à chaque protocole). Message "Free quota exhausted" : 3 occurrences restantes (2 dans `_free_quota_exhausted_response` non-stream + 1 dans `_free_stream_refuse_bytes`). |
| B2 | Étendre le retry stream au-delà de attempt==0 | `opencode.py:7005-7065` (`_make_stream_retry_loop`) | ✅ Fait (non committé) | Garde `attempt == 0` supprimée : chaque clé en échec (429/401/403) est pausée (`_pause_key_for_quota_reset` / `KEY_PAUSE_401_SEC` / 1800s) AVANT `_find_alternative_key`, donc chaque failover progresse vers une clé fraîche quel que soit `attempt`. 403 datapolicy short-circuite sans pause (requête condamnée). Tests : `tests/test_b2_stream_double_failover.py` — double-failover K1→K2 (attempt 0) →K3 (attempt 1, le cas P6) → épuisement `False` ; 401 à attempt=1. |

### Lot C — Robustesse concurrentielle (P7, P8) — TERMINÉ ✅ (non committé)

| # | Action | Fichier | Statut | Résultat |
|---|---|---|---|---|
| C1 | Protéger `_free_model_cooldowns` | `opencode.py:5519-5624` | ✅ Fait (non committé) | `_free_cooldown_lock = threading.Lock()` (convention F-H3 : mix sync/async) ; variantes `_locked` + wrappers publics ; sweep/set/check sous lock. |
| C2 | Documenter la contrainte threading.Lock | `opencode.py` | ✅ Fait | `threading.Lock` gardé dans `_KeyPauser` avec commentaire de sécurité (ligne 106, 1416, 1731). |

### Lot D — Configuration et cohérence (P9, P10, P12) — TERMINÉ ✅ (non committé, D2 partiel voulu)

| # | Action | Fichier | Statut | Résultat |
|---|---|---|---|---|
| D1 | Rendre `KEY_PAUSE_401_SEC` configurable | `opencode.py:256`, `config.yaml:397` | ✅ Fait (non committé) | `KEY_PAUSE_401_SEC = float(yaml_get("key_pause", "key_pause_401_sec", 3600.0))` ; entrée `key_pause.key_pause_401_sec: 3600` dans `config.yaml`. Garde-fou : `tests/test_latency_rotation.py:291` (`== 3600.0` par défaut). |
| D2 | Unifier la normalisation 429 | `opencode.py` | ⚠️ Partiel (voulu) | Payant 429→503 (anti-fenêtre auth Claude Code) ; free strict 429 + Retry-After (sémantique quota client). Divergence documentée, pas un bug. |
| D3 | Centraliser la lecture de `on_429_action` | `config/settings.py:432-450` | ✅ Fait (non committé) | `normalize_429_action()` + `get_429_action()` (hot-reload safe, lecture YAML live) utilisés par `opencode.py:5713`, `vpn_manager.py:2637`, `free_ip_pool.py:1100,1699` (normalizer partagé). |

### Lot E — Résilience (P11, P13, P14) — TERMINÉ ✅

| # | Action | Fichier | Statut | Résultat |
|---|---|---|---|---|
| E1 | Capturer l'IP au moment du 429 | `opencode.py:6031,6050-6054` | ✅ Fait | IP capturée via `attempt.current_ip` (ligne 6018) et passée à `_mark_free_stations_429` avec `stations=[attempt]`. |
| E2 | Affiner l'exemption rotation-in-flight | `opencode.py:5603` | ✅ Fait | Check `any_rotation_in_flight()` intégré dans `_free_stations_exhausted`. |
| E3 | Ajouter un backoff global 429 | `opencode.py:3342-3410` | ✅ Fait | Compteur glissant global (`_g429_hits` deque, `_record_global_429` / `_global_429_remaining`) : ≥10 429 upstream en 30s → backoff 15s (`circuit_breaker.global_429_*` config). Rejet en amont via `Global429BackoffMiddleware` (503 + Retry-After, skip `/api/`,`/health`). Enregistrement aux 2 funnels 429 : paid (`_pause_key_for_quota_reset`) et free (`_mark_free_stations_429`). 69 tests passent. |

### Lot F — Persistance et observabilité (P15, P16) — VÉRIFIÉ SANS CHANGEMENT ✅

| # | Action | Fichier | Statut | Résultat |
|---|---|---|---|---|
| F1 | Await le save de `_KeyPauser` | `opencode.py:335-339` (`_save`) | ✅ Vérifié — pas de changement | Tous les `pause_key` critiques sur 429 passent par `_pause_key_for_quota_reset` (async, `await` — 6 sites : non-stream ~6693, stream ~6957, messages ~9152/~10238, chat ~11775, responses ~12586). Le `run_in_executor` ne couvre que l'I/O fichier YAML (pas de dépendance de lecture : l'état live est déjà en mémoire sous lock) ; la seule fenêtre de perte est un crash avant flush, couverte par `load()` au reboot (test `test_persist_roundtrip_survives_new_instance`). Await le save bloquerait la boucle sur I/O disque à chaque 429 — fire-and-forget voulu ici. |
| F2 | Réduire la verbosité des cooldowns | `opencode.py:5540` (`_set_free_cooldown`), `dashboard/display.py:129` (`debug`) | ✅ Vérifié — pas de changement | 1 429 réel = 1 seul `_log` (`FREE COOLDOWN: key for Ns`) ; les transitions d'état passent déjà par `_debug` (sweep, cascade ~5669, _KeyPauser internes) et `debug()` est no-op quand `DEBUG=false` (`display.py:131`). Pas de log par tentative, pas de hot loop : `_log` écrit en mémoire (pas de flush disque par ligne), le disque n'est touché que par `debug()` en mode DEBUG. |

### Lot G — Interactions options GUI + rotation indépendante (P17-P21) — TERMINÉ ✅

| # | Action | Fichier | Statut | Résultat |
|---|---|---|---|---|
| G1 | Bad-marker toutes les stations hedge en cas de 429 | `opencode.py:~5965` (`_try_free_model_first`, hedge all-429) + `~4650` (`_hedged_fetch`) | ✅ Fait | `_hedge_stations_429: list` collecte les `(station, retry_after)` ; à l'épuisement, `_mark_free_stations_429(..., stations=list(_hedge_cands))` bad-mark chacune. Test : `test_hedge_all_429_marks_all_stations`. |
| G2 | Bloquer `paid_hedge_after_ms` en strict_free | `free_ip_pool.py` (miroir `_strict_free` + hot-reload `cfg`) | ✅ Fait | `_strict_free` hot-reloadable depuis `config.yaml` ; si strict → ZÉRO jambe paid (`FreeQuotaExhausted` au lieu de fallback). Test : `test_paid_hedge_blocked_in_strict_free`. |
| G3 | Préparer le handling 429 stream pour hedge futur | `opencode.py:5699` (`_on_free_429_stream(failed_stations=None)`) | ✅ Fait | Paramètre `failed_stations: list` (défaut `[station courante]`) → `_mark_free_stations_429` supporte N stations. |
| G4 | Détecter l'épuisement compte en mode failover | `free_ip_pool.py` (`on_quota_exhausted` + `set_failover_exhausted_cb`) | ✅ Fait | ≥3 429 consécutifs sur IPs différentes en `routing=failover` → callback `opencode._free_failover_exhausted` (cooldown "sentinelle" toutes stations, 120s) ; un 200 reset le compteur cascade. Pas d'import direct (cycle) : injection via setter. Test : `test_failover_cascade_detection`. |
| G5 | Rotation/cooldown INDÉPENDANTE par station (P21) | `opencode.py:5617`, `free_ip_pool.py` (`_per_station`) | ✅ Fait | `_mark_free_stations_429` : cooldown et/ou rotation PAR station selon `on_429_action`, jamais d'attente globale. En hedge, perdante 429 bad-marked individuellement même si gagnante 200 (`test_hedge_loser_429_badmarked`) ; 429 sur station 2 → seule station 2 traitée (`test_independent_station_rotation`). Cooldown keyé par station (`_per_station`), pas de cross-contamination. |

Tests : `tests/test_free_multi_attempt.py` (G1, G2, G4, G5 + A1-A5) — 69 passed avec `test_lot_h_protocol.py` + `test_effort_mapping.py`.

### Lot H — Audit conversions protocoles (P22-P27) — TERMINÉ ✅

| # | Action | Fichier | Statut | Résultat |
|---|---|---|---|---|
| H1 | Corriger `cache_creation_input_tokens` | `protocol_mapping.py:148` | ✅ Fait | Helper `_extract_cache_creation_tokens()` lit `prompt_tokens_details.cache_creation_tokens` / top-level `cache_creation_input_tokens` / `prompt_cache_miss_tokens`, fallback 0 ; câblé dans `openai_to_anthropic` (ligne ~1109). |
| H2 | Préserver le reasoning en OpenAI→Anthropic | `protocol_mapping.py:1202` | ✅ Fait | `openai_to_anthropic_request` convertit `reasoning_content`/`reasoning` historique en bloc `thinking` avec signature locale (`_local_signature`), inséré en premier ; rôle assistant seulement, contenu non vide. |
| H3 | Préserver `redacted_thinking` | `protocol_mapping.py:170, 598` | ✅ Fait | `_redacted_thinking_cache` (OrderedDict LRU, max 512) stocke les blocs `redacted_thinking` retirés (clé sha256 du champ `data`) au lieu de les supprimer définitivement. |
| H4 | Vérifier mapping effort complet | `protocol_mapping.py:208, 1314` | ✅ Fait | Tous niveaux × familles (glm-5/deepseek/défaut) vérifiés ; sens inverse ajouté : `reasoning_effort` → `thinking.{type, budget_tokens}` ; tests dans `tests/test_effort_mapping.py`. |
| H5 | Vérifier cache dans les streams | `opencode.py:8969, 12355, 12498` | ✅ Vérifié | `cache_read_input_tokens` capté au `message_start` et propagé en `prompt_tokens_details.cached_tokens` (OpenAI) / `cache_read_input_tokens` (Anthropic) dans les 2 sens. |
| H6 | Vérifier outils dans les conversions | `protocol_mapping.py:638-669, 1186-1200` | ✅ Vérifié | `tool_use`↔`tool_calls`↔`tool_result` symétriques ; filtre d'orphelins ne supprime que les `tool_result` sans `tool_call`/`tool_use` précédent. |
| H7 | Vérifier conversions Responses API | `protocol_mapping.py:1553, 1620, 1905, 2024` | ✅ Vérifié | Round-trip Anthropic→Responses→Anthropic et OpenAI→Responses→OpenAI préservent reasoning, outils et cache. |

Tests : `tests/test_lot_h_protocol.py` (H1, H2, H3, H5, H6, H7) + `tests/test_effort_mapping.py` (H4).

---

## 4. Ordre de priorité

```
P1  (bug type)              → Lot A1 ✅ FAIT
P2  (strict_free)           → Lot A2 ✅ FAIT
P3  (free_exc_fallback)     → Lot A3 ✅ FAIT
P4  (on_429_action)         → Lot A4+A5 ✅ FAIT
P21 (rotation indépendante) → Lot G5 ✅ FAIT
P17 (hedge 429)             → Lot G1 ✅ FAIT
P18 (paid_hedge strict)     → Lot G2 ✅ FAIT
P20 (failover cascade)      → Lot G4 ✅ FAIT
P22 (cache_creation)        → Lot H1 ✅ FAIT
P26 (effort asymétrie)      → Lot H4 ✅ FAIT
P23 (reasoning DROP)        → Lot H2 ✅ FAIT
P24 (redacted_thinking)     → Lot H3 ✅ FAIT
P27 (outils orphelins)      → Lot H6 ✅ VÉRIFIÉ
P5  (duplication)           → Lot B1 ✅ FAIT
P6  (retry limité)          → Lot B2 ✅ FAIT (non committé, test test_b2_stream_double_failover.py)
P7  (concurrence)           → Lot C1 ✅ FAIT (non committé, _free_cooldown_lock)
P9  (config 401)            → Lot D1 ✅ FAIT (non committé, key_pause_401_sec)
P12 (propagation)           → Lot D3 ✅ FAIT (non committé, normalize_429_action/get_429_action)
P11 (IP drift)              → Lot E1 [~10 lignes]
P14 (thundering)            → Lot E3 ✅ FAIT
P15 (save)                  → Lot F1 [~5 lignes]
P16 (logs)                  → Lot F2 [~3 lignes]
P25 (glm cache_control)     → Lot H ✅ VÉRIFIÉ
P19 (hedge stream)          → Lot G3 [~10 lignes, dette future]
P26 (Responses round-trip)  → Lot H7 ✅ VÉRIFIÉ
```

---

## 5. Tests à ajouter

| Test | Couvre |
|---|---|
| ✅ `test_free_429_cooldown_only` (test_free_multi_attempt.py) | A4 : `on_429_action=cooldown` → cooldown appliqué, pas de rotation |
| ✅ `test_free_429_rotate_only` (test_free_multi_attempt.py) | A4 : `on_429_action=rotate` → rotation déclenchée, pas de cooldown |
| ✅ `test_free_429_both` (test_free_multi_attempt.py) | A4 : `on_429_action=both` → cooldown + rotation |
| ✅ `test_strict_free_station_first` (test_free_multi_attempt.py) | A3 : strict_free + station-first → retry stations avant refus |
| ✅ `test_strict_free_direct` (test_free_multi_attempt.py) | A3 : strict_free + direct → jambe résidentielle directe |
| ✅ `test_strict_free_refuse_all_exhausted` (test_free_multi_attempt.py) | A2 : refus seulement si toutes stations épuisées |
| ✅ `test_free_quota_exhausted_retry_after_type` (test_free_multi_attempt.py) | A1 : `Retry-After` est une string entière |
| ✅ `test_independent_station_rotation` (test_free_multi_attempt.py) | G5 : 429 sur station 2 → station 2 traitée immédiatement, autres stations continuent de servir |
| ✅ `test_hedge_loser_429_badmarked` (test_free_multi_attempt.py) | G5 : hedge, station perdante reçoit 429, gagnante 200 → perdante bad-marked |
| ✅ `test_hedge_all_429_marks_all_stations` (test_free_multi_attempt.py) | G1 : hedge N stations toutes 429 → toutes bad-marked |
| ✅ `test_paid_hedge_blocked_in_strict_free` (test_free_multi_attempt.py) | G2 : `paid_hedge_after_ms>0` + `strict_free` → pas de fallback paid |
| ✅ `test_failover_cascade_detection` (test_free_multi_attempt.py) | G4 : 3 429 consécutifs sur IPs différentes en failover → cooldown global |
| ✅ `test_cache_creation_tokens_mapped` (test_lot_h_protocol.py) | H1 : `cache_creation_input_tokens` reflète la valeur upstream |
| ✅ `test_reasoning_preserved_openai_to_anthropic` (test_lot_h_protocol.py) | H2 : `reasoning_content` historique préservé vers Anthropic |
| ✅ `test_redacted_thinking_roundtrip` (test_lot_h_protocol.py) | H3 : `redacted_thinking` survit à un aller-retour via OpenAI |
| ✅ `test_effort_mapping_all_levels` (test_effort_mapping.py) | H4 : low/medium/high/xhigh/max correctement mappés |
| ✅ `test_tools_roundtrip_anthropic_openai` (test_lot_h_protocol.py) | H6 : tool_use → tool_calls → tool_use sans perte |
| ✅ `test_tools_roundtrip_responses` (test_lot_h_protocol.py) | H7 : outils préservés en Responses API |
| ✅ `test_cache_stream_propagation` (test_lot_h_protocol.py) | H5 : `cache_read_input_tokens` dans les streams convertis |

---

## 6. Risques

- **B1 (refactor unifié)** : risque élevé de régression sur 4 endpoints simultanés. À faire en dernier, après validation des lots A-C.
- **E3 (circuit breaker)** : peut masquer un problème upstream réel si le seuil est trop bas. Commencer avec un seuil conservateur (ex: 10 429 / 30s).
- **A3 (direct en strict_free)** : la jambe "direct" utilise l'IP résidentielle, qui peut être géo-bloquée. Vérifier que le geo-gate autorise cette jambe.
- **G1 (hedge 429)** : modifier `_hedged_fetch` pour propager la liste des stations échouées change la signature interne. Vérifier que les 2 appelants (`opencode.py:5827`) sont mis à jour.
- **G2 (paid_hedge strict)** : le check `strict_free` dans `free_ip_pool.py` nécessite d'importer `IP_ROTATION` depuis `opencode.py` (déjà fait via `update_config`). Vérifier le hot-reload.
- **G4 (failover cascade)** : un cooldown global en mode failover peut bloquer le modèle sur TOUTES les stations pendant 120s. C'est le comportement voulu (épuisement compte), mais il faut que le dashboard l'affiche clairement.
- **G5 (rotation indépendante)** : keyer le cooldown par station au lieu de (model, IP) change la sémantique de `_free_model_cooldowns`. Les stations partageant la même IP de sortie ne seront plus bloquées ensemble — c'est le comportement voulu, mais vérifier que le sweep (`_sweep_free_cooldowns`) gère les clés obsolètes après rotation.
- **H2 (reasoning préservé)** : réinjecter du `reasoning_content` comme bloc `thinking` avec signature locale peut être rejeté par l'upstream Anthropic si la signature n'est pas reconnue. Le mécanisme `strip_synthetic_thinking` (`protocol_mapping.py:980`) devra être compatible.
- **H3 (redacted_thinking)** : stocker des données chiffrées en cache de session pose une question de mémoire (ces blocs peuvent être volumineux). Prévoir une limite LRU.
- **H6 (outils)** : les fixtures existantes (`tests/test_conversion_golden.py`, `scripts/gen_golden_fixtures.py`) doivent être étendues avec des séquences d'outils multi-tours.