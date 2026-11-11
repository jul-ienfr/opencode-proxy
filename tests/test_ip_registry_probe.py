"""StationProbe tests (no network)."""

from __future__ import annotations

import json

import opencode as oc
from free.identities import VERSION_SPECS, project_for
from free.ip_registry.adapters.probe import (
    PAYLOAD_PATH,
    PROBE_VERSIONS,
    StationProbe,
    classify_429_scope,
)


class FakeResponse:
    def __init__(self, status: int, headers: dict | None = None, text: str = ""):
        self.status_code = status
        self.headers = headers or {}
        self.text = text
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
    assert len(result.request_fingerprint) == 64
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


def test_429_scope_classifier_separates_daily_rate_and_transient():
    assert (
        classify_429_scope('{"error":{"type":"FreeUsageLimitError"}}') == "daily"
    )
    assert (
        classify_429_scope('{"error":{"message":"rate limit exceeded"}}')
        == "rate_limit"
    )
    assert classify_429_scope("Endpoint is unavailable") == "transient"
    assert classify_429_scope("") == "unknown"


async def test_probe_429_classifies_a_daily_quota_body():
    post = FakePost(
        [
            FakeResponse(
                429,
                {"retry-after": "65000"},
                text='{"error":{"type":"FreeUsageLimitError"}}',
            )
        ]
    )
    probe = StationProbe(post=post)

    result = await probe.probe(6, "m")

    assert result.status == 429
    assert result.quota_scope == "daily"


async def test_probe_429_classifies_a_transient_body():
    post = FakePost(
        [
            FakeResponse(
                429,
                {},
                text='{"error":{"message":"Endpoint is unavailable"}}',
            )
        ]
    )
    probe = StationProbe(post=post)

    result = await probe.probe(6, "m")

    assert result.status == 429
    assert result.quota_scope == "transient"


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
        "edit",
        "experimental_image_generate",
        "glob",
        "grep",
        "question",
        "read",
        "shell",
        "skill",
        "subagent",
        "webfetch",
        "websearch",
        "write",
        "execute",
    ]
    assert body["stream"] is True
    assert body["max_completion_tokens"] == 32000
    assert "tool_choice" not in body
    assert body["stream_options"] == {"include_usage": True}
    assert body["messages"][0]["role"] == "system"
    assert "You are an AI agent running in OpenCode" in body["messages"][0]["content"]


async def test_probe_headers_match_the_1195_official_wire():
    response = FakeResponse(200)
    post = FakePost([response])
    probe = StationProbe(post=post)

    await probe.probe(1, "deepseek-v4.1-flash-free")

    headers = post.calls[0]["headers"]
    assert headers["User-Agent"] == "opencode/latest/2.2.5/cli"
    assert headers["x-opencode-client"] == "cli"
    base_project = str(getattr(oc, "_OPENCODE_PROJECT", "") or "global")
    assert headers["x-opencode-project"] == project_for("probe", base_project, 0)
    assert headers["x-opencode-session-id"].startswith("ses_")
    assert headers["Authorization"] == "Bearer public"
    assert headers["x-opencode-session"] == headers["x-opencode-session-id"]
    assert headers["x-session-affinity"] == headers["x-opencode-session-id"]
    assert headers["x-session-id"] == headers["x-opencode-session-id"]
    assert "x-opencode-request" not in headers


async def test_probe_project_matches_the_general_path_for_its_egress():
    egress = "87.120.102.53"
    post = FakePost([FakeResponse(200)])
    probe = StationProbe(post=post, egress=lambda station: _async(egress))

    await probe.probe(6, "deepseek-v4.1-flash-free")

    headers = post.calls[0]["headers"]
    base_project = str(getattr(oc, "_OPENCODE_PROJECT", "") or "global")
    assert headers["x-opencode-project"] == project_for(egress, base_project, 0)


def test_probe_version_is_deterministic_and_frozen_until_profiles_complete():
    assert StationProbe._version_for("87.120.102.53") == StationProbe._version_for(
        "87.120.102.53"
    )
    # [2026-11-06] Gel sur 1.19.5 : seul TLS identique au wire 2.2.5.
    assert StationProbe._version_for("87.120.102.53") == "1.19.5"
    versions = {StationProbe._version_for(f"10.0.0.{i}") for i in range(32)}
    assert versions == {"1.19.5"}


def test_probe_headers_follow_the_selected_official_version():
    # The 2.2.5 spec is the current official wire for every selected
    # template version; the template version still drives the TLS replay.
    for version in ("1.18.31", "1.19.5"):
        h = StationProbe._headers("87.120.102.53", version)
        assert h["User-Agent"] == "opencode/latest/2.2.5/cli"
        assert h["x-opencode-session-id"].startswith("ses_")
        assert h["x-opencode-session"] == h["x-opencode-session-id"]


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
    assert len(content) == 43285  # captured Content-Length for this exact body


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
    capture = json.loads(
        (PAYLOAD_PATH.parent / "captures" / "2.2.5.json").read_text(
            encoding="utf-8"
        )
    )
    expected_values = capture["headers"]

    generated = StationProbe._headers("87.120.102.53", "1.19.5")

    # Content-Length is added by curl; every other header is ours.
    assert set(generated) == set(expected_values) - {"Content-Length"}
    for name in ("Authorization", "Content-Type", "User-Agent", "x-opencode-client"):
        assert generated[name] == expected_values[name]
    assert generated["x-opencode-session"].startswith("ses_")
    assert generated["x-opencode-session"] == generated["x-opencode-session-id"]
    assert generated["x-session-affinity"] == generated["x-opencode-session-id"]
    assert generated["x-session-id"] == generated["x-opencode-session-id"]
    assert "x-opencode-request" not in generated
    project = generated["x-opencode-project"]
    assert project == "global" or (
        len(project) == 40
        and all(c in "0123456789abcdefABCDEF" for c in project)
    )


