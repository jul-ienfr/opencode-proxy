"""Tests unitaires de scripts/bench_proxy.py (023 T068).

Les mesures reseau reelles ne sont pas testees ici : on couvre les helpers
purs (percentiles, agregation, surcout, taux de succes, validation URL, CLI).
"""

from __future__ import annotations

import importlib.util
import pathlib
import sys

import pytest

ROOT = pathlib.Path(__file__).resolve().parent.parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

_SPEC = importlib.util.spec_from_file_location(
    "bench_proxy", ROOT / "scripts" / "bench_proxy.py"
)
assert _SPEC and _SPEC.loader
bench = importlib.util.module_from_spec(_SPEC)
_SPEC.loader.exec_module(bench)


class TestPercentile:
    def test_empty_returns_zero(self):
        assert bench.percentile([], 50) == 0.0

    def test_single_value(self):
        assert bench.percentile([42.0], 50) == 42.0
        assert bench.percentile([42.0], 95) == 42.0

    def test_known_values(self):
        values = [1.0, 2.0, 3.0, 4.0, 5.0]
        assert bench.percentile(values, 0) == 1.0
        assert bench.percentile(values, 50) == 3.0
        assert bench.percentile(values, 95) == 5.0
        assert bench.percentile(values, 100) == 5.0

    def test_unsorted_input(self):
        assert bench.percentile([5.0, 1.0, 3.0], 50) == 3.0

    def test_rejects_pct_out_of_range(self):
        with pytest.raises(ValueError, match="hors"):
            bench.percentile([1.0], -1)
        with pytest.raises(ValueError, match="hors"):
            bench.percentile([1.0], 101)
        with pytest.raises(ValueError, match="hors"):
            bench.percentile([1.0], "50")  # type: ignore[arg-type]


class TestAggregateLatencies:
    def test_empty_has_no_fake_zero(self):
        agg = bench.aggregate_latencies([])
        assert agg["count"] == 0
        for key in ("min_ms", "mean_ms", "p50_ms", "p90_ms", "p95_ms", "p99_ms", "max_ms"):
            assert agg[key] is None

    def test_single_sample(self):
        agg = bench.aggregate_latencies([12.5])
        assert agg["count"] == 1
        assert agg["min_ms"] == 12.5
        assert agg["max_ms"] == 12.5
        assert agg["p50_ms"] == 12.5

    def test_multiple_samples(self):
        agg = bench.aggregate_latencies([10.0, 20.0, 30.0, 40.0, 50.0])
        assert agg["count"] == 5
        assert agg["min_ms"] == 10.0
        assert agg["max_ms"] == 50.0
        assert agg["mean_ms"] == 30.0
        assert agg["p50_ms"] == 30.0


class TestProxyOverhead:
    def test_no_direct_measure_returns_none(self):
        over = bench.proxy_overhead([10.0, 20.0], [])
        assert over["p50_proxy_ms"] == 10.0
        assert over["p50_direct_ms"] is None
        assert over["overhead_p50_ms"] is None
        assert over["overhead_p95_ms"] is None

    def test_no_proxy_measure_returns_none(self):
        over = bench.proxy_overhead([], [10.0])
        assert over["p50_proxy_ms"] is None
        assert over["overhead_p50_ms"] is None

    def test_computes_overhead(self):
        proxy = [30.0, 40.0, 50.0, 60.0, 70.0]
        direct = [10.0, 15.0, 20.0, 25.0, 30.0]
        over = bench.proxy_overhead(proxy, direct)
        assert over["p50_proxy_ms"] == 50.0
        assert over["p50_direct_ms"] == 20.0
        assert over["overhead_p50_ms"] == 30.0
        assert over["overhead_p95_ms"] is not None

    def test_negative_overhead_is_reported(self):
        # direct plus lent que proxy: on rapporte la difference reelle.
        over = bench.proxy_overhead([10.0], [20.0])
        assert over["overhead_p50_ms"] == -10.0


class TestSuccessRate:
    def test_total_zero(self):
        assert bench.success_rate(0, 0) == 0.0

    def test_nominal(self):
        assert bench.success_rate(75, 100) == 0.75

    def test_rejects_invalid(self):
        with pytest.raises(ValueError):
            bench.success_rate(-1, 10)
        with pytest.raises(ValueError):
            bench.success_rate(1, -1)
        with pytest.raises(ValueError):
            bench.success_rate(11, 10)


class TestUrlHelpers:
    def test_validate_url_accepts_http(self):
        assert bench.validate_url("http://localhost:4000") == "http://localhost:4000"
        assert bench.validate_url(" https://example.test ") == "https://example.test"

    def test_validate_url_rejects_bad(self):
        for bad in ["", "   ", "ftp://x", "http:///path", "not-a-url"]:
            with pytest.raises(ValueError):
                bench.validate_url(bad)

    def test_endpoint_url(self):
        assert bench.endpoint_url("http://localhost:4000", "/v1/messages") == (
            "http://localhost:4000/v1/messages"
        )
        assert bench.endpoint_url("http://localhost:4000/", "/v1/messages") == (
            "http://localhost:4000/v1/messages"
        )
        assert bench.endpoint_url(
            "http://localhost:4000/v1/messages", "/v1/messages"
        ) == "http://localhost:4000/v1/messages"

    def test_endpoint_url_rejects_relative_path(self):
        with pytest.raises(ValueError):
            bench.endpoint_url("http://localhost:4000", "v1/messages")

    def test_url_port(self):
        assert bench.url_port("http://localhost:4000") == 4000
        assert bench.url_port("http://localhost") == 80
        assert bench.url_port("https://example.test") == 443
        assert bench.url_port("ftp://x") is None


