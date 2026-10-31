"""Régression P0.7 : estimation mémoire de la queue DB (cap 256 Mo)."""

from __future__ import annotations

import opencode as oc


class _Row:
    def __init__(self, req, resp):
        self.request_body = req
        self.response_body = resp


def test_db_row_bytes_sums_str_and_bytes():
    assert oc._db_row_bytes(_Row("x" * 100, b"y" * 50)) == 150
    assert oc._db_row_bytes(_Row(None, None)) == 0
    # Objet non mesurable : estimation forfaitaire (jamais 0 silencieux).
    assert oc._db_row_bytes(_Row({"a": 1}, None)) == 4096


def test_db_item_bytes_only_requests():
    row = _Row("a" * 10, "b" * 20)
    assert oc._db_item_bytes(("requests", row)) == 30
    assert oc._db_item_bytes(("free_usage", ("x",))) == 0
    assert oc._db_item_bytes(None) == 0


def test_queue_byte_cap_is_bounded():
    assert oc._DB_QUEUE_BYTES_MAX == 256 * 1024 * 1024
    assert oc._db_queue.maxsize == 10000
