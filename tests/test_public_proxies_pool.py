"""Pool integration for public proxies (worker lifecycle + feed selection)."""

from __future__ import annotations

import json
import time

import pytest

import free.pool as fp


class _Station:
    def __init__(self, sid: int = 1):
        self._station = sid
        self._config = {}
        self.current_ip = "10.0.0.1"
        self.pid = None
        self._quota_per_ip = 500
        self.enabled = True
        self.proxy_mode = "vpn"
        self.status = "connected"
        self.current_server = None
        self._egress_state = "tunnel_up_full"
        self._last_effective_ok_at = time.monotonic()
        self.socks5_url = f"socks5://127.0.0.1:{1080 + sid}"

    def get_status(self) -> dict:
        return {"station": self._station, "status": self.status}

    def note_free_request(self) -> None:
        pass

    def arm_egress_watchdog(self) -> None:
        pass


class _FakeProcess:
    def poll(self):
        return None


class _FakeHandle:
    def __init__(self):
        self.process = _FakeProcess()
        self.port = 9999
        self.stopped = False

    def local_proxy_url(self, upstream_id: str) -> str:
        return f"http://{upstream_id}:token@127.0.0.1:{self.port}"

    def is_alive(self) -> bool:
        return True

    def stop(self) -> None:
        self.stopped = True


def _pool() -> fp.FreeIPPool:
    pool = fp.FreeIPPool(_Station())
    pool._egress_providers = []
    return pool


def _rows() -> list[dict]:
    return [
        {"url": "https://1.2.3.4:443", "egress_ip": "1.2.3.4"},
        {"url": "https://1.2.3.5:443", "egress_ip": "1.2.3.4"},
        {"url": "https://1.2.3.6:443", "egress_ip": "1.2.3.6"},
        {"url": "https://1.2.3.7:443", "egress_ip": "1.2.3.7"},
    ]


def test_set_public_proxies_dedupes_egress_and_builds_transport(monkeypatch):
    handle = _FakeHandle()
    monkeypatch.setattr("free.public_egress_worker.start_worker_process", lambda upstreams: handle)
    pool = _pool()
    count = pool.set_public_proxies(_rows(), max_active=2, max_per_24=2)
    assert count == 2
    assert [row["egress_ip"] for row in pool._public_https_proxies] == ["1.2.3.4", "1.2.3.6"]
    ep = pool._https_next()
    assert ep is not None
    assert ep.socks5_url.startswith("http://p")
    assert ep.socks5_url.endswith(":token@127.0.0.1:9999")
    assert ep.provider == "public"
    assert ep.egress_ip in {"1.2.3.4", "1.2.3.6"}


def test_public_proxies_cap_per_24(monkeypatch):
    handle = _FakeHandle()
    monkeypatch.setattr("free.public_egress_worker.start_worker_process", lambda upstreams: handle)
    pool = _pool()
    rows = [
        {"url": "https://1.2.3.4:443", "egress_ip": "1.2.3.4"},
        {"url": "https://1.2.3.5:443", "egress_ip": "1.2.3.5"},
        {"url": "https://1.2.4.4:443", "egress_ip": "1.2.4.4"},
    ]
    count = pool.set_public_proxies(rows, max_active=10, max_per_24=1)
    assert count == 2
    assert [row["egress_ip"] for row in pool._public_https_proxies] == ["1.2.3.4", "1.2.4.4"]


def test_public_proxies_are_idempotent_when_signature_is_unchanged(monkeypatch):
    handle = _FakeHandle()
    calls = []

    def _start(upstreams):
        calls.append(upstreams)
        return handle

    monkeypatch.setattr("free.public_egress_worker.start_worker_process", _start)
    pool = _pool()
    pool.set_public_proxies(_rows(), max_active=2, max_per_24=2)
    pool.set_public_proxies(_rows(), max_active=2, max_per_24=2)
    assert len(calls) == 1


