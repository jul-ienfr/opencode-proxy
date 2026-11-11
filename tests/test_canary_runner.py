"""End-to-end canary runner tests with a local HTTP dashboard stub."""

from __future__ import annotations

import importlib.util
import json
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent


def _payloads(*, usable=6):
    return {
        "summary": {"enabled": True, "total": 7070},
        "stations": {
            "items": [
                {
                    "station": station,
                    "state": "serving",
                    "client_usable": station <= usable,
                    "reason": "" if station <= usable else "dead",
                }
                for station in range(1, 7)
            ]
        },
        "slo": {
            "six_of_six": usable == 6,
            "stations_usable": usable,
            "stations_total": 6,
            "fleet_ratio_pct": usable / 6 * 100,
            "availability_1h_pct": 99.5,
            "target_pct": 99.0,
        },
        "watchdog": {"heartbeat": {"status": "ok"}, "incidents": []},
        "discovery": {
            "items": [{"status": "ok", "stale": False, "server_count": 7070}]
        },
        "transports": {
            "items": [
                {
                    "transport": name,
                    "attempts": 100,
                    "successes": 95,
                    "degraded": False,
                    "health": "healthy",
                }
                for name in ("wireguard", "openvpn-udp", "openvpn-tcp")
            ]
        },
        "rotations": {"items": [{"result": "committed"}]},
    }


def _server(payloads):
    routes = {
        "/api/egress/qualification/summary": payloads["summary"],
        "/api/egress/qualification/stations": payloads["stations"],
        "/api/egress/qualification/slo": payloads["slo"],
        "/api/egress/qualification/watchdog": payloads["watchdog"],
        "/api/egress/qualification/discovery": payloads["discovery"],
        "/api/egress/qualification/transports": payloads["transports"],
        "/api/egress/qualification/rotations": payloads["rotations"],
    }

    class Handler(BaseHTTPRequestHandler):
        def do_GET(self):
            path = self.path.split("?", 1)[0]
            payload = routes.get(path)
            if payload is None:
                self.send_error(404)
                return
            body = json.dumps(payload).encode("utf-8")
            self.send_response(200)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)

        def log_message(self, *_args):
            pass

    server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    return server, f"http://127.0.0.1:{server.server_port}"


def _runner():
    spec = importlib.util.spec_from_file_location(
        "canary_qualification_runner",
        ROOT / "scripts" / "canary_qualification.py",
    )
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def test_runner_fetches_and_writes_passing_report(tmp_path):
    server, base = _server(_payloads())
    output = tmp_path / "canary.json"
    try:
        code = _runner().main(
            ["--api-base", base, "--output", str(output)]
        )
    finally:
        server.shutdown()
        server.server_close()
    assert code == 0
    report = json.loads(output.read_text(encoding="utf-8"))
    assert report["passed"] is True
    assert report["evidence"]["stations_usable"] == 6
    assert report["api_base"] == base


def test_runner_returns_nonzero_on_incomplete_fleet(tmp_path):
    server, base = _server(_payloads(usable=5))
    output = tmp_path / "canary.json"
    try:
        code = _runner().main(
            ["--api-base", base, "--output", str(output)]
        )
    finally:
        server.shutdown()
        server.server_close()
    assert code == 1
    report = json.loads(output.read_text(encoding="utf-8"))
    assert report["passed"] is False
    assert any(
        item["name"] == "6-stations-usable" and not item["ok"]
        for item in report["checks"]
    )
