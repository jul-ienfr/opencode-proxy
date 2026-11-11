"""Source parsers and candidate normalization."""

from __future__ import annotations

import pytest

from free.egress_sources import (
    SourceDefinition,
    dedupe_candidates,
    fetch_text,
    interleave_by_source,
    load_source_file,
    normalize_proxy,
    parse_geonode_json,
    parse_payload,
    parse_public_json,
    parse_public_text,
    parse_source_definitions,
    read_source_text,
)


@pytest.mark.parametrize(
    "value",
    [
        "127.0.0.1:8080",
        "10.0.0.1:3128",
        "192.168.1.1:80",
        "::1:8080",
        "localhost:8080",
        "0.0.0.0:80",
        "[::1]:8080",
    ],
)
def test_private_and_loopback_candidates_are_rejected(value):
    assert normalize_proxy(value) is None


def test_public_candidate_is_normalized():
    row = normalize_proxy("1.2.3.4:8443")
    assert row == {
        "url": "http://1.2.3.4:8443",
        "scheme": "http",
        "host": "1.2.3.4",
        "port": 8443,
        "ipv6": False,
    }


def test_public_text_parser_handles_schemes_comments_and_blanks():
    rows = parse_public_text(
        "# comment\n\n1.2.3.4:80\nhttps://5.6.7.8:443\nsocks5://9.10.11.12:1080\n"
    )
    assert [row["url"] for row in rows] == [
        "http://1.2.3.4:80",
        "https://5.6.7.8:443",
        "socks5://9.10.11.12:1080",
    ]


def test_public_json_parser_accepts_strings_dicts_and_nested_data():
    rows = parse_public_json(
        '{"data": ["1.2.3.4:80", {"ip": "5.6.7.8", "port": 8080, "protocols": ["socks5"]}]}'
    )
    assert [row["scheme"] for row in rows] == ["http", "socks5"]
    assert rows[1]["port"] == 8080


def test_public_json_protocol_flags_are_supported():
    rows = parse_public_json(
        '[{"ip": "1.2.3.4", "port": 1080, "socks5": 1}, '
        '{"ip": "5.6.7.8", "port": 443, "ssl": 1}]'
    )
    assert [row["scheme"] for row in rows] == ["socks5", "https"]


def test_country_suffix_is_stripped_from_text_lines():
    rows = parse_public_text("1.2.3.4:8080:France\n")
    assert rows[0]["host"] == "1.2.3.4"
    assert rows[0]["port"] == 8080


def test_geonode_parser_prefers_socks5_when_declared():
    rows = parse_geonode_json(
        '{"data": [{"ip": "1.2.3.4", "port": 1080, "protocols": ["socks5", "http"]}]}'
    )
    assert rows[0]["scheme"] == "socks5"


def test_payload_dispatches_on_source_type():
    source = SourceDefinition(id="a", type="public_text", url="https://example.test")
    assert parse_payload(source, "1.2.3.4:80")[0]["host"] == "1.2.3.4"


def test_interleave_gives_each_source_a_slot():
    rows = [
        *[{"source_id": "a", "host": f"1.1.1.{index}"} for index in range(10)],
        *[{"source_id": "b", "host": f"2.2.2.{index}"} for index in range(3)],
    ]
    out = interleave_by_source(rows, limit=6)
    assert sum(1 for row in out if row["source_id"] == "a") == 3
    assert sum(1 for row in out if row["source_id"] == "b") == 3


def test_dedupe_bounds_and_drops_duplicates():
    rows = [
        {"scheme": "http", "host": "1.2.3.4", "port": 80},
        {"scheme": "http", "host": "1.2.3.4", "port": 80},
        {"scheme": "http", "host": "5.6.7.8", "port": 80},
    ]
    assert len(dedupe_candidates(rows, limit=10)) == 2
    assert len(dedupe_candidates(rows, limit=1)) == 1


def test_source_definitions_reject_unknown_type_and_missing_id():
    assert parse_source_definitions([{"id": "a", "type": "nope"}]) == []
    assert parse_source_definitions([{"type": "public_text"}]) == []
    rows = parse_source_definitions(
        [{"id": "a", "type": "public_text", "url": "https://example.test", "interval_s": 1}]
    )
    assert rows[0].interval_s == 60


def test_redirect_to_private_host_is_rejected():
    from free.egress_sources import _SafeRedirectHandler

    handler = _SafeRedirectHandler()
    with pytest.raises(ValueError):
        handler.redirect_request(None, None, 302, "Found", {}, "http://127.0.0.1/secret")


def test_local_text_source_is_read_from_disk(tmp_path):
    path = tmp_path / "proxies.txt"
    path.write_text("1.2.3.4:443\n# comment\n", encoding="utf-8")
    source = SourceDefinition(id="manual", type="local_text", url=str(path), scheme="https")
    assert "1.2.3.4:443" in read_source_text(source)
    assert parse_payload(source, read_source_text(source))[0]["scheme"] == "https"


def test_external_source_catalog_is_loaded():
    from pathlib import Path

    rows = load_source_file(Path(__file__).resolve().parent.parent / "config" / "egress_sources.yaml")
    assert len(rows) >= 20
    assert any(source.scheme == "socks5" and source.enabled for source in rows)
    assert rows == sorted(rows, key=lambda source: source.priority, reverse=True)


def test_fetch_text_accepts_an_injected_fetcher():
    assert fetch_text("https://example.test", fetcher=lambda url, timeout: f"{url}:{timeout}") == "https://example.test:20.0"


@pytest.mark.parametrize(
    "url",
    [
        "file:///etc/passwd",
        "http://127.0.0.1/list",
        "http://localhost/list",
        "http://10.0.0.1/list",
        "http://192.168.1.1/list",
        "http://[::1]/list",
    ],
)
def test_source_url_rejects_ssrf_targets(url):
    with pytest.raises(ValueError):
        SourceDefinition.from_config({"id": "a", "type": "public_text", "url": url})
