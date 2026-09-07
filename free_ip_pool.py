"""free_ip_pool — SHIM Phase 6 refonte (compatibilité, ne pas étendre).

Domicile canonique : ``free.pool`` (déplacement pur, contenu identique).
Ce module re-exporte toute la surface historique (``FreeIPPool``,
``Socks5Endpoint``, ``_clamp_seconds``, ``logger`` — MÊMES objets ; ce
module ne contient aucun ``global`` : tout l'état vit dans les instances,
donc aucune divergence possible) pour les consommateurs historiques :
``tests/test_free_*``, ``tests/test_geo_routing.py``…,
``tests/test_rotation_concurrency.py``…, ``scripts/smoke_todo10.py``
(façade gelée ADR-006 §5).

Suppression prévue Phase 9 après preuve de non-usage (``grep``).
"""

from free.pool import FreeIPPool as FreeIPPool
from free.pool import Socks5Endpoint as Socks5Endpoint
from free.pool import _clamp_seconds as _clamp_seconds
from free.pool import logger as logger

__all__ = [
    "FreeIPPool",
    "Socks5Endpoint",
    "_clamp_seconds",
    "logger",
]
