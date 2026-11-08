# Data Model — 011 Station fleet usability

**Feature** : `specs/011-station-fleet-usability/`  
**Source de vérité** : `logs/registry.db` (SQLite WAL).  
**Principes** : station, session et IP sont trois objets distincts ; un bucket
de quota n'appartient qu'à une IP ; une station ne partage jamais son egress.

## 1. Entités

### 1.1 DesiredStationSpec

Décrit ce qui doit exister, indépendamment de l'état observé.

| Champ | Type | Contraintes | Rôle |
|---|---|---|---|
| `provider` | `nordvpn \| warp \| proxies \| direct` | requis | Famille d'egress. |
| `station` | int | 1–10, unique par provider | Slot logique. |
| `generation` | int | monotone, >= 1 | Version du desired state. |
| `enabled` | bool | défaut true | Maintenance/usage. |
| `stack` | `openvpn \| wireguard \| warp` | requis pour NordVPN/WARP | Transport. |
| `transport` | `openvpn-udp \| openvpn-tcp \| wireguard \| socks5` | requis | Transport effectif. |
| `host` | str | optionnel | Hostname pinné (NordVPN). |
| `country` | str | optionnel | Pays demandé (NordVPN). |
| `port` | int | 1–65535, unique | Port SOCKS/HTTP. |
| `container` | str | unique | Nom Docker. |
| `image` | str | digest pour WARP | Image. |
| `ipv6` | bool | défaut false | Famille WARP. |
| `spec_hash` | str | dérivé | Détection de changement. |
| `updated_at` | float | epoch | Horodatage. |

**Validation** :

- `provider=nordvpn` ⇒ `stack ∈ {openvpn, wireguard}`, `container` commence par
  `opencode-vpn`, `port` dans la plage de la station.
- `provider=warp` ⇒ `stack=warp`, `image` épinglée, `port` unique, `container`
  commence par `warp-`.
- `provider=direct` ⇒ une seule ligne, pas de conteneur.
- `generation` ne peut que croître ; une génération inférieure est ignorée.

### 1.2 DesiredEndpointSpec

Pour WARP/proxys, un endpoint est un conteneur ou un proxy statique.

| Champ | Type | Contraintes | Rôle |
|---|---|---|---|
| `provider` | `warp \| proxies` | requis | Famille. |
| `label` | str | unique par provider | Identité stable. |
| `container` | str | unique | Nom Docker. |
| `host` | str | requis | `127.0.0.1` pour WARP. |
| `port` | int | unique | Port SOCKS. |
| `ipv6` | bool | requis | Famille. |
| `image` | str | requis | Image/digest. |
| `managed` | bool | requis | Le système peut recréer. |
| `enabled` | bool | défaut true | Usage/maintenance. |
| `generation` | int | monotone | Version. |
| `spec_hash` | str | dérivé | Changement. |

### 1.3 SlotSpec / SlotObservation / SlotStatus (consolidation)

Ces objets sont communs à NordVPN et WARP. Les champs spécifiques restent dans
`provider_details` ; le moteur ne connaît que le commun.

**SlotSpec**

| Champ | Type | Rôle |
|---|---|---|
| `provider` | `nordvpn\|warp\|proxies\|direct` | Famille. |
| `slot_id` | int | Identifiant stable. |
| `generation` | int | Version. |
| `enabled` | bool | Désir. |
| `stack` | str | `openvpn/wireguard/warp/socks5`. |
| `transport` | str | Transport effectif. |
| `endpoint` | str | `host:port` ou hostname. |
| `container` | str | Nom Docker. |
| `image` | str | Image/digest. |
| `family` | `ipv4\|ipv6` | Famille d'egress. |
| `metadata` | dict | Champs provider (pays, proto, options). |

**SlotObservation**

| Champ | Type | Rôle |
|---|---|---|
| `container_state` | str | running/exited/missing. |
| `health` | str | healthy/unhealthy/unknown. |
| `tunnel_up` | bool | Route/tunnel présent. |
| `dns_ok` | bool | Résolution. |
| `egress_ip` | str | IP observée. |
| `block` | str | `/64` ou `/32`. |
| `quota_state` | enum | unknown/ok/low/exhausted. |
| `reset_at` | float | Reset fournisseur. |
| `client_usable` | bool | Preuve récente. |
| `last_evidence` | str | Hash/bytes/timestamp. |
| `reason` | str | Cause. |

