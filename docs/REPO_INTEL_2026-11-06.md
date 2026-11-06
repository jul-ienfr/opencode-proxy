# Dépôts tiers — ce qui est exploitable pour la jambe free

Analyse des dépôts fournis le 2026-11-06. Chaque fait est classé
**adopté**, **à tester** ou **rejeté**, avec la raison. Aucun code n'est copié :
seules les mesures et les règles de forme sont reprises.

> **Documentation complète** : le paquet Spec Kit
> [`specs/004-repo-parity-audit/`](../specs/004-repo-parity-audit/spec.md)
> contient les fiches détaillées, le journal des changements, les mesures et
> les contrats. L'audit consolidé est
> [`docs/AUDIT_REPOS_2026-11-06.md`](AUDIT_REPOS_2026-11-06.md).

## Deuxième lot (2026-11-06)

| Dépôt | Apport | Verdict |
|---|---|---|
| `ebouther/opencode-unlimited-proxy` | 4 gates mesurés, session canonique `…ffe`, quota par bloc `/64`/`/32`, WARP IPv6 | **Référence gate/quota** |
| `xueweijian/opencode-edge-proxy` | Geo-gate US, patch SSE `finish`/`[DONE]` | **Référence geo/SSE** |
| `developingchet/zengate` | Gateway sur vrai `opencode serve`, quotas structurés | **Repli architectural** |

Faits nouveaux qui changent le diagnostic :

- Le gate est **`read` seul** ; le nombre d'outils et les autres noms sont
  sans effet. `T-413` est clos.
- La session doit être **canonique** (`…ffe`, horloge récente), pas un
  `ses_` aléatoire. Notre mint est conforme (`tests/test_session_canonical.py`).
- Le quota est par **bloc d'egress** (`/64` IPv6, `/32` IPv4), reset 00:00
  UTC. `server_ip_key` tronque déjà l'IPv6 au `/64`.
- WARP IPv4 est une **NAT partagée** ; la rotation IPv4 n'ajoute pas de
  headroom. Le tunnel WARP fournit en revanche une **IPv6 `/64`** distincte
  (`2a09:bac5:32d4:be::/64`), exposée comme station `warp-ipv6` : 6 × 200 en
  rotation. L'hôte Windows n'a pas d'IPv6 globale, mais ce n'est pas
  nécessaire : l'IPv6 est dans le tunnel.

## Synthèse

| Dépôt | Nature | Verdict | Ce qu'on prend |
|---|---|---|---|
| `rianilham28/opencode2api` | Passerelle Rust, free-only, fingerprint 2.x | **Le plus utile** | `<env>` obligatoire, `read` + `stream:true`, RA = minuit UTC, `reasoning_content` |
| `xd88dx/afree-proxy` | Passerelle Go multi-upstreams | **Utile** | Quota ~200 req/5 h par IP, plafond de cooldown 20 min |
| `alztrk/opencode-ip-rotator` | FastAPI + rotateur Cloudflare WARP | **Utile** | Schéma `read` non vide, WARP comme source d'IP |
| `beastbroak30/opencode-zen-proxy` | Petit gateway Python via `opencode serve` | **Repli architectural** | Le vrai client comme backend d'identité |
| `nahdd123uosackr/opencode_proxy` | Wrapper Streamlit d'un relais Node | **Rien** | — |
| `xljiulang/opencode-proxy` | Passerelle Rust 2.2.5 | **Utile** | Gate à 7 outils, `properties` non vides |

## 1. `rianilham28/opencode2api`

Source : README, section « Free-tier gate » et « Vendor specials » (mesures
2026-10-25, re-capturées contre 2.2.2 le 2026-11-05).

### Faits

- Le gate free exige **`stream: true`** ; un corps bufferisé est refusé puis
  replié côté proxy.
- Le tableau `tools` doit contenir une entrée nommée **exactement `read`**.
  Paramètres, description et wrapper sont libres (`{"name":"read"}` suffit) ;
  `READ`/`Read` échoue ; zéro outil échoue ; **les outils étrangers passent**.
- Un couple **`<env>…</env>`** contenant **`Working directory:`** doit être
  présent dans **un** message système. Le contenu autour est libre, le chemin
  peut être vide ou arbitraire. `<environment>`, `<env x="1">` et
  `Working Directory:` échouent. Un premier couple vide **empoisonne** la
  requête : il faut alors insérer un bloc valide **avant**.
