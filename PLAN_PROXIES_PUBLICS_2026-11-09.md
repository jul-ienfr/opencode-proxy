# Plan — pool de proxys publics comme provider d'egress

**Date** : 2026-11-09
**Objet** : récupérer, valider et intégrer des listes de proxys publics dans la
jambe free, comme provider d'egress supplémentaire (`proxies`), sans toucher aux
six stations WARP ni à la jambe payante.
**Statut** : Phase 0 terminée (collecte + mesure réelle). Phase 1 à valider.

---

## 0. Verdict

**Go, mais en provider d'appoint capé, jamais comme socle.**

- 20 045 endpoints publics collectés, 10 994 hôtes distincts, 23 sources.
- Sur un échantillon de 600 : 149 vivants (24,8 %), 133 egress uniques.
- Sur les 149 vivants, **29 répondent réellement 200** au wire free
  (`deepseek-v4.1-flash-free`), avec **29 IP de sortie distinctes**.
- 86 répondent 429 `FreeUsageLimitError` : ces IP sont déjà épuisées, pas
  mortes. Un re-test de 30 d'entre elles avec `glm-5.3-flash-free` donne
  **30/30 429** → traiter un 429 comme un bucket IP épuisé, pas comme un proxy
  mort.
- Les 29 gagnants viennent tous de la même liste HTTPS (proxifly/zloi, listes
  identiques). Les échantillons HTTP et SOCKS5 n'ont produit **aucun 200**.
- Rendement : ~4,8 % de l'échantillon, ~19,5 % des vivants. C'est suffisant
  pour ajouter quelques buckets, pas pour remplacer WARP.

**Décision** : Phase 1 = importer les 29 validés dans un pool dédié, désactivé
par défaut, avec re-validation à l'import, TTL court, cap 24 et fail-closed.
Phase 2 = scheduler + dashboard + scan complet. Aucun trafic payant, aucun
secret, aucun cookie ne doit passer par ces proxys.

---

## 1. Ce qui existe déjà dans le projet

Le chemin est déjà à moitié construit ; il manque la source et le registre.

| Élément | Rôle | Emplacement |
|---|---|---|
| `PROVIDERS = ("nordvpn","warp","proxies","direct")` | Le provider `proxies` existe déjà | `free/auto_egress.py:12` |
| `Socks5Endpoint` | Proxy statique duck-typé en station | `free/pool.py:257` |
| `HttpsProxyEndpoint` | Proxy HTTPS CONNECT, `proxy_tls_verify = False` | `free/pool.py:410` |
| `set_https_proxies()` | Charge la liste HTTPS statique | `free/pool.py:4113` |
| `_provider_matches()` | `proxies` = tout endpoint non-WARP | `free/pool.py:4302` |
| `egress_providers` | Priorité/choix du provider | `free/pool.py:4640-4698` |
| `https_fallback_enabled` | Repli HTTPS si aucune station Docker | `free/pool.py:4725` |
| API HTTPS proxies | CRUD + test + fallback | `dashboard/api.py:5727` |
| `scripts/test_proxy_pool.py` | Test free 200 sur une liste | dépôt |
| `scripts/test_free_egress.py` | Wire free 2.2.5 exact | dépôt |
| `free/quota_ledger.py` / `free/ip_quota.py` | Bucket par IP, cooldowns | dépôt |

**Manques** : collecte planifiée, registre d'état, scoring, import automatique
dans le pool, matrice multi-modèles, onglet dashboard.

---

## 2. Concept : un proxy public est un bucket jetable, pas une station

### 2.1 Unité de valeur

Le quota free est **par IP de sortie**. L'unité utile n'est donc pas
`host:port` mais **l'egress IP**. Deux endpoints derrière la même IP ne valent
qu'un bucket ; 29 endpoints avec 29 egress valent 29 buckets.

### 2.2 Pipeline

```text
sources publiques
      │  fetch + normalisation + dédup endpoint
      ▼
candidats (host, port, scheme, auth, source)
      │  liveness : GET https://api64.ipify.org via le proxy
      ▼
vivants (egress IP, famille, latence)
      │  gate free : POST capture 2.2.5, Bearer public, 1 modèle
      ▼
validés 200 / 429 / dead / duplicate
      │  registre : score, TTL, cooldown (modèle, IP), quarantaine
      ▼
sélection top-N (egress distincts, cap /24, cap provider)
      │  import atomique
      ▼
FreeIPPool.set_https_proxies(provider="public")
```

### 2.3 États d'un endpoint

| État | Sens | Transition |
|---|---|---|
| `candidate` | vu dans une liste, pas testé | → `alive` / `dead` |
| `alive` | egress résolu | → `validated_200` / `quota_429` |
| `validated_200` | free 200 récent | TTL 12 h, re-test avant import |
| `quota_429` | IP épuisée | TTL 24 h, ne pas retester en boucle |
| `dead` | timeout / refus / TLS | quarantaine 7 j |
| `expired` | TTL dépassé | revalidation ou purge |

