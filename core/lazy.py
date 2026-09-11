"""core.lazy — proxy de module à import différé (Phase 3b-1 boot).

Motif : ``import httpx`` sur le chemin *import → listen* coûte **~81 ms**
mesurés (``python -X importtime``), dont l'essentiel est dans
``httpx/__init__.py`` ligne 15 ::

    try:
        from ._main import main      # CLI httpx
    except ImportError: ...

``httpx._main`` tire ``rich`` (~33 ms : ``rich.console`` + ``rich.progress``),
``click`` (~10 ms) et ``pygments``. **Aucun** de ces modules ne sert au proxy :
ils n'existent que pour la commande ``httpx`` en ligne de commande. C'est aussi
la raison pour laquelle rendre ``dashboard/display.py`` lazy n'avait pas suffi —
``rich`` revenait par cette porte.

``LazyModule`` permet de garder **toute la syntaxe existante** (``httpx.AsyncClient``,
``except httpx.ReadError``, constructions, etc.) sans charger le module avant le
premier accès réel — c'est-à-dire à la première requête amont, jamais au boot.

Contrat d'usage (le typage mypy est préservé) ::

    from typing import TYPE_CHECKING, Any

    if TYPE_CHECKING:
        import httpx
    else:
        from core.lazy import LazyModule
        httpx = LazyModule("httpx")   # type: ignore[assignment]

mypy n'analyse que la branche ``TYPE_CHECKING`` (idiome supporté) : il voit le
vrai module et type ``httpx.X`` normalement. À l'exécution, ``TYPE_CHECKING``
vaut ``False`` et c'est le proxy qui est lié.

ATTENTION — ce que le proxy NE peut PAS différer :

* une **annotation non quotée** au niveau module ou dans une signature est
  évaluée à la définition → elle déclencherait l'import. Les annotations
  concernées doivent être écrites en littéral de chaîne
  (``_client: "httpx.AsyncClient | None" = None``) ;
* une **valeur** construite au niveau module (``TIMEOUT = httpx.Timeout(...)``)
  est évaluée immédiatement → à remplacer par une fabrique appelée à l'usage.
"""

from __future__ import annotations

import importlib
from types import ModuleType
from typing import Any

__all__ = ["LazyModule", "load"]


class LazyModule:
    """Proxy d'un module : l'import réel a lieu au premier accès d'attribut.

    * thread-safe de fait : le GIL protège la double-vérification, et un import
      concurrent du même module est idempotent (``sys.modules``) ;
    * transparent : ``__getattr__`` délègue tout au module réel, y compris les
      classes d'exception utilisées dans les clauses ``except`` ;
    * ``__repr__`` indique si le module est déjà chargé (debug/tests).
    """

    __slots__ = ("_name", "_mod")

    def __init__(self, name: str) -> None:
        object.__setattr__(self, "_name", name)
        object.__setattr__(self, "_mod", None)

    @property
    def __name__(self) -> str:
        return object.__getattribute__(self, "_name")

    def _load(self) -> ModuleType:
        mod = object.__getattribute__(self, "_mod")
        if mod is None:
            mod = importlib.import_module(object.__getattribute__(self, "_name"))
            object.__setattr__(self, "_mod", mod)
        return mod

    def __getattr__(self, item: str) -> Any:
        # Appelé uniquement si l'attribut n'est pas trouvé dans les slots.
        return getattr(self._load(), item)

    def __setattr__(self, name: str, value: Any) -> None:
        """Patch d'attribut délégué au module réel (chargé au besoin).

        Indispensable pour ``monkeypatch.setattr(oc.httpx, "AsyncClient", stub)``
        (tests/test_free_vpn_required.py, test_tool_compat.py…) : écrire sur le
        proxy ne patcherait que le proxy, alors que le code amont lit le VRAI
        module. On écrit donc dans ``httpx`` lui-même — sémantique identique à
        celle d'un ``import httpx`` classique (y compris le teardown du
        monkeypatch, qui doit pouvoir restaurer l'attribut d'origine).
        """
        if name in ("_name", "_mod"):
            object.__setattr__(self, name, value)
            return
        setattr(self._load(), name, value)

    def __delattr__(self, name: str) -> None:
        if name in ("_name", "_mod"):
            object.__delattr__(self, name)
            return
        delattr(self._load(), name)

    def __dir__(self) -> list[str]:
        return sorted(set(object.__dir__(self)) | set(dir(self._load())))

    def __repr__(self) -> str:
        name = object.__getattribute__(self, "_name")
        loaded = object.__getattribute__(self, "_mod") is not None
        return f"<LazyModule {name!r}{' (loaded)' if loaded else ' (deferred)'}>"


def load(name: str) -> ModuleType:
    """Import + mise en cache, à utiliser depuis du code non annoté."""
    return LazyModule(name)._load()
