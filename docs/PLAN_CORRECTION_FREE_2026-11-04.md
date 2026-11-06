# Plan de correction — free sans credential personnel

**Date** : 2026-11-04
**Objectif final** : six stations client-utilisables en free, sans compte ni
credential personnel.
**Base** : audit du code + `docs/AUDIT_SYSTEME_FREE_2026-11-04.md` +
`docs/OPENCODE_FREE_ECOSYSTEM_2026-11-04.md`.

---

## 0. Résumé exécutif

Le projet a déjà la bonne intuition (identité par IP, ledger, allocateur). Le
format a été aligné sur la capture du client officiel 2.2.5 (en-têtes, corps,
13 outils, `max_completion_tokens`), et la cause racine des 429 directs a été
identifiée : le gate décode le préfixe 12-hex de `ses_` comme un timestamp
descendant. Un `ses_` aléatoire est rejeté avec `FreeUsageLimitError` même sur
une IP qui a du quota ; le client officiel et le minter de production utilisent
`descending()`.

Il reste deux chantiers :

1. **Session ID** : le harnais `scripts/test_free_egress.py` utilisait un
   préfixe aléatoire — corrigé ; la régression `scripts/test_session_id.py`
   verrouille le format.
2. **Egress** : les six stations ont été rebindées sur le pool
   `87.120.102.0/24` (six egress distincts, 12/12 requêtes 200). Le bug
   Compose des stations 5–10 et le transport OpenVPN de la station 1 sont
   corrigés. Le reste du pool reste à explorer pour des spares.

---

## 1. Audit : ce qui est bon, ce qui manque

| Dimension | État actuel | Écart | Fichier |
|---|---|---|---|
| Session `ses_` | `_mint("ses", "descending")`, longueur 30 vérifiée | conforme | `free/identities.py:325,369` |
| `x-opencode-request` | envoyé | **doit être absent en v2** | `free/identities.py:475`, `opencode.py:8955,8995` |
| `x-opencode-client` | `desktop` | **`cli` pour le chemin anonyme** | `free/identities.py:473`, `ROTATION_VERSIONS:142` |
| UA | `opencode/1.19.5 ...` puis spec 2.2.5 `desktop` | **`opencode/latest/2.2.5/cli`** | `free/identities.py:89`, `free/impersonation.json` |
| `b3` / `traceparent` | ajoutés par `apply_spec` | conforme | `free/impersonation.py` |
| `x-session-affinity` / `X-Session-Id` | ajoutés par `apply_spec` | conforme | `free/impersonation.py` |
| Corps 2.2.5 | `official_payload.json` = 13 outils, `<env>` | conforme | `free/official_payload.json` |
| `max_completion_tokens` | le builder émet `max_tokens` | **utiliser `max_completion_tokens`** | `opencode.py:6737,7871` |
| TLS Bun | `_get_free_fp_kwargs` | conforme, à vérifier après changements | `opencode.py` |
| Auth | `Bearer st_...` via `.env` | **retirer le token du chemin free** | `free/console_auth.py`, `.env` |
| Quota | ledger par IP + jour | OK, mais pas de dimension identité/IPv6 | `free/quota_ledger.py:211` |
| 403 vs 429 | `model_usable` confond les deux | **séparer forme et quota** | `free/pool.py`, `free/station_health.py` |
| Egress | 6 IP VPN datacenter | **aucun egress résidentiel/mobile/IPv6** | `config.yaml`, `docker-compose.yml` |
| Probes | chaînes sans budget, brûlent le quota | **budget + arrêt sur 429** | `scripts/fresh_chain.py` |
| Réparation | détruit la station avant de prouver | **pré-qualifier, MTTR** | `scripts/fresh_chain.py:874` |
| Observabilité | SLO par station | **SLO par egress/IP** | `scripts/station_slo_watch.py` |

---

## 2. Architecture cible

```text
client ─► proxy free ─► identité v2 ─► egress pool ─► gate
                            │                │
                    session canonique   ledger (egress_ip, jour, modèle)
                    UA/client cli       quota + cooldown
                    tools + <env>       IPv4 / IPv6 séparés
                            │
                    transport au choix :
                    A. wire curl_cffi (Bun TLS)
                    B. opencode serve (vrai client)
```