def test_manual_https_and_public_lists_coexist(monkeypatch):
    handle = _FakeHandle()
    monkeypatch.setattr("free.public_egress_worker.start_worker_process", lambda upstreams: handle)
    pool = _pool()
    pool.set_https_proxies([{"host": "manual.example", "port": 89}])
    pool.set_public_proxies(_rows(), max_active=1, max_per_24=2)
    assert len(pool._manual_https_proxies) == 1
    assert len(pool._public_https_proxies) == 1
    assert len(pool._https_proxies) == 2


def test_set_public_proxies_can_use_container_isolation(monkeypatch):
    handle = _FakeHandle()
    calls = []

    def _start_container(upstreams, *, image):
        calls.append((upstreams, image))
        return handle

    monkeypatch.setattr("free.public_egress_container.start_worker_container", _start_container)
    pool = _pool()
    count = pool.set_public_proxies(
        _rows(),
        max_active=1,
        max_per_24=2,
        isolation="container",
        container_image="python:3.12-slim",
    )
    assert count == 1
    assert calls and calls[0][1] == "python:3.12-slim"
    assert pool._public_worker is handle


def test_configure_public_proxies_loads_feed(monkeypatch, tmp_path):
    handle = _FakeHandle()
    monkeypatch.setattr("free.public_egress_worker.start_worker_process", lambda upstreams: handle)
    feed = tmp_path / "feed.jsonl"
    feed.write_text(
        "".join(json.dumps(row) + "\n" for row in _rows()),
        encoding="utf-8",
    )
    pool = _pool()
    pool.configure_public_proxies(
        {
            "enabled": True,
            "feed_path": str(feed),
            "max_active": 1,
            "max_per_24": 2,
        }
    )
    assert len(pool._public_https_proxies) == 1
    assert pool._public_worker is handle


def test_configure_public_proxies_prefers_the_catalog_rows(monkeypatch):
    handle = _FakeHandle()
    monkeypatch.setattr("free.public_egress_worker.start_worker_process", lambda upstreams: handle)
    pool = _pool()
    pool._public_rows_provider = lambda: [
        {"url": "https://1.2.3.9:443", "egress_ip": "1.2.3.9", "last_status": 200}
    ]
    pool.configure_public_proxies(
        {"enabled": True, "feed_path": "unused.jsonl", "max_active": 4, "isolation": "process"}
    )
    assert [row["egress_ip"] for row in pool._public_https_proxies] == ["1.2.3.9"]


def test_empty_catalog_keeps_the_current_public_pool(monkeypatch):
    handle = _FakeHandle()
    monkeypatch.setattr("free.public_egress_worker.start_worker_process", lambda upstreams: handle)
    pool = _pool()
    pool.set_public_proxies(_rows()[:1], max_active=2, max_per_24=2)
    pool._public_rows_provider = lambda: []
    pool.configure_public_proxies(
        {"enabled": True, "feed_path": "unused.jsonl", "max_active": 2, "isolation": "process"}
    )
    assert [row["egress_ip"] for row in pool._public_https_proxies] == ["1.2.3.4"]


def test_configure_disabled_clears_public_rows(monkeypatch):
    handle = _FakeHandle()
    monkeypatch.setattr("free.public_egress_worker.start_worker_process", lambda upstreams: handle)
    pool = _pool()
    pool.set_public_proxies(_rows(), max_active=1, max_per_24=2)
    pool.configure_public_proxies({"enabled": False})
    assert pool._public_https_proxies == []
    assert pool._public_worker is None


def test_adaptive_health_uses_provider_outcome_log():
    class _Ep:
        provider = "warp"
        _station = -1
        pid = "warp:test"

    pool = _pool()
    pool._note_egress_outcome(_Ep(), 429)
    pool._note_egress_outcome(_Ep(), 200)
    health = pool._adaptive_health(["warp", "proxies"])
    assert health["warp"]["recent_429"] == 1
    assert health["warp"]["ok"] == 1
    assert health["proxies"]["recent_429"] == 0


