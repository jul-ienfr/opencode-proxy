# Audit — impersonation et format des requêtes free

**Date** : 2026-11-05  
**Périmètre** : jambe gratuite `opencode` — en-têtes, identité, TLS, corps, IDs, endpoint.  
**Méthode** : capture locale du client officiel installé, sans compte, plus rejeu du proxy contre un serveur de capture.  
**Statut** : audit terminé ; aucune correction appliquée au code de production.

---

## 0. Synthèse exécutive

Le client officiel n'est pas un seul client. Il existe **deux lignes incompatibles** :

| Ligne | Version installée | Wire | Corps |
|---|---|---|---|
| npm `opencode-ai` | **1.19.5** | 13 en-têtes, pas de tracing, `x-opencode-request` | 11 outils, `max_tokens`, `tool_choice: auto` |
| desktop / zip | **2.2.5** | 16 en-têtes, `b3`/`traceparent`, quartet de session | 13 outils, `max_completion_tokens`, pas de `tool_choice` |

Le proxy actuel est un **hybride** : il force l'UA, le client et le corps 2.2.5 sur des identités 1.18.31 / 1.19.0 / 1.19.3 / 1.19.5, dont le TLS reste celui de l'ancienne ligne. Trois identities sur quatre produisent donc une combinaison qui n'existe chez aucun client réel :

- UA `opencode/latest/2.2.5/cli` ;
- TLS Bun 1.3.14 (profil 1.18.31 / 1.19.3) ;
- corps 2.2.5 (13 outils, `max_completion_tokens`) ;
- en-têtes 2.2.5 mais **ordre différent** (tracing après `Content-Length`).

Les points les plus graves, par ordre :

1. **Incohérence version / TLS / corps** sur 3 des 4 versions en rotation (risque fonctionnel et tell majeur).
2. **Ordre des en-têtes faux sur le chemin per-IP réel** : `b3`, `traceparent`, `x-session-affinity`, `x-session-id` partent **après `Content-Length`** au lieu des positions 4, 5, 10, 11. La suite de tests ne le voit pas parce qu'elle teste le chemin sans IP.
3. **Outils « placeholder »** (`properties: {}`, « DO NOT CALL ») envoyés pour les clients non officiels, alors que les mesures internes du projet indiquent que ce type de définition prend un 403. Contradiction non résolue.
4. **Fuite du prompt réel et du project ID** : le prompt système figé contient le chemin, le texte d'`AGENTS.md` et la liste des skills du workspace ; `apply_spec` remplace `x-opencode-project: global` par le hash du dépôt réel. Cela annule une partie de la séparation par IP.
5. **Trace instable** : l'ID de trace officiel est stable sur les requêtes d'une même session ; le proxy en mint un nouveau à chaque requête.
6. **Session tournante toutes les 30 min** et **`msg_` neuf à chaque egress** : divergences assumées pour l'anti-corrélation, mais détectables et parfois fonctionnelles.

Le TLS, lui, est **exact** pour 1.18.31, 1.19.5 et 2.2.5 (ce dernier réutilise le profil 1.19.5) : le replay curl_cffi reproduit les ClientHello officiels (JA3/MD5 identiques). Ce n'est donc pas le point à corriger en premier.

### 0.1 Incident en cours (2026-11-05)

La jambe free est signalée **down** : l'opérateur ne voit que des 429 « quota dépassé », alors que le quota n'est pas prouvé épuisé. Les traces locales de 24 h montrent :

- 2 377 requêtes, toutes issues du smoke test `python-requests` sur `jev-1.13-free` ;
- 2 141 `502 all tunnels failed`, 227 `429` avec `Retry-After` de **42–58 s** ;
- aucun trafic client réel depuis le **2026-11-04 20:28 UTC** (dernier `deepseek-v4.1-flash-free` en 200).

Un `Retry-After` de 42–58 s est un **rate-limit court**, pas le bucket journalier (qui pointe vers minuit UTC avec plusieurs heures de RA). Le 502 est un problème de tunnel/egress. Surtout, le corps `Free usage limit reached for jev-1.13-free. Retry after 54s.` est produit **par le proxy lui-même** quand `model_global_remaining > 0` : la requête ne part même pas vers l'amont. C'est un cooldown local, pas un quota fournisseur ; il doit être purgé et vérifié avant toute rotation (Plan M du runbook).

La première campagne live du 2026-11-05 22:47–22:52 UTC a testé les profils exacts 2.2.5 CLI et desktop sur l'IP directe : les deux reçoivent `FreeUsageLimitError` (RA ~6 800–6 956 s), **aucun 403/`FreeTierError`**. Le corps et l'ordre exacts passent donc la couche signature ; c'est le bucket IP qui est épuisé. Mais l'infrastructure est bloquée : `0/6` stations utilisables, `AUTH_FAILED` OpenVPN, SOCKS `user rejected`, WireGuard sans DNS. Le prérequis est donc le **Plan N (credentials/egress)** ; sans une IP fraîche, on ne peut pas distinguer CLI et desktop au-delà de la couche quota.

