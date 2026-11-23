# Research — pourquoi la flotte « tourne » mais n'est pas utilisable

Date : 2026-11-17. Toutes les mesures sont horodatées UTC et reproductibles à
partir de `logs/requests.db`, `logs/fresh_chain*.log`,
`logs/fresh_chain_supervisor.log` et des captures `free/captures/`.

## 1. Ce que le client reçoit réellement

Sur la fenêtre 12:00-21:30, `requests.db` compte, pour
`deepseek-v4.1-flash-free` : 22 × 200 et 60 × 429. Les 429 portent
`free_status=429`, `error="HTTP 429"` et **`response_body` vide** : le corps
amont est jeté. Impossible, en l'état, de distinguer un quota d'une brûlure de
forme dans la télémétrie.

## 2. Le format n'est pas évaluable sur une IP déjà utilisée

A/B contrôlé le 2026-11-17 à 21:29 sur l'IP 187.14.243.53 (station 1) :

| Variante | Statut | Retry-After | Corps amont |
|---|---|---|---|
| Corps officiel verbatim (`free/captures/2.3.0.json`, 43 453 o, sha `117db511…`) | 429 | 9035 s | `FreeUsageLimitError` |
| Corps construit par `_free_wire_body` (31 844 o, sha `d0e0894f…`) | 429 | 9032 s | `FreeUsageLimitError` |
| Corps officiel verbatim (répétition) | 429 | 9028 s | `FreeUsageLimitError` |

Conclusion : sur une IP déjà brûlée, les deux formats échouent avec le même
corps et le même `Retry-After`. Le protocole d'A/B doit utiliser **une IP neuve
par variante** (principe XVIII). Toute mesure antérieure sur IP réutilisée est
nulle.

## 3. Le superviseur détruit le quota qu'il veut réserver

Extraits de `logs/fresh_chain_supervisor.log` (2026-11-17) :

```
21:23:11 station 1 pinned to dk167 … egress=187.14.243.53
21:25:39 registry: station 1 released 187.14.243.53 (live_unusable; … strikes 1)
21:25:56 WINNER station=1 lu133 … egress=187.13.184.78
21:26:16 registry: station 1 released 187.13.184.78 (live_unusable; … strikes 1)
21:28:49 WINNER station=1 hu60 … egress=187.13.29.155
21:25:45 ALERT client_reserve=0 (<2) — priorité client, refill sur stations libres
21:25:45/57/26:04/26:16/26:22  6 chaînes démarrées en parallèle
```

Chaque `WINNER` est précédé de `free status=200` **et** de
`confirmation status=200` : **deux** requêtes au corps officiel complet
(43 453 o, `max_completion_tokens: 32000`) sont envoyées sur l'IP neuve. Puis,
~90-150 s plus tard, l'IP est relâchée `live_unusable` et une nouvelle chaîne
la remplace. Mesure indépendante : cinq requêtes officielles de suite sur la
station 2 (IP 187.14.235.193, via `socks5h://127.0.0.1:1081`) renvoient
429/429/429/429/429 avec `Retry-After ≈ 8960 s`.

Conclusion : le cycle de qualification consomme le bucket de l'IP qu'il vient
de prouver, puis jette l'IP. Le client hérite d'un bucket vide. C'est la cause
dominante des 429 « quota », pas l'impersonation.

## 4. Libérations non prouvées

`21:26:03 registry: station 2 released 187.13.203.90 (live_mismatch; live
186.247.169.86)` puis `21:26:47 registry: station 2 released 187.14.235.193
(live_mismatch; live 1.2.3.4)`. `1.2.3.4` n'existe que comme valeur de test
dans `scripts/smoke_todo10.py` : une lecture d'IP dégradée a produit un
`mismatch`, et le superviseur a relâché une IP dont le `WINNER` venait d'être
confirmé 200 (`21:26:22`). `_UNUSABLE_STRIKES_MAX = 1` rend un seul verdict
suffisant.

## 5. Le wire client est un hybride, pas la capture

Corps réellement construit par `_free_wire_body` pour une requête Claude Code
(mesuré 21:29) :

```
keys  : model, messages, tools, tool_choice, stream, stream_options, system,
        max_completion_tokens      (8 clés, `system` au niveau racine)
tools : 14 (13 officiels + `Read`)
bytes : 31 844
```

