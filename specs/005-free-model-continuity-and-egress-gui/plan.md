# Plan — Architecture conceptuelle

**Feature**: `005-free-model-continuity-and-egress-gui`  
**Statut**: conception, aucune implémentation.  
**Spec**: [spec.md](./spec.md)  
**Research**: [research.md](./research.md)

## 1. Vue d'ensemble

```
┌────────────────────────────── GUI (Svelte) ──────────────────────────────┐
│  FreeModels : default actif, source, pin/auto, dernier switch            │
│  FreeEgress : mode VPN/WARP, nombre, plan, apply, stations, audit        │
└──────────────────────────────────┬───────────────────────────────────────┘
                                   │ /api/free-models, /api/free-egress
┌──────────────────────────────────▼───────────────────────────────────────┐
│ dashboard/api.py                                                          │
│  - expose l'état, valide, délègue aux contrôleurs                         │
└───────────────┬───────────────────────────────────┬──────────────────────┘
                │                                   │
┌───────────────▼──────────────────┐   ┌────────────▼──────────────────────┐
│ FreeModelContinuity              │   │ FreeEgressController              │
│  lit FREE_MODELS + probes        │   │  desired state → plan → apply     │
│  choisit/persiste le target      │   │  providers: VPN | WARP            │
└───────────────┬──────────────────┘   └────────────┬──────────────────────┘
                │                                   │
┌───────────────▼──────────────────┐   ┌────────────▼──────────────────────┐
│ config.yaml / settings           │   │ DockerRunner + Pool reload        │
│ FREE_DISCOVERY_DEFAULT_TARGET    │   │ gluetun / caomingjun-warp         │
└──────────────────────────────────┘   └────────────┬──────────────────────┘
                                                    │
┌───────────────────────────────────────────────────▼──────────────────────┐
│ Identity layer (P0)                                                       │
│  StationIdentityVault · PromptTemplate · ConversationAffinity · Audit    │
└──────────────────────────────────────────────────────────────────────────┘
```

## 2. Continuité du modèle free

### 2.1 Composant

`FreeModelContinuity` (module proposé `free/model_continuity.py`) :

- **entrées** : `FREE_MODELS`, `FREE_MODEL_MAP`, `FREE_DISCOVERY_DEFAULT_TARGET`,
  `free_models_probed.json`, config `free_discovery.*` ;
- **sorties** : `ModelSelection` (target, source, reason, previous) ;
- **effets** : mise à jour de `FREE_DISCOVERY_DEFAULT_TARGET`, persistance
  optionnelle de `free_discovery.default_target`, événement GUI/log.

### 2.2 Algorithme

```
si pinned_target non vide : retourner pinned (source=pinned)
si target ∈ FREE_MODELS et probe(target) != dead : retourner target (source=config)
candidats = [m ∈ FREE_MODELS | probe(m) == alive]
si candidats vides : retourner no_free_model (aucune écriture)
trier candidats par :
  1. même famille que target (deepseek/glm/mimo/…)
  2. index dans free_discovery.preference
  3. probe le plus récent
  4. ordre lexicographique (stable)
choisir le premier
si auto_default_target : persister free_discovery.default_target
propager FREE_DISCOVERY_DEFAULT_TARGET
logger l'ancien/nouveau + raison
```

Règles :

- une entrée **explicite** de `FREE_MODEL_MAP` n'est jamais réécrite ;
- `limited` n'est pas choisi automatiquement (conservateur) ;
- `unproven` n'est pas choisi s'il existe un `alive` ;
- aucun modèle payant ;
- verrou `asyncio.Lock` + écriture atomique de `config.yaml` ;
- déclenchement : après `ensure_free_models_on_boot`, après chaque probe, et
  à la demande (`POST /api/free-models/reconcile`).

### 2.3 Propagation

| Consommateur | Aujourd'hui | Cible |
|---|---|---|
| `FREE_DISCOVERY_DEFAULT_TARGET` | chargé une fois | mis à jour par le resolver |
| `FREE_MODEL_MAP` | statique + homonymes | jamais réécrit par la politique |
| `scripts/fresh_chain.py --model` | défaut hardcodé | dérivé du target actif |
| `scripts/fresh_chain_supervisor.py --models` | `allocator.probe_models` | dérivé de `FREE_MODELS` + probes |
| `opencode.py:26861` | `jev-1.13-free` | supprimé/replacé par le resolver |
| GUI `/api/free-models` | inventaire | + `active_default`, `source`, `last_switch` |

