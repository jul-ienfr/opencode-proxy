# Contrats providers — 023-client-usable-egress

## 1. Interface commune

```python
class EgressProvider(Protocol):
    name: str                  # nordvpn | warp | proxies | direct
    can_rotate: bool
    bucket_key: Callable[[Endpoint], str]
    wire_profile: str          # version de capture exigée
    quota_source: str          # retry_after | shared | none
    health_source: str

    async def health(self, endpoint: Endpoint) -> Health: ...
    async def rotate(self, endpoint: Endpoint, reason: str) -> Rotation: ...
    async def read_ip(self, endpoint: Endpoint) -> IpObservation: ...
```

Invariants communs :

1. Le wire est identique sur les quatre chemins (`wire_fp`, JA3/JA4, ordre des
   en-têtes). Le CONNECT d'un proxy ne modifie pas le ClientHello vers
   l'origine.
2. Aucune rotation ne recrée un conteneur : l'API de contrôle Gluetun est la
   seule voie (`PUT /v1/vpn/settings`).
3. Un provider désactivé n'est plus sélectionné en < 60 s et n'est plus sondé
   après `drain_s`.
4. Toute rotation et libération journalise `reason` + `evidence_ref`.

## 2. NordVPN

| Élément | Contrat |
|---|---|
| Endpoints | 6 stations (`station_count = 6`, borne dure) |
| Transports | WireGuard, OpenVPN UDP, OpenVPN TCP, cycle par station |
| Bucket | IP `/32` |
| Rotation | control API ; `StartedAt` du conteneur inchangé |
| Santé | handshake + sonde officielle |
| Quota | `Retry-After` fournisseur |
| Réserve | oui, si preuve du même `wire_fp` |
| Interdits | recréation de conteneur, rotation non bornée |

Tests d'acceptation :

- rotation → IP change, `StartedAt` identique, `docker inspect` inchangé ;
- 429 quota → `reset_at` posé, IP exclue jusqu'au reset ;
- 429 format sur IP non prouvée → `burned`, ≥ 24 h.

## 3. WARP

| Élément | Contrat |
|---|---|
| Endpoints | conteneurs `warp-*`, SOCKS `:11080` (IPv4), `:11085` (IPv6) |
| Bucket | `/64` IPv6 ; IPv4 NAT partagée = capacité 0 |
| Rotation | reconnect/enregistrement borné ; aucune recréation non bornée |
| Santé | sonde egress par endpoint |
| Quota | `Retry-After`/partagé |
| Réserve | oui pour un `/64` distinct, jamais pour IPv4 |

Tests d'acceptation :

- deux endpoints IPv4 du même /16 ne doublent pas la capacité ;
- deux `/64` distincts comptent deux buckets ;
- IPv4 épuisé → bascule `/64` ou autre provider, jamais de réserve fantôme.

## 4. Proxys

| Élément | Contrat |
|---|---|
| Sources | liste configurable, refresh périodique |
| Qualification | liveness, anonymat, latence, denylist |
| Bucket | `host:port` (capacité 1 par endpoint qualifié) |
| Rotation | refresh de source, pas de rotation d'IP |
| Santé | liveness |
| Quota | partagé |
| Réserve | jamais |

Tests d'acceptation :

- un proxy mort sort en < 60 s et ne sert plus ;
- un proxy partagé épuisé est `spent` avec `reset_at` exact ;
- `max_active` et l'isolation sont respectés.

## 5. Direct

| Élément | Contrat |
|---|---|
| Endpoint | egress de l'hôte |
| Bucket | IP hôte (capacité 1) |
| Rotation | impossible |
| Santé | egress hôte |
| Quota | partagé |
| Réserve | jamais |
| Garde | `enforce_vpn_only` refuse le direct |

Tests d'acceptation :

- un 200 client est reproductible quand le bucket partagé a du quota ;
- un 429 est relayé avec `reset_at` exact, jamais un 503 déguisé ;
- activer/désactiver le direct ne change pas le `wire_fp` observé.

## 6. Matrice de capacités

| Capacité | NordVPN | WARP | Proxys | Direct |
|---|---|---|---|---|
| `can_rotate` | oui | oui (borné) | oui (source) | non |
| bucket | `/32` | `/64` (IPv4 = 0) | `host:port` | IP hôte |
| réserve client | oui | oui (`/64`) | non | non |
| quota honnête (`Retry-After`) | oui | oui | partagé | partagé |
| wire identique | oui | oui | oui | oui |