### 2.4 Règles non négociables

1. **Jambe free uniquement** : jamais de clé payante, cookie, header
   `x-opencode-*` de compte, ni Authorization autre que `Bearer public`.
2. **TLS destination toujours vérifié** : on ne désactive que la vérification
   du certificat **du proxy** (`PROXY_SSL_VERIFYPEER/HOST`), comme le fait déjà
   `HttpsProxyEndpoint`. Le contenu reste chiffré de bout en bout jusqu'à
   `opencode.ai`.
3. **Aucune donnée utilisateur dans les sondes** : le corps de test est la
   capture synthétique `free/captures/2.2.5.json`.
4. **Fail-closed** : si le pool public n'a aucun endpoint validé, on retombe
   sur WARP/NordVPN/direct selon la config existante ; on n'ouvre pas un
   chemin non prévu.
5. **Cap de concurrence par proxy** : un proxy public n'est pas une station
   dimensionnée ; `max_inflight_per_proxy` bas (4) pour éviter de le brûler.
6. **Pas de rotation immédiate sur 429** : le 429 est un bucket `(modèle, IP)`
   épuisé ; on marque et on choisit un autre egress. Rotation seulement si
   tout le pool est épuisé.

### 2.5 Sécurité : ce que le proxy voit, ce qu'il ne voit pas

**Réponse courte** : avec les invariants ci-dessous, un proxy public ne peut
pas lire les en-têtes, le corps, les prompts, `Authorization` ni les
identifiants `x-opencode-*` — le TLS va de bout en bout jusqu'à
`opencode.ai` et le certificat d'origine est vérifié. En revanche, il voit
**toujours** les métadonnées de transport et peut corréler les sessions.
« Aucune interception » au sens absolu n'existe pas avec un proxy tiers.

| Élément | Visible par le proxy ? | Pourquoi |
|---|---|---|
| IP source de la machine | **Oui** | c'est son client |
| Destination `opencode.ai:443` / SNI | **Oui** | CONNECT / ClientHello |
| Timing, volume, sens du trafic | **Oui** | transport |
| Empreinte TLS du client | **Oui** | ClientHello |
| En-têtes HTTP, corps, prompts | **Non** | TLS de bout en bout |
| `Authorization: Bearer public` | **Non** | dans le TLS |
| `x-opencode-project/session/request`, `b3`, `traceparent` | **Non** | dans le TLS |
| Clé API / cookie / token console | **Non, car absents** | wire anonyme ; à verrouiller |
| Identifiants du proxy (`Proxy-Authorization`) | **Oui** | destinés au proxy ; jamais réutilisés |

#### Invariants à coder (Phase 1)

1. **Wire anonyme forcé** sur le chemin public : `Authorization: Bearer public`,
   suppression de `Cookie`, `x-opencode-org-id`, `x-api-key`, `api-key`.
2. **Fail-closed** si `OPENCODE_ALLOW_CONSOLE_CREDENTIAL` est vrai **et**
   `OPENCODE_CONSOLE_TOKEN` est défini : on refuse d'utiliser un proxy public
   plutôt que de risquer d'envoyer un vrai credential.
3. **TLS destination vérifié** : ne désactiver que `PROXY_SSL_VERIFYPEER/HOST`
   pour le hop proxy. Aucun `verify=False` sur l'origine. Test de
   non-régression avec un proxy qui tente un MITM : la requête doit échouer.
4. **Isolation du processus** : le processus principal contient
   `OPENCODE_API_KEY`, `OPENCODE_GO_AUTH_COOKIE`, etc. Un proxy malveillant qui
   exploite `libcurl` peut lire l'environnement. Le worker d'egress public doit
   tourner avec un env scrubé (aucun secret), sans accès disque en écriture,
   sous un utilisateur non privilégié. À défaut, considérer ce risque comme
   résiduel et ne pas activer le pool public sur la même instance que les clés
   payantes.
5. **Aucun trafic payant** ne doit recevoir un proxy public : test dédié.
6. **Redaction** : ne jamais logger `username:password` d'un proxy ; ne pas
   exposer les credentials dans le dashboard. Préférer les proxys publics sans
   authentification.
7. **DNS** : les gagnants sont HTTPS (CONNECT), donc le proxy résout la
   destination. Pour un SOCKS5, utiliser `socks5h` si l'on veut éviter une
   fuite DNS locale.
8. **Endpoint verrouillé** : sur un proxy non fiable, seuls `https://` et un
   host `opencode.ai` (ou sous-domaine) sont acceptés. Un endpoint HTTP ou un
   host étranger lève avant le POST — c'est la protection contre une erreur de
   routage qui enverrait une clé payante au mauvais endroit.
9. **Option forte** : pin SPKI de `opencode.ai` (avec pin de secours) sur le
   chemin public pour se protéger d'une CA compromise. À évaluer, car cela
   ajoute de la maintenance.

#### Ce que ça ne garantit pas