## 3. Contrôleur d'egress

### 3.1 État désiré

Bloc `free_egress` proposé (migration depuis `ip_rotation.proxy_mode` +
`station_count`) :

```yaml
free_egress:
  mode: warp                 # vpn | warp | socks5 | direct
  station_count: 5
  managed: true              # ne touche qu'aux stations gérées
  warp:
    provider: caomingjun     # caomingjun | warp-minimal
    base_port: 11080
    ipv6: true
    image: caomingjun/warp:latest
  vpn:
    provider: nordvpn
    transport: wireguard
    dns: dot
    max_stations: 6
  identity:
    secret_path: logs/identity_secret
    session_ttl_s: 1800
    affinity_ttl_s: 3600
    prompt_template: free/captures/2.2.5-system-minimal.txt
```

### 3.2 Composants

- `FreeEgressController` : lit l'état désiré, calcule le plan, sérialise les
  apply, publie l'état.
- `Provisioner` (interface) :
  - `VpnProvisioner` : réutilise `_apply_station_count`, compose/gluetun ;
  - `WarpProvisioner` : `docker run` idempotent, ports/labels, `/64` distincts.
- `DockerRunner` : wrapper CLI injectable (fake pour tests).
- `EgressPlanner` : pur, produit `create/keep/remove/verify`.
- `PoolBinder` : écrit `socks5_proxies` **managed** et recharge le pool.

### 3.3 Plan / apply

```
plan(desired, actual):
  create = desired - actual (par label)
  keep   = intersection
  remove = actual - desired
  verify = create + keep
apply(plan):
  lock
  preflight (docker, ports, limites provider)
  créer les nouvelles stations
  vérifier egress + probe free + audit identité
  activer les nouvelles dans le pool
  drainer/retirer les anciennes
  persister la config en dernier
  unlock
```

- Blue/green : les anciennes restent actives jusqu'à vérification des
  nouvelles.
- En cas d'échec : retirer les nouvelles, garder les anciennes, rapport.
- Idempotence : un apply sans delta ne fait rien.
- Verrou : un seul apply à la fois ; le second renvoie `409 busy`.

### 3.4 Limites provider

| Provider | Limite | Comportement |
|---|---|---|
| VPN NordVPN | sessions simultanées (~5–6 mesurées) | refuser au-delà, message clair |
| WARP `caomingjun` | registrations Cloudflare (throttle) | planifier par vagues, retry backoff |
| WARP `warp-minimal` | registration throttle | provider optionnel |

## 4. Isolation d'identité (P0)

> **Règle de sécurité** : ne pas dépasser une station tant que cette couche
> n'est pas en place. Le round-robin actuel envoie le même historique sous
> plusieurs identités.

### 4.1 `StationIdentity`

Secret local persistant (`logs/identity_secret`, 32 octets, 0600, gitignoré).
Pour chaque label :

```
seed      = HMAC-SHA256(secret, "station:" + label)
project   = HMAC-SHA256(secret, "project:" + label).hex()[:40]
trace     = HMAC-SHA256(secret, "trace:" + label).hex()[:32]
ses_state = état de mint isolé par station (compteur + last_ms)
env_cwd   = "/workspace/" + safe(label)
```

- `project` : 40 hex, unique par station, jamais `global` ni hash du dépôt.
- `ses_` : canonique `…ffe`, par `(station, conversation)`, rotation 30 min.
- `msg_` : frais par requête.
- `trace` : stable par station ; span par requête.
- Aucun ID global (`_proxy_session_id`) dans la jambe free.

### 4.2 Prompt templaté

Le prompt officiel est conservé pour la signature du gate, mais :

```
You are an AI agent running in OpenCode, ...
Today's date: <UTC courante>
<env>
  Working directory: /workspace/<label>
</env>
```

- pas de `C:\Users\...`, pas de nom d'utilisateur, pas de hostname ;
- pas de `Workspace root folder`, `Platform`, `Is directory a git repo`,
  `Prefer ...` (non requis par le gate) ;