- `tool_choice:"none"` n'est jamais relayé.
- TLS : BoringSSL au commit Bun ; OpenSSL/rustls → 429.
- DeepSeek « thinking mode » : **chaque** tour assistant doit porter
  `reasoning_content` dès que `tools` est présent, sinon 400.
- `Retry-After` est la distance au prochain **00:00 UTC**, pas la vraie
  recovery.

### Adopté

- Garde `<env>` dans `_free_wire_body`, y compris sur le chemin « signature
  officielle » qui retournait le corps tel quel. Un bloc valide est inséré
  avant un éventuel bloc inutilisable ; le prompt officiel n'est jamais
  modifié.
- `read` conservé dans la grille, avec un schéma non vide.

### Rejeté

- Session par processus : notre proxy garde un `ses_` par **(IP, conversation)**
  avec rotation 30 min, ce qui correspond au client 2.2.5 capturé. Le
  « par processus » de ce dépôt revient au même pour un client qui vit une
  requête, mais pas pour un proxy long-lived.

## 2. `xd88dx/afree-proxy`

Source : README, sections « OpenCode zen », « 429 », « Proxy isolation ».

### Faits

- Quota free **par IP de sortie**, environ **200 requêtes / 5 h**.
- `429` renvoyé tel quel au client (pas de 502), pour qu'il applique son
  propre backoff.
- `Retry-After` = distance à 00:00 UTC. Mesure : `RA=20 h 46`, puis **200 sur
  la même IP 18 minutes plus tard**. Le plafond de cooldown est donc fixé à
  **20 minutes**, pas à la valeur brute.
- Une identité (clé) ne doit jamais apparaître sur plusieurs IP de sortie :
  le pool paraît suspect. Pour le free, l'identité est l'IP ; notre vault
  `(UA, project, ses_, msg_)` par IP respecte déjà cette règle.
- `FreeTierError` 403 : retenter une fois avec une nouvelle session dans la
  même requête, puis cooldown 15 min.

### Adopté

- Plafond `ledger_max_block_s: 1200` : le ledger garde le `Retry-After` brut
  pour l'observabilité, mais `reset_at` est plafonné pour que l'IP redevienne
  candidate et soit re-testée. Un nouveau 429 la rebloque.

### Rejeté

- Session neuve à chaque appel (comportement 2.1.4) : contraire au wire 2.2.5
  capturé, où la session est stable par conversation.

### À calibrer

- `quota_per_ip` : la valeur actuelle (500) est au-dessus des ~200 req/5 h
  mesurés. À ajuster quand un compteur serveur fiable sera disponible ; le
  ledger journalier reste la couche autoritaire.

## 3. `alztrk/opencode-ip-rotator`

Source : README + `services/upstream.py` (34 étoiles, mis à jour le
2026-11-05).

### Faits

- Gate minimal qui passe : `Authorization: Bearer public`, UA
  `opencode/<channel>/<version>/cli`, `x-opencode-client`,
  `x-opencode-project`, quartet de session. **Ni `b3` ni `traceparent` ne sont
  nécessaires** — notre jeu complet reste un sur-ensemble.
- `_env_present` exige que le contenu **commence** par `<env>` ; c'est plus
  strict que la mesure de rianilham (contenu libre). Notre prompt officiel
  2.2.5 place le bloc au milieu et passe : la position n'est donc pas
  contraignante.
- Outil `read` avec `properties: {"path": …}` et `required: ["path"]`.
- TLS : force TLS 1.2 + liste de ciphers OpenSSL. Alternative si le
  fingerprint Bun dérive ; pas nécessaire aujourd'hui.
- **Rotation d'IP par Cloudflare WARP** : chaque endpoint WARP est une IP de
  sortie gratuite, sans limite de sessions NordVPN.

### Adopté

- Schéma `read` non vide (`path`), aligné sur le gate à 7 outils.

### Testé — WARP (2026-11-06)

Conteneur `caomingjun/warp:latest`, SOCKS5 sur `127.0.0.1:11080`, probe avec
le wire 2.2.5 exact (`--client cli --project global`) :

| Test | Egress | Résultat |
|---|---|---|
| Première connexion WARP | `104.28.230.202` | **200**, 5,6 s, premier chunk SSE |
| Redémarrage du conteneur | `104.28.230.202` | même IP |
| `registration delete` + `registration new` ×3 | `104.28.230.202` | même IP |
| Deuxième conteneur simultané | `104.28.230.202` | même IP |
| Override vers 5 endpoints Cloudflare | — | tunnel non connecté |