- **Anonymat vis-à-vis du proxy** : il voit l'IP source et peut lier toutes les
  requêtes qui passent par lui, même si les `ses_` changent. Pour casser ce
  lien, il faut chaîner un premier saut (WARP) avant le proxy public ou changer
  de proxy par session.
- **Confidentialité des métadonnées** : destination, volume, timing, empreinte
  TLS. Si le modèle de menace inclut « le proxy ne doit pas savoir que je parle
  à opencode.ai », un proxy public n'est pas acceptable.
- **Disponibilité** : le proxy peut couper, ralentir ou renvoyer des erreurs.
- **Exploit du client** : un proxy malveillant peut attaquer `libcurl` ou le
  parseur HTTP. L'isolation du point 4 est la seule vraie mitigation.
- **Compromission d'une CA** : sans pin, un certificat `opencode.ai` frauduleux
  délivré par une CA compromise passerait. Probabilité faible, impact élevé.

**Verdict** : on peut garantir **la confidentialité du contenu et l'absence de
secrets applicatifs dans la requête**, pas l'absence de métadonnées ni
l'absence de tout risque d'exploit. La garantie dépend de l'application stricte
des invariants 1–5, pas seulement du plan.

#### État d'implémentation (2026-11-09)

Les invariants 1, 2, 3 et 5 sont maintenant câblés dans le code, pas seulement
documentés :

- `free/public_proxies.py` : `sanitize_free_headers()` supprime tous les
  en-têtes porteurs de secrets et force `Bearer public` sur un proxy non
  fiable ; `assert_untrusted_proxy_safe()` refuse la requête si le credential
  console est activé ou si une valeur secrète d'environnement apparaît dans un
  en-tête ; `assert_secure_endpoint()` refuse un endpoint non HTTPS sur un
  proxy non fiable ; `redact_proxy_url()` masque `user:pass` dans les logs.
- `opencode.py` : `_guard_free_wire_headers()` et
  `_assert_secure_free_endpoint()` sont appelés sur les deux chemins free
  réels — non-stream (`_do_free_request_curl_cffi`) et stream
  (`_open_free_stream`) — juste avant le POST. Le chemin payant/geo
  (`_open_via_pool`) refuse explicitement tout endpoint non fiable : une clé
  payante ne peut pas partir par un proxy public.
- `tests/test_public_proxies_security.py` : 11 tests couvrent la suppression
  des en-têtes secrets, le fail-closed console, la détection d'un secret
  injecté sous un nom custom, l'interdiction d'un endpoint HTTP, la redaction,
  l'interdiction de `verify=False` sur l'origine, et le refus du chemin
  payant/geo.
- `tests/test_public_proxy_mitm.py` : test vivant — un proxy qui répond 200 au
  CONNECT puis présente un certificat auto-signé pour `opencode.ai` est rejeté
  (`CertificateVerifyError`). C'est la preuve que le proxy ne peut pas lire le
  contenu.
- `free/public_egress_worker.py` : proxy CONNECT local isolé. Le processus
  principal ne parle qu'à `127.0.0.1` ; le worker relaie vers le proxy public
  (HTTP/HTTPS/SOCKS5), avec token local, allowlist `opencode.ai:443`, et
  environnement scrubbé (`worker_env()`). Un exploit du proxy public ne voit
  que les URLs publiques, pas les clés du proxy principal.
- `tests/test_public_egress_worker.py` : 7 tests — relais HTTP, relais HTTPS
  (certificat auto-signé), mauvais token, cible interdite, env scrubbé, et
  lancement réel du sous-processus.
- `free/pool.py` : `Socks5Endpoint`/`HttpsProxyEndpoint` acceptent un
  `transport_url` local ; `set_https_proxies()` préserve désormais
  `transport_url`, `provider`, `label`, `egress_ip`, `block` et `ipv6`.
- `free/pool.py` : `set_public_proxies()` sélectionne le feed (dédup egress,
  cap `/24`, cap `max_active`), démarre le worker **avant** le swap, publie des
  lignes `provider="public"` avec `transport_url`, et arrête l'ancien worker
  avec 300 s de grâce. `configure_public_proxies()` applique le bloc
  `ip_rotation.public_proxies` ; `close()` arrête le worker.
- `config.yaml` : bloc `public_proxies` ajouté, `enabled: false` par défaut.
- **Smoke réel du worker** : upstream `https://37.203.35.42:443`,
  `GET https://api64.ipify.org` → `200` egress `37.203.35.42` ; puis
  `scripts/test_proxy_pool.py` via l'URL locale du worker →
  `deepseek-v4.1-flash-free` **200** en 1,6 s. Le script de test n'a parlé
  qu'à `127.0.0.1` ; le proxy public n'a jamais vu le processus principal.
- **Smoke pool réel** : feed `logs/proxy_lists/working.jsonl`, 24 lignes
  actives, worker sur port local, `close()` propre.
