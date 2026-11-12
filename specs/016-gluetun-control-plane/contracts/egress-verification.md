# Contrat — Vérification d'egress

**Feature** : `016-gluetun-control-plane`
**Composant** : `internal/egress/{source,consensus,canary}`
**Version** : `1.0.0`

---

## 1. Objectif

Prouver l'IP de sortie réelle du tunnel, indépendamment des dires d'un seul
service, avec un consensus k-of-n sur la **valeur d'IP**, l'exclusion de l'IP
d'hôte et un canary HTTP 200 avec contenu. Une vérification non prouvée n'est
jamais convertie en succès.

## 2. Configuration

| Variable | Défaut | Description |
|---|---|---|
| `CONTROL_EGRESS_ENABLED` | `true` | Active la vérification. |
| `CONTROL_EGRESS_SOURCES` | `http:ipify,dns:whoami,publicip:internal` | Sources ordonnées (`kind:name`). |
| `CONTROL_EGRESS_QUORUM` | `2` | k du k-of-n. |
| `CONTROL_EGRESS_MIN_SOURCES` | `3` | n minimal ; en dessous → `insufficient_sources`. |
| `CONTROL_EGRESS_ALLOW_SINGLE` | `false` | Autorise une seule source (déconseillé). |
| `CONTROL_EGRESS_CACHE_TTL_S` | `10` | TTL du dernier résultat. |
| `CONTROL_EGRESS_SOURCE_TIMEOUT_MS` | `1500` | Timeout par source. |
| `CONTROL_EGRESS_SOURCE_BACKOFF_MS` | `30000` | Backoff par source en échec. |
| `CONTROL_EGRESS_SOURCE_BACKOFF_MAX_MS` | `300000` | Plafond. |
| `CONTROL_EGRESS_FAMILY` | `4` | Famille primaire (`4`, `6`, `both`). |
| `CONTROL_EGRESS_FORBIDDEN_IPS` | vide | Liste CSV d'IP à rejeter. |
| `CONTROL_EGRESS_HOST_IP_AUTO` | `true` | Capture l'IP d'underlay au boot. |
| `CONTROL_CANARY_URL` | vide | URL neutre ; vide ⇒ canary désactivé. |
| `CONTROL_CANARY_METHOD` | `GET` | Méthode. |
| `CONTROL_CANARY_EXPECT_STATUS` | `200` | Statut attendu. |
| `CONTROL_CANARY_MIN_BODY_BYTES` | `1` | Corps minimal non vide. |
| `CONTROL_CANARY_EXPECT_SHA256` | vide | Hash optionnel du corps. |
| `CONTROL_CANARY_TIMEOUT_MS` | `2000` | Timeout. |

Le canary est un endpoint **neutre** : il ne consomme pas le quota modèle et ne
doit jamais pointer vers l'API du fournisseur de modèles.

## 3. Sources

### 3.1 Types

| Kind | Exemple | Indépendance | Remarque |
|---|---|---|---|
| `http` | `https://api.ipify.org?format=json` | Réseau différent du DNS | Parse JSON ou texte ; timeout strict. |
| `dns` | `whoami.cloudflare` TXT via le resolver du tunnel | Chemin DNS | Prouve aussi que le DNS du tunnel fonctionne. |
| `publicip` | données `publicip` internes du fork | Chemin interne | **Ne suffit jamais seul** : peut être l'IP d'hôte. |
| `static` | `https://ifconfig.me/ip` | Secours | Désactivable. |

### 3.2 Contrat d'une source

```go
type Source interface {
    Name() string
    Kind() string
    Fetch(ctx context.Context, family int) (Observation, error)
}
```

- Retourne au plus une IP + métadonnées (pays, ville, ASN, org, latence).
- Erreur typée : `timeout`, `http_status`, `malformed`, `empty`, `banned`.
- Aucune source ne doit écrire dans `registry.db`.
- Une source en échec est mise en backoff exponentiel ; elle ne bloque pas les
  autres (collecte parallèle, résultats partiels acceptés).

### 3.3 Capture de l'IP d'hôte

Au démarrage, avant le tunnel :

1. Lire les interfaces non-loopback (`net.Interfaces`).
2. Conserver les IP globales unicast comme `host_ips` (v4 et v6).
3. Si `CONTROL_EGRESS_HOST_IP_AUTO=false`, utiliser `CONTROL_HOST_IP` fourni.
4. Toute observation égale à `host_ips` est rejetée `host_ip`.
5. L'IP d'hôte n'est jamais utilisée comme preuve, même si toutes les sources la
   renvoient.

## 4. Consensus

### 4.1 Algorithme

```text
verify(sources, k, n, family):
  observations = parallelFetch(sources, timeout)
  accepted = []
  for o in observations:
      if o.err != nil: continue
      if !validIP(o.ip): reject(o, reason); continue
      if belongsToHost(o.ip): reject(o, "host_ip"); continue
      if inForbidden(o.ip): reject(o, "forbidden_ip"); continue
      accepted.append(o)

  if len(accepted) < max(k, n):
      # n est un minimum de sources *disponibles*, pas de succès
      if len(accepted) < k: return {verified:false, reason:"insufficient_sources"}
      if len(accepted) < n: return {verified:false, reason:"insufficient_sources"}

  groups = groupByIP(accepted)          # égalité exacte netip.Addr
  winner = group with count >= k, highest count
  if winner == nil:
      return {verified:false, reason:"no_quorum", observations}
  if tie(winner, other with same count):
      # départage : préférer la source de kind dns, puis l'IP la plus petite
      winner = deterministicTieBreak(winner, other)

  dissenters = observations not in winner group
  confidence = high if len(dissenters)==0
               medium if len(dissenters)==1
               low otherwise
  if canaryEnabled and not canaryOK():
      return {verified:false, reason:"canary_failed", ...}
  return {verified:true, ip:winner.ip, quorum:winner.count, ...}
```

