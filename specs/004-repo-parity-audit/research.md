# Research — Dépôts tiers, faits de code et divergences

**Feature**: `004-repo-parity-audit`  
**Date**: 2026-11-06  
**Méthode**: lecture des README **et** des fichiers de code critiques, puis
confrontation aux mesures live du proxy. Chaque fait porte une qualité de
preuve :

- **[M]** mesuré par le dépôt (probe live documenté dans son code/README) ;
- **[R]** reproduit par nous sur l'IP directe ou WARP le 2026-11-06 ;
- **[D]** documenté seulement (README, commentaire, pas de probe) ;
- **[I]** inféré du code (lecture, pas d'exécution).

---

## 1. `xljiulang/opencode-proxy`

**URL**: https://github.com/xljiulang/opencode-proxy  
**Langage**: Rust (wreq 0.15.3, BoringSSL)  
**Architecture**: passerelle locale Responses ↔ Chat, identité OpenCode 2.2.5,
  TLS et ordre d'en-têtes contrôlés, service Windows/Linux.  
**Fichiers lus**: `src/compat_proxy/gate.rs`, `src/compat_proxy/identity.rs`,
  `src/opencode_profile.rs`, `resources/opencode_tools.json`,
  `docs/PROTOCOL_NOTES.md`, `Cargo.toml`.

### Faits de code

- **Gate à 7 outils** [M] : `ZEN_GATE_TOOL_NAMES = glob, read, skill, webfetch,
  websearch, write, execute` ; `ZEN_MIN_GATE_TOOLS = 7` ; un outil ne compte
  que si son nom est dans la liste **et** si `parameters.properties` est un
  objet non vide. Probe 2026-11-05 : 0–6 → `429 FreeUsageLimitError`, 7 → 200.
- **`<env>` minimal** [M] : `Working directory:` et les tags sont requis ; la
  valeur du chemin est libre ; les autres champs (`Workspace root folder`,
  `Platform`, `Prefer…`) sont optionnels. Probe 2026-11-06 : suppression de
  `Working directory` → 429 ; suppression des autres → 200.
- **Injection `<env>`** [I] : `inject_chat_identity_context` préfixe le
  **premier** message système si son contenu ne contient pas déjà `<env>`, sinon
  il insère un message système en tête. Il ne vérifie pas la validité du
  premier couple — d'où le probe séparé de rianilham.
- **UA** [M] : `opencode/latest/2.2.5/cli`. Le gate ne lit que la forme
  `opencode/…/cli` ; `opencode/9.9.9/cli` passe, `curl/8` échoue.
- **Profil TLS** [M] : 17 ciphers, 11 sigalgs, courbes
  `X25519MLKEM768, X25519, P-256, P-384`, ALPN `http/1.1`, GREASE off,
  extensions dans l'ordre `0,2,3,4,5,6,7,8,9,11,14,15,17`. JA3
  `1523504b38f0fae0d881d4b6554aac1b`, JA4
  `t13d1713h1_5b57614c22b0_7f527383e249`. **[R]** notre TLS 1.19.5 a le même
  JA3.
- **Ordre d'en-têtes** [M] : 14 noms, puis tri byte-wise par le writer HTTP/1.1
  (`Authorization` remonte avant `b3`). Le jeu inclut `x-opencode-org-id`
  (workspace `wrk_*`), que le free anonyme n'envoie pas.
- **Alias d'outils** [M] : les outils client sont aliasés
  `fn_<camelCase>_ex` (`exec_command` → `fn_execCommand_ex`) ; `ex_exec_command`
  échoue encore. Le mapping est restauré dans les réponses.
- **`max_completion_tokens`** [I] : ajouté à 32 000 seulement si le client n'a
  pas de `max_output_tokens` ; un budget client explicite est respecté.
- **Frame `cost`** [D] : OpenCode ajoute une frame après `[DONE]` ; elle ne doit
  jamais atteindre le client.

### Adopté

- `properties` non vide sur les placeholders.
- Garde `<env>` et sémantique du premier couple (via rianilham, qui est plus
  précis).
- Référence TLS/UA pour une future capture 2.2.5.

### Rejeté / non adopté

- Alias `fn_<camelCase>_ex` : non nécessaire sur `deepseek-v4.1-flash-free`
  **[R]** (`read_file`, `exec_command` → 200).
- `x-opencode-org-id` : inutile pour le free anonyme.
- 7 outils obligatoires : non reproduit **[R]** le 2026-11-06 (`read` seul +
  `<env>` → 200). Voir « Contradictions ».

---

## 2. `rianilham28/opencode2api`

**URL**: https://github.com/rianilham28/opencode2api  
**Langage**: Rust (x2api template, reqwest + vendor leg BoringSSL)  
**Architecture**: un processus, un upstream OpenCode Zen free-only, quatre
  dialectes clients (`chat`, `messages`, `responses`, `gemini`), pool d'egress
  à sessions collantes.  
**Fichiers lus**: `crates/service/src/provider.rs` (2 075 lignes),
  `crates/opencode2api-transport/src/lib.rs` (4 568 lignes),
  `README.md`, `docs/compliance/openai.md` (référencé).

### Faits de code

- **Gate minimal** [M] : `stream: true`, un outil nommé exactement `read`,
  un bloc `<env>` avec `Working directory:` dans un message système. Les outils
  étrangers passent ; `Read`/`READ` échoue ; zéro outil échoue.
- **`<env>` : premier couple seulement** [M] : `has_env_block` cherche le
  premier `<env>` et le premier `</env>` ; un premier couple vide devant un
  valide empoisonne la requête. `ensure_env_block` préfixe le bloc au premier
  message système s'il contient déjà `<env`, sinon l'ajoute à la fin ; s'il n'y
  a pas de message système, il en insère un.
- **`read`** [M] : wrapper complet, `description` **string** (un renderer strict
  renvoie 400 si `description` est `null`), `parameters` objet. Le schéma vide
  suffit au gate mais peut être refusé par certains modèles.
- **Session** [M] : `ses_` + 12 hex (horloge **inversée**) + 14 base62 ;
  `mint_opencode_session` est appelé **une fois par provider** ; la session est
  donc stable pour le processus. `x-opencode-session-id` est obligatoire.
- **Trace** [M] : `trace_id` 32 hex minté une fois par provider, **span par
  requête** ; `b3` = `{trace}-{span}-1-{parent}`, `traceparent` =
  `00-{trace}-{span}-01`.
- **Project** [I] : 40 hex minté une fois par provider, pas dérivé du workspace.
- **`tool_choice: none`** [I] : supprimé du corps (`chat_body`), jamais relayé.
- **`max_tokens` → `max_completion_tokens`** [I] : renommé ; `stream_options`
  `include_usage` forcé.
- **`retry_hint`** [I] : lit `retry-after-ms` d'abord, puis `retry-after`
  (secondes ou HTTP-date). `MAX_COOLDOWN_SECS = 120` pour les clés.
- **Endpoint par modèle** [M] : `muse-spark-1.2/1.3-contributor-free` ne
  répondent que sur `/responses` ; le `api` du catalogue gagne quand il existe.
- **Trim `[DONE]`** [I] : machine à états qui coupe tout après
  `data: [DONE]` + terminateur ; la frame `cost` post-`[DONE]` ne passe jamais.
- **Egress** [I] : lanes chaudes par session proxy, `SpentIps` (IP déjà
  épuisées, cap 4096), `ip_probe_url` **optionnel et désactivé par défaut**
  (coûte une requête par session), `prewarm` HEAD, rotation à 80 % du TTL.

### Adopté

- Sémantique du **premier couple** `<env>`.
- Retrait de `tool_choice: none`.
- Plafond de cooldown court (nous avons retenu 20 min, pas 120 s : le quota est
  par IP, pas par clé).

### Rejeté / non adopté

- Session par provider : nous gardons par `(IP, conversation)`.
- Trace stable par processus : nous gardons stable par session.
- `read` à schéma vide : nous gardons `path` non vide.
- `SpentIps` + probe : notre ledger `(IP, jour, bucket)` couvre déjà ; la probe
  coûte une requête.

---

## 3. `xd88dx/afree-proxy`

**URL**: https://github.com/xd88dx/afree-proxy  
**Langage**: Go  
**Architecture**: passerelle multi-upstreams (Cline, OpenCode Zen, WorkBuddy,
  TraeWork, OpenRouter, AMDCloud, TokenHarbor, Other), pool de comptes/keys,
  panneau d'admin, isolation identité ↔ egress.  
**Fichiers lus**: `internal/app/zen_session.go`, `internal/app/zen_endpoint.go`,
  `README.md`.

### Faits de code

- **Quota par IP** [D] : ~200 requêtes / 5 h pour le free Zen. C'est une
  propriété de l'IP de sortie, pas du compte.
- **429** [M] : `Retry-After` est la distance au prochain 00:00 UTC, pas la
  vraie recovery. Mesure : `RA=20 h 46`, puis **200 sur la même IP 18 minutes
  plus tard**. Le cooldown est plafonné à 20 min.
- **Session** [M] : le CLI 2.1.4 crée une session par `run`. Une session
  collante réutilisée sur des requêtes sans historique est **shadow-ban jusqu'à
  minuit UTC** après le premier appel. Le code actuel (`zenCallIdentity`) mint
  donc une session par appel API, la réutilise pour les retries du même appel,
  et la renouvelle sur 403.
- **403 FreeTier** [I] : renouveler la session une fois dans la même requête,
  puis cooldown 15 min de la clé ; une 403 « propre » qui suit l'egress, pas la
  clé, est un problème d'IP.
- **Endpoint par modèle** [M] : `muse-spark` → `/responses` ; endpoint appris
  sur un 500/400 caractéristique, persisté dans `.zen-endpoints.json`, jamais
  appris sur une erreur réseau.
- **Egress** [D] : une identité par IP de sortie ; le pool ne bascule jamais
  une identité vers une autre IP ; `DISABLE_DIRECT` interdit le direct.

### Adopté

- Plafond de cooldown 20 min (header et `trial_dead`).
- Principe « une identité par IP » (déjà dans notre vault).

### Rejeté / non adopté

- Session par appel : correct pour 2.1.4, pas pour le wire 2.2.5.
- Apprentissage d'endpoint : notre `free_endpoint_for` couvre déjà
  `muse`/`spark`/`jev`.

### À calibrer

- `quota_per_ip: 500` dans `config.yaml` vs ~200 req/5 h. Le ledger journalier
  reste la couche autoritaire ; la valeur de profil est une estimation.

---

## 4. `alztrk/opencode-ip-rotator`

**URL**: https://github.com/alztrk/opencode-ip-rotator  
**Langage**: Python (FastAPI, curl_cffi) + rotateur WARP  
**Architecture**: proxy OpenAI/Anthropic, découverte de modèles, pool de
  proxies, rotation Cloudflare WARP, dashboard SQLite.  
**Fichiers lus**: `services/upstream.py`, `rotator.py`, `Dockerfile.warp`,
  `README.md`.

### Faits de code

- **Gate minimal** [I] : `apply_free_request_shape` force `stream: true`,
  préfixe `<env>` (mais exige que le contenu **commence** par `<env>`), ajoute
  un outil `read` avec `path`, ne touche pas aux autres outils.
- **En-têtes** [I] : `Bearer public`, UA
  `opencode/latest/2.1.4/cli`, `x-opencode-client`, quartet de session. **Pas
  de `b3`/`traceparent`** — ils ne sont donc pas nécessaires au gate.
- **TLS** [D] : force TLS 1.2 et une liste de ciphers OpenSSL. Alternative si
  le fingerprint Bun dérive ; pas nécessaire chez nous.
- **Rotation WARP** [I] : `warp-cli disconnect`, `registration delete`,
  `registration new`, `connect`, vérifie que l'IP a changé, réessaie sinon.
- **WARP comme egress** **[R] 2026-11-06** : conteneur `caomingjun/warp`,
  SOCKS5 `127.0.0.1:11080`, egress `104.28.230.202`, probe 2.2.5 exacte
  (`--client cli --project global`) → **200** en 5,6 s. Redémarrage,
  `registration new` ×3, second conteneur et cinq overrides d'endpoint →
  **même IP** ou tunnel non connecté. WARP = une IP par hôte/réseau, pas six.

### Adopté

- Schéma `read` non vide.
- WARP testé comme station 2 potentielle.

### Rejeté / non adopté

- Session par appel.
- TLS 1.2 OpenSSL.
- Rotation WARP : non effective sur ce réseau.

---

## 5. `beastbroak30/opencode-zen-proxy`

**URL**: https://github.com/beastbroak30/opencode-zen-proxy  
**Langage**: Python (FastAPI, httpx)  
**Architecture**: proxy OpenAI/Anthropic, découverte et health-check des
  modèles free, backend optionnel `opencode serve`.  
**Fichiers lus**: `server.py`, `README.md`.

### Faits de code

- **Identité** [I] : `OC_VERSION = "1.18.35"` ; `oc_id("ses")` utilise
  `int(time.time()*1000)` en hex **sans inversion** ; session par utilisateur
  réutilisée 30 min.
- **Gate** [I] : force `stream: true` ; `ZEN_TOOLS` par défaut ; route
  `muse`/`spark` vers `/responses`.
- **Backend `opencode serve`** [I] : si `GET /global/health` répond, le proxy
  crée une session, envoie `prompt_async`, et tail `/event` pour produire les
  deltas. Le vrai client fournit l'identité et la clé.
- **Découverte** [D] : health-check de chaque modèle au démarrage et toutes les
  30 min ; les modèles 400/401/403/404/410 sont désactivés.
- **Statut live** [M] : `0/37` modèles free depuis son IP (2026-11-05), tous
  `FreeUsageLimitError` ou `unavailable`. Confirme que le 429 est un quota
  d'IP.

### Adopté

- Rien dans le wire.
- L'architecture `opencode serve` reste un repli si le gate change encore.

### Rejeté

- Session non inversée : le gate l'accepte peut-être, mais l'inversion est le
  comportement officiel.
- Session 30 min par utilisateur : nous avons déjà mieux (par IP + conversation).

---

## 6. `nahdd123uosackr/opencode_proxy`

**URL**: https://github.com/nahdd123uosackr/opencode_proxy  
**Langage**: JavaScript/Node + Deno + Go (uTLS), wrapper Streamlit  
**Architecture**: relais Node/Deno, pont uTLS Go vers `opencode.ai`.  
**Fichiers lus**: `utls_bridge/tls_bun.go`, `utls_bridge/main.go`,
  `README.md`, arborescence.

### Faits de code

- **ClientHello Bun exact** [M] : 17 ciphers, courbes
  `X25519MLKEM768` (4588, 1216 octets) puis `X25519`, 20 sigalgs dans l'ordre
  2.1.4, pas d'extension Padding, ALPN `http/1.1`, JA3
  `1523504b38f0fae0d881d4b6554aac1b`.
- **Pas de keep-alive** [I] : `DisableKeepAlives: true`, `outReq.Close = true`,
  `CloseIdleConnections()` sur 429. Commentaire : « prevents 429 pooling
  poisoning ».
- **Pont** [I] : serveur HTTP local qui forward tout vers `opencode.ai` avec le
  ClientHello Bun ; le proxy Node/Deno l'utilise comme transport.
- **Docs** [D] : `docs/14_opencode_cli2x_모사_uTLS_브릿지...` décrit le pont et
  le routage par nœud fixe ; non lu en entier.

### Adopté

- Rien dans le code.
- Référence de secours pour le ClientHello Bun si `curl_cffi` dérive.

### Non adopté

- Pont uTLS séparé : notre `curl_cffi` 1.19.5 passe et évite un processus Go.
- `DisableKeepAlives` : candidate à évaluer (voir divergences).

---

## 7. `xueweijian/opencode-edge-proxy`

**URL**: https://github.com/xueweijian/opencode-edge-proxy  
**Langage**: JavaScript (Wasmer Edge / WinterJS)  
**Architecture**: reverse proxy edge déployé en `us-losa1`, qui contourne le
  geo-gate de `muse-spark`/`beam-free` avec une sortie US.  
**Fichiers lus**: `README.md`, `src/index.js`.

### Faits de code

- **Geo-gate** [M] : certains modèles renvoient
  `RegionError: This model is not available in your country` depuis la Chine ;
  une sortie US (Wasmer Edge) passe. Aucun en-tête ne corrige ce refus.
- **UA governance** [I] : les UA génériques (`okhttp`, `python-requests`,
  `undici`, `axios`, …) sont remplacés par `cherry-opencode-proxy/1.0` pour
  éviter le blocage Cloudflare 1010. Ce n'est pas l'UA du gate free ; c'est une
  protection edge.
- **Session** [I] : priorité client `x-opencode-session` → alias
  `x-conversation-id`/`x-session-id`/`x-correlation-id` → empreinte
  `sha256("ocp1|model|system|first-user")[0:16]` → UUID. Cette valeur n'est pas
  un `ses_` canonique : elle vise `/zen/go/v1`, pas le gate free `/inference`.
- **SSE finish patch** [I] : pour les endpoints chat-completions uniquement,
  suit `finish_reason` et `[DONE]` déjà émis et **ne complète que le manquant**.
  Jamais sur `/responses` (terminaison propre, `[DONE]` inexistant).
- **Nettoyage IP client** [I] : supprime `x-forwarded-for`,
  `cf-connecting-ip`, `x-vercel-ip-*`, etc.
- **Wasmer Free** [D] : 150 GB ingress + 150 GB egress/mois, 1 M requêtes,
  100 apps. C'est une source d'IP US gratuite, mais l'egress reste partagé.

### Adopté

- Rien dans le wire : notre TSV/SSE gère déjà la clôture (et mieux : une
  troncature réelle produit une erreur, pas un faux `stop`).
- Le geo-gate est documenté comme une limite séparée.

### Non adopté

- Session par empreinte/UUID : incompatible avec le gate `ses_` canonique.
- Wasmer Edge : piste d'IP US pour les modèles région-locked, pas une station
  locale.

---

## 8. `developingchet/zengate`

**URL**: https://github.com/developingchet/zengate  
**Langage**: Node.js (Express), 10 étoiles, MIT  
**Architecture**: gateway OpenAI/Responses qui **lance le vrai CLI
  `opencode serve`** comme backend privé, sans clé upstream.  
**Fichiers lus**: `README.md` (très détaillé).

### Faits

- **Pas d'imitation** [D] : le gateway exécute le vrai client et traduit
  OpenAI ↔ sessions OpenCode. Chaque requête API crée une session courte,
  supprimée ensuite.
- **Classification quota** [D] : `429 upstream_quota_exhausted` vs
  `upstream_rate_limited` ; nécessite OpenCode ≥ 2.1.2 pour la classification
  structurée. Le gateway **n'infère jamais** le quota depuis le texte humain.
- **`Retry-After`** [D] : les délais multi-heures sont préservés ; si absents
  ou invalides, les deux champs sont omis (reset inconnu).
- **Profil strict `chat`** [D] : retire le prompt système et les outils du
  backend ; le provider peut alors refuser (le gate free a besoin du contexte
  de codage). Confirme que le prompt officiel est la signature, pas du bruit.
- **Fonction calling émulé** [D] : les schémas passent dans le prompt, les
  appels sont reconstruits. Pas applicable à notre wire natif.
- **Sécurité** [D] : clé gateway, rate limit, queue, body limits, SSRF
  protection sur les attachments.

### Adopté

- Aucun code.
- Confirme l'architecture `opencode serve` comme repli (déjà connue via
  `beastbroak30`).