**Mise à jour 2026-11-06** : l'ordre 2.2.5 a été corrigé et vérifié end-to-end (`b3` en 4, `traceparent` en 5, quartet en 8–11) ; le project `global` n'est plus remplacé par le hash du dépôt, et le trace ID est stable par session. Le token NordVPN fourni a été accepté, les credentials service régénérés, et l'`AUTH_FAILED` s'est révélé être une **limite de sessions simultanées** (six stations + un test = refus ; six arrêtées + un test = auth immédiate). Les profils CLI **et** desktop exacts, testés dans le namespace VPN sur `87.120.102.86`, reçoivent `FreeUsageLimitError` avec le même `Retry-After` (~71 431 s) : le wire passe la couche signature, l'IP est épuisée.

**Clean-room 2026-11-06** : client officiel Linux 1.19.5 neuf, workspace vide, HOME vide, conteneur neuf. Sur l'IP directe fraîche, le client officiel rend **200 `pong`** et le replay exact rend **200** ; sur l'IP VPN partagée `64.238.204.228`, le client officiel rend **429** et le replay exact rend **429**. Le proxy local, redémarré en direct, rend **200 `pong`**. Le 429 VPN est donc bien un quota d'IP, pas une différence d'impersonation. Le diagnostic ci-dessus reste valable pour le corps `compat`, les placeholders et le prompt.

Le diagnostic de cet audit reste la cause **latente** probable des faux quotas, mais il ne faut pas rotationner une IP ni changer le wire avant d'avoir classé le 429 réel du client. Le runbook `docs/PLAN_RETABLISSEMENT_FREE_2026-11-05.md` est prioritaire.

---

## 1. Méthode de capture

### 1.1 Clients

| Client | Chemin | Version | Source |
|---|---|---|---|
| Desktop bundlé | `%LOCALAPPDATA%\Programs\@opencode-aidesktop\resources\opencode-cli.exe` | `opencode v2.2.5` | installation desktop existante |
| npm | `%TEMP%\oc-audit\node_modules\opencode-ai\bin\opencode.exe` | `1.19.5` | `npm install opencode-ai@1.19.5`, installation temporaire |
| npm global | `%APPDATA%\npm\opencode.cmd` | `1.18.31` | installation existante, utilisé pour le TLS |

Le feed desktop `https://opencode.ai/update/api/latest/desktop/opencode/latest.yml` annonce **2.2.5** (release 2026-10-30). Le registre npm annonce **1.19.5** (`latest`). Ce sont deux canaux distincts : comparer les numéros naïvement (2.2.5 > 1.19.5) n'a pas de sens pour le canal npm.

### 1.2 Profil jetable

Chaque client est lancé avec :

- `HOME`, `USERPROFILE`, `XDG_*`, `APPDATA`, `LOCALAPPDATA` dans un répertoire temporaire ;
- un `opencode.jsonc` qui pointe le provider `opencode` vers `http://127.0.0.1:<port>/v1` ;
- modèle `opencode/deepseek-v4.1-flash-free` ;
- variables `OPENCODE_CONSOLE_TOKEN`, `OPENCODE_API_KEY`, `OPENCODE_GO_AUTH_COOKIE` retirées ;
- commande `run ... ping --format json` (2.2.5 avec `--standalone`, 1.19.5 sans).

Aucun compte, aucun credential réel. Le serveur local répond en SSE et enregistre les octets bruts.

### 1.3 TLS

Un listener TCP local reçoit le ClientHello sur `https://localhost:<port>/v1` (SNI `localhost`), sans terminer le handshake. Le ClientHello est parsé (ciphers, extensions, groups, sigalgs, ALPN) et comparé aux constantes du proxy.

### 1.4 Artefacts

Tous les fichiers sont dans `.artifacts/audit-2026-11-05/` (non versionné) :

| Fichier | Contenu |
|---|---|
| `capture-1.19.5.json`, `1.19.5-0/1.json` | requêtes HTTP complètes 1.19.5 |
| `capture-2.2.5.json`, `2.2.5-0/1.json` | requêtes HTTP complètes 2.2.5 desktop |
| `tls-1.18.31-sni.json`, `tls-1.19.5-sni.json`, `tls-2.2.5-sni.json` | ClientHello avec SNI |
| `headers-*.json` | en-têtes émis par `_official_free_headers` |
| `e2e-1.19.5.json` | requête réelle du proxy via curl_cffi vers un serveur local |
| `body-probe.json` | transformations `_free_wire_body` |
| `tls-exact.json` | replay TLS avec les kwargs exacts du pool |
| `capture_official.py`, `capture_tls.py`, `probe_*.py` | outillage de mesure |

### 1.5 État de migration observé

Le dépôt est en cours de bascule 1.19.5 → 2.2.5, et l'audit capture cet état intermédiaire :

