# Présélection des IP/serveurs et rotation anticipée — recherche & design

> 29/10/2026. Recherche live : API NordVPN, API de contrôle gluetun, DB de production
> (`logs/requests.db`, 72 h), code du proxy. Outil de rapport :
> `scripts/ip_preselection.py`. Aucun changement de comportement dans ce document.

## 1. Réponse courte

Les deux idées sont **bonnes et faisables**, mais il faut corriger une hypothèse :

- **L'API NordVPN ne donne pas la vitesse d'une IP.** Elle donne la **charge
  agrégée** du serveur (`load`), son `hostname`, son IP d'**entrée** et son statut.
  C'est un **prior**, pas une mesure.
- **L'IP publique vue par l'upstream n'est pas l'IP d'entrée de l'API.** Preuve live :
  s1 public `130.195.218.134` ← `nl1050.nordvpn.com` (entrée `154.47.21.114`) ;
  s6 public `187.14.109.233` ← `fr1331.nordvpn.com` (entrée `187.14.109.225`).
  Sur 250 serveurs API, **0** correspondance avec les 256 IP mesurées en DB.
  Joindre les deux est donc **faux**.
- **L'unité de présélection doit être le `hostname` du serveur**, pas l'IP de l'API.
  L'IP publique reste la mesure de qualité ; le hostname est la clé de décision.
- **La rotation anticipée sur perte de débit est possible** : le proxy a déjà le
  registre qualité par IP, le moteur de rotation latence (TTFB, tendance, anti-flap)
  et le pin de hostnames par l'API de contrôle. Il manque le **câblage** qualité →
  rotation et l'**attribution hostname** des mesures.
- **Le meilleur signal de qualité reste la mesure locale en tok/s** (6× d'écart
  entre IP), complétée par le `load` NordVPN comme prior pour les serveurs jamais vus.

## 2. Ce que les API exposent réellement

### 2.1 API NordVPN (publique, sans auth, non officielle)

Endpoints utiles :

