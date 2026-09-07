"""
vpn — boundaries unifiées.

Regroupe les modules dispersés:
- vpn/manager.py (domicile canonique Phase 6 — ex-vpn_manager.py) :
  VPNManager, stations 1..10, docker compose, control server
- free/pool.py (domicile canonique Phase 6 — ex-free_ip_pool.py) :
  FreeIPPool, per-IP cooldown, 429 handling, station usable
- free/rotation.py (domicile canonique Phase 6 — ex-shared+latency) :
  SharedRotationState (shared_rotation.json recent-IP cursors)
- shared_state.py — registre cross-module des managers (évite import cycle)

[Phase 6 refonte — chantier 3] Façade PARESSEUSE (PEP 562) : AUCUN import
au chargement du package. Les importations eager précédentes
(``from vpn_manager import …`` au top-level) créaient un cycle fatal avec
le shim ``vpn_manager.py`` (``config/settings.py`` → ``vpn_manager`` →
``vpn`` → ``free`` → ``vpn_manager`` partiel → ImportError). L'accès
``vpn.VPNManager`` résout vers le canonique À L'USAGE (mêmes objets).
"""

__all__ = ["SharedRotationState", "VPNManager", "FreeIPPool"]


def __getattr__(name: str):
    if name == "VPNManager":
        from vpn.manager import VPNManager as _VM

        return _VM
    if name == "FreeIPPool":
        from free.pool import FreeIPPool as _FP

        return _FP
    if name == "SharedRotationState":
        from free.rotation import SharedRotationState as _SR

        return _SR
    raise AttributeError(f"module 'vpn' has no attribute {name!r}")