- `free/official_payload.json.bak` est un payload **1.19.5** (11 outils) ; le fichier courant est **2.2.5** (13 outils) ;
- `free/impersonation.json.bak` porte `opencode/latest/2.2.5/desktop` ; le fichier courant force `.../cli` ;
- `free/identities.py` liste encore **1.18.31 / 1.19.0 / 1.19.3 / 1.19.5** ;
- `scripts/opencode_client_sync.py` ne suit que le feed desktop et passe `--standalone` en dur.

Ce n'est donc pas un bug isolé : c'est une migration à mi-chemin. Le plan ci-dessous la termine explicitement, au lieu de continuer à empiler des overlays.

---

## 2. Ce qu'envoie le client officiel

### 2.1 Deux profils de référence

**Profil A — npm 1.19.5 (ligne v1)**

```http
POST /v1/chat/completions HTTP/1.1
Authorization: Bearer public
Content-Type: application/json
User-Agent: opencode/1.19.5 ai-sdk/provider-utils/4.0.23 runtime/bun/1.4.4
x-opencode-client: desktop
x-opencode-project: a24ad984335289d5bb037e290b55f7fb5e04ce6b
x-opencode-request: msg_1adc7985b001oYJFtRxOhrEyTO
x-opencode-session: ses_e523867d5ffemgq8wgn44uz5vq
x-opencode-session-id: ses_e523867d5ffemgq8wgn44uz5vq
Connection: keep-alive
Accept: */*
Host: 127.0.0.1:49643
Accept-Encoding: gzip, deflate, br, zstd
Content-Length: 51026
```

Corps principal :

```json
{
  "model": "deepseek-v4.1-flash-free",
  "max_tokens": 32000,
  "messages": [ ... ],
  "tools": [ 11 outils ],
  "tool_choice": "auto",
  "stream": true,
  "stream_options": { "include_usage": true }
}
```

- 51 026 octets, 11 outils : `bash, edit, glob, grep, read, skill, task, todowrite, webfetch, websearch, write`.
- Prompt système v1 : 26 965 caractères, commence par `You are opencode, an interactive CLI tool...`.
- Requête de titre : 2 503 octets, clés `model, max_tokens, temperature, reasoning_effort, messages, stream, stream_options`, 0 outil, système 2 096 caractères.

**Profil B — desktop bundlé 2.2.5 (ligne v2)**

```http
POST /v1/chat/completions HTTP/1.1
Authorization: Bearer public
Content-Type: application/json
User-Agent: opencode/latest/2.2.5/desktop
b3: 4b60f73ae9773da9341253b88016ee21-4e2b356e6ae5e7da-1-53bed1d1e36b9bd8
traceparent: 00-4b60f73ae9773da9341253b88016ee21-4e2b356e6ae5e7da-01
x-opencode-client: desktop
x-opencode-project: a24ad984335289d5bb037e290b55f7fb5e04ce6b
x-opencode-session: ses_e52382b43ffe5kVX99pBPvGjGB
x-opencode-session-id: ses_e52382b43ffe5kVX99pBPvGjGB
x-session-affinity: ses_e52382b43ffe5kVX99pBPvGjGB
x-session-id: ses_e52382b43ffe5kVX99pBPvGjGB
Connection: keep-alive
Accept: */*
Host: 127.0.0.1:58053
Accept-Encoding: gzip, deflate, br, zstd
Content-Length: 43285
```

Corps principal :

```json
{
  "model": "deepseek-v4.1-flash-free",
  "messages": [ ... ],
  "tools": [ 13 outils ],
  "stream": true,
  "stream_options": { "include_usage": true },
  "max_completion_tokens": 32000
}
```

- 43 285 octets, 13 outils : `edit, experimental_image_generate, glob, grep, question, read, shell, skill, subagent, webfetch, websearch, write, execute`.
- Prompt système v2 : 26 993 caractères, commence par `You are an AI agent running in OpenCode...`.
- Requête de titre : 2 125 octets, clés `model, messages, stream, stream_options`, 0 outil, système 1 806 caractères.
- **Le zip standalone 2.2.5** (référence `free/captures/2.2.5.json`) a la même structure, les mêmes 13 outils et le même prompt (hors date) que le desktop ; seules changent les valeurs variables (message utilisateur, IDs, trace) et :
  - `User-Agent: opencode/latest/2.2.5/cli` ;
  - `x-opencode-client: cli`.

### 2.2 Ordre exact des en-têtes

**1.19.5 — 13 en-têtes**

```text
Authorization, Content-Type, User-Agent, x-opencode-client, x-opencode-project,
x-opencode-request, x-opencode-session, x-opencode-session-id,
Connection, Accept, Host, Accept-Encoding, Content-Length
```

**2.2.5 — 16 en-têtes**

