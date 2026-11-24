"""[P3 2026-11-21 — T307] Couverture du module `free/provider_capabilities.py`.

Le module est pur (aucune I/O) et pilote des décisions structurantes :
bucket `/64` pour l'IPv6, éligibilité réserve.
Sans tests, une inversion de `bucket_key()` ferait croire WARP IPv4 utilisable.
"""

from __future__ import annotations

from free import provider_capabilities as pc


def test_every_declared_provider_has_capabilities():
    assert set(pc.PROVIDERS) == set(pc.PROVIDER_NAMES)
    for name in pc.PROVIDER_NAMES:
        caps = pc.capabilities(name)
        assert caps.name == name
        assert caps.health_source
        assert caps.wire_profile == "2.3.0"


def test_unknown_provider_fails_closed_to_direct():
    caps = pc.capabilities("does-not-exist")
    assert caps.name == pc.DIRECT
    assert caps.can_rotate is False
    assert caps.reserve_eligible is False


def test_warp_ipv6_bucket_is_normalized_to_64():
    one = pc.bucket_key(pc.WARP, "2a09:bac1:27c0:2060::1")
    two = pc.bucket_key(pc.WARP, "2a09:bac1:27c0:2060::ffff")
    assert one == two == "2a09:bac1:27c0:2060::/64"


def test_warp_ipv4_bucket_is_shared_not_per_address():
    assert pc.bucket_key(pc.WARP, "203.0.113.7") == "warp-ipv4-shared"
    assert pc.bucket_key(pc.WARP, "203.0.113.8") == "warp-ipv4-shared"


def test_proxies_bucket_is_keyed_by_endpoint():
    assert pc.bucket_key(pc.PROXIES, "203.0.113.7", endpoint="p1") == "proxy:p1"
    assert pc.bucket_key(pc.PROXIES, "203.0.113.7") == "proxy:203.0.113.7"


def test_nordvpn_bucket_is_the_address_itself():
    assert pc.bucket_key(pc.NORDVPN, "203.0.113.7") == "203.0.113.7"


def test_reserve_eligibility_excludes_proxies_and_direct():
    assert pc.capabilities(pc.NORDVPN).reserve_eligible is True
    assert pc.capabilities(pc.WARP).reserve_eligible is True
    assert pc.capabilities(pc.PROXIES).reserve_eligible is False
    assert pc.capabilities(pc.DIRECT).reserve_eligible is False


def test_normalize_ipv6_64_is_idempotent_and_safe_on_garbage():
    assert pc.normalize_ipv6_64("2a09:bac1:27c0:2060::1") == "2a09:bac1:27c0:2060::/64"
    assert pc.normalize_ipv6_64("not-an-ip") == "not-an-ip"
    assert pc.normalize_ipv6_64("") == ""
