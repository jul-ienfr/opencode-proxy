# Quota free, identité et reset : ce qui identifie réellement un client

**Analyse + implémentation (2026-11-01).**

Ce document répond à une question précise : parmi tout ce qu'un client envoie
(Bun, SDK, TLS, en-têtes, corps, IP, compte…), quel élément le provider utilise
pour **attribuer le quota free** et **calculer l'heure de reset** ? Et que
faudrait-il changer pour obtenir un quota qui soit réellement « le mien » ?

La réponse tient en une phrase :

> En free, le provider ne connaît pas « moi ». Il connaît une **IP d'egress**
> (IPv6 tronquée au /64). Le quota est un compteur `(ip, interval)` dans
> `ip_rate_limit`. Le reset est **propre à chaque IP** : la mesure live du
> 2026-11-01 donne des resets entre **00:01 et 00:53 UTC** (pas un minuit
> unique). Les 18 h / 19 h / 24 h sont le temps restant jusqu'au reset de
> **cette IP**, pas une identité ni un reset global.

> **Correction 2026-11-01** : le code public `getRetryAfterDay` calcule un
> minuit UTC unique, mais la production mesurée ne se comporte pas ainsi
> (resets par IP espacés de ~1 h, stables pour une même IP). Voir
> [`docs/free-429-investigation.md`](free-429-investigation.md) pour les
> preuves, le replay exact du client officiel et les tests ASN/modèles.

Tout le reste — Bun, JA3, UA, `x-opencode-project`, `x-opencode-session`,
`x-opencode-request`, `x-opencode-client`, le corps, les outils — est au mieux
journalisé, au pire supprimé avant l'appel au modèle. Aucun de ces champs ne
crée de bucket de quota.

---

## 2. Preuve par le code

Références : commit public `47f33329` du dépôt `sst/opencode` (les chemins
correspondent au console gateway `opencode.ai`). Le comportement live mesuré
ci-dessous correspond exactement à ce code.

### 2.1 Le seul point d'entrée : `x-real-ip`

`packages/console/app/src/routes/zen/util/handler.ts` :

```ts
const rawIp = input.request.headers.get("x-real-ip") ?? ""
const ip = rawIp.includes(":") ? rawIp.split(":").slice(0, 4).join(":") : rawIp
const rawZenApiKey = opts.parseApiKey(input.request.headers)
const zenApiKey = rawZenApiKey === "public" ? undefined : rawZenApiKey
const sessionId = input.request.headers.get("x-opencode-session") ?? ""
const requestId = input.request.headers.get("x-opencode-request") ?? ""
const ocClient = input.request.headers.get("x-opencode-client") ?? ""
const userAgent = input.request.headers.get("user-agent") ?? ""
```

Points décisifs :

1. **`x-real-ip` est la seule source de l'IP.** Si l'en-tête est absent, l'IP
   vaut `"unknown"` : tous les clients sans en-tête partagent alors un bucket
   commun. En production Cloudflare fournit l'en-tête ; un client ne peut pas
   le choisir (testé plus bas).
2. **IPv6 est tronquée aux 4 premiers groupes**, c'est-à-dire au **/64**.
   `2001:db8:abcd:1234:5678:9abc:def0:1111` → `2001:db8:abcd:1234`.
   Changer l'interface ID dans le même /64 ne change **pas** le bucket.
3. **`Authorization: Bearer public` est converti en `undefined`.** Il n'y a
   donc ni clé API, ni compte, ni workspace dans la jambe free.
4. `sessionId`, `requestId`, `ocClient`, `userAgent` sont lus **uniquement
   pour les métriques** (`logger.metric`).

### 2.2 Le limiteur free : `ip_rate_limit`

`packages/console/app/src/routes/zen/util/handler.ts` :

```ts
const rateLimiter = modelInfo.allowAnonymous
  ? createIpRateLimiter(modelInfo.id, modelInfo.rateLimit, ip, input.request)
  : createKeyRateLimiter(modelInfo.id, modelInfo.rateLimit, zenApiKey, input.request)
await rateLimiter?.check()
```

Autrement dit :

- modèle `allowAnonymous` (free) → **limiteur par IP** ;
- modèle payant → **limiteur par clé API**.

`packages/console/app/src/routes/zen/util/ipRateLimiter.ts` :

```ts
const limits = Subscription.getFreeLimits()
const headersExist = true // checkHeaders temporairement désactivé
const dailyLimit = !headersExist ? limits.dailyRequestsFallback : (rateLimit ?? limits.dailyRequests)
const isDefaultModel = headersExist && !rateLimit

const ip = !rawIp.length ? "unknown" : rawIp
const now = Date.now()
const lifetimeInterval = ""
const dailyInterval = rateLimit
  ? `${buildYYYYMMDD(now)}${modelId.substring(0, 2)}`
  : buildYYYYMMDD(now)
```

Le schéma `packages/console/core/src/schema/ip.sql.ts` :

```ts
export const IpRateLimitTable = mysqlTable(
  "ip_rate_limit",
  {
    ip: varchar("ip", { length: 45 }).notNull(),
    interval: varchar("interval", { length: 10 }).notNull(),
    count: int("count").notNull(),
  },
  (table) => [primaryKey({ columns: [table.ip, table.interval] })],
)
```

Conséquences :

- La clé primaire est **`(ip, interval)`**. Il n'y a ni `session`, ni
  `project`, ni `user`, ni `workspace` dans cette table.
- `interval` vaut `YYYYMMDD` (8 chars) pour un modèle « default », ou
  `YYYYMMDD` + les **2 premiers caractères du model id** (10 chars) quand le
  modèle a un `rateLimit` explicite.
  - `deepseek-v4.1-flash-free` → préfixe `de` ;
  - `mimo-v2.6-flash-free` → préfixe `mi` ;
  - `flint-40b-free` → préfixe `fl` ;
  - deux modèles `nemotron-*` partagent le préfixe `ne`.
- Pour un modèle default, `lifetimeInterval = ""` : un second compteur
  **lifetime par IP** est tenu, mais il ne bloque jamais directement. Il sert
  seulement à `_isNew` (voir 2.3).