```text
Authorization, Content-Type, User-Agent, b3, traceparent,
x-opencode-client, x-opencode-project,
x-opencode-session, x-opencode-session-id, x-session-affinity, x-session-id,
Connection, Accept, Host, Accept-Encoding, Content-Length
```

Le quartet `x-opencode-session` / `x-opencode-session-id` / `x-session-affinity` / `x-session-id` porte **la même valeur** dans une session simple. `x-opencode-request` n'existe plus en 2.2.5.

### 2.3 Sémantique du tracing

Sur les deux requêtes d'un même run (titre puis principal) :

- `b3` et `traceparent` partagent le **même trace ID** (`4b60f73a...`) ;
- le **span ID change** à chaque requête (`4e2b...` puis `a8de...`) ;
- `b3` est au format `traceid-spanid-1-parentspanid` ;
- `traceparent` est au format W3C `00-traceid-spanid-01`.

Le trace ID est donc **stable par session/processus**, pas par requête. C'est le point que le proxy ne respecte pas.

### 2.4 TLS

ClientHello avec SNI (`localhost`) :

| Client | Taille | Extensions | Groups | Sigalgs | JA3 MD5 |
|---|---:|---|---|---|---|
| 1.18.31 | 517 | `0,23,65281,10,11,35,16,5,13,18,51,45,43,21` | `29,23,24` | 9 | `d871d02cecbde59abbf8f4806134addf` |
| 1.19.5 | 1 500 | `0,23,65281,10,11,35,16,5,13,18,51,45,43` | `4588,29,23,24` | 11 | `1523504b38f0fae0d881d4b6554aac1b` |
| 2.2.5 | 1 500 | identique 1.19.5 | `4588,29,23,24` | 11 | `1523504b38f0fae0d881d4b6554aac1b` |

Le profil TLS 1.19.5 est donc **réutilisable tel quel pour 2.2.5**. Le profil 1.18.31 reste distinct (Bun 1.3.14, padding 21, pas de courbe 4588).

Rejeu exact du proxy (mêmes kwargs que le pool `curl_cffi`, avec `impersonate: chrome131` + `ja3` + `extra_fp`) :

| Version | Extensions | Groups | MD5 | Verdict |
|---|---|---|---|---|
| 1.18.31 | `0,...,43,21` | `29,23,24` | `d871d02...` | identique |
| 1.19.5 | `0,...,43` | `4588,29,23,24` | `1523504...` | identique |

**Conclusion TLS : conforme.** Le seul problème est l'*association* de ce TLS à un UA 2.2.5 pour les identities 1.18.31 / 1.19.0 / 1.19.3.

### 2.5 IDs

- Format `ses_` / `msg_` : 30 caractères, 12 hex + 14 base62. Le proxy respecte ce format.
- 1.19.5 : `x-opencode-request` = ID du message utilisateur, stable sur les retries d'un même message.
- 2.2.5 : plus de `x-opencode-request` ; l'identité est portée par les quatre en-têtes de session.
- `x-opencode-session` = *affinity* (peut différer de `session.id` dans un fork) ; `x-opencode-session-id` = `session.id`.
- `x-opencode-parent-session-id` apparaît uniquement pour un sous-agent. Non observé sur un run simple.

### 2.6 Endpoint, auth, transport

- `POST /inference/openai/v1/chat/completions` en production (chemin confirmé par `free/captures/2.2.5.json`).
- `Authorization: Bearer public` pour le free.
- `Content-Type: application/json`, JSON compact.
- HTTP/1.1 uniquement (ALPN `http/1.1`), pas de H2.
- `Accept: */*`, `Connection: keep-alive`, `Accept-Encoding: gzip, deflate, br, zstd`.
- Aucun cookie, aucun `Accept-Language`, aucun `sec-ch-ua`, aucun `x-api-key`.

---

## 3. Ce qu'envoie le proxy

### 3.1 En-têtes — chemin per-IP réel

Mesure end-to-end (`e2e-1.19.5.json`) : requête réelle envoyée par `_do_free_request_curl_cffi` avec une identité 1.19.5 et l'`apply_spec` 2.2.5 actuel.

```text
1  Authorization
2  Content-Type
3  User-Agent              opencode/latest/2.2.5/cli
4  x-opencode-client       cli
5  x-opencode-project      9194486223361eda2f459b81ac93aedcb0bbfacf
6  x-opencode-session      ses_...
7  x-opencode-session-id   ses_...
8  Connection
9  Accept
10 Host
11 Accept-Encoding
12 Content-Length
13 b3
14 traceparent
15 x-session-affinity
16 x-session-id
```

Comparé au 2.2.5 officiel :

| En-tête | Position officielle | Position proxy | Écart |
|---|---:|---:|---|
| `b3` | 4 | 13 | +9 |
| `traceparent` | 5 | 14 | +9 |
| `x-opencode-client` | 6 | 4 | −2 |
| `x-opencode-project` | 7 | 5 | −2 |
| `x-opencode-session` | 8 | 6 | −2 |
| `x-opencode-session-id` | 9 | 7 | −2 |
| `x-session-affinity` | 10 | 15 | +5 |
| `x-session-id` | 11 | 16 | +5 |
| `Connection` | 12 | 8 | −4 |