Deux classes d'egress :

- **datacenter** : les six conteneurs actuels, et toute IP datacenter externe ;
- **résidentiel/mobile** : HTTP/SOCKS, WARP, IPv6.

Aucun fallback local (Ollama, LM Studio, vLLM) n'est prévu : ce plan ne
l'utilise pas et ne le proposera pas.

Un egress n'est pas une station : plusieurs stations peuvent partager un egress
externe, et une station peut changer d'egress sans être recréée.

### 2.1 Les IP datacenter ne sont pas exclues

Le plan ne classe pas les datacenter comme « mauvaises ». Elles sont un egress
de première classe, simplement plus susceptibles d'être flagged. La règle est :
**tester chaque IP, garder celles qui rendent 200, et les utiliser en priorité
tant qu'elles ont du quota.**

- `Egress.type = "datacenter"`, avec `asn`, `range`, `shared_score`.
- Score de réputation : taux de 200 / 429 / 403 sur fenêtre glissante,
  fraîcheur, latence, headroom.
- Un 429 sur une IP datacenter ne condamne pas le range ; un 403 de forme
  condamne la requête, pas l'IP.
- Les datacenter sont préférés aux egress résidentiels **quand ils passent**
  (latence, coût, débit) ; le résidentiel est le repli, pas l'inverse.
- Diversité : plusieurs ASN/ranges, IPv4 et IPv6, plusieurs préfixes.
- Objectif : **part de trafic datacenter maximisée**, sans jamais exclure une
  IP sur une série d'échecs.
- Un datacenter qui ne rend que des 429 passe en `COOLING` avec son reset ;
  il est re-testé après échéance, pas retiré définitivement.

### 2.2 Cause racine des 429 : le `ses_` temporel (2026-11-04 16:30)

Le gate free ne compte pas les requêtes d'un `ses_` arbitraire : il décode les
12 premiers hex comme le résultat de `descending()`
(`packages/schema/src/identifier.ts`) :

```text
current = Date.now() * 0x1000 + counter
value   = ~current                 (complément à 48 bits)
prefix  = 6 octets hex de value
session = "ses_" + prefix + 14 base62
```

Un préfixe aléatoire (`secrets.token_hex(6)`) sort de la fenêtre temporelle et
le gate renvoie `FreeUsageLimitError` + `Retry-After` jusqu'à 00:00 UTC. Ce
n'est pas un quota épuisé : c'est un **format de session invalide**.

Preuve mesurée (2026-11-04 16:19–16:30 UTC) :

| Test | Session | Résultat |
|---|---|---|
| client officiel 2.2.5 direct | mintée par le client | **200** |
| client officiel via MITM | mintée par le client | 429 (transport modifié) |
| replay Bun, session aléatoire | `secrets.token_hex(6)` | 429 |
| replay Bun, préfixe officiel + suffixe neuf | `ses_e58a47718ffe…` | **200** |
| replay Bun, session mintée `descending()` | `ses_e589f124effe…` | **200** |
| même session, 5 requêtes | idem | **200 × 5** |
| session mintée il y a 35 min | idem | **200** |

Le client officiel et le minter `opencode._mint_oc_id("ses", "descending")`
sont conformes ; seul le harnais de test utilisait un préfixe aléatoire. Les
429 du harnais ne sont donc **pas** une mesure du quota datacenter.

**Axe de quota réel** : le free est météré par **bloc d'egress** — un `/32` en
IPv4, un `/64` en IPv6 — avec reset quotidien à **00:00 UTC**
(`opencode-unlimited-proxy`, mesuré le 2026-10-27). Six stations IPv4 = six
`/32`, donc six buckets, pas un partage. Un VPS avec un `/64` routé peut
fournir des centaines de buckets IPv6, chacun avec son propre quota.

**État mesuré le 2026-11-04 (sessions valides uniquement)** :