### Non adopté

- Session par requête (le vrai CLI), prompt-based tools, gateway Node.
- La préservation des `Retry-After` multi-heures : nous plafonnons à 20 min
  pour la disponibilité, choix documenté.

---

## 9. `ebouther/opencode-unlimited-proxy`

**URL**: https://github.com/ebouther/opencode-unlimited-proxy  
**Langage**: Python (stack) + engine `opencode2api` (GPL-3.0), 51 étoiles,
  7 forks, MIT pour le stack.  
**Architecture**: gateway OpenAI-compatible + pool WARP userspace, clé par
  **bloc d'egress**. Linux uniquement (host networking + UDP 2408).  
**Fichiers lus**: `README.md`, `scripts/gate-probe.py`,
  `docs/free-tier-gates.md`.

### Faits majeurs

- **Quatre gates** [M] (mesures 2026-10-16, une variable à la fois) :
  1. **UA** `opencode/<version>` avec version **≥ 1.18.0** ; `1.17.2` →
     `426 Upgrade Required` ; UA tiers/bare → `403`.
  2. **Session canonique** : `x-opencode-session` (pas `-id` seul) doit matcher
     `^ses_[0-9a-f]{12}[0-9A-Za-z]{14}$` **et** décoder
     `~(unix_ms << 12 | counter)` sur 48 bits, avec un timestamp récent
     (±30 jours). Un `ses_` aléatoire/sha256/UUID est refusé. Le premier mint
     d'une ms a les 3 derniers hex à `ffe`.
  3. **`read` exact** : un seul outil nommé exactement `read` suffit ; le
     nombre et les autres noms sont sans effet ; `read_file`/`Read`/`READ`
     échouent ; zéro outil échoue ; `{"name":"read"}` sans `parameters` → 400.
  4. **`stream: true`** : `stream:false` → 403 sur les deux endpoints.
