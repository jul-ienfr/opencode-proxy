# Evidence — 009 dashboard IA reorg

**Date**: 2026-11-07
**Feature**: `specs/009-dashboard-ia-reorg/`
**Scope**: UI-only; no API/config/runtime change.

## Build

```text
npm --prefix dashboard-ui run check  -> 0 errors, 0 warnings
npm --prefix dashboard-ui run build  -> OK (674 ms)
```

## Browser checks

Tested on `http://127.0.0.1:4000` with the rebuilt `static/dist`.

### Modèles Gratuits

- 5 sub-tabs visible: Vue d’ensemble, Egress & providers, Routage free,
  Qualité & probes, Avancé.
- `#free-models/overview` → inventory visible.
- `#free-models/egress` → VpnStatus + VpnServers + Allocator.
- `#free-models/routing` → VpnRouting.
- `#free-models/quality` → quality/probes only.
- `#free-models/advanced` → advanced/WARP/HTTPS panels.

### Configuration

- 5 sub-tabs visible: Serveur, Modèles & routage, Clés & accès, Outils web,
  Maintenance.
- `#config/server` → server card.
- `#config/models` → mapping + custom routes + models.
- `#config/keys` → API keys.
- `#config/web` → web search/fetch.
- `#config/maintenance` → links to logs/debug.

### Aliases

| Input | Result |
|---|---|
| `#config` | `#config` → server sub-tab |
| `#free-models` | overview |
| `#vpn` | free-models/egress |
| `#config/nope` | normalized to `#config/server` |

### Dirty guard

- Modify server port → switch to Models with `confirm=false` → stays on
  Server.
- Same with `confirm=true` → switches to Models.

### i18n

- EN: Server, Models & routing, Keys & access, Web tools, Maintenance.
- FR: Serveur, Modèles & routage, Clés & accès, Outils web, Maintenance.

### Search

- `warp` → result `egress`, jump + hint « Section : Egress & providers ».
- `web search` → result `web`, jump + hint « Section : Outils web ».
- The result list is keyboard-navigable (ArrowUp/Down, Enter, Escape).

### Keyboard / no-remount

- Focus the active tab, ArrowRight → next sub-tab active and focused.
- Sub-tab changes no longer remount the page (`App.svelte` only reloads when
  the main tab changes), so drafts and the search hint survive.
- Dirty draft test: port `4000 → 4001`, switch to Models with confirm=false
  stays on Server; with confirm=true goes to Models; returning to Server
  keeps the draft `4001` (not saved).

### Navigation

- Browser back/forward keeps the correct sub-tab.
- Console errors: 0.

## Known gaps (debt)

- Sub-tab components are not lazy-loaded individually; pages are lazy at the
  top level (T007/T041).
- Main-tab navigation (Stats/Config/…) while a form is dirty is not guarded;
  only sub-tab navigation is.
- FreeModels/Config sections are conditionally rendered in the existing page
  files, not extracted into 10 new components (to avoid duplicating shared
  state/handlers).
- 375 px visual capture and first-render measurement still to do (T039/T041).
