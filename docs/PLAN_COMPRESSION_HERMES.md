# Plan (corrigé après audit) — « compression bloquée (cooldown:30) » Hermes Agent via le proxy

## 0. Verdict d'audit — certitudes (logs + code, 2026-09-08)

**Cause réelle prouvée : l'appel résumé ne part JAMAIS en paid, il meurt en free avec un HTTP 400.**

Chaîne exacte, vérifiée sur 8/8 appels `summarization agent` (`logs/debug.log.1`, 01:26–01:38Z) :

1. Hermes appelle le résumé via `POST /v1/chat/completions`, `model='deepseek-v4-flash'`, `tools=[]`, UA `OpenAI/Python` (signature distinctive, lignes 36753, 37217, 37542, 38556, 40445, 40739, 41026, 42415).
2. Le proxy applique le free-first : `[chat-stream] attempting free model 'deepseek-v4-flash-free' first` → tentatives sur `https://opencode.ai/zen/v1/chat/completions` via pool SOCKS / direct.
3. Chaque tentative retourne **400** `{"error":{"type":"server_error","message":"Error from provider (Console): Upstream request failed: Model is unavailable."}}` — endpoint free géo-restreint, souvent sans proxy VPN (`⚠️ no VPN proxy — direct connection will likely 400`).
4. Sur ce 400 non-429, [`_free_non429_cooldown_strict`](opencode.py#L7120-L7138) s'applique : `strict_free` étant à `true` ([config.yaml:265](config.yaml#L265)), elle fait `return True` → `yield _free_stream_refuse_bytes(...)` → `return`.
5. **La jambe paid ([opencode.py:11051-11062](opencode.py#L11051)) n'est jamais atteinte.** Preuve log : `[fallback] persist free_status=400 paid_status=None` + `_save_request success=False` (req 1094, lignes 37284-37285 ; idem 1098, lignes 37589-37590). Les 6 autres req n'ont aucune trace paid ni réponse client.

Hermes reçoit donc un HTTP 400 au lieu d'un résumé, en déduit « compression impossible » → `cooldown:30` + `CONTEXT_OVERFLOW_BLOCKED_WARNING`. (Les seuils côté Hermes — 96000 / ~130801 tokens, timeout 120s — restent du côté client, non vérifiables ici ; l'échec systématique côté proxy, lui, est prouvé.)

**Hypothèse RÉFUTÉE — ancien point 2 (« cooldowns 429 partagés par (modèle, IP) → empoisonnement en boucle ») :**
zéro `FREE ... RATE LIMITED (429)` sur ces requêtes. La clé (modèle, IP/proxy) existe bien ([opencode.py:5044-5094](opencode.py#L5044)), mais elle n'est pas en cause ici. Le tueur est le **400 + `strict_free`**, pas le 429. Tout correctif centré uniquement sur l'anti-empoisonnement 429 ne changera rien à cet incident.

**Confirmé — ancien point 3 :** `/v1/models` n'expose pas `context_length` (handler [opencode.py:10372-10403](opencode.py#L10372) : `id/object/created/owned_by/status/requests` uniquement).

## 0bis. Paramètres effectifs pris en compte (config.yaml + code)

Snapshot vérifié le 2026-09-08 — le plan doit rester cohérent avec ces valeurs :

- `strict_free: true` ([config.yaml:265](config.yaml#L265)) — **gratuit strict actif** : tout échec free non-429 → refus client, jamais de paid. C'est le paramètre central de l'incident.
- `free_model_map: deepseek-v4-flash → deepseek-v4-flash-free` ([config.yaml:140](config.yaml#L140)) — le résumé Hermes passe toujours par le free d'abord, aucun alias paid direct n'existe.
- `free_exception_fallback: station-first` ([config.yaml:306](config.yaml#L306)) — sur exception, on réessaie d'autres stations, pas de jambe directe.
- `max_free_attempts: 5` ([config.yaml:305](config.yaml#L305)) **mais** `auto_max_free_attempts: true` → `effective_free_max_attempts()` clampé à **[1, 3]** ([opencode.py:4347](opencode.py#L4347)). Cohérent avec les logs : 2-3 tentatives 400 par résumé, pas 5.
- `free_parallel: enabled, mode strict, hedge_delay_ms: 150, hedge_max_attempts: 2` ([config.yaml:203-208](config.yaml#L203)) — les tentatives free peuvent être hedgées, sans effet sur l'issue.
- `paid_hedge_after_ms: 0` ([config.yaml:254](config.yaml#L254)) — **aucun filet paid en parallèle** : la jambe paid n'existe qu'après épuisement du budget free (puis refuse strict). Confirme qu'il n'y a pas de chemin paid caché.
- `proxy_mode: vpn, station_count: 6, dual_station: true` + timeouts `connect: 5 / read: 600` ([config.yaml:12-16](config.yaml#L12)) — le 400 `Model is unavailable` arrive en **quelques secondes** (fast-fail, pas timeout) : chaque résumé échoue vite, Hermes réessaie (8 résumés en ~12 min le 08/09 01:26→01:38Z), puis `cooldown:30`. Le mismatch proxy-read-600s vs Hermes-120s n'est **pas** le mécanisme ici.
- `on_429_action: both` ([config.yaml:266](config.yaml#L266)) — concerne le 429 uniquement, hors cause (zéro 429 observé).
- **Pas de knob `skip_free` / `force_paid`** : le routeur (`app/router/__init__.py:route_for`) ne fait que remapper des noms de modèles via `custom_routes`, et `FREE_MODEL_MAP.get(model_id)` s'applique **après** le routage. Donc **le contournement « forçage de route paid en config » (ancien nº3) est impossible sans code** — corrigé ci-dessous.
- `_free_non429_cooldown_strict` pose **toujours** `_set_free_cooldown(free_model, 60, ...)` ([opencode.py:7124](opencode.py#L7124)), même si on bypasse le refuse : le Lot 0 doit **garder** ce cooldown (le free est de toute façon cassé) et ne contourner que le `return True`.

C'est la condition sine qua non. Sans elle, les Lots 1-3 ne réparent rien.

- **Détecteur** (déjà identifié, à réutiliser) : `tools=[]` + signature `summarization agent` dans le contenu `messages` + `model='deepseek-v4-flash'`.
  **Action corrigée** : pas seulement « éviter le cooldown 429 », mais **laisser passer en paid** — contourner le `return True` de `_free_non429_cooldown_strict` pour ce trafic, de sorte que le flux continue vers la jambe paid ([opencode.py:11051-11062](opencode.py#L11051)).
- **Implémentation** : flag `_is_summary` calculé dans `openai_stream` (handler `chat_completions`), passé à `_free_non429_cooldown_strict` qui retourne `(False, None)` quand il est posé ; ou hook au site d'appel avant le refuse. Attention : `_free_stream_refuse_bytes` persiste `free_status` via `_stream_error_response` — l'exemption doit intervenir **avant** cet appel.
- **Option config** (recommandé) : `summary_paid_bypass: true` dans `config.yaml`, pour pouvoir couper sans redéployer du code.
- **Test d'acceptation** : forcer un 400 free sur un appel résumé → log `[paid-stream]` présent, client reçoit HTTP 200 + résumé. Vérifier qu'un appel normal `tools=[]` non-résumé (ex. titrage de session) reste soumis à `strict_free` — pas de bypass trop large.

## Lot 1 (réorienté) — Fast-path résumé en paid direct

Toujours valable, mais **second** (optimisation après le Lot 0, pas alternative) : routage direct du résumé détecté vers l'endpoint paid (`zen/go`), sans passer par le free d'abord — évite la latence des tentatives 400 + le risque 429. Garder le timeout court côté résumé.

## Lot 2 (inchangé) — Exposer `context_length` via `/models`

Utile (aide Hermes à calibrer ses seuils), **non causal** : ne répare pas un résumé qui échoue à 100 %. Handler à étendre : [opencode.py:10372-10403](opencode.py#L10372).

## Lot 3 (dépriorisé) — Durcissement streaming

Non causal ici (le résumé meurt avant de streamer). À ne rouvrir que s'il reste des échecs résiduels après Lots 0-1.

## Contournements immédiats, reclassés (sans code)

1. **(recommandé, marche à coup sûr)** Épingler `auxiliary.compression` en **direct paid, hors proxy** — contourne exactement le mécanisme prouvé (free-400 + refuse `strict_free`).
2. `strict_free: false` — efficace mais ouvre le paid à **tout** le trafic free-first (coût). Temporaire seulement, en attendant le Lot 0.
3. `/new` — palliatif uniquement, repousse l'échéance sans réparer le résumé.

(NB : aucun forçage « route paid » n'est possible en config seule — le routeur ne remappe que des noms de modèles et `FREE_MODEL_MAP` s'applique après ; voir §0bis.)

## Traçabilité des preuves

- Requêtes résumé : `logs/debug.log.1` lignes 36753 (1086), 37217 (1094), 37542 (1098), 38556 (10b1), 40445 (10c4), 40739 (10ce), 41026 (10d6), 42415 (10e5).
- Refus prouvés : lignes 37284-37285 (1094), 37589-37590 (1098).
- Code : [opencode.py:7120-7138](opencode.py#L7120-L7138) (refuse), [opencode.py:11051-11062](opencode.py#L11051) (jambe paid jamais atteinte), [opencode.py:5044-5094](opencode.py#L5044) (clé cooldown, hors cause), [opencode.py:10372-10403](opencode.py#L10372) (`/v1/models`).
- Config : [config.yaml:265](config.yaml#L265) (`strict_free: true`).