- Le nombre exact de requêtes (`dailyRequests`) provient de
  `Resource.ZEN_LIMITS` (valeur de déploiement, non visible dans le code
  applicatif public). Les rapports communautaires parlent d'environ 200
  requêtes/jour ; le code peut doubler ce nombre pour une IP « neuve »
  (voir 2.3).

### 2.3 Le reset : `getRetryAfterDay`

Toujours dans `ipRateLimiter.ts` :

```ts
_isNew = isDefaultModel && lifetimeCount < dailyLimit * 7

if ((_isNew && dailyCount >= dailyLimit * 2) || (!_isNew && dailyCount >= dailyLimit))
  throw new FreeUsageLimitError(dict["zen.api.error.rateLimitExceeded"], getRetryAfterDay(now))
```

```ts
export function getRetryAfterDay(now: number) {
  return Math.ceil((86_400_000 - (now % 86_400_000)) / 1000)
}
```

`now` est un timestamp Unix en millisecondes. `now % 86_400_000` est le temps
écoulé depuis **00:00 UTC**. Le code public calcule donc :

> **le nombre de secondes restantes jusqu'au prochain minuit UTC.**

Mais la **mesure live 2026-11-01** ne suit pas cette formule à la lettre : sur
cinq IPs différentes, `date + Retry-After` donne des resets entre **00:01:34**
et **00:53:10 UTC**, stables pour une même IP. Le code de production a donc
changé (fenêtre 24 h glissante, offset ou jitter par IP), ou le `Retry-After`
inclut un décalage par IP. La seule source de vérité est le `Retry-After`
live ; le ledger stocke `reset_at = now + Retry-After` **sans snap** sur
minuit. Voir [`docs/free-429-investigation.md`](free-429-investigation.md) §3.

`_isNew` mérite une note : une IP dont le compteur lifetime est inférieur à
`dailyLimit * 7` est considérée comme « nouvelle » et a droit à **2× la limite
quotidienne**. Après 7 jours d'usage (lifetime ≥ 7×daily), elle repasse à 1×.
Ce bonus est lui aussi **par IP**, pas par utilisateur.

### 2.4 Ce que le handler fait des en-têtes d'identité

Toujours dans `handler.ts`, avant l'appel au provider :

```ts
const stickyId = sessionId ? sessionId : (authInfo?.workspaceID ?? ip)
const stickyTracker = createStickyTracker(modelInfo.id, modelInfo.stickyProvider, stickyId)
```

Puis, au moment de forwarder :

```ts
headers.delete("host")
headers.delete("content-length")
headers.delete("x-opencode-request")
headers.delete("x-opencode-session")
headers.delete("x-opencode-project")
headers.delete("x-opencode-client")
```

Donc :

- `x-opencode-session` sert au **sticky provider** (et, pour un compte payant,
  à la colonne `sessionID` de `UsageTable`) ;
- `x-opencode-request` et `x-opencode-client` ne servent qu'aux métriques ;
- **`x-opencode-project` n'est même pas lu** par le handler. Il est supprimé
  avant l'appel au modèle ;
- aucun `x-opencode-*` n'atteint le provider du modèle. Ces en-têtes sont une
  convention **client ↔ console gateway**, pas une identité upstream.

Le `track()` du limiteur est appelé **après la réponse du provider**, y compris
pour un non-200 :

```ts
const json = await res.json()
await rateLimiter?.track()
```

Conséquence pratique : une requête qui passe le `check()` puis échoue chez le
provider (400, 429 upstream, 5xx) **consomme quand même** une unité du quota
IP. Les retries agressifs et le fan-out brûlent le budget.

### 2.5 Le client officiel : ce qu'il envoie vraiment

Extrait du bundle 1.19.5 installé sur cette machine (`opencode.exe`, chaîne
`@opencode/LLM`) :

```js
let E = e.model.providerID.startsWith("opencode") ? (yield* gt).project.id : void 0;
return {
  ...
  headers: {
    "x-opencode-session-id": e.sessionID,
    ...(e.parentSessionID ? { "x-opencode-parent-session-id": e.parentSessionID } : {}),
    ...(e.model.providerID.startsWith("opencode")
      ? {
          ...(E ? { "x-opencode-project": E } : {}),
          "x-opencode-session": e.sessionID,
          "x-opencode-request": e.user.id,
          "x-opencode-client": e.flags.client,
          "User-Agent": hr,
        }
      : {
          "x-session-affinity": e.sessionID,
          "X-Session-Id": e.sessionID,
          "User-Agent": hr,
        }),
    ...(e.parentSessionID ? { "x-parent-session-id": e.parentSessionID } : {}),
    ...e.model.headers,
    ...D,
  },
}
```

Et sur le wire (capture mitm du 2026-11-01, 1.19.5, client `desktop`) :

```text
Authorization: Bearer public
Content-Type: application/json
User-Agent: opencode/1.19.5 ai-sdk/provider-utils/4.0.23 runtime/bun/1.4.4
x-opencode-client: desktop
x-opencode-project: global
x-opencode-request: msg_195974961001XQe8Tldyf1iiJb
x-opencode-session: ses_e6a68b6bafferOd9gHMxsU3upU
x-opencode-session-id: ses_e6a68b6bafferOd9gHMxsU3upU
Connection: keep-alive
Accept: */*
Host: opencode.ai
Accept-Encoding: gzip, deflate, br, zstd
Content-Length: 2477
```

Aucun cookie. Aucun `x-opencode-machine-id`, `x-device-id`, `x-install-id`.
Le `machineId` présent dans le binaire (package `node-machine-id`) n'est **pas**
envoyé sur l'inférence.

`project.id` vaut `global` quand le client tourne hors d'un dépôt git ; dans un
dépôt, c'est un identifiant de projet (hash). Le handler ne le lit pas, donc
les deux formes sont acceptables côté quota.

### 2.6 Preuve empirique du reset

