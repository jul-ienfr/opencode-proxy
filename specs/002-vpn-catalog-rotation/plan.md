# Implementation Plan: Rotation catalogue mondial et réparation automatique

**Branch** : `002-vpn-catalog-rotation`  
**Date** : 2026-11-04  
**Spec** : `specs/002-vpn-catalog-rotation/spec.md`  
**Audit** : `specs/002-vpn-catalog-rotation/audit.md`  
**Statut** : Phase 0/1 en cours — voir `implementation-log.md`

---

## 1. Résumé

Le plan remplace la boucle actuelle « 20 candidats, ordre hérité, un seul
propriétaire de réparation » par une architecture en quatre étages :

1. **Catalogue unifié** : API NordVPN + manifeste Gluetun, avec provenance,
   fraîcheur, endpoint IP et clé publique.
2. **Préflight** : vérification rapide et parallèle de l'endpoint (handshake
   WireGuard, DNS, RTT) avant toute création de session.
3. **Sélecteur** : classement par **score composite de performance récente**,
   toutes les stations indépendantes, tous les pays autorisés, unicité d'IP
   comme seule contrainte dure, pool chaud.
4. **Réparateur unique** : superviseur qui détecte tout 429 quota, retire la
   station du routage, promeut une IP chaude ou lance une réparation
   catalogue **pour cette station uniquement**, et vérifie le manifeste du
   conteneur cible avant chaque bind.

Le plan ajoute une **couche de fallback** (IP, DNS, catalogue, manifeste,
bind, probe, superviseur), un **transport auto tri-stack** (WireGuard →
OpenVPN UDP → OpenVPN TCP) par serveur, une **refonte strangler** (domaine
pur, ports/adapters, use cases) et une **base `registry.db`** unique pour les
serveurs, IP, quotas et décisions.

Le résultat attendu : remplacement en < 60 s, stations indépendantes et
parallèles, toutes les IP disponibles utilisables, zéro station « connectée
mais inutilisable ».

---

## 2. Contexte technique

**Langage** : Python 3.12.  
**Orchestration** : Docker Compose, Gluetun `qmcgaw/gluetun` (image
`latest` actuellement — à épingler), stations `vpn-gluetun-1..10`.  
**Persistance** : SQLite (`logs/ip_registry.db`, `logs/catalog_claims.db`),
JSON d'état (`logs/free_pool.json`, `logs/fresh_chain_state*.json`).  
**Réseau** : SOCKS5 Gluetun (`127.0.0.1:1079+N`), HTTP (`8887+N`), API de
contrôle Gluetun (`:8000` interne).  
**Tests** : pytest, tests d'intégration Docker, canary live.  
**Plateforme** : Windows + Docker Desktop + PowerShell.

### Contraintes réelles

- Sessions NordVPN simultanées bornées (nombre exact à mesurer et exposer) :
  on ne peut pas tester 9 787 serveurs en parallèle avec des tunnels
  complets.
- Un bind complet coûte 15–60 s ; un échec de préflight doit coûter < 2 s.
- Le quota free est par IP d'egress, avec reset quotidien.
- Le catalogue Gluetun contient 2 839 serveurs absents de l'API ; ils ne sont
  pas tous morts (408 observations 200 sur 7 jours) et restent éligibles.
- Un même hostname peut produire jusqu'à 169 IP distinctes (NAT NordVPN) :
  le hostname et l'IP sont deux axes de rotation différents.
- Toutes les stations partagent la même clé WireGuard ; le budget de sessions
  doit être vérifié avec cette clé.
- Le mode `auto` doit essayer **WireGuard, OpenVPN UDP et OpenVPN TCP par
  serveur**, pas affecter un slot fixe par station.

---

## 3. Constitution Check

Les principes de `specs/001-free-station-availability/spec.md` restent
applicables :

- **P1 — Un seul propriétaire** : le superviseur est le seul à réparer.
- **P2 — Quota par IP** : aucune décision globale de compte.
- **P3 — Preuve avant promotion** : 200 avec contenu obligatoire.
- **P4 — Indépendance et unicité d'IP** : aucune contrainte par pays ; une IP
  n'est jamais partagée par deux stations.
- **P5 — Observabilité** : chaque décision a une cause et une preuve.
- **P6 — Zéro intervention manuelle** : watchdog + reprise sur incident.

