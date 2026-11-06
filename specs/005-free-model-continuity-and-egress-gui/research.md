# Research — Continuité du modèle free, egress GUI et isolation d'identité

**Feature**: `005-free-model-continuity-and-egress-gui`  
**Date**: 2026-11-06  
**Statut**: conceptualisation, aucune implémentation.

## 1. Méthode

Lecture du code existant, pas des suppositions :

- `config/discovery.py`, `config/settings.py` — découverte, probe, target ;
- `opencode.py` — résolution free, `_apply_station_count`, fallbacks ;
- `free/identities.py`, `free/impersonation.py` — identités par IP, trace ;
- `free/pool.py` — pool `socks5`, round-robin, labels ;
- `dashboard/api.py` — modes, station_count, config ;
- `dashboard-ui/src/pages/FreeModels.svelte`, `vpn/VpnStatus.svelte`,
  `vpn/VpnRouting.svelte` — GUI ;
- `scripts/fresh_chain.py`, `scripts/fresh_chain_supervisor.py` — modèles
  passés aux chaînes ;
- `free/official_payload.json`, `free/captures/2.2.5.json` — prompt système.

## 2. Découverte de modèles — ce qui existe déjà

| Élément | Rôle | Constat |
|---|---|---|
| `config/discovery.py::apply_discovered_free_models` | Met à jour `FREE_MODELS`, `MODELS`, `FREE_MODEL_MAP` | **Ne persiste pas** le default target : `log.warning` + fallback local seulement (lignes 632–636) |
| `free_models_probed.json` | Verdicts `alive/limited/dead`, statut, raison, date | Mis à jour automatiquement (vu `2026-11-06T08:06:26Z`) |
| `free_models_verified.json` | Modèles vérifiés par probe réel | Plus ancien, couverture partielle |
| `ensure_free_models_on_boot()` | Lance la découverte au boot + background | Existe déjà |
| `FREE_DISCOVERY_DEFAULT_TARGET` | Target runtime | Chargé une fois depuis `free_discovery.default_target` |
| `opencode.py::_resolve_free_model` | Résout le modèle free d'une requête | `FREE_MODEL_MAP` explicite souverain ; homonyme ; candidats ; repli |
| `opencode.py:26861` | Fallback d'un chemin | `jev-1.13-free` **hardcodé** |
| `scripts/fresh_chain.py` | `--model` par défaut | `deepseek-v4.1-flash-free` **hardcodé** |
| `scripts/fresh_chain_supervisor.py` | `--models` | Vient de `allocator.probe_models` (config), pas d'un littéral |

**Conclusion** : la brique « ajouter/supprimer les modèles disponibles » existe
et fonctionne. Le manque est une **politique de target** : lire l'état déjà
probe, choisir un remplaçant, le persister, et propager aux consommateurs.
Il ne faut **pas** ajouter un second fetcher ni une seconde sonde.

### Options de continuité du modèle

| Option | Avantages | Inconvénients | Verdict |
|---|---|---|---|
| A. Nouveau service de découverte | Contrôle total | Duplique l'existant, deux vérités, plus de fetches | Rejeté |
| B. Resolver au-dessus de `FREE_MODELS` + `free_models_probed.json` | Zéro duplication, s'appuie sur le probe existant | Dépend de la fraîcheur du probe | **Recommandé** |
| C. Sélection manuelle GUI uniquement | Simple | Ne survit pas à une disparition | Insuffisant |

## 3. Egress GUI — ce qui existe déjà

| Élément | Rôle | Constat |
|---|---|---|
| `VpnStatus.svelte` | Sélecteur de mode | `vpn \| socks5 \| direct` ; **pas de WARP** |
| `VpnRouting.svelte` | Nombre de stations | `station_count` 1–10, POST `/api/vpn-config` |
| `dashboard/api.py::set_vpn_proxy_mode` | Change le mode | Valide `vpn\|socks5\|direct`, fan-out managers |
| `dashboard/api.py` station_count | Hot-reload | `_apply_station_count(n)` 1–10, idempotent |
| `opencode.py::_apply_station_count` | Crée/supprime les stations gluetun | Existe pour NordVPN |
| Conteneurs WARP | Quatre `caomingjun/warp` | Créés **à la main**, hors GUI/config managée |
| `socks5_proxies` | Liste statique | Mélange les WARP et le direct, sans notion de « managed » |

**Conclusion** : le mode VPN a déjà son chemin de provisioning ; le mode WARP
n'en a pas. Il faut un **contrôleur d'egress** au-dessus des deux, avec un
bloc `managed` pour ne pas écraser les proxies manuels.

### Options d'egress