- `free/public_egress_container.py` : mode d'isolation **conteneur** opt-in.
  Le conteneur ne monte que le script worker et `/config` (URLs publiques),
  jamais le dépôt ni `.env` ; `--read-only`, `--cap-drop=ALL`,
  `--security-opt=no-new-privileges`, `--user=65534:65534`, limites
  PID/mémoire/CPU, port publié sur `127.0.0.1` uniquement.
- `config.yaml` : `isolation: process` par défaut, `container_image:
  python:3.12-slim`.
- **Smoke conteneur réel** : `start_worker_container` avec
  `37.203.35.42:443` → `GET https://api64.ipify.org` **200**, egress
  `37.203.35.42` ; test free via le conteneur → réponse amont valide (429
  quota, le tunnel TLS fonctionne) ; pool `isolation=container` → 1 ligne
  active, `close()` propre.

- **API** : `GET /api/vpn/public-proxies` (état read-only, sans
  `transport_url` ni token) et `POST /api/vpn/public-proxies/refresh`
  (protégé par `DASHBOARD_TOKEN`, applique `enabled/feed_path/max_active/
  max_per_24/isolation/container_image` et recharge le worker).
- **Scheduler** : `public_proxies.refresh_interval_s` (défaut 21600) relance
  la revalidation en tâche asyncio ; `close()` annule la tâche. Le validateur
  free réel (`opencode._revalidate_public_proxies`, capture 2.2.5, wire Bun)
  re-teste chaque endpoint **via le worker isolé** : 200 et 429 sont conservés
  (429 = bucket épuisé), timeout/403/5xx sont retirés. Si tout est mort, le
  pool courant est conservé. Le rechargement du feed reste manuel/API.
- **UI Svelte** : onglet « Proxys publics » (`PublicProxies.svelte`),
  i18n EN/FR, activation/désactivation, rechargement du feed, état du worker
  et tableau des egress. Le bundle `static/dist` est reconstruit ; l'UI ne
  reçoit jamais `transport_url` ni le token local.

**Limite actuelle** : le pool démarre/arrête le worker, charge le feed,
revalide périodiquement via le wire free réel, expose une API, un bouton de
revalidation et un onglet dashboard. L'image conteneur est pinnée par digest
dans `config.yaml`.

**Activation du 2026-11-09** : `public_proxies.enabled: true`, feed
`working_200.jsonl` (2 endpoints), isolation `container`, `max_active: 2`,
part de trafic `proxies: 5 %` (`warp 45 / nordvpn 50`). Le worker conteneur est
actif, les deux endpoints répondent 200 à la revalidation via le worker. Le
pool est donc en production locale, avec une part volontairement faible.

**Preuve client 2026-11-09** : avant le filtre de capacité réelle, un chat
`deepseek-v4.1-flash-free` renvoyait `429 Free quota exhausted on all VPN
stations` alors que le proxy public était sain. Après ajout de
`_provider_has_free_bucket()` (bucket modèle/IP réellement libre) et du
préflight multi-provider, le même chat répond **200 `pong`** via le proxy
public pendant que WARP/NordVPN sont en 429. Le journal adaptatif enregistre
les 429 par provider et réduit leur part au profit des providers sains.

Deux modes d'isolation :

- `process` (défaut) : env scrubé + `cwd` temporaire, mais même utilisateur ;
  un exploit code-exec pourrait encore lire des fichiers du projet.
- `container` : pas de montage du dépôt, pas d'env, non-root, read-only,
  cap-drop, limites — c'est le mode à utiliser pour la garantie maximale.

Le TLS garantit déjà que le proxy ne voit pas le contenu ; le conteneur ferme
le risque d'exploit du processus worker.

---

## 3. Résultats de la Phase 0 (mesures réelles du 2026-11-09)

### 3.1 Collecte

`scripts/fetch_proxy_lists.py` — 23 sources, 1,2 s, snapshot reproductible.

| Métrique | Valeur |
|---|---:|
| Sources OK | 23 / 23 |
| Endpoints uniques | **20 045** |
| Hôtes uniques | **10 994** |
| HTTP | 5 972 |
| HTTPS | 1 259 |
| SOCKS5 | 12 814 |
| Lignes brutes traitées | ~45 000 |

Top sources par endpoints uniques ajoutés :

| Source | Lignes | Ajoutés | Doublons |
|---|---:|---:|---:|
| `proxifly_socks5` | 5 164 | 4 666 | 498 |
| `aliilapro_socks5` | 3 757 | 3 078 | 679 |
| `thespeedx_http` | 2 914 | 2 914 | 0 |
| `thespeedx_socks5` | 2 668 | 2 668 | 0 |
| `proxifly_http` | 4 616 | 1 836 | 2 780 |
| `proxyscrape_socks5` | 2 000 | 1 001 | 999 |
| `proxifly_https` | 897 | 897 | 0 |
| `zloi_socks5` | 766 | 666 | 100 |
| `sunny_http` | 1 738 | 491 | 1 247 |
| `geonode_socks5` | 1 164 | 417 | 747 |

Les listes se recouvrent massivement : `hookzof` (5 164) et `proxifly_socks5`
sont identiques ; `zloi_https` et `proxifly_https` sont identiques. La valeur
vient de la **déduplication par egress**, pas du nombre de lignes.

