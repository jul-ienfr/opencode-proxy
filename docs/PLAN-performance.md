# PLAN — Accélération maximale du proxy (audit vitesse complet)

> 2026-08-25 · Priorité : P1 (vitesse perçue utilisateur) · Effort total : ~6-8 h
> Plan complémentaire : `docs/PLAN-raisonnement.md` (signature thinking, P1 également)

## 0. Verdict de l'audit (mesuré/vérifié ligne par ligne)

**Le chemin payé est excellent** : client httpx partagé HTTP/2 (pool 500/200,
keepalive 30 s), middlewares pure-ASGI (< 0,1 ms cumulés), DB async, geo caché
600 s. Overhead proxy ≈ 10-25 ms hors conversion.

**Le chemin free porte presque toute la dette** — dont deux ralentisseurs
capables d'ajouter des SECONDES :

| # | Ralentissement | Où | Gain si corrigé | Difficulté |
|---|---|---|---|---|
| 1 | Verrou de session curl tenu pendant TOUT le POST non-streaming → head-of-line blocking : 1 requête LLM bloque toutes les autres du même proxy+identité | opencode.py:4054 | p95 ÷ N sous concurrence | Moyenne |
| 2 | Nouvelle session curl_cffi PAR requête geo-tunnelée (handshake SOCKS5+TLS complet à chaque fois) au lieu du pool existant | opencode.py:4781 | **300-1000 ms/requête** | Facile |
| 3 | Cache conversion : json.dumps du body ENTIER pour la clé + double deepcopy (hit ET miss) + collisions hash Python possibles (mauvaise conversion servie !) | protocol_mapping.py:557-578 | 2-8 ms/gros contexte + correctesse | Moyenne |
| 4 | Estimation tiktoken du contexte complet AVANT le premier yield SSE | opencode.py:7574, 8455 | 5-30 ms de TTFB | Facile |
| 5 | Double passe redaction traffic_capture (incrémentale puis globale) | traffic_capture.py:336 | 0,5-3 ms/grosse req | Trivial |
| 6 | Task churn SSE : 2 tasks créées/détruites PAR delta | opencode.py:6148 | 10-50 ms/stream | Moyenne |
| 7 | SSE flush par token-delta sans micro-batch | opencode.py:8803+ | 5-20 ms/stream | Moyenne |
| 8 | Lock global pool curl sur simple dict.get | opencode.py:1519 | µs | Trivial |
| 9 | f-strings _debug évaluées même DEBUG off en boucle chaude | opencode.py:8723+ | 0,01-0,05 ms/chunk | Trivial |
| 11 | Réponse curl re-décodée bytes→str→json | opencode.py:4316 | 0,5-2 ms/500 Ko | Trivial |

## 1. Phase 1 — QUICK WINS (~2 h, gain immédiat)

1. **#2 Pool geo** : `_open_via_pool` réutilise `_get_pooled_curl_session(proxy_url, impersonate)` → **300-1000 ms gagnés par requête géo-tunnelée**
2. **#5 Redaction unique** : supprimer le re-redact global dans `_finish` pour les frames non-binaires (l'incrémentale est idempotente)
3. **#4 TTFB** : déplacer `stream_in_est` après `message_start` (ou remplir usage au `message_delta` final)
4. **#8/#9/#11** : micro-fixes gratuits (lock→get sans contention, gardes DEBUG, parse bytes direct)

## 2. Phase 2 — CONCURRENCE & CORRECTESSE (~3 h)

5. **#1 Head-of-line blocking** : remplacer le verrou unique par un pool de
   N sessions par (proxy, identité) OU vérifier/acquérir le concurrent-safe
   (curl_cffi ≥ 0.7 tolère les posts simultanés sur une session) — à valider
   par test de charge avant merge.
6. **#3 Cache conversion correct** : clé = blake2b des body_bytes bruts (déjà
   dispo dans le handler) au lieu de dumps+hash ; objet converti gelé partagé
   (zéro deepcopy) ; éviction LRU conservée.
7. **#6 Recyclage des tasks SSE** : un seul read_task recyclé tant que pending.
8. **#7 Micro-batch SSE** : accumuler les deltas entre deux lectures upstream
   et émettre groupé.

## 3. Phase 3 — VALIDATION MESURÉE (~2 h)

1. Protocole avant/après : sondes existantes (TTFB streaming, total small
   completion ×3 modèles) + bench_perf.
2. Objectifs chiffrés :
   - Requête géo-tunnelée : −500 ms min
   - Free p95 sous concurrence : ÷2 minimum
   - TTFB stream : −20 ms
   - Overhead proxy fixe : < 15 ms
3. Tests de charge : 10 requêtes free parallèles (avant : sérialisées par le
   verrou #1 ; après : parallèles réelles).

## 4. Hors périmètre / déjà optimaux

- Chemin payé httpx (H2 + pooling) ✓
- DB writer async batch ✓ · middlewares ASGI purs ✓ · geo caché 600 s ✓
- MTU 1280 : coût ~10 % débit bulk, INCHANGEABLE (PMTUD bloqué réseau) — c'est
  le prix de la fiabilité TLS (cf. ADR-005)
- Rotation pays/stations : par design (résilience > vitesse pure)

## 5. Liens

- Signature thinking (P1 associé) : `docs/PLAN-raisonnement.md`
- État général : `docs/PLAN-v10-etat.md`