- **Geo-gate** [M rapporté] : `403 FreeTierError` + message
  `not available in your country` ; aucun en-tête ne corrige, il faut une
  sortie US.
- **Quota par bloc** [M] (2026-10-27) : le compteur est keyé par **bloc
  d'egress** — `/64` IPv6, `/32` IPv4 — avec reset **00:00 UTC**. WARP IPv4
  est une NAT partagée, donc la rotation IPv4 n'ajoute rien. Une sortie WARP
  IPv6 avec un `/64` routé et une source liée par exit obtient son propre
  bloc. Le compteur n'est **pas** par adresse individuelle.
- **Dedupe** [I] : `ofu-dedupe` mesure l'egress réel et draine les nœuds qui
  partagent une adresse, pour que six nœuds ne soient pas trois.
- **Probe** [M] : `scripts/gate-probe.py` isole les quatre gates avec des
  contrôles négatifs et un baseline positif ; 8/8 attendu, exit 3 si le refus
  est geo/quota/auth/version.
- **Limite Windows** [D] : le mode par défaut exige `network_mode: host`,
  UDP 2408 et un `/64` routé ; Docker Desktop ne fournit pas l'host namespace.

### Adopté

- **Test de session canonique** (`tests/test_session_canonical.py`) : layout
  `ffe`, horloge implicite, forme 30 caractères.
