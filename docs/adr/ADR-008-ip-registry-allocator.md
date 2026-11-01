# ADR-008 — Registre d’IP et allocateur de stations

- Statut : proposé (Phase 0)
- Date : 2026-11-01
- Portée : jambe free, sélection des IP, binding serveur, allocation

## Contexte

La jambe free est limitée par IP. Les IP NordVPN sont majoritairement des IPs
datacenter partagées, souvent déjà épuisées. Un `200` est rare et instable :
`.57` et `.13` ont été perdus parce que gluetun a reconnecté la station sur un
autre serveur. Le système actuel sait tester et marquer les 429, mais il ne
sait pas :

- classer les IP de façon persistante ;
- réassigner une IP précise à une station ;
- conserver une bonne IP tant qu’elle fonctionne.

## Décision

Introduire un **registre d’IP** et un **allocateur** :

1. Catalogue NordVPN (serveurs, pays, charge).
2. Registre SQLite (`servers`, `observations`, `ip_state`, `assignments`).
3. Score 0–100 + éligibilité (reset, cooldown, géo, claim, assignation).
4. Allocateur à la demande : uniquement les stations qui ont besoin d’une IP.
5. Binder serveur : reconnecter une station à un **hostname précis**, pas à un
   pays.
6. Conservation d’une IP `200` jusqu’à échec, rotation ou cycle.

## Invariants

- Une IP = une station assignée.
- Aucun test d’une IP avant `reset_at`.
- Aucun test si toutes les stations sont saines.
- Un `200` fige la station.
- Toute décision est persistée avant l’action (lease) et après le résultat.
- Un 429 en cours d’utilisation est enregistré (IP, date, Retry-After, reset)
  et libère l’assignation (`reason=runtime_429`).

## Binding serveur — décision après spike

Trois adaptateurs possibles :

1. `SERVER_HOSTNAMES_STATIONn` + recréation Compose.
2. `PUT /v1/vpn/settings` avec `countries` + `hostnames`.
3. Config WireGuard custom par serveur.

**Spike du 2026-11-01** (station 1, `al110.nordvpn.com`) :

- `SERVER_HOSTNAMES` + recréation Compose, deux fois → **même egress**
  `186.247.44.61`.
- `PUT /v1/vpn/settings` avec `countries` + `hostnames` fonctionne pour la
  première connexion, mais un second `PUT` identique retourne
  `settings left unchanged` (pas de reconnexion).
- `PUT /v1/vpn/status` stop/start retourne `rc=8` et bloque la station en
  `stopping` ; la station a dû être recréée.

Décision :

1. **Binder principal = Compose hostname** (`SERVER_HOSTNAMES_STATIONn` +
   recréation + vérification de l’egress).
2. **Control API = fast-path uniquement quand le hostname change**, jamais pour
   une reconnexion à l’identique.
3. **Custom WireGuard** = option de secours si un provider ne supporte pas
   `SERVER_HOSTNAMES`.

Le spike est documenté dans
`docs/spikes/2026-11-01-server-binding.md`.

## Conséquences

### Positives

- IP classées et réutilisables.
- Rotation uniquement en cas de besoin réel.
- Historique complet des 429 et des assignations.
- Architecture testable, hexagonale, sans couplage au provider.

### Négatives / risques

- Complexité supplémentaire (SQLite, leases, binder).
- Dépendance à la capacité de gluetun à pin un hostname.
- Le quota reste externe : le système ne peut pas créer d’IP fraîche.

## Mise en œuvre

- Phase 0 : domaine, scoring, éligibilité, schéma SQLite, flags.
- Phase 1 : catalogue + registre en `observe`.
- Phase 2 : spike binder, go/no-go.
- Phase 3 : allocateur canary (station 6).
- Phase 4 : flotte + dashboard.
- Phase 5 : présélection/cycle.

## Références

- `docs/plan-ip-registry-allocator.md`
- `free/ip_registry/`
- `free/quota_ledger.py`
- `scripts/nordvpn_scan.py`
