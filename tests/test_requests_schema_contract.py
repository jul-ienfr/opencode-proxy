"""Schema/INSERT contract: one column list, no silent 35/36 drift."""

from __future__ import annotations

import re
import sqlite3

from app.db import _INSERT_REQUESTS_SQL, init_requests_schema


def test_insert_columns_match_the_real_requests_schema():
    conn = sqlite3.connect(":memory:")
    try:
        init_requests_schema(conn, busy_timeout=5000, cache_size=1000, mmap_size=1024)
        schema_columns = {row[1] for row in conn.execute("PRAGMA table_info(requests)")}
    finally:
        conn.close()

    match = re.search(
        r"INSERT\s+OR\s+REPLACE\s+INTO\s+requests\s*\((.*?)\)\s*VALUES",
        _INSERT_REQUESTS_SQL,
        flags=re.IGNORECASE | re.DOTALL,
    )
    assert match, _INSERT_REQUESTS_SQL
    insert_columns = [part.strip() for part in match.group(1).split(",") if part.strip()]
    assert insert_columns
    assert len(insert_columns) == len(set(insert_columns)), "colonne dupliquée dans l'INSERT"
    assert set(insert_columns) <= schema_columns
    # Colonnes historiques présentes mais non écrites par l'INSERT principal.
    assert schema_columns - set(insert_columns) <= {"hedged", "winner_station"}
    assert _INSERT_REQUESTS_SQL.count("?") == len(insert_columns)


def test_server_host_is_indexed_for_quality_rebuild():
    conn = sqlite3.connect(":memory:")
    try:
        init_requests_schema(conn, busy_timeout=5000, cache_size=1000, mmap_size=1024)
        indexes = {row[1] for row in conn.execute("PRAGMA index_list(requests)")}
    finally:
        conn.close()
    assert "idx_requests_server_host_time" in indexes
