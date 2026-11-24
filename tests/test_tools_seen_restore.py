"""[P1 2026-11-21 — T150] Restauration du registre `_tools_used_seen` au boot."""

from __future__ import annotations

import sqlite3

import opencode as oc


def test_restore_tools_used_seen_reads_all_rows_and_dedupes(tmp_path):
    """Le registre repart vide à chaque boot : un scan complet, une fois,
    doit rendre les outils absents des 5 000 dernières lignes."""
    db = tmp_path / "requests.db"
    con = sqlite3.connect(db)
    try:
        con.execute("CREATE TABLE requests (tools_used TEXT)")
        con.executemany(
            "INSERT INTO requests(tools_used) VALUES (?)",
            [
                ('["Read","Grep"]',),
                ('["Grep","Bash"]',),
                ("[]",),
                (None,),
                ("not-json",),
            ],
        )
        con.commit()
    finally:
        con.close()

    seen: set[str] = set()
    restored = oc._restore_tools_used_seen(str(db), seen)

    assert seen == {"Read", "Grep", "Bash"}
    assert restored == 3