Capture officielle 2.3.0 : `model, messages, tools, stream, stream_options,
max_completion_tokens` (6 clés, `system` dans `messages`, 13 outils,
43 453 o). Le chemin client émet donc un corps différent de la référence ; la
constitution XVIII interdit de l'envoyer sur une IP prouvée tant qu'il n'est
pas converti ou explicitement classé `compat`.

## 6. Quatre chemins, deux modèles de configuration

- `config.yaml` porte `ip_rotation.proxy_mode: vpn` (source de vérité de la
  jambe free, valeurs `vpn|socks5|direct`) ;
- il porte aussi `egress_providers: [nordvpn, warp, proxies]` avec
  `egress_strategy: balance` et des poids 52/24/24 ;
- `direct` n'est pas un provider de cette liste, alors que la spec 008 le
  présente comme une case à cocher.

Il y a donc deux vérités runtime pour la même décision, et le direct n'a pas de
contrat de capacité (pas de bucket, pas de réserve, pas de `reset_at` propre).

## 7. Ce que font déjà les specs voisines

- 011 définit `client_usable` et la capacité par bucket (`/32`, `/64`).
- 015 définit le SLO, la rotation rapide et le cycle des transports.
- 022 définit la réserve client et la séparation probe/client, mais pas la
  brûlure de format ni la preuve de libération.
- 003 définit la parité wire et l'outillage de diff.

La présente feature les relie : un seul modèle d'état d'IP, un seul constructeur
de wire, un seul réparateur, quatre providers de première classe.

## 8. Décisions techniques proposées

1. **`ip_state` comme machine à états unique** (SQLite, `registry.db`) :
   `unproven → proven → serving → spent|burned|mismatch`, avec `wire_fp`,
   `proven_at`, `reset_at`, `burned_at`, `bucket_key`, `evidence_ref`.
2. **`wire_profile` unique** : un constructeur, un `wire_fp`, un garde
   d'admission. Le mode `compat` ne peut pas cibler une IP `proven`.
3. **Ledger de sondes séparé** : `probe_budget_per_ip=2`,
   `probe_cooldown_s=600`, `reserve_probe_forbidden=true`.
4. **Réparateur unique avec token bucket** : `min_interval_s=600` par station,
   coalescence `(provider, station)`, maximum global de rotations par heure.
5. **Lecteurs d'IP concordants** : deux sources (`ipify` + control API Gluetun,
   ou `ipinfo`), rejet des adresses non globales/sentinelles, jamais de
   libération sur une seule lecture.
6. **Classification 429 avant décision** : conserver le corps amont, extraire
   `reset_at` du `Retry-After` fournisseur, écrire `wire_fp` et la classe.
7. **Unification de configuration** : `egress.providers.<name>.enabled`
   devient la seule vérité, avec migration de `proxy_mode` et
   `egress_providers` au chargement.

## 9. Inconnues à lever pendant l'implémentation

- Combien de requêtes client un bucket NordVPN fraîchement prouvé supporte-t-il
  réellement après les deux sondes de qualification ? (à mesurer par IP neuve :
  N requêtes officielles espacées, `N ∈ {3,5,10}`).
- Le direct et les proxys publics partagent-ils le bucket avec d'autres
  utilisateurs ? (mesure : 429 immédiat sur IP jamais vue = bucket partagé).
- Le gate accepte-t-il un corps converti depuis Claude Code (outils mappés sur
  les 13 officiels) ? (A/B à IP neuve, ≥ 20 échantillons par variante).

## 10. Matrice des clients et familles de protocole

Le proxy doit servir des clients qui parlent trois protocoles différents. Un
adapter par **famille**, pas par marque :

| Client | Famille | Endpoint principal | Auth | Stream | Tools | Particularités |
|---|---|---|---|---|---|---|
| Claude Code | `anthropic_messages` | `/v1/messages`, `/v1/messages/count_tokens` | `x-api-key` + `anthropic-version` | SSE `message_start`/`content_block_*` | `input_schema`, `tool_use`/`tool_result` | blocs `thinking`, `cache_control`, betas |
| Codex CLI | `openai_responses` | `/v1/responses` | `Authorization: Bearer` | SSE `response.*` | function tools, `tool_choice` | `reasoning.effort`, `prompt_cache_key` |
| Kilo Code | `openai_chat` | `/v1/chat/completions` | Bearer | SSE `data:`/`[DONE]` | function tools | extension VS Code, mode Anthropic optionnel |
| Hermes Agent | `openai_chat` | `/v1/chat/completions` | Bearer | SSE | function tools | prompt système long, outils custom |
| DeepSeek | `openai_chat` | `/v1/chat/completions` | Bearer | SSE | function tools | `reasoning_content` obligatoire par message assistant |
| Aider | `openai_chat` | `/v1/chat/completions` | Bearer | souvent non-stream | rare ou absent | éditions par diff, `max_tokens` |
| SDK OpenAI | `openai_chat`/`openai_responses` | selon | Bearer | SSE | function tools | en-têtes `x-stainless-*` |
| client inconnu | `custom` | configurable | configurable | oui/non | oui/non | profil JSON, défauts sûrs |

