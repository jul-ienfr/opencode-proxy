# Contract — Option Ownership

**But**: garantir qu'un réglage n'apparaît que dans **un** sous-onglet et
qu'il garde un consommateur runtime.

## Matrice

| Réglage / carte | Sous-onglet | Composant | Consommateur | Test |
|---|---|---|---|---|
| Inventaire free, counts, policy | Free / Overview | `free/Overview.svelte` | `/api/free-models` | UI |
| Mode/checkboxes providers, VpnStatus, allocator | Free / Egress | `free/Egress.svelte` | `_free_proxy_mode`, pool | UI + API |
| Free attempts, strict/fallback, routage | Free / Routing | `free/Routing.svelte` | `free_attempts`, `strict_free` | UI + API |
| Quality/latency/timeouts/post-rotation | Free / Quality | `free/Quality.svelte` | `quality_rotation`, `latency_rotation` | UI |
| WARP-only, HTTPS, diagnostics | Free / Advanced | `free/Advanced.svelte` | config WARP/HTTPS | UI |
| Serveur, port, bind, restart, debug | Config / Server | `config/Server.svelte` | `/api/config`, restart | UI + API |
| Mapping, routes custom, catalogue | Config / Models | `config/Models.svelte` | `MODELS`, `/api/config` | UI + API |
| API keys, token | Config / Keys | `config/Keys.svelte` | `API_KEYS` | UI + API |
| Web search/fetch | Config / Web | `config/Web.svelte` | `/api/config/web-*` | UI + API |
| Updates, logs, backups, debug | Config / Maintenance | `config/Maintenance.svelte` | maintenance API | UI |

## Règles

1. Une clé de config ne peut apparaître que dans **une** ligne de la matrice.
2. Un réglage `display_only=true` est marqué comme tel dans l'UI.
3. La matrice est mise à jour à chaque déplacement de carte.
4. Un test de non-régression vérifie qu'aucune clé n'est dupliquée.
5. Un réglage sans consommateur est supprimé ou marqué « affichage seul ».