Cause : `_OPENCODE_HEADER_ORDER_119` ne contient ni `b3`, ni `traceparent`, ni `x-session-affinity`, ni `x-session-id`. `apply_spec` ajoute ces clés **après** les clés existantes, et libcurl les place après `Content-Length` car elles ne figurent pas dans `header_order`.

Le fixture `free/captures/2.2.5.json` a, lui, le bon ordre. C'est `opencode_client_sync.build_spec` qui trie les en-têtes (`sorted(headers)`) et `apply_spec` qui les réinsère dans l'ordre du dict d'entrée, pas dans l'ordre du fixture.

### 3.2 En-têtes — chemin sans IP (tests, sondes)

`_official_free_headers()` sans `egress_ip` produit 14 en-têtes, sans `x-opencode-session` :

```text
Authorization, Content-Type, User-Agent, x-opencode-session-id,
x-session-affinity, x-session-id, Connection, Accept, Host, Accept-Encoding,
x-opencode-client, x-opencode-project, b3, traceparent
```

C'est ce chemin que `tests/test_official_client_parity.py::test_header_set_exact` vérifie. Il ne correspond ni au 2.2.5 officiel (16 en-têtes, `x-opencode-session` présent, `b3` en 4e), ni au 1.19.5 officiel (13 en-têtes, `x-opencode-request` présent). **La suite de tests valide un wire qui n'est pas le wire de production.**

### 3.3 Corps

| Cas | Comportement proxy | Écart |
|---|---|---|
| Corps 2.2.5 officiel | passe tel quel | conforme (hors en-têtes) |
| Corps 1.19.5 officiel | passe tel quel | corps v1 conservé, en-têtes 2.2.5 → hybride |
| Client sans système officiel | injecte le payload 2.2.5 + note | corps 2.2.5 modifié |
| Client sans outil | injecte 13 outils + `tool_choice: auto` + note | 2.2.5 n'a pas de `tool_choice` |
| Client avec outils | 13 placeholders + extras | placeholders non conformes aux mesures |
| Titre / summary | passe ou injecte selon le marqueur | pas de template 2.2.5 dédié |

Exemple mesuré sur un client « Hermes » (`body-probe.json`) :

```text
clés      model, messages, tools, tool_choice, stream, stream_options, max_completion_tokens
outils    14 = 13 placeholders + read_file
tool_choice auto
système   26 993 (officiel) + 3 344 (système client tronqué + note)
octets    34 273
```

Le 2.2.5 officiel n'a **ni `tool_choice` ni 14e outil**. Les 13 définitions officielles ont été remplacées par des placeholders ; avec `read_file`, les 14 outils totalisent 3 105 octets, avec `properties: {}` et `DO NOT CALL`. Or les commentaires de mesure du projet (`opencode.py`, section gate 2026-10-15) disent exactement l'inverse :

> description placeholder (« Do not use this tool... ») → 403 même avec le bon nom ;  
> `properties: {}` vide → 403 même avec description réaliste.

C'est une **contradiction non résolue** dans le dépôt : le code envoie ce que ses propres mesures disent refusé. Il faut une mesure A/B avant de trancher.

### 3.4 Prompt système

Le payload 2.2.5 figé contient :

```text
Today's date: Wed Nov 04 2026
...
Working directory: C:\Users\julie\Downloads\opencode-proxy-one
Workspace root folder: C:\Users\julie\Downloads\opencode-proxy-one
Platform: win32
```

Deux problèmes :

1. **Date figée** : le client frais du 2026-11-05 envoie `Thu Nov 05 2026`. Le proxy envoie la veille.
2. **Chemin réel** : le prompt contient le chemin absolu du workspace de l'opérateur. Il est envoyé à l'amont pour tous les clients non officiels. C'est une fuite d'information et un fingerprint fort.

Le corps 2.2.5 frais est identique au payload figé **à la seule ligne de date près** (même longueur 26 993, même outils, mêmes schémas). Le reste du payload est donc fidèle ; c'est le templating qui manque.

### 3.5 Project ID

`apply_spec` fait :

```python
current_project = str(headers.get("x-opencode-project") or "").strip()
if project and (not current_project or current_project == "global"):
    headers["x-opencode-project"] = project
```

Conséquence mesurée :

```python
IpIdentity("203.0.113.9", base_project="global", persist=False).project
# -> "global"
# mais le header émis :
# -> "a24ad984335289d5bb037e290b55f7fb5e04ce6b"  (le dépôt réel)
```

Un client officiel hors dépôt git envoie `global`. Le proxy envoie le hash du dépôt réel. Ce n'est ni fidèle, ni utile pour l'anti-corrélation : toutes les IP « global » sont liées au même dépôt.

