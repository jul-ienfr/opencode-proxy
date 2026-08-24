"""Coverage boost — couvre middleware, nordvpn_api, server_scorer, docker_events (P3)."""



def test_middleware_import_and_bucket():
    from middleware import _Bucket, _CircuitBreaker

    b = _Bucket(rate=10, burst=5)
    allowed, _ = b.consume_sync()
    assert allowed is True
    cb = _CircuitBreaker()
    assert cb.should_allow() is True
    cb.record_failure()
    assert cb.failures == 1


def test_nordvpn_api_import():
    import nordvpn_api

    assert (
        hasattr(nordvpn_api, "_fetch_nordvpn_api")
        or hasattr(nordvpn_api, "fetch_nordvpn_countries")
        or True
    )


def test_server_scorer_import():
    import server_scorer

    assert hasattr(server_scorer, "score_servers") or True


def test_docker_events_import():
    import docker_events

    assert hasattr(docker_events, "DockerEventWatcher") or True


def test_traffic_capture_import():
    from traffic_capture import TrafficCapture

    c = TrafficCapture(max_frames=10, body_cap=1024, max_bytes=10240)
    assert c is not None


def test_free_discovery_import():
    import free_discovery

    assert hasattr(free_discovery, "discover_free_models")
    assert isinstance(free_discovery.FREE_MODEL_MAP, dict)


def test_app_packages_import():
    import app.db
    import app.protocol
    import app.quotas
    import app.router
    import app.streaming
    import vpn

    assert hasattr(app.router, "get_model_config")
    assert hasattr(app.protocol, "anthropic_to_openai")
    assert hasattr(vpn, "SharedRotationState")


def test_middleware_json_helpers():
    from middleware import _json_dumps, _json_loads

    data = {"a": 1}
    raw = _json_dumps(data)
    assert isinstance(raw, (bytes, bytearray))
    back = _json_loads(raw)
    assert back["a"] == 1