Requête directe (sans proxy), identité officielle 1.19.5, le
2026-11-01 à `06:09:52 UTC` :

```text
status: 429
date: Sun, 01 Nov 2026 06:09:52 GMT
retry-after: 64208
x-opencode-log-id: 01a195ba-88fd-7306-95a4-b79e0b54c793
cf-ray: a538bbae19fb61c8-PDX
server: cloudflare
body: {"type":"error","error":{"type":"FreeUsageLimitError", ...}}
```

Calcul indépendant :

```text
prochain minuit UTC = 2026-11-02T00:00:00Z
maintenant           = 2026-11-01T06:09:52Z
secondes restantes   = 64 207 s
retry-after annoncé  = 64 208 s   (arrondi ceil + latence)
```

Le `Retry-After` n'est pas une propriété de l'identité : c'est l'horloge.

Test de spoof de l'en-tête, même minute :

```text
x-real-ip: 203.0.113.77  (envoyé par le client)
→ status 429, retry-after 64184, expected 64183
```

Cloudflare écrase/normalise `x-real-ip` : le client ne choisit pas son bucket
par en-tête.

Test d'un modèle free jamais utilisé via le proxy depuis l'IP locale
(`flint-40b-free`, préfixe `fl`) :

```text
status 429, retry-after 64172, même body FreeUsageLimitError
```

C'est cohérent avec le bucket **default partagé** (`YYYYMMDD`), pas avec un
bucket par modèle. La preuve définitive demanderait une IP neuve : sur une IP
neuve, exécuter le même test et comparer. Voir la matrice E2/E3.

---

## 3. Hiérarchie des identités

Toutes les couches ne jouent pas le même rôle. La confusion vient de les
appeler toutes « identité ».

| Niveau | Exemples | Portée | Clé de quota ? |
|---|---|---|---|
| **Compte** | clé API `st_…`, workspace `wrk_…`, org, abonnement Go | un utilisateur payant | **oui** (payant/Go) |
| **Réseau** | IP source, préfixe IPv6 /64, ASN | un point d'egress | **oui** (free) |
| **Client** | version OpenCode, Bun, JA3/JA4, UA, ordre des en-têtes, corps | tous les installs de cette version | non (accès / pool) |
| **Installation** | `project.id`, `ses_…`, `msg_…` | une installation / conversation | non (routing, logs) |
| **Requête** | `x-opencode-request`, timestamp, nonce | une requête | non (logs) |

Le quota free est au niveau **réseau**. Le quota payant est au niveau
**compte**. Les niveaux client/installation/requête servent à la conformité,
au routage, au cache et à l'observabilité — pas au compteur.

C'est pour cela que « parfaire l'impersonation » et « obtenir un quota unique »
sont deux problèmes orthogonaux :

- l'impersonation agit sur la couche **client** ;
- le quota free agit sur la couche **réseau** ;
- seul un compte agit sur la couche **compte**.

---

## 4. Cartographie couche par couche

| Couche | Valeur / exemple | Unique par installation ? | Unique par requête ? | Visible provider ? | Utilisé pour le quota ? | Doit varier par IP ? |
|---|---|---|---|---|---|---|
| IP source IPv4 | `187.40.18.94` | par egress (dédiée ou partagée) | non | oui (`x-real-ip`) | **oui, clé free** | **oui, c'est le levier** |
| IPv6 /64 | `2001:db8:abcd:1234::/64` | par egress | non | oui (tronquée) | **oui, clé free** | **oui, nouveau /64** |
| IPv6 interface ID | `…:1111` | par adresse | non | non (tronquée) | non | non (même /64) |
| ASN / pays / PoP | ISP, `cf-ray` `-PDX` | non | non | oui (Cloudflare) | non (risque/accès) | non |
| TCP fingerprint | TTL, window, MSS, options | non (par OS) | non | oui (edge) | non | non (garder cohérent) |
| TLS ClientHello | JA3/JA4, ciphers, ext, curves, sigalgs, ALPN | non (par version Bun) | non (random/key_share éphémères) | oui (edge) | non | **non** (doit suivre la version) |
| TLS resumption | tickets / PSK | par session TLS | non | oui | non | non (pool par version) |
| HTTP | HTTP/1.1, `POST /inference/...` | non | non | oui | non | non |
| En-têtes | 13 headers, ordre exact | non (par version) | non | oui | non (mais `checkHeaders` → pool) | non (garder exact) |
| `Authorization` | `Bearer public` | non (constante free) | non | oui → `undefined` | non | non |
| `User-Agent` | `opencode/1.19.5 ai-sdk/... runtime/bun/1.4.4` | non (par version) | non | oui (log) | non | non (suit la version) |
| `x-opencode-client` | `desktop` | non (enum) | non | oui (log) | non | non |
| `x-opencode-project` | `global` ou hash 40 hex | oui (par projet) | non | oui, mais **non lu** et supprimé | **non** | facultatif |
| `x-opencode-session` | `ses_…` | par conversation | non | oui (sticky provider) | **non** (free) | non (par conversation) |
| `x-opencode-session-id` | même `ses_…` | par conversation | non | oui, mais handler lit `x-opencode-session` | non | non |
| `x-opencode-request` | `msg_…` | non | oui | oui (log) | non | non |
| Cookies | aucun dans les captures | — | — | Cloudflare seulement | non | ne pas en inventer |
| Corps | model, messages, tools, system, max_tokens, stream_options | non (par version/requête) | oui (messages) | oui | non (le `model` choisit le préfixe de bucket) | non (garder la forme officielle) |
| Ordre JSON | ordre du serializer officiel | non | non | oui | non | non |
| Timing / concurrence | retries, rafales | non | oui | statistiquement | non (abus) | rester humain |
| DNS / SNI | `opencode.ai` | non | non | oui | non | non |
| Machine ID local | `machineId` du binaire | oui | non | **non, jamais envoyé** | non | sans objet |
| Compte / clé | `st_…`, `wrk_…` | oui | non | oui | **oui, payant/Go** | sans objet |

Deux nuances importantes :

