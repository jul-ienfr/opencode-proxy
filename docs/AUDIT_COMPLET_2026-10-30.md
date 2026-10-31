# Audit complet du proxy — 30/10/2026

**Périmètre** : gestion des stations, rotation et sélection des IP, quotas
(gratuits et OpenCode Go), contexte/compaction, cache, traductions de
protocole, alignement avec le client OpenCode officiel.

**Méthode** : lecture du code et des tests, état live de l'instance
`http://localhost:4000` (`/health?detail=full`, `/api/vpn-status`,
`/api/pool-status`, `/api/free-models`, `/api/live-models`, `/api/quotas`,
`/api/drift`, `/api/vpn/diagnostic`, `/metrics`), catalogue officiel
`models.opencode.ai/api.json` du 30/10, binaire officiel installé
(`resources/opencode-cli.exe --version` → `opencode v2.2.2`), suite pytest
complète et gate de contrats.

> **Verdict** : le proxy fonctionne et sert du trafic réel, mais plusieurs
> défauts P0 n'étaient pas visibles dans les dashboards : fuite de secrets
> dans le diagnostic, écrasement de `credentials.env`, compteur de quota
> remis à zéro en boucle, cache inter-protocoles, pertes de contexte dans les
> conversions Responses→Anthropic, et fausse lecture « quota 0 % sain ».
> Ces points sont corrigés et **vérifiés après redémarrage** (voir § 5 bis).

### 5 bis. Vérification post-redémarrage (30/10 11:41)

| Contrôle live | Résultat |
|---|---|
| Processus | PID **37716**, `python opencode.py --no-gui`, health en 1 s |
| `/health` | `ok`, `status_reasons=[]`, aucun compteur de tokens négatif |
| Flotte | 4/4 stations `connected` (1,2,3,4), aucun `AUTH_FAILED` ni rotation storm récent |
| Compteurs | `requests_this_ip = [18, 6, 37, 4]`, stables entre deux lectures (ancre OK) |
| `station_count` / `dual_station` | **4** / **False** (le YAML et l'API sont d'accord) |
| Identités | `opencode/prod/2.2.2/cli` et `.../desktop` |
| Quota Go | `status=error` explicite (fenêtres manquantes), plus de faux 0 % |
| Drift | **3** orphelins free (`flint-40b-free`, `iceman-contributor-free`, `jev-1.13-free`) au lieu de 54 |
| Modèles / pool free | 85 exposés / 10 dans le pool |
| Diagnostic | 40 valeurs `***REDACTED***`, **aucun** préfixe de secret réel |
| `config.yaml` | `control_api_key` **absent** ; `.env` et `credentials.env` conservent la clé |
| Tests | suite complète verte (exit 0) ; gate contrats 513/513 |

### 5 ter. Vérification post-redémarrage (30/10 13:18)

| Contrôle live | Résultat |
|---|---|
| Processus | PID **5252**, `python opencode.py --no-gui`, health en 2 s |
| `/health` | `ok`, `status_reasons=[]`, compteurs tokens sans valeur négative |
| Flotte | 4/4 stations `connected` après stabilisation (s1 brièvement `degraded` puis reconstruite) |
| `station_count` / `dual_station` | **4** / **False** |
| `/api/drift` | **0 orphelin**, statut `candidates` (union des jambes live + exclusion des routes internes `jev`) |
| Découverte free | URLs `/inference/v1/models` + `/zen/go/v1/models` ; plus de 404 sur `/inference/go/openai/v1/models` |
| Quota Go | `status=error` **explicite** : « authentication expired (redirected to the login page) » — cookie à renouveler |
| Smoke `/v1/chat/completions` | **200**, `deepseek-v4.1-flash-free`, contenu `OK`, usage cache inclusif |
| Smoke `/v1/messages` | **200**, `stop_reason=end_turn`, bloc `thinking` puis texte `OK` (le test initial lisait `content[0].text`, qui était le bloc thinking) |
| Compaction serveur | `enabled=true`, `auto=true`, `keep_tokens=15000`, checkpoint officiel + `<recent-context>` ; trigger proactif `context_limit ? buffer` ; shape implicite limit?e au vocabulaire de r?sum? |
| Compaction native | `/v1/responses/compact` relay? au provider Responses ; streaming natif Responses relay? octet par octet |
| Pauses de cl?s | 403 model-scoped (une cl? bloqu?e pour un mod?le reste utilisable pour les autres) |
| Tests | suite complète verte (exit 0) ; gate contrats 513/513 ; `config_audit --strict` 0 clé morte/manquante |

### 5 quater. Passage à 6 stations (30/10 18:07)

