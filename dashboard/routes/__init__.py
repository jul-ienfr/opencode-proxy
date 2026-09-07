"""dashboard.routes — groupes de routes du dashboard (Phase 8 refonte, amorce).

Découpage ``dashboard/api.py`` SANS changer les URLs : chaque groupe vit
dans son module et ``register_dashboard`` (inchangé, ``dashboard/api.py``)
les monte. Les routes elles-mêmes sont des closures capturant l'état du
registre — leur extraction viendra avec la DI ``app.state`` (Phase 9).
Cette PR n'extrait que la couche 100 % découplée :

* ``dashboard.routes.static`` — pré-compression gzip + middleware
  Cache-Control (stdlib + starlette seuls, args injectés).
"""

from dashboard.routes import static

__all__ = ["static"]