def test_adaptive_selection_avoids_blocked_provider(monkeypatch):
    pool = _pool()
    pool._egress_providers = ["warp", "nordvpn"]
    pool._egress_strategy = "balance"
    pool._egress_weights = {"warp": 50, "nordvpn": 50}
    pool._egress_adaptive = {
        "enabled": True,
        "min_samples": 3,
        "recent_429_penalty": 0.25,
        "min_weight_pct": 2,
        "window_s": 300,
    }
    monkeypatch.setattr(
        pool,
        "_adaptive_health",
        lambda available: {
            "warp": {"ok": 0, "n429": 10, "recent_429": 10, "blocked_ratio": 1.0},
            "nordvpn": {"ok": 10, "n429": 0, "recent_429": 0, "blocked_ratio": 0.0},
        },
    )
    picks = {pool._select_egress_provider(["warp", "nordvpn"]) for _ in range(20)}
    assert picks == {"nordvpn"}


@pytest.mark.asyncio
async def test_revalidate_public_proxies_drops_dead(monkeypatch, tmp_path):
    monkeypatch.setattr(fp.FreeIPPool, "_PUBLIC_STATUS_PATH", str(tmp_path / "public_status.json"))
    handles = []

    def _start(upstreams):
        handle = _FakeHandle()
        handles.append(handle)
        return handle

    monkeypatch.setattr("free.public_egress_worker.start_worker_process", _start)
    pool = _pool()
    pool.set_public_proxies(_rows(), max_active=2, max_per_24=2)
    eps = [ep for ep in pool._socks5_eps if ep.provider == "public"]
    assert len(eps) == 2
    statuses = {eps[0].pid: 200, eps[1].pid: 0}

    async def _validator(endpoints, *, model, concurrency):
        assert model == "deepseek-v4.1-flash-free"
        assert concurrency >= 1
        return statuses

    pool.set_public_validator(_validator)
    kept = await pool.revalidate_public_proxies()
    assert kept == 1
    assert len(pool._public_https_proxies) == 1
    assert pool._public_https_proxies[0]["egress_ip"] == eps[0].egress_ip
    assert pool._public_https_proxies[0]["last_status"] == 200
    assert pool._public_https_proxies[0]["last_checked_at"] > 0
    status_file = tmp_path / "public_status.json"
    assert status_file.exists()
    assert "last_status" in status_file.read_text(encoding="utf-8")


@pytest.mark.asyncio
async def test_opencode_public_validator_maps_statuses(monkeypatch):
    import opencode as oc

    class _Ep:
        def __init__(self, pid: str):
            self.pid = pid
            self.socks5_url = f"http://{pid}:token@127.0.0.1:9999"

    class _Resp:
        def __init__(self, status: int):
            self.status_code = status

        async def aclose(self) -> None:
            pass

    async def _fake(body, headers, proxy_url=None, station=None, endpoint=None):
        return _Resp(200 if station.pid == "ok" else 429)

    monkeypatch.setattr(oc, "_do_free_request_curl_cffi", _fake)
    result = await oc._revalidate_public_proxies([_Ep("ok"), _Ep("quota")])
    assert result == {"ok": 200, "quota": 429}


@pytest.mark.asyncio
async def test_public_refresh_ticker_starts_and_stops(monkeypatch, tmp_path):
    handle = _FakeHandle()
    monkeypatch.setattr("free.public_egress_worker.start_worker_process", lambda upstreams: handle)
    feed = tmp_path / "feed.jsonl"
    feed.write_text(json.dumps(_rows()[0]) + "\n", encoding="utf-8")
    pool = _pool()
    pool.configure_public_proxies(
        {
            "enabled": True,
            "feed_path": str(feed),
            "max_active": 1,
            "max_per_24": 2,
            "refresh_interval_s": 21600,
        }
    )
    assert pool._public_refresh_task is not None
    await pool.close()
    assert pool._public_refresh_task is None


@pytest.mark.asyncio
async def test_pool_close_stops_public_worker(monkeypatch):
    handle = _FakeHandle()
    monkeypatch.setattr("free.public_egress_worker.start_worker_process", lambda upstreams: handle)
    pool = _pool()
    pool.set_public_proxies(_rows(), max_active=1, max_per_24=2)
    await pool.close()
    assert handle.stopped is True