def test_probe_header_switch_for_11831_and_1193():
    # The 2.2.5 spec is the official wire for every template version.
    expected = [
        "Authorization",
        "Content-Type",
        "User-Agent",
        "x-opencode-client",
        "x-opencode-project",
        "x-opencode-session",
        "x-opencode-session-id",
        "b3",
        "traceparent",
        "x-session-affinity",
        "x-session-id",
        "Connection",
        "Accept",
        "Host",
        "Accept-Encoding",
        "Content-Length",
    ]
    for version in ("1.18.31", "1.19.3"):
        h = StationProbe._headers("87.120.102.53", version)
        assert set(h) == set(expected) - {"Content-Length"}
        assert h["x-opencode-session"] == h["x-opencode-session-id"]
        assert h["x-session-affinity"] == h["x-opencode-session-id"]
        assert h["x-session-id"] == h["x-opencode-session-id"]


def test_probe_golden_body_bytes_match_the_capture_length():
    probe = StationProbe()
    content = probe._wire_bytes("deepseek-v4.1-flash-free")
    assert len(content) == 43285
    assert content == oc._serialize_json_body(probe._body)
    assert content.startswith(b'{"model":"deepseek-v4.1-flash-free","messages"')


def test_template_fingerprint_is_stable_and_model_sensitive():
    probe = StationProbe()
    first = probe._template_fingerprint("deepseek-v4.1-flash-free")
    second = probe._template_fingerprint("deepseek-v4.1-flash-free")
    other = probe._template_fingerprint("glm-5.3-flash-free")
    assert first == second
    assert first != other
    assert len(first) == 64


class FakeSSEResponse(FakeResponse):
    def __init__(self, status: int, frames: list[bytes], headers: dict | None = None):
        super().__init__(status, headers or {"content-type": "text/event-stream"})
        self._frames = list(frames)

    def iter_content(self, chunk_size: int = 1024):
        yield from self._frames


async def test_probe_200_accepts_the_first_content_frame():
    post = FakePost(
        [
            FakeSSEResponse(
                200,
                [
                    b'data: {"choices":[{"delta":{"content":"ok"}}]}\n\n',
                ],
            )
        ]
    )
    probe = StationProbe(post=post, validate_sse=True)

    result = await probe.probe(6, "deepseek-v4.1-flash-free")

    assert result.status == 200
    assert result.valid is True
    assert result.validation == "content"
    assert result.ok is True


async def test_probe_200_rejects_an_sse_error_frame():
    post = FakePost(
        [
            FakeSSEResponse(
                200,
                [
                    b'data: {"error":{"message":"FreeUsageLimitError"}}\n\n',
                ],
            )
        ]
    )
    probe = StationProbe(post=post, validate_sse=True)

    result = await probe.probe(6, "deepseek-v4.1-flash-free")

    assert result.status == 200
    assert result.valid is False
    assert result.validation == "error-frame"
    assert result.ok is False


async def test_probe_200_rejects_a_json_error_body():
    post = FakePost(
        [
            FakeSSEResponse(
                200,
                [b'{"error":{"message":"FreeUsageLimitError"}}'],
                headers={"content-type": "application/json"},
            )
        ]
    )
    probe = StationProbe(post=post, validate_sse=True)

    result = await probe.probe(6, "deepseek-v4.1-flash-free")

    assert result.status == 200
    assert result.valid is False
    assert result.validation == "error-body"
    assert result.ok is False


async def test_probe_200_without_a_frame_is_an_empty_stream():
    """A 200 that closes without content is not a recovery."""

    post = FakePost([FakeSSEResponse(200, [])])
    probe = StationProbe(post=post, validate_sse=True)

    result = await probe.probe(6, "deepseek-v4.1-flash-free")

    assert result.status == 200
    assert result.valid is False
    assert result.validation == "empty-stream"
    assert result.ok is False


async def test_probe_200_waits_for_content_after_a_role_frame():
    post = FakePost(
        [
            FakeSSEResponse(
                200,
                [
                    b'data: {"choices":[{"delta":{"role":"assistant"}}]}\n\n',
                    b'data: {"choices":[{"delta":{"content":"ok"}}]}\n\n',
                ],
            )
        ]
    )
    probe = StationProbe(post=post, validate_sse=True)

    result = await probe.probe(6, "deepseek-v4.1-flash-free")

    assert result.status == 200
    assert result.valid is True
    assert result.validation == "content"


async def _async(value: str) -> str:
    return value