Capacités négociables par client : `stream`, `tools`, `parallel_tool_calls`,
`tool_choice`, `reasoning`, `vision`, `cache_control`, `count_tokens`,
`max_context`, `system_role`, `stop_sequences`, `metadata`.

## 11. Ce qui existe déjà et ce qui manque

Existe (mesuré dans `opencode.py`) :

- en-têtes Anthropic de parité (`_ANTHROPIC_BETA`, `_ANTHROPIC_BINDING_BETA`,
  `anthropic-version`) ;
- conversions `chat_sse_to_anthropic`, `openai_to_anthropic`,
  `_collect_chat_completion`, `adapt_chat_completion` ;
- healing d'appels d'outils (`_ChatToolCallHealer`, `tool_call_supported`,
  cas `todowrite`) ;
- raisonnement (`ensure_deepseek_reasoning`, fusion `reasoning_content`) ;
- endpoints `/v1/responses` et `/v1/messages/count_tokens` ;
- politique d'outils partielle (`_FREE_BLOCKED_TOOL_NAMES`, placeholders,
  note « ne pas appeler », plafond d'outils client).

Manque :

1. **registre déclaratif** : les comportements sont des branches dans le
   monolithe, pas une donnée par client ;
2. **négociation de capacités** : aucune table client → capacités → conversion ;
3. **fixtures golden** par client et snapshot de `wire_fp` ;
4. **admission par `wire_fp`** : un corps `compat` peut encore atteindre un
   bucket prouvé ;
5. **politique d'outils unifiée** : listes dispersées, pas de mapping
   client→officiel ni de retour d'appel garanti ;
6. **fidélité SSE testée** par famille (aucun contrat automatisé) ;
7. **mapping d'erreurs** par famille (ad hoc aujourd'hui) ;
8. **multimodal et contexte** : comportement non spécifié par client ;
9. **observabilité par client** : absente des métriques.

## 12. Détail des quatre providers

- **NordVPN** : 6 stations, transports WireGuard/OpenVPN UDP/TCP, rotation par
  control API (`PUT /v1/vpn/settings`), bucket `/32` par IP. Contraintes :
  limite de sessions simultanées, brûlure de format (principe XVIII), sondes
  qui consomment le bucket.
- **WARP** : conteneurs `warp-*`, SOCKS `127.0.0.1:11080` (IPv4) et `:11085`
  (IPv6). L'IPv4 WARP est une **NAT partagée** (capacité 0) ; seul un `/64`
  IPv6 distinct compte comme bucket. Provisioning/enregistrement, santé par
  endpoint, rotation bornée.
- **Proxys** : sources publiques, qualification liveness/anonymat/latence,
  denylist, bucket par endpoint, isolation (`container`), `max_active: 3`.
  Fiabilité par nature faible : fallback, jamais réserve garantie.
- **Direct** : egress de l'hôte, bucket partagé, aucune rotation, 429 honnête
  avec `reset_at` ; `enforce_vpn_only` l'interdit tant qu'il est actif.

Le point commun : **le wire est identique sur les quatre chemins**. Le CONNECT
d'un proxy ne modifie pas le ClientHello vers l'origine ; un test le vérifie.

## 13. Risques spécifiques à la couche client

- Un client change son schéma de tools (Claude Code, Codex) : mitigation =
  fixtures golden + `wire_fp` + test de contrat par famille.
- Un client envoie un outil au nom officiel avec un schéma incompatible :
  mitigation = mapping explicite et préférence pour le shim officiel.
- Un client attend un événement SSE non émis : mitigation = tests de fidélité
  par famille et capture de référence.
- Un client inconnu envoie un corps non supporté : mitigation = profil
  `custom`, 422 local explicite, jamais un envoi non conforme.