### 3.2 Liveness

`scripts/probe_proxy_pool.py --per-scheme 200` — 600 testés.

| Schéma | Testés | Vivants | % | Egress uniques |
|---|---:|---:|---:|---:|
| HTTP | 200 | 5 | 2,5 % | 5 |
| HTTPS | 200 | 132 | 66,0 % | 120 |
| SOCKS5 | 200 | 12 | 6,0 % | 8 |
| **Total** | **600** | **149** | **24,8 %** | **133** |

Le taux HTTPS est gonflé par des fermes de proxys publics déjà connues
(beaucoup de `:443`), mais ce sont aussi les seuls à produire des 200.

### 3.3 Gate free

`scripts/test_proxy_pool.py --proxies-file logs/proxy_lists/alive.txt` —
149 endpoints vivants.

| Statut | Nombre |
|---|---:|
| **200** | **29** |
| 429 `FreeUsageLimitError` | 86 |
| dead (timeout/refus) | 18 |
| timeout côté test | 5 |
| duplicate (même egress) | 11 |

Les 29 gagnants sont **tous HTTPS**, avec **29 egress distincts**. Latence
des 200 : ~1,4 s à ~6,6 s (p50 ~2,3 s). Liste :
`logs/proxy_lists/working.txt` et `working.jsonl`.

**Piège de mesure important** : un premier run avec vérification TLS du proxy
activée donnait **0/149**. Le code de prod désactive déjà cette vérification
pour les proxys HTTPS (`HttpsProxyEndpoint.proxy_tls_verify = False`). Les
scripts `test_proxy_pool.py` et `test_free_egress.py` ont été alignés ; sans
cela, on jette 29 proxys valides en les croyant morts.

### 3.4 Le 429 est un bucket, pas une tombe

30 des 86 endpoints 429 ont été re-testés avec `glm-5.3-flash-free` :
**30/30 429**. Conclusion opérationnelle : ne pas dépenser de requêtes à
re-tester chaque modèle sur une IP 429 ; marquer le bucket et passer au
suivant. Le registre garde quand même le modèle et le `Retry-After` pour
réouvrir au reset.

### 3.5 Stabilité du seed

Les 29 validés ont été re-testés une seconde fois, quelques minutes plus tard :
**28/29 répondent encore 200**, 1 est passé 429. Latence 1,4–8,1 s. C'est
suffisant pour un seed, mais confirme qu'il faut re-tester à l'import et
garder un TTL court — un endpoint public peut basculer en 429 ou mourir en
quelques minutes.

**Revalidation du 2026-11-09 (fin de journée)** : sur les 29, **2 sont encore
200** (`81.172.178.145:8443`, `89.124.8.77:443`) et **27 sont 429**. Le feed
`working.jsonl` a été mis à jour avec `last_status`/`last_checked_at` ;
`working_200.jsonl`/`working_200.txt` contiennent le seed immédiat à 2
endpoints. Pour une activation immédiate, pointer `feed_path` sur
`working_200.jsonl` ; pour laisser les 27 buckets se réinitialiser, garder
`working.jsonl` et laisser le scheduler revalider.

---

## 4. Architecture cible

### 4.1 Nouveau bloc `ip_rotation.public_proxies`

```yaml
ip_rotation:
  # Le provider `proxies` n'est activé qu'après import d'au moins un validé.
  egress_providers: [warp, proxies, nordvpn]
  egress_strategy: fallback        # ou balance avec des poids
  egress_weights:
    warp: 80
    proxies: 15
    nordvpn: 5
  public_proxies:
    enabled: false                 # opt-in explicite
    feed_path: logs/proxy_lists/working.jsonl
    refresh_interval_s: 21600      # 6 h
    max_active: 24                 # cap dur
    max_per_24: 2                  # diversité /24
    probe_timeout_s: 6
    probe_concurrency: 80
    free_probe_concurrency: 8
    free_probe_model: deepseek-v4.1-flash-free
    ttl_200_s: 43200               # 12 h
    ttl_429_s: 86400               # 24 h
    ttl_dead_s: 604800             # 7 j
    min_success_rate: 0.5
    max_inflight_per_proxy: 4
```

Notes :

- `egress_providers` est déjà le sélecteur de priorité
  (`free/pool.py:4640-4698`). `fallback` = premier provider disponible dans
  l'ordre ; `balance` = smooth weighted round-robin.
- `proxies` doit rester **après** `warp` par défaut : le pool public est un
  appoint, pas le socle.
- `public_proxies.enabled: false` au départ. L'import des 29 validés se fait
  manuellement ou via l'API, puis on active.

### 4.2 Stockage et mapping vers le pool

Les gagnants sont des proxys HTTPS CONNECT. Le stockage naturel est
`https_proxies`, mais `set_https_proxies()` ne conserve aujourd'hui que
`host/port/username/password/enabled` (`free/pool.py:4113`). Il faut
l'étendre pour préserver les métadonnées déjà supportées par
`HttpsProxyEndpoint` :

