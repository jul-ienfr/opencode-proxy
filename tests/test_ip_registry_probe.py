"""StationProbe tests (no network)."""

from __future__ import annotations

import json

import opencode as oc
from free.identities import VERSION_SPECS, project_for
from free.ip_registry.adapters.probe import PAYLOAD_PATH, PROBE_VERSIONS, StationProbe


class FakeResponse:
    def __init__(self, status: int, headers: dict | None = None):
        self.status_code = status
        self.headers = headers or {}
        self.closed = False

    def close(self) -> None:
        self.closed = True


class FakePost:
    def __init__(self, responses: list):
        self._responses = list(responses)
        self.calls: list[dict] = []

    def __call__(self, url, **kwargs):
        self.calls.append(kwargs)
        item = self._responses.pop(0)
        if isinstance(item, Exception):
            raise item
        return item


async def test_probe_200_uses_official_headers():
    response = FakeResponse(200)
    post = FakePost([response])
    probe = StationProbe(post=post, egress=lambda station: _async("87.120.102.53"))

    result = await probe.probe(6, "deepseek-v4.1-flash-free")

    assert result.status == 200
    assert result.ok
    assert result.latency_ms is not None
    assert response.closed
    kwargs = post.calls[0]
    assert kwargs["headers"]["Authorization"] == "Bearer public"
    assert json.loads(kwargs["content"])["model"] == "deepseek-v4.1-flash-free"
    assert kwargs["stream"] is True
    assert "socks5h://127.0.0.1:1085" in kwargs["proxies"]["https"]


async def test_probe_429_reads_retry_after():
    post = FakePost([FakeResponse(429, {"retry-after": "3600"})])
    probe = StationProbe(post=post)

    result = await probe.probe(6, "m")

    assert result.status == 429
    assert result.retry_after_s == 3600.0
    assert result.is_quota_429


async def test_probe_falls_back_to_local_dns():
    post = FakePost([RuntimeError("remote dns failed"), FakeResponse(200)])
    probe = StationProbe(post=post)

    result = await probe.probe(2, "m")

    assert result.status == 200
    assert len(post.calls) == 2
    assert "socks5h://" in post.calls[0]["proxies"]["https"]
    assert "socks5://" in post.calls[1]["proxies"]["https"]


async def test_probe_returns_error_when_both_fail():
    post = FakePost([RuntimeError("a"), RuntimeError("b")])
    probe = StationProbe(post=post)

    result = await probe.probe(1, "m")

    assert result.status == 0
    assert result.error is not None
    assert "RuntimeError" in result.error


async def test_probe_uses_the_full_official_tool_grid_in_order():
    response = FakeResponse(200)
    post = FakePost([response])
    probe = StationProbe(post=post)

    await probe.probe(1, "deepseek-v4.1-flash-free")

    body = json.loads(post.calls[0]["content"])
    names = [tool["function"]["name"] for tool in body["tools"]]
    assert names == [
        "bash",
        "edit",
        "glob",
        "grep",
        "read",
        "skill",
        "task",
        "todowrite",
        "webfetch",
        "websearch",
        "write",
    ]
    assert body["stream"] is True
    assert body["max_tokens"] == 32000
    assert body["tool_choice"] == "auto"
    assert body["stream_options"] == {"include_usage": True}
    assert body["messages"][0]["role"] == "system"
    assert "You are opencode" in body["messages"][0]["content"]


async def test_probe_headers_match_the_1195_official_wire():
    response = FakeResponse(200)
    post = FakePost([response])
    probe = StationProbe(post=post)

    await probe.probe(1, "deepseek-v4.1-flash-free")

    headers = post.calls[0]["headers"]
    assert (
        headers["User-Agent"]
        == "opencode/1.19.5 ai-sdk/provider-utils/4.0.23 runtime/bun/1.4.4"
    )
    assert headers["x-opencode-client"] == "desktop"
    base_project = str(getattr(oc, "_OPENCODE_PROJECT", "") or "global")
    assert headers["x-opencode-project"] == project_for("probe", base_project, 0)
    assert headers["x-opencode-session-id"].startswith("ses_")
    assert headers["Authorization"] == "Bearer public"
    assert "x-session-affinity" not in headers
    assert "x-session-id" not in headers


async def test_probe_project_matches_the_general_path_for_its_egress():
    egress = "87.120.102.53"
    post = FakePost([FakeResponse(200)])
    probe = StationProbe(post=post, egress=lambda station: _async(egress))

    await probe.probe(6, "deepseek-v4.1-flash-free")

    headers = post.calls[0]["headers"]
    base_project = str(getattr(oc, "_OPENCODE_PROJECT", "") or "global")
    assert headers["x-opencode-project"] == project_for(egress, base_project, 0)


def test_probe_version_is_deterministic_and_varies_by_egress():
    assert StationProbe._version_for("87.120.102.53") == StationProbe._version_for(
        "87.120.102.53"
    )
    assert StationProbe._version_for("87.120.102.53") in (
        "1.19.5",
        "1.19.3",
        "1.18.31",
        "1.19.0",
    )
    versions = {StationProbe._version_for(f"10.0.0.{i}") for i in range(32)}
    assert len(versions) > 1, "les profils ne doivent pas être figés sur 1.19.5"