| Option | Avantages | Inconvénients | Verdict |
|---|---|---|---|
| A. Étendre `proxy_mode` + `station_count` | Réutilise l'existant | Deux sémantiques (VPN/WARP) dans un champ prévu pour 3 modes | Possible |
| B. Nouveau bloc `free_egress` + contrôleur | Sémantique claire, extensible | Migration des clés existantes | **Recommandé** |
| C. Script de provisioning séparé | Simple à tester | Hors GUI, pas d'état désiré | Repli |

### Provider WARP

| Provider | Avantages | Inconvénients |
|---|---|---|
| `caomingjun/warp` (actuel) | 4 `/64` distincts prouvés, simple | Un conteneur par station, ~50 Mo, registration Cloudflare |
| `warp-minimal` (MASQUE) | Plusieurs exits dans un process, UDP 443 | Registration actuellement throttlée, binaire récent |

**Recommandation** : interface `WarpProvider` avec `caomingjun` par défaut ;
`warp-minimal` ajoutable sans changer le contrôleur.

## 4. Isolation d'identité — surface actuelle

### Ce que le provider voit

| Vecteur | Valeur actuelle | Risque |
|---|---|---|
| `x-opencode-project` | Dérivé de l'IP (`free/identities.py`) | Deux stations sur le même IPv4 partagent le project |
| `x-opencode-session` quartet | `ses_` par `(IP, conversation)` | Correct par IP, mais le pool round-robin change d'IP entre les tours |
| `x-opencode-request` / `msg_` | Frais par envoi | Correct |
| `b3` / `traceparent` | Trace stable par session | À vérifier station-scopé ; sinon partagé |
| `User-Agent` | 2.2.5/1.19.5/1.19.3 | Profils vérifiés distincts |
| `x-opencode-client` | `cli` (2.2.5), `desktop` (1.19.5/1.19.3) | Plusieurs valeurs maintenant, chacune portée par son profil |
| TLS JA3/JA4 | Bun 1.4.4 | Partagé par conception |
| `<env>` | Chemin réel du workspace (nom d'utilisateur + arborescence), date de capture, `Platform: win32`, `Is directory a git repo: yes` | **Fuite directe de la machine** |
| Prompt système | Capture 2.2.5 avec le chemin réel | **Fuite directe** |
| `Authorization` | `Bearer public` | Partagé par conception (free anonyme) |
| Cookies / IP client headers | Absents | Correct |

### Le problème du round-robin

`free/pool.py::_socks5_next` avance à chaque requête. Une conversation de cinq
tours peut donc sortir par cinq stations, avec cinq `ses_`/project différents
et le **même historique**. C'est la corrélation la plus simple pour le
provider : même contenu, identités différentes, même modèle.

Le pool possède déjà deux réglages voisins, mais aucun n'est keyé par
conversation :

| Réglage existant | Effet | Limite |
|---|---|---|
| `socks5_auto_rotate: true` | round-robin strict à chaque requête | pas d'affinité |
| `socks5_auto_rotate: false` | sticky global sur le proxy courant | pas par conversation |
| `free_parallel_routing: round-robin/failover` | routage de la parallélisation | pas une affinité de conversation |

**Recommandation** : affinité de conversation (sticky station) au-dessus du
sélecteur existant ; le round-robin ne s'applique qu'aux conversations
nouvelles ou sans historique. Ne pas créer un second pool.

### Le problème du prompt

Le prompt officiel 2.2.5 contient le vrai workspace. Même avec des IDs
distincts, le provider lit le même chemin sur toutes les stations. Il faut
**templater** le prompt :

- conserver le marqueur officiel (signature du gate) ;
- remplacer le `<env>` par un bloc minimal synthétique
  (`Working directory: /workspace/<label>`) ;
- date UTC courante, pas la date de capture ;
- supprimer `Workspace root folder`, `Platform`, `Is directory a git repo`,
  `Prefer ...` (ils ne sont pas requis par le gate — probe xljiulang
  2026-11-06).

### Options d'identité

| Option | Avantages | Inconvénients | Verdict |
|---|---|---|---|
| A. Garder le vault par IP | Déjà en place | Project partagé sur un même bloc, pas station-scopé | Insuffisant |
| B. `StationIdentity` par label + HMAC(secret) | Disjoint par station, stable, pas de données machine | Nécessite un secret local et une migration | **Recommandé** |
| C. Identité aléatoire par requête | Disjoint | Casse l'affinité/cache, instable | Rejeté |

## 5. Risques et preuves manquantes

- Le probe `free_models_probed.json` peut être stale ; la politique doit
  traiter `unproven` avec prudence (préférer `alive`, sinon fallback).
- Le profil TLS/UA unique est un fingerprint partagé ; le documenter comme
  limite, ou introduire plusieurs profils vérifiés plus tard.
- Le failover d'une conversation vers une nouvelle station expose le même
  historique sous une nouvelle identité ; le minimiser et le journaliser.
- Le provisioning WARP peut être throttlé par Cloudflare ; le plan doit
  échouer proprement et conserver l'ancien ensemble.
- Les sessions NordVPN sont limitées ; le contrôleur doit refuser un
  `station_count` au-delà de la limite mesurée, avec un message clair.
- Les tests d'isolation doivent être déterministes : construire les tuples
  sans envoyer de requêtes, puis un test live sur IP sacrifiable.

## 6. Décisions proposées (à valider)

- **D-01** Utiliser `FREE_MODELS` + `free_models_probed.json` comme source de
  vérité ; pas de nouveau fetcher.
- **D-02** Persister le default target seulement si
  `free_discovery.auto_default_target: true` (défaut proposé : true).
- **D-03** Nouveau bloc `free_egress` (mode, station_count, providers) avec
  migration depuis `proxy_mode`/`station_count`.
- **D-04** Interface `WarpProvider` ; `caomingjun/warp` par défaut.
- **D-05** `StationIdentity` par label + HMAC d'un secret local.
- **D-06** Affinité de conversation obligatoire ; round-robin seulement pour
  les nouvelles conversations.
- **D-07** Prompt templaté minimal ; audit de sous-strings interdites.
- **D-08** Blue/green : vérifier les nouvelles stations avant de drainer les
  anciennes.
- **D-09** Générateur de profils vérifiés : registre avec preuves live, TTL,
  rejet des tuples mixtes ; seuls les profils `verified` sont émis.

## 7. Profils vérifiés — existant et limites

### 7.1 Le générateur existe déjà

`free/identity_generator.py` construit un profil complet à partir de
`VERSION_SPECS` :

- version, client, UA, suffixe Bun, ja3/ja3_md5, sigalgs, TLS GREASE ;
- en-têtes du provider et ordre de transport ;
- corps officiel (`free/official_payload.json`) et compteur d'outils.

Mais son champ `verified` est statique :

```python
"verified": version in VERSION_SPECS
            and VERSION_SPECS[version]["verified"]
            and client in VERIFIED_CLIENTS
```

et les constantes limitent fortement ce qui est réellement utilisable :

| Constante | Valeur | Effet |
|---|---|---|
| `ROTATION_VERSIONS` | `("1.19.5",)` | une seule version assignée |
| `VERIFIED_CLIENTS` | `("desktop",)` | `cli`/`app` non vérifiés live |
| `SUPPORTED_CLIENTS` | `desktop, cli, app` | enum seulement |
| `VERSION_SPECS` | 1.18.31/1.18.35/1.19.0/1.19.3/1.19.5 | 5 versions, 2 familles TLS |

`VERSION_SPECS` ne contient pas **2.2.5** : la capture 2.2.5 vit dans
`free/impersonation.json` / `free/captures/2.2.5.json`. Le registre doit
décrire ce profil comme une définition séparée (ou étendre proprement
`VERSION_SPECS` avec une capture complète), pas le déduire de 1.19.5.

### 7.2 Deux familles TLS seulement

| Versions | Bun | JA3 |
|---|---|---|
| 1.18.31 / 1.18.35 / 1.19.0 / 1.19.3 | 1.3.14 | `d871d02c…` |
| 1.19.5 / 2.2.5 | 1.4.4 | `1523504b…` |

Vérifier « plusieurs profils » donne donc de la diversité **UA/client/corps**,
mais au mieux **deux empreintes TLS**. Pour davantage de familles, il faut
capturer d'autres clients officiels ou runtimes ; inventer un JA3 ou mélanger
les versions produit un tuple impossible.

### 7.3 Le corps doit suivre la version

`free/identity_generator.py` utilise `free/official_payload.json` (corps
2.2.5, 13 outils) pour **toutes** les versions. Un profil 1.19.x avec ce corps
est un hybride. Pour des profils purs, il faut un corps capturé par version
(10 outils pour 1.18.31–1.19.3, 11 pour 1.19.5, 13 pour 2.2.5) et un audit
de cohérence.

### 7.4 Vérification

Les briques existent : `scripts/mint_capture.py --verify` (mint + 200),
`scripts/test_free_egress.py --station/--mint/--repeat`, et le pool
`Socks5Endpoint` avec `egress_ip`/`/64`. Il manque un **registre** et une
**matrice de profils** qui envoient chaque candidat sur des blocs distincts,
classent `quota` vs `reject`, et écrivent la preuve.

### 7.5 Décision

Ne pas remplacer `identity_generator.py` : l'envelopper dans
`ProfileRegistry` + `ProfileVerifier` + `ProfileAssigner`, et faire du registre
la seule source des profils émis.
