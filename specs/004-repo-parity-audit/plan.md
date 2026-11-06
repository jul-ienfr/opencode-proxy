# Plan — Audit, correctifs et état de la jambe free

**Feature**: `004-repo-parity-audit`  
**Spec**: [spec.md](./spec.md)  
**Research**: [research.md](./research.md)  
**Tasks**: [tasks.md](./tasks.md)  
**Mesures**: [measurements.md](./measurements.md)  
**Contrats**: [contracts/](./contracts/)

## 1. Architecture actuelle de la jambe free

```
client (OpenAI/Anthropic/Responses)
        │
        ▼
opencode.py — routeur ASGI, mapping de dialectes, pool de sessions curl_cffi
        │
        ├── free/identities.py      identité par IP (version, client, project,
        │                           ses_/msg_, ordre d'en-têtes)
        ├── free/impersonation.py   application de l'ordre et des IDs
        ├── free/official_payload  corps 2.2.5 (13 outils, prompt système)
        ├── free/quota_ledger.py   bucket (IP, jour UTC, bucket)
        ├── free/ip_quota.py       lease qualité/latence par (modèle, IP)
        └── free/pool.py           stations, rotation, ledger, cooldowns
        │
        ▼
curl_cffi 1.19.5 (BoringSSL, JA3 1523504b…) → opencode.ai
        │
        ├── mode direct : IP publique de l'hôte
        └── mode VPN    : gluetun / SOCKS5 par station
```

Le wire free est un **hybride assumé** :

- corps, ordre d'en-têtes et noms d'outils : capture 2.2.5 ;
- UA, `project`, session et TLS : profil 1.19.5 (TLS identique au 2.2.5).

Le gate actuel accepte cet hybride. La spec 003 visait un profil 2.2.5
complet ; ce plan documente l'écart au lieu de le masquer.

## 2. Changements appliqués pendant cette session

### 2.1 Placeholders d'outils — `opencode.py`

**Fonction**: `_free_official_placeholder_defs`.

| Avant | Après |
|---|---|
| `parameters: {"type":"object","properties":{}}` | `parameters: {"type":"object","properties":{"path":{"type":"string"}}}` |
| 13 outils non comptés si le gate exige des propriétés non vides | 13 outils comptés dans les deux régimes |

**Raison**: `xljiulang` mesure qu'un schéma vide ne compte pas dans le gate à
7 outils. Le coût est ~50 octets par outil. La grille reste sous 4 500 octets.

**Test**: `tests/test_free_body_conformite.py::test_official_placeholders_are_minimal_but_keep_names`.

### 2.2 Garde `<env>` — `opencode.py`

**Fonctions**: `_free_env_block_valid` (nouvelle), `_free_ensure_env_block`
(nouvelle), `_free_wire_body` (branchement).

Règles implémentées :

1. Le **premier** token `<env` trouvé dans un message système est le seul qui
   compte. S'il n'est pas exactement `<env>`, ou si le premier `</env>` ne
   contient pas `Working directory:`, le bloc est invalide.
2. Si aucun bloc valide n'existe, un message système `<env>…</env>` est inséré
   **en tête**, sans modifier le prompt officiel.
3. La vérification se fait **avant** le retour rapide de `_free_wire_body` :
   un corps « signature officielle » qui a perdu son `<env>` est corrigé.

**Raison**: `rianilham` et `xljiulang` mesurent que `<env>` est un credential
de corps ; un premier couple vide empoisonne la requête. Notre ancien scan
acceptait un couple valide plus loin.

**Tests**: `test_env_guard_adds_block_when_official_marker_has_no_env`,
`test_env_guard_keeps_existing_valid_block`,
`test_env_guard_rejects_empty_first_pair_before_valid_one`,
`test_env_guard_rejects_malformed_first_tag`.

**Contrat**: [contracts/env-guard.md](./contracts/env-guard.md).

### 2.3 `tool_choice: "none"` — `opencode.py`

**Fonction**: `_free_wire_body` (`need_tc_none`).

| Avant | Après |
|---|---|
| `none` retiré seulement quand le proxy injectait la grille | `none` retiré de toute copie free |

**Raison**: le gate refuse `none` (mesuré sur le summary) ; un client
« signature OK » qui désactive les outils prenait un faux quota.

