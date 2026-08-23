"""test_identity_pool.py — identity diversity pool (vpn_manager.py).

The plan's "one profile per IP" requirement: with identity_diversity on,
the single chrome131 seed expands into a large deterministic cartesian
pool — for every known impersonation target, a grid of header variants
(chrome/edge x2 sec-ch-ua brand-order permutations x5 Accept-Language;
firefox/safari x5 Accept-Language). user_agent stays None everywhere so
curl_cffi's bundle and the curated _UA_BY_IMPERSONATE map stay the UA
sources.

Covered here (plan "Vérification" section 1, offline):
  * diversity=False → explicit profiles untouched (strict retrocompat)
  * diversity=True → structural expansion: per-target variant counts,
    every target covered, dedup by (impersonate, ua, extra_headers)
  * determinism: same input ⇒ same pool (stable order for the dashboard)
  * user_agent stays None (pool never overrides the UA sources)
  * cap identity_max_profiles (256) — max_profiles is a hard clamp
  * _normalize_identity_profiles: invalid/unknown entries skipped,
    empty input → chrome131 seed (never an empty pool)
  * _UA_BY_IMPERSONATE covers every known target with a non-empty UA

Pure module-level functions — no asyncio, no docker, no state file.
"""

import pytest

import vpn_manager as vm

# Chrome/edge: 2 sec-ch-ua brand-order permutations x 5 Accept-Language.
_CHROME_EDGE_VARIANTS = 10
# firefox/safari: 5 Accept-Language only (no client hints).
_FIREFOX_SAFARI_VARIANTS = 5


def _assert_valid_profile(p):
    assert isinstance(p, dict)
    assert p["impersonate"] in vm._KNOWN_IMPERSONATIONS
    assert p["user_agent"] is None, (
        "pool must never pin a UA — curl_cffi bundle / curated map are the sources"
    )
    assert isinstance(p["extra_headers"], dict)
    if p["extra_headers"]:
        assert "Accept-Language" in p["extra_headers"] or "sec-ch-ua" in p["extra_headers"]
        assert "sec-ch-ua" not in p["extra_headers"] or p["impersonate"].startswith(
            ("chrome", "edge")
        )


class TestRetroCompat:
    def test_diversity_off_returns_base_untouched(self):
        base = [{"impersonate": "chrome131", "user_agent": None, "extra_headers": {}}]
        pool = vm._build_identity_pool(base, diversity=False, max_profiles=256)
        assert pool == base  # exact identity, no expansion

    def test_diversity_off_multi_profile_kept(self):
        base = [
            {"impersonate": "chrome131", "user_agent": None, "extra_headers": {}},
            {"impersonate": "firefox144", "user_agent": None, "extra_headers": {}},
        ]
        pool = vm._build_identity_pool(base, diversity=False, max_profiles=256)
        assert len(pool) == 2
        assert {p["impersonate"] for p in pool} == {"chrome131", "firefox144"}

    def test_default_seed_single_profile_pins_everyone(self):
        """With only a chrome131 seed the len<=1 gate in current_identity
        keeps identity[0] — exactly the pre-plan behavior."""
        base = vm._normalize_identity_profiles(None)
        assert len(base) == 1
        assert base[0]["impersonate"] == "chrome131"


class TestIdentityHeaderVariants:
    def test_chrome_variant_structure(self):
        variants = vm._identity_header_variants("chrome131")
        assert len(variants) == _CHROME_EDGE_VARIANTS
        for v in variants:
            assert "Accept-Language" in v
            assert "sec-ch-ua" in v
            assert '"Google Chrome";v="131"' in v["sec-ch-ua"]  # every order still brands Chrome
        # Both brand orders present (permutation), 5 languages each
        orders = {v["sec-ch-ua"] for v in variants}
        assert len(orders) == 2
        assert len({v["Accept-Language"] for v in variants}) == 5

    def test_edge_target_uses_edge_brand(self):
        variants = vm._identity_header_variants("edge101")
        assert len(variants) == _CHROME_EDGE_VARIANTS
        assert all("Microsoft Edge" in v["sec-ch-ua"] for v in variants)

    def test_firefox_safari_no_client_hints(self):
        for target in ("firefox144", "safari180"):
            variants = vm._identity_header_variants(target)
            assert len(variants) == _FIREFOX_SAFARI_VARIANTS
            assert all("sec-ch-ua" not in v for v in variants)
            assert len({v["Accept-Language"] for v in variants}) == 5


