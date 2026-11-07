# Contract — EgressProvider

**But**: interface commune à WARP, aux clients WARP alternatifs, aux providers
gluetun et au gateway IPv6 VPS.

## Protocol

```python
class EgressProvider(Protocol):
    name: str

    def rotate(
        self,
        endpoint: EgressEndpoint,
        *,
        forbidden_blocks: set[str],
        probe: Callable[[EgressEndpoint], bool | None] | None = None,
    ) -> EgressRotationResult: ...

    def refresh_egress(self, endpoint: EgressEndpoint) -> str | None: ...

    def health(self) -> ProviderHealth: ...
```

## EgressRotationResult

```python
@dataclass(frozen=True)
class EgressRotationResult:
    label: str
    ok: bool
    old_ip: str | None
    new_ip: str | None
    block: str | None
    attempts: int
    reason: str
    provider: str
```

`reason` ∈ `rotated`, `same_block`, `duplicate_block`, `quota_exhausted`,
`egress_unresolved`, `health_timeout`, `docker_error`, `gateway_error`,
`disabled`.

## Invariants

1. `rotate()` ne retourne `ok=True` que si :
   - `new_ip` est une IP valide ;
   - la famille correspond à `endpoint.ipv6` ;
   - `block` est dérivé de `new_ip` (`/64` ou `/32`) ;
   - le bloc n'est ni dans `forbidden_blocks`, ni déjà affecté à un autre
     endpoint ;
   - le probe quota (si fourni) ne retourne pas `False`.
2. `refresh_egress()` mesure l'egress réel ; il ne fait pas confiance au
   rapport du conteneur/gateway.
3. Toute rotation qui a changé l'IP réelle met à jour `endpoint.egress_ip`,
   `endpoint.block` et `endpoint.current_ip`, même si le bloc est rejeté.
4. Un échec de rotation ne laisse jamais `current_ip` sur une IP qui n'est
   plus celle du conteneur/gateway.
5. `health()` ne lève jamais ; il retourne un état `unknown` en cas d'erreur.

## Implémentations

| Provider | Classe | Transport |
|---|---|---|
| WARP Docker | `WarpDockerProvider` | `docker restart/recreate` + SOCKS5 |
| WARP alternatif | `WarpAltProvider` | HTTP/SOCKS5 (MicroWARP/usque) |
| Gluetun | `GluetunProvider` | contrôle Docker/Gluetun |
| IPv6 VPS | `Ipv6GatewayProvider` | API HTTP locale + SOCKS5 |
| Statique | `StaticProvider` | pas de rotation, health seulement |

## Tests obligatoires

- `rotate` avec un bloc dupliqué → `duplicate_block`, pas d'application.
- `rotate` avec un probe `False` → `quota_exhausted`, IP trackée.
- `refresh_egress` avec une famille incorrecte → rejet.
- `health` avec provider down → `unknown`, pas d'exception.
- compatibilité : config sans `provider` → `WarpDockerProvider` historique.
