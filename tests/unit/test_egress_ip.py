from free import egress_ip as eip


def test_sentinel_and_private_are_rejected():
    for value in ("1.2.3.4", "0.0.0.0", "127.0.0.1", "10.0.0.1", "192.0.2.10", "", "unknown"):
        assert not eip.is_global_ip(value), value
    assert eip.is_global_ip("8.8.8.8")
    assert eip.is_global_ip("2a09:bac1:27c0:2060::1")