```python
{
    "host": "37.203.35.42",
    "port": 443,
    "username": None,
    "password": None,
    "enabled": True,
    "label": "public:37.203.35.42",
    "ipv6": False,
    "egress_ip": "37.203.35.42",
    "managed": False,
    "container": None,
    "image": None,
    "block": "37.203.35.0/24",
    "provider": "public",
}
```

Cela évite un `refresh_egress()` au premier appel et permet au ledger de
keyer directement sur la bonne IP. `_provider_matches("proxies")` accepte
`provider != "warp"`, donc `public` est bien sélectionné comme `proxies`.

### 4.3 Registre

Phase 1 : `logs/proxy_lists/registry.jsonl` + écriture atomique + verrou
fichier. Champs :

```json
{
  "url": "https://37.203.35.42:443",
  "scheme": "https",
  "host": "37.203.35.42",
  "port": 443,
  "egress_ip": "37.203.35.42",
  "family": "ipv4",
  "block": "37.203.35.0/24",
  "sources": ["proxifly_https", "zloi_https"],
  "status": "validated_200",
  "model": "deepseek-v4.1-flash-free",
  "latency_ms": 1437,
  "success_count": 1,
  "fail_count": 0,
  "last_ok_at": "2026-11-09T...Z",
  "expires_at": "2026-11-09T...Z",
  "score": 0.93
}
```

Phase 2 : migration vers `logs/registry.db` (SQLite, WAL) si le scheduler et
le dashboard doivent écrire en concurrence. Pas bloquant pour la Phase 1.

### 4.4 Sélection

1. Filtrer `enabled`, non expiré, egress non vide, statut != `dead`.
2. Dédupliquer par `egress_ip` (garder le meilleur score).
3. Scorer : `0.5 * success_rate + 0.3 * latency_score + 0.2 * freshness`,
   avec un prior de confiance par source.
4. Prendre les `max_active` meilleurs, avec cap `max_per_24` et cap par ASN
   (optionnel).
5. Swap atomique dans le pool ; garder la liste précédente pour rollback.
6. Ne jamais importer un endpoint non re-testé dans les dernières 12 h.

### 4.5 Poids adaptatifs selon les 429

Les poids `egress_weights` restent la base. `free/adaptive_weights.py` les
module à chaque sélection (`balance`) :

- les 429 **récents** (fenêtre `egress_adaptive.window_s`, 300 s par défaut)
  réduisent la part du provider concerné ;
- un provider dont **tous** les buckets connus sont bloqués tombe à 0 ;
- le reste est renormalisé à 100, donc la part perdue est redistribuée aux
  providers sains ;
- un provider qui récupère retrouve sa part dès que ses 429 sortent de la
  fenêtre — pas de pénalité historique permanente.

Config :

```yaml
ip_rotation:
  egress_adaptive:
    enabled: true
    window_s: 300
    min_samples: 3
    recent_429_penalty: 0.25
    min_weight_pct: 2
```

Limite honnête : si **tous** les buckets (WARP, NordVPN, publics) sont à 429,
aucun poids ne peut produire un 200 ; le client reçoit alors un 429 avec
`Retry-After`. L'adaptatif évite les 429 évitables quand au moins un bucket
sain existe.

### 4.6 Cycle de vie

- **Refresh** : toutes les 6 h, jitter, hors heures de pointe.
- **Revalidation** : avant chaque import ; 1 requête free suffit.
- **429** : `(model, egress)` en cooldown jusqu'au `Retry-After` (souvent
  ~15 h) ; ne pas re-tester en boucle.
- **3 échecs consécutifs** (timeout/refus) : quarantaine 7 j.
- **Disparition** : TTL `dead` puis purge ; le registre garde l'historique
  pour le scoring des sources.

---

## 5. Implémentation Phase 1 (prochaine étape, ~0,5–1 jour)

| # | Tâche | Fichier | Critère de done |
|---|---|---|---|
| 1.1 | Module pur `public_proxies` (dataclass, parse, score, TTL, cap /24) | `free/public_proxies.py` | tests unitaires verts |
| 1.2 | Étendre `set_https_proxies()` aux métadonnées | `free/pool.py` | `provider`, `label`, `egress_ip`, `block` conservés |
| 1.3 | Charger `public_proxies` dans `update_config()` | `free/pool.py` | fusion `https_proxies` + public, opt-in |
| 1.4 | Config par défaut désactivée | `config.yaml` | `enabled: false`, config_audit 0 dead |
| 1.5 | Import du seed validé | `scripts/import_public_proxies.py` | 29 lignes, re-test 200, cap 24 |
| 1.6 | API minimale | `dashboard/api.py` | GET liste + POST refresh/import |
| 1.7 | Tests de non-régression | `tests/test_public_proxies*.py` | gate `pytest -k "not docker"` |
| 1.8 | Smoke live | commandes §8 | 3/3 200 sur le seed, 0 payant |
| 1.9 | Garde wire anonyme + fail-closed console | `free/public_proxies.py` | `Cookie`/`x-api-key` absents, refus si credential console |
| 1.10 | Test MITM/TLS destination | `tests/test_public_proxies_security.py` | un proxy qui présente un faux cert fait échouer la requête |
| 1.11 | Redaction credentials proxy | logs + dashboard | aucun `user:pass` dans les logs/API |
| 1.12 | Isolation worker public | `free/public_egress_worker.py`, `free/public_egress_container.py` | modes process + conteneur faits, câblés via `public_proxies.isolation` |