- IP résidentielle directe `37.69.230.71` → **200** avec une session mintée ;
- six stations, cinq egress distincts (`187.13.20.133`, `187.13.210.165`,
  `186.247.66.3`, `187.14.124.220`, `187.40.254.179`) → **429** avec la même
  session mintée ; station 1 en timeout ;
- les cycles `fresh_chain` / `test_selection_cycle` (sessions valides) ont
  testé 11 hostnames sur au moins 5 `/24` → 429 / no-egress.

Les 429 des stations sont donc réels au niveau IP à cet instant, mais le
harnais `test_free_egress.py` (session aléatoire) ne doit plus servir de
preuve. Politique : `residential_first` tant que le taux de 200 datacenter
reste < 10 %, datacenter en best-effort, re-test d'un `/32` après reset.

### 2.3 Capture de test mintée (workflow)

- `free/captures/2.2.5.json` : capture-template (corps + ordre exact des
  en-têtes du client 2.2.5). Le `ses_` qu'elle contient est un exemple ; le
  harnais ne le réutilise pas par défaut.
- `scripts/mint_capture.py` : re-mint un `ses_` neuf (`descending()`), met à
  jour la capture, archive l'ancienne dans `free/captures/archive/`. Options :
  `--verify` (envoi réel), `--model`, `--from-raw` pour amorcer depuis un
  corps capturé.
- `scripts/test_free_egress.py` : lit la capture et re-mint un `ses_` **par
  requête** ; les quatre en-têtes (`x-opencode-session`,
  `x-opencode-session-id`, `x-session-affinity`, `x-session-id`) reçoivent le
  même ID neuf, et `b3`/`traceparent` sont régénérés. `--repeat N` produit N
  requêtes avec N IDs différents ; `--reuse-session` réutilise l'ID de la
  capture ; `--mint` re-mint la capture avant les tests.
- Un `ses_` aléatoire est un faux quota : ne jamais conclure sur un 429 sans
  vérifier que le préfixe 12-hex décode à l'instant présent
  (`scripts/test_session_id.py`).

### 2.4 Retest des IP datacenter « non fonctionnelles » (2026-11-04 ~17:10 UTC)

Toutes les requêtes utilisent `free/captures/2.2.5.json` et un `ses_` neuf par
requête. Résultats détaillés : `logs/datacenter_retest_2026-11-04.json`.

| Station | Hostname | Pays | Egress | Statut |
|---|---|---|---|---|
| 2 | `us14166.nordvpn.com` | United States | `187.14.235.240` | **429** |
| 3 | `au1103.nordvpn.com` | Australia | — | no-egress |
| 4 | `bb4.nordvpn.com` | Barbados | — | no-egress |
| 5 | `se-uk4.nordvpn.com` | United Kingdom | — | no-egress |
| 6 | `bw2.nordvpn.com` | Botswana | `186.247.72.118` | **429** |
| 2 | `au1101.nordvpn.com` | Australia | — | no-egress |
| 3 | `uk2020.nordvpn.com` | United Kingdom | `185.216.146.135` | **429** |
| 4 | `pl296.nordvpn.com` | Poland | `187.15.160.13` | **429** |
| 5 | `de1607.nordvpn.com` | Germany | `187.15.143.135` | **429** |
| 6 | `jp847.nordvpn.com` | Japan | `2.56.190.36` | SSL error |

Bilan : 10 hostnames, 6 egress obtenus sur 6 `/24` distincts, **0 × 200**,
5 × 429, 4 no-egress, 1 erreur SSL. Test complémentaire sur l'egress
`185.216.146.135` (station 3) : `big-pickle`, `glm-5.3-flash-free`,
`mimo-v2.6-flash-free` → 429 ; `step-5-preview-free` → 400
« Model is unavailable ». Le refus n'est donc pas lié à un modèle. Les 429 ne
sont plus imputables au format de session : à cet instant, ces IP datacenter
sont refusées. L'IP résidentielle directe reste **200**. C'est un constat par
IP, pas une conclusion sur la classe ; la suite est l'exploration d'autres
pools ou d'un egress résidentiel/mobile.

### 2.5 IP NordVPN trouvées et six stations validées (2026-11-04 ~17:40 UTC)