- **Confirmation du quota par bloc** : notre `server_ip_key` tronque déjà
  l'IPv6 au `/64` ; le modèle est aligné.
- **Résolution de la contradiction 7 outils vs `read` seul** : `read` seul
  est la règle ; notre grille à 13 outils reste un sur-ensemble sûr.

### Non adopté / bloqué

- **WARP IPv6 `/64`** : l'hôte Windows n'a **aucune IPv6 globale** (seulement
  `fe80::`), donc cette voie n'est pas disponible ici. Il faudrait un hôte
  Linux avec un `/64` routé.
- **Engine `opencode2api`** : Linux-only, GPL-3.0, host networking.
- **WARP IPv4** : NAT partagée, n'ajoute pas de headroom ; notre WARP
  `104.28.230.202` reste utile comme deuxième IP mais pas comme multiplicateur.

---

## 10. Balayage GitHub du 2026-11-06 (tri par mise à jour)

Requêtes API : `opencode proxy`, `opencode free`, `opencode quota`,
`opencode zen`, `opencode unlimited`, `opencode warp`, `opencode ip rotation`,
`opencode gateway`, `opencode2api`, `opencode free tier`, `opencode warp`,
`opencode ipv6` — **365 dépôts uniques**, triés par `pushed_at` décroissant.
La plupart sont des UIs/tableaux de bord d'agents ; le sous-ensemble utile :