**Seed immédiat** : `logs/proxy_lists/working.jsonl` (29 endpoints, 29 egress).
Ne pas l'importer tel quel sans re-test : un proxy public peut mourir entre
deux runs.

---

## 6. Phase 2 — scheduler et dashboard (~1–2 jours)

1. **Scheduler** `free/proxy_scheduler.py` :
   - boucle asyncio, intervalle configurable, jitter ;
   - fetch → probe → gate → registre → sélection → `set_https_proxies` ;
   - jamais dans le chemin de requête ; swap atomique ;
   - en cas d'échec, conserver la dernière liste valide.
2. **Dashboard** onglet « Proxies publics » :
   - compteurs : collectés, vivants, 200, 429, dead, egress uniques ;
   - tableau : endpoint, egress, statut, latence, source, dernier test ;
   - boutons : Refresh, Re-test, Importer, Activer/Désactiver ;
   - bandeau : « provider d'appoint, cap 24, fail-closed ».
3. **API** :
   - `GET /api/vpn/public-proxies`
   - `POST /api/vpn/public-proxies/refresh` → 202 + job id
   - `POST /api/vpn/public-proxies/import` → re-test + swap
   - `POST /api/vpn/public-proxies/toggle`
4. **Observabilité** :
   - `/api/free-health` : `public_proxies: {active, alive, validated, egress}` ;
   - logs : `[public-proxies] harvested=... alive=... validated=... imported=...` ;
   - alerte si `active < 4` ou si `validated_pct < 10 %`.

---

## 7. Phase 3 — qualité et durabilité (optionnel)

- Scan complet des 20 045 en batches (liveness puis gate), de nuit.
- Score par source : garder les sources qui produisent des 200, couper les
  listes qui ne produisent que des doublons ou des 429.
- Diversité : cap par /24, par ASN, par pays ; éviter 10 buckets dans le même
  /24.
- Re-test des 429 au reset (`Retry-After`) plutôt que rotation immédiate.
- Comparaison A/B : `proxies` en fallback vs balance 10–20 %.
- Si le rendement s'effondre (< 10 validés), désactiver le provider sans
  toucher WARP.

---

## 8. Commandes et artefacts

```powershell
# 1. Collecte (23 sources → logs/proxy_lists/)
python scripts/fetch_proxy_lists.py --concurrency 16 --timeout 30

# 2. Liveness sur un échantillon (0 = tout, plus long)
python scripts/probe_proxy_pool.py --per-scheme 200 --concurrency 80 --timeout 6

# 3. Gate free sur les vivants
python scripts/test_proxy_pool.py --proxies-file logs/proxy_lists/alive.txt `
  --limit 149 --concurrency 16 --timeout 20 `
  --output logs/proxy_lists/free_probe.json

# 4. Scan complet (nuit)
python scripts/probe_proxy_pool.py --per-scheme 0 --concurrency 120 --timeout 5
python scripts/test_proxy_pool.py --proxies-file logs/proxy_lists/alive.txt `
  --limit 5000 --concurrency 24 --timeout 15
```

Artefacts locaux (non versionnés, reproductibles) :

| Fichier | Contenu |
|---|---|
| `logs/proxy_lists/pool.jsonl` | 20 045 endpoints normalisés |
| `logs/proxy_lists/pool.txt` | URLs `scheme://host:port` |
| `logs/proxy_lists/summary.json` | stats par source |
| `logs/proxy_lists/probe.json` | 149 vivants + egress |
| `logs/proxy_lists/free_probe_tls.json` | 29 × 200, 86 × 429, etc. |
| `logs/proxy_lists/working.jsonl` / `.txt` | 29 endpoints validés |
| `logs/proxy_lists/raw/` | réponses brutes des 23 sources |

---

## 9. Tests et gates

- **Unitaires** : `normalize_line` (plain, scheme, auth, IPv6 `[::1]:8080`,
  `host:port:country` de zloi, IP privée, port invalide, socks4 ignoré),
  dédup par egress, TTL, score, cap /24.
- **TLS** : un proxy HTTPS avec certificat auto-signé doit passer la sonde
  *proxy* mais échouer si le certificat **destination** est invalide. Vérifier
  que `PROXY_SSL_VERIFYPEER/HOST` ne sont désactivés que pour le hop proxy.
- **Intégration** : `set_https_proxies` conserve `provider/label/egress_ip` ;
  `_provider_matches("proxies")` sélectionne `public` ; aucun endpoint public
  n'est choisi pour une requête payante.
