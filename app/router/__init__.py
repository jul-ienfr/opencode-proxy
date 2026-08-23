"""
app.router — routing _route_for + get_model_config

Extraction de opencode.py: routing table + SORTED_ROUTES + get_model_config @lru_cache 512
DI via app.state (ou config.settings direct pour compat).
"""

from config import get_model_config as get_model_config
from config.settings import SORTED_ROUTES as SORTED_ROUTES
from config.settings import SORTED_CUSTOM_ROUTES as SORTED_CUSTOM_ROUTES

# _route_for reste dans opencode.py pour l'instant (import circulaire évité)
# Prochaine PR: déplacer _route_for ici et faire opencode.py re-export:
#   from app.router import _route_for as _route_for


def _route_for_stub(model: str):
    """Placeholder — l'impl réelle vit encore dans opencode.py:_route_for."""
    from opencode import _route_for as _orig

    return _orig(model)


__all__ = ["get_model_config", "SORTED_ROUTES", "SORTED_CUSTOM_ROUTES", "_route_for_stub"]