### 3.6 Trace, session, message

| Élément | Officiel 2.2.5 | Proxy | Écart |
|---|---|---|---|
| Trace ID | stable sur les requêtes d'une session | neuf à chaque requête (`_trace_ids()`) | détectable |
| Span ID | neuf par requête | neuf par requête | conforme |
| `x-opencode-session` | stable par session | stable, mais tourne toutes les 30 min (`_SES_ROTATE_S`) | détectable / fonctionnel |
| `x-opencode-request` | absent en 2.2.5, stable par message en 1.19.5 | absent en 2.2.5 ; en 1.19.5, neuf par egress | divergence assumée |
| `x-opencode-parent-session-id` | présent pour un sous-agent | absent | cas non couvert |

### 3.7 TLS

Le profil choisi par `_get_free_fp_kwargs(_ident_version)` suit la version du vault :

- 1.18.31 / 1.19.0 / 1.19.3 → Bun 1.3.14 (`d871d02...`) ;
- 1.19.5 → Bun 1.4.4 (`1523504...`).

Avec l'overlay 2.2.5, l'UA est 2.2.5 pour toutes. Seule l'identité 1.19.5 a donc un TLS cohérent avec 2.2.5. Les trois autres sont des combinaisons impossibles.

---

## 4. Matrice de divergences

Légende : **F** = fonctionnel (risque gate/quota), **P** = privacy, **D** = détection/fidélité, **T** = outillage.

| # | Dimension | Officiel 2.2.5 | Proxy actuel | Sévérité | Type |
|---|---|---|---|---|---|
| D1 | Version / TLS / UA | une installation cohérente | UA 2.2.5 + TLS 1.3.14 pour 3/4 des IP | **critique** | F, D |
| D2 | Ordre des en-têtes | `b3` 4e, `traceparent` 5e, session 8–11 | tracing après `Content-Length` | **haute** | D |
| D3 | Outils | 13 définitions officielles complètes | placeholders `properties: {}` + note | **haute** | F |
| D4 | `tool_choice` | absent | `auto` ajouté aux clients non officiels | moyenne | D |
| D5 | Project `global` | `global` | hash du dépôt réel | **haute** | P |
| D6 | Prompt système | date + chemin de la machine cliente | date figée + chemin réel de l'opérateur | **haute** | P, D |
| D7 | Trace ID | stable par session | neuf par requête | moyenne | D |
| D8 | Session TTL | persistant | rotation 30 min | moyenne | F, D |
| D9 | `x-opencode-request` | absent en 2.2.5 ; stable par message en 1.19.5 | absent en 2.2.5 ; neuf par egress en 1.19.5 | moyenne | D |
| D10 | Client / UA | `cli` (zip) ou `desktop` (app) | `cli` forcé pour tous | moyenne | D |
| D11 | Titre / summary | templates dédiés | pas de template 2.2.5 dédié | moyenne | F, D |
| D12 | `x-opencode-parent-session-id` | présent pour un sous-agent | absent | basse | D |
| D13 | `x-opencode-directory` / `workspace` | absents des captures | ajoutés sur certains chemins | basse | D |
| D14 | Responses | non capturé | shim `read` + `prompt_cache_key` | inconnue | F |
| D15 | Outil de sync | capture multi-requêtes, ordre, canal | `--standalone` codé en dur, ordre perdu, un seul corps | moyenne | T |
| D16 | Tests de parité | wire réel | testent le chemin sans IP | **haute** | T |

---

## 5. Findings détaillés

### F1 — Une seule identité doit être cohérente (critique)

Le proxy découple trois décisions :

- la version de l'identité (`free.identities.VERSION_SPECS`) ;
- l'UA / le client (`free/impersonation.json`) ;
- le corps (`free/official_payload.json`).

Rien ne garantit que les trois parlent du même client. Aujourd'hui, `apply_spec` impose 2.2.5 à toutes les identities. Pour 1.18.31 / 1.19.0 / 1.19.3, le TLS et le label de version restent anciens. C'est exactement le genre de combinaison qu'un gate peut corréler.

**Recommandation** : introduire un objet `FreeWireProfile` unique, résolu une fois par requête, qui porte version, canal, distribution, UA, client, TLS, ordre des en-têtes, template de corps et sémantique des IDs. `_official_free_headers` et `_free_wire_body` doivent recevoir ce profil, pas décider chacun de leur côté.

### F2 — L'ordre des en-têtes est faux sur le chemin réel (haute)

Le chemin per-IP ajoute `x-opencode-session`, puis `apply_spec` ajoute le tracing et l'affinité. `header_order` ne les connaît pas. Résultat mesuré : quatre en-têtes après `Content-Length`.

**Recommandation** : reconstruire le dict dans l'ordre cible et ajouter une constante d'ordre 2.2.5 complète :