class TestPayloadAndHeaders:
    def test_build_payload(self):
        p = bench.build_payload("deepseek-v4.1-flash", stream=True, max_tokens=1)
        assert p["model"] == "deepseek-v4.1-flash"
        assert p["stream"] is True
        assert p["max_tokens"] == 1
        assert p["messages"][0]["role"] == "user"

    def test_build_headers_stream_accept(self):
        h = bench.build_headers("k", stream=True)
        assert h["accept"] == "text/event-stream"
        assert h["x-api-key"] == "k"
        assert h["authorization"] == "Bearer k"
        h2 = bench.build_headers("k", stream=False)
        assert h2["accept"] == "application/json"


class TestFormatTable:
    def test_format_table_handles_missing(self):
        text = bench.format_table(
            {
                "generated_at": "2026-11-22T00:00:00+00:00",
                "base_url": "http://localhost:4000",
                "direct_url": None,
                "path": "/v1/messages",
                "model": "m",
                "config": {"concurrency": 10},
                "non_stream": {
                    "proxy": {
                        "attempted": 0,
                        "success_rate": 0.0,
                        "latency": {"p50_ms": None, "p95_ms": None},
                    },
                    "direct": None,
                    "overhead": {"overhead_p50_ms": None, "overhead_p95_ms": None},
                },
                "sse": {"ttfb": {"count": 0, "p50_ms": None, "p95_ms": None}},
                "streams": {
                    "success": 0,
                    "total": 100,
                    "success_rate": 0.0,
                    "ttfb": {"p50_ms": None, "p95_ms": None},
                },
                "rss": {"available": False, "pid": None, "peak_mb": None},
            }
        )
        assert "n/a" in text
        assert "0/100" in text
        assert "RSS : null" in text

    def test_format_table_reports_values(self):
        text = bench.format_table(
            {
                "generated_at": "now",
                "base_url": "http://localhost:4000",
                "direct_url": "http://direct:1",
                "path": "/v1/messages",
                "model": "m",
                "config": {"concurrency": 2},
                "non_stream": {
                    "proxy": {
                        "attempted": 2,
                        "success_rate": 1.0,
                        "latency": {"p50_ms": 12.3, "p95_ms": 20.0},
                    },
                    "direct": {
                        "success_rate": 1.0,
                        "latency": {"p50_ms": 10.0, "p95_ms": 18.0},
                    },
                    "overhead": {"overhead_p50_ms": 2.3, "overhead_p95_ms": 2.0},
                },
                "sse": {"ttfb": {"count": 2, "p50_ms": 5.0, "p95_ms": 6.0}},
                "streams": {
                    "success": 100,
                    "total": 100,
                    "success_rate": 1.0,
                    "ttfb": {"p50_ms": 7.0, "p95_ms": 9.0},
                },
                "rss": {"available": True, "pid": 123, "peak_mb": 88.5},
            }
        )
        assert "12.3 ms" in text
        assert "100/100" in text
        assert "88.5 ms" in text
        assert "pid 123" in text


class TestParseArgs:
    def test_defaults(self):
        args = bench.parse_args([])
        assert args.base_url == "http://localhost:4000"
        assert args.direct_url is None
        assert args.requests == 30
        assert args.concurrency == 10
        assert args.streams == 100

    def test_rejects_invalid_numbers(self):
        with pytest.raises(SystemExit):
            bench.parse_args(["--requests", "0"])
        with pytest.raises(SystemExit):
            bench.parse_args(["--concurrency", "0"])
        with pytest.raises(SystemExit):
            bench.parse_args(["--streams", "0"])
        with pytest.raises(SystemExit):
            bench.parse_args(["--timeout", "0"])
        with pytest.raises(SystemExit):
            bench.parse_args(["--max-tokens", "0"])
        with pytest.raises(SystemExit):
            bench.parse_args(["--rss-pid", "0"])

    def test_rejects_bad_url(self):
        with pytest.raises(SystemExit):
            bench.parse_args(["--base-url", "ftp://bad"])
        with pytest.raises(SystemExit):
            bench.parse_args(["--direct-url", "not-a-url"])

    def test_accepts_direct_url(self):
        args = bench.parse_args(["--direct-url", "http://127.0.0.1:9999"])
        assert args.direct_url == "http://127.0.0.1:9999"


class TestPhaseSummary:
    def test_counts_successes_and_errors(self):
        summary = bench._phase_summary(
            [
                {"status": 200, "latency_ms": 10.0, "error": None},
                {"status": 500, "latency_ms": 20.0, "error": None},
                {"status": None, "latency_ms": None, "error": "ConnectError: boom"},
            ],
            attempted=3,
        )
        assert summary["attempted"] == 3
        assert summary["responses"] == 2
        assert summary["success"] == 1
        assert summary["success_rate"] == 0.3333
        assert summary["errors"] == 1
        assert summary["status_counts"] == {"200": 1, "500": 1}
        assert summary["latency"]["count"] == 1
        assert summary["latency"]["p50_ms"] == 10.0
