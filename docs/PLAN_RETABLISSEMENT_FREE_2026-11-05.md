# Plan de rétablissement — jambe free

**Date** : 2026-11-05  
**Déclencheur** : la jambe free ne rend plus de 200 ; l'opérateur ne voit que des 429 « quota dépassé », alors que le quota n'est pas forcément épuisé.  
**Constat historique** : une impersonation incomplète (mauvais nombre d'outils, mauvais corps, mauvais en-têtes) produit aussi un 429 `FreeUsageLimitError` — un faux quota.  
**Objectif** : retrouver un 200 avec contenu sur une IP, puis sur les six stations, sans paid, sans second compte, sans attendre.

Ce document est le **runbook opérationnel**. Le plan SpecKit `specs/003-official-client-parity/plan.md` reste le plan d'architecture ; ici, on rétablit le service.

---

## 0. Ce que disent les traces (2026-11-05)

Requêtes `requests.db` sur 24 h :

| Indicateur | Valeur |
|---|---:|
| Requêtes totales | 2 377 |
| `free_status=502` | 2 141 |
| `free_status=429` | 227 |
| Modèle | `jev-1.13-free` uniquement |
| Client | `python-requests/2.33.1` (smoke test), aucun client réel |
| Dernière requête client réelle | 2026-11-04 20:28 UTC, `deepseek-v4.1-flash-free`, **200** |

Les 429 récents sont de la forme :

```json
{"error":"Free usage limit reached for jev-1.13-free. Retry after 54s."}
```

`Retry-After` = **42–58 s**, pas minuit UTC. Ce n'est **pas** le quota journalier : c'est un **rate-limit court par modèle** (ou par couple modèle/IP). Le 502 « all tunnels failed » est un problème de tunnel/egress, pas de quota.

À l'inverse, les anciens 429 `deepseek` étaient :

```json
{"type":"error","error":{"type":"FreeUsageLimitError",
 "message":"Free usage limit reached. Free models work in OpenCode ..."}}
```

avec `Retry-After` de plusieurs heures (ex. 47 634 s). C'est le vrai bucket journalier par IP.

**Conclusion** : il y a au moins deux causes distinctes. Rotation d'IP sur un 429 court = perte de temps. Changement d'impersonation sur un 502 = perte de temps. Il faut trier avant d'agir.

### 0.1 Première campagne live (2026-11-05 22:47–22:52 UTC)

| Test | Egress | Profil | Statut | `Retry-After` | Corps |
|---|---|---|---:|---:|---|
| exact 2.2.5 CLI | direct résidentiel | `cli` | 429 | 6 956 s | `FreeUsageLimitError` |
| exact 2.2.5 CLI | direct résidentiel | `cli` | 429 | 6 948 s | `FreeUsageLimitError` |
| exact 2.2.5 CLI | direct résidentiel | `cli` | 429 | 6 948 s | `FreeUsageLimitError` |
| exact 2.2.5 desktop | direct résidentiel | `desktop` | 429 | 6 784 s | `FreeUsageLimitError` |

Les quatre requêtes utilisent le **corps 2.2.5 exact** (13 outils, `max_completion_tokens`, sans `tool_choice`), l'ordre 2.2.5 exact et le TLS 1.19.5. Aucune ne reçoit `FreeTierError` ni 403 : la signature de forme **semble acceptée** ; c'est le bucket de l'IP résidentielle qui est épuisé. Les modèles `deepseek`, `glm` et `mimo` partagent le même bucket sur cette IP.

Note : ces tests utilisent le `x-opencode-project` de la capture (`a24ad...`), pas `global`. La variante `global` (`--project global`) reste à tester sur une IP fraîche.

### 0.2 Blocage infrastructure

| Composant | État mesuré | Conséquence |
|---|---|---|
| Stations 1–6 | `0/6 usable`, `state=repairing`, `last_error=auth`, `last_ok_age ~14,7 h` | aucun egress frais |
| OpenVPN stations 3/5 | `AUTH_FAILED` (« credentials might be wrong ») | tunnel impossible |
| OpenVPN stations 2/4 | `Connection reset` | tunnel instable |
| WireGuard station 6 | handshake OK, DNS/healthcheck timeout | pas d'internet |
| SOCKS `socks-se8.nordvpn.com` | `User was rejected by the SOCKS5 server` | credentials service refusés |
| Direct résidentiel | `FreeUsageLimitError`, RA ~6 800–6 956 s | bucket IP épuisé |

**Conclusion** : le prochain blocage n'est ni le corps ni les en-têtes. C'est l'**egress** : les credentials NordVPN service sont refusés (OpenVPN et SOCKS), et l'IP directe est en quota. Tant qu'une IP fraîche n'existe pas, on ne peut pas distinguer « CLI accepté » de « desktop accepté » au-delà du fait que les deux atteignent la couche quota sans 403.

Un test WireGuard complémentaire sur `de1129.nordvpn.com` (station 6) a donné : handshake « setup complete », puis DNS/healthcheck en timeout et `curl` SOCKS sans réponse. Le `.env` a été restauré après le test. Cela suggère que la clé WireGuard ou le serveur n'est pas exploitable, pas que le wire HTTP est mauvais.

Le plan N (auth/egress) est donc le prérequis des plans A/B de parité.

### 0.3 Corrections déjà appliquées (2026-11-06)

- **Ordre 2.2.5** : `_OPENCODE_HEADER_ORDER_225` + passage des noms d'en-têtes à `_get_free_fp_kwargs`. Le wire réel mesuré (e2e curl_cffi) est désormais : `Authorization, Content-Type, User-Agent, b3, traceparent, x-opencode-client, x-opencode-project, x-opencode-session, x-opencode-session-id, x-session-affinity, x-session-id, Connection, Accept, Host, Accept-Encoding, Content-Length`.
- **Rotation gelée sur 1.19.5** : c'est le seul profil TLS (Bun 1.4.4) identique au 2.2.5 ; les combinaisons UA 2.2.5 + TLS 1.3.14 ne sont plus produites. Les anciens fichiers `logs/identities/*.json` figés sur 1.18.31/1.19.0/1.19.3 **migrent vers 1.19.5 au chargement** et sont re-persistés.
- **Project `global`** : `apply_spec` ne remplace plus `global` (ou vide) par le hash du dépôt réel.
- **Trace ID** : stable par session (`_TRACE_IDS`), span neuf par requête.
- **Cooldown local** : `FreeIPPool.clear_model_global()` + `POST /api/free/quota/clear` (`scope=model_global|all`).
- **Outillage** : `scripts/refresh_nord_credentials.py`, `scripts/test_free_egress.py --client/--project/--dry-run`, `tests/test_official_wire_e2e.py`.
- Tests : **144 cas** ciblés passent, dont l'ordre 2.2.5 réel, le project `global`, la stabilité du trace, le gel/migration de version et le cooldown local.

Reste à faire avant un 200 durable : **Plan N** (token/credentials), puis **Plan A** live CLI/desktop, puis le corps `compat` (placeholders vs définitions complètes) et le prompt templaté.

### 0.4 Credentials valides et limite de sessions (2026-11-06)

Le token fourni a été accepté (`/v1/users/services/credentials` → 200) et les credentials service ont été régénérés. Le vrai blocage restant est la **limite de sessions simultanées NordVPN** :

- six stations actives + un conteneur de test = `AUTH_FAILED` ;
- six stations arrêtées, un seul conteneur de test = **`Initialization Sequence Completed`** immédiat ;
- l'egress obtenu (`87.120.102.86`) répond `FreeUsageLimitError` avec `Retry-After` ~71 431 s : le wire exact CLI **et** desktop est accepté, mais cette IP est épuisée.

Donc : les credentials ne sont plus le problème. Il faut (1) mesurer la limite exacte de sessions du compte, (2) ne pas lancer plus de stations que cette limite, (3) laisser l'allocateur choisir un serveur/une IP fraîche, (4) relancer le probe exact sur l'IP obtenue.

**Test de limite** : arrêter toutes les stations, démarrer **une** station, vérifier `AUTH` + egress, puis ajouter les stations une par une jusqu'au premier `AUTH_FAILED`. La dernière valeur qui passe est la limite réelle. Si elle est inférieure à 6, le nombre de stations doit être aligné (ou les autres appareils du compte déconnectés) ; aucun paid n'est requis.

### 0.5 Clean-room 2026-11-06 — résultat final

Deux passes avec client neuf (binaire Linux officiel 1.19.5), workspace vide, HOME vide, conteneur neuf, et sans état proxy :

| Scénario | Client officiel | Replay exact du proxy | Verdict |
|---|---|---|---|
| IP directe résidentielle fraîche (`37.69.230.71`) | **200 `pong`** | **200** | wire conforme |
| IP VPN partagée (`64.238.204.228`) | **429** `Free usage limit reached` | **429** | IP épuisée |
| Proxy local `POST /v1/chat/completions` en direct | — | **200 `pong`**, **200 code**, **200 stream** | jambe free opérationnelle |

Conclusion : le client officiel et le replay exact ont le **même comportement sur la même IP**. Le 429 VPN n'est pas un échec d'impersonation ; c'est le bucket de l'IP partagée. Le 200 direct prouve que le wire exact (16 en-têtes, 13 outils, TLS Bun 1.4.4) passe.

**État courant** : proxy redémarré en `proxy_mode: direct`, `station_count: 1` (sauvegardes `config.yaml.bak-one-station-20261106` et `config.yaml.bak-direct-20261106`). Il rend 200 sur l'IP directe. Les six stations VPN restent à réparer quand la limite de sessions sera alignée.

### 0.6 Recherche sur l'historique (2026-11-06)

Candidats extraits de `requests.db` (succès 200, hostname + IP) :

| Host | IP historique | Succès | Dernier 200 |
|---|---|---:|---|
| `us12806.nordvpn.com` | `87.120.102.200` | 211 | 2026-11-04 20:21 |
| `al74.nordvpn.com` | `87.120.102.61` | 1 586 | 2026-11-04 19:26 |
| `al46.nordvpn.com` | `87.120.102.220` | 803 | 2026-11-04 21:04 |
| `al41.nordvpn.com` | `87.120.102.90` | 515 | 2026-11-04 20:31 |
| `al36.nordvpn.com` | `87.120.102.9` | 411 | 2026-11-04 20:25 |
| `pl296.nordvpn.com` | `87.120.102.14` | 119 | 2026-11-04 20:03 |
| `al117.nordvpn.com` | `87.120.102.72` | 154 | 2026-11-04 17:53 |
| `uk2433.nordvpn.com` | `89.46.62.134` | 78 | 2026-11-04 15:08 |

Testés un par un en WireGuard, une seule session : `us12806`, `pl296`, `al74`, `al117`, `uk2433`, `us13473`, `au1140`, `nl-onion9`, `lt40`, `au1101`, plus `al46`, `al73`, `al37`, `al36`, `al41`, `al45`. Tous les egress obtenus répondent **429** (IP partagée déjà consommée) ; certains serveurs ne donnent même pas d'egress. Trois serveurs hors historique (`nz123`, `ie257`, `mx173`) : `mx173` donne `187.13.205.237` → 429.

Conclusion : l'historique sert à **ordonner** les candidats, pas à garantir le quota courant. Une IP historiquement bonne peut être épuisée aujourd'hui. La seule IP fraîche disponible sur cette machine est l'IP directe `37.69.230.71`, qui rend 200.

### 0.7 Dépôts tiers et correctifs (2026-11-06)

Analyse complète dans `docs/REPO_INTEL_2026-11-06.md`. Trois règles en sont
sorties, appliquées au code :

1. **Garde `<env>`** — deux passerelles indépendantes mesurent qu'un message
   système doit porter `<env>…</env>` avec `Working directory:`, sinon
   `429 FreeUsageLimitError` même avec le bon prompt et `read`. Notre prompt
   officiel 2.2.5 en contient un, mais le chemin « signature officielle »
   renvoyait le corps tel quel : un prompt recopié sans `<env>` partait donc
   sans garde. `_free_wire_body` vérifie maintenant **avant** le retour rapide
   et insère un bloc valide si nécessaire.
2. **`properties` non vide** — `xljiulang/opencode-proxy` mesure qu'un schéma
   `{"type":"object","properties":{}}` ne compte pas dans le gate. Les
   placeholders gardent leur nom mais reçoivent `{"path":{"type":"string"}}`.
3. **Plafond de cooldown** — `Retry-After` est la distance à 00:00 UTC, pas la
   vraie recovery (`afree-proxy` : RA 20 h 46, puis 200 sur la même IP 18 min
   plus tard). Deux verrous, pas un : `ledger_max_block_s: 1200` pour le
   ledger **et** `free.cooldown_cap_s: 1200` pour `_free_429_cooldown_seconds`.
   Le second est indispensable : le premier ne plafonnait que le ledger, alors
   que la station était encore parkée par le header (jusqu'à 24 h) ou par
   `trial_dead_cooldown_s` (6 h). Le plafond s'applique après ce `max`.
4. **`tool_choice: "none"` retiré** de la copie free : le gate le refuse, et
   l'ancien code ne le corrigeait que lorsqu'il injectait lui-même la grille.

**Cloudflare WARP testé (2026-11-06)** : conteneur `caomingjun/warp` + SOCKS5,
probe 2.2.5 exacte → **200** sur `104.28.230.202` en 5,6 s. C'est une
deuxième station utilisable sans NordVPN et sans toucher à l'IP directe.
En revanche, la rotation ne marche pas sur ce réseau : redémarrage,
`registration delete/new` ×3, second conteneur et cinq overrides d'endpoint
donnent la même IP (ou pas de tunnel). WARP = **une IP par hôte/réseau**, pas
six. Pour six stations WARP, il faut six hôtes avec six IP publiques
différentes.

**Test de gate décisif** : un corps avec le marqueur de prompt officiel
**sans `<env>`**, un seul outil `read` et `stream:true` rend **200** via le
proxy après la garde. Donc, sur cette IP au 2026-11-06, le gate est bien
`<env>` + `read` (+ stream) ; la mesure « 7 outils obligatoires » de
`xljiulang` n'est pas reproduite. Notre grille à 13 outils reste un
sur-ensemble sûr.

**Deux stations opérationnelles (2026-11-06)** : le pool est passé en
`proxy_mode: socks5` avec WARP (`127.0.0.1:11080`, `104.28.230.202`) et un
pont SOCKS5 direct (`scripts/direct_socks5.py`, `127.0.0.1:11084`,
`37.69.230.71`). Rotation observée `11080 → 11084 → 11080 → 11084`, 4 × 200 ;
stream 200. Sauvegarde config `config.yaml.bak-socks5-20261106`. Contrat :
`specs/004-repo-parity-audit/contracts/egress.md`.

**Troisième bloc IPv6 (2026-11-06)** : le tunnel WARP sort aussi en
`2a09:bac5:32d4:be::13:23e` (`/64`). Probe `/inference` avec le wire exact et
`IPRESOLVE=6` → **200** ; `IPRESOLVE=4` → **200**. Le bloc est **exposé dans
le pool** comme entrée `warp-ipv6` (`ipv6: true`, label distinct,
`IPRESOLVE=6` par session, ledger `/64`) : six requêtes distinctes, **6 × 200**
en rotation IPv6 → direct → IPv4. Sauvegarde `config.yaml.bak-ipv6-20261106`.
Doc : `docs/IPV6_WARP_STATION_2026-11-06.md`. Test négatif : trois sessions
fraîches sur `186.247.77.36` → **3 × 429** (RA ~61 905 s), donc la rotation de
session ne débloque pas une IP épuisée ; le quota est par bloc.

**Gate et quota — deuxième lot (2026-11-06)** : `ebouther` mesure le gate en
**quatre conditions** (UA ≥ 1.18.0, session `ses_` canonique `…ffe`, outil
`read` exact, `stream:true`), plus le bloc `<env>` ; le nombre d'outils est
sans effet. Le quota est keyé par **bloc d'egress** (`/64` IPv6, `/32` IPv4),
reset 00:00 UTC, pas par IP exacte. WARP IPv4 est une NAT partagée ; l'hôte
Windows n'a aucune IPv6 globale (`fe80::` seulement), donc la voie `/64`
demande un hôte Linux avec un préfixe routé. Notre `server_ip_key` tronque
déjà l'IPv6 au `/64`, et le mint `ses_` est conforme
(`tests/test_session_canonical.py`).

**Diagnostic stations VPN (2026-11-06)** : les six stations bouclaient sur un
échec DNS gluetun. `DNS_UPSTREAM_RESOLVER_TYPE=plain` ne résout rien
(`127.0.0.1:53 server misbehaving`) et l'override généré
(`logs/compose-hostname-*.yml`) forçait `plain` après deux échecs DoT.
OpenVPN échoue `AUTH_FAILED` ; WireGuard authentifie. Un conteneur témoin
**WireGuard + DoT + MTU 1419 + `HEALTH_VPN_DURATION_INITIAL=60s`** est
healthy et sort sur `187.40.243.35` (mais cette IP est 429). Deux stations
sont déjà saines (`186.247.77.36`, `186.247.77.6`) et leurs IP sont aussi
429 (RA ~64 200–64 700 s). Conclusion : le tunnel est réparable, le blocage
restant est le quota par IP. Le superviseur a été arrêté pour l'expérience ;
le config passe à `dns.fallback_order: [dot]` et
`fast_discovery_transport: wireguard`.

---

## 1. Triage 429 — 5 minutes

| Corps / indice | `Retry-After` | Signification | Action |
|---|---:|---|---|
| `Free usage limit reached for <model>. Retry after 42s` **généré par le proxy** | 30–120 s | cooldown local `model_global_until` ; la requête ne part pas | **Plan M** : purger/verifier le cooldown, pas de rotation |
| `FreeUsageLimitError` + message « subscribe to Go » | heures → minuit UTC | bucket journalier **par IP** | rotation d'IP, pas d'identité |
| `Free usage limit reached for <model>. Retry after 42s` **relayé de l'amont** | 30–120 s | rate-limit court modèle/IP | attendre le RA, sérialiser (Plan I) |
| `FreeTierError: free tier can only be used from within OpenCode` | — | signature de forme refusée | wire exact, rotation inutile |
| 403 `FreeTierError` | — | idem | wire exact |
| 502 `all tunnels failed` | — | tunnel/egress mort | réparation station, pas quota |
| 200 + flux vide | — | problème SSE/stream | inspecter la réponse, pas le quota |

### Commandes de triage

```powershell
# 1. Les dernières requêtes réelles et leur statut
python logs/recent_requests.py

# 2. Les 429 récents et leur corps exact
python .artifacts/audit-2026-11-05/query_429b.py

# 3. Le dernier 429 du client (à capturer côté client)
#    noter : modèle demandé, corps de réponse, Retry-After, IP d'egress
```

**Règle** : ne jamais changer deux variables à la fois. D'abord classer le 429, ensuite appliquer le plan correspondant.

---

## 2. Plans candidats (A → N)

### Plan N — Restaurer l'egress et les credentials (prérequis de tout test live)

**But** : retrouver au moins une IP fraîche avant de parler de parité.

Le diagnostic du 2026-11-05 montre `0/6` stations utilisables, `AUTH_FAILED` sur OpenVPN, SOCKS `user rejected`, et l'IP directe en quota. Aucun test CLI/desktop ne peut être concluant sans une IP fraîche.

- Vérifier `credentials.env` : `OPENVPN_USER` / `OPENVPN_PASSWORD` (service NordVPN), sans les logger.
- Vérifier le token : `python scripts/refresh_nord_credentials.py --check`. **2026-11-06** : token accepté (200), credentials service régénérés. Un token 401 signifie qu'il faut un nouveau token.
- Vérifier le nombre de sessions simultanées du compte : **mesuré** — six stations + un conteneur de test = `AUTH_FAILED` ; six stations arrêtées, un seul conteneur = auth immédiate. La limite est donc atteinte par la flotte actuelle, pas par les credentials.
- Démarrer **une** station, vérifier `AUTH` OK puis `egress` réel (`GET /v1/publicip/ip`, probe `https://api.ipify.org`). Ajouter les stations une par une jusqu'au premier `AUTH_FAILED` pour mesurer la limite exacte.
- Si la limite est < 6, aligner `station_count` ou libérer les sessions d'autres appareils du compte ; ne pas lancer de 7e session.
- Ne pas rotationner en boucle : un `AUTH_FAILED` avec toutes les sessions occupées n'est pas une mauvaise IP.
- Tester le SOCKS `socks-*:1080` avec les mêmes credentials ; si `User rejected`, les credentials sont en cause, pas le réseau.
- WireGuard : vérifier la clé privée et le `SERVER_HOSTNAMES` ; le handshake peut réussir sans DNS (le healthcheck DNS échoue).
- Ne pas rotationner en boucle : chaque `AUTH_FAILED` est une preuve de credentials, pas une mauvaise IP.
- Une fois une IP fraîche obtenue, revenir au Plan A.

**Quand** : `0/6 usable`, `AUTH_FAILED`, SOCKS rejected, ou aucune IP fraîche.
**Attendu** : 1 station `usable`, egress public stable, probe 200.
**Risque** : aucun ; c'est un prérequis.
**Rollback** : conserver les autres stations telles quelles.

### Plan A — Replay exact 2.2.5 `cli` (recommandé en premier)

**But** : reproduire à l'octet près le client officiel 2.2.5 du fixture, sur une IP connue, avec le corps exact.

- En-têtes : ordre 2.2.5 complet (`b3`, `traceparent` en 4–5 ; session en 8–11).
- Corps : 13 outils officiels **complets**, `max_completion_tokens: 32000`, `stream: true`, `stream_options`, **pas** de `tool_choice`.
- TLS : profil 1.19.5/Bun 1.4.4 (`1523504b...`), déjà exact.
- IDs : `ses_`/`msg_` format client ; trace stable par session.
- Prompt : date du jour, chemin synthétique, project `global` ou dérivé IP.

**Quand** : dès qu'un 403/`FreeTierError`/429 long apparaît sur un client réel.
**Attendu** : 200 + contenu. Si oui, le problème est la forme, pas le quota.
**Risque** : le gate exige peut-être un prompt figé ; garder le rollback B.
**Rollback** : `OPENCODE_FREE_WIRE_STYLE=legacy` ou profil 1.19.5.

### Plan B — Rollback 1.19.5

**But** : revenir au dernier wire qui marchait avant le 2026-11-04 20:28 (deepseek 200).

- 13 en-têtes, 11 outils, `max_tokens`, `tool_choice: auto`, pas de `b3`/`traceparent`.
- TLS 1.19.5 (Bun 1.4.4).

**Quand** : le Plan A échoue en 403/429 alors que le payload 2.2.5 est en cause (13 outils, pas de `tool_choice`).
**Attendu** : 200 sur l'IP qui marchait.
**Risque** : le gate a peut-être durci depuis ; le 1.19.5 peut être refusé.
**Rollback** : revenir à 2.2.5.

### Plan C — Correctif d'ordre des en-têtes uniquement

**But** : si le corps passe déjà, ne changer que l'ordre.

- Ajouter `b3`, `traceparent`, `x-session-affinity`, `x-session-id` dans `header_order`.
- Reconstruire le dict dans l'ordre 2.2.5.
- Ne pas toucher au corps.

**Quand** : un 200 existe avec le corps actuel mais un 403/429 apparaît dès qu'on ajoute le tracing.
**Risque** : faible, rollback par `OPENCODE_FREE_HEADER_ORDER_ON=0`.

### Plan D — Correctif de corps uniquement

**But** : si les en-têtes sont déjà acceptés, corriger le corps.

- Remplacer les placeholders par les 13 définitions complètes.
- Retirer `tool_choice` en 2.2.5.
- Garder 13 outils exactement, pas d'outil client en plus pour le mode `official`.
- Conserver `max_completion_tokens: 32000`.

**Quand** : 403/`FreeTierError` avec un corps réduit ou placeholders.
**Risque** : budget de corps ; rollback placeholders.

### Plan E — A/B placeholders vs définitions complètes

**But** : trancher la contradiction du dépôt (le code envoie des placeholders, les mesures antérieures disent 403).

- Variante E1 : 13 placeholders `properties: {}`.
- Variante E2 : 13 définitions complètes.
- Même IP, même minute, témoin intercalé, 20 échantillons.

**Quand** : dès qu'un client tiers (Hermes, Claude Code) est utilisé.
**Attendu** : une des deux seulement passe ; on généralise.
**Risque** : aucun si on garde le mode `official` séparé.

### Plan F — Prompt templaté (date / chemin / AGENTS.md)

**But** : supprimer la fuite et le tell sans casser la signature.

- Variante F1 : payload figé actuel (date 2026-11-04, chemin réel).
- Variante F2 : date du jour seulement.
- Variante F3 : date + chemin synthétique + `AGENTS.md` neutralisé.
- Variante F4 : prompt officiel complet mais sans contenu propre à l'opérateur.

**Quand** : si le gate ne hash pas le prompt, ou si le 403 apparaît sur un prompt daté.
**Risque** : si le gate hash le prompt exact, F2/F3 échouent → garder F1 en rollback.

### Plan G — TLS / transport

**But** : vérifier que le TLS n'est pas le problème.

- TLS 1.18.31 = `d871d02...` (Bun 1.3.14, padding 21).
- TLS 1.19.5 / 2.2.5 = `1523504...` (Bun 1.4.4, courbe 4588).
- Le proxy est déjà exact sur ces deux profils.

**Quand** : seulement si un ClientHello capturé diffère. Sinon, ne pas y toucher.
**Risque** : casser un profil qui marche.

### Plan H — Routage et modèle

**But** : arrêter de confondre `jev-1.13-free` (SystemOne, rate-limit court) avec le free chat.

- `jev-1.13-free` vit sur `/inference/v1/systemone`, avec un rate-limit court.
- Les smoke tests qui martèlent jev toutes les secondes produisent les 429 courts et les 502.
- Router le client vers `deepseek-v4.1-flash-free`, `glm-5.3-flash-free` ou `mimo-v2.6-flash-free`.
- Réduire la fréquence des smoke tests ou les sortir du chemin client.

**Quand** : 429 courts + 502 + aucun trafic client réel dans la DB.
**Attendu** : disparition des 429 courts, retour du trafic client.
**Risque** : faible ; garder jev pour les tests SystemOne uniquement.

### Plan I — Concurrence et backoff

**But** : éviter le rate-limit court par rafale.

- Sérialiser par `(modèle, IP)` avec un sémaphore.
- Respecter le `Retry-After` court (30–120 s) au lieu de retenter immédiatement.
- Pas de hedge sur un 429 court : un hedge multiplie la rafale.
- Un seul retry par requête cliente, puis erreur explicite.

**Quand** : 429 courts répétés, surtout après un redémarrage ou un smoke test.
**Risque** : latence accrue ; acceptable.

### Plan J — IP / egress

**But** : traiter le vrai quota journalier.

- `FreeUsageLimitError` + RA long → bucket IP épuisé.
- Changer d'IP (station/rotation), pas d'identité.
- Vérifier l'egress réel avant de conclure (`current_ip`, probe).
- Ne jamais conclure du pool à partir d'une IP.

**Quand** : RA long, reset à minuit UTC, plusieurs IP testées.
**Risque** : consommation de rebind ; ledger.

### Plan K — Profils versionnés complets

**But** : retrouver la diversité sans hybride.

- Un profil complet par version (1.18.31, 1.19.0, 1.19.3, 1.19.5, 2.2.5).
- Chaque profil porte UA, client, TLS, ordre, corps, titre, IDs.
- Test de cohérence par tuple.

**Quand** : après le rétablissement, pas avant.
**Risque** : complexité ; à faire progressivement.

### Plan L — Canary et rollback

**But** : déployer sans casser les six stations.

- 1 station canary, 10 requêtes, témoin.
- Si 10/10 → 2 stations → 6 stations.
- Critère : 200 + contenu, pas seulement 200.
- Rollback automatique si le taux de 200 chute.

**Quand** : après chaque correctif.
**Risque** : aucun si le canary est respecté.

### Plan M — Purger le cooldown local (faux 429 du proxy)

**But** : traiter le 429 que le proxy fabrique lui-même.

Le corps exact est :

```json
{"error":"Free usage limit reached for jev-1.13-free. Retry after 54s."}
```

Il est produit par `opencode.py` quand `pool.model_global_remaining(model) > 0` ; **aucune requête ne part vers l'amont**. C'est un cooldown en mémoire, pas un quota fournisseur.

- Vérifier `GET /api/free-health` → `model_global_backoff`, `quota_ledger`, `quotas`.
- Chercher dans `logs/debug.log` le 429 amont qui a armé le cooldown (modèle, IP, RA).
- Vérifier que le cooldown est bien décroissant et qu'il n'est pas ré-armé par le 429 local lui-même.
- Purger via `POST /api/free/quota/clear` si disponible, ou redémarrer le proxy en gardant l'état ; vérifier que le modèle repart.
- **Implémenté** : `FreeIPPool.clear_model_global(model)` purge le backoff ciblé ou tous, et `POST /api/free/quota/clear` accepte `{"model": "...", "scope": "model_global"|"all"}` (le clear par défaut d'un modèle purge aussi son backoff global). Le ledger d'IP reste `clear_quota_buckets`.
- Ajouter un test : un 429 local ne doit ni prolonger ni ré-armer `model_global_until` ; il doit expirer.
- Ajouter un test : `clear_model_global(model)` remet `model_global_remaining(model)` à 0 et la requête suivante atteint l'amont.

**Quand** : 429 local, aucune trace de requête amont, DB `free_model_usage` avec `ip` vide ou identique.
**Attendu** : la prochaine requête atteint réellement l'amont ; on voit un 200 ou un vrai 429 amont.
**Risque** : faible ; le cooldown est un cache, pas une source de vérité.

---

## 3. Arbre de décision

```text
État de la flotte
│
├─ 0/6 usable / AUTH_FAILED / SOCKS rejected / aucune IP fraîche
│  └─ PLAN N (credentials + egress) → obtenir 1 station usable
│
Client reçoit 429
│
├─ Corps = "Free usage limit reached for <model>. Retry after Ns." et aucune requête amont
│  └─ PLAN M (cooldown local) → vérifier/purger, pas de rotation
│
├─ Corps = FreeTierError / "free tier can only be used from within OpenCode"
│  └─ PLAN A (replay exact 2.2.5) → si échec PLAN B (1.19.5) → si échec PLAN D/E
│
├─ Corps = FreeUsageLimitError + RA long (heures → minuit UTC)
│  └─ PLAN J (rotation IP) → retest sur la nouvelle IP
│
├─ Corps = "Free usage limit reached for <model>. Retry after <60s" relayé de l'amont
│  └─ PLAN H (modèle/routage) + PLAN I (backoff) → pas de rotation
│
├─ 403
│  └─ PLAN A/C/D/E (wire) → pas de rotation
│
├─ 502 "all tunnels failed"
│  └─ réparation tunnel/egress (pas wire, pas quota)
│
└─ 200 mais flux vide
   └─ vérifier SSE/stream, pas le quota
```

---

## 4. Séquence de rétablissement (0–60 min)

1. **Vérifier l'egress** : `GET /api/free-health` → `capacity.usable_stations`. Si `0/6`, `AUTH_FAILED` ou SOCKS rejected → **Plan N** avant tout. Obtenir **une** station usable.
2. **Geler** la rotation d'identité : `OPENCODE_CLIENT_VERSION=2.2.5`, `ROTATION_VERSIONS=("2.2.5",)`, ou profil unique.
3. **Capturer** la requête cliente exacte et son 429 (modèle, corps, RA, IP).
4. **Classer** avec le tableau §1.
5. **Si 429 local** (`Free usage limit reached...` sans requête amont) → **Plan M** : vérifier/purger `model_global_until` avant tout.
6. **Plan A** sur une IP connue : exact 2.2.5 `cli`, 13 outils complets, ordre complet, TLS 1.19.5, date du jour, project `global`.

```powershell
# Test exact 2.2.5 (capture + payload) sur une station ou en direct
python scripts/test_free_egress.py --proxy direct --model deepseek-v4.1-flash-free --repeat 1
python scripts/test_free_egress.py --station 3 --model deepseek-v4.1-flash-free --repeat 1
```

7. Si 200 → **canary** 10 requêtes, puis 2 stations, puis 6.
8. Si 429 court → attendre le RA, relancer la même requête ; si 200, c'était le rate-limit (Plan H/I).
9. Si 429 long → **Plan J** : nouvelle IP, retester le même wire.
10. Si 403 → **Plan D/E** : plein outils vs placeholders ; puis **Plan F** si le prompt est en cause.
11. Si 502 → **Plan H/réparation tunnel** ; ne pas toucher au wire.
12. Ne jamais conclure « quota épuisé » depuis une seule IP ; ne jamais proposer le paid.

---

## 5. Campagne de tests complète

### 5.1 Tests unitaires (hors réseau)

- Ordre exact des 16 en-têtes 2.2.5, avec `b3` en 4 et `traceparent` en 5.
- Tuple de cohérence `(version, UA, client, TLS, header_order, body_keys, tools)`.
- `project=global` ne devient jamais le hash du dépôt.
- Prompt : date UTC, aucun chemin réel, aucun `AGENTS.md` de l'opérateur.
- Trace : même trace ID sur deux requêtes d'une session, spans différents.
- Corps : 13 outils, pas de `tool_choice`, `max_completion_tokens=32000`.
- Placeholders : interdits par défaut en mode `official`.
- Titre : template 2.2.5 dédié (pas de grille principale).

```powershell
python -m pytest tests/test_official_client_parity.py `
  tests/test_free_wire_complet.py `
  tests/test_free_official_payload.py `
  tests/test_tls_empreinte_binaire.py `
  tests/test_per_ip_identity.py -q
```

### 5.2 Tests de contrat / parité

- Diff normalisé proxy ↔ `2.2.5-1.json` : noms, ordre, clés, outils, schémas.
- Diff proxy ↔ `1.19.5-1.json` pour le rollback.
- Test end-to-end curl_cffi sur le chemin **per-IP** (pas le chemin sans IP).
- TLS : capture ClientHello locale et comparaison JA3/MD5.
- Titre + principal : deux templates distincts.
- **Matrice CLI vs desktop** : `--dry-run` doit montrer les 16 en-têtes dans le même ordre, seuls `User-Agent` et `x-opencode-client` changent ; le corps et les 13 outils sont identiques.
- **Test live CLI vs desktop** : une requête par profil sur la même IP fraîche, témoin intercalé ; comparer 200/403/429 et `FreeTierError`.

```powershell
python scripts/test_free_egress.py --dry-run --client cli
python scripts/test_free_egress.py --dry-run --client desktop
# après Plan N (IP fraîche) :
python scripts/test_free_egress.py --station 1 --client cli --project global --model deepseek-v4.1-flash-free --repeat 1
python scripts/test_free_egress.py --station 1 --client desktop --project global --model deepseek-v4.1-flash-free --repeat 1
```

```powershell
python .artifacts/audit-2026-11-05/probe_e2e.py
python .artifacts/audit-2026-11-05/capture_tls.py --cli <client> --version 2.2.5 --standalone
```

### 5.3 Tests d'intégration

- SSE → JSON non-stream : contenu et `tool_calls` conservés.
- Conversation > 30 min : session stable.
- Deux IP : aucun `ses_`, `msg_`, `project`, trace ou cwd partagé.
- Client officiel (prompt + `read`) : corps non modifié.
- Client Hermes : mode `compat` explicite, outils réalistes.
- Titre / summary : pas d'injection de la grille principale.

### 5.4 Canary live

- 1 IP, 1 requête, puis 10 requêtes consécutives.
- Même IP, même fenêtre, témoin intercalé.
- Mesurer : 200, 403, 429 court, 429 long, 502, TTFT, contenu non vide.
- 20 échantillons par variante (A–F) pour l'A/B.
- Passer de 1 → 2 → 6 stations seulement si 10/10.

### 5.5 Charge et concurrence

- 6 stations en parallèle, 8 requêtes par station.
- Vérifier qu'un 429 court ne déclenche pas de hedge storm.
- Vérifier que le backoff respecte le RA.
- Vérifier que le ledger ne marque pas une IP comme épuisée sur un 429 court.

### 5.6 Privacy / sécurité

- Grep des requêtes sortantes : chemin réel, `AGENTS.md`, project hash, IDs partagés.
- Vérifier qu'aucune clé payante ne part (invariant A.0).
- Vérifier qu'aucun cookie / header navigateur n'est ajouté.

```powershell
rg -n "C:\\\\Users\\\\julie|AGENTS.md|a24ad984" .artifacts/audit-2026-11-05/
```

### 5.7 Rollback

- `OPENCODE_CLIENT_VERSION=1.19.5` → wire 1.19.5.
- `OPENCODE_FREE_WIRE_STYLE=legacy` → wire 1.18.x.
- `OPENCODE_FREE_HEADER_ORDER_ON=0` → sans ordre imposé.
- Vérifier qu'un rollback redonne 200 sans redéploiement.

---

## 6. Critères de succès

1. **200 + contenu** sur une IP avec le Plan A, puis 10/10.
2. **6/6 stations** client-utilisables, 200 + contenu.
3. **Zéro `FreeTierError`** sur le chemin client.
4. **429 courts** gérés par backoff, pas par rotation.
5. **429 longs** traités par rotation d'IP, pas par changement d'identité.
6. **Zéro 502** sur le chemin client (les 502 de probe SystemOne ne comptent pas).
7. **Diff de wire vide** contre la capture 2.2.5.
8. **Aucune fuite** de chemin/project/AGENTS.md.
9. **Tests unitaires + e2e + canary** verts.
10. **Rollback** disponible sans redéploiement.

---

## 7. Ce qu'il ne faut pas faire

- Rotationner l'IP sur un 429 court : c'est un rate-limit, pas un quota.
- Traiter un `AUTH_FAILED` comme une mauvaise IP : c'est un problème de credentials/sessions, pas d'egress.
- Changer UA/TLS/corps en même temps : on ne sait plus ce qui a marché.
- Conclure « quota épuisé » depuis une IP ou depuis un 429 sans corps.
- Proposer le paid, un second compte ou l'attente comme solution free.
- Lancer la campagne 6 stations avant le canary 1 station.
- Se fier au 200 seul : un 200 sans contenu n'est pas un succès.