```text
Authorization,Content-Type,User-Agent,b3,traceparent,
x-opencode-client,x-opencode-project,x-opencode-session,x-opencode-session-id,
x-session-affinity,x-session-id,Connection,Accept,Host,Accept-Encoding,Content-Length
```

Puis vérifier le wire réel via un test end-to-end curl_cffi, pas seulement le dict.

### F3 — Les placeholders d'outils contredisent les mesures (haute)

`_free_official_placeholder_defs` envoie `properties: {}` et « DO NOT CALL ». Les commentaires de `opencode.py` (gate 2026-10-15) et `config/free_shims.py` disent que ce type de définition prend un 403. Si c'est toujours vrai, la majorité des requêtes non officielles échouent. Si le gate a changé, il faut le documenter et retirer les avertissements contraires.

**Recommandation** : A/B sur la même IP, même seconde, avec témoin :

- A : 13 définitions officielles complètes ;
- B : 13 placeholders ;
- C : 13 placeholders + 1 outil client ;
- D : 13 définitions complètes + 1 outil client.

Mesurer 200/403/429 et le contenu. Ne pas conclure depuis le code.

### F4 — Le project `global` est remplacé par le dépôt réel (haute, privacy)

Un client hors git envoie `global`. Le proxy envoie `a24ad...`. Cela lie toutes les IP « global » au même dépôt et ne ressemble à aucun client réel.

**Recommandation** : supprimer l'override `global` dans `apply_spec`. Si l'unicité par IP est souhaitée, dériver un `global` par IP (`sha256("global|ip")[:40]`) ou laisser `global` tel quel. Ne jamais injecter la valeur du dépôt de l'opérateur.

### F5 — Le prompt système fuit le chemin réel et la date est figée (haute)

Le payload contient `C:\Users\julie\Downloads\opencode-proxy-one` et `Wed Nov 04 2026`. Il est injecté dans toutes les requêtes non officielles.

Au-delà du chemin, le prompt contient aussi le texte d'`AGENTS.md` et la liste des skills du workspace : c'est le prompt propre à cette installation, pas un prompt neutre. Injecté chez un client tiers, il expose la configuration de l'opérateur.

**Recommandation** : templater au moment de l'envoi :

- `Today's date` → date UTC courante ;
- `Working directory` / `Workspace root folder` → chemin synthétique par identité (même valeur que `x-opencode-directory` si conservé) ;
- `Platform` → `win32` si le profil est Windows, sinon la plateforme du profil ;
- `Is directory a git repo` → cohérent avec le project ID.

Mesurer si le gate accepte ces variantes (date seule, chemin seul, les deux). Si le gate exige le prompt exact, il faut au moins retirer le chemin réel et garder une date dynamique.

### F6 — Le trace ID doit être stable par session (moyenne)

`apply_spec` appelle `_trace_ids()` à chaque requête. L'officiel garde le même trace ID sur le titre et le principal. Un gate qui corrèle les traces voit des sessions incohérentes.

**Recommandation** : stocker `trace_id` dans l'identité (ou dans la session), mint le span par requête, et propager le même trace ID dans `b3` et `traceparent`.

### F7 — La session tourne toutes les 30 minutes (moyenne)

`_SES_ROTATE_S = 1800`. Un client officiel garde sa session. Une rotation en cours de conversation casse le sticky routing et le prefix cache.

**Recommandation** : rotation par conversation, pas par temps ; ou TTL longue (24 h) avec rotation uniquement sur quota/erreur. Conserver la rotation comme kill-switch.

### F8 — `x-opencode-request` en 1.19.5 (moyenne)

Le proxy mint un `msg_` par egress, alors que l'officiel garde le même message ID sur les retries. C'est assumé pour l'anti-corrélation. Si le gate ne vérifie que le format, c'est sans risque fonctionnel ; c'est un tell.

**Recommandation** : dériver le `msg_` de `(IP, task_id)` de façon stable, ou le conserver par tâche et changer seulement à la rotation d'identité. Documenter le compromis.

### F9 — Client / UA selon la distribution (moyenne)

`cli` est correct pour le zip standalone ; `desktop` est correct pour l'app desktop. Le proxy force `cli` pour tous. Le fixture `free/captures/2.2.5.json` est un zip, donc `cli` est cohérent avec ce fixture. Si l'opérateur veut imiter l'app desktop installée, il faut un profil `desktop`.

**Recommandation** : ajouter `distribution` au profil (`npm`, `standalone-zip`, `desktop-app`) et choisir UA/client en conséquence. Ne pas mélanger.

### F10 — Titre et summary (moyenne)

Les requêtes de titre 2.2.5 n'ont ni outils ni `max_completion_tokens`, et un système de 1 806 caractères. Le proxy ne modélise pas ce template ; il passe le corps du client ou injecte la grille principale. Le résultat peut être un corps qui n'existe chez aucun client.

**Recommandation** : capturer et stocker les templates `title` et `compaction`, et les sélectionner par type de requête.

### F11 — Outil de synchronisation (moyenne)