| Endpoint | Ce qu'il donne | Ce qu'il ne donne pas |
|---|---|---|
| `GET /v1/servers?limit=…&filters[country_id]=…` | `id`, `name`, `hostname`, `station` (IP d'entrée), `ips[]`, `load` (0–100), `status`, technologies, locations | latence, tok/s, débit, perte, jitter |
| `GET /v1/servers/recommendations?filters[country_id]=…&filters[servers_technologies][identifier]=openvpn_udp&limit=50` | serveurs triés par score interne, avec `load`, `hostname`, `station`, `ips` | mesure réelle de débit |
| `GET /v1/servers/countries` | `id` pays → noms | — |

Exemple live (NL) : `nl1263.nordvpn.com`, `load=10`, `station=186.247.163.28`,
`ips[0].ip=186.247.163.28`. **`station` et `ips` sont des IP d'entrée** ; l'IP
publique de sortie peut être dans un autre /24 (NAT NordVPN).

Analyse live : **250 serveurs online** dans les 5 pays configurés, `load` de **8 à 40**
(médiane 17) ; 0 serveur à `load` > 80. Le `load` est **agrégé** (tous clients,
toutes technos) et **rafraîchi lentement** ; il ne reflète ni la route vers
l'upstream gratuit, ni la charge de l'API opencode.

**Conclusion** : le `load` est un excellent **filtre de départ** (déjà utilisé par
`_least_loaded_hostnames`), pas une mesure de qualité.

### 2.2 API de contrôle gluetun (locale, port 8000, non publiée)

Documentation : `qdm12/gluetun-wiki/setup/advanced/control-server.md`.

| Route | Usage |
|---|---|
| `GET /v1/vpn/status` | `running` / `stopped` |
| `GET /v1/publicip/ip` | IP publique réelle (egress) |
| `PUT /v1/vpn/settings` | `server_selection` : `countries`, `hostnames` → **pin serveur** |
| `GET /v1/vpn/settings` | **à ne jamais appeler** : expose les credentials |

**Pas de statistiques par IP/serveur** dans l'API de contrôle. Elle sert à
**choisir** le serveur (hostnames) et à lire l'IP publique, pas à mesurer.
Le container possède aussi une liste de serveurs (`/gluetun/servers/*.json`,
`format-servers`) avec hostname/ville/protocoles, **sans `load` ni débit**.

### 2.3 Nos propres mesures (la vérité terrain)

`logs/requests.db` contient déjà : `free_model_ip` (IP publique réelle),
`station`, `tokens_output`, `duration_ms`, `ttfb_ms`, `success`, `stop_reason`,
`aborted`, `timestamp` (UTC ISO `T…Z`).

Fenêtre 72 h : **256 IP publiques**, **28 414 requêtes** exploitables.
Top IP mesurées :

| IP publique | n | tok/s | TTFB |
|---|---:|---:|---:|
| 186.247.161.97 | 182 | **99,7** | — |
| 176.223.172.212 | 131 | **98,2** | — |
| 187.13.199.245 | 225 | **93,3** | — |
| 187.13.8.145 | 84 | **92,4** | — |
| 187.40.49.127 | 188 | **88,6** | — |
| 186.247.163.225 | 222 | **83,7** | — |

À l'inverse, des IP tournent à **5–20 tok/s** ; l'écart est de **6×**. C'est
exactement le signal à utiliser pour présélectionner. **Mais** la DB ne stocke
pas le `hostname` du serveur : impossible de relier une IP publique à un
`hostname` a posteriori. **C'est le trou à combler en premier.**

## 3. Ce que le code fait déjà (à réutiliser, pas à réinventer)

| Élément | Fichier | État |
|---|---|---|
| Fetch `load` + top-K hostnames | `vpn/manager.py:5910` (`_fetch_nord_loads`) | actif, cache 300 s, filtre techno |
| Exclusion blacklist + serveurs récents | `vpn/manager.py:5963` (`_least_loaded_hostnames`) | actif |
| Pin hostnames via control API | `vpn/manager.py:5083` (`_control_pin_country_gated`) | actif ; fallback pays |
| Qualité par IP (tok/s, TTFB, succès, slow lane) | `free/quality.py:184` (`observe_completion`), `:234` (`is_slow_lane`) | actif |
| Moteur latence : soft/hard, tendance prédictive, anti-flap, global_degraded | `free/rotation.py:483`, `:846`, `:879` | **partiellement câblé** : marque des cooldowns, mais aucun déclencheur de rotation qualité |
| Sonde réseau (GET statique) | `free/probe.py` | actif, périodique, ne mesure pas l'upstream |
| Prewarm connexion | `opencode.py:18811` | actif (TCP/TLS, pas de qualité) |
| Hook fin de flux | `opencode.py:11014` (`_record_free_completion`) | actif |

**Le point clé** : le moteur latence `record_request()` retourne `soft`/`hard`,
mais le retour n'est **pas** suivi d'une rotation ; le pool filtre seulement les
IP `hard` (`free/pool.py:815`). Le slow lane qualité met le score à 0 et évite
l'IP, **mais ne change pas l'IP**. On ne rotationne aujourd'hui que sur **quota
(460)** ou **429** — jamais sur **perte de débit**.

## 4. Design proposé

### 4.1 Attribution : `server_host` (Phase 0, prérequis)

1. Le manager connaît déjà le hostname réel via les logs gluetun
   (`_extract_current_hostname`, `_current_hostname`). Au moment du pin, les
   hostnames candidats sont connus ; après connexion, le dernier
   `[host] Peer Connection Initiated` est la vérité.
2. Ajouter `station.current_host` (cache) mis à jour :
   - au pin réussi (premier hostname candidat, puis confirmation logs) ;
   - au scan de logs (dernier hostname).
3. Ajouter la colonne `server_host TEXT` à `requests` (ALTER au boot, comme
   `ttfb_ms`/`stop_reason`) et l'ajouter à `_INSERT_REQUESTS_SQL`.
4. `_record_free_completion` transmet `server_host=getattr(station, "current_host", None)`.
5. Le registre qualité garde `(station, ip)` **et** un index `host → qualité`
   (EWMA tok/s, TTFB, succès, n, dernier vu).
6. Rebuild au boot depuis la DB (72 h) : `GROUP BY server_host, free_model_ip` ;
   pas de nouveau fichier d'état. Les lignes sans hostname restent IP-only.

Sans cette phase, toute présélection par qualité est **impossible** : on ne peut
pas relier l'IP publique mesurée au serveur choisi.

### 4.2 Ledger serveur et tiers

Pour chaque `hostname` :

```
quality = {
  n, tps_ewma, ttfb_ewma, success_rate, last_seen,
  load_prior,          # API NordVPN, cache 300 s
  tier: A | B | C
}
```

Règles proposées (configurables) :

| Tier | Condition | Usage |
|---|---|---|
| **A — prouvé rapide** | `n ≥ 3` et `tps ≥ 50` et pas slow lane | **en premier** |
| **B — prometteur** | `n < 3` et `load ≤ 30` | ensuite, par `load` croissant |
| **C — lent connu** | `n ≥ 3` et `tps < 20` ou slow lane | **seulement** si A/B épuisés |

Exclusions : `status != online`, `load > 80`, blacklist AUTH/TLS, serveur récent
(`_recent_servers`), IP récente (`recent_ip_window`), pays hors périmètre.

**Diversité** : ne pas attribuer le même hostname à deux stations simultanément ;
le registre `_recent_servers` est déjà partagé.

**Quotas par tier** (la partie « utiliser les meilleures d'abord, puis les moins
bonnes ») :

| Tier | Quota avant rotation |
|---|---:|
| A | 460 (quota actuel) |
| B | 250 (on teste sans immobiliser 460 requêtes) |
| C | 60 (fallback court) |

Ainsi, une IP A rapide sert longtemps ; une IP B est testée sur 250 requêtes ;
une IP C n'est utilisée qu'en dernier recours et brièvement.

### 4.3 Sélection du prochain hostname

Pipeline (dans `_least_loaded_hostnames`, avec un callback qualité du pool) :

```
candidats API (pays, openvpn_udp, online)
  → exclure blacklist / récents / load > max
  → joindre quality[hostname]
  → tier A : trier tps desc, load asc
  → tier B : trier load asc
  → tier C : trier tps desc (le moins mauvais)
  → prendre top-K (5) → PUT server_selection.hostnames
  → connecter → confirmer le hostname réel dans les logs
```

Le pin actuel envoie **top-K** hostnames et laisse gluetun choisir ; c'est bien.
La différence est l'**ordre** : aujourd'hui `load` seul ; demain `tier/tps` puis
`load`. Fallback intégral vers `load` si la qualité est froide ou l'API en échec.

### 4.4 Rotation anticipée (le cœur de la demande)

Déclencheurs (évalués à chaque fin de flux, après `observe_completion`) :

1. **Débit bas** : `tps_ewma < 20` après **3** échantillons
   (`slow_lane_min_samples`) et `station.request_count ≥ 10`.
2. **Tendance** : baisse > **25 %** du `tps_ewma` sur les 5 dernières mesures,
   même si encore > 20 (le moteur a déjà `predictive_trend_pct` pour le TTFB ;
   on ajoute le même signal sur le débit).
3. **TTFB** : `ttfb_ewma > 8 000 ms` après 3 mesures (déjà marqué par le moteur).

Garde-fous **indispensables** :

- `LatencyRotationEngine.can_soft_rotate(sid, total)` : max **6 rotations/h/station**,
  pause globale si ≥ 50 % des stations ont tourné en 10 min.
- **Pas de rotation si toute la flotte est lente** : si ≥ 2 stations actives sont
  sous le plancher, c'est l'upstream/le client, pas l'IP ; rotationner brûlerait
  des IP et du quota d'auth.
- **Pas de rotation** pendant un événement amont (`_upstream_event_active`).
- **Minimum 10 requêtes** sur l'IP (évite de juger sur le warm-up).
- **Compte** : plafond de sessions NordVPN (4 aujourd'hui) ; une rotation qui
  échoue en AUTH ne doit pas boucler (déjà corrigé).
- **Coût** : chaque rotation = 1 nouvelle IP + auth + 1 prewarm ; le gain est de
  remplacer une IP à 10 tok/s après 10 requêtes au lieu de 460.

Action : `pool._launch_rotation(station, priority=0)` ; le pin choisit le prochain
hostname par le pipeline 4.3. Le moteur `_note_rotation`/`on_rotation_done` remet
les compteurs à zéro pour la nouvelle IP.

### 4.5 Qualification après rotation (option recommandée)

Le `prewarm` actuel ne teste que TCP/TLS. Pour éviter de servir 10 requêtes
lentes avant de détecter une mauvaise IP :

- après connexion, envoyer **une complétion minuscule** (`max_tokens` 1–4) via la
  station, sur le même endpoint gratuit ;
- mesurer **TTFB + tok/s** ;
- si `ttfb > 8 s` ou `tps < 15` : marquer le hostname **C**, tenter le **candidat
  suivant** (max 2 essais), puis servir ;
- coût : 1–2 requêtes par rotation, ~6 rotations/h/station max → négligeable
  devant 500 requêtes/IP ; le quota de l'IP est entamé de 1–2.

Ne pas utiliser `robots.txt` seul : il mesure le réseau, pas la vitesse de
génération de l'upstream. La sonde réseau existante reste utile pour le TTFB
connexion, pas pour le tok/s.

## 5. Preuves live (29/10)

- API : 250 serveurs online, `load` 8–40, médiane 17.
- DB 72 h : 256 IP, 28 414 requêtes ; top 99,7 / 98,2 / 93,3 tok/s ; bas 5–20.
- Appariement API `station` ↔ DB `free_model_ip` : **0/250**.
- Mapping courant :
  - s1 : public `130.195.218.134` ← `nl1050.nordvpn.com` (entrée `154.47.21.114`)
  - s6 : public `187.14.109.233` ← `fr1331.nordvpn.com` (entrée `187.14.109.225`)
  - s2 : public `187.15.111.67` (hostname absent du tail de logs)
  - s5 : public `5.253.115.76` (hostname absent du tail)
- Qualité live : s1 65,4 ; s2 53,6 ; s5 50,5 ; s6 33,6 tok/s ; 4 stations actives
  (s3/s4 exclues, plafond de sessions du compte).

## 6. Plan d'implémentation

| Phase | Contenu | Risque | Valeur |
|---|---|---|---|
| **0** | `server_host` DB + `station.current_host` + index qualité host + rebuild DB | faible (observation seule) | **prérequis** |
| **1** | Rapport `scripts/ip_preselection.py` enrichi (host, tier, top IP) | nul | valide les seuils |
| **2** | Tri `_least_loaded_hostnames` par tier/tps + callback qualité | moyen (fallback load) | utilise les meilleurs serveurs |
| **3** | Déclencheur rotation qualité/tendance + garde-fous + config | moyen (rotations supplémentaires) | **rotation avant quota** |
| **4** | Qualification post-rotation (1–4 tokens, 2 essais) | moyen (quota + auth) | évite les mauvaises IP |

Config proposée (hot-reloadable) :

```yaml
ip_rotation:
  quality_preselection:
    enabled: true
    fast_tps: 50.0
    slow_tps: 20.0
    max_load: 80
    tier_b_quota: 250
    tier_c_quota: 60
  quality_rotation:
    enabled: true
    min_requests: 10
    min_samples: 3
    trend_pct: 25.0
    ttfb_ceiling_ms: 8000
    max_per_hour: 6
    global_slow_guard: 2
  post_rotation_probe:
    enabled: false
    max_attempts: 2
    max_tokens: 4
```

## 7. Risques et limites

- **API non officielle** : peut changer/rate-limiter ; cache 300 s, fallback load,
  jamais bloquant.
- **`load` ≠ débit** : ne jamais l'utiliser seul pour exclure un serveur ; c'est
  un prior pour les inconnus.
- **IP d'entrée ≠ IP publique** : ne pas joindre les deux ; le hostname est la clé.
- **Plafond de sessions** : 4 tunnels simultanés sur le compte ; une rotation
  supplémentaire peut AUTH_FAILED ; l'anti-flap et le garde-fou global sont
  obligatoires.
- **Rotations supplémentaires** : plus d'IP consommées et plus d'auth ; plafonner
  à 6/h/station et 50 % de flotte.
- **Upstream global** : si toutes les stations sont lentes, rotationner ne sert à
  rien ; le garde-fou `global_slow_guard` évite la tempête.
- **Mesure** : tok/s dépend du modèle, de la longueur de sortie et du prompt-cache ;
  comparer à modèle comparable, avec `n ≥ 3`, et privilégier l'EWMA.
- **Pas de gain RPM garanti** : à 12 rpm, le goulot est le client/upstream ; le
  gain attendu est la **latence et la qualité**, pas le plafond RPM.

## 8. Décision recommandée

1. **Phase 0 maintenant** : attribuer le hostname aux mesures ; c'est le
   prérequis et c'est sans risque.
2. **Phase 2 ensuite** : trier les candidats par qualité mesurée + load, avec
   fallback ; on utilise les meilleurs serveurs d'abord.
3. **Phase 3 derrière un flag** : rotation anticipée sur débit/tendance, avec
   anti-flap et garde-fou global ; c'est la demande « ne pas attendre le quota ».
4. **Phase 4 optionnelle** : qualification post-rotation si les rotations
   supplémentaires restent dans le budget du compte.

Le prototype `scripts/ip_preselection.py` montre déjà le problème d'attribution
(0/250) et la distribution de qualité ; il servira à valider les seuils avant
d'activer la Phase 2/3.

## 9. Implémentation GUI (29/10/2026)

Les réglages sont désormais **exposés dans l'interface** : page *Free Models* →
section **« IP quality & early rotation »** (`VpnAdvanced.svelte`). Chaque champ
est sauvegardé immédiatement via `POST /api/vpn-config` (blocs imbriqués
fusionnés, pas de perte des autres clés), persisté dans `config.yaml` et
rechargé à chaud.

| Bloc `ip_rotation` | Clés GUI | Effet réel |
|---|---|---|
| `quality_preselection` | `enabled`, `fast_tps`, `slow_tps`, `max_load`, `tier_b_quota`, `tier_c_quota` | Tri des hostnames candidats (A rapide → B inconnu/moyen → C lent), plafond de charge API, quota par tier. Efficace en **OpenVPN** (pin de hostnames) ; en WireGuard gluetun choisit le pair, mais la qualité reste mesurée par IP. |
| `quality_rotation` | `enabled`, `min_requests`, `min_samples`, `trend_pct`, `ttfb_ceiling_ms`, `max_per_hour`, `global_slow_guard` | Rotation **avant** le quota si tok/s < plancher, TTFB > plafond ou tendance en baisse ; garde-fous min./cooldown/anti-flap/flotte lente. |
| `post_rotation_probe` | `enabled`, `max_attempts`, `max_tokens` | **Expérimental** : persisté pour la Phase 4, pas encore branché (la sonde upstream 1–4 tokens reste à implémenter). |

**Attribution hostname** : les logs OpenVPN donnent directement le hostname ;
WireGuard ne loggue que l'IP de peering. Une carte complète `entry IP → hostname`
est donc construite depuis `GET /v1/servers?limit=8000` (≤ 1×/h) et complète les
mesures. Vérifié en live : s1 `fr957`, s2 `ch485`, s5 `de1618`, s6 `de1217`.

**État par défaut** : présélection active, rotation anticipée **désactivée**
(l'opérateur l'active dans la GUI), sonde désactivée. La rotation qualité reste
indépendante de `latency_rotation.enabled` : elle possède son propre cooldown
par station (600 s) et son plafond horaire.