1. **`checkHeaders`** : le code public a `headersExist = true` (check
   temporairement désactivé), mais la config `ZEN_LIMITS.checkHeaders` existe
   et peut router les clients non conformes vers `dailyRequestsFallback`
   (pool réduit). C'est pour cela que l'impersonation des en-têtes compte pour
   **l'accès au pool principal**, pas pour l'identité de quota. Le client
   officiel envoie `x-opencode-client: desktop` ; notre proxy l'envoie aussi.
2. **Le `model`** choisit le préfixe du bucket quand un `rateLimit` explicite
   existe. Ce n'est pas une identité, mais cela peut changer de compteur. Sur
   les modèles « default », tous partagent `YYYYMMDD`.

---

## 5. Bun a-t-il une identité propre ?

**Non — au sens où la question l'entend.** Bun a une **empreinte de version**,
pas une identité d'installation ou d'utilisateur.

- Le ClientHello de Bun 1.4.4 (OpenCode 1.19.5) est :
  `771,4865-…,0-23-65281-…-43,4588-29-23-24,0`
  (JA3-MD5 `1523504b38f0fae0d881d4b6554aac1b`).
- Le ClientHello de Bun 1.3.14 (1.18.31 / 1.19.0 / 1.19.3) est :
  `771,4865-…,0-23-65281-…-43-21,29-23-24,0`
  (JA3-MD5 `d871d02cecbde59abbf8f4806134addf`).
- Tous les installs officiels de la **même version** envoient la même famille
  TLS. Les champs `random` et `key_share` changent à chaque connexion, mais ce
  sont des valeurs éphémères, pas un identifiant persistant.
- Le bundle ne contient aucun `x-opencode-machine-id` / `x-device-id` /
  `x-install-id` sur l'inférence. Le `machineId` local n'est pas transmis.

Donc :

