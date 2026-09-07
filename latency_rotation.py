"""latency_rotation — SHIM Phase 6 refonte (compatibilité, ne pas étendre).

Domicile canonique : ``free.rotation`` (fusion byte-identique avec
``shared_rotation.py``). MÊMES objets pour les consommateurs historiques :
``tests/test_latency_rotation.py``, ``tests/test_features_121.py``,
``tests/test_lot7_ops.py``, ``station_supervisor.py``,
``dashboard/api.py`` (façade gelée ADR-006 §5).

Note : le singleton ``_ENGINE`` (rebind via ``global`` dans
``get_engine()``) vit dans le canonique — on y accède UNIQUEMENT via
``get_engine()`` (aucun lecteur direct en repo).
"""

from free.rotation import COOLDOWN_HARD as COOLDOWN_HARD
from free.rotation import COOLDOWN_SOFT as COOLDOWN_SOFT
from free.rotation import EngineConfig as EngineConfig
from free.rotation import LatencyRotationEngine as LatencyRotationEngine
from free.rotation import _lat_logger as logger
from free.rotation import get_engine as get_engine

__all__ = [
    "COOLDOWN_HARD",
    "COOLDOWN_SOFT",
    "EngineConfig",
    "LatencyRotationEngine",
    "get_engine",
    "logger",
]
