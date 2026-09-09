# PLAN — Correction `chatcmpl_27f16308d5c80-47` (tiny-output sur huge-context)

> Statut : PLAN uniquement — rien n'est implémenté.
> Requête : `chatcmpl_27f16308d5c80-47`, `2026-09-08T17:48:10Z`, `muse-spark-1.3-contributor` via `muse-spark-1.3-contributor-free`.

## 1. Incident (faits)

- `POST /chat` 2 186 177 bytes, 11 tools (`bash`, `edit`, `glob`, `grep`, `question`, `read`, `skill`, `task`, `todowrite`, `webfetch`, `write`), `max_tokens=32000`, client `opencode/1.18.29`, `ip=127.0.0.1` — `logs/debug.log:81926-81930`.
- Route : `muse-spark-1.3-contributor | openai | https://opencode.ai/zen/go/v1/responses`, tentative free-first — `logs/debug.log:81931-81941`.
- `est_input=433151` — `logs/debug.log:81942-81943`.
- Premier SSE à `17:48:09Z` soit **TTFB ~38 s** (référence voisins 82k in : ~3 s).
- 14x `response.output_text.delta` puis `content_part.done`, `output_item.done`, `response.completed` — `logs/debug.log:82138-82156`.
- `truncated without finish_reason → synthesizing stop`, `in=597258 out=26`, `success=1`, `duration 39796 ms` — `logs/debug.log:82157-82160`.
- DB : `tokens_input=597258`, `tokens_output=26`, `tools_used=[]`, `error=None`, `success=1`, `protocol=openai`, `is_stream=1`, `response_body=NULL`, `request_body` stocké 54 685 chars (tronqué) pour 2,1 Mo réels.

Perception client : tour agent vide (micro-texte, 0 tool-call) alors que le proxy répond `finish_reason=stop` + `[DONE]` nominal.

## 2. Preuves / tests read-only (diag vérifié, rien modifié)

Scripts jetables : `C:\Users\julie\AppData\Local\Temp\opencode\diag*.py` + `q*.py` (hors repo).

- **T-est vs réel** : `433151` estimé vs `597258` upstream = **sous-estimation ~37 %**. Cause structurelle : `protocol/tokens.py:22-36` (`estimate_tokens = len//3` sur deltas) et `protocol/tokens.py:39-90` (`estimate_input_tokens` ne concatène que textes extraits, pas le JSON intégral : clés, schémas tools, overhead). L'upstream compte tout.
- **T9 (08/09, tiny<100)** : `Read` 16x, `Bash` 8x, `PowerShell` 2x — la micro-sortie **avec** tool-call est le cas normal d'un tour agent. Seuls **2 cas `[]`** ce jour-là, moyenne in `336k` dont la présente requête. Conclusion : `out<100` seul ≠ échec ; la signature d'échec est **`in énorme + out minuscule + tools_used==[]`**.
- **T10 (tout historique)** : `in>400k AND out<100 AND tools==[]` = **11 cas** seulement (dont 8x `mimo-v2.5-free` à `in=1048570/out=0`, palier différent). Pour `muse-spark-1.3` : notre requête + `chatcmpl_21f913eef2400-97` (`484032→38`). Rare mais bloquant.
- **T5/T8 (session)** : un seul appel `127.0.0.1` dans l'heure avant ; précédent à `13:39` avec `73k in`. Croissance `73k → 597k` en ~4 h = **session longue sans compaction**, pas boucle rapide.
- **T7** : taux tiny 2-6 % les 07-08/09 (pics 17-44 % les 03-04/09, autre régime, hors scope).
- **TTFB** : 38 s < watchdog 90 s (`config.yaml:429-431`), donc aucune protection ne se déclenche. Timeouts `connect 5 / read 600` (`config.yaml:12-16`) : 39,8 s passe.
- **Normalité trompeuse du `truncated…`** : `app/protocol/mapping.py:2544-2565` (`response.completed` → `{"choices": [], "usage"}`) + `opencode.py:11420-11437` (break finalize) + `opencode.py:11539-11556` (synthèse `stop`) = chemin **nominal** Responses API. Le log n'est pas en soi un diagnostic d'échec.
- **Post-mortem impossible** : `observability/db.py:34` (`MAX_BODY_STORAGE=100_000`) + `observability/db.py:41-79` (`truncate_body_for_storage` garde `messages[:2]`, la **tête**) → on conserve le system-prompt, on perd la queue (vraie question + derniers tool-results). `response_body=NULL` en stream.
- **Succès mensonger** : `_finalize_stream_tokens` (`opencode.py:7354-7402`) réconcilie sans garde ratio ; `_save_and_log_request` (`opencode.py:6886`) écrit `success=1` ; `_cb_record_success` (`opencode.py:2888`) récompense l'endpoint.

Hypothèses écartées : 429/quota (status 200 partout), timeout réseau (39,8 s < 600 s), body-limit (2,1 Mo < 10 Mo, `config.yaml:20`), mapping (`DISABLE_MAPPING` match OK).

## 3. Cause profonde (chaîne causale)

1. **Client** : session opencode accumule historique + tool-results sans compaction (`73k → 597k`, 2,1 Mo). Le proxy n'impose aucune limite utile.
2. **Proxy estimation** : `est_input` sous-évalue de ~37 %, donc aucun seuil ne peut fonctionner tel quel.
3. **Proxy garde absente** : pas de pré-vol contextuel (ni warn ni 413 actionnable) avant forward vers le free-tier.
4. **Upstream free** : sur 597k, TTFB 38 s puis micro-réponse 26 tokens / 0 tool-call (peine ou dégradation, cause externe non pilotable).
5. **Proxy conversion** : `response.completed` sans `finish_reason` → `stop` synthétisé, `success=1` + CB success → échec silencieux.
6. **Proxy observabilité** : body tête-seule + pas de preview réponse → incident non rejouable.