Le pool **`87.120.102.0/24`** (Albanie) accepte le free anonyme avec un `ses_`
minté par `descending()`. Six stations, six egress distincts, **12/12
requêtes 200** (deux sessions neuves par station) :

| Station | Hostname | Egress | Transport |
|---|---|---|---|
| 1 | `al46.nordvpn.com` | `87.120.102.220` | WireGuard |
| 2 | `al73.nordvpn.com` | `87.120.102.45` | WireGuard |
| 3 | `al37.nordvpn.com` | `87.120.102.23` | WireGuard |
| 4 | `al36.nordvpn.com` | `87.120.102.14` | WireGuard |
| 5 | `al41.nordvpn.com` | `87.120.102.84` | WireGuard |
| 6 | `al45.nordvpn.com` | `87.120.102.200` | WireGuard |
| 7 | `al46.nordvpn.com` | `87.120.102.218` | WireGuard (healthcheck DNS KO, SOCKS5 OK) |

Validation proxy `:4000` : **200**, `STATUS_OK`, `billing_source=free`.
Après rotation des stations 3 et 1 (station 3 : `al75`/`87.120.102.72`
épuisée après 107 requêtes → `al37`/`.23` ; station 1 : `al74`/`.54`
épuisée → `al46`/`.220`) : **6/6 stations 200** via SOCKS5. La 7e station
(`al46`/`.218`) rend **2/2 200** ; elle est marquée `unhealthy` par le
healthcheck DNS mais son SOCKS5/HTTP fonctionne.

Deux bugs corrigés au passage :

- `docker-compose.yml` : les services `vpn-gluetun-5..10` n'exposaient pas
  `SERVER_HOSTNAMES_STATIONn` / `SERVER_COUNTRIES_STATIONn` ; les rebinds des
  stations 5/6 étaient ignorés (elles restaient sur la liste pays globale →
  187.x/185.x 429). Les services 5 à 10 ont maintenant les variables.
- station 1 : `VPN_TYPE_STATION1=wireguard` (OpenVPN TCP vers `al46:443`
  était reset). `.env` pinne les six hostnames ; les conteneurs sont alignés.

Les hostnames `al36/al41/al45/al73/al74/al75` ne sont plus dans le snapshot API
7 172, mais Gluetun les résout encore vers `87.120.102.x` (manifeste runtime
9 786). Le pool contient d'autres IPs : si une IP s'épuise, prendre un autre
hostname Albanie (`al37`, `al46`, `al74`…) et retester.

---

## 3. Les lots

### Lot 0 — Format v2 et sortie du credential personnel (1–2 jours)

**But** : que la requête free soit indiscernable d'un client 2.2.5, sans token.

- Ajouter `2.2.5` à `VERSION_SPECS` : UA `opencode/latest/2.2.5/cli`,
  `client="cli"`, `session_id=True`, Bun 1.4.4.
- Supprimer `x-opencode-request` des deux builders
  (`free/identities.py:475`, `opencode.py:8955,8995`).
- Passer `x-opencode-client` à `cli` pour le chemin anonyme.
- `max_completion_tokens: 32000` dans le wire builder, plus `max_tokens`.
- Vérifier le bloc `<env>` et les schémas `read`/`shell` verbatim.
- Retirer `OPENCODE_CONSOLE_TOKEN`/`ORG` du `.env` ; `console_auth` devient
  opt-in explicite, jamais automatique.
- Unifier le probe et le chemin client sur le même builder.

**Critère d'acceptation** : une requête synthétique par station produit
exactement les en-têtes/corps de la capture 2.2.5 (hors IDs), avec un `ses_`
minté par `descending()` (préfixe temporel vérifié), et renvoie **429**
(quota IP) ou **200**, jamais **403** (forme). Un `ses_` aléatoire doit être
re-minté avant de conclure à un quota.

**Risque** : sans token, le service peut tomber à 429 tant qu'aucun egress
propre n'est branché. Le lot 1 doit être prêt en parallèle.

### Lot 1 — Egress pool (3–5 jours)

**But** : qualifier les datacenter existants, ajouter des egress, et maximiser
la part de trafic servie par des datacenter qui passent.

