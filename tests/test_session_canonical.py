"""Verrouille le format canonique des sessions `ses_` (gate 2).

Source : docs/free-tier-gates.md de ebouther/opencode-unlimited-proxy
(2026-10-16/27) et le probe `gate-probe.py` : le serveur décode les 12 hex
comme `Identifier.descending()` et refuse une valeur dont l'horloge implicite
est hors fenêtre. Un `ses_` bien formé mais aléatoire (sha256/UUID) est refusé.

Le layout exact est `~(unix_ms << 12 | counter)` sur 48 bits, rendu en 12 hex.
Le premier mint d'une milliseconde a donc les 3 derniers hex à `ffe`.
"""

from __future__ import annotations

import threading
import time

from free.identities import _mint


def _expected_head(ms: int, counter: int = 1) -> str:
    value = (~((ms << 12) | counter)) & ((1 << 48) - 1)
    return f"{value:012x}"


def test_minted_session_is_canonical_descending():
    state = {"last_ms": 0, "counter": 0, "lock": threading.Lock()}
    before = int(time.time() * 1000)
    sid = _mint("ses", "descending", state)
    after = int(time.time() * 1000)

    assert sid.startswith("ses_")
    assert len(sid) == 30
    head = sid[4:16]
    assert len(head) == 12
    assert all(c in "0123456789abcdef" for c in head)
    assert head.endswith("ffe"), "compteur 1 → low 12 bits 0xffe"

    # Le head doit correspondre à l'horloge de mint, à quelques ms près.
    assert any(head == _expected_head(ms) for ms in range(before - 2, after + 3)), (
        f"head {head} ne correspond à aucune ms de [{before}, {after}]"
    )


def test_capture_session_is_canonical_shape():
    """La session de la capture 2.2.5 doit avoir le layout canonique."""
    head = "e587cf1beffe"
    assert len(head) == 12
    assert all(c in "0123456789abcdef" for c in head)
    assert head.endswith("ffe")