- pas de date de capture ;
- le bloc `<env>` reste littéral et contient `Working directory:`.

### 4.3 Affinité de conversation

```
key = sha256(model | system | first_user | conversation_id)
station = affinity.get(key) or selector.pick_new()
affinity.set(key, station, ttl)
```

- même conversation → même station, même `ses_`, même project ;
- round-robin uniquement pour les nouvelles conversations / sans historique ;
- s'appuie sur le sélecteur existant (`_socks5_next` / `_socks5_current`) ;
  `socks5_auto_rotate` reste un fallback global, pas l'affinité ;
- failover seulement si la station est inutilisable ; nouvelle identité,
  événement journalisé ;
- TTL configurable (`identity.affinity_ttl_s`, défaut 3600).

### 4.4 Audit anti-corrélation

`IdentityAudit` (au démarrage + après apply + API) :

1. construire les tuples de toutes les stations ;
2. vérifier `project`, `ses_`, `msg_`, trace disjoints ;
3. scanner le prompt templaté contre une liste de sous-strings interdites :
   `C:\Users\`, nom d'utilisateur, hostname, hash du dépôt, vrai workspace,
   `Downloads`, date de capture ;
4. vérifier qu'aucune session curl/cookie n'est partagée ;
5. résultat : `ok | duplicate_identity | prompt_leak | shared_session` ;
6. une station en échec est exclue du pool (ou l'apply est bloqué).

### 4.5 Vecteurs partagés par conception

| Vecteur | Partagé ? | Raison |
|---|---|---|
| `Authorization: Bearer public` | oui | free anonyme |
| TLS JA3/JA4 Bun | oui | un seul profil vérifié |
| UA/version | oui (aujourd'hui) | un seul profil vérifié 1.19.5 |
| `x-opencode-client` | oui | valeur canonique unique (`cli` capture 2.2.5 ; `desktop` hérité à migrer) |
| Adresse IP/bloc | non | par station |
| `project`, `ses_`, `msg_`, trace | non | par station |
| `<env>` / prompt | non | synthétique par station |

Si plusieurs profils UA/TLS vérifiés existent plus tard, le wrapper peut les
répartir par station ; ce n'est pas requis pour l'isolation des IDs mais c'est
l'objet de P0.5.

### 4.6 Générateur de profils vérifiés (P0.5)

Le générateur `free/identity_generator.py` existe ; il faut l'envelopper, pas
le réécrire.

```
ProfileRegistry   (free/profiles/verified_profiles.json, atomique)
      ▲
ProfileVerifier   (bloc frais, 200 + contenu, 2 blocs distincts, 429=quota)
      ▲
ProfileGenerator  (identity_generator + filtre verified/stale)
      ▲
ProfileAssigner   (HMAC(secret, "profile:"+label), 1 profil/station)
      ▲