Le plan ne viole aucun de ces principes. Il renforce P1 (un seul
propriétaire), P3 (préflight + probe) et P4 (unicité d'IP).

---

## 4. Architecture cible

```text
                    ┌─────────────────────────────┐
                    │  CatalogService             │
                    │  API NordVPN + Gluetun      │
                    │  provenance / fraîcheur     │
                    └──────────────┬──────────────┘
                                   │ CatalogServer
                                   ▼
                    ┌─────────────────────────────┐
                    │  PreflightProber            │
                    │  DNS + handshake WG + RTT   │
                    │  parallèle, sans session    │
                    └──────────────┬──────────────┘
                                   │ EndpointTest
                                   ▼
┌──────────────┐    need    ┌─────────────────────┐   bind    ┌──────────────┐
│ RepairOrch.  │───────────▶│  Selector / Ranker  │──────────▶│ Binder       │
│ (superviseur)│            │  score + IP unique  │           │ compose/API  │
└──────┬───────┘            └──────────┬──────────┘           └──────┬───────┘
       │                               │                             │
       │ promote                       │ warm spare                  │ verify
       ▼                               ▼                             ▼
┌──────────────┐            ┌─────────────────────┐           ┌──────────────┐
│  WarmPool    │            │  EgressRegistry     │◀──────────│  Probe       │
│  IP vérifiées│            │  IP ↔ hostname ↔ pays│           │  200 + contenu│
└──────────────┘            └─────────────────────┘           └──────────────┘
       │                               │
       └───────────────┬───────────────┘
                       ▼
              ┌─────────────────┐
              │ Observability   │
              │ SLO / incidents │
              └─────────────────┘
```

### 4.1 Composants

| Composant | Responsabilité | Ne fait pas |
|---|---|---|
| `CatalogService` | fusionne API + Gluetun, marque provenance/fraîcheur, expose les candidats | ne bind pas, ne probe pas |
| `PreflightProber` | teste DNS/endpoint/handshake en parallèle, sans tunnel complet | ne décide pas de la qualité finale |
| `Selector` | classe par score composite, applique l'unicité d'IP et l'exploration de découverte, choisit le prochain candidat | n'exécute pas Docker |
| `WarmPool` | stocke les IP vérifiées prêtes à promouvoir | n'accepte que 200 + contenu |
| `Binder` | écrit l'override, recrée la station, attend l'egress | ne choisit pas le hostname |
| `Probe` | vérifie 200 + contenu + 429 + latence via SOCKS5 | ne blackliste pas seul |
| `RepairOrchestrator` | détecte le besoin, réserve le lease, enchaîne les candidats, persiste | ne recrée pas une station déjà réparée |
| `Observability` | SLO par station, incidents, prochain candidat, sweep progress | ne décide pas |

### 4.2 Flux nominal

0. **Démarrage** : lire `station_count`, les stations activées et les options
   GUI. Conserver toute station déjà connectée et `client_usable` (200 avec
   contenu, pas de 429 actif). Arrêter les stations hors `station_count`.
   Compléter uniquement les stations manquantes ou non utilisables avec les
   meilleures IP disponibles.
1. Toutes les 5–10 s, `RepairOrchestrator` lit l'état des stations et du
   quota.
2. Pour chaque station, il calcule `client_usable` :
   tunnel connecté + egress vérifié + 200 récent + pas de 429 actif pour le
   modèle demandé.
3. Si `client_usable=false`, il marque la station `DRAINING`, la retire du
   routage, et demande une remplaçante **pour cette station uniquement**.
4. Le sélecteur prend la **meilleure IP disponible** du `WarmPool` ou du
   classement, sans contrainte de pays. Il vérifie qu'elle n'est assignée à
   aucune autre station.
5. Le `PreflightProber` valide l'endpoint ; si échec, candidat suivant en
   < 2 s.
6. Le `Binder` vérifie le manifeste du conteneur cible, écrit l'override,
   recrée, attend l'egress.
7. Le `Probe` teste le 200 avec contenu ; si 200, l'IP entre dans
   `EgressRegistry` et la station devient `USABLE`.
8. Si 429 daily, l'IP est `EXHAUSTED` avec `reset_at` ; le candidat suivant
   est pris immédiatement. Si no-egress, le hostname est marqué et backoffé.
9. Si aucun candidat classé ne marche, le balayage catalogue reprend en
   arrière-plan, par lots variés, sans quota par pays.

### 4.3 Machine à états d'une station

```text
BOOT
  │
  ▼
CONNECTING ──► VERIFYING ──► USABLE
  │               │             │
  │               │             ├─ 429 daily ──► DRAINING ──► REPAIRING
  │               │             ├─ proactive ON + headroom < seuil ──► DRAINING
  │               │             └─ tunnel down ──► REPAIRING
  │               │
  │               └─ probe fail ──► REPAIRING
  │
  └─ auth/key ──► BROKEN (parked, pas de rotation)
```

Règles :

- `USABLE` exige un 200 avec contenu dans `proof_ttl_s` et aucun 429 actif.
- `DRAINING` n'est plus sélectionné pour de nouvelles requêtes, mais peut
  finir les requêtes en cours.
- `REPAIRING` est exclusive **par station** : un seul job par station, mais
  plusieurs stations peuvent réparer en parallèle. Aucun gel global n'est
  déclenché par un 429 quota.
- `BROKEN` est réservé aux causes non réparables par une IP (auth, clé,
  credentials) ; une station `BROKEN` ne consomme pas de budget de rebind.
- `STANDBY` est une station au-delà du budget de sessions ; elle n'est pas
  comptée comme `BROKEN`. Elle ne doit pas partager l'IP d'une station
  active.

---

## 5. Algorithmes

### 5.0 Comment on obtient une IP réellement utilisable

L'API NordVPN et le manifeste Gluetun ne fournissent **pas** l'IP d'egress ni
le quota free. Ils fournissent des serveurs (hostname, endpoint, pays, clé
WG). L'IP d'egress et le quota ne s'obtiennent qu'en se connectant et en
testant :

```text
1. Catalogue      : choisir un serveur (API + Gluetun) → hostname + endpoint
2. Préflight      : l'endpoint répond-il ? (handshake WG / RTT)
3. Bind           : recréer la station sur ce hostname, vérifier le manifeste
4. Egress         : lire l'IP publique à travers le tunnel (api.ipify.org, etc.)
5. Probe free     : appeler l'endpoint officiel → 200 avec contenu, ou 429
6. Enregistrement : IP, hostname, pays, latence, débit, statut, reset_at
7. Classement     : score composite, disponibilité, unicité d'IP
```

Une IP n'entre dans le classement qu'après l'étape 5. Une IP 429 reste
connue mais indisponible jusqu'à `reset_at`. C'est ce cycle qui permet de
« passer de la plus performante à la moins performante », puis de réutiliser
une performante redevenue disponible.

### 5.1 Score composite d'une IP

Le score doit refléter la **performance réelle récente** d'une IP, pas le load
API ni son pays. Proposition de formule, poids en configuration :

```text
score_ip =
    0.40 * success_rate_recent     # 200 / (200 + 429 + bind_error), lissé
  + 0.25 * latency_score           # 1 - min(1, p50_ms / budget_ms)
  + 0.20 * throughput_score        # min(1, tokens_per_s / target_tps)
  + 0.10 * stability               # 1 - min(1, stdev / p50)
  + 0.05 * freshness               # exp(-age_h / freshness_ttl_h)
```

Contraintes :

- Le score est calculé depuis `observations` (hostname, IP, statut, date,
  station) sur une fenêtre récente (7 jours par défaut), avec un minimum de
  `min_samples` observations. Les compteurs `ip_state.tests/successes` ne
  sont pas utilisés : ils sont pollués par l'import.
- `success_rate_recent` utilise un lissage de Laplace pour ne pas surclasser
  une IP avec 1/1 succès.
- Le **pays et le continent ne sont pas des facteurs de score**. La source
  (`api`/`gluetun`) ne départage qu'à score strictement égal ; la fraîcheur
  est un composant mineur (5 %).
- La disponibilité est une **porte**, pas un poids : une IP `EXHAUSTED` avant
  `reset_at`, une IP déjà assignée ou une IP sans preflight OK est exclue du
  classement.
- Le headroom de quota est un **départage**, pas un poids : il ne doit pas
  faire passer une IP moins performante devant une meilleure. Il sert
  seulement à choisir entre deux scores égaux.
- L'exploration (hostnames/IP non testés) se fait dans le **balayage de
  découverte** ; elle ne s'applique pas à l'affectation d'une station qui a
  besoin d'une IP.
- Si une métrique manque (pas de latence, pas de débit), utiliser une valeur
  **neutre** avec une pénalité de confiance, jamais zéro : une IP peu mesurée
  ne doit pas être condamnée.
- Le score est déterministe et testé unitairement.
- Pour une **IP déjà connue** (pool chaud, registre), on utilise `score_ip`.
  Pour un **hostname pas encore bindé**, on utilise un `score_host` agrégé :
  taux de bind réussi, taux de 200 des IP déjà produites par ce hostname, et
  fraîcheur. L'IP obtenue après bind est ensuite notée individuellement.
  Cela évite de devoir connaître l'IP avant de choisir le serveur.

### 5.2 Sélection indépendante avec unicité d'IP

```text
besoin = station sans IP utilisable
occupées = IP assignées à toutes les autres stations (USABLE, DRAINING,
           REPAIRING, STANDBY connectée)

candidats = IP connues avec :
    preflight_ok
    manifest_ok(station)
    reset_at null ou passé
    état disponible (pas EXHAUSTED, pas en cooldown)
    IP pas dans occupées
    (pays autorisé si un allowlist GUI est configuré — optionnel)
  + hostnames non bindés avec :
    score_host, preflight_ok, manifest_ok(station)
    (leur IP sera connue après bind)

trier par score_ip (IP connue) ou score_host (hostname)
prendre le premier
si aucun : capacity_degraded (toutes épuisées / assignées / aucun candidat)
```

Règles dures :

- **Aucune contrainte par pays ni par continent.** Toutes les stations
  peuvent utiliser n'importe quel pays.
- **Une IP = une station.** L'identité est l'IP d'egress, pas le hostname :
  deux hostnames différents qui produisent la même IP sont considérés comme
  un doublon. L'unicité est vérifiée à la promotion et à l'assignation, y
  compris pour les stations `STANDBY` connectées.
- Si deux stations ont besoin d'une IP en même temps, elles prennent les deux
  meilleures IP distinctes ; la seconde ne doit jamais attendre la première
  pour une raison de pays.
- Un hostname ou un pays peut être temporairement dépriorisé après N échecs,
  mais jamais exclu durablement et jamais bloquant pour les autres.
- Aucun hostname, pays ou préfixe d'IP en dur. Un test de régression scanne
  les modules de sélection.
- Une préférence de diversité (pays ou /24) peut être ajoutée comme **option
  GUI désactivée par défaut** ; elle ne doit jamais devenir une porte dure
  sans action explicite de l'opérateur.

### 5.3 Gestion du 429

```text
sur 429(status, retry_after, body):
    scope = classify(body)   # daily | rate_limit | transient | unknown
    si scope == daily:
        ip.reset_at = now + retry_after (ou reset fournisseur)
        ip.state = EXHAUSTED
        station.state = DRAINING
        warm = warm_pool.pop_best_available(exclude_assigned=True)
        si warm: promote(warm)
        sinon: repair(station, reason="quota_429")  # job indépendant
    si scope == transient:
        ip.cooldown = short_backoff
        retry plus tard, ne pas blacklister
    si scope == rate_limit:
        ip.cooldown = min(retry_after, rate_limit_cap)
    si scope == unknown:
        traiter comme transient, journaliser
```

Points clés :

- **Toujours** déclencher la réparation sur un 429 daily, même si la station
  est pinnée et même si un autre modèle de probe a du quota.
- Le pool ne doit plus appeler `_launch_rotation()` / `connect_next()` sur un
  429 : il publie un **besoin de réparation** dans la file du superviseur.
  C'est la contrepartie de FR-129 (un seul propriétaire).
- Chaque 429 crée un **job indépendant** pour la station concernée. Deux
  stations en 429 ont deux jobs parallèles ; aucun `global_429_pause` ne doit
  les bloquer.
- Ne jamais re-tester une IP avant `reset_at`.
- Ne jamais condamner un hostname à cause d'un 429 : le hostname peut
  produire une autre IP avec un bucket frais.
- Le 429 sur un modèle ne doit pas condamner les autres modèles si le bucket
  est réellement par modèle ; si le bucket est `default`, l'IP est condamnée
  pour tous, et c'est explicite.
- Une IP performante qui atteint `reset_at` redevient **disponible** et
  réintègre le classement ; elle sera utilisée à la prochaine rotation qui en
  a besoin, sans bascule proactive.

### 5.4 Pool chaud

- Taille cible : `warm_pool_size` = `station_count` + 3 (9 par défaut).
- Une IP entre dans le pool après un 200 avec contenu, un quota non épuisé
  et une IP non déjà assignée. Le pays n'entre pas dans le critère.
- TTL : `warm_ttl_s` (ex. 6 h) ; une IP plus vieille est re-testée.
- Le pool est réconcilié au boot depuis SQLite ; les leases expirés sont
  libérés.
- Une IP du pool ne peut être promue que si elle n'est pas déjà assignée.
- Le pool est réapprovisionné en arrière-plan, jamais sur le chemin synchrone
  d'une requête client.

### 5.5 Balayage catalogue

**Préflight (rapide, parallèle) :**

- Résoudre l'endpoint IP (API) et vérifier que le hostname est dans le
  manifeste du conteneur cible.
- Envoyer une initiation de handshake WireGuard avec la clé **partagée** et
  attendre la réponse (ou un timeout court).
- Mesurer le RTT, marquer `preflight_ok` / `preflight_failed`.
- Parallélisme : `preflight_workers` (ex. 64), sans créer de tunnel complet.
- Un échec de préflight coûte < 2 s et n'écrit pas d'override.

**Bind (coûteux, séquentiel par station) :**

- Seuls les `preflight_ok` passent au bind.
- Le binder vérifie le manifeste, écrit l'override, recrée, attend l'egress.
- Si `no-egress`, marquer `bind_error` avec la cause et backoff.
- Si 429, marquer l'IP `EXHAUSTED` et continuer immédiatement.
- Si 200, promouvoir et arrêter le balayage pour cette station.
- Les binds de stations différentes peuvent tourner en parallèle ; un bind
  par station à la fois.

**Ordre :**

1. Pool chaud : meilleure IP disponible.
2. Toutes les IP disponibles classées par score composite (API et Gluetun
   mélangés ; la source ne départage qu'à score égal).
3. Hosts/IP non testés, en variant les pays et les sources pour la découverte.
4. Re-test des hosts en backoff expiré.
5. Aucun quota par pays : un pays performant peut être visité plusieurs fois.

**Progression :**

- `SweepCursor` persiste pays, hostname, dernier test, prochain éligible.
- Un redémarrage reprend au curseur ; aucun reset à zéro.
- Le balayage est interrompu dès qu'une station trouve un 200.
- Le balayage ne bloque jamais une requête client : il tourne en fond, avec
  un budget de binds/min.

### 5.6 Vérification du manifeste

Avant chaque bind :

```text
manifest = charger_manifeste_du_conteneur(station)
si hostname absent:
    synchroniser_manifeste(station)
    vérifier_à_nouveau(hostname)
    si toujours absent:
        marquer manifest_missing
        candidat suivant (sans bind)
```

- La synchronisation doit être atomique (tmp + rename) et vérifiée par une
  lecture dans le conteneur (`docker exec grep`).
- Le manifeste installé doit inclure l'API courante et le manifeste Gluetun.
- Épingler la version de l'image Gluetun (`qmcgaw/gluetun:vX.Y.Z`) pour
  éviter les changements de format de manifeste.
- Un hostname `manifest_missing` ne doit jamais être enregistré comme
  `no-egress` : la cause est différente.

### 5.7 Réparation et leases

- Un seul job de réparation **par station** ; plusieurs stations peuvent
  réparer en parallèle. Le superviseur est le seul propriétaire.
- `max_parallel_repairs` = `station_count` (ou paramètre GUI), borné par le
  budget de sessions. Aucun gel global pour un 429 quota.
- `lease_ttl_s` > `bind_timeout_s + marge` (ex. 300 s pour un bind de 60 s).
- Le lease est renouvelé à chaque étape (preflight, bind, probe).
- Un lease orphelin expire et est libéré au boot.
- Le binder et le probe sont idempotents : rejouer un bind ne doit pas
  produire deux assignments.
- La promotion d'une IP est atomique : `UPDATE ... WHERE assigned_station IS
  NULL`.

### 5.8 Réconciliation et démarrage

Au boot :

1. Charger `station_count`, `disabled_stations`, `max_sessions`, options GUI.
2. Vérifier la cohérence config / `.env` / compose / conteneurs.
3. **Conserver** toute station déjà connectée et `client_usable` (probe 200
   avec contenu, pas de 429 actif). Ne pas la recréer. Une station connectée
   sans preuve récente est testée 2–3 fois avec un court délai avant d'être
   déclarée non utilisable : un timeout isolé ne doit pas provoquer une
   rotation.
4. Arrêter les stations hors `station_count` (ex. station 7).
5. Expirer les leases orphelins.
6. Recalculer `client_usable` depuis SQLite et les probes récentes.
7. Compléter uniquement les stations manquantes ou non utilisables avec les
   meilleures IP disponibles.
8. Démarrer le superviseur et le watchdog.
9. Réapprovisionner le pool chaud en arrière-plan.

### 5.9 Matrice de fallback

Chaque dépendance a une chaîne ordonnée, configurable, avec timeout et
backoff par entrée :

| Dépendance | Chaîne de fallback |
|---|---|
| Catalogue serveurs | API NordVPN → cache API → manifeste Gluetun → embarqué → registre local |
| Endpoint serveur | endpoint API `station` → résolution DNS → IP du manifeste → cache |
| Manifeste station | volume de la station → cache fusionné → embarqué → resynchronisation |
| Image Gluetun | version épinglée → version précédente connue → image locale |
| DNS | DoH Cloudflare → DoH Google → DoT Quad9 → plain `1.1.1.1` → plain `8.8.8.8` → resolver hôte |
| Site d'IP | SOCKS5h + liste d'URLs → SOCKS5 + liste → proxy HTTP + liste → API contrôle Gluetun `/v1/publicip/ip` → `docker exec` + liste |
| Bind | Compose override → API contrôle Gluetun (si hostname supporté) → redémarrage Docker avec pays |
| Probe free | modèle principal → modèles de secours (`probe_models`) → retry transport |
| Transport VPN | WireGuard → OpenVPN UDP → OpenVPN TCP → serveur suivant → boucle |
| Superviseur | watchdog interne → relance du superviseur → redémarrage conteneur → alerte |
| Dashboard | API locale → fichier d'état → logs | 

Règles :

- Le resolver hôte n'est utilisé que pour le **control-plane** (catalogue,
  API, dashboard) ; jamais pour le trafic de la station, pour éviter une fuite
  DNS hors tunnel.
- Un fallback ne doit **jamais** recréer une station déjà utilisable.
- Chaque bascule est journalisée avec la source, la raison et la durée.
- Après `N` échecs consécutifs sur une entrée, elle entre en cooldown et la
  suivante est utilisée ; elle est re-testée après expiration.
- Une chaîne qui échoue entièrement produit `DEGRADED` avec la liste des
  chemins essayés, pas un `BROKEN` silencieux.

### 5.10 Transport VPN auto : WireGuard → OpenVPN UDP → OpenVPN TCP

En mode `vpn_stack: auto`, le sélecteur ne choisit pas une technologie
globale pour la station : il essaie les trois **pour le serveur candidat**.

```text
pour chaque serveur candidat:
    pour transport dans ordre_configuré(transport_order):
        si serveur ne supporte pas transport: continuer
        si transport en cooldown: continuer
        bind(serveur, transport)
        si bind échoue: enregistrer transport_attempt; continuer
        egress = détecter_ip()          # avec ses propres fallbacks
        si pas d'egress: enregistrer; continuer
        résultat = probe_free()
        si 200 avec contenu:
            garder (serveur, transport, IP); retour
        si 429 daily:
            enregistrer IP EXHAUSTED/reset_at; continuer
        sinon:
            enregistrer; continuer
    # les trois transports ont échoué pour ce serveur
    marquer serveur en échec/backoff
# tous les serveurs ont échoué
attendre backoff, puis boucler sur le premier transport
```

Points clés :

- `transport_order` par défaut : `wireguard`, `openvpn-udp`,
  `openvpn-tcp` (configurable GUI).
- Un transport non supporté par le serveur (technologies API/manifeste) ne
  compte pas comme un échec.
- Le même serveur peut produire des IP différentes selon le transport ; une
  IP 429 sur un transport n'interdit pas d'essayer le suivant.
- Chaque station a son propre pointeur de transport ; aucune station n'est
  forcée sur un slot fixe (`s1=WG`, `s2=OV TCP`, etc.).
- Les essais OpenVPN passent par le budget AUTH global et un backoff ; si
  tous les transports échouent avec `auth`, la station est `BROKEN`/parked,
  pas en boucle chaude.
- `vpn_stack: wireguard|openvpn-udp|openvpn-tcp` force un transport ; `auto`
  active la boucle.
- Le support OpenVPN existe déjà côté binder (`vpn_type`, `OPENVPN_PROTOCOL`),
  mais le test ponctuel UK OV TCP n'a pas produit d'egress en 45 s. La Phase 0
  doit valider OV UDP/TCP avec un timeout adapté et un serveur dédié avant de
  considérer la boucle comme acquise.

### 5.11 Détection d'IP : chaîne de fallback

```text
1. SOCKS5h + [api.ipify.org, icanhazip.com, checkip.amazonaws.com,
              ipinfo.io/ip, ifconfig.me/ip]
2. SOCKS5  + mêmes URLs
3. HTTP proxy (8887+N) + mêmes URLs
4. Gluetun control API: GET /v1/publicip/ip
5. docker exec <station> wget -qO- <URL>
6. si tout échoue mais probe officiel == 200:
       marquer ip_unknown, retenter en fond, ne pas affecter la station
```

- Chaque URL a un timeout court et un backoff.
- L'IP retournée doit être une IP valide ; une page d'erreur ou un HTML n'est
  pas une IP.
- Une IP `ip_unknown` ne peut pas être affectée tant que l'unicité n'est pas
  prouvée ; la station reste `DEGRADED` mais n'est pas jetée.
- Le probe officiel 200 est la **preuve ultime** que le tunnel fonctionne ;
  il ne doit pas dépendre d'un site d'IP externe.

### 5.12 DNS : chaîne de fallback

- Configurer Gluetun avec plusieurs resolvers (DoH/DoT/plain) et un ordre
  explicite ; ne jamais dépendre d'un seul provider.
- Si le resolver courant échoue, basculer sur le suivant sans recréer la
  station si possible ; sinon recréer avec la configuration de secours.
- Le resolver utilisé et l'échec sont journalisés.
- Un échec DNS ne doit pas être classé `no-egress` : c'est `dns_error`, une
  cause distincte, réparable sans condamner l'IP.
- Pour les appels critiques (API catalogue, manifeste), utiliser le resolver
  hôte en secours, avec cache disque.

### 5.13 Contrat GUI : chaque option a un effet

Chaque contrôle du GUI doit être décrit dans un manifeste :

```text
gui_option:
  key: ip_registry.selection.score_weights.latency
  consumer: free/ip_registry/scoring.py
  effect: change le classement des IP
  test: tests/test_gui_contract.py::test_latency_weight_changes_ranking
```

Exemples d'options et de consommateurs attendus :

| Option GUI | Consommateur runtime | Effet observable |
|---|---|---|
| `station_count` | superviseur + compose | nombre de stations |
| `vpn_stack` / `transport_order` | manager + binder | technologie essayée |
| `proactive_rotation.enabled` | pool | rotation avant épuisement |
| `score_weights.*` | sélecteur | ordre des IP |
| `warm_pool_size` | pool chaud | nombre de spares |
| `preflight.enabled` | prober | saut des endpoints morts |
| `max_parallel_repairs` | superviseur | parallélisme des réparations |
| `allow_countries` | sélecteur | pays éligibles (vide = tous) |
| `dns.order` | manager/container | resolver utilisé |
| `ip_echo.urls` | egress | site de test d'IP |
| `sweep.*` | balayage | débit et durée |

Règles :

- Une option sans `consumer` ou sans `test` fait échouer la CI.
- Une option « affichage seul » doit être explicitement marquée.
- Le GUI affiche la valeur **effective** et la date de dernière application.
- `scripts/config_audit.py --strict` doit passer ; le contrat GUI complète
  l'audit `yaml_get` pour les options lues via `cfg.get`/`update_config`.
- Toute option qui ne change rien est supprimée : pas de faux contrôle.

---

## 6. Modèle de données

### 6.1 Tables principales (SQLite)

```text
catalog_server(
  hostname PK, country, city, load, technologies,
  endpoint_ip, wg_pubkey, source, api_seen_at,
  manifest_seen_at, stale, first_seen, last_seen, score_host
)

endpoint_test(
  id PK, hostname, endpoint_ip, test_type, transport,
  ok, rtt_ms, tested_at, error, fallback_source
)

egress_ip(
  ip PK, hostname, country, asn, first_seen,
  last_ok_at, last_429_at, reset_at, successes,
  failures, latency_ms, throughput_tps, score_ip, state
)

station(
  id PK, container, socks_port, transport_effective, transport_order,
  state, country, current_ip, current_host, client_usable,
  last_probe_at, last_200_at, reason
)

lease(
  id PK, hostname, station, started_at, expires_at,
  renewed_at, status
)

warm_spare(
  ip PK, hostname, country, model, verified_at,
  expires_at, score
)

repair_job(
  id PK, station, cause, started_at, finished_at,
  candidates_tried, transports_tried, result, last_error
)

sweep_cursor(
  id PK, country, hostname, last_tested_at,
  next_eligible_at, attempts, failures
)

observation(
  id PK, station, hostname, ip, transport, status, valid,
  retry_after_s, reset_at, latency_ms, model,
  tested_at, error
)

fallback_event(
  id PK, dependency, from_source, to_source, reason,
  station, started_at, duration_ms, success
)

incident(
  id PK, scope, started_at, ended_at, cause, remediation
)
```

### 6.2 Index et contraintes

- `egress_ip.state`, `egress_ip.reset_at`, `egress_ip.country`.
- `catalog_server.country`, `catalog_server.source`, `stale`.
- `lease.hostname` unique tant que `status='active'`.
- `egress_ip` unique par IP ; une IP ne peut être assignée qu'à une station.
- `observation` indexé par `tested_at`, `hostname`, `ip`, `status`,
  `transport`.
- `warm_spare` indexé par `expires_at`, `country`.
- `fallback_event` indexé par `dependency`, `started_at`, `station`.

### 6.3 Migration depuis l'existant

- Conserver `ip_registry.db` comme base de départ.
- Supprimer ou archiver les 3 286 lignes `hostname=''` (import d'inventaire)
  après sauvegarde ; elles ne sont pas bindables.
- Fusionner `catalog_claims.db` dans `catalog_server` + `lease`.
- Conserver `fresh_chain_state*.json` en lecture seule pour l'historique,
  puis les archiver.
- Ne pas supprimer `requests.db` : il reste la source de qualité réelle.

---

## 7. Configuration cible

| Clé actuelle | Valeur | Problème | Cible |
|---|---|---|---|
| `ip_registry.allocator.mode` | `active` | deux propriétaires | un seul propriétaire |
| `ip_registry.allocator.max_candidates_per_chain` | `20` | plafond arbitraire | `0` (illimité) ou `catalog_sweep_batch` |
| `ip_registry.allocator.rebind_limit_per_min` | `6` | bride le balayage | `12` (paramétrable) |
| `ip_registry.allocator.max_per_country` | `2` | tri obsolète | supprimé (aucun quota pays) |
| `ip_registry.allocator.max_per_24` | `2` | porte dure | supprimé ; préférence /24 optionnelle GUI |
| `ip_rotation.server_countries` | liste de 150 pays | périmètre hard | vide = tous les pays ; allowlist GUI optionnelle |
| `ip_registry.allocator.host_429_cooldown_s` | `10` | re-test trop tôt | `600` (ou reset) |
| `ip_registry.allocator.no_egress_backoff_s` | `300` | ok mais long | `120` avec préflight |
| `ip_registry.allocator.no_egress_backoff_max_s` | `3600` | ok | `86400` |
| `ip_registry.allocator.settle_s` | `12` | lent | `6` pour préflighté |
| `ip_registry.allocator.egress_wait_s` | `45` | lent | `20` pour préflighté |
| `ip_registry.allocator.lease_ttl_s` | `300` | inutilisé par les claims | `300` et appliqué |
| `ip_rotation.country_rotation` | `false` | rotation pays inutile | supprimé (le catalogue choisit l'IP) |
| `ip_rotation.server_pick_mode` | `gluetun` | pas de hostname | `catalog` (nouveau) |
| `ip_rotation.use_nordvpn_api` | `false` | clé morte | remplacée par `catalog.sources` |
| `ip_rotation.vpn_stack` | `auto` | flips globaux, pas de boucle par serveur | `auto` = tri-stack par candidat |
| `ip_rotation.auto_mixed_stacks` | `false` | slots fixes par station | supprimé (chaque station essaie les 3) |
| `ip_registry.allocator.transports` | wireguard partout | force WG | remplacé par `transport.order` |
| `ip_rotation.station_repair.enabled` | `true` | double réparation | `false` (superviseur seul) |
| `ip_rotation.station_count` | `6` | station 7 en marche | `6` + arrêt station 7 |
| `ip_rotation.bad_ip_ttl_s` | `900` | ok | `900` |
| `ip_rotation.quota_ip_block_s` | `300` | court | `reset_at` |

Nouvelles clés proposées :

```yaml
ip_registry:
  catalog:
    api_ttl_s: 21600
    gluetun_ttl_s: 86400
    sources: [api, gluetun]   # API pour la fraîcheur, Gluetun pour la couverture
  preflight:
    enabled: true
    workers: 64
    timeout_s: 2.0
    handshake: true
  selection:
    unique_ip: true           # seule contrainte dure
    allow_countries: []        # vide = tous les pays ; allowlist GUI optionnelle
    deny_countries: []         # vide par défaut
    prefer_distinct_countries: false  # option GUI, désactivée par défaut
    prefer_distinct_subnets: false    # option GUI, désactivée par défaut
    exploration_pct: 0.05      # balayage de découverte uniquement
    min_success_rate: 0.25
    min_samples: 5
    warm_pool_size: 9
    warm_ttl_s: 21600
    score_weights:
      success: 0.40
      latency: 0.25
      throughput: 0.20
      stability: 0.10
      freshness: 0.05
  proactive_rotation:
    enabled: false             # option GUI, désactivée par défaut
    threshold_pct: 80
  repair:
    max_parallel: 6            # = station_count, borné par le budget de sessions
    # shared_wireguard_key retiré le 2026-11-10 : transports indépendants
  sweep:
    enabled: true
    batch_size: 200
    hosts_per_min: 120
    binds_per_min: 12
    max_duration_h: 24
  fallbacks:
    dns:
      order: [cloudflare-doh, google-doh, quad9-dot, 1.1.1.1, 8.8.8.8]
      timeout_s: 3
    ip_echo:
      urls: [https://api.ipify.org, https://icanhazip.com,
             https://checkip.amazonaws.com, https://ipinfo.io/ip,
             https://ifconfig.me/ip]
      schemes: [socks5h, socks5, http]
      gluetun_control: true
      docker_exec: true
    catalog:
      order: [api, api_cache, gluetun, embedded, registry]
    manifest:
      order: [station_volume, merged_cache, embedded]
    bind:
      order: [compose, control_api, docker_restart]
  gui_contract:
    enabled: true
    strict: true

ip_rotation:
  vpn_stack: auto              # auto | wireguard | openvpn-udp | openvpn-tcp
  transport_order: [wireguard, openvpn-udp, openvpn-tcp]
  transport_cycle_timeout_s: 45
  transport_auth_backoff_s: 90
```

---

## 8. Budget de performance

Hypothèses :

- 9 787 serveurs Gluetun, 6 948 API.
- Préflight : 0,5–2 s par serveur, 64 workers → 9 787 / 64 × 1 s ≈ **2,5 min**
  en théorie, **< 30 min** en pratique avec timeouts.
- Bind : 15–30 s par candidat préflighté ; jusqu'à 6 stations → 6 réparations
  parallèles maximum (bornées par le budget de sessions).
- Balayage complet : **< 24 h** en arrière-plan, sans bloquer le service.
- Remplacement 429 : pool chaud → **< 10 s** ; réparation → **< 60 s p95**.

Sans préflight, 9 787 × 60 s / 6 chaînes ≈ **27 h**. C'est la différence
entre « tester le catalogue » et « attendre plus d'un jour ».

---

## 9. Plan par phases

### Phase 0 — Garde-fous et baseline (0,5–1 j)

**Objectif** : figer l'état, mesurer, éviter les régressions.

- T001 : baseline SLO (routable, healthy, 429 par IP, bind success).
- T002 : test de régression « aucun littéral pays/hostname/IP ».
- T003 : test de cohérence config / `.env` / compose / conteneurs.
- T004 : test de non-régression sur les bugs identifiés (station 6
  manifeste, lease 15 s, station 7, lignes sans hostname).
- T005 : snapshot DB + export des catalogues.

**Sortie** : baseline chiffrée, tests rouges qui décrivent les bugs.

### Phase 1 — Quick wins (1–2 j)

**Objectif** : arrêter l'hémorragie sans changer l'architecture.

- T010 : supprimer `PROVEN_HOSTS` et `_is_gold_ip` du chemin de sélection.
- T011 : classer par qualité observée, API et Gluetun mélangés ; la source ne
  départage qu'à score égal.
- T012 : vérifier le manifeste du conteneur avant le bind ; marquer
  `manifest_missing`.
- T013 : porter le lease de claim à 300 s et le renouveler.
- T014 : désactiver `station_repair.enabled` (superviseur seul).
- T015 : arrêter/désactiver la station 7 hors `station_count`.
- T016 : purger/archiver les 3 286 lignes `hostname=''`.
- T017 : supprimer le `return` du 429 sur station pinnée ; publier un besoin
  de réparation **pour cette station uniquement**.
- T018 : exposer `client_usable` par station et par modèle dans le dashboard.
- T019 : test de charge : 20 candidats, mesurer bind success avant/après.
- T023–T026 : chaînes de fallback IP/DNS, contrat GUI, tri-stack auto
  (détail dans `tasks.md`).

**Sortie** : plus de cantonnement Albanie, plus de crash loop manifeste,
plus de double réparation, 429 pris en charge.

### Phase 2 — Sélecteur catalogue (3–5 j)

**Objectif** : remplacer la boucle actuelle par le sélecteur.

- T020 : schéma `catalog_server`, `endpoint_test`, `egress_ip`, `lease`,
  `warm_spare`, `sweep_cursor`.
- T021 : `CatalogService` (fusion API + Gluetun, provenance, fraîcheur).
- T022 : `Selector` (score composite, unicité d'IP, tous pays, exploration de
  découverte).
- T023 : `WarmPool` (TTL, promotion atomique, réapprovisionnement).
- T024 : `RepairOrchestrator` (état station, un job par station, jobs
  parallèles entre stations, leases).
- T025 : gestion 429 par modèle et par IP (`daily`, `transient`,
  `rate_limit`), sans gel global quota.
- T026 : `SweepCursor` persistant + reprise au boot.
- T027 : tests unitaires du score composite, de l'unicité d'IP, du 429, des
  leases, du parallélisme.
- T028 : test d'intégration avec binder/probe factices (100 hosts).
- T039 : transport auto tri-stack (WireGuard → OpenVPN UDP → OpenVPN TCP),
  boucle par serveur, budget AUTH.
- T040 : chaînes de fallback IP echo / DNS / catalogue / manifeste / bind.
- T041 : contrat GUI + audit config strict + tests d'effet par option.

**Sortie** : sélection par performance, pool chaud, réparation < 60 s,
0 IP partagée, zéro hardcode, fallbacks opérationnels, options GUI effectives.

### Phase 3 — Performance et préflight (3–5 j)

**Objectif** : rendre le balayage complet viable.

- T030 : `PreflightProber` (DNS, endpoint, handshake WireGuard, RTT).
- T031 : parallélisation 64 workers + budget hosts/min.
- T032 : bind rapide (settle 6 s, egress wait 20 s) pour les préflightés.
- T033 : vérification du manifeste par conteneur + sync atomique.
- T034 : épinglage de l'image Gluetun + test de compatibilité manifeste.
- T035 : mesure du taux de préflight OK et du taux de bind OK.
- T036 : test de balayage sec sur 1 000 hosts, puis 9 787.
- T037 : dashboard sweep progress / ETA.

**Sortie** : balayage complet < 24 h, bind réussi ≥ 60 % sur préflightés.

### Phase 4 — Fiabilité et SLO (2–4 j)

**Objectif** : tenir la durée.

- T040 : rotation préventive **optionnelle** (GUI, désactivée par défaut) ;
  aucun seuil actif si l'option est off.
- T041 : incident journal + alertes (`client_usable < 5`, `warm_pool < 3`,
  `capacity_degraded`, `duplicate_ip`).
- T042 : re-test programmé des hosts/pools en cooldown.
- T043 : purge/renouvellement des leases orphelins.
- T044 : test de panne : tuer une IP, un pays, un /24, le superviseur.
- T045 : test de redémarrage : reprise au curseur, pas de double bind.
- T046 : test d'unicité : 6 stations, 6 IP distinctes, 0 IP partagée ;
  test de démarrage : 2 stations utilisables conservées, 4 complétées.

**Sortie** : 6/6 routable ≥ 99 %, 0 station connectée non utilisable,
0 IP partagée.

### Phase 5 — Validation et déploiement (2–3 j)

**Objectif** : passer en production sans régression.

- T050 : shadow mode (sélecteur observe, ne bind pas) 24 h.
- T051 : canary 1 station pendant 24 h.
- T052 : canary 3 stations, puis 6.
- T053 : soak 72 h avec trafic réel.
- T054 : comparer SLO avant/après ; critères de rollback.
- T055 : documentation opérateur (dashboard, causes, actions).
- T056 : revue post-incident et ajustement des poids.

**Sortie** : 6/6, 0 IP partagée, zéro intervention manuelle sur 72 h.

### Phase 6 — Décommissionnement et dette (3–5 j)

**Objectif** : retirer l’ancien chemin et fermer la dette P1/P2.

- T070 : supprimer `fresh_chain*` et `catalog_claims` (shims → suppression).
- T071 : archiver les JSON d’état legacy et les tables non utilisées.
- T072 : extraire les monolithes par lots (`vpn/manager.py`,
  `free/pool.py`, `opencode.py`) ; aucun nouveau module > 1 500 lignes.
- T073 : fermer les dettes P1/P2 de `debt.md` ; mettre à jour le registre.
- T074 : revue d’architecture post-migration (ADR, imports, tests).
- T075 : documentation finale et runbook.

**Sortie** : 0 P0/P1 ouverte, legacy supprimé, architecture cible stable.

---

## 10. Stratégie de tests

### Unitaires

- Score : mêmes entrées → même score ; hostname 93 % > hostname 3 % ; une IP
  avec 1/1 succès ne dépasse pas une IP avec 100/100.
- Unicité : deux stations ne peuvent jamais recevoir la même IP, y compris
  en promotion concurrente.
- Tous pays : aucun pays n'est interdit par défaut ; un host Gluetun-only
  performant peut dépasser un host API médiocre.
- Hardcode : scan du code, échec si `al\d+`, `87.120.102`, nom de pays.
- 429 : daily → `reset_at` ; transient → pas de blacklist ; rate_limit →
  cooldown court ; pas de pause globale sur quota.
- Lease : 300 s > bind 60 s ; expiration ; reprise.
- Manifeste : host absent → `manifest_missing`, pas `no-egress`.
- Warm pool : promotion atomique, pas de double assignation.
- Sweep : reprise au curseur, pas de re-test avant backoff.
- Reset : une IP épuisée sort du classement, puis réintègre après `reset_at`
  et peut être réutilisée à la prochaine rotation.
- Fallback IP : site principal KO → site 2 ; tous les sites KO + probe 200 →
  `ip_unknown`, station non jetée.
- Fallback DNS : resolver principal KO → resolver 2 ; échec classé
  `dns_error`, pas `no-egress`.
- Transport auto : WG KO → OV UDP OK ; WG+UDP KO → OV TCP OK ; tout KO →
  serveur suivant puis boucle.
- GUI : chaque option du manifeste a un test d'effet ; une option sans
  consommateur fait échouer la CI.

### Intégration

- Binder/probe factices : 100 hosts avec des résultats connus ; vérifier
  l'ordre par score, l'unicité d'IP et le temps de remplacement.
- Deux 429 simultanés : deux jobs parallèles, aucune attente croisée.
- Démarrage : 2 stations utilisables conservées, 4 complétées.
- Panne du site d'IP principal : bascule vers le suivant ; panne de tous les
  sites : `ip_unknown` + probe officiel 200 → station conservée.
- Panne DNS : bascule resolver ; échec total → `DEGRADED` + fallback IP
  littérale/control API.
- Transport auto : serveur WG KO/OV TCP OK ; vérifier le bind OV TCP.
- GUI : modifier chaque option et vérifier l'effet runtime.
- Docker réel : un conteneur de test, un hostname valide, un hostname
  invalide ; vérifier `manifest_missing`.
- Redémarrage du superviseur en cours de bind : aucun double assignment.
- Panne d'un /24 : l'incident est signalé ; aucune contrainte de diversité
  n'empêche la reprise.

### Live canary

- 6 stations, 6 IP distinctes, pays variés selon la performance.
- 50 requêtes free par station ; ≥ 99 % de 200 avec contenu.
- Forcer un 429 sur une IP ; mesurer le remplacement ; vérifier que les
  autres stations ne bougent pas.
- Vérifier `warm_pool >= 6` en régime nominal.
- Vérifier 0 station `EXHAUSTED` encore routée et 0 IP partagée.

### Soak

- 72 h de trafic réel.
- `client_usable >= 5/6` ≥ 99,9 %, 6/6 ≥ 99 %.
- 0 incident P0 (bind avant reset, double assignment, station hors scope).

---

## 11. Risques et mitigations

| Risque | Impact | Mitigation |
|---|---|---|
| Le préflight handshake n'est pas fiable pour tous les serveurs | faux négatifs | seuil conservateur, re-test, fallback bind |
| Le compte NordVPN limite les sessions avec la clé partagée | file d'attente | budget de sessions mesuré, `STANDBY`, exposition |
| Le catalogue Gluetun contient des hosts morts | faux échecs | score par qualité, TTL, re-test, marquage |
| Le manifeste change avec l'image `latest` | crash loop | épingler la version, vérifier après sync |
| Le score sur-apprend les vieilles IP à 100 % | biais de sélection | fenêtre récente, lissage, exploration de découverte |
| Aucune diversité pays → panne corrélée d'un pays | outage partielle | IP unique, monitoring, option GUI de préférence pays (off par défaut) |
| Le balayage consomme trop de rebinds | AUTH rate limit | budget hosts/min, binds/min, backoff |
| Un 429 est en fait transient | rotation inutile | classification, cooldown court |
| Le pool chaud est vide au pire moment | outage | taille 9, réapprovisionnement continu |
| Deux réparateurs recréent la station | churn | un seul propriétaire, lease |
| Deux stations obtiennent la même IP pendant une promotion | quota partagé | contrainte d'unicité atomique + test P0 |
| Tous les sites d'IP tombent | faux `no-egress` | fallback control API/docker exec + probe officiel comme preuve ultime |
| Tous les DNS tombent | bind impossible | plusieurs DoH/DoT/plain + resolver hôte + cache |
| Le tri-stack boucle trop vite | AUTH rate limit | budget AUTH, backoff, cycle timeout, jitter |
| Une option GUI n'a aucun effet | fausse impression de contrôle | contrat GUI + `config_audit.py --strict` + test d'effet |
| Le dashboard montre `healthy` au lieu de `client_usable` | fausse impression | définition unique, colonne par modèle |

---

## 12. Rollout et rollback

**Rollout** :

1. Shadow : le nouveau sélecteur calcule et journalise, mais l'ancien chemin
   reste actif.
2. Canary 1 station : la station 1 utilise le nouveau chemin ; les autres
   restent sur l'ancien.
3. Canary 3 stations : 3 IP distinctes, pays choisis par performance.
4. 6 stations : bascule complète.
5. Soak 72 h.

**Rollback** :

- Interrupteur `ip_registry.catalog.enabled: false` → ancien chemin.
- Conserver les anciens fichiers d'état et la DB.
- Un rollback ne doit pas perdre les assignments en cours ; les leases sont
  réconciliés au boot.

**Critères de rollback** :

- `client_usable < 4/6` pendant > 10 min.
- `routable=0` > 1 % sur 1 h.
- Plus de 3 stations en `BROKEN`.
- Divergence de config non résolue.

---

## 13. Fichiers probablement concernés (indicatif)

| Fichier | Rôle dans le plan |
|---|---|
| `free/ip_registry/domain.py` | entités `CatalogServer`, `EgressIp`, `Lease`, `Observation` |
| `free/ip_registry/sqlite_registry.py` | schéma, `eligible`, leases, transitions |
| `free/ip_registry/allocator.py` | remplacé par `RepairOrchestrator` |
| `free/ip_registry/adapters/compose_binder.py` | vérification manifeste, bind rapide |
| `free/ip_registry/adapters/probe.py` | probe 200/contenu, classification 429 |
| `free/ip_registry/adapters/egress.py` | egress multi-URL, SOCKS/HTTP/control API/docker exec |
| `free/catalog_claims.py` | remplacé par `lease` + `sweep_cursor` |
| `scripts/fresh_chain.py` | remplacé par le sélecteur + binder + tri-stack auto |
| `vpn/manager.py` | transport auto par serveur, fallback DNS/bind, suppression des slots fixes |
| `scripts/config_audit.py` | audit config strict + contrat GUI |
| `config/gui_contract.py` | manifeste option GUI → consommateur → test |
| `dashboard/api.py` | valeur effective, fallback events, transport effectif |
| `rotation/` | nouveau package domaine/application/ports/adapters/interfaces |
| `rotation/infrastructure/db/` | schéma, migrations, repositories, rétention, backup |
| `rotation/infrastructure/net/` | egress/probe/DNS/preflight fallbacks |
| `rotation/domain/policies/` | sélection, unicité, quota, transport, fallback |
| `specs/002-vpn-catalog-rotation/architecture.md` | architecture cible + strangler |
| `specs/002-vpn-catalog-rotation/data-model.md` | schéma DB complet |
| `specs/002-vpn-catalog-rotation/debt.md` | registre de dette |
| `scripts/fresh_chain_supervisor.py` | devient `RepairOrchestrator` |
| `free/pool.py` | 429 → réparation, plus de skip pinné |
| `vpn/manager.py` | rotation catalogue ou délégation au superviseur |
| `config.yaml` | nouvelles clés, suppression des mortes |
| `dashboard/api.py` | `client_usable` par modèle, sweep progress |
| `docker-compose.yml` | stations 7–10, image épinglée, clé WG partagée |

---

## 14. Décisions

### Actées

1. **Pays** : toutes les stations peuvent utiliser n'importe quel pays,
   indépendamment ; aucun quota par pays ni par continent.
2. **Rotation** : par station, sur 429 ou manuelle, en parallèle ; pas de
   rotation de flotte synchronisée.
3. **Démarrage** : conserver les stations utilisables, compléter les
   manquantes.
4. **Propriétaire** : superviseur seul.
5. **Station 7** : supprimée.
6. **Clé WG** : commune à toutes les stations.
7. **Rotation proactive** : option GUI, désactivée par défaut.
8. **Score** : composite (taux de 200, latence, débit, stabilité, fraîcheur).
9. **Reset** : réutilisation à la prochaine rotation qui en a besoin, sans
   bascule proactive.
10. **Préflight** : accepté.
11. **Fallbacks** : maximum de fallback sur toutes les dépendances (IP, DNS,
    catalogue, manifeste, bind, probe, superviseur).
12. **Transport auto** : WireGuard → OpenVPN UDP → OpenVPN TCP, boucle par
    serveur ; chaque station indépendante.
13. **GUI** : chaque option a un effet réel, un consommateur runtime et un
    test ; aucune option inerte.
14. **Architecture** : refonte strangler, domaine pur, ports/adapters, use
    cases, workers ; pas de big-bang.
15. **Base de données** : `registry.db` source unique, schéma normalisé,
    migrations, contraintes, provenance, rétention, backup.
16. **Dette** : registre `debt.md` suivi par phase ; 0 P0 ouverte aux gates.

### Poids du score (tranchés)

1. **Taux de 200 récent : 40 %** — prédit le plus directement la capacité à
   servir.
2. **Latence : 25 %** — première perception de performance.
3. **Débit : 20 %** — qualité des réponses longues.
4. **Stabilité : 10 %** — évite les IP qui oscillent.
5. **Fraîcheur : 5 %** — départage les observations anciennes.

Ces poids sont ajustables dans le GUI et testés par le contrat GUI.

---

## 15. Definition of Done

- [ ] Toutes les stations indépendantes, tous les pays autorisés, 0 IP
      partagée, aucun hardcode.
- [ ] 429 daily → remplacement < 60 s p95, jamais de station sur IP épuisée,
      autres stations non affectées.
- [ ] `client_usable >= 5/6` ≥ 99,9 %, 6/6 ≥ 99 % sur 72 h de soak.
- [ ] Balayage complet < 24 h, reprise au curseur, statut par serveur.
- [ ] Manifeste vérifié avant chaque bind ; zéro crash loop `hostname not
      valid`.
- [ ] Un seul propriétaire de réparation ; zéro double assignment.
- [ ] Au démarrage, stations utilisables conservées ; seules les manquantes
      complétées.
- [ ] Fallbacks IP/DNS/catalogue/manifeste/bind/probe opérationnels ; un site
      d'IP ou un DNS unique ne peut pas rendre une station inutilisable.
- [ ] Transport auto : WireGuard → OpenVPN UDP → OpenVPN TCP, boucle par
      serveur, par station.
- [ ] Chaque option GUI a un effet testé ; `config_audit.py --strict` et le
      contrat GUI passent.
- [ ] Dashboard : `client_usable` par modèle, warm pool, sweep progress,
      score, reset_at, transport effectif, fallback events, raison par
      station.
- [ ] Tests unitaires, intégration, canary et soak verts.
- [ ] Documentation opérateur et procédure de rollback.
- [ ] Architecture cible en place : domaine sans I/O, ports/adapters, use
      cases, workers ; test d’import vert.
- [ ] `registry.db` source unique, migrations/backups/intégrité vérifiés ;
      `telemetry.db` séparée et purgée.
- [ ] 0 P0/P1 ouverte dans `debt.md` ; legacy `fresh_chain*`/`catalog_claims`
      supprimé.
- [ ] Aucun nouveau module > 1 500 lignes ; monolithes réduits par lots.

---

## 16. Refonte de l’architecture

La refonte est détaillée dans `architecture.md`. Résumé :

- **Hexagonal** : domaine pur / application (use cases) / ports / adapters /
  interfaces.
- **Modules** : `rotation/domain`, `rotation/application`, `rotation/ports`,
  `rotation/infrastructure`, `rotation/interfaces`.
- **Règles** : domaine sans I/O ; pas de cycle ; clock/rng/réseau/Docker
  injectés ; adapters remplaçables.
- **Workers** : reconcile, repair, sweep, catalog refresh, health, retention,
  config.
- **Migration strangler** : fondations → shadow → canary 1 station → 3 → 6 →
  décommissionnement des scripts legacy.
- **Dette** : voir `debt.md` ; aucun nouveau module > 1 500 lignes ; pas de
  nouvel état JSON autoritatif ; pas de clé config sans consommateur.

Critères de sortie :

- test d’import des règles de dépendance vert ;
- 100 cas shadow avec parité de décision ;
- 0 double assignment / 0 IP partagée ;
- `fresh_chain*` et `catalog_claims` en shim puis supprimés ;
- `vpn/manager.py` réduit aux adapters runtime.

---

## 17. Base de données serveurs / IP

Le schéma complet est dans `data-model.md`. Résumé :

- `logs/registry.db` (SQLite WAL) : **source unique** des serveurs,
  transports, endpoints, IP, quotas, leases, observations, scores, warm pool,
  sweep, jobs, fallbacks, incidents.
- `logs/telemetry.db` : requêtes réelles, rétention 7–30 j, agrégats séparés
  (le `requests.db` de 3 Go ne doit plus être la base de décision).
- Contraintes : PK/FK, `CHECK`, index uniques partiels pour l’unicité d’IP,
  migrations versionnées.
- Provenance : `source_id`, `fetched_at`, `first_seen`, `last_seen`, `stale`.
- Scores matérialisés (`score_snapshots`) à partir d’observations fenêtrées.
- Rétention, `integrity_check`, backup `VACUUM INTO`, restauration testée.
- Migration des tables/JSON legacy, archivage des lignes non bindables.

Critères de sortie :

- `registry.db` < 250 Mo en régime nominal ;
- `integrity_check=ok` ;
- 0 IP partagée ;
- 0 hostname sans provenance ;
- migration d’une copie de production < 10 min, rollback testé.

---

## 18. Dette technique

Le registre complet est dans `debt.md`. Règles :

- 0 P0 ouverte à chaque gate ;
- P1 planifiée avec phase et critère de sortie ;
- pas de nouvelle dette non listée ;
- `debt.md` mis à jour à chaque phase ;
- les monolithes sont extraits par lots, pas réécrits d’un coup.

Dettes P0 de la Phase 1 : sources de vérité multiples, hardcodes Albanie, DB
polluée, lease 15 s, deux propriétaires, fallback IP/DNS absent, pas de
migrations/backups, station 7 hors scope.

---

## 19. Phases de la refonte

La refonte s’ajoute aux phases 0–5 sans les remplacer :

| Phase | Refonte / données | Sortie |
|---|---|---|
| 0 | ADR, registre de dette, baseline DB | plan stable |
| 1 | DB v1, migrations, fallbacks, GUI contract, quick wins | 0 P0 ouverte |
| 2 | domaine/ports/adapters, sélecteur, leases, warm pool | parité shadow |
| 3 | tri-stack, préflight, sweep, télémétrie | perf |
| 4 | observabilité, rétention, backups, chaos | fiabilité |
| 5 | canary/soak/rollout | production |
| 6 | décommissionnement legacy, réduction monolithes | dette P1/P2 fermée |