`scripts/opencode_client_sync.py` :

- passe `--standalone` en dur → ne fonctionne pas avec le CLI npm 1.19.5 ;
- ne suit que le feed desktop, pas npm ;
- `build_spec` ne persiste ni `client`, ni `project`, ni l'ordre des en-têtes ;
- `pick_main_request` ne garde qu'un corps (le principal), pas le titre ;
- `free/identity_generator.py` documente encore 11 outils alors que le payload courant en a 13 ;
- `apply_spec` ne sait appliquer que quelques en-têtes, pas un template complet.

**Recommandation** : réécrire le sync autour d'un manifeste versionné, canal-aware, multi-requêtes, avec un `--check` qui échoue si le wire capturé diffère du manifeste.

### F12 — Tests de parité (haute, outillage)

La sous-suite de parité passe (99 cas, aucun échec), mais :

- `test_header_set_exact` teste le chemin **sans IP** (14 en-têtes, pas le wire de production 16) ;
- `OFFICIAL_ORDER` place `b3`/`traceparent` en 13/14 au lieu de 4/5 ;
- `test_identity_generator.py` attend un profil 1.19.5 avec UA 2.2.5, client `cli` et corps 13 outils — une combinaison impossible ;
- `test_ua_per_endpoint` attend le même UA pour chat et responses, alors que la ligne v1 utilisait deux bundles.

**Recommandation** : ajouter un test end-to-end qui capture le wire réel (curl_cffi → serveur local) sur le chemin per-IP, et un test de cohérence qui vérifie le tuple `(version, UA, client, TLS, body, header order)`.

---

## 6. Ce qui est déjà conforme

- TLS 1.18.31 et 1.19.5 : replay exact (JA3/MD5 identiques, SNI, groups, sigalgs).
- Jeu d'en-têtes 2.2.5 : les 16 noms sont corrects (seul l'ordre est faux).
- Jeu d'en-têtes 1.19.5 sans spec : les 13 noms et l'ordre sont exacts.
- Corps 2.2.5 officiel : passe tel quel, clés/ordre/outils/schémas identiques à la capture fraîche.
- Corps 1.19.5 officiel : passe tel quel.
- `Authorization: Bearer public`, `Content-Type`, `Accept`, `Accept-Encoding`, HTTP/1.1.
- Aucun header navigateur, aucun cookie, aucune clé payante.
- Format `ses_` / `msg_` : 30 caractères, 12 hex + 14 base62.
- Ordre des 13 outils 2.2.5 : exact.
- Unicité par IP des sessions/projects/IDs : assurée par le vault.
- `max_completion_tokens: 32000` : exact pour 2.2.5.
- `stream_options.include_usage: true` : exact.

---

## 7. Limites de l'audit

- **Pas de mesure live du gate** : aucune requête n'a été envoyée à `opencode.ai` avec ces variantes. Les verdicts 200/403/429 sont des hypothèses issues du code et des mesures antérieures, pas de ce run.
- **Pas de capture HTTP fraîche 1.19.3** : le TLS 1.19.3 est verrouillé par les constantes et les captures antérieures, mais le wire HTTP 1.19.3 n'a pas été re-capturé ici.
- **Pas de capture `/responses` 2.2.5** : le modèle free utilisé passe par `/chat/completions`. La jambe Responses reste non vérifiée.
- **Desktop app vs CLI** : le binaire desktop bundlé et le zip standalone sont deux distributions du même 2.2.5 ; l'UA/client diffèrent. L'audit couvre les deux, mais le choix de cible reste une décision d'opérateur.
- **Le gate peut ignorer l'ordre des en-têtes et le TLS** : dans ce cas, D2/D1 sont des tells de fidélité, pas des causes de refus. La mesure A/B est indispensable avant de prioriser.

---

## 8. Verdict

Le proxy **copie correctement le corps 2.2.5** et **rejoue correctement le TLS** des deux lignes. Il échoue sur la **cohérence** : il combine un corps/UA 2.2.5 avec des identités et des TLS 1.18/1.19, et il casse l'ordre du wire 2.2.5 sur le chemin per-IP réel. Il ajoute deux fuites (chemin réel, project réel) et deux incohérences de session (trace par requête, session 30 min).

La correction la plus rentable n'est pas un nouveau profil exotique : c'est de **choisir une cible canonique** (2.2.5 `cli` recommandé, car le payload et le fixture actuels sont déjà 2.2.5 `cli`) et de faire porter **toutes** les décisions par un seul profil. Le plan `specs/003-official-client-parity/` détaille cette trajectoire.

Pour rétablir le service **maintenant**, le runbook `docs/PLAN_RETABLISSEMENT_FREE_2026-11-05.md` donne l'ordre opérationnel : triage du 429, replay exact 2.2.5 sur une IP, canary, puis seulement les corrections de fond. Le plan de parité n'est pas un prérequis au premier 200 ; il garantit que le 200 tient dans le temps.