**SlotStatus** (schéma minimal commun)

```json
{
  "provider": "nordvpn",
  "slot_id": 1,
  "generation": 7,
  "state": "client_usable",
  "client_usable": true,
  "egress_ip": "87.120.102.85",
  "block": "87.120.102.85/32",
  "quota_state": "ok",
  "reset_at": null,
  "last_evidence": "sha256:...;bytes=1536",
  "reason": null,
  "next_action": "none",
  "provider_details": {
    "container": "opencode-vpn",
    "hostname": "al41.nordvpn.com",
    "stack": "wireguard",
    "transport": "wireguard"
  }
}
```

**Règle** : tout provider doit produire ce schéma ; `provider_details` est
libre mais ne remplace jamais les champs communs.

### 1.4 StationState

État observé, écrit par le réconciliateur et les probes.

| Champ | Type | Contraintes | Rôle |
|---|---|---|---|
| `provider` | str | requis | Famille. |
| `station` | int | PK composite | Slot. |
| `container` | str | requis | Nom réel. |
| `generation` | int | requis | Génération appliquée. |
| `state` | enum | voir §2 | État lifecycle. |
| `client_usable` | bool | défaut false | Preuve récente. |
| `current_ip` | str | optionnel | Egress observé. |
| `current_host` | str | optionnel | Host observé. |
| `block` | str | optionnel | `/64` ou `/32`. |
| `quota_state` | enum | `unknown/ok/low/exhausted` | Quota. |
| `reset_at` | float | optionnel | Reset fournisseur. |
| `last_probe_at` | float | optionnel | Dernière sonde. |
| `last_200_at` | float | optionnel | Dernier contenu. |
| `last_evidence` | str | optionnel | Preuve (hash/taille). |
| `reason` | str | optionnel | Cause. |
| `updated_at` | float | requis | Horodatage. |

### 1.5 EgressEndpoint

Projection opérationnelle d'un endpoint WARP/proxy.

| Champ | Type | Rôle |
|---|---|---|
| `id` | int | PK. |
| `provider` | str | `warp`/`proxies`. |
| `label` | str | Identité. |
| `container` | str | Nom Docker. |
| `host`/`port` | str/int | Adresse SOCKS. |
| `ipv6` | bool | Famille. |
| `managed` | bool | Recréable. |
| `enabled` | bool | Usage. |
| `egress_ip` | str | IP live observée. |
| `block` | str | Bucket. |
| `generation` | int | Version. |
| `last_seen_at` | float | Dernière observation. |
| `last_error` | str | Dernière erreur. |

### 1.6 EgressIP / QuotaBucket / Lease

Existants dans `registry.db` (`egress_ips`, `quota_buckets`, `leases`).

- `egress_ips` : `ip` PK, `provider`, `block_key`, `quota_axis`, `state`,
  `reset_at`, `last_ok_at`, `last_429_at`, `successes`, `failures`.
- `quota_buckets` : PK `(ip, model, day, bucket)`, `used`, `successes`,
  `failures`, `last_status`, `reset_at`, `source`, `provider`.
- `leases` : `lease_id` PK, `station`, `ip`, `hostname`, `transport`, `model`,
  `expires_at`, `released_at`, `reason`, `status`.

**Règles** :

- Un `EgressIP` actif ne peut être référencé que par un lease actif.
- `block_key` = `/64` pour IPv6, `/32` pour IPv4.
- Deux endpoints actifs ne peuvent pas avoir le même `block_key`.
- `reset_at` vient du fournisseur ; aucune valeur locale ne l'écrase.

### 1.7 RotationJob

| Champ | Type | Rôle |
|---|---|---|
| `id` | int | PK. |
| `provider` | str | Famille. |
| `station` | int | Slot. |
| `trigger` | enum | `429/quota/dead/manual/preventive`. |
| `old_ip`/`new_ip` | str | IP avant/après. |
| `old_block`/`new_block` | str | Bucket avant/après. |
| `attempts` | int | Tentatives. |
| `result` | enum | `ok/failed/skipped`. |
| `reason` | str | Cause. |
| `started_at`/`ended_at` | float | Durée. |

### 1.8 RepairJob

Existant (`repair_jobs`) ; ajouter `action`, `evidence`, `generation`.

