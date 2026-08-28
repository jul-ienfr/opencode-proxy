# PLAN COMMUN — Performance & Raisonnement (v10-P1)

> 2026-08-25 · Fusion de `PLAN-performance.md` + `PLAN-raisonnement.md` ·
> Effort total ~10 h · Priorité P1 · Chaque phase = gate vert indépendant

## Objectifs mesurés

| Objectif | Avant | Cible |
|---|---|---|
| Requête géo-tunnelée | +300-1000 ms (session jetable) | −500 ms min |
| Free p95 sous concurrence | sérialisé par verrou session | ÷2 minimum |
| TTFB streaming client | +5-30 ms (estimation tokens) | −20 ms |
| Overhead fixe free | 10-45 ms/requête | < 15 ms |
| **Raisonnement client** | thinking sans signature → abandonné en multi-tours | signature présente + signature_delta émis |
| Redaction capture | double passe par requête | simple passe |

---

## PHASE 1 — Quick wins vitesse (~2 h)

| # | Fix | Fichier | Gain |
|---|---|---|---|
| 1.1 | `_open_via_pool` réutilise le pool curl existant (fini la session jetable) | opencode.py:4781 | 300-1000 ms/req géo |
| 1.2 | Supprimer le re-redact global dans `TrafficCapture._finish` (incrémentale déjà faite) | traffic_capture.py:336 | 0,5-3 ms/grosse req |
| 1.3 | Déplacer l'estimation tokens après `message_start` (TTFB) | opencode.py:7574, 8455 | 5-30 ms TTFB |
| 1.4 | Micro-fixes : dict.get sans lock global curl ; gardes DEBUG sur f-strings chaudes ; parse JSON bytes direct | opencode.py:1519, 8723 ; 4316 | 0,5-2 ms/500 Ko |

Gate : suite EXIT=0 + bench sans régression + sondes TTFB comparées.

## PHASE 2 — Raisonnement complet (~4 h) — *la plainte utilisateur*

Constat validé : blocs thinking servis SANS `signature` (non-streaming) et
SANS `signature_delta` (streaming) → clients Anthropic-compatibles les
abandonnent en multi-tours. Le mot « signature » n'existe nulle part dans le code.

| # | Fix | Fichier |
|---|---|---|
| 2.1 | Helper `_local_signature(text)` : HMAC SHA256 (clé dérivée VPN_CONTROL_API_KEY) base64 | protocol_mapping.py |
| 2.2 | Non-streaming : `"signature": <locale>` sur chaque bloc thinking synthétisé ; mapping binaire → `redacted_thinking` | protocol_mapping.py::openai_to_anthropic |
| 2.3 | Streaming : émettre `content_block_delta {signature_delta}` AVANT chaque `content_block_stop` de bloc thinking, y compris fin de stream brutal | chemins curl_cffi wrap + payés-B |
| 2.4 | Multi-tours : blocs thinking SYNTHÉTIQUES strippés de l'historique vers upstreams (openai ET anthropic-direct) ; blocs ORIGINAUX passthrough intact (signatures authentiques préservées) | anthropic_to_openai entrée |

Gate : probes thinking (non-stream + stream) → signature PRÉSENTE / delta ≥1 ;
golden fixture resp_reasoning mise à jour ; test multi-tours aller-retour.

## PHASE 3 — Concurrence & conversion (~3 h)

| # | Fix | Fichier | Gain |
|---|---|---|---|
| 3.1 | #1 Head-of-line : pool de N sessions curl par (proxy, identité) OU validation concurrent-safe curl_cffi ≥0.7 (test de charge avant merge) | opencode.py:4054 | p95 ÷N sous concurrence |
| 3.2 | #3 Cache conversion : clé blake2b(body_bytes) + objet gelé partagé (zéro deepcopy), corrige la collision hash | protocol_mapping.py:557 | 2-8 ms/gros contexte + correctesse |
| 3.3 | #6 Recyclage read_task SSE (fini 2 tasks/delta) | opencode.py:6148 | 10-50 ms/stream |
| 3.4 | #7 Micro-batch deltas entre lectures upstream | opencode.py:8803+ | 5-20 ms/stream |

## PHASE 4 — VALIDATION MESURÉE (~2 h)

1. Protocole avant/après : sondes (TTFB stream, total ×3 modèles ×3 runs),
   bench_perf, 10 free parallèles (avant : sérialisées par 3.1).
2. Objectifs du tableau initial revus un à un.
3. Suite complète EXIT=0 · ruff 0 · drift 5 familles.
4. Mise à jour journal + docs (provider-opencode.md §perf).

## Hors périmètre / déjà optimaux

Chemin payé httpx H2+pooling ✓ · middlewares ASGI purs ✓ · DB async ✓ · geo caché ✓ · MTU 1280 inchangé (prix fiabilité, ADR-005) · rotation pays (résilience > vitesse).
