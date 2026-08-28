"""
vpn — boundaries unifiées (Phase3 P3.2)

Regroupe les modules dispersés:
- vpn_manager.py (3903l) — VPNManager, station 1..10, docker compose, control server
- free_ip_pool.py (1410l) — FreeIPPool, per-IP cooldown, 429 handling, station usable
- shared_rotation.py (378l) — SharedRotationState (shared_rotation.json recent-IP cursors)
- shared_state.py — registre cross-module des managers (évite import cycle)
- traffic_capture.py (493l) — ring 500/32MiB pure-ASGI
- server_scorer.py — scoring NordVPN
- nordvpn_api.py — API NordVPN
- docker_events.py — docker events wake-up watchdog

Extraction progressive: chaque module garde son fichier à la racine pour compat
`import vpn_manager` / `import free_ip_pool` etc, mais `vpn` est le package
canonique pour les nouveaux imports (`from vpn import VPNManager`).

P3.2 final: déplacer les fichiers dans vpn/ et faire les racines re-exporter:
  vpn/manager.py, vpn/pool.py, vpn/shared.py, vpn/capture.py, etc.
"""

from shared_rotation import SharedRotationState as SharedRotationState

try:
    from vpn_manager import VPNManager as VPNManager
except ImportError:
    VPNManager = None  # type: ignore

try:
    from free_ip_pool import FreeIPPool as FreeIPPool
except ImportError:
    FreeIPPool = None  # type: ignore

__all__ = ["SharedRotationState", "VPNManager", "FreeIPPool"]