| Champ | Type | Rôle |
|---|---|---|
| `station` | int | Slot. |
| `provider` | str | Famille. |
| `cause` | enum | `auth/server_dead/dns/transport/quota_429/healthcheck/proxy_down`. |
| `action` | enum | `restart/recreate/repin/rotate/park/wait`. |
| `attempts` | int | Tentatives. |
| `result` | enum | `ok/failed/parked`. |
| `evidence` | str | Preuve. |
| `started_at`/`finished_at` | float | Durée. |

### 1.9 Incident

Existant (`incidents`) : `scope`, `station`, `cause`, `remediation`, `details`,
`started_at`, `ended_at`. Utilisé pour les fenêtres 1 h/24 h/7 j/30 j.

## 2. Machine à états d'une station

```text
created
  │ start
  ▼
starting
  │ container running
  ▼
tunnel_up
  │ DNS ok
  ▼
dns_ok
  │ egress IP observé
  ▼
egress_ok
  │ quota non épuisé
  ▼
quota_ok
  │ probe client avec contenu
  ▼
client_usable ──► degraded (preuve ancienne / erreurs)
  │                    │
  │ park               │ recover
  ▼                    ▼
parked ◄──────────── repairing
  │                    │
  └──── resume ────────┘
```

**Transitions autorisées** :

| De | Vers | Condition |
|---|---|---|
| `created` | `starting` | desired enabled |
| `starting` | `tunnel_up` | conteneur running + route tunnel |
| `starting` | `failed` | timeout/erreur Docker |
| `tunnel_up` | `dns_ok` | résolution DNS |
| `dns_ok` | `egress_ok` | probe egress |
| `egress_ok` | `quota_ok` | pas de 429/quota |
| `quota_ok` | `client_usable` | probe client avec contenu |
| `client_usable` | `degraded` | preuve > TTL ou erreurs |
| `degraded` | `repairing` | cause classée |
| `repairing` | `client_usable` | probe ok |
| `repairing` | `parked` | budget/échecs |
| `parked` | `starting` | `reset_at`/park expiré |
| any | `failed` | erreur non récupérable |
| `failed` | `starting` | action opérateur/reconcile |

**Invariants** :

- Aucun saut direct de `created` à `client_usable`.
- `client_usable` exige `last_evidence` non vide et récent.
- `parked` a toujours `parked_until` et `cause`.
- `failed` est terminal jusqu'à action explicite.

## 3. Relations

```text
DesiredStationSpec 1 ─── 1 StationState
StationState       1 ─── N Lease
Lease              N ─── 1 EgressIP
EgressIP           1 ─── N QuotaBucket
StationState       1 ─── N RotationJob
StationState       1 ─── N RepairJob
DesiredEndpointSpec 1 ── 1 EgressEndpoint
EgressEndpoint     1 ── 1 EgressIP
```

## 4. Validation et contraintes SQL

```sql
-- desired state
UNIQUE(provider, station)
CHECK (generation >= 1)
CHECK (provider IN ('nordvpn','warp','proxies','direct'))

-- endpoints
UNIQUE(provider, label)
UNIQUE(container)
UNIQUE(port)
CHECK (host = '127.0.0.1' OR provider <> 'warp')

-- buckets
UNIQUE(provider, block_key) WHERE state = 'assigned'
CHECK (block_key IS NULL OR block_key LIKE '%/64' OR block_key LIKE '%/32')

-- leases
CHECK (status IN ('active','released','expired','abandoned'))
UNIQUE(ip) WHERE status = 'active'
```

## 5. Migration

1. Backup `VACUUM INTO logs/backups/registry-<ts>.db`.
2. `PRAGMA integrity_check`.
3. Ajouter `desired_state`, `rotation_jobs`, colonnes `provider`/`generation`.
4. Backfill depuis `config.yaml`, `.env`, `socks5_proxies`, `station_count`.
5. Créer les `DesiredStationSpec` manquants (6 NordVPN, 6 WARP).
6. Marquer les endpoints orphelins `disabled` (pas de suppression).
7. Valider : 12 desired, 12 state, 0 doublon de bucket.
8. Rollback : restaurer le backup si une contrainte échoue.

## 6. Rétention

- `observations` : 30 jours.
- `incidents` : 180 jours.
- `rotation_jobs`/`repair_jobs` : 90 jours.
- `leases` : 30 jours après `released_at`.
- `quota_buckets` : 7 jours après `reset_at`.
- Backup quotidien, vérification `integrity_check`, purge WAL.
