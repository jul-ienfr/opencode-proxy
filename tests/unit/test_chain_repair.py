"""[P3 2026-11-21 — T307] Gardes pures de `free/chain_repair.py`."""

from __future__ import annotations

import json

from free import chain_repair


def test_auto_transport_keeps_every_candidate():
    rows = [{"t": "wireguard"}, {"t": "openvpn-tcp"}]
    assert chain_repair.filter_candidates_for_transport(rows, "auto") == rows


def test_transport_filter_prefers_advertising_rows_and_falls_back():
    rows = [
        {"host": "a", "transports": ["wireguard"]},
        {"host": "b", "transports": ["openvpn-tcp"]},
        {"host": "legacy"},  # pas de métadonnée : éligible (legacy)
    ]
    kept = chain_repair.filter_candidates_for_transport(rows, "wireguard")
    assert [r["host"] for r in kept] == ["a", "legacy"]

    # Aucun candidat ne déclare le transport : on garde tout pour laisser
    # remonter la vraie erreur de bind.
    assert chain_repair.filter_candidates_for_transport(
        [{"host": "x", "transports": ["openvpn-udp"]}], "wireguard"
    ) == [{"host": "x", "transports": ["openvpn-udp"]}]


def test_lock_is_exclusive_while_owner_is_alive(tmp_path):
    path = tmp_path / "chain.lock"
    assert chain_repair.acquire_lock(
        path, pid_alive_fn=lambda pid: True, now=lambda: 100.0
    )
    # Le fichier existe et un propriétaire vivant bloque une seconde prise.
    assert not chain_repair.acquire_lock(
        path, pid_alive_fn=lambda pid: True, now=lambda: 101.0
    )


def test_dead_owner_releases_the_lock(tmp_path):
    path = tmp_path / "chain.lock"
    path.write_text(json.dumps({"pid": 999, "ts": 0.0}), encoding="utf-8")
    assert chain_repair.acquire_lock(
        path, pid_alive_fn=lambda pid: False, now=lambda: 100.0
    )
    data = json.loads(path.read_text(encoding="utf-8"))
    assert data["ts"] == 100.0


def test_stale_owner_releases_the_lock_even_if_alive(tmp_path):
    path = tmp_path / "chain.lock"
    path.write_text(json.dumps({"pid": 999, "ts": 0.0}), encoding="utf-8")
    # 4 100 s > 3 600 s : le lock est considéré périmé.
    assert chain_repair.acquire_lock(
        path, pid_alive_fn=lambda pid: True, now=lambda: 4100.0
    )
    data = json.loads(path.read_text(encoding="utf-8"))
    assert data["ts"] == 4100.0


def test_release_only_removes_the_lock_of_its_owner(tmp_path):
    path = tmp_path / "chain.lock"
    path.write_text(json.dumps({"pid": 123, "ts": 1.0}), encoding="utf-8")
    chain_repair.release_lock(path, pid=456)
    assert path.exists()
    chain_repair.release_lock(path, pid=123)
    assert not path.exists()


def test_exhausted_egress_only_returns_a_known_burned_ip():
    assert chain_repair.exhausted_egress({"ip": "1.2.3.4"}, {"1.2.3.4"}) == "1.2.3.4"
    assert chain_repair.exhausted_egress({"ip": "1.2.3.4"}, set()) == ""
    assert chain_repair.exhausted_egress({}, {"1.2.3.4"}) == ""


def test_transport_parts_mapping():
    assert chain_repair.transport_parts("wireguard") == ("wireguard", None)
    assert chain_repair.transport_parts("openvpn-tcp") == ("openvpn", "tcp")
    assert chain_repair.transport_parts("openvpn-udp") == ("openvpn", "udp")