**Test**: `test_tool_choice_none_is_dropped_from_free_wire`.

### 2.4 Plafond de cooldown station — `opencode.py`

**Fonctions**: `_FREE_COOLDOWN_CAP_DEFAULT`, `_free_cooldown_cap_seconds`
(nouvelle), `_free_429_cooldown_seconds`, `_handle_free_429` (appelant).

Formule effective :

```
cap = free.cooldown_cap_s (défaut 1200 s ; 0 = désactivé)
header_ou_body = Retry-After (secondes/date) ou durée humaine du corps
base = min(header_ou_body, cap)            # _free_429_cooldown_seconds
si scope == trial-exhausted et pas de header :
    base = max(base, trial_dead_cooldown_s)
cd_station = min(base, cap)                 # appliqué après le max
```

| Avant | Après |
|---|---|
| header honoré jusqu'à 24 h | plafonné à 20 min |
| `trial_dead_cooldown_s` (6 h) reprenait par-dessus | plafonné à 20 min |
| station parkée ~19 h après un `Retry-After` de minuit | re-testée après 20 min |

**Raison**: `afree` mesure `RA=20 h 46` puis un 200 sur la même IP 18 min plus
tard. `rianilham` plafonne à 120 s pour les clés. 20 min est le compromis pour
un quota par IP.

**Tests**: `test_cooldown_cap_applies_to_huge_retry_after`,
`test_cooldown_cap_can_be_disabled`, mise à jour de
`test_cooldown_header_wins_over_body`.

**Contrat**: [contracts/cooldown.md](./contracts/cooldown.md).

### 2.5 Plafond ledger — `free/quota_ledger.py`, `free/pool.py`

**Champ**: `QuotaLedger.max_block_s` (défaut 0 = pas de plafond).

Dans `note_result` :

```
si 429 FreeUsageLimitError et ra > 0 :
    reset_at = ts + min(ra, max_block_s)   # si max_block_s > 0
sinon :
    reset_at = prochain minuit UTC
```

**Raison**: le ledger doit refléter le bucket serveur, mais un plafond
optionnel permet de re-tester une IP sans attendre minuit. Le défaut 0
préserve le contrat historique et les tests ; la production active 1 200 s
via `config.yaml`.

**Test**: `tests/test_quota_ledger.py::test_retry_after_cap_reprobes_before_midnight`.

### 2.6 Configuration — `config.yaml`

```yaml
free:
  cooldown_cap_s: 1200

ip_quota:
  ledger_max_block_s: 1200

ip_registry:
  dns:
    fallback_order:
    - dot
  allocator:
    fast_discovery_transport: wireguard
```

Les deux premières valeurs sont indépendantes : le premier plafonne la
**station**, le second le **ledger**. Les deux sont nécessaires. Les deux
dernières corrigent le diagnostic stations (voir §2.9).

### 2.6.1 Diagnostic stations VPN

Les six stations bouclaient sur un échec DNS gluetun. Cause : l'override
généré (`logs/compose-hostname-*.yml`) forçait
`DNS_UPSTREAM_RESOLVER_TYPE=plain` après deux échecs DoT, et `plain` ne résout
rien sur ce réseau. OpenVPN échoue aussi `AUTH_FAILED` (credentials stale) ;
WireGuard authentifie.

Recipe validée sur un conteneur témoin :

```
VPN_TYPE=wireguard
DNS_UPSTREAM_RESOLVER_TYPE=dot
DNS_UPSTREAM_RESOLVERS=cloudflare,google
WIREGUARD_MTU=1419
HEALTH_VPN_DURATION_INITIAL=60s
```

Résultat : conteneur healthy, egress `187.40.243.35` (mais IP 429). Deux
stations existantes sont healthy (`186.247.77.36`, `186.247.77.6`) et leurs
IP sont aussi 429 (RA ~64 200–64 700 s). Le tunnel est donc réparable ; le
blocage restant est le quota par IP.