Le proxy ne peut pas forcer l'upstream à bien répondre sur 597k ; il peut **refuser/guider avant**, **détecter après**, et **rendre rejouable**.

## 4. Plan de correction (sans implémentation)

### P0 — Observabilité rejouable (prérequis)
- Logger par stream : TTFB, `stream_out`, échantillon 500 chars des deltas texte (pas de secrets : réutiliser le redacteur existant).
- Stocker `response_preview` (500 chars) + `ttfb_ms` + `est_input` + `actual_in` dans `requests` (migration additive, NULL-safe).
- Modifier `truncate_body_for_storage` : mode huge-body = garder **tête (system+tools, borné) + QUEUE (N derniers messages)** au lieu de tête seule ; garder `original_count` et `dropped_chars`.
- Dashboard : colonne `suspect` + filtre `in>200k AND out<100 AND tools==[]`.
- Fichiers : `opencode.py:11442-11474`, `opencode.py:11589-11633`, `observability/db.py:41-79`, `dashboard/api.py`.
- Test : fixture SSE 597k→26 rejouée, vérifier preview + TTFB + queue conservée.

### P1 — Détection « succès vide » (stop le fauxvert)
- Dans `_finalize_stream_tokens` / `_save_and_log_request` : si `actual_in>200000 AND final_out<100 AND tools_used==[]` → `log_tag=suspect_tiny_output`, `success=0` (ou `success=1 + warn` à trancher, défaut `0` pour ne plus récompenser), **ne pas** appeler `_cb_record_success`, émettre `_cb_record_failure` ou neutre (à trancher, défaut neutre+compteur).
- Exclure explicitement les tours tool-call-only (`tools_used non vide` → jamais suspect).
- Fichiers : `opencode.py:7354-7402`, `opencode.py:11615-11634`, `opencode.py:2888`.
- Test : matrice `{(597k,26,[])→suspect, (203k,87,[bash])→ok, (80k,63,[])→ok}`.

### P2 — Garde contexte pré-forward (cause profonde côté proxy)
- Recalibrer `estimate_input_tokens` pour Muse/Spark (facteur mesuré 1,37 ou comptage JSON complet en `to_thread`, sans réintroduire tiktoken dans la boucle chaude — cf. commentaire `protocol/tokens.py:23-31`).
- Seuils (proposés, à valider par A/B logs) : `>200k` → header `X-Proxy-Warning: huge-context` + log warn ; `>450k` → `413` JSON actionnable (`compactez la session, session neuve, réduire tool-results`) au lieu de forwarder.
- Rendre seuils configurables (`config.yaml`, hot-reload comme les autres sections).
- Fichiers : `protocol/tokens.py:39-90`, `opencode.py:8618`, `opencode.py:11117`, `config.yaml`, `config/settings.py`.
- Test : body 2,1 Mo → 413 avec message ; body 73k → passe ; overhead mesuré <5 ms p99.

### P3 — Cause profonde côté session (client, via proxy)
- Documenter la procédure opencode : `/compact`, session neuve, limiter `bash`/`read` verbeux (c'est le 2,1 Mo).
- Option proxy (à trancher) : endpoint `GET /api/session-risk?bytes=` ou réponse `413` incluant `retry_with_compaction:true` que le client peut afficher.
- Non-objectif : compacter automatiquement le contenu (risque sémantique) — refus explicite > réécriture silencieuse.

### P4 — Politique retry/fallback (bornée)
- Uniquement sur signature P1 **avant** tout byte client (`_stream_has_yielded==False`, `opencode.py:7409`) : 1 retry station fraîche ; sinon erreur explicite `upstream_tiny_output` (jamais `stop` silencieux).
- Ne jamais retry après début de stream (concaténation interdite).
- Fallback payant `go/v1/responses` seulement si clé configurée (`strict_free` respecté, `config.yaml:272`).
- Fichiers : `opencode.py:11476-11536` (pattern existant `response.incomplete`), `opencode.py:11635-11720`.
- Test : retry 1x puis erreur typée ; aucun double `[DONE]`.

### P5 — Validation globale
- `pytest tests/test_proxy.py` + nouveaux tests fixtures SSE (tiny-avec-outils, tiny-sans-outils, completed-sans-usage).
- Rejeu DB : les 11 cas T10 doivent basculer `suspect`, les tours normaux rester `success=1`.
- Métriques 7 j : part `suspect`, TTFB p95 par bucket d'input, faux positifs ~0 sur tours tool-call.

## 5. Risques / non-objectifs
- Seuils trop bas → 413 abusifs : atténué par warn-avant-refus + configurabilité.
- Retry → coût upstream doublé : borné à 1x, pré-stream uniquement.
- Ne corrige pas l'upstream free lui-même ; ne compacte pas le contenu à la place du client.

## 6. Critères d'acceptation
- La même requête (rejeu fixture) est marquée `suspect_tiny_output`, sans CB success, avec preview + TTFB + queue de body disponibles.
- Un body >450k reçoit 413 actionnable ; un tour `bash`-only à 80 tokens reste succès.
- Aucune modification du chemin heureux petit-contexte (tests verts).
