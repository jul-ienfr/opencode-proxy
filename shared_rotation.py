"""shared_rotation — SHIM Phase 6 refonte (compatibilité, ne pas étendre).

Domicile canonique : ``free.rotation`` (fusion byte-identique avec
``latency_rotation.py`` — seuls ajustements : en-tête, ``ROOT`` ré-ancré
racine repo, loggers nommés explicitement). MÊMES objets (état en
instances, ``ROOT``/constantes à valeur identique) pour les consommateurs
historiques : ``tests/test_shared_rotation.py``,
``tests/test_vpn_freshness.py``, ``tests/test_rotation_n_stations.py``,
``vpn/__init__.py`` (façade gelée ADR-006 §5).

Suppression prévue Phase 9 après preuve de non-usage (``grep``).
"""

from free.rotation import ROOT as ROOT
from free.rotation import SharedRotationState as SharedRotationState
from free.rotation import _fresh as _fresh
from free.rotation import _now_utc as _now_utc
from free.rotation import _parse_utc as _parse_utc
from free.rotation import logger as logger

__all__ = [
    "ROOT",
    "SharedRotationState",
    "_fresh",
    "_now_utc",
    "_parse_utc",
    "logger",
]