Actions : `dns.fallback_order` réduit à `[dot]` (si un hôte bloque 853, on
change d'hôte au lieu de casser le DNS) et `fast_discovery_transport` passé à
`wireguard` (OpenVPN `AUTH_FAILED`). Le superviseur a été arrêté pour
l'expérience puis relancé avec cette config.

Limite : le superviseur relancé a quand même réécrit l'override en
`VPN_TYPE=openvpn` (`us10469`), donc `AUTH_FAILED`. Le réglage
`fast_discovery_transport` ne suffit pas : `fresh_chain_supervisor.py`
`_preferred_transport` renvoyait `auto` tel quel et le child choisissait
OpenVPN. Corrigé : `auto` → transport de repli (`wireguard`). Le superviseur
et son child ont été arrêtés pour ne pas brûler de sessions ; trois stations
restent saines (`-2`, `-3`, `-5`) mais leurs IP sont 429. Le correctif
s'appliquera au prochain démarrage du proxy/superviseur.

### 2.6.2 Deuxième station : WARP + pont direct

**Nouveaux fichiers** :

- `scripts/direct_socks5.py` : serveur SOCKS5 minimal (CONNECT IPv4/domaine/
  IPv6, sans auth), écoute `127.0.0.1:11084`, log dans
  `logs/direct_socks5.log`.
- `scripts/start_direct_socks5.ps1` : démarrage idempotent du pont.

**Config actuelle** (cinq blocs WARP actifs, direct désactivé) :

```yaml
ip_rotation:
  proxy_mode: socks5
  socks5_auto_rotate: true
  socks5_proxies:
  - {host: 127.0.0.1, port: 11080, label: warp-ipv4}
  - {host: 127.0.0.1, port: 11080, label: warp-ipv6, ipv6: true}
  - {host: 127.0.0.1, port: 11085, label: warp-ipv6-2, ipv6: true}
  - {host: 127.0.0.1, port: 11086, label: warp-ipv6-3, ipv6: true}
  - {host: 127.0.0.1, port: 11087, label: warp-ipv6-4, ipv6: true}
  - {host: 127.0.0.1, port: 11084, label: direct, enabled: false}
```

**Pourquoi** : le pool `socks5` traite chaque entrée comme une station
duck-typée, avec bad-mark et quota. WARP apporte une deuxième IP 200 ; le pont
direct garde l'IP directe dans la rotation et sert de repli quand WARP 429.
Quatre conteneurs WARP supplémentaires donnent quatre `/64` distincts.

**Mesure** : 4 requêtes `11080`/`11084` → 4 × 200 (première version) ; puis
six entrées → 6 × 200 ; enfin direct désactivé → **5 × 200**, aucun
`socks5:direct`.

**Limite** : le pont est un processus séparé. S'il meurt, le pool utilise
WARP puis retombe sur le direct via `(None, None)`, mais perd le round-robin.
`T-414` couvre le démarrage automatique.

### 2.6.3 Gate et quota — deuxième lot de dépôts

`ebouther/opencode-unlimited-proxy` mesure le gate comme **quatre conditions
de forme** (une variable à la fois, 2026-10-16) :

1. UA `opencode/<version>` ≥ 1.18.0 ;
2. `x-opencode-session` exact, `ses_` + 12 hex + 14 base62, layout
   `…ffe` et horloge récente ;
3. un outil nommé exactement `read` — le nombre et les autres noms sont sans
   effet ;
4. `stream: true`.

Le bloc `<env>` avec `Working directory:` reste une condition séparée
(mesure 2026-10-25), couverte par notre garde.

Le quota est keyé par **bloc d'egress** (`/64` IPv6, `/32` IPv4), reset
00:00 UTC. Notre `server_ip_key` tronque déjà l'IPv6 au `/64`. WARP IPv4 est
une NAT partagée : la rotation IPv4 n'ajoute pas de headroom. L'hôte Windows
n'a aucune IPv6 globale (`fe80::` seulement), donc la voie `/64` demande un
hôte Linux avec un préfixe routé.

**Conséquences** : `T-413` clos ; test de session canonique ajouté ; pas de
changement de wire. `T-416` (IPv6 `/64`) et `T-417` (probe de gate local)
restent ouverts.

### 2.6.4 Station IPv6 WARP (T-421)

Le tunnel WARP existant fournit un egress IPv6 `2a09:bac5:32d4:be::/64`,
distinct de la NAT IPv4 `104.28.230.202`. Implémentation :

- `Socks5Endpoint` : champs `label`, `ipv6`, `egress_ip` ; `pid = label` ;
  `refresh_egress` force `IPRESOLVE=6` (api64/cloudflare trace).
- `set_socks5_proxies` / `_rebuild_static_eps` : lecture et propagation des
  trois champs ; warning si labels dupliqués.
- `_get_pooled_curl_session` : `curl_options` de session, fusionnés avec les
  options proxy TLS.
- `_do_free_request_curl_cffi` : `IPRESOLVE=6` + variant `-ipv6` pour une
  session dédiée ; `curl_options` passé seulement s'il existe (tests).
- `on_request` : sonde IPv6 one-shot si `egress_ip` n'est pas configuré.

**Mesure live** : six requêtes distinctes, **6 × 200**, rotation
`IPv6 → direct → IPv4` (première version) ; puis quatre conteneurs WARP
supplémentaires (quatre `/64` distincts) et **5 × 200** après désactivation
du direct. Le ledger enregistre les IPv6 réelles et tronque au `/64`.

Détail complet : [`docs/IPV6_WARP_STATION_2026-11-06.md`](../../docs/IPV6_WARP_STATION_2026-11-06.md).

### 2.7 Tests ajoutés ou adaptés

| Fichier | Changement |
|---|---|
| `tests/test_free_body_conformite.py` | placeholders non vides ; 4 tests `<env>` ; 1 test `tool_choice` ; fixture officielle avec `<env>` |
| `tests/test_free_official_payload.py` | `V2_SYSTEM` contient un `<env>` valide |
| `tests/test_quota_ledger.py` | plafond `max_block_s` |
| `tests/test_trial_dead_quota.py` | plafond 20 min ; tests cap actif/désactivé |
| `tests/test_session_canonical.py` | layout `ses_` `…ffe`, horloge implicite, forme 30 caractères |
| `tests/test_socks5_ipv6_station.py` | labels distincts même `host:port`, `ipv6`, egress réel, ledger `/64` |

### 2.8 Documentation

| Fichier | Contenu |
|---|---|
| `docs/REPO_INTEL_2026-11-06.md` | analyse des neuf dépôts, deux passes |
| `docs/PLAN_RETABLISSEMENT_FREE_2026-11-05.md` | §0.6 historique, §0.7 dépôts et correctifs |
| `docs/AUDIT_REPOS_2026-11-06.md` | audit consolidé et matrice |
| `specs/004-repo-parity-audit/` | ce paquet |

## 3. Travaux antérieurs (spec 003, non commités)

Ces changements ne datent pas de cette session mais font partie de l'état
actuel. Ils sont documentés ici pour que le `git diff` reste lisible.

| Zone | Changement | Référence |
|---|---|---|
| Ordre 2.2.5 | `_OPENCODE_HEADER_ORDER_225` (16 en-têtes) et passage des vrais headers à `_get_free_fp_kwargs` | `specs/003-official-client-parity/` |
| Project | `x-opencode-project: global` n'est plus remplacé par le hash du dépôt | `free/impersonation.py` |
| Trace | trace ID stable par session | `free/impersonation.py` |
| Rotation | `ROTATION_VERSIONS=("1.19.5",)` + migration des anciens fichiers | `free/identities.py` |
| Quota | `FreeIPPool.clear_model_global()` + `POST /api/free/quota/clear` | `free/pool.py`, `opencode.py` |
| Credentials | `scripts/refresh_nord_credentials.py` (check/apply) | `scripts/` |
| Probe | `scripts/test_free_egress.py --client cli|desktop --project --dry-run` | `scripts/` |
| E2E | `tests/test_official_wire_e2e.py` (16 en-têtes exacts) | `tests/` |

## 4. Décisions

- **D-01 — Ne pas changer l'UA maintenant.** Le corps/ordre sont 2.2.5, l'UA
  est 1.19.5. Le gate ne lit que la forme ; changer l'UA sans re-capture
  complète introduirait un autre hybride. Documenté comme divergence ouverte
  `T-401`.
- **D-02 — Garder la session par `(IP, conversation)`, rotation 30 min.**
  Plus proche du client 2.2.5 que la session par appel d'afree ; le test de
  shadow-ban est destructif pour l'IP directe, donc non exécuté.
- **D-03 — Plafonner à 20 min, pas 120 s.** Le quota est par IP ; 120 s est
  adapté à une clé, pas à un bucket journalier partagé.
- **D-04 — Garder le ledger par `(IP, jour UTC, bucket)`.** Le bucket serveur
  est journalier ; le plafond de re-test est une politique de disponibilité,
  pas une vérité serveur.
- **D-05 — Ne pas adopter l'alias `fn_<camelCase>_ex`.** Non nécessaire sur
  le modèle actuel ; à réactiver si un modèle refuse un nom snake_case.
- **D-06 — Ne pas intégrer WARP comme station 2 dans cette itération.** L'IP
  WARP passe, mais la rotation ne donne qu'une IP par hôte. L'intégration
  demande un modèle de station SOCKS5 et une politique de quota distincte.
- **D-07 — Ne pas adopter `opencode serve`.** Repli architectural si le gate
  change ; pas nécessaire tant que le wire direct passe.
- **D-08 — Ne pas toucher au keep-alive après 429.** Candidate non mesurée ;
  l'ajouter sans preuve ajouterait des handshakes.
- **D-09 — Gate = `read` seul.** La mesure une-variable-à-la-fois d'`ebouther`
  clôt `T-413`. On garde la grille à 13 outils comme sur-ensemble ; on ne
  réduit pas à `read` seul, sans bénéfice.
- **D-10 — Quota par bloc, pas par IP.** Le ledger tronque déjà l'IPv6 au
  `/64`. La voie six stations passe par six blocs distincts ; sur cet hôte
  Windows sans IPv6 globale, il faut un hôte Linux avec un `/64` routé ou six
  blocs IPv4. Ne pas simuler une IPv6 locale.
- **D-11 — Ne pas copier le patch SSE de l'edge proxy.** Il ajoute un `stop`
  même sur une vraie troncature. Notre relais ajoute la clôture manquante mais
  transforme une troncature réelle en erreur : c'est plus honnête pour un
  agent.
- **D-12 — Station IPv6 WARP séparée, pas un flag global.** `IPRESOLVE=6` est
  une option de session : on isole la session IPv6 (`variant -ipv6`) et on
  labellise l'endpoint (`warp-ipv6`) pour ne pas collisionner avec
  `warp-ipv4` sur le même `host:port`. Un `/64` par device ; pour plusieurs
  blocs, voir `warp-minimal` (T-422).

## 5. Risques et mitigations

| Risque | Impact | Mitigation |
|---|---|---|
| L'UA 1.19.5 + corps 2.2.5 reste un hybride détectable | Gate futur | `T-401` : capture 2.2.5 complète puis profil unique |
| Session collante shadow-ban (afree) | Perte d'IP jusqu'à minuit | `T-402` : test sur IP sacrifiable, pas sur le direct |
| Le plafond 20 min re-teste une IP encore épuisée | Quelques 429 de sonde | Le ledger rebloque ; coût borné |
| WARP non rotatif sur cet hôte | Une seule station WARP | `T-403` : autres hôtes ou autres IP publiques |
| Six stations VPN non vérifiées | Objectif 6 stations | `T-404` : limite de sessions + IP fraîches |
| Rien n'est commité | Perte de l'état | `T-405` : commit + message de référence |
| `quota_per_ip: 500` surestimé | Rotation tardive | `T-406` : calibrer sur ~200 req/5 h |
| Frame `cost` avant `[DONE]` | Client strict | `T-407` : tester un client OpenAI strict |

## 6. Rollback

- **Config** : retirer `free.cooldown_cap_s` et `ip_quota.ledger_max_block_s`
  → comportement historique (plafond 0).
- **Code** : les changements sont additifs ; un revert de `opencode.py`,
  `free/quota_ledger.py`, `free/pool.py`, `config.yaml` et des quatre tests
  suffit.
- **Proxy** : redémarrer `pythonw.exe opencode.py` ; le mode direct ne dépend
  pas des stations.
- **WARP** : `docker rm -f warp-test` ; aucune config du proxy n'en dépend.

## 7. Vérification

1. `python -W ignore -m pytest tests -q -p no:warnings -k "free or ..."` → exit 0.
2. `POST /v1/chat/completions` `ping` → 200 `pong`.
3. `tool_choice: none` + `<env></env>` → 200.
4. Stream → 200, `[DONE]`.
5. WARP `scripts/test_free_egress.py --proxy socks5h://127.0.0.1:11080` → 200.

Voir [quickstart.md](./quickstart.md) et [measurements.md](./measurements.md).