Conclusion : **WARP fournit une IP utilisable supplémentaire par hôte/réseau,
mais ne tourne pas sur cette machine.** Le rotateur d'`alztrk` suppose que
`registration new` change l'IP ; ici Cloudflare réassigne la même. Pour six
stations WARP, il faudrait six hôtes (ou six IP publiques) différents, pas six
conteneurs.

La bonne nouvelle : l'IP WARP passe le gate **sans NordVPN**, sans session
supplémentaire et sans toucher à l'IP directe. C'est une deuxième station
immédiatement exploitable.

### Rejeté

- Session régénérée à chaque requête (même raison que ci-dessus).
- Downgrade TLS 1.2 : notre TLS Bun/BoringSSL rend 200 sur IP fraîche.

## 4. `beastbroak30/opencode-zen-proxy`

Source : README + statut live du 2026-11-05.

- Route les requêtes vers un **`opencode serve` local** : le vrai backend
  fournit la clé, les en-têtes de session et le protocole par modèle. C'est
  l'architecture « client officiel comme backend », qui élimine par
  construction tout écart de wire.
- Statut live : **0/37 modèles free** depuis son IP, tous en
  `FreeUsageLimitError` ou `unavailable`. Confirme une fois de plus que le
  429 est un quota d'IP, pas un défaut de forme.

### Verdict

Repli architectural si le gate change encore. Pas adopté maintenant : il
faudrait un processus `opencode serve` authentifié par station, ce qui
consomme plus de ressources que le wire actuel, déjà prouvé 200.

## 5. `nahdd123uosackr/opencode_proxy`

Wrapper Streamlit qui démarre un relais Node (`server.js`) et expose `/v1`,
`/res`, `/chat`. Aucune documentation de protocole, aucun test, 0 étoile.
**Rien à prendre.**

## 6. `xljiulang/opencode-proxy` (analyse précédente)

- Gate mesuré le 2026-11-05 : **7 outils nommés** (`glob`, `read`, `skill`,
  `webfetch`, `websearch`, `write`, `execute`) avec
  **`parameters.properties` non vide**. 0–6 outils → 429, 7 → 200. Un schéma
  vide ne compte pas.
- `resources/opencode_tools.json` contient les schémas réels.
- Les noms d'outils client snake_case (`exec_command`) échouent ; alias
  `fn_<camelCase>_ex` passe.

### Adopté

- `properties` non vide sur les placeholders. Notre grille en contient 13
  (dont les 7), donc elle satisfait aussi cette mesure.

### Non adopté

- Alias `fn_<camelCase>_ex` : nos tests live 2026-11-06 montrent que
  `read_file` et `exec_command` passent déjà en 200 sur une IP fraîche. Pas de
  raison de changer le nom des outils client tant que la mesure ne le impose
  pas.

## Convergence des mesures

| Point | rianilham | afree | alztrk | xljiulang | Notre wire |
|---|---|---|---|---|---|
| `stream:true` | requis | requis | forcé | — | forcé |
| `read` | requis | — | ajouté | 1 des 7 | présent (grille) |
| `<env>` + `Working directory:` | requis | — | ajouté | — | présent (prompt officiel) + garde |
| `properties` non vide | non | — | oui | **requis** | corrigé |
| Session | par processus | par appel | par appel | — | par (IP, conversation) |
| TLS | BoringSSL Bun | — | TLS 1.2 OpenSSL | Bun | Bun 1.4.4 |
| RA = minuit UTC | oui | oui (mesuré) | — | — | plafonné 20 min |

Le seul désaccord porte sur la **durée de vie de la session** et sur le
**nombre d'outils**. Nos mesures live tranchent : le client officiel 2.2.5 et
notre replay rendent le même verdict sur la même IP, et notre grille à 13
outils passe. On garde donc le wire capturé, en ajoutant les deux garde-fous
qui ne coûtent rien (`<env>`, `properties` non vides).

## Deuxième passe — lecture du code (pas seulement des README)

### Ce qui a été confirmé dans le code

- **`<env>` : seul le premier couple compte.** `rianilham` (`has_env_block`)
  et `xljiulang` (`inject_chat_identity_context`) s'arrêtent au premier token
  `<env>`. Un `<env></env>` vide, ou `<env x="1">`, devant un couple valide
  fait refuser la requête. Notre garde scannait tous les couples et pouvait
  donc laisser passer ce cas : corrigé.