# ── expansion, dedup, determinism, cap ───────────────────────────


class TestPool:
    def test_every_known_target_covered(self):
        base = vm._normalize_identity_profiles(None)  # ["chrome131"]
        pool = vm._build_identity_pool(base, diversity=True, max_profiles=256)
        targets = {p["impersonate"] for p in pool}
        assert targets == set(vm._KNOWN_IMPERSONATIONS)

    def test_pool_size_matches_grid(self):
        base = vm._normalize_identity_profiles(None)
        pool = vm._build_identity_pool(base, diversity=True, max_profiles=256)
        chrome_edge = sum(1 for t in vm._KNOWN_IMPERSONATIONS if t.startswith(("chrome", "edge")))
        ff_safari = len(vm._KNOWN_IMPERSONATIONS) - chrome_edge
        expected = (
            len(base) + chrome_edge * _CHROME_EDGE_VARIANTS + ff_safari * _FIREFOX_SAFARI_VARIANTS
        )
        assert expected > 150  # "quasi-illimité"
        assert len(pool) == expected

    def test_all_profiles_valid_and_deduped(self):
        base = vm._normalize_identity_profiles(None)
        pool = vm._build_identity_pool(base, diversity=True, max_profiles=256)
        for p in pool:
            _assert_valid_profile(p)
        keys = {(p["impersonate"], vm._headers_key(p["extra_headers"])) for p in pool}
        assert len(keys) == len(pool)  # no duplicate fingerprint

    def test_deterministic_order(self):
        base = vm._normalize_identity_profiles(None)
        a = vm._build_identity_pool(list(base), diversity=True, max_profiles=256)
        b = vm._build_identity_pool(list(base), diversity=True, max_profiles=256)
        assert a == b

    def test_cap_clamps_pool(self):
        base = vm._normalize_identity_profiles(None)
        pool = vm._build_identity_pool(base, diversity=True, max_profiles=50)
        assert len(pool) == 50
        assert vm._build_identity_pool(base, diversity=True, max_profiles=10_000) is not None

    def test_cap_one_profile(self):
        """identity_max_profiles disabled → seed only (diversity grid dropped)."""
        base = vm._normalize_identity_profiles(None)
        pool = vm._build_identity_pool(base, diversity=True, max_profiles=1)
        assert len(pool) == 1


# ── normalize ────────────────────────────────────────────────────


class TestNormalize:
    def test_invalid_entries_skipped(self):
        base = vm._normalize_identity_profiles(
            [
                {"impersonate": "chrome131", "user_agent": "UA", "extra_headers": {"X": "1"}},
                {"impersonate": "totally-unknown-target", "user_agent": None, "extra_headers": {}},
                "not-a-dict",
            ]
        )
        assert len(base) == 1
        assert base[0]["impersonate"] == "chrome131"
        assert base[0]["user_agent"] == "UA"
        assert base[0]["extra_headers"] == {"X": "1"}

    def test_empty_input_seeds_default(self):
        assert vm._normalize_identity_profiles([])[0]["impersonate"] == "chrome131"
        assert vm._normalize_identity_profiles(None)[0]["impersonate"] == "chrome131"

    def test_blank_ua_normalized_to_none(self):
        base = vm._normalize_identity_profiles(
            [{"impersonate": "firefox144", "user_agent": "   ", "extra_headers": None}]
        )
        assert base[0]["user_agent"] is None
        assert base[0]["extra_headers"] == {}


# ── curated UA map ───────────────────────────────────────────────


class TestUAByImpersonate:
    def test_every_target_has_desktop_ua(self):
        for target in vm._KNOWN_IMPERSONATIONS:
            ua = vm._UA_BY_IMPERSONATE[target]
            assert ua.startswith("Mozilla/5.0")
            assert len(ua) > 30

    def test_family_specific_markers(self):
        assert "Chrome/" in vm._UA_BY_IMPERSONATE["chrome131"]
        assert "Edg/" in vm._UA_BY_IMPERSONATE["edge101"]
        assert "Firefox/" in vm._UA_BY_IMPERSONATE["firefox144"]
        assert "Safari/" in vm._UA_BY_IMPERSONATE["safari180"]
