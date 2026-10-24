"""V6.5.3 — Budget de temps total par requete, avec abandon propre.

**Le probleme.** Une requete peut aujourd'hui attendre indefiniment : un amont
qui accepte la connexion puis ne repond plus laisse le client suspendu sans
statut, sans message, sans fin. Aucune couche ne borne le total — les timeouts
existants bornent des etapes (TTFB, inactivite SSE), pas la duree de vie.

**Ce que fait ce middleware.** Il borne la duree **totale** d'une requete et,
a l'echeance :
  * si rien n'a ete envoye au client -> **504** avec un message explicite ;
  * si les en-tetes sont deja partis -> une derniere trame `error` (si le flux
    est SSE), puis fermeture. On ne peut pas changer un statut deja emis ; le
    minimum honnete est de dire pourquoi ca s'arrete.

**Pourquoi un budget par defaut tres large.** Le budget n'est pas la pour
optimiser, il est la pour empecher l'attente infinie. Mesure de la jambe free :
21 appels, maximum 13,4 s. Un plafond a 900 s ne se declenchera jamais sur du
trafic normal, et coupera net un amont fige.

**Ce qu'il ne fait pas.** Il n'annule pas l'amont cote serveur (impossible depuis
ici) : il libere le client et laisse la connexion amont se fermer d'elle-meme.
"""

from __future__ import annotations

import asyncio
import json

__all__ = ["RequestDeadlineMiddleware", "is_exempt_path", "resolve_budget"]

#: Chemins qui ne doivent JAMAIS etre bornes par ce budget : le tableau de bord
#: sert des fichiers et des evenements longs, et `/health` doit repondre meme
#: quand tout le reste est bloque.
_EXEMPT_PREFIXES = (
    "/health",
    "/api/",
    "/static/",
    "/dashboard",
    "/metrics",
    "/favicon",
    "/ws",
)

#: Budgets absurdes : on les ramene dans une plage sure plutot que de les subir.
_MIN_BUDGET = 5.0
_MAX_BUDGET = 86_400.0


def is_exempt_path(path: str | None) -> bool:
    """Ce chemin echappe-t-il au budget ? (pur, testable)"""
    value = str(path or "")
    return any(value.startswith(prefix) for prefix in _EXEMPT_PREFIXES)


def resolve_budget(configured, default: float = 900.0, min_budget: float = _MIN_BUDGET) -> float:
    """Budget effectif en secondes ; ``0`` ou invalide => desactive.

    ``default`` sert quand la configuration est absente. Une valeur ``<= 0``
    desactive explicitement — c'est le seul moyen de dire « je ne veux pas de
    plafond » sans ambiguite.
    """
    try:
        value = float(configured if configured is not None else default)
    except (TypeError, ValueError):
        value = float(default)
    if value <= 0:
        return 0.0
    return max(float(min_budget), min(_MAX_BUDGET, value))


def _error_payload(path: str, budget: float) -> bytes:
    message = f"Request exceeded the total time budget ({budget:.0f}s) and was aborted"
    if "/messages" in path or path.startswith("/anthropic"):
        body = {"type": "error", "error": {"type": "timeout_error", "message": message}}
    else:
        body = {"error": {"message": message, "type": "timeout_error"}}
    return json.dumps(body, ensure_ascii=False).encode()


class RequestDeadlineMiddleware:
    """ASGI pur : borne la duree totale d'une requete et abandonne proprement."""

    def __init__(self, app, budget_getter=None, default_budget: float = 900.0, min_budget: float = _MIN_BUDGET):
        self.app = app
        self.budget_getter = budget_getter
        self.default_budget = default_budget
        # `min_budget` existe pour les tests : sans lui, on ne pourrait pas
        # verifier l'abandon sans attendre cinq secondes. En production il garde
        # sa valeur par defaut, qui protege d'un budget absurde.
        self.min_budget = min_budget

    def _budget(self) -> float:
        try:
            configured = self.budget_getter() if self.budget_getter else None
        except Exception:
            configured = None
        return resolve_budget(configured, self.default_budget, self.min_budget)

    async def __call__(self, scope, receive, send):
        if scope.get("type") != "http" or is_exempt_path(scope.get("path")):
            await self.app(scope, receive, send)
            return

        budget = self._budget()
        if budget <= 0:
            await self.app(scope, receive, send)
            return

        path = str(scope.get("path") or "")
        state = {"started": False, "content_type": b"", "done": False}

        async def send_wrapper(message):
            if message.get("type") == "http.response.start":
                state["started"] = True
                for name, value in message.get("headers") or []:
                    if name.lower() == b"content-type":
                        state["content_type"] = value.lower()
            elif message.get("type") == "http.response.body" and not message.get("more_body"):
                state["done"] = True
            await send(message)

        try:
            await asyncio.wait_for(self.app(scope, receive, send_wrapper), timeout=budget)
        except (asyncio.TimeoutError, TimeoutError):
            await self._abort(send, state, path, budget)

    async def _abort(self, send, state, path, budget) -> None:
        """Rend l'echeance visible au client, au mieux de ce qui est parti."""
        if state["done"]:
            return
        if not state["started"]:
            body = _error_payload(path, budget)
            try:
                await send(
                    {
                        "type": "http.response.start",
                        "status": 504,
                        "headers": [
                            (b"content-type", b"application/json"),
                            (b"content-length", str(len(body)).encode()),
                        ],
                    }
                )
                await send({"type": "http.response.body", "body": body, "more_body": False})
            except Exception:
                pass
            return
        # Deja commence : on ne peut plus changer le statut. On ferme en disant
        # pourquoi, uniquement si le client sait lire une erreur SSE.
        if b"event-stream" in state["content_type"]:
            frame = (
                b"event: error\ndata: "
                + json.dumps({"type": "error", "error": {"type": "timeout_error",
                              "message": f"time budget {budget:.0f}s exceeded"}}).encode()
                + b"\n\n"
            )
            try:
                await send({"type": "http.response.body", "body": frame, "more_body": False})
            except Exception:
                pass