- **`tool_choice: "none"` n'est jamais relayé.** `rianilham` le supprime
  (`chat_body`), notre code ne le corrigeait que sur le chemin où il injecte
  la grille. Un client « signature OK » qui désactive les outils prenait un
  faux quota : corrigé.
- **Le `Retry-After` est plafonné côté station, pas seulement côté ledger.**
  `afree` plafonne à 20 min ; `rianilham` à 120 s pour les clés. Notre
  `_free_429_cooldown_seconds` honorait le header jusqu'à 24 h **et** le
  `trial_dead_cooldown_s` (6 h) reprenait par-dessus. Le plafond de 20 min
  s'applique maintenant après ce `max` : c'est ce qui rend une station
  réellement re-testable.
- **La session officielle est stable par conversation/processus**, pas
  régénérée à chaque appel. `rianilham` la mint une fois par provider ;
  `xljiulang` la mappe par thread client ; `afree` la mint par appel parce
  qu'il vise le CLI 2.1.4. Notre choix (par IP + conversation, rotation
  30 min) est cohérent avec 2.2.5.
- **Le `read` doit avoir une description string et un schéma objet.** Un
  renderer strict renvoie `400` si `description` est `null`, et un schéma
  vide peut être refusé par certains modèles. Nos placeholders ont les deux.

### Ce qui est intéressant mais non adopté

- **UA `opencode/latest/2.2.5/cli`** (`xljiulang`) : le gate ne lit que la
  forme `opencode/…/cli` ; notre UA 1.19.5 capturé passe. On pourra l'aligner
  lors d'une prochaine capture 2.2.5, sans changer le reste du wire.
- **Profil TLS exact** (`xljiulang` : 17 ciphers, 11 sigalgs, courbes
  X25519MLKEM768/X25519/P-256/P-384, GREASE off ; `nahdd` : 20 sigalgs pour
  2.1.4). Notre `curl_cffi` 1.19.5 rend 200 sur IP fraîche ; le profil exact
  est une référence de secours, pas un correctif nécessaire.
- **Alias `fn_<camelCase>_ex`** (`xljiulang`) : nécessaire pour les noms
  snake_case sur certains modèles. Nos tests live `read_file`/`exec_command`
  passent sans alias sur `deepseek-v4.1-flash-free`. À garder en réserve si un
  modèle refuse un nom d'outil.
- **Endpoint par modèle** (`afree` `zen_endpoint.go`, `rianilham`
  `is_responses_style`) : `muse-spark-*` ne répond que sur `/responses`, et
  certains modèles ne répondent que sur `/chat/completions`. `afree` apprend
  l'endpoint sur un 500/400 caractéristique et le persiste. Notre modèle
  actuel est chat ; à implémenter si un modèle Responses-only entre dans la
  grille.
- **`SpentIps` + `ip_probe_url`** (`rianilham`) : cache des IP déjà épuisées
  et sonde optionnelle pour écarter une nouvelle session qui retombe sur une
  IP connue. Notre ledger `(IP, jour, bucket)` joue déjà ce rôle ; la sonde
  coûte une requête et n'est pas rentable.
- **Trim du frame `cost` après `[DONE]`** (`rianilham`, `nahdd`) : OpenCode
  ajoute `{"choices":[],"cost":"0"}` après `[DONE]` ; certains clients
  stricts le rejettent. Notre relais coupe au `[DONE]` sur les chemins
  convertis ; à vérifier si un client se plaint.
- **Pas de keep-alive après un 429** (`nahdd` : `DisableKeepAlives`,
  `CloseIdleConnections` sur 429) : une socket réutilisée après un 429 peut
  propager l'échec. Notre pool remet la session dans le pool après un 429 ;
  candidate d'amélioration, non mesurée comme nécessaire.
- **`opencode serve` comme backend** (`beastbroak30`) : le vrai client
  fournit l'identité. Repli architectural si le gate change encore.

### Ce qui est rejeté

- **Session neuve à chaque appel** (`afree`, `alztrk`) : correct pour 2.1.4,
  pas pour le wire 2.2.5 capturé.
- **TLS 1.2 + ciphers OpenSSL** (`alztrk`) : notre BoringSSL/Bun passe.
- **Session non inversée** (`beastbroak30` utilise `ms` brut) : le gate
  l'accepte peut-être, mais l'inversion est le comportement officiel ; on
  garde l'inversion.
- **`opencode` sans en-têtes de trace** : nos captures 2.2.5 les portent ;
  les retirer n'apporte rien.