| Contrôle live | Résultat |
|---|---|
| Processus | PID **2932**, `python opencode.py --no-gui`, health en 1 s |
| `/health` | `ok` au démarrage, puis `degraded` uniquement après le smoke Gemini qui a re-pausé la clé payante morte (401) |
| `station_count` / `dual_station` | **6** / **False** (`config.yaml` et API d'accord) |
| Conteneurs | `opencode-vpn` + `opencode-vpn-2..6`, ports 8888/1080, 8889/1081, 8890/1082, 8891/1083, **8892/1084**, **8893/1085**, tous `healthy` |
| Pool | **6/6 stations connectées** (2,4,5,6 déjà saines ; s1/s3 passées de NordVPN OpenVPN à **WireGuard**, puis connectées avec de nouvelles IP) |
| Redémarrage de contrôle | après relance du proxy, **6/6 reconnectées** avec les stacks persistées (s1/s3 WireGuard) |
| Routage | requêtes free servies en 200 ; les tunnels morts (`tunnel_down`) sont exclus par la nouvelle garde pool |
| `/api/drift` | **0 orphelin** |
| Smoke free | 3× `/v1/chat/completions` **200**, `/v1/messages` **200** |
| Gemini | route native active ; l'erreur upstream est maintenant relayée explicitement (`401 This API key has expired`) au lieu d'un faux 200 |
| Tests | suite complète **exit 0** ; gate contrats **513/513** ; `config_audit --strict` **336 clés, 0 morte/manquante** |

**Correctif de fiabilité associé** : le watchdog peut remettre une station en
`connected` alors que sa sonde egress est morte (`tunnel_down`). Le pool exclut
désormais ces stations du routage (la grâce PC-9 de rotation douce reste
prioritaire), pour ne plus envoyer de requêtes dans un tunnel mort.

### 5 quinquies. Contrôle contexte / alignement client officiel (30/10 23:00)

| Contrôle | Résultat |
|---|---|
| Source officielle | `upstream_compaction_latest.ts` (client 2.2.2) relu : seuil `context_limit − max(16k, 10-15 %)`, `keep=15k`, checkpoint + `<recent-context>`, re-compaction de S1+queue |
| Forme native Responses | le client officiel envoie `context_management` en **tableau** (`[{type:"compaction",compact_threshold}]`) ; la détection proxy ne lisait que la forme dict — corrigée (dict **et** tableau) |
| Contexte free | plafond aligné sur la fenêtre free officielle : **900 Ko** (200k tokens, seuil 180k). Un historique de 474 Ko est envoyé **intégralement** ; au-delà de 900 Ko le trim protège le tour courant et la tâche |
| Compaction proactive free | déclencheur sur `free.max_wire_bytes` (**900 Ko**) pour les modèles free-mappés non officiels ; `keep_tokens` reste le **keep officiel 15k**, re-résumé quand la queue dépasse le budget. Les clients officiels (system+`read`) ne sont pas recompactés |
| Estimateur contexte | `instructions` + `input` (Responses), arguments `tool_calls` Chat et schémas `function.parameters` désormais comptés ; sans cela la porte Responses était estimée à ~0 token |
| Live long contexte sans en-tête | requête **97 993 o / 11 messages**, **aucun `x-opencode-*`** → **200**, réponse `TIGRE-88`, **3,2 s**, `input_tokens=22 608` : historique **complet** envoyé |
| Live gros contexte | **600 Ko / 28 messages / 24 outils** → **200** (input 86 991) ; **800 Ko** → **200** (input 109 213), sans compaction |
| Processus / flotte | PID **23528**, health en 3 s, smoke free **200**, **6/6** stations `connected`/`tunnel_up_full`, `/api/drift` **0 orphelin** |
| Smoke free | `/v1/chat/completions` → **200**, `deepseek-v4.1-flash-free`, `x-opencode-proxy-tier: free` |
| Compaction native Anthropic | beta `compact-2026-01-12` ajouté si edit `compact*`/bloc `compaction` (même client sans header) ; `/v1/responses/compact` testé (URL, remap, 4xx intacts, refus non natif) |
| Overflow réactif | ~35 regex officielles (`provider-error.ts:16-54`) + exclusions débit/`max_tokens`, `400/413 (no body)` ; couvre `model_context_window_exceeded`, `input token count…`, `request_too_large`, etc. En stream, la trame SSE porte le texte amont + `invalid_request_error` (le client peut classifier/compacter) |
| Budget free | placeholders officiels minimaux (nom + « ne pas appeler » + schéma vide) : **6,9 Ko → 2,4 Ko**, les 10 noms du gate sont conservés ; l'espace va à la queue récente. Trim basé sur la copie finale, y compris clients officiels >900 Ko |
| Résumé free | transcript tronqué : début + 3 échantillons centrés du milieu + fin, pour ne pas perdre les décisions intermédiaires |
| `<recent-context>` | aligné `messageToText` : raisonnement assistant, `[Tool result]` vs `[Tool error]`, pièces jointes avec mime, tool_result Anthropic non présenté comme `[User]` |
| Capacités modèle | `context_management.supported=true` pour Anthropic/Responses natifs (relayé), false pour Chat/Gemini |
| Fenêtre par modèle | `max_input_tokens`/`max_tokens` = min(modèle demandé, cible free) sur route `free_first` ; ex. alias 1M/128k → 200k/32k si la jambe free le limite |
| Config client | **aucune modification** : l’alignement des fenêtres est fait côté proxy (`min(modèle, cible free)` + règle officielle) ; le client officiel garde sa propre configuration |
| Seuil proactif | `calculateCeiling` officiel : `max(10 %, 16k)` (16k si fenêtre ≥ 32k), `limit.input` prioritaire ; plus de compaction à 4k sur une fenêtre 20k |
| Chaînage Responses | `previous_response_id`/`conversation`/`item_reference` refusés en 400 sur cible non-Responses (Chat/Anthropic/Gemini) au lieu d'être ignorés silencieusement ; relais intact vers une cible Responses native |
| Cache checkpoint | première + dernière borne du préfixe : un client qui réécrit son début (compaction côté client) force un nouveau résumé. Sans en-tête `x-opencode-*` (harnais DeepSeek), clé dérivée des deux premiers messages : la compaction free reste active |
| Limites exposées | `max_input_tokens` = `limit.input` prioritaire (limite d'entrée réelle) |
| Tests | gate contrats **516/516** ; `config_audit --strict` **336 clés, 0 morte/manquante** ; suite complète **EXIT=0** |

**Mesure du gate free (31/10)** : des copies finales de **450 Ko à 900 Ko**
(24 outils, 28 messages) passent en **200** sans compaction. L'ancienne borne
de 96 Ko était une hypothèse trop prudente. Le plafond est aligné sur la
fenêtre free officielle : **900 Ko** (200k tokens, seuil 180k), avec le
`keep` officiel de 15k. La limite restante est la fenêtre du modèle, pas une
limite de corps du gate.

**Reste opérationnel** : le cookie de workspace Go est expiré (le scraper le
dit maintenant explicitement au lieu de « page invalide ») et la clé payante
`.env` reste morte (401) — Gemini répond donc 401/502 explicite, pas 501.
Les stations s1/s3, qui échouaient en boucle sur les serveurs OpenVPN
NordVPN (`server-refused`/TLS/`Connection reset`), ont été basculées en
**WireGuard** et sont désormais connectées. Le drift est aligné
(0 orphelin). Les P0 de rotation C2/C4/C5/C6/C7/C8/C9/C10 et la compaction
free/native ont été corrigés ; les points encore ouverts sont listés en § 3.

---

## 1. État live au moment de l'audit

| Mesure | Valeur / constat |
|---|---|
| `/health` | `ok`, DB `ok`, upstream `ok`, 0 clé en pause |
| Stations | 4 déclarées, 4 conteneurs ; s1 `connecting` avec rotation en vol, s2 `connected` avec `last_rotation_error = timeout 240 s` |
| AUTH NordVPN | rafales `AUTH_FAILED` sur s1/s3/s4, `auth_fail_30min` jusqu'à 5, `auth_cooling` 300 s, `rotation_storm` sur s1 (11 rotations/h > 10) |
| Quota gratuit | `quota_per_ip=500`, seuils observés 40–50 sur certaines stations (tier C), `remaining` parfois incohérent |
| Quota Go | `/api/quotas` en **erreur** : « Could not parse quota data from the OpenCode Go page » — le dashboard affichait malgré tout des fenêtres à 0 % |
| Modèles | 85 exposés par `/v1/models`, 87 entrées `config.models`, catalogue actuel 130 free + 37 payants |
| Drift | `/api/drift` : 54 « orphelins » calculés contre un snapshot **du 2026-10-20** (128/33), donc périmé |
| Compteurs tokens | input **négatif** pour `glm-5.3-flash`, `deepseek-v4.1-flash`, `mimo-v2.6-flash` |
| SLO | `breached` : `mimo-v2.6-flash-free` p50 26,4 s / p95 123,5 s |
| Flux vides | 7 trames d'erreur, 6 échecs free `deepseek-v4.1-flash-free`, 4 fins silencieuses |
| Secrets | `/api/vpn/diagnostic` renvoyait le compose complet (clé API, cookie Go, token NordVPN, clé de contrôle) ; `config.yaml` suivi par git contenait `control_api_key` en clair |
| Config | `config_yaml_dirty: true` (le YAML sur disque diffère de l'état chargé) |

Ces constats sont des **symptômes live** ; certains seront résolus par le
redémarrage, d'autres restent dans la feuille de route (§ 6).

---

## 2. Ce qui a été corrigé

### 2.1 Sécurité (P0)

| Correctif | Fichier | Preuve |
|---|---|---|
| `credentials.env` n'est plus écrasé : upsert, clés `VPN_CONTROL_API_KEY`, `HTTP_CONTROL_SERVER_AUTH_DEFAULT_ROLE`, `NORDVPN_TOKEN` préservées, écriture tmp+fsync+replace, injection par saut de ligne refusée | `dashboard/api.py` `_write_credentials_env` | `tests/test_vpn_credentials_security.py` |
| `/api/vpn/diagnostic` masque les valeurs secrètes du `docker compose config` | `dashboard/api.py` `_redact_compose_text` | même fichier de test |
| `control_api_key` retiré de la valeur suivie dans `config.yaml` (déplacé vers `credentials.env`/env) | `config.yaml`, `vpn/manager.py` | `config_audit --strict` : 0 clé morte/manquante |

**Action opérateur restante** : la clé de contrôle a été exposée dans un
fichier suivi par git ; elle doit être **révoquée/rotée**, puis reportée
uniquement dans `credentials.env`. Le token NordVPN et la clé API présents
dans le compose ont la même exposition historique.

### 2.2 Stations (P0/P1)

| Correctif | Fichier | Effet |
|---|---|---|
| `stop_station` exclut la station (`_disabled=True`), force `disconnected`, vide l'IP et supprime le conteneur | `ops/fleet.py` | le pool ne re-sélectionne plus un conteneur supprimé |
| `heal` / `connect_all` ignorent les stations `_disabled` ; `start_station`/`connect_station` les réactivent explicitement | `ops/fleet.py` | plus de résurrection des stations désactivées |
| `connect()` refuse une station `_disabled` | `vpn/manager.py` | garde-fou supplémentaire |
| `station_count` est persisté même quand le runtime correspond déjà | `opencode.py` `_apply_station_count` | plus de miroir YAML périmé après upscale/downscale |
| `forced_pool` géo : un pays inconnu n'est plus accepté | `free/pool.py` `_station_usable` | une politique stricte ne peut plus sortir hors pays autorisés |

### 2.3 Rotation / quotas (P0)

| Correctif | Fichier | Effet |
|---|---|---|
| `_per_station` met à jour `last_confirmed_ip` après un changement d'IP hors `switch_ip` | `free/pool.py` | fin du reset de compteur en boucle ; un 429 n'est plus effacé par une simple lecture `/api/vpn-status` ou `/metrics` |
| `_await_rotation` attend le **changement d'IP**, plus le worker éternel | `free/pool.py` | le chemin « attendre une IP fraîche » réussit dès que la rotation a atterri |
| Les refus locaux (storm, breaker, fail-fast, downscale) ne sont plus comptés comme des pannes amont | `free/pool.py` | fin du gel de flotte auto-entretenu |
| `dual_station` reflète le flag de config ; `station_count` est exposé | `free/pool.py` | plus de contradiction YAML/API |
| `/api/drift` compare les ids au **leg réel** (`route_leg`/`catalog_legs`) et au catalogue vivant | `opencode.py` | 54 faux orphelins → 3 réels |
| Le trafic **payant** géo-forwardé ne passe plus par `on_request()` | `opencode.py` `_open_via_pool` | le quota gratuit n'est plus consommé par du payant |
| Pause 429 même avec une **clé unique** (non-stream et stream), pas de rejeu de la même clé pausée | `opencode.py` | plus de boucle 429 mono-clé ; le breaker global 429 est alimenté |
| `_pause_key_for_quota_reset` applique la pause par défaut quand aucune métadonnée workspace n'existe | `opencode.py` | une clé sans `go_workspace_id` n'est plus re-testée à chaque tour |
| `select_next_key` utilise la clé `.env` valide si toutes les clés listées sont pausées | `core/keys.py` | la clé de secours n'est plus ignorée |
| `save_api_keys` atomique, refuse les lignes sans clé | `config/settings.py` | plus de fichier corrompu ni de clé vide sélectionnable |
| Merge des clés API par alias **ou** valeur masquée ; l'UI n'envoie plus seulement la nouvelle ligne | `dashboard/api.py`, `dashboard-ui/src/pages/Config.svelte` | sauvegarder une clé n'efface plus les autres |
| Parse quota Go : les 3 fenêtres sont exigées ; un parse partiel est une erreur | `dashboard/quota.py` | plus de faux « 0 % / 100 % restant » |
| L'UI n'affiche plus un quota comme sain quand `status=error` | `dashboard-ui/src/pages/Quotas.svelte` | l'erreur est visible |
| Le cache SWR n'est marqué frais que si au moins un fetch a réussi | `dashboard/quota.py` | un échec persistant ne fige plus l'état « fresh » |

### 2.4 Cache (P0)

| Correctif | Fichier | Effet |
|---|---|---|
| La clé de cache inclut un **namespace** : chemin, modèle résolu, endpoint/jambe, type de handler, hash auth/betas/versions | `server/cache.py`, `opencode.py` `_response_cache_namespace` | plus de collision `/v1/messages` ↔ `/v1/chat/completions`, ni inter-tenant |
| `previous_response_id` et `conversation` ne sont plus exclus de la clé | `server/cache.py` | plus de réponse d'une autre conversation stateful |
| Une réponse 200 `{"error": ...}` n'est plus mise en cache | `server/cache.py` | plus de faux succès rejoué 300 s |
| Compteurs hit/miss/`skipped_errors` exposés | `server/cache.py` | diagnostic du cache |
| Les campagnes de tests vérifient la séparation protocole/tenant | `tests/test_responses_conformance_l15.py` | non-régression |

### 2.5 Contexte / traductions (P0)

| Correctif | Fichier | Effet |
|---|---|---|
| `instructions` top-level Responses est converti en `system` Anthropic | `app/protocol/mapping.py` | le prompt système n'est plus perdu |
| `output_text` et `refusal` de l'historique assistant sont acceptés | idem | les réponses assistant précédentes ne sont plus vidées |
| Les `tool_result` sont émis **immédiatement** après le `tool_use`, même si l'assistant final suit | idem (P4 et P6) | plus de 400 « tool_use without tool_result » |
| Les IDs `tool_use`/`tool_result` sont scrubbés de façon symétrique ; les résultats orphelins sont ignorés | idem | plus d'orphelins |
| Les documents utilisent `title` (schéma Anthropic), plus `name` | idem | conformité API |
| `stop` string est converti en `stop_sequences` tableau ; `temperature`/`top_p` sont clampés 0–1 | idem | plus de 400 sur des corps Chat valides |
| Usage Anthropic→Chat/Responses : `prompt_tokens`/`input_tokens` incluent le cache (non caché + cache read + cache write) | idem | auto-compaction et coût corrigés |
| `finish_reason="tool_calls"` quand la réponse porte des outils | idem | les SDK exécutent bien l'outil |
| `estimate_input_tokens` accepte une chaîne (mode `keep_tokens`) | `protocol/tokens.py` | la compaction réduit réellement l'historique |
| `keep_recent_pairs <= 0` garde au moins la paire courante | `app/compaction/truncate.py` | plus de checkpoint seul |
| SSE de compaction : `input_tokens` n'est plus remis à 0 | `opencode.py` | usage correct |
| `_extract_text` tolère un `source` non-dict | `app/protocol/mapping.py` | plus de 500 sur corps malformé |

Les fixtures golden ont été régénérées pour les 11 contrats impactés
(`docs/v1-response-golden/`) et relues : usage inclusif, `title`,
`output_text`, ordre `tool_result`, `finish_reason`, orphelins.

### 2.6 Alignement client officiel

| Point | État |
|---|---|
| UA v2 (`opencode/prod/<version>/<client>`) | version alignée sur le binaire officiel installé **2.2.2** (`vpn/manager.py`, `opencode.py`) |
| UA free ai-sdk | conservé sur les versions **mesurées** 1.18.31/1.19.3 ; 1.19.5 n'a pas été capturé, donc non ajouté (règle « ne pas inventer une version ») |
| En-têtes free | trio officiel `x-opencode-session-id` + `x-session-affinity` + `x-session-id`, quartet legacy retiré |
| `tool_choice: "auto"` | conservé (mesuré sur le client officiel) |
| `max_tokens` / `stream: true` / shims free | conservés (load-bearing pour la porte free) |
| Reprises HTTP | 2 tentatives, statuts officiels, `retry-after` honoré (déjà en place) |
| Conversions Anthropic↔OpenAI↔Responses | raison d'être du proxy, conservées et corrigées |

### 2.7 Gestion du contexte / compaction — alignement officiel

Le code officiel V2 (`packages/core/src/session/compaction.ts`, binaire 2.2.2) a
été relu et comparé au proxy :

| Point | Décision |
|---|---|
| Gabarit, règles, `filled()`, NUDGE, prompt de mise à jour | déjà verbatim ; conservés |
| Budget récent | `keep_tokens=15000` par défaut (officiel `compaction.keep.tokens`) ; `keep_recent_pairs=4` reste le repli legacy |
| Déclenchement | `enabled=true`, `auto=true` : trigger proactif `context_limit − max(16k, 10 %)` (fenêtres du catalogue, `limit.input` prioritaire ; 16k seulement si fenêtre ≥ 32k) + récupération sur 400-overflow |
| Requête résumeur | la conversation réelle (system/tools/messages ou instructions/tools) est envoyée au résumeur payant, avec le prompt ajouté en message user → réutilise le cache de prompt. Repli transcript texte si la forme d'API diffère, si la jambe est free, ou si l'amont rejette la requête pleine (400/413/422) |
| Checkpoint | format officiel exact : ligne d'explication, `<summary>`, `<recent-context>` sérialisé (`[User]:`, `[Assistant]:`, `[Assistant tool call]:`, `[Tool result]:`, sorties coupées à 1 250 chars) |
| Queue récente | sérialisée dans `<recent-context>` ; la dernière paire user (requête qui a overflowé) reste verbatim pour que le retry ait une instruction à traiter |
| Corrective | même requête + NUDGE quand l'historique complet est utilisé ; sinon texte + « Previous attempt » |
| Compaction native | `/v1/responses/compact` relayé au provider Responses ; streaming Responses-natif relayé octet par octet ; le proxy ne forge jamais d'item chiffré |
| Cache de session | checkpoint proactif caché par en-tête de session (`x-opencode-session-id`/`x-session-id`) et réutilisé tant que le préfixe client correspond ; sans en-tête, réactif seulement |
| Gemini | dialecte natif `google/v1beta` converti (Chat/Anthropic/Responses → Chat → Gemini, retour) | **implémenté** (`app/protocol/gemini.py`) |
| Divergence assumée | sous-sessions non modélisées au-delà de l'isolation par en-tête |

Détail complet : `docs/compaction.md` § 1 bis.

---

## 3. Ce qui reste à corriger (priorisé)

### P0 — corrigés après l'audit initial

1. `connect_next()` conserve désormais la référence de rotation détachée
   jusqu'à sa fin réelle ; un downscale ne peut plus recréer le conteneur.
2. Breaker half-open : un échec de sonde pendant le warm-up est enregistré
   (plus de sonde armée à vie).
3. Rotation qualité : mêmes gardes anti-flap / pause maintenance que la
   rotation latence (`can_soft_rotate`, `paused`, `global_paused_until`).
4. Quota sous concurrence : incréments sous `_pool_pick_lock`, retry et hedge
   loser comptés sur la station qui envoie réellement la requête.
5. Anti-réutilisation d'IP : `_ip_recent()` combine registre partagé **ou**
   historique local ; `_connect_next`/pin effacent l'hostname précédent.
6. Persistance quota : `save_pool_state` toutes les 25 s (boucle 30 s), plus
   seulement sur bad-mark/rotation.
7. Attribution hostname : plus de pin `hostnames[0]` présenté comme serveur
   réel ; l'hostname est résolu depuis les logs après connexion.
8. Compaction free/mixed : détection du prompt résumeur Responses, `history_api`
   distinct de l'`api` résumeur, checkpoint officiel.
9. Clamp des tokens de sortie : appliqué aussi aux passthroughs natifs
   (`max_tokens`, `max_completion_tokens`, `max_output_tokens`).
10. Quota Go / clé payante : **opérationnel** — cookie de workspace expiré et
    clé `.env` morte (401). Le proxy le dit maintenant explicitement ; il faut
    renouveler le cookie et la clé pour valider la moitié payante.

### P1 — corrigés après l'audit initial

- Compteurs de tokens négatifs : clamp à 0 à l'ingestion, à l'agrégat
  quotidien et à la restauration DB.
- `/api/drift` : union des jambes live (`opencode`/`opencode-go`) + exclusion
  des routes internes ; **0 orphelin** live.
- `dual_station` : `station_count` est la source ; `dual_station=False` quand
  une seule station est déclarée/utilisée.
- Cache de conversion : borne octets 64 Mo en plus de 512 entrées.
- 5ᵉ breakpoint `cache_control` : limite réappliquée après
  `_restructure_for_cache` ; le TTL `1h` du system est préservé.
- `parallel_tool_calls:false` → `disable_parallel_tool_use` ; `content_filter`
  → `refusal` ; `response.incomplete` émis pour `max_tokens`/refus.
- `_restructure_for_cache` conserve le `cache_control` d'origine (TTL inclus).
- Compaction : `keep_tokens=15000` (officiel), `enabled=true`, checkpoint
  officiel avec `<recent-context>`, requête résumeur = conversation réelle +
  prompt sur la jambe payante, repli texte sur 400/413/422.
- Quotas : retries/hedges comptés, cooldown `(modèle, IP)` consulté,
  `last_confirmed_ip` corrigé, cascade 429 limitée aux vrais quotas.
- Stations : `_docker_ops_in_flight` incrémenté autour des opérations docker,
  stations `disabled` retirées au boot, `_n_expected` ne compte plus les
  stations désactivées, `latency_rotation` partiel fusionné au lieu de
  réinitialiser les seuils.
- Alignement : bases upstream officielles par protocole ; catalogue rafraîchi
  130/37 ; découverte `/models` mappée vers les endpoints legacy qui servent
  réellement la liste.

### P1 — encore ouverts

- `/v1/responses` stream : vrai streaming quand l'amont est Responses-natif ;
  le backend Chat→Responses collecte encore avant de rejouer le SSE.
- ~~Trigger proactif officiel (`context_limit − buffer`)~~ : implémenté
  (fenêtres du catalogue + cache de checkpoint par session).
- ~~Compaction native provider : relay-only~~ : `/v1/responses/compact` est
  relayé au provider ; le proxy ne forge toujours pas d'item chiffré.
- `prompt_cache_key` est relayé sur Chat (Responses→Chat), mais pas inventé
  pour Anthropic ; sous-sessions non modélisées au-delà de l'isolation par
  en-tête de session. Gemini est implémenté (adaptateur natif).
- ~~Compaction `is_compaction_shape` peut classer un long prompt user-only
  (> 1000 chars) comme compactage~~ : corrigé — la shape implicite exige
  désormais un vocabulaire de résumé/compaction (`summar`, `compact`,
  `checkpoint`) ; un long prompt générique n'est plus compacté. `summary_lean`
  et tiny-retry restent des gardes free à surveiller.
- ~~Pause 403 globale 30 min~~ : corrigée — les 403 région/modèle sont
  maintenant *model-scoped* (`KeyPauser.pause_key(..., model=...)`), sans
  bloquer la clé pour les autres modèles ; 401/429 restent globaux.
- Message de pause quota « jusqu'au reset » alors que `max_pause` borne ;
  cache `by_key` free peu informatif.
- ~~`_apply_stack` global : no-op sur s1 puis force l'état des autres ; pas de
  lock~~ : corrigé (lock par boucle + état `_stack_effective` propagé à toutes
  les stations ciblées).
- ~~`native_passthrough` reste un flag de documentation~~ : à `false`, le
  streaming Responses-natif et `/v1/responses/compact` sont désactivés et le
  proxy retombe sur la conversion/collecte. `summary_paid_bypass`, `keep_tail`
  et `max_attempts` sont appliqués.

### P2 — maintenabilité

- Monolithe `opencode.py` (~24 k lignes) et `mapping.py` (~5,3 k lignes) :
  extraction progressive, règles d'import.
- Config YAML permissive : Pydantic + validation au boot, clés inconnues
  signalées.
- Tests : plusieurs tests verrouillaient l'ancien comportement (32
  colonnes, fixtures SSE sans double newline, UA 2.0.28, mappings free) ;
  ils ont été mis à jour dans cet audit.
- Hot-reload partiel : `latency_rotation` partiel remet les seuils par
  défaut ; `free_parallel`/`quality_preselection` partiels perdent des
  clés ; `shared_rotation_file` n'est pas réellement changé à chaud.

---

## 4. Matrice d'alignement avec le client officiel

| Élément | Client officiel | Proxy | Verdict |
|---|---|---|---|
| Endpoint chat payant | `inference/go/openai/v1/chat/completions` | `inference/go/openai/v1/chat/completions` | **aligné** |
| Endpoint chat free | `inference/openai/v1/chat/completions` | `inference/openai/v1/chat/completions` | **aligné** |
| Endpoint messages | `inference/anthropic/v1/messages` (free) / `inference/go/anthropic/v1/messages` (paid) | idem | **aligné** |
| Endpoint responses | `inference/openai/v1/responses` (free) / `inference/go/openai/v1/responses` (paid) | idem | **aligné** |
| Routage modèle→protocole | `provider.npm`/`api` du **modèle** | catalogue + config, 4 mismatches mesurés sur 87 | **partiel** |
| UA v2 | `opencode/prod/2.2.2/<client>` | idem | **aligné** |
| UA free | `opencode/1.19.5 ai-sdk/...` (dernier CLI) | 1.18.31/1.19.3 mesurés | **divergence justifiée** : seules les versions capturées sont utilisées ; la version n'est pas un facteur de porte |
| En-têtes de session free | trio `x-opencode-session-id`/`x-session-affinity`/`x-session-id` | idem | **aligné** |
| Corps free | client officiel complet (10 outils + system) | copie officielle + outils client | **aligné / load-bearing** |
| `stream: true` free | requis par la porte | forcé | **divergence justifiée** (mesurée) |
| `tool_choice: "auto"` | émis si outils | idem | **aligné** |
| `max_tokens` | émis | idem sur free ; conversion conditionnelle `max_completion_tokens` pour o-series | **aligné** |
| Reprises | 2, backoff 500 ms ×2^n, jitter, `retry-after` | idem | **aligné** |
| Erreurs | classification InvalidRequest/ProviderInternal/Unknown | idem + `core/errors.py` | **aligné** |
| Stream vide | erreur, pas 200 silencieux | EmptyStreamGuard | **aligné** |
| Usage cache | inclusif OpenAI, non caché Anthropic | corrigé dans les deux sens | **aligné** |
| `cache_control` | 4 breakpoints max | 4 max réappliqués après restructure ; TTL préservé | **aligné** |
| Conversion Anthropic↔OpenAI | n'existe pas | cœur du proxy | **divergence voulue** |
| Compaction serveur | auto proactif + résumé natif/context-epoch | récupération overflow + résumé officiel (gabarit, keep 15k, checkpoint) | **aligné sur le cœur, trigger proactif non reproduit** |
| Pool VPN / rotation IP | n'existe pas | cœur du proxy | **divergence voulue, load-bearing** |
| Découverte free / dashboard / quotas | n'existe pas | cœur du proxy | **divergence voulue** |

**Règle appliquée** : tout ce qui est commun (wire, UA, en-têtes, reprises,
usage, erreurs, streaming) doit être identique ; les divergences qui restent
sont soit **mesurées comme load-bearing** (corps free, `stream:true`, pool
d'IP), soit des **fonctions ajoutées** (conversion, compaction, dashboard,
quotas), soit des **dettes explicitement listées** ci-dessus.

---

## 5. Validation

| Contrôle | Résultat |
|---|---|
| `python -m pytest -q` | **exit 0**, aucune failure |
| `python scripts/ci_contracts.py` | **513 tests, 0 échec** (29 fichiers) |
| `python scripts/config_audit.py --strict` | 336 clés, 0 morte, 0 manquante |
| `npm run build` (dashboard-ui) | OK, bundle `static/dist` reconstruit |
| `python scripts/precompress.py` | 48 sidecars gzip/brotli |
| Nouveaux tests | sécurité credentials, fleet stop, quota anchor, merge clés API, fallback `.env`, parse quota, 429 mono-clé, conversions P0, checkpoint officiel + keep 15000 + résumeur full-history |

**État final** : instance redémarrée (PID **2932**), health `ok` après
l'expiration de la pause de clé, `station_count=6`, **6/6 stations
connectées** (s1/s3 en WireGuard),
`/api/drift` 0 orphelin, smoke free `/v1/chat/completions` et `/v1/messages`
en 200. Gemini est routé nativement ; la clé payante morte produit désormais
un 401/502 upstream explicite. Le watchdog stations a été audité et corrigé
(seuil egress, budget, arrêt/rotation, breaker superviseur, observabilité) :
voir `docs/AUDIT_WATCHDOG_STATIONS_2026-10-30.md`. Le cookie Go et la clé
payante restent à renouveler côté opérateur.

---

## 6. Ordre d'exécution recommandé

1. ~~Redémarrer le proxy et vérifier health/drift/quotas~~ : fait (PID 2932,
   6 stations, 0 orphelin, quota Go en erreur d'authentification explicite).
2. **Révoquer/roter** la clé de contrôle, le token NordVPN et la clé API
   qui ont été exposés dans le compose / `config.yaml`.
3. **Réparer la paire payante** (nouvelle clé OpenCode Go + cookie) ou
   assumer la jambe free seule ; sans cela, les 401 payants restent.
4. ~~P0 rotation : task détachée, breaker half-open, anti-flap qualité,
   quota sous concurrence~~ : fait.
5. ~~P0 anti-réutilisation / persistance quota / hostname~~ : fait.
6. ~~P0 conversion/compaction : résumeur free, API mixte, clamp natif~~ : fait ;
   trigger proactif et compaction native provider faits.
7. ~~P1 alignement : bases upstream officielles, cache de conversion borné~~ :
   fait. Restent `prompt_cache_key` Chat et sous-sessions (Gemini fait).
8. **P2** : découpage, config typée, hot-reload complet, benchmarks e2e.

---

## 7. Fichiers touchés par cet audit

- Sécurité / dashboard : `dashboard/api.py`, `dashboard/quota.py`,
  `dashboard-ui/src/pages/Config.svelte`, `dashboard-ui/src/pages/Quotas.svelte`,
  `static/dist/` (build).
- Quotas / clés : `core/keys.py`, `config/settings.py`, `config.yaml`.
- Stations : `ops/fleet.py`, `vpn/manager.py`, `opencode.py`, `.env`
  (`VPN_TYPE_STATION1/3=wireguard`), `docker-compose.yml` (stations 1-10).
- Rotation / pool : `free/pool.py`.
- Cache : `server/cache.py`, `opencode.py`.
- Contexte / conversions : `app/protocol/mapping.py`,
  `app/compaction/truncate.py`, `protocol/tokens.py`.
- Gemini : `app/protocol/gemini.py`, `config/models_catalog.py`,
  `config/live_models.py`, `config/catalog_routes.json`.
- Tests : nouveaux fichiers `tests/test_*_p0.py`, `tests/test_gemini_adapter.py`
  + mises à jour des tests figés sur l'ancien comportement (DB 35 colonnes,
  fixtures SSE, UA 2.2.2, mappings free explicites, usage inclusif).
- Golden : `docs/v1-response-golden/*.json` régénérés et relus.
- Watchdog : audit dédié `docs/AUDIT_WATCHDOG_STATIONS_2026-10-30.md`
  (egress, budget de restart, rotations, breaker superviseur, tests).
