"""Disponibilité par egress — 024 Phase 1.5 (T314 / T320).

Fonctions pures : classent un résultat d'egress en un verdict borné
(``alive`` / ``quota`` / ``transport``) et produisent les séries Prometheus
par egress. Aucun I/O réseau ; la lecture de l'état se fait via
:mod:`free.ip_state` côté hôte.

Règle projet (AGENTS.md §0) : une série d'échecs sur une IP ne vaut pas pour
l'ensemble du pool ; un verdict de modèle n'est ``alive`` que sur un **200
avec contenu**, jamais sur un 429 (principe VII).
"""

from __future__ import annotations

from collections.abc import Iterable
from typing import Any

#: Verdicts bornés d'un egress (labels Prometheus stables).
ALIVE = "alive"
QUOTA = "quota"
TRANSPORT = "transport"

VERDICTS: frozenset[str] = frozenset({ALIVE, QUOTA, TRANSPORT})


def egress_verdict(
    status: int | None,
    *,
    reset_at: float | None = None,
    now: float | None = None,
) -> str:
    """[T314] Classe un résultat d'egress en verdict borné.

    * ``2xx``  -> ``alive`` (le modèle a répondu) ;
    * ``429``  -> ``quota`` (bucket épuisé ; ``reset_at`` fournisseur si connu) ;
    * tout le reste (0, 5xx, timeout, ``None``) -> ``transport``.

    ``reset_at``/``now`` sont acceptés pour rester compatibles avec un appelant
    qui veut horodater, mais ne changent pas la classe : seul le code décide.
    """
    try:
        code = int(status) if status is not None else 0
    except (TypeError, ValueError):
        code = 0
    if 200 <= code < 300:
        return ALIVE
    if code == 429:
        return QUOTA
    return TRANSPORT


def model_alive_from_egress(verdicts: Iterable[str]) -> bool:
    """[T314] Le modèle est ``alive`` dès qu'un egress a répondu ``alive``.

    Un ``quota`` (429) ou un ``transport`` ne rend **jamais** le modèle
    ``alive`` : seule une réponse 2xx le fait (principe VII). Un modèle peut
    donc rester servi alors que des IP sont épuisées, tant qu'une IP répond.
    """
    return any(str(v) == ALIVE for v in verdicts)


def state_rows(states: Iterable[Any]) -> list[tuple[str, int]]:
    """[T320] Séries ``egress_ip_state{provider,egress,state}`` = 1.

    Une ligne par IP d'egress connue, sa machine à états exposée telle quelle
    (``proven``/``serving``/``spent``/``burned``/``unproven``/``mismatch``).
    """
    rows: list[tuple[str, int]] = []
    for st in states:
        provider = str(getattr(st, "provider", "") or "").strip()
        ip = str(getattr(st, "ip", "") or "").strip()
        state = str(getattr(st, "state", "") or "").strip()
        if not provider or not ip or not state:
            continue
        rows.append((f'provider="{provider}",egress="{ip}",state="{state}"', 1))
    return rows


def reset_rows(states: Iterable[Any]) -> list[tuple[str, float]]:
    """[T320] Séries ``egress_reset_at_seconds{provider,egress}`` = reset_at.

    Seules les IP avec un ``reset_at`` (``spent``/``burned``) sont émises :
    c'est ce qui permet d'alerter sur « combien d'IP sont bloquées et jusqu'à
    quand » sans lire la base à la main.
    """
    rows: list[tuple[str, float]] = []
    for st in states:
        reset = getattr(st, "reset_at", None)
        if not reset:
            continue
        provider = str(getattr(st, "provider", "") or "").strip()
        ip = str(getattr(st, "ip", "") or "").strip()
        if not provider or not ip:
            continue
        rows.append((f'provider="{provider}",egress="{ip}"', float(reset)))
    return rows


__all__ = [
    "ALIVE",
    "QUOTA",
    "TRANSPORT",
    "VERDICTS",
    "egress_verdict",
    "model_alive_from_egress",
    "reset_rows",
    "state_rows",
]