ProfileAudit      (tuple cohérent, profils partagés, TLS family)
```

- **Candidats** : 1.18.31/1.19.0/1.19.3/1.19.5 desktop, 2.2.5 cli. Deux
  familles TLS seulement (Bun 1.3.14 `d871…`, Bun 1.4.4 `1523…`).
- **Corps** : capturer le corps par version (11 outils pour 1.19.x, 13 pour
  2.2.5), ne pas réutiliser le corps 2.2.5 pour un profil 1.19.x.
- **Preuve** : `scripts/mint_capture.py --verify` + `scripts/test_free_egress.py`
  comme base ; le vérificateur ajoute la matrice de blocs et le registre.
- **Assignation** : profils distincts si `M >= N` ; sinon réutilisation
  marquée `shared_profile`.
- **Fallback** : profil canonical 1.19.5/2.2.5 vérifié ; jamais payant.
- **Audit** : UA ↔ Bun ↔ JA3 ↔ ordre ↔ corps ↔ session headers.

Détail : [contracts/verified-profiles.md](./contracts/verified-profiles.md).

## 5. API et GUI (concept)

### 5.1 Endpoints

| Méthode | Route | Rôle |
|---|---|---|
| `GET` | `/api/free-models` | + `active_default`, `default_source`, `last_switch`, `pinned` |
| `POST` | `/api/free-models/reconcile` | force la réconciliation |
| `PUT` | `/api/free-models/default` | pin/auto |
| `GET` | `/api/free-egress` | désiré, réel, stations, blocs, audit |
| `POST` | `/api/free-egress/plan` | dry-run |
| `PUT` | `/api/free-egress` | persiste + apply |
| `GET` | `/api/free-egress/identity-audit` | tuples/disjonction/fuites |
| `GET` | `/api/free-egress/profiles` | registre vérifié/stale/rejected |
| `POST` | `/api/free-egress/profiles/verify` | vérifie un candidat sur blocs frais |

### 5.2 GUI

- **FreeModels** : bandeau « modèle actif », source (`config/auto/pinned`),
  dernier switch, bouton Reconcile, sélecteur Auto/Pin.
- **FreeEgress** (nouvelle carte ou page) :
  - radio `VPN (NordVPN)` / `WARP` ;
  - nombre de stations 1–10 avec limites ;
  - bouton « Voir le plan » puis « Appliquer » ;
  - table des stations : label, mode, IP/bloc, état, dernière erreur,
    `profile_id`, `tls_family`, `shared_profile` ;
  - badge audit identité (`ok`, `duplicate`, `leak`) et badge profils
    (`distinct`, `shared`, `stale`).
- Les modes `socks5`/`direct` restent en « avancé ».

## 6. Phases de mise en œuvre (futures)

| Phase | Contenu | Prérequis |
|---|---|---|
| P0 | Isolation d'identité : vault, prompt templaté, affinité, audit | aucune |
| P0.5 | Profils vérifiés : registre, vérificateur, assignation, audit | P0 |
| P1 | Model continuity : resolver read-only + audit | P0 |
| P2 | Persistance du default + GUI FreeModels | P1 |
| P3 | Egress controller : plan/dry-run + tests fake | P0/P0.5/P1 |
| P4 | Provisioner VPN (réutilise `_apply_station_count`) | P3 |
| P5 | Provisioner WARP (caomingjun, puis warp-minimal) | P3 |
| P6 | GUI FreeEgress + audit visuel | P4/P5 |
| P7 | Auto-start/boot + runbook | P6 |

> **P0 est bloquant** : ne pas activer plusieurs stations avant l'isolation,
> sinon le provider peut corréler l'historique.
>
> **P0.5 est bloquant pour la diversité** : sans registre vérifié, toutes les
> stations partagent le même profil TLS/UA. On peut démarrer en multi-station
> avec le profil canonical unique, mais l'audit doit afficher ce résidu.

## 7. Risques et mitigations

| Risque | Mitigation |
|---|---|
| Collision de project/session | HMAC par label + audit disjonction |
| Fuite du vrai chemin | prompt templaté + scan de sous-strings |
| Conversation éclatée | affinité sticky + TTL |
| Failover qui duplique l'historique | failover uniquement sur station morte, journalisé |
| WARP throttle | vagues + backoff + ancien ensemble conservé |
| Limite VPN | validation `station_count` + message |
| Config drift | `free_egress` bloc managé + migration |
| Apply concurrent | verrou + 409 |
| Probe stale | préférer `alive`, sinon `no_free_model` |
| Docker indisponible | préflight + rollback |
| Seulement 2 familles TLS | audit `shared_profile`, capturer d'autres clients, ne jamais inventer un JA3 |
| Profil hybride corps/version | corps capturé par version + audit de cohérence |
| `429` pris pour un rejet de profil | classer `quota`, changer de bloc |
| Registre de profils stale | TTL + re-vérification avant assignation |

## 8. Rollback

- modèle : conserver l'ancien `default_target` dans `ModelSelection.previous` ;
  un `POST /reconcile` avec pin restaure l'ancien.
- egress : plan/apply conserve l'ancien ensemble jusqu'à vérification ; un
  `PUT` avec l'ancien état désiré revient en arrière.
- identité : le secret et les labels sont stables ; un redémarrage ne change
  pas les tuples.
- profils : conserver l'ancien registre ; un profil `rejected`/`stale` est
  exclu et remplacé par le canonical vérifié, jamais par un profil inventé.