- Créer `free/egress.py` : `Egress` (id, type, adresse, proxy, santé, quota,
  dernier 200, dernier 429, `asn`, `range`, `shared_score`), avec types
  `datacenter`, `residential`, `mobile`, `ipv6`, `warp`, `client`.
- Ajouter un panneau de configuration (ou `config.yaml`) pour les egress
  externes ; le type `datacenter` accepte une IP ou un proxy.
- Router les requêtes free par egress, pas seulement par station.
- Ordre de test :
  1. **datacenter existants** : chaque station, chaque nouvelle IP, chaque
     range/ASN non testé ;
  2. **IPv6 datacenter** (buckets séparés) ;
  3. proxy résidentiel/mobile HTTP/SOCKS ;
  4. WARP ;
  5. `opencode serve` sur l'egress retenu.
- Conserver un datacenter qui rend 200 même s'il est minoritaire ; le préférer
  tant qu'il a du headroom.

**Critère d'acceptation** : au moins un egress **datacenter** et un egress
**externe** renvoient **200** avec `Bearer public` et le format v2 ; part
datacenter ≥ 50 % du trafic free quand elle passe ; six egress distincts à
terme.

**Risque** : un proxy résidentiel peut être lent, instable, ou voir le trafic ;
le ledger doit le mettre en cooldown, pas condamner la station. Un proxy
résidentiel est un coût récurrent : budget par Go et coupure automatique.

### Lot 2 — Ledger par IP et budget de probes (2–3 jours)

**But** : ne plus brûler le quota en réparant.

- Clé `(egress_ip, modèle, jour, identity)` ; IPv4 et IPv6 séparés ; reset
  minuit.
- Distinguer `403` (forme/origine) de `429` (quota) :
  - `403` → corriger le wire, ne pas marquer l'IP ;
  - `429` avec `Retry-After` → cooldown de l'IP, pas de la station ;
  - `429` sans 200 antérieur → mauvaise IP, quarantaine longue.
- Budget de probes : N/heure et N/jour ; **zéro probe** quand l'IP est en 429.
- Un probe de bind/egress ne doit pas consommer le gate ; seul le probe de
  validation finale touche le free.

**Critère d'acceptation** : 0 probe sur une IP en 429 ; 0 station marquée
« broken » à cause d'un 429 de forme ; ledger expose le quota restant.

### Lot 3 — Réparation et MTTR (3–5 jours)

**But** : ne plus perdre une station pour tester un candidat.

- Pré-qualifier un hostname (manifeste, transport, auth, egress) avant le
  recreate.
- Ne jamais détruire la dernière station servante.
- 2 réparations parallèles maximum, 8 candidats, budget de rebind par besoin.
- Supprimer le `settle_s` aveugle, réduire l'egress à une URL, supprimer le
  `--delay 8`.
- Un `429` de quota ne déclenche pas de rotation de station.

**Critère d'acceptation** : MTTR p50 < 60 s ; aucun épisode 0/6.

### Lot 4 — Observabilité et continuité (1–2 jours)

- SLO par egress et par station ; journal d'incidents.
- Dashboard : `egress_ip`, quota restant, `403` vs `429`, dernier 200.
- Alerte si `egress_disponibles < 2`.
- Bascule automatique vers l'egress suivant sur 429/403 de quota ; si aucun
  egress ne répond, le proxy renvoie un 429 propre avec `Retry-After`, sans
  fallback local.
- **Aucun fallback Ollama/LM Studio/vLLM** : la continuité repose uniquement
  sur la largeur du pool d'egress.

### Lot 5 — Spec et gouvernance

- Mettre à jour la constitution : « aucun credential personnel dans le chemin
  free », « quota par IP », « pas de paid/second compte/attente ».
- Mettre à jour la spec : SLO par egress, critères de sortie, tests de forme.
- Mettre à jour `tasks.md` avec les lots ci-dessus.

---

## 4. Matrice de tests