- **varier le JA3 par IP est une erreur** : tu obtiens une combinaison
  impossible (UA 1.19.5 + JA3 1.3.14, ou l'inverse) qui peut être signalée
  par l'edge ;
- **le JA3 doit suivre la version revendiquée**, pas l'IP ;
- si tu veux changer de « face » pour une IP, change le **profil de version
  complet** : version ↔ Bun ↔ JA3 ↔ sigalgs ↔ ordre des en-têtes ↔ présence de
  `x-opencode-session-id` ↔ corps ;
- les valeurs réellement propres à une installation sont `project.id`,
  `ses_…`, `msg_…` — et elles ne comptent pas pour le quota.

La règle pratique :

> **Un profil de version est global et figé. Une identité d'installation est
> fraîche par IP. Le quota est une propriété de l'IP.**

---

## 6. Pourquoi 18 h / 19 h / 24 h ?

Le `Retry-After` est le compte à rebours jusqu'au reset **propre à l'IP**. Pour
une IP dont le reset tombe à minuit UTC, le tableau ci-dessous s'applique :

| Moment du 429 (UTC) | `Retry-After` | Affichage |
|---|---|---|
| 00:00:00 | 86 400 s | 24 h |
| 00:00:01 | 86 399 s | ~24 h |
| 05:00:00 | 68 400 s | 19 h |
| 06:00:00 | 64 800 s | 18 h |
| 18:00:00 | 21 600 s | 6 h |
| 23:59:59 | 1 s | 1 s |

Mais les resets mesurés le 2026-11-01 ne sont **pas** tous à minuit : cinq IPs
ont donné des resets entre **00:01:34** et **00:53:10 UTC**. Deux IPs peuvent
donc afficher « 19 h » et « 18 h » au même moment alors que leurs resets
diffèrent réellement. Le reset n'est pas un instant global ; c'est un instant
**par bucket IP**.

Conséquences :

- le `date` de la réponse + `Retry-After` donne le reset exact de **cette
  IP** ; il faut le stocker tel quel, sans snap sur minuit ;
- changer d'IP crée un nouveau bucket avec son propre reset ;
- si le provider utilise une fenêtre glissante de 24 h, la fenêtre peut être
  entretenue par d'autres utilisateurs de la même IP partagée ;
- ne jamais supposer qu'une IP redeviendra disponible à un minuit global.

Voir [`docs/free-429-investigation.md`](free-429-investigation.md) §3 pour la
table des resets par IP et §7 pour la seule voie free (IP fraîche
non-datacenter ou compte).

---

## 7. Ce que « quota unique » peut vouloir dire

Il y a trois choses différentes derrière cette expression.

### 7.1 Un bucket free exclusif (recommandé si l'objectif est la stabilité)

Le quota free est `(ip, YYYYMMDD[, préfixe])`. Pour qu'il soit « à moi », il
faut une **IP d'egress dédiée** que personne d'autre n'utilise :

- IP résidentielle statique (idéal) ;
- IP dédiée chez un VPN (mieux qu'une IP partagée, mais datacenter) ;
- IPv6 /64 dédié (si l'ISP délègue un /56 ou /48, chaque /64 est un bucket).

Ce n'est pas un reset « à 19 h » : c'est un bucket qui ne se vide pas à cause
des autres. Le reset reste 00:00 UTC.

### 7.2 Un quota agrégé (N IPs = N buckets)

Chaque IP (/64) a son propre compteur journalier. Faire tourner les IPs
augmente le total disponible, mais :

- chaque bucket reste soumis au reset UTC ;
- les IPs datacenter sont souvent **déjà épuisées** (partagées) ;
- les IPs CGNAT sont partagées par des dizaines d'utilisateurs ;
- le provider peut voir la rotation comme un abus (ASN, réputation).

C'est la stratégie actuelle du proxy (stations VPN), avec les limites connues.

### 7.3 Un vrai quota de compte (payant / Go)

C'est le seul quota réellement **personnel**, avec des fenêtres différentes :

- `KeyRateLimitTable` : clé API + `model[0:27]` + minute ; défaut 1000/min,
  `Retry-After: 60` ;
- Go : rolling 5 h, weekly, monthly ancré à la souscription ;
- `GoUsageLimitError` porte `workspace` et `limitName` dans `metadata`.

Mais il faut un compte, une clé ou un abonnement — ce que l'utilisateur refuse.
Sans compte, il n'existe **aucun** champ client qui transforme une IP en
« utilisateur » distinct pour le quota free.

### 7.4 Ce qui ne marchera pas

- changer `x-opencode-project` / `x-opencode-session` / `x-opencode-request` ;
- changer la version, le Bun, le JA3 ou l'ordre des en-têtes ;
- changer le corps, les outils ou le prompt système ;
- ajouter des cookies ou des headers « machine » ;
- spoof `x-real-ip` (testé : Cloudflare écrase) ;
- changer l'adresse IPv6 dans le même /64.

Tous ces champs sont soit ignorés, soit journalisés, soit supprimés avant le
provider. Aucun n'entre dans `ip_rate_limit`.

### 7.5 Risques opérationnels

- Le code de quota est IP-only, mais l'edge Cloudflare peut utiliser l'ASN,
  la réputation de l'IP et le taux de rotation pour challenger/bloquer, en
  dehors du compteur.
- Les IPs datacenter sont souvent déjà épuisées ou challengeées ; une IP
  résidentielle dédiée est plus stable.
- Une rotation agressive et des retries sur 429 brûlent le bucket (le
  `track()` est appelé après la réponse) et peuvent ressembler à un abus.
- Le contournement du quota free peut être contraire aux conditions du
  service ; c'est un risque opérationnel à arbitrer, pas une limite technique
  du proxy.

---

## 8. Matrice d'expériences contrôlées

Objectif : prouver chaque affirmation sur le wire, pas seulement par lecture
de code. Une seule variable change à la fois.

| ID | Question | Manipulation | Mesure | Attendu si l'IP est la clé | Statut |
|---|---|---|---|---|---|
| E0 | Le reset est-il minuit UTC ? | 429 à une heure connue | `date`, `retry-after` | `retry-after ≈ 86400 − secondes depuis 00:00 UTC` | **fait** (64208 vs 64207) |
| E1 | Les champs d'identité comptent-ils ? | Même IP, nouveau project/session/msg/version | 429 + `retry-after` | même 429, même reset | **fait** (client officiel + profil généré) |
| E2 | Le bucket est-il global ou par modèle ? | Même IP, modèle free jamais utilisé (`flint-40b-free`) | 429 ou 200 | si global : 429 ; si par modèle : 200 | **fait** : 429 (cohérent avec global) |
| E3 | Une IP neuve suffit-elle ? | IP résidentielle/mobile neuve, **même** identité que l'IP brûlée | 200 ou 429 | 200 → l'IP est la clé | **à faire** (pas d'IP neuve dispo) |
| E4 | Le /64 est-il la clé IPv6 ? | Deux adresses dans le même /64, puis deux /64 différents | 429/200 | même /64 : même bucket ; /64 neuf : bucket neuf | **à faire** (IPv6 requis) |
| E5 | Un échec upstream consomme-t-il ? | Requête qui passe `check()` puis 400/429 upstream | compteur du bucket (via épuisement contrôlé) | oui, `track()` est appelé après la réponse | à faire |
| E6 | Le reset dépend-il de l'heure ? | 429 à 05:00 puis 06:00 UTC | `retry-after` | 19 h puis 18 h, même instant de reset | **fait implicitement** |
| E7 | Le bonus « nouvelle IP » existe-t-il ? | IP neuve, épuiser jour 1 puis jour 8 | nombre de requêtes avant 429 | ~2× jour 1, 1× après lifetime ≥ 7×daily | à faire (long) |
| E8 | Peut-on choisir `x-real-ip` ? | Ajouter `x-real-ip: 203.0.113.77` | 200/429 | 429 identique → edge écrase | **fait** |
| E9 | Le chemin payant est-il différent ? | Clé API / Go | limiteur, reset | `KeyRateLimitTable` / Go windows | bloqué (clé expirée) |

### 8.1 Schéma d'enregistrement

Pour chaque tentative, journaliser :

```json
{
  "ts_utc": "2026-11-01T06:09:52Z",
  "server_date": "Sun, 01 Nov 2026 06:09:52 GMT",
  "egress_ip": "187.40.18.94",
  "ip_norm": "187.40.18.94",
  "ip_version": 4,
  "model": "deepseek-v4.1-flash-free",
  "bucket_guess": "20261101",
  "identity_version": "1.19.5",
  "client": "desktop",
  "session": "ses_…",
  "project": "global",
  "tls_ja3_md5": "1523504b…",
  "status": 429,
  "error_type": "FreeUsageLimitError",
  "retry_after": 64208,
  "reset_at_utc": "2026-11-02T00:00:00Z",
  "cf_ray": "a538bbae19fb61c8-PDX",
  "log_id": "01a195ba-88fd-7306-95a4-b79e0b54c793",
  "notes": "direct, no proxy"
}
```

`reset_at_utc` doit être calculé depuis `server_date + retry_after`, pas depuis
l'horloge locale. Si `retry_after` est petit (≤ 120 s), c'est un limiteur
court (clé/TPM/upstream), pas le bucket free journalier.

### 8.2 Ordre recommandé

1. E3 d'abord : une IP neuve, même identité. C'est le test le plus discriminant.
2. E4 ensuite : même /64 vs /64 neuf.
3. E2/E7 sur l'IP neuve : modèle jamais utilisé, puis épuisement contrôlé.
4. E5 : provoquer un 400 propre et vérifier la consommation.
5. E1/E6/E8 sont déjà couverts ; les rejouer pour documenter.

---

## 9. Architecture cible (plan, pas encore de code)

### 9.1 Composants

```text
EgressPool ──> EgressEndpoint
                 (station_id, ip, ip_norm, asn, country, health)
                      │
                      ▼
              IdentityAllocator ──> IdentityProfile
                      │                 (version, client, project,
                      │                  ses, msg, tls_profile, generation)
                      ▼
                 QuotaLedger <────── RequestRunner ──────> Provider
                 (ip_norm, utc_date, bucket)                 │
                      ▲                                      │
                      └────────── 429 / retry-after ─────────┘
```

### 9.2 `EgressEndpoint`

- `ip` observée (via un echo IP ou la réponse du provider) ;
- `ip_norm` : IPv4 telle quelle ; IPv6 tronquée aux **4 premiers groupes**
  (même règle que `handler.ts`) ;
- `asn`, `country`, `station_id`, `source` (vpn/direct/mobile) ;
- `health`, `last_seen`, `last_429_at`.

Le point critique : **normaliser exactement comme le serveur**. Si le proxy
garde l'IPv6 complète, il croira que deux adresses du même /64 sont deux
buckets, alors que le serveur les fusionne.

### 9.3 `IdentityProfile`

- `egress_id` : liaison **1:1** avec une IP ; jamais partagé entre deux IPs ;
- `version`, `client`, `project`, `ses_global`, `conv_sessions`, `msg` ;
- `tls_profile` : JA3/sigalgs/header order **figés par version** ;
- `generation` : pour une rotation d'installation sur la même IP (linkage),
  pas pour le quota ;
- `created_at`, `last_used_at`.

Règle : un profil = une IP = une installation. Le profil ne traverse pas les
IPs. Mais le **profil de version** (TLS/headers/body) est partagé par toutes
les IPs de la même version ; seule l'identité d'installation change.

### 9.4 `QuotaLedger`

Clé :

```text
(ip_norm, utc_date, bucket)
bucket = "default"                  # modèle sans rateLimit explicite
       | model_id[:2]               # modèle avec rateLimit explicite
```

Valeurs :

- `requests`, `first_request_at`, `last_request_at` ;
- `last_status`, `last_error_type` ;
- `retry_after`, `reset_at` ;
- `confidence` (`observed`, `inferred`, `unknown`) ;
- `notes`.

Politique conservatrice : tant que E2/E7 n'ont pas prouvé le contraire,
traiter **tous les modèles free comme un seul bucket `default`**. Ne jamais
supposer qu'un changement de modèle libère du quota.

### 9.5 `ResetClock`

- `server_now ≈ now` (le header `date` n'est pas propagé dans tous les
  appelants ; l'écart est absorbé par le snap sur minuit UTC) ;
- `reset_at = server_now + retry_after` ;
- vérifier `reset_at ≈ next_utc_midnight` (tolérance 120 s) ;
- si `retry_after <= 120` : limiteur court (clé/TPM/upstream), ne pas
  l'écrire comme reset journalier ;
- si l'écart avec minuit UTC est important : logger une anomalie (le code a
  peut-être changé, ou c'est un autre limiteur).

### 9.6 `IdentityAllocator`

- choisir une `EgressEndpoint` dont le ledger n'a **aucune** entrée pour la
  date UTC courante ;
- si toutes les IPs sont épuisées : attendre le plus proche `reset_at`, ou
  acquérir une nouvelle IP ;
- **ne jamais** appeler `rotate_installation()` pour espérer échapper à un 429
  sur la même IP : cela change le linkage, pas le bucket ;
- en cas de 429 `FreeUsageLimitError` : marquer le bucket `exhausted`, poser
  `reset_at`, ne pas retenter sur la même IP avant le reset.

### 9.7 `RequestRunner`

- avant l'envoi : vérifier le headroom du bucket (réserver 10–20 %) ;
- après l'envoi : mettre à jour le ledger **même pour un non-200** (le serveur
  `track()` après la réponse) ;
- sur 429 : parser `error.type` et `retry-after` ; distinguer
  `FreeUsageLimitError` (bucket IP) de `RateLimitError` (clé, 60 s) et de
  `GoUsageLimitError` (compte) ;
- ne pas fan-out sur un 429 : chaque tentative qui passe le `check()` consomme ;
- garder `x-opencode-session` stable par conversation (sticky provider) ;
- ne pas changer le profil TLS par IP.

### 9.8 `Observability`

Logger au minimum : `egress_ip`, `ip_norm`, `bucket_guess`, `status`,
`error_type`, `retry_after`, `reset_at`, `cf_ray`, `log_id`,
`identity_version`, `session`, `project`, `tls_ja3_md5`.

Cela permet de répondre à la question « pourquoi ce 429 ? » en une ligne :
bucket épuisé, limiteur court, ou provider instable.

### 9.9 Migration depuis l'existant

- `free/identities.py` fait déjà **1 IP = 1 identité** ; on garde.
- Ajouter `EgressEndpoint` + `QuotaLedger` + normalisation IPv6 /64.
- Changer la sémantique de `rotate_installation()` : rotation de linkage /
  version, **pas** de quota. Le quota ne se contourne que par l'egress.
- Ajouter une rotation d'egress distincte, avec ledger.
- Tests : normalisation /64, calcul `reset_at`, distinction des types d'erreur,
  non-réutilisation d'un bucket épuisé.

### 9.10 Sessions TLS, cookies et générations

Le pool curl actuel est clé par `<proxy>|<egress_ip>|bun|free-<version>`
(`_get_pooled_curl_session`), donc deux IPs ne partagent jamais une session
TLS — c'est le bon réflexe. En revanche, deux **générations** sur la même IP
partagent encore le pool : même ticket TLS, même cookie jar. Ce n'est pas une
clé de quota (le bucket est l'IP), mais c'est un lien d'installation. Pour une
impersonation stricte, la clé devrait inclure la génération :
`<proxy>|<ip_norm>|<generation>|free-<version>`. Idem pour les sondes qui
utilisent le singleton global `_free_session_id()` sans IP : elles peuvent
partager un `ses_` entre IPs. Ce n'est pas le quota, mais cela peut corréler
deux egress dans les logs.

---

## 10. Ce qu'il ne faut pas faire

- **Varier le JA3 par IP** en gardant la version fixe : combinaison impossible.
- **Croire qu'un nouveau `ses_` / `project` / `msg_` reset le quota** : le
  handler ne les utilise pas pour le compteur.
- **Spoofer `x-real-ip`** : testé, Cloudflare écrase.
- **Fan-out de retries sur 429** : chaque échec post-`check()` consomme.
- **Utiliser des IPs datacenter partagées** pour un quota « personnel » :
  souvent déjà épuisées.
- **Faire confiance à l'horloge locale** : utiliser `date` + `retry-after`.
- **Confondre 18 h / 19 h / 24 h avec des resets différents** : c'est le même
  minuit UTC, vu à des heures différentes.
- **Confondre `x-opencode-project` avec un identifiant de quota** : il n'est
  même pas lu par le handler.

---

## 11. Ce qu'il faut faire

- **Une IP = un profil = un ledger.** Lier l'identité d'installation à
  l'egress, pas l'inverse.
- **Normaliser IPv6 au /64** exactement comme le serveur.
- **Garder le profil de version cohérent** : version ↔ Bun ↔ JA3 ↔ headers ↔
  body. Le TLS suit la version, pas l'IP.
- **Réserver du headroom** et arrêter avant la limite ; ne pas brûler le
  bucket en retries.
- **Traiter tous les modèles free comme un bucket `default`** jusqu'à preuve
  du contraire.
- **Calculer `reset_at` depuis le serveur** (`date` + `retry-after`), pas
  depuis l'horloge locale.
- **Pour un quota vraiment personnel** : IP dédiée (résidentielle statique ou
  /64 dédié) ou compte payant/Go. Il n'y a pas de troisième voie côté free.

---

## 12. Réponses directes

**« Bun a-t-il sa propre identité ? »**
Il a une empreinte de **version** (JA3, UA, sigalgs), partagée par tous les
installs de cette version. Ce n'est pas une identité d'installation, encore
moins d'utilisateur. Le `machineId` local n'est pas envoyé.

**« Faut-il modifier Bun pareil pour chaque IP ? »**
Non. Le profil de version doit rester **global et cohérent**. Ce qui change
par IP, ce sont les identifiants d'installation (`project`, `ses_`, `msg_`) —
et, pour le quota, l'IP elle-même. Varier le TLS par IP casse l'impersonation
au lieu de la renforcer.

**« Sur quoi se base le provider pour dire 19 h ? »**
Sur l'heure UTC au moment du 429 : `Retry-After = 86400 − (now mod 86400)`.
Ce n'est pas un identifiant, c'est une horloge. 19 h = 05:00 UTC ; 18 h =
06:00 UTC ; 24 h = juste après minuit.

**« Comment m'attribuer un quota unique ? »**
En free : une IP d'egress dédiée (IPv4 ou /64 IPv6) que personne d'autre
n'utilise. Le bucket `(ip, date)` devient alors le tien, mais il reset toujours
à 00:00 UTC. Sinon, N IPs = N buckets agrégés, avec les limites du partage.
Le seul quota réellement personnel est un compte payant/Go.

**« Pourquoi les autres ont 18 h / 24 h ? »**
Parce qu'ils ont atteint la limite à une autre heure UTC. L'instant de reset
est le même ; seul le compte à rebours diffère.

---

## 13. Annexes

### 13.1 Sources

- `packages/console/app/src/routes/zen/util/handler.ts` —
  <https://github.com/sst/opencode/blob/47f33329/packages/console/app/src/routes/zen/util/handler.ts>
- `packages/console/app/src/routes/zen/util/ipRateLimiter.ts` —
  <https://github.com/sst/opencode/blob/47f33329/packages/console/app/src/routes/zen/util/ipRateLimiter.ts>
- `packages/console/core/src/schema/ip.sql.ts` —
  <https://github.com/sst/opencode/blob/47f33329/packages/console/core/src/schema/ip.sql.ts>
- `packages/console/app/src/routes/zen/util/trialLimiter.ts` —
  <https://github.com/sst/opencode/blob/47f33329/packages/console/app/src/routes/zen/util/trialLimiter.ts>
- `packages/console/app/src/routes/zen/util/keyRateLimiter.ts` —
  <https://github.com/sst/opencode/blob/47f33329/packages/console/app/src/routes/zen/util/keyRateLimiter.ts>
- `packages/console/core/src/subscription.ts` (limites `free`, `lite`,
  `black`).

### 13.2 Limite secondaire : le budget trial par IP

`trialLimiter.ts` tient un **second** compteur par IP, `IpTable.usage`, en
tokens lifetime, comparé à `promoTokens` :

```ts
const limit = Subscription.getFreeLimits().promoTokens
_isTrial = (data?.usage ?? 0) < limit
```

Il ne provoque pas `FreeUsageLimitError` ; il choisit seulement si la requête
est routée vers les `trialProvider`. C'est encore une clé **IP**, pas une clé
de session/compte. À suivre dans le ledger comme un budget séparé
(`bucket = "trial_tokens"`), mais il ne change pas le reset journalier.

### 13.3 Limite payante (contraste)

`keyRateLimiter.ts` :

```ts
const LIMIT = rateLimit ?? 1000
const interval = `${modelId.substring(0, 27)}-${yyyyMMddHHmm}`
if (count >= LIMIT) throw new RateLimitError(..., 60)
```

Clé = `zenApiKey` (pas l'IP), fenêtre = minute, `Retry-After = 60 s`. C'est un
tout autre modèle d'attribution : la clé/le workspace est l'identité, et les
fenêtres Go (rolling/weekly/monthly) sont personnelles. C'est le seul chemin
qui donne un reset « à moi » — mais il exige un compte.

### 13.4 Ce que le proxy doit retenir

1. Le quota free est un compteur **`(ip_norm, date UTC)`**.
2. Le reset est **00:00 UTC**, pas un timestamp par utilisateur.
3. Les champs d'identité client sont pour **l'accès et la conformité**, pas
   pour le quota.
4. Le seul levier de quota free est **l'egress** (IP ou /64).
5. Le seul quota personnel est **le compte payant/Go**.

---

## 14. Implémentation (2026-11-01)

Le plan ci-dessus est implémenté dans le proxy. Aucun changement de client.

### 14.1 Nouveau module : `free/quota_ledger.py`

- `server_ip_key()` reproduit exactement la troncature du handler
  (`rawIp.split(":")[:4]`), y compris les formes compressées et les listes
  `X-Forwarded-For`.
- `utc_day()` → `YYYYMMDD` ; `next_utc_midnight()` → timestamp du prochain
  00:00 UTC (même formule que `getRetryAfterDay`).
- `bucket_for(model, mode)` → `"default"` (tous les free) ou `model[:2]`
  (mode `model_prefix`, pour les modèles à `rateLimit` explicite).
- `QuotaLedger` : registre thread-safe, persisté dans
  `logs/quota_ledger.json` (écriture atomique, rétention 3 jours, plafond
  8192 entrées). Chaque entrée porte `requests`, `successes`, `failures`,
  `last_status`, `last_error`, `retry_after`, `reset_at`, `exhausted`,
  `source`.

Règles :

- un `FreeUsageLimitError` marque le bucket `(IP, jour UTC)` jusqu'à
  `now + Retry-After` (snappé sur minuit UTC si l'écart est ≤ 120 s) ;
- sans `Retry-After`, le reset est le prochain minuit UTC ;
- un 429 transitoire (`Endpoint is unavailable`, `RateLimitError`) **ne
  bloque pas** le bucket ;
- un 200 observé lève l'exhaustion (le ledger était faux ou le reset est
  arrivé) ;
- `daily_limit=0` (défaut) : seule l'exhaustion observée bloque. Une valeur
  > 0 ajoute un block proactif à `limit − réserve`.

### 14.2 Intégration pool (`free/pool.py`)

- `FreeIPPool.__init__` instancie le ledger depuis `ip_quota` :
  `ledger_enabled`, `ledger_bucket_mode`, `ledger_daily_limit`.
- `_station_usable()` et `model_usable()` rejettent une IP dont le bucket est
  épuisé (`daily-bucket-exhausted`). Le gate `model=None` continue de voir le
  tunnel : un bucket épuisé n'est pas un tunnel mort.
- `_note_model_request()` compte chaque tentative ; `note_model_ip()` enregistre
  le résultat (200 / 429 / autre) et classe le 429 via le corps.
- `all_usable_buckets_exhausted(model)` : vrai si **toutes** les stations
  tunnel-usable ont un bucket épuisé (sert au gate et au preflight).
- `get_status()` expose `quota_ledger` (buckets + `earliest_reset_iso`).

### 14.3 Intégration `opencode.py`

- `_mark_free_stations_429()` transmet `body`, `error_type` et `retry_after`
  au ledger ; le `FreeUsageLimitError` d'une IP n'est plus transformé en
  cooldown modèle-global immédiat. Le cooldown global reste réservé au cas
  « ≥ 3 IP distinctes échouent » (détection existante dans `note_model_ip`).
- La rotation d'identité **sur la même IP** a été retirée du chemin 429 :
  elle ne rend aucun quota. Seule la rotation d'egress reste armée.
- `_free_bucket_retry_after()` renvoie le reset du ledger (le plus proche)
  au lieu du dernier header vu.
- Le gate de warmup ne bloque plus 45 s quand toutes les IPs routables sont
  épuisées : il laisse le chemin free rendre le 429 immédiatement.
- `_free_preflight_retry_after()` vérifie l'exhaustion **avant** le bypass
  « rotation en vol » et renvoie le reset du ledger.
- `_wait_for_fresh_free()` renvoie `False` immédiatement quand toutes les IPs
  routables sont épuisées, au lieu d'attendre 45 s une rotation qui échoue.
  La prochaine requête profitera d'une IP fraîche si une rotation aboutit.
- `_free_stations_exhausted()` ne renvoie plus `True` sur le seul cooldown
  modèle-global : un bucket frais (IP neuve) prime. Le cooldown global reste
  le fallback quand aucune station n'a de bucket frais.

### 14.4 Configuration

```yaml
ip_quota:
  ledger_enabled: true
  ledger_bucket_mode: default   # default | model_prefix
  ledger_daily_limit: 0         # 0 = inconnu (seul le 429 observé bloque)
```

### 14.5 Vérification live

- Six IPs déjà 429 ont été amorcées dans le ledger (reset minuit UTC).
- Une IP fraîche (`187.40.42.3`) a été tentée une fois : 429, puis marquée.
- Requête suivante : **429 en 0,02 s**, `Retry-After` = secondes jusqu'à
  `2026-11-02T00:00:00Z`, sans attente de warmup ni rotation.
- `/api/pool-status` expose `quota_ledger.buckets` avec 8 IPs, toutes
  `exhausted=true`, `reset_at_iso=2026-11-02T00:00:00Z`.
- Les buckets sont partagés entre modèles free (`bucket=default`), donc un
  changement de modèle ne contourne pas l'IP épuisée.

### 14.6 Tests

- `tests/test_quota_ledger.py` : normalisation IP, reset, bucket partagé,
  429 transitoire vs `FreeUsageLimitError`, expiry, persistance, snapshot.
- `tests/test_quota_bucket_pool.py` : une IP épuisée bloque la station mais
  pas le gate tunnel ; une IP neuve est un bucket neuf ; un 200 lève le
  blocage ; un 429 transitoire ne bloque pas.
- Mise à jour : `test_free_quota_vs_transient.py` (le 429 `FreeUsageLimitError`
  marque le bucket et rotationne l'egress, sans gel modèle-global immédiat),
  `test_identity_rotation.py` (plus de rotation d'identité sur la même IP),
  `test_free_nonstream_status.py` (double curl avec `variant`).

### 14.7 Limite restante

La valeur exacte de `dailyRequests` reste inconnue (`ZEN_LIMITS` côté
service). Le ledger est donc **observé** : il bloque après un vrai 429, pas
après un compteur deviné. Les expériences E3/E4 (IP neuve, /64 neuf) restent
à faire pour verrouiller la preuve expérimentale ; le comportement actuel est
déjà aligné sur le code serveur et sur la mesure live.