| Dépôt | MAJ | Langage | Apport | Verdict |
|---|---|---|---|---|
| `Deury08/warp-minimal` | 2026-11-06 | Go | WARP **MASQUE** (UDP 443), exits IPv6 `/64`, pool, userspace, binaire **Windows** | **Top find** |
| `wbi35772/opencode2api-win` | 2026-11-04 | Go | Gateway Windows + WARP pool, **session-affinity 429** | Session testée : inefficace |
| `vispar-tech/openrot` | 2026-11-01 | Python | Bridge WARP, 429 self-rotation, **swap du prompt de titre** | À inspecter (titre) |
| `acruz6421-bot/opencode-infinite-proxy` | 2026-10-29 | JS | WARP rotation + reset session/project sur 429 | Session inefficace ; projet à tester |
| `leangtg123-art/opencode-termux-unlimited` | 2026-11-04 | Shell | Pool WARP ARM64 Termux | Hors Windows |
| `Munkh-Orgi1/opencode-free` | 2026-10-30 | Batch | Launcher Windows, rotation WARP sur limite | Simple, windows |
| `gkhantyln/opencode-warp` | 2026-11-02 | JS | WARP egress, refresh IP, 429 auto-protection | Redondant |
| `Ashineqq/opencode2api` | 2026-10-18 | Go | **IPv6-first** egress, 403 causé par DNS IPv4 détourné | Piste 403 |
| `scyslz/opencode-forward` | 2026-10-13 | Go | Reverse proxy Zen, en-têtes CLI, isolation IPv4/IPv6 | Référence |
| `synthronyx/opencode-ip-rotator` | 2026-10-24 | Python | Fork d'`alztrk` (WARP + dashboard) | Redondant |
| `KilimcininKorOglu/warp-proxy` | 2026-10-19 | Go | WARP + bridge OpenAI/Anthropic | Redondant |
| `rustemmustafingovua-byte/opencode-free-stack` | 2026-11-06 | JS | Routage free + fallback + quotas quotidiennes | Modèle-level |
| `kexuejin/dsh-opencode-suite` | 2026-11-06 | JS | Pool multi-key Go + quota failover | Zen Go, pas free |
| `nranka79/opencode-llm-router` | 2026-11-06 | TS | Routeur free → abonnement → API | Modèle-level |

