# Runbook — capacité egress et six stations

**Feature**: 010-egress-station-sizing
**État de référence**: 2026-11-07, WARP-only, 6/6 OK.

## 1. Objectif

Six stations client-utilisables, avec bascule automatique sur un egress qui
répond `200`. Le mode et les poids ne doivent jamais être confondus avec la
capacité.

| Notion | Rôle |
|---|---|
| `egress_strategy` | Fallback/Priorité ou Répartition |
| `egress_weights` | Part de trafic (Répartition) |
| `egress_capacity.target_slots` | Nombre de stations visées (défaut 6) |
| `egress_capacity.reserve_policy` | `full`, `shared` ou `proportional` |
| `station_count` | Nombre de stations NordVPN |
| `socks5_proxies` | Endpoints WARP/proxys |

## 2. Lire la capacité

```bash
curl -s http://127.0.0.1:4000/api/egress/capacity | jq
```

Champs utiles :

- `verdict` : `ok`, `degraded`, `insufficient` ;
- `total_usable` / `total_required` ;
- `providers.<name>.{configured,enabled,usable,required,observed_share}` ;
- `warnings` : `single_provider`, `full_degraded`, `no_reserve`,
  `endpoint_imbalance`, `endpoint_hotspot`, `observed_shift`,
  `direct_single_ip`, etc.

### Signification

| Verdict | Sens |
|---|---|
| `ok` | La cible est couverte avec la politique choisie |
| `degraded` | Couverte maintenant, mais une panne ferait passer sous la cible |
| `insufficient` | Moins de stations utilisables que la cible |

## 3. Choisir le dimensionnement

### Fallback (priorité)

- Le premier provider coché sert.
- Les suivants ne servent qu'en cas d'échec/quota.
- `reserve_policy: full` → 6 par provider sélectionné.
- 6 NordVPN + 6 WARP = 12 endpoints, 6 de réserve.
- 6 + 3 = 6 maintenant, 3 après bascule → `degraded`.

### Répartition (charge)

- Les poids sont une part de **trafic**, pas un nombre de stations.
- 6 + 6 avec 50/50 → 12 endpoints, ~8,3 % par endpoint.
- 3 + 3 avec 50/50 → 6 endpoints, ~16,7 % par endpoint, aucune réserve.
- 4 + 2 avec 67/33 → charge/endpoint égalisée.
- `proportional` choisit l'allocation qui minimise l'écart de charge.

### Direct

Toujours **1 endpoint**. Un poids élevé sur Direct concentre le quota sur
l'IP de la machine ; le GUI émet `direct_single_ip`.

## 4. Appliquer depuis le GUI

`Modèles Gratuits > Egress & providers > Capacité`

- **Cible** : 1–10.
- **Politique** : Redondance totale / Capacité partagée / Proportionnelle.
- **Appliquer** : persiste `target_slots` et `reserve_policy` uniquement.
- **Appliquer {n} stations NordVPN** : visible seulement si NordVPN est coché
  et si `station_count` diffère de la cible ; confirmation obligatoire.
- **Endpoints (n)** : cases à cocher WARP/proxys ; aucune création/suppression
  de conteneur.

## 5. Audit

Chaque action est journalisée dans `logs/egress_audit.jsonl` :

```bash
tail -n 20 logs/egress_audit.jsonl | jq
```

Actions : `capacity_config`, `egress_mode`, `station_count`,
`socks5_toggle/add/remove`. Aucun username/password n'est journalisé.

## 6. Incidents connus

### `single_provider`

WARP seul couvre 6/6, mais il n'y a pas de seconde jambe. C'est attendu et
non bloquant. Pour le supprimer, sélectionner un second provider avec sa propre
capacité (6 + 6).

### NordVPN `429` / fingerprint

Les stations NordVPN peuvent recevoir `429` avec un `Retry-After` long et le
même fingerprint sur plusieurs IP. Ce n'est pas un problème de transport ni de
capacité : le quota free amont est épuisé pour ce fingerprint. Ne pas lancer
six chaînes en boucle ; laisser WARP servir les six stations et réessayer plus
tard avec un quota/fingerprint frais.

### `wireguard lease busy`

Le projet utilise une clé NordLynx partagée : un seul endpoint WireGuard à la
fois. La config doit répartir les transports :

```yaml
ip_registry:
  allocator:
    auth_fallback_transport: ''
    transports:
      - {station: 1, transport: openvpn-udp}
      - {station: 2, transport: openvpn-udp}
      - {station: 3, transport: openvpn-tcp}
      - {station: 4, transport: openvpn-udp}
      - {station: 5, transport: wireguard}
      - {station: 6, transport: openvpn-udp}
```

### Endpoints WARP classés « proxies »

Le toggle GUI doit préserver `label`, `managed`, `provider`, `container`,
`block`, `egress_ip`. Si un ancien build a strippé ces champs, restaurer
`config.yaml` depuis le commit `12469ee` ou réappliquer les métadonnées avant
redémarrage.

### Superviseur NordVPN qui tourne en WARP-only

Le superviseur doit être idle quand `egress_providers` ne contient pas
`nordvpn`. Vérifier :

```bash
tail -n 5 logs/fresh_chain_supervisor.log
# NordVPN non sélectionné — supervisor idle
```

## 7. Retour à l'état stable WARP-only

```bash
curl -s -X POST http://127.0.0.1:4000/api/vpn/proxy-mode \
  -H 'Content-Type: application/json' \
  -d '{"providers":["warp"],"strategy":"fallback","weights":{"warp":100}}'
```

Attendre `6/6` et `verdict=ok`. Le warning `single_provider` reste normal.

## 8. Vérifications

```bash
python -m pytest -k "not docker" -q
python -m ruff check free/capacity.py free/pool.py dashboard/api.py
npm --prefix dashboard-ui run check
npm --prefix dashboard-ui run build
```

Probes egress réels :

```bash
curl -s -X POST http://127.0.0.1:4000/api/vpn/socks5/test \
  -H 'Content-Type: application/json' \
  -d '{"host":"127.0.0.1","port":11080}'
```

Attendu : `{"ok": true, "ip": "...", "latency_ms": ..., "error": null}`.
