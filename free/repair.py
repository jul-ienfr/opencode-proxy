"""Shim de compatibilité — domicile canonique : ``station_repair``.

Le module a été déplacé hors du package ``free`` pour éviter le cycle
``vpn.manager -> free.repair -> free/__init__ -> free.pool -> vpn.manager``.
Le nouveau code importe ``station_repair`` ; ce shim reste pour les tests et
les imports historiques.
"""

from __future__ import annotations

from station_repair import *  # noqa: F401,F403
from station_repair import __all__ as __all__  # noqa: F401
