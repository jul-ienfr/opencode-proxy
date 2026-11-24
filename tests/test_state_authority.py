"""T308 — ``station_state`` is the single state authority (US2 / P0-02).

Three surfaces read the same fake ``registry.db``:

* the qualification store API (``station_state`` / ``list_station_states``);
* the dashboard read model (``free.qualification.readmodel.stations``), which
  is importable without starting a server;
* a direct SQL read of ``station_state``.

They must expose the same ``(station, client_usable, current_ip)`` triples.  A
divergence on any of them fails the test: this is the permanent guard for
US2 ("une seule vérité d'état").  A later update to one station must also be
visible on every surface, including the five-second dashboard cache.
"""

from __future__ import annotations

import sqlite3
from pathlib import Path

from free.qualification import readmodel
from free.qualification.store import QualificationStore


def _seed_states(store: QualificationStore) -> None:
    store.upsert_station_state(
        1,
        state="USABLE",
        container="opencode-vpn",
        transport="wireguard",
        current_ip="10.0.0.1",
        current_host="one.example",
        client_usable=True,
        now=100.0,
    )
    store.upsert_station_state(
        2,
        state="REPAIRING",
        container="opencode-vpn-2",
        transport="openvpn-udp",
        current_ip="10.0.0.2",
        current_host="two.example",
        client_usable=False,
        now=100.0,
    )
    store.upsert_station_state(
        3,
        state="DRAINING",
        container="opencode-vpn-3",
        transport="wireguard",
        current_ip="10.0.0.3",
        current_host="three.example",
        client_usable=True,
        now=100.0,
    )


def _store_surface(store: QualificationStore) -> dict[int, tuple[bool, str]]:
    """Projection read through the qualification store API."""

    surface = {
        int(row["station"]): (bool(row["client_usable"]), str(row["current_ip"]))
        for row in store.list_station_states()
    }
    # The single-station accessor must agree with the listing; otherwise the
    # API surface itself is inconsistent before we even compare it.
    for station, expected in surface.items():
        row = store.station_state(station)
        assert row is not None, f"store.station_state({station}) lost a persisted row"
        assert (
            int(row["station"]),
            bool(row["client_usable"]),
            str(row["current_ip"]),
        ) == (station, *expected)
    return surface


def _readmodel_surface(db_path: Path) -> dict[int, tuple[bool, str]]:
    """Projection read through the dashboard read model (no server needed)."""

    # The projection caches for five seconds. A write in the same process must
    # be visible immediately, so drop this registry's cached payload first.
    readmodel.invalidate(str(db_path))
    payload = readmodel.stations(str(db_path))
    return {
        int(item["station"]): (bool(item["client_usable"]), str(item["current_ip"]))
        for item in payload["items"]
    }


def _sql_surface(db_path: Path) -> dict[int, tuple[bool, str]]:
    """Projection read directly from the registry table."""

    conn = sqlite3.connect(str(db_path))
    try:
        rows = conn.execute(
            "SELECT station, client_usable, current_ip "
            "FROM station_state ORDER BY station"
        ).fetchall()
    finally:
        conn.close()
    return {
        int(station): (bool(client_usable), str(current_ip))
        for station, client_usable, current_ip in rows
    }


def _assert_no_divergence(db_path: Path, store: QualificationStore) -> None:
    store_rows = _store_surface(store)
    readmodel_rows = _readmodel_surface(db_path)
    sql_rows = _sql_surface(db_path)
    assert store_rows == sql_rows, (
        "qualification store API diverges from registry.db: "
        f"store={store_rows!r} sql={sql_rows!r}"
    )
    assert readmodel_rows == sql_rows, (
        "dashboard read model diverges from registry.db: "
        f"readmodel={readmodel_rows!r} sql={sql_rows!r}"
    )
    assert store_rows, "the fixture must persist at least one station_state row"


def test_station_state_surfaces_do_not_diverge(tmp_path):
    """The store, the dashboard projection and SQL agree on every station."""

    db = tmp_path / "registry.db"
    store = QualificationStore(db)
    try:
        _seed_states(store)
        _assert_no_divergence(db, store)
        assert set(_sql_surface(db)) == {1, 2, 3}
    finally:
        store.close()


def test_station_state_update_is_visible_on_every_surface(tmp_path):
    """Updating one station reaches store, read model and SQL with no stale row."""

    db = tmp_path / "registry.db"
    store = QualificationStore(db)
    try:
        _seed_states(store)
        _assert_no_divergence(db, store)

        store.upsert_station_state(
            2,
            state="USABLE",
            container="opencode-vpn-2",
            transport="wireguard",
            current_ip="10.0.0.22",
            current_host="two.example",
            client_usable=True,
            now=200.0,
        )
        _assert_no_divergence(db, store)

        for name, surface in (
            ("store", _store_surface(store)),
            ("readmodel", _readmodel_surface(db)),
            ("sql", _sql_surface(db)),
        ):
            assert surface[2] == (True, "10.0.0.22"), f"{name} kept a stale row"
            # Unrelated rows are never rewritten by one surface or another.
            assert surface[1] == (True, "10.0.0.1"), f"{name} changed station 1"
            assert surface[3] == (True, "10.0.0.3"), f"{name} changed station 3"
    finally:
        store.close()