| Test | Egress | Transport | Attendu |
|---|---|---|---|
| T0a | station actuelle (datacenter) | wire v2 | 429, jamais 403 |
| T0b | **nouvelle IP datacenter** (autre hostname/range) | wire v2 | 200 si bucket libre |
| T0c | **datacenter IPv6** | wire v2 | 200 si bucket libre |
| T0d | direct, `ses_` aléatoire | wire v2 | **429 `FreeUsageLimitError` (faux quota)** |
| T0e | direct, `ses_` minté `descending()` | wire v2 | **200** |
| T1 | IP résidentielle directe | wire v2 | 200 ou 429 selon quota |
| T2 | proxy mobile/résidentiel | wire v2 | 200 si bucket libre |
| T3 | IPv6 station | wire v2 | 200 si bucket libre |
| T4 | station + `opencode serve` | vrai client | 200 ou 429 |
| T5 | WARP | wire v2 | 200 ou 429 |
| T6 | datacenter + `opencode serve` | vrai client | 200 ou 429 |

Le test décisif est **T0b/T1/T2** : si une IP datacenter fraîche, ou une IP
résidentielle propre, renvoie 200 avec `Bearer public`, le free sans compte est
prouvé. Si toutes renvoient 429, c'est le bucket de ces IP à cet instant ; on
passe à l'egress suivant, sans conclure sur l'ensemble.

---

## 5. Points de décision

- **G0** : format v2 validé (0 × 403 sur 20 requêtes).
- **G1** : au moins un egress externe 200.
- **G2** : six egress 200, quota ledger stable.
- **G3** : SLO ≥ 5/6, jamais 0/6.
- Si G1 échoue après 20 egress testés : le free OpenCode n'est pas accessible
  sans credential sur ce réseau ; documenter et continuer la recherche
  d'egress. Pas de fallback local, pas de compte personnel.

---

## 6. Ce qu'on ne fait pas

- Ne pas utiliser `OPENCODE_CONSOLE_TOKEN` dans le chemin free.
- Ne pas créer de comptes en masse.
- Ne pas traiter un 429 de quota comme une panne de station.
- Ne pas recréer une station pour tester un hostname non pré-qualifié.
- Ne pas conclure « aucune IP disponible » après une série d'échecs : tester
  l'egress suivant.
- **Ne pas utiliser de fallback local** (Ollama, LM Studio, vLLM) : jamais.

---

## 7. Ordre d'exécution conseillé

1. **Lot 0** (format) + tests T0a/T0b/T1 en parallèle.
2. **Lot 1** (egress) dès qu'un proxy résidentiel, une IP datacenter fraîche ou
   IPv6 est disponible.
3. **Lot 2** (ledger/budget) avant de relancer les chaînes.
4. **Lot 3** (MTTR) une fois le free stable.
5. **Lot 4** (observabilité) en permanence.
6. **Lot 5** (spec) au fil des lots.

---

## 8. Revue du plan

**Verdict** : bon sur la direction, pas encore optimal. Trois faiblesses
structurelles doivent être corrigées avant exécution.

| Critère | Verdict | Ce qui manque | Correction |
|---|---|---|---|
| **Fiable** | moyen | dépend d'un egress externe non identifié ; pas de mode ombre ; pas de calibration de l'unité de quota | mode ombre, pool d'egress, expérience de calibration |
| **Optimal** | perfectible | le vrai client est traité comme un transport égal alors qu'il ne passe pas les outils | matrice de transport par usage |
| **Qualitatif** | bon | pas de tests de contrat 403/429, pas de canary de format | tests + canary + sync script |
| **Performant** | bon | pas de budget p95/failover par egress | timeouts, hedge, p95 |
| **Maintenable** | bon | pas de source unique `egress`, pas de runbook | `free/egress.py`, flags, runbooks |

### 8.1 Fiabilité

- **Dépendance à l'egress** : G1 peut échouer. Le plan doit garder un pool
  d'egress redondant, pas un fallback local.
- **Mode ombre** : le nouveau chemin tourne en `observe` sur 10 % du trafic,
  compare 403/429/200 avec l'ancien, puis bascule. Aucun basculement à l'aveugle.
- **Pas de rollback token** : le token personnel est exclu ; le rollback est
  l'egress précédent, pas le compte.
- **Pas de fallback local** : Ollama/LM Studio/vLLM sont explicitement hors
  périmètre, définitivement.