def test_probe_headers_follow_the_selected_official_version():
    h_old = StationProbe._headers("87.120.102.53", "1.18.31")
    assert h_old["User-Agent"].startswith(
        "opencode/1.18.31 ai-sdk/provider-utils/4.0.23 runtime/bun/1.3.14"
    )
    assert "x-opencode-session-id" not in h_old

    h_new = StationProbe._headers("87.120.102.53", "1.19.5")
    assert h_new["User-Agent"].startswith(
        "opencode/1.19.5 ai-sdk/provider-utils/4.0.23 runtime/bun/1.4.4"
    )
    assert h_new["x-opencode-session-id"].startswith("ses_")


async def test_probe_passes_the_selected_version_to_the_tls_replay():
    egress = "87.120.102.53"
    post = FakePost([FakeResponse(200)])
    probe = StationProbe(post=post, egress=lambda station: _async(egress))

    await probe.probe(6, "deepseek-v4.1-flash-free")

    assert post.calls[0]["fp_version"] == StationProbe._version_for(egress)


def test_probe_curl_post_sends_the_compact_official_bytes(monkeypatch):
    """curl_cffi's json= adds indent=2; the official Bun body is compact.
    The probe must send the same orjson bytes as the real free leg."""
    captured = {}

    def _fake_post(url, **kwargs):
        captured.update(kwargs)
        return FakeResponse(200)

    monkeypatch.setattr("curl_cffi.requests.post", _fake_post)
    monkeypatch.setattr(oc, "_get_free_fp_kwargs", lambda version="": ("", {}))

    probe = StationProbe()
    body = dict(probe._body)
    probe._curl_post("https://opencode.ai/x", headers={}, json=body)

    content = captured["content"]
    assert isinstance(content, bytes)
    assert content == oc._serialize_json_body(body)
    assert len(content) == 46010  # captured Content-Length for this exact body


def test_probe_curl_post_applies_the_1195_tls_replay(monkeypatch):
    captured = {}

    def _fake_post(url, **kwargs):
        captured.update(kwargs)
        return FakeResponse(200)

    monkeypatch.setattr("curl_cffi.requests.post", _fake_post)
    monkeypatch.setattr(
        oc,
        "_get_free_fp_kwargs",
        lambda version="": (
            "",
            {
                "impersonate": "chrome131",
                "ja3": "official-ja3",
                "extra_fp": {"tls_grease": False},
                "http_version": "v1",
            },
        ),
    )

    probe = StationProbe()
    probe._curl_post("https://opencode.ai/x", headers={}, json={})

    assert captured["impersonate"] == "chrome131"
    assert captured["ja3"] == "official-ja3"
    assert captured["extra_fp"] == {"tls_grease": False}
    assert captured["http_version"] == "v1"


def test_probe_versions_are_all_known_official_profiles():
    assert set(PROBE_VERSIONS) <= set(VERSION_SPECS)
    assert len(set(PROBE_VERSIONS)) == len(PROBE_VERSIONS)


def test_probe_header_order_and_static_values_match_the_capture():
    captured = json.loads(PAYLOAD_PATH.read_text(encoding="utf-8"))[
        "_request_headers"
    ]
    expected_order = [name for name, _value in captured]
    expected_values = dict(captured)

    generated = StationProbe._headers("87.120.102.53", "1.19.5")

    # Content-Length is added by curl; every other header is ours, in order.
    assert list(generated) + ["Content-Length"] == expected_order
    for name in (
        "Authorization",
        "Content-Type",
        "User-Agent",
        "x-opencode-client",
        "Connection",
        "Accept",
        "Host",
        "Accept-Encoding",
    ):
        assert generated[name] == expected_values[name]
    assert generated["x-opencode-request"].startswith("msg_")
    assert generated["x-opencode-session"].startswith("ses_")
    assert generated["x-opencode-session-id"] == generated["x-opencode-session"]
    project = generated["x-opencode-project"]
    assert project == "global" or (
        len(project) == 40
        and all(c in "0123456789abcdefABCDEF" for c in project)
    )


def test_probe_header_switch_for_11831_and_1193():
    old = StationProbe._headers("87.120.102.53", "1.18.31")
    assert list(old) + ["Content-Length"] == [
        "Authorization",
        "Content-Type",
        "User-Agent",
        "x-opencode-client",
        "x-opencode-project",
        "x-opencode-request",
        "x-opencode-session",
        "Connection",
        "Accept",
        "Host",
        "Accept-Encoding",
        "Content-Length",
    ]

    new = StationProbe._headers("87.120.102.53", "1.19.3")
    assert list(new) + ["Content-Length"] == [
        "Authorization",
        "Content-Type",
        "User-Agent",
        "x-opencode-client",
        "x-opencode-project",
        "x-opencode-request",
        "x-opencode-session",
        "x-opencode-session-id",
        "Connection",
        "Accept",
        "Host",
        "Accept-Encoding",
        "Content-Length",
    ]


def test_probe_golden_body_bytes_match_the_capture_length():
    probe = StationProbe()
    content = probe._wire_bytes("deepseek-v4.1-flash-free")
    assert len(content) == 46010
    assert content == oc._serialize_json_body(probe._body)
    assert content.startswith(b'{"model":"deepseek-v4.1-flash-free","max_tokens":32000')


async def _async(value: str) -> str:
    return value
