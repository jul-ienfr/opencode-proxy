#!/usr/bin/env python3
"""Régression : les `ses_` free doivent passer le contrôle temporel du gate.

Le gate free décode les 12 premiers hex de `ses_` comme un timestamp
descendant (``descending()`` dans ``packages/schema/src/identifier.ts``),
pas comme un identifiant aléatoire. Un préfixe tiré au hasard
(``secrets.token_hex(6)``) est rejeté avec ``FreeUsageLimitError`` et un
``Retry-After`` jusqu'à 00:00 UTC, même sur une IP qui a du quota. C'était
le bug du harnais ``scripts/test_free_egress.py`` : tous ses 429 directs
et stations étaient des faux négatifs.

Ce test vérifie que le minter de production produit un préfixe qui décode
à l'instant présent, et que l'ancien format aléatoire ne decode pas dans
la fenêtre.
"""

from __future__ import annotations

import secrets
import string
import sys
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

import opencode as oc  # noqa: E402

B62 = string.ascii_letters + string.digits
MASK48 = (1 << 48) - 1
MOD36 = 1 << 36


def decoded_timestamp_ms(session: str) -> int:
    prefix = int(session[4:16], 16)
    current = (~prefix) & MASK48
    return current >> 12


def now_timestamp_ms() -> int:
    return int(time.time() * 1000) % MOD36


def freshness_delta_ms(session: str) -> int:
    return abs(decoded_timestamp_ms(session) - now_timestamp_ms())


def check_minted() -> None:
    sid = oc._mint_oc_id("ses", "descending")
    assert sid.startswith("ses_"), sid
    assert len(sid) == 30, (len(sid), sid)
    assert all(c in "0123456789abcdef" for c in sid[4:16]), sid
    assert all(c in B62 for c in sid[16:]), sid
    delta = freshness_delta_ms(sid)
    assert delta < 5_000, (delta, sid)
    print(f"OK minted {sid} delta_ms={delta}")


def check_unique() -> None:
    ids = {oc._mint_oc_id("ses", "descending") for _ in range(100)}
    assert len(ids) == 100, len(ids)
    print("OK 100/100 uniques")


def check_legacy_random_rejected() -> None:
    # L'ancien format du harnais : 12 hex aléatoires + 14 base62.
    # La probabilité qu'un tirage tombe dans la fenêtre de 5 s est ~7e-8 ;
    # on teste 20 tirages pour rendre la régression déterministe en pratique.
    for _ in range(20):
        sid = "ses_" + secrets.token_hex(6) + "".join(secrets.choice(B62) for _ in range(14))
        assert freshness_delta_ms(sid) > 5_000, sid
    print("OK 20/20 random prefixes outside the freshness window")


def main() -> int:
    check_minted()
    check_unique()
    check_legacy_random_rejected()
    print("PASS")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
