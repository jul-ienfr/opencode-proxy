from free import egress_ip as eip


def test_sentinel_and_private_are_rejected():
    for value in ("1.2.3.4", "0.0.0.0", "127.0.0.1", "10.0.0.1", "192.0.2.10", "", "unknown"):
        assert not eip.is_global_ip(value), value
    assert eip.is_global_ip("8.8.8.8")
    assert eip.is_global_ip("2a09:bac1:27c0:2060::1")


def test_two_agreeing_sources_confirm():
    agreement = eip.agree(
        [
            eip.IpObservation("ipify", "8.8.8.8"),
            eip.IpObservation("gluetun", "8.8.8.8"),
        ]
    )
    assert agreement.ok and agreement.ip == "8.8.8.8"


def test_one_source_is_not_enough():
    agreement = eip.agree([eip.IpObservation("ipify", "8.8.8.8")])
    assert not agreement.ok


def test_disagreement_never_adopts():
    agreement = eip.agree(
        [
            eip.IpObservation("ipify", "8.8.8.8"),
            eip.IpObservation("gluetun", "1.1.1.1"),
        ]
    )
    assert not agreement.ok


def test_sentinel_source_cannot_confirm():
    agreement = eip.agree(
        [
            eip.IpObservation("ipify", "8.8.8.8"),
            eip.IpObservation("gluetun", "1.2.3.4"),
        ]
    )
    assert not agreement.ok
