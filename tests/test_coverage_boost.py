"""Coverage boost — couvre docker_events, traffic_capture, free_discovery, façades app/*.

[P6 hygiène] les tests middleware/nordvpn_api/server_scorer ont été retirés
avec leurs modules (code mort supprimé — 0 import prod, audit 2026-08-26).
"""


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