- **Calibration du quota** : avant le lot 2, mesurer l'unité réelle :
  - même IP + même session ;
  - même IP + nouvelle session ;
  - nouvelle IP + même session.
  Trois requêtes par cas. Le résultat détermine la clé du ledger
  (IP seule, IP+session, ou IP+compte).
- **Garde 0/6** : aucun lot ne peut laisser la flotte sans egress servable ;
  si le nouveau chemin échoue, l'ancien reste actif jusqu'à G1.

### 8.2 Optimalité

Matrice de transport à ajouter au plan :

| Transport | Outils | Streaming | Gate d'origine | Usage |
|---|---|---|---|---|
| **wire v2** (`curl_cffi`) | oui | oui | contournable par reconstruction | chemin agentique principal |
| **vrai client** (`opencode serve`) | non (outils du client seulement) | oui | passe par construction | validation, chat, secours |

Le wire reste le transport principal parce que les clients demandent des outils.
Le vrai client est le **validateur de référence** et le secours ; il ne remplace
pas le wire pour l'agentique.

### 8.3 Qualitatif

Tests de contrat à ajouter :

- 20 requêtes par egress : compter 200 / 403 / 429, refuser un egress qui
  produit un 403 de forme.
- Vérifier la session : `^ses_[0-9a-f]{12}[0-9A-Za-z]{14}$`.
- Vérifier l'absence de `x-opencode-request`.
- Vérifier `x-opencode-client: cli`, UA `opencode/.../cli`.
- Vérifier `max_completion_tokens`, `stream_options`, `<env>`, outils
  `read`/`shell` verbatim.
- Canary quotidien : `opencode_client_sync.py --capture --diff` ; si le format
  change, le test échoue avant la production.

### 8.4 Performant

- Budget par egress : connect < 5 s, TTFB < 12 s, total < 30 s.
- Failover < 2 s entre deux egress.
- Supprimer le `settle_s` aveugle et le `--delay 8`.
- Hedge sur deux egress seulement si le premier dépasse 1,5 s.
- p50/p95 par egress et par station, pas seulement par station.

### 8.5 Maintenable

- Une seule abstraction `Egress` ; `free/pool.py` ne connaît que `Egress`.
- Flags : `free.enabled`, `free.transport=wire|client`,
  `free.egress_policy=datacenter_first|external_first`,
  `free.probe_budget_per_hour`, `free.probe_budget_per_day`.
- Runbooks : « 403 de forme », « 429 de quota », « egress mort », « aucun
  egress propre ».
- Logs structurés : `egress`, `station`, `status`, `quota`, `reset`, `cause`.
- Un tableau de bord : part datacenter, quota par egress, 403 vs 429, dernier
  200.

### 8.6 Sources d'egress à approvisionner

| Source | Coût | Confiance | Latence | Statut |
|---|---|---|---|---|
| Datacenter existants (6 stations) | déjà payé | moyenne | bonne | à qualifier |
| Nouvelles IP datacenter (autres hosts/ranges) | inclus | moyenne | bonne | à tester |
| IPv6 datacenter | inclus | moyenne | bonne | à activer |
| **VPS Linux avec `/64` IPv6 routé** | récurrent | bonne | bonne | **à provisionner** |
| Résidentiel/mobile HTTP/SOCKS | récurrent | à évaluer | variable | à acheter/tester |
| WARP | gratuit | faible | moyenne | à tester |
| `opencode serve` sur un egress | inclus | bonne | moyenne | à intégrer |

### 8.7 Coût, confiance et sécurité

- Un proxy résidentiel voit les métadonnées de connexion (CONNECT) ; le contenu
  reste TLS vers `opencode.ai`. Ne jamais y placer de secret en clair.
- Budget par Go et par requête ; coupure automatique au-delà.
- Ne pas journaliser les URLs de proxy avec credentials.
- Les IP datacenter gratuites restent préférées : coût nul, latence meilleure,
  et elles respectent « autant que faire se peut ».