### 4.2 Règles

- Consensus **par valeur exacte** ; pas de consensus géographique.
- `k` par défaut 2, `n` par défaut 3 ; `k ≤ n`.
- Si `allow_single_source=true` et `k=1`, une source suffit, mais le résultat est
  marqué `confidence=low` et `single_source=true` (jamais par défaut).
- Les observations rejetées restent dans `dissenters` avec leur raison.
- `verified=false` si le canary échoue, même avec quorum.
- IPv4 et IPv6 sont des consensus séparés ; `family=both` exige un quorum par
  famille ou retourne les deux résultats.

### 4.3 Rejets durs

| Raison | Condition |
|---|---|
| `private` | RFC1918, ULA `fc00::/7`. |
| `loopback` | `127.0.0.0/8`, `::1`. |
| `link_local` | `169.254.0.0/16`, `fe80::/10`. |
| `cgnat` | `100.64.0.0/10`. |
| `multicast` | `224.0.0.0/4`, `ff00::/8`. |
| `unspecified` | `0.0.0.0`, `::`. |
| `host_ip` | Égale à une IP d'underlay capturée. |
| `forbidden_ip` | Présente dans `CONTROL_EGRESS_FORBIDDEN_IPS`. |
| `malformed` | Parse impossible, port inclus, domaine. |
| `wrong_family` | Famille non demandée. |
| `documentation` | Plages TEST-NET (`192.0.2.0/24`, etc.) **en production** ; autorisées en test avec `CONTROL_EGRESS_ALLOW_TEST_NETS=true`. |

## 5. Canary

```text
ok = status == EXPECT_STATUS
  && body_bytes >= MIN_BODY_BYTES
  && (EXPECT_SHA256 == "" || sha256(body) == EXPECT_SHA256)
  && no transport error
  && latency <= timeout
```

- `200` avec corps vide ⇒ `failure="empty_body"`.
- `200` avec erreur TLS ⇒ `failure="tls"`.
- `3xx` non suivi ⇒ `failure="status_302"` (pas de redirection par défaut).
- Le canary est exécuté **à travers le tunnel** (proxy/SOCKS ou namespace), pas
  depuis l'hôte.
- Une seule requête par vérification ; pas de retry agressif.

## 6. Cache et concurrence

- Cache du dernier `EgressVerification` par station, TTL 10 s.
- `GET /v1/egress` sert le cache ; `?fresh=true` force.
- Single-flight : une seule collecte simultanée par station ; les appels
  concurrents attendent le même résultat.
- Une collecte qui dépasse `timeout_s` retourne `503 not_ready` avec
  `retry_after_ms`.
- Le cache n'est **jamais** utilisé pour un `committed` de job : le job exige
  `fresh=true`.

## 7. Endpoints

| Méthode | Route | Scope | Description |
|---|---|---|---|
| GET | `/v1/egress` | reader | dernier résultat (cache) |
| GET | `/v1/egress/verify` | reader | collecte fraîche (`fresh`, `quorum`, `min_sources`, `canary`, `timeout_s`) |
| GET | `/v1/egress/canary` | reader | canary seul |
| GET | `/v1/egress/history` | reader | historique borné (`limit`, `since`) |

Réponse `EgressVerification` : voir `data-model.md` §8.

## 8. Métriques

```text
gluetun_egress_verifications_total{result}          # verified|no_quorum|insufficient|canary_failed
gluetun_egress_source_requests_total{source,result}
gluetun_egress_source_latency_seconds{source}
gluetun_egress_source_banned{source}                # 0/1
gluetun_egress_quorum                              # dernière valeur
gluetun_egress_dissenters                          # dernier nombre
gluetun_egress_canary_ok                           # 0/1
gluetun_egress_cache_hits_total
```

## 9. Critères d'acceptation

| ID | Scénario | Attendu |
|---|---|---|
| EG-AT-01 | 3 sources, 2 répondent `203.0.113.7`, 1 `198.51.100.9` | `verified=true`, `ip=203.0.113.7`, `confidence=high`, dissident listé. |
| EG-AT-02 | 1 seule source, `allow_single_source=false` | `verified=false`, `insufficient_sources`. |
| EG-AT-03 | L'IP d'hôte est renvoyée par 3 sources | Toutes rejetées `host_ip`, `verified=false`. |
| EG-AT-04 | Canary 200 corps vide | `canary_failed`, `empty_body`, pas de commit. |
| EG-AT-05 | Vérification fraîche < 10 s, `fresh=false` | `cached=true`, même résultat. |
| EG-AT-06 | Source menteuse + 2 sources honnêtes | Quorum honnête, `confidence=medium`. |
| EG-AT-07 | Source 429 | Backoff, exclue, quorum avec les autres. |
| EG-AT-08 | `family=6` sans connectivité v6 | `verified=false`, raison explicite, pas de fallback silencieux en v4. |

## 10. Hors périmètre

- Géolocalisation certifiée (les champs pays/ville/ASN sont consultatifs).
- Test de débit ou de qualité du lien.
- Appel à l'API du fournisseur de modèles (quota).
- Vérification de l'IP de l'hôte public avant tunnel (hors capture d'underlay).
