"""Ordre des clés du corps chat officiel (extrait d'``opencode.py``).

Le client officiel émet les clés dans un ordre mesuré (242/243 requêtes).
Ce module isole la constante et le réordonnanceur pour que ``opencode`` et les
tests partagent une seule définition.
"""

from __future__ import annotations

#: Ordre des clés du corps chat officiel — MESURÉ 242/243 requêtes.
CLIENT_CHAT_KEY_ORDER: tuple[str, ...] = (
    "model",
    "max_tokens",
    "messages",
    "tools",
    "tool_choice",
    "stream",
    "stream_options",
)

_CLIENT_CHAT_KEY_ORDER = CLIENT_CHAT_KEY_ORDER


def reorder_client_body(wire: dict) -> dict:
    """Réordonne les clés du corps selon la séquence officielle mesurée.

    Les clés inconnues (non mesurées chez le client) sont conservées À LA
    FIN, dans leur ordre d'origine — on ne supprime ni n'invente rien.
    """
    try:
        out = {k: wire[k] for k in CLIENT_CHAT_KEY_ORDER if k in wire}
        for k, v in wire.items():
            if k not in out:
                out[k] = v
        return out
    except Exception:
        return wire


_reorder_client_body = reorder_client_body

__all__ = [
    "CLIENT_CHAT_KEY_ORDER",
    "_CLIENT_CHAT_KEY_ORDER",
    "_reorder_client_body",
    "reorder_client_body",
]
