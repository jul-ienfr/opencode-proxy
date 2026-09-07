"""station_supervisor — SHIM Phase 8 refonte (compatibilité, ne pas étendre).

Domicile canonique : ``ops.supervisor`` (déplacement pur, contenu
identique). MÊMES objets (zéro ``global`` dans le module : état en
instances, constantes jamais rebindées) pour les consommateurs
historiques : ``tests/test_plan30_optimisation.py``,
``tests/test_station_isolation.py``, ``opencode.py`` (lazy, chemin
historique inchangé — règle §14) (façade gelée ADR-006 §5).

Suppression prévue Phase 9 après preuve de non-usage (``grep``).
"""

from ops.supervisor import WARMUP_EXCLUDED_REQUESTS as WARMUP_EXCLUDED_REQUESTS
from ops.supervisor import StationSupervisor as StationSupervisor
from ops.supervisor import build_supervisors as build_supervisors
from ops.supervisor import logger as logger
from ops.supervisor import sync_supervisors as sync_supervisors
from ops.supervisor import warmup_excluded_requests as warmup_excluded_requests

__all__ = [
    "StationSupervisor",
    "WARMUP_EXCLUDED_REQUESTS",
    "build_supervisors",
    "logger",
    "sync_supervisors",
    "warmup_excluded_requests",
]