### Tests décisifs

1. **IPv6 WARP = nouveau bloc 200.** Le conteneur WARP existant sort en
   `2a09:bac5:32d4:be::13:23e` (`/64`) ; `cloudflare.com/cdn-cgi/trace` le
   confirme en IPv4 **et** IPv6. Le probe avec notre wire exact
   (`IPRESOLVE=6`, TLS Bun, session `…ffe`, `<env>`, `read`) rend **200** ;
   en `IPRESOLVE=4`, **200** aussi. C'est un troisième bloc utilisable sans
   nouvelle registration.
2. **La rotation de session ne débloque pas une IP 429.** Trois sessions
   fraîches sur `186.247.77.36` (SOCKS5 `1081`) → **3 × 429**, RA ~61 905 s.
   Le quota est bien par **bloc**, pas par `(session, bloc)`. L'idée
   `wbi35772` ne s'applique pas à `/inference` (ou pas à ce modèle).
3. **`warp-minimal` Windows démarre mais ne s'enregistre pas.**
   `-version` OK, `/health` OK, mais `POST api.cloudflareclient.com/v0a2158/reg`
   expire (throttle Cloudflare après plusieurs registrations aujourd'hui).
   Le binaire est bon ; à retenter plus tard ou depuis une autre IP.

### Conséquence

Le levier immédiat n'était pas un nouveau compte ni une nouvelle session :
c'était **le bloc IPv6 déjà présent** dans le tunnel WARP. Il est maintenant
exposé comme station distincte (`warp-ipv6`, `IPRESOLVE=6`, egress réel
`2a09:…/64` dans le ledger) : **6 × 200** en rotation IPv6 → direct → IPv4.
Reste à retenter `warp-minimal` pour plusieurs `/64` (T-422).

## Convergences et contradictions

### Convergences solides

| Fait | Dépôts | Notre état |
|---|---|---|
| `stream: true` obligatoire | xljiulang, rianilham, alztrk, beastbroak, ebouther | forcé |
| `read` exact obligatoire, **compte indifférent** | rianilham, ebouther | présent (13 outils = sur-ensemble) |
| `<env>` + `Working directory:` obligatoire | xljiulang, rianilham, alztrk | présent + garde |
| Un premier couple `<env>` invalide empoisonne | rianilham | corrigé |
| `tool_choice: none` refusé | rianilham | corrigé |
| Session `ses_` **canonique** (`…ffe`, horloge récente) | ebouther | mint conforme + test |
| `x-opencode-session` exact (pas `-id` seul) | ebouther | présent (quartet 2.2.5) |
| `Retry-After` ≠ recovery réelle | afree, rianilham | plafonné 20 min |
| 429 = quota d'**egress block** (`/64` v6, `/32` v4), reset 00:00 UTC | ebouther, afree | ledger `server_ip_key` /64 |
| 429 ≠ wire | afree, beastbroak, nos clean-rooms | confirmé |
| BoringSSL/Bun nécessaire | xljiulang, rianilham, nahdd | `curl_cffi` 1.19.5 |
| DeepSeek `reasoning_content` sur chaque tour assistant | rianilham | appliqué |

### Contradictions ouvertes

1. **Gate à 7 outils vs `read` seul — RÉSOLU.** `ebouther` a mesuré une
   variable à la fois (2026-10-16) : `read` seul suffit, le nombre et les
   autres noms sont sans effet, `read_file`/`Read` échouent. xljiulang mesurait
   7 outils (2026-11-05) mais probablement sur une autre forme
   d'endpoint/modèle, ou avec des schémas vides. **Nous reproduisons `read`
   seul + `<env>` → 200** (2026-11-06). Notre grille à 13 outils reste un
   sur-ensemble sûr ; `T-413` est clos.
2. **Session stable vs session par appel.** rianilham/xljiulang : stable par
   processus/thread. afree : par appel (CLI 2.1.4), sinon shadow-ban. Nous :
   stable par `(IP, conversation)`, rotation 30 min. Non tranché ; test
   destructif à ne pas faire sur l'IP directe.
3. **Trace stable vs par requête.** rianilham : trace par processus, span par
   requête. xljiulang : `trace_headers()` par requête. Nous : stable par
   session. Écart cosmétique, non mesuré comme facteur de gate.
4. **UA 2.2.5 vs 1.19.5.** La capture 2.2.5 dit
   `opencode/latest/2.2.5/cli` ; notre vault dit
   `opencode/1.19.5 ai-sdk/provider-utils/4.0.23 runtime/bun/1.4.4`. Le corps
   et l'ordre sont 2.2.5 ; l'UA ne l'est pas. Voir divergence D-01.
5. **`max_completion_tokens` vs `max_tokens`.** 2.2.5 utilise
   `max_completion_tokens` ; 1.19.5 utilise `max_tokens`. Notre copie 2.2.5
   convertit ; un corps « signature OK » peut encore passer `max_tokens`.

### Qualité de preuve

- Les probes xljiulang/rianilham/afree sont datés et reproductibles par leur
  code, mais non rejoués par nous (sauf le gate `read` seul et le TLS JA3).
- Les mesures WARP et proxy sont **les nôtres**, datées du 2026-11-06.
- Les affirmations « 0/37 » et « RA=20 h 46 » sont des mesures du dépôt, pas
  des nôtres ; elles sont classées [M] et non [R].