- **Non-régression** : `pytest -k "not docker"`, `ruff check`, 
  `python scripts/config_audit.py --strict`, smoke `/api/vpn-status`.
- **Live** : 3 endpoints du seed, 3 × 200, p50 < 5 s ; puis un run avec
  `public_proxies.enabled: false` pour vérifier que WARP reste seul.

---

## 10. Risques et mitigation

| Risque | Probabilité | Impact | Mitigation |
|---|---|---|---|
| Listes périmées / proxys morts | Élevée | Pool vide | TTL court, re-test à l'import, dernière liste valide conservée |
| IP déjà 429 | Élevée (86/149) | Aucun gain | Gate 200 avant import, cooldown par `(model, IP)` |
| Proxy malveillant / MITM | Moyenne | Fuite de données | TLS destination vérifié, `Bearer public`, aucun cookie/clé, corps synthétique |
| Exploit du client HTTP via proxy malveillant | Faible | Lecture de l'env (clés payantes) | isolation worker/env scrubé, mises à jour curl_cffi, moindre privilège |
| Latence élevée | Moyenne | UX dégradée | Score TTFB, cap 24, provider d'appoint, fallback WARP |
| Churn / disparition | Élevée | Buckets instables | TTL, quarantaine, refresh 6 h, ne pas dépendre du pool |
| Blocage sous-réseau | Moyenne | 10 buckets perdus | Cap /24, diversité ASN/pays |
| Complexité / régression pool | Moyenne | Stations cassées | module pur + tests, opt-in, rollback config, aucune modif du chemin payant |
| CGU / réputation | Faible | Compte free | trafic free uniquement, pas de compte, pas de clé |

---

## 11. Critères de succès

**Phase 1** :

- 20–24 endpoints publics actifs, tous re-testés 200 dans les 12 h ;
- 100 % des requêtes publiques en `Bearer public`, 0 secret ;
- 0 requête payante routée par un proxy public ;
- p50 free public < 5 s, p95 < 10 s ;
- WARP 6/6 inchangé quand `public_proxies.enabled: false` ;
- `pytest -k "not docker"` + `config_audit --strict` verts.

**Phase 2** :

- refresh automatique toutes les 6 h, sans trou de service ;
- ≥ 50 egress validés sur un scan complet ;
- auto-quarantaine après 3 échecs ;
- dashboard honnête : `active`, `alive`, `validated`, `429`, `dead`.

---

## 12. Ce qu'il ne faut pas faire

- Remplacer WARP par les proxys publics : le rendement est trop faible et
  instable.
- Importer les 897 lignes de `proxifly_https` sans gate 200 : 86/149 vivants
  sont déjà 429.
- Désactiver la vérification TLS **destination** : on perd la protection
  contre un proxy MITM.
- Envoyer une clé, un cookie ou un header de compte dans une sonde.
- Rotationner sur chaque 429 : c'est un bucket épuisé, pas un proxy mort.
- Compter les `host:port` comme des buckets : seul l'egress IP compte.
- Laisser `public_proxies.enabled: true` sans surveillance : un pool public
  se vide en quelques heures.

---

## 13. Décision demandée

Valider la Phase 1 avec :

1. **Cap 24** endpoints actifs, **2 max par /24**, provider `proxies` en
   **fallback après WARP** (poids 0 en balance au départ) ;
2. **Seed = 29 validés** re-testés à l'import, `enabled: false` tant que le
   re-test n'est pas vert ;
3. **Pas de scan complet** tout de suite : d'abord le seed, puis un scan de
   nuit si le seed tient 24 h.

Après accord, j'implémente `free/public_proxies.py`, l'extension de
`set_https_proxies`, l'import du seed et les tests.

---

## 14. Mise à jour 2026-11-09 — catalogue d'egress (013)

Le plan one-shot ci-dessus est remplacé par le catalogue provider-agnostique
`specs/013-egress-catalog/` :

- découverte publique par sources déclaratives, curseur persistant et budget
  de 500 candidats par cycle ; les 429 ne sont pas re-testés avant `reset_at` ;
- WARP : chaque `/64` observé est validé par le vrai gate free et historisé ;
  preuve live du 2026-11-09 : **12 blocs `/64` usable** sur 13 uniques ;
- NordVPN : les 6 IP sont suivies ; les buckets journaliers épuisés restent
  `quota`, jamais `dead` ;
- le pool public n'est alimenté que par des options `usable` ; un catalogue
  vide conserve le pool précédent ;
- `scripts/autoscale_max_ips.py` est archivé dans `scripts/archive/` et n'a
  plus de mode `--loop` : le scheduler du catalogue est la seule
  automatisation ;
- gates : `pytest -k "not docker"`, `config_audit --strict`,
  `platform_audit --strict`, `test_option_map.py`, `npm run check`.

Le cap actif reste borné (64 publics, 12 WARP utilisables, 6 sessions
NordVPN) ; le catalogue conserve toutes les options connues pour la rotation.

