"""023 — chaos / scénario de panne proxy (sentinel rejection + recovery path).

Le plan 023 cite explicitement la panne ``live 1.2.3.4`` (sentinel) comme
incident à rejeter ; ce test verrouille ce comportement et le chemin de
récupération (une IP globale concordante reste acceptée).
"""

from __future__ import annotations

from free import egress_ip


def _obs(*ips: str) -> list[egress_ip.IpObservation]:
    return [
        egress_ip.IpObservation(source=f"src{i}", ip=ip)
        for i, ip in enumerate(ips)
    ]


def test_sentinel_and_private_ips_never_promoted():
    rejected = [
        "1.2.3.4",        # sentinel 023
        "0.0.0.0",
        "127.0.0.1",
        "10.0.0.5",
        "192.168.1.2",
        "172.16.0.9",
        "100.64.0.1",
        "::1",
        "fc00::1",
        "fe80::1",
        "2001:db8::1",
    ]
    for ip in rejected:
        verdict = egress_ip.agree(_obs(ip, ip))
        assert not verdict.ok, ip
        assert verdict.ip == ""


def test_agreed_global_ip_is_accepted():
    verdict = egress_ip.agree(_obs("8.8.8.8", "8.8.8.8"))
    assert verdict.ok is True
    assert verdict.ip == "8.8.8.8"
    assert verdict.reason == "agreed"


def test_discordant_sources_do_not_promote():
    verdict = egress_ip.agree(_obs("8.8.8.8", "1.1.1.1"))
    assert verdict.ok is False
    assert "disagreement" in verdict.reason


def test_sentinel_cannot_override_valid_source():
    # Concordance impossible si l'une des sources renvoie un sentinel.
    verdict = egress_ip.agree(_obs("8.8.8.8", "1.2.3.4"))
    assert verdict.ok is False
