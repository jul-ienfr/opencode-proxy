# Evidence — 008 Fallback vs Répartition

**Date**: 2026-11-07
**Feature**: `specs/008-auto-egress-mode/`
**Scope**: strategy UI, weights, persistence, runtime SWRR.

## Build

```text
npm --prefix dashboard-ui run check  -> 0 errors, 0 warnings
npm --prefix dashboard-ui run build  -> OK
python -m pytest tests/test_auto_egress_config.py tests/test_vpn_endpoints.py
  -> 59 passed
ruff check free/auto_egress.py free/pool.py dashboard/api.py tests/... -> OK
```

## API

`POST /api/vpn/proxy-mode` accepts `providers`, `strategy`, `weights`:

```json
{
  "providers": ["nordvpn", "warp", "proxies"],
  "strategy": "balance",
  "weights": {"nordvpn": 2, "warp": 1, "proxies": 1}
}
```

Persisted result:

```text
egress_strategy = "balance"
egress_weights  = {"nordvpn": 50, "warp": 25, "proxies": 25}
```

`/api/vpn-status` and `/api/vpn-config` expose both fields. A restart
restores the strategy and weights.

## GUI — browser checks

Tested on `http://127.0.0.1:4000/?v=balance2#free-models/egress`.

### Segmented control

- `Fallback / Priorité` active by default.
- Click `Répartition / Charge` → sliders appear, total `100 %`.
- Switching back hides sliders and shows ranks `1`, `2`, …

### One provider

```text
NordVPN  [============ 100% ] 100%   ↑ ↓
Total : 100 %
```

### Two providers

- Check WARP while NordVPN is at 100 % → `50 / 50`.
- Move WARP slider to 70 → `NordVPN 30 %`, `WARP 70 %`, total `100 %`.
- Stacked bar widths: `30 %` + `70 %`.
- `Tout équilibrer` → `50 / 50`.

### Fallback order

- Switch to `Fallback / Priorité` → no sliders.
- Ranks match the configured order (`1 NordVPN`, `2 WARP`, …).
- ↑/↓ moves a provider and persists the new order.

## Backend selection

- Fallback: `_select_egress_provider(["nordvpn","warp"])` → `nordvpn`;
  `["warp"]` → `warp`; `[]` → `None`.
- Balance 50/30/20 over 100 picks → exactly 50/30/20.
- WARP down → `50/20` renormalized to `71/29`, no division by zero.
- Weight 0 → never picked while a positive-weight provider is available;
  used only when it is the last one.
- 4 providers (`nordvpn`, `warp`, `proxies`, `direct`) → 25/25/25/25.
- 3 providers equalize → 34/33/33 (largest remainder).
- Direct is combinable with other providers since T069; the GUI warns that it
  uses the machine IP without a tunnel.

## Notes

- A provider removed/down is excluded from the candidate list and the
  remaining weights are renormalized for that decision.
- `egress_weights` is kept while in Fallback so switching back to Répartition
  restores the last split.