**Conclusion** : après ces ajouts, le plan est fiable sous réserve d'une source
d'egress, qualitatif par les tests de contrat, performant par les budgets, et
maintenable par l'abstraction `Egress` et les runbooks. Il n'est pas « optimal »
au sens absolu : la ressource rare reste l'IP propre, et aucun code ne peut la
créer.

---

## 9. Journal des tests — 2026-11-04

| Test | Egress | Résultat |
|---|---|---|
| T0a | station 3, format v2, `Bearer public` | **429**, pas 403 |
| T0a | station 1, format v2, `Bearer public` | **429**, pas 403 |
| T0b | `187.13.20.15` (`au1101`) | **429** |
| T0b | `187.13.20.54` (`au1102`) | egress obtenu, non probé (limite `--max 2`) |
| T1 | IP résidentielle directe (`37.69.230.71`) | **429** |
| T0d | même IP, `cli` + **hash de projet** (`a24ad984…`), `Bearer public` | **200** (une requête réelle) |
| T0d | même IP après le 200 | **429** (bucket consommé) |
| T0e | station 3, `cli` + hash | **429** |
| T0e | station 1, `cli` + hash | timeout (tunnel/port) |
| T0f | cycle 11 hostnames, 8 pays, 4 types | **5 × 429 sur 5 `/24` distincts, 6 no-egress, 0 × 200** |
| T1 | Docker Desktop (même IP publique) | **429** |
| T1 | WSL2 Ubuntu 22.04 | NAT `192.168.31.0/24`, pas d'IPv6 public ; même egress hôte |
| IPv6 | aucune adresse publique sur l'hôte ni dans WSL2 | non testable |
| WARP | `api.cloudflareclient.com` | injoignable, `wgcf register` bloqué |
| Token console | station | **200**, exclu par la règle « sans credential personnel » |

**Conclusion** : le format `cli` + **hash de projet** + `Bearer public` a rendu
**200** sur l'IP résidentielle (`37.69.230.71`). `desktop` et `global` échouent.
L'IP n'est donc pas bloquée : le format était faux, et le 429 qui suit est le
bucket consommé par les tests. **G1 est atteint une fois** ; il faut arrêter les
probes et attendre le reset avant de valider les six stations.

Le quota free par `/32` est petit : les tests (client officiel + replays) l'ont
épuisé. La prochaine étape est de corriger le proxy (`cli` + hash), de ne plus
sonder, et de refaire un seul test après reset.

---

## 10. Scénario VPS datacenter (recommandé)

Le quota est par `/32` IPv4 ou `/64` IPv6. Un VPS Linux avec un **`/64` IPv6
routé** transforme une machine datacenter en des centaines de buckets.

1. VPS Linux (Debian/Ubuntu), Docker + Compose v2, préfixe IPv6 `/64` routé.
2. Déployer `ebouther/opencode-unlimited-proxy` :
   ```bash
   git clone https://github.com/ebouther/opencode-unlimited-proxy
   cd opencode-unlimited-proxy/stack
   cp .env.example .env
   # WARP_ACCEPT_TOS=1 et WARP_SOURCE_IPV6=<prefix>/64
   docker compose up -d
   ```
3. Vérifier :
   ```bash
   curl -s http://127.0.0.1:8080/healthz | jq .warp
   curl -s http://127.0.0.1:8080/v1/models -H 'Authorization: Bearer local-test-key'
   ```
4. Brancher le proxy : soit les clients pointent directement sur
   `http://<vps>:8080/v1`, soit la jambe free du proxy utilise ce gateway comme
   upstream (`baseURL`, clé `local-test-key`).
5. Si le VPS n'a pas de `/64`, utiliser le mode **proxies custom** :
   `warp.enabled: false`, liste de proxies HTTP/SOCKS, et ne démarrer que
   `config-init` + `opencode-engine`.

**Non supporté sur Windows** : le mode WARP par défaut exige
`network_mode: host`, UDP 2408 et un `/64` routé, que Docker Desktop ne fournit
pas. Le mode proxies custom reste possible.

**Alternative sans VPS** : brancher un proxy résidentiel/mobile dans
`scripts/test_free_egress.py --proxy …` et vérifier le 200 avant de l'ajouter au
pool.
