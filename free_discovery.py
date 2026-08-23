"""
free_discovery — centralisation F-L4 (audit Phase2)

Single source pour free_model_map + free_discovery cascade
(pricing/is_free/capabilities.free/-free + HTML filet + default_target mimo-v2.5-free)
dispersés auparavant dans opencode.py + config/settings.py:416,1142.

Ce module est importé par config/settings.py (re-export) et opencode.py.
La logique reste dans config.settings pour hot-reload, ce fichier est le facade
testable (discover_free_models) et le point d'entrée unique.
"""

from config.settings import (
    FREE_DISCOVERY_AUTO_PERSIST as FREE_DISCOVERY_AUTO_PERSIST,
)
from config.settings import (
    FREE_DISCOVERY_DEFAULT_TARGET as FREE_DISCOVERY_DEFAULT_TARGET,
)
from config.settings import (
    FREE_DISCOVERY_ENABLED as FREE_DISCOVERY_ENABLED,
)
from config.settings import (
    FREE_DISCOVERY_INTERVAL as FREE_DISCOVERY_INTERVAL,
)
from config.settings import (
    FREE_MODEL_MAP as FREE_MODEL_MAP,
)
from config.settings import (
    FREE_MODELS as FREE_MODELS,
)


def discover_free_models():
    """Facade testable — déclenche la discovery synchrone (config.settings)."""
    try:
        from config.settings import _ensure_free_models_sync

        return _ensure_free_models_sync()
    except Exception:
        return []


__all__ = [
    "FREE_MODEL_MAP",
    "FREE_MODELS",
    "FREE_DISCOVERY_ENABLED",
    "FREE_DISCOVERY_INTERVAL",
    "FREE_DISCOVERY_AUTO_PERSIST",
    "FREE_DISCOVERY_DEFAULT_TARGET",
    "discover_free_models",
]
