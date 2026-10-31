"""test_identity_pool.py — profils d'identité = client OpenCode officiel.

[2026-10-25] Les anciens profils navigateur (UA Chrome/Firefox/Safari,
sec-ch-ua, Accept-Language) ne passent pas la gateway : un profil est
désormais une identité du client officiel v2
(``opencode/<channel>/<version>/<client>``). Le champ ``impersonate`` ne
sert que de PORTEUR TLS curl_cffi — aucun en-tête navigateur n'est émis.

Couvert ici (offline) :
  * diversity=False → profils explicites intacts (rétrocompat stricte)
  * diversity=True → grille des clients officiels (cli/desktop), UA v2,
    zéro en-tête navigateur, ordre déterministe, dédoublonnage, cap
  * normalisation : entrées invalides ignorées, UA navigateur écarté,
    en-têtes navigateur retirés, entrée vide → seed chrome131
"""

import vpn_manager as vm

_OFFICIAL = f"opencode/prod/{vm.OFFICIAL_VERSION}/"


def _official_profile(client: str) -> dict:
    return {"impersonate": "chrome131", "user_agent": f"{_OFFICIAL}{client}", "extra_headers": {}}


def _assert_official_profile(p):
    assert isinstance(p, dict)
    assert p["impersonate"] in vm._KNOWN_IMPERSONATIONS
    assert p["extra_headers"] == {}, "aucun en-tête navigateur ne doit partir"
    ua = p.get("user_agent")
    assert isinstance(ua, str) and ua.startswith(_OFFICIAL), ua


class TestRetroCompat:
    def test_diversity_off_returns_base_untouched(self):
        base = [{"impersonate": "chrome131", "user_agent": None, "extra_headers": {}}]
        pool = vm._build_identity_pool(base, diversity=False, max_profiles=256)
        assert pool == base  # identité exacte, aucune expansion

    def test_diversity_off_multi_profile_kept(self):
        base = [
            {"impersonate": "chrome131", "user_agent": None, "extra_headers": {}},
            {"impersonate": "firefox144", "user_agent": None, "extra_headers": {}},
        ]
        pool = vm._build_identity_pool(base, diversity=False, max_profiles=256)
        assert len(pool) == 2
        assert {p["impersonate"] for p in pool} == {"chrome131", "firefox144"}

    def test_default_seed_single_profile_pins_everyone(self):
        """Seed seul (diversity off) : le len<=1 gate garde profile[0]."""
        base = vm._normalize_identity_profiles(None)
        assert len(base) == 1
        assert base[0]["impersonate"] == "chrome131"


class TestPool:
    def test_pool_is_official_client_grid(self):
        base = vm._normalize_identity_profiles(None)
        pool = vm._build_identity_pool(base, diversity=True, max_profiles=256)
        assert len(pool) == len(vm.OFFICIAL_CLIENT_NAMES)
        assert {p["user_agent"].rsplit("/", 1)[-1] for p in pool} == set(vm.OFFICIAL_CLIENT_NAMES)
        for p in pool:
            _assert_official_profile(p)
            assert p["impersonate"] == "chrome131"  # porteur TLS, pas une face

    def test_explicit_official_profile_kept_and_deduped(self):
        base = vm._normalize_identity_profiles([_official_profile("cli")])
        pool = vm._build_identity_pool(base, diversity=True, max_profiles=256)
        assert len(pool) == len(vm.OFFICIAL_CLIENT_NAMES)
        assert {p["user_agent"] for p in pool} == {
            f"{_OFFICIAL}{c}" for c in vm.OFFICIAL_CLIENT_NAMES
        }

    def test_explicit_custom_profile_kept(self):
        custom = {"impersonate": "firefox144", "user_agent": "custom/1.0", "extra_headers": {"X": "1"}}
        base = vm._normalize_identity_profiles([custom])
        pool = vm._build_identity_pool(base, diversity=True, max_profiles=256)
        assert custom in pool
        assert len(pool) == len(vm.OFFICIAL_CLIENT_NAMES) + 1

    def test_deterministic_order(self):
        base = vm._normalize_identity_profiles(None)
        a = vm._build_identity_pool(list(base), diversity=True, max_profiles=256)
        b = vm._build_identity_pool(list(base), diversity=True, max_profiles=256)
        assert a == b

    def test_cap_clamps_pool(self):
        base = vm._normalize_identity_profiles(None)
        pool = vm._build_identity_pool(base, diversity=True, max_profiles=1)
        assert len(pool) == 1
        assert vm._build_identity_pool(base, diversity=True, max_profiles=10_000) is not None


class TestNormalize:
    def test_invalid_entries_skipped(self):
        base = vm._normalize_identity_profiles(
            [
                {"impersonate": "chrome131", "user_agent": "custom/1.0", "extra_headers": {"X": "1"}},
                {"impersonate": "totally-unknown-target", "user_agent": None, "extra_headers": {}},
                "not-a-dict",
            ]
        )
        assert len(base) == 1
        assert base[0]["impersonate"] == "chrome131"
        assert base[0]["user_agent"] == "custom/1.0"
        assert base[0]["extra_headers"] == {"X": "1"}

    def test_browser_ua_discarded(self):
        """Ancien format : un UA navigateur n'est jamais réémis."""
        base = vm._normalize_identity_profiles(
            [
                {
                    "impersonate": "chrome131",
                    "user_agent": (
                        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
                        "(KHTML, like Gecko) Chrome/131.0.0.0 Safari/537.36"
                    ),
                    "extra_headers": {},
                }
            ]
        )
        assert base[0]["user_agent"] is None

    def test_browser_headers_stripped_custom_kept(self):
        base = vm._normalize_identity_profiles(
            [
                {
                    "impersonate": "firefox144",
                    "user_agent": None,
                    "extra_headers": {
                        "Accept-Language": "de-DE,de;q=0.9",
                        "sec-ch-ua": '"Firefox";v="144"',
                        "Cookie": "session=abc",
                        "User-Agent": "Mozilla/5.0 (X11; Linux x86_64) Firefox/144.0",
                        "X-Custom": "1",
                    },
                }
            ]
        )
        assert base[0]["extra_headers"] == {"X-Custom": "1"}

    def test_empty_input_seeds_default(self):
        assert vm._normalize_identity_profiles([])[0]["impersonate"] == "chrome131"
        assert vm._normalize_identity_profiles(None)[0]["impersonate"] == "chrome131"

    def test_blank_ua_normalized_to_none(self):
        base = vm._normalize_identity_profiles(
            [{"impersonate": "firefox144", "user_agent": "   ", "extra_headers": None}]
        )
        assert base[0]["user_agent"] is None
        assert base[0]["extra_headers"] == {}


class TestOfficialProfileFormat:
    """[2026-10-25] Format cible : {client: cli|desktop} dérive l'UA v2."""

    def test_client_derives_official_ua(self):
        base = vm._normalize_identity_profiles([{"client": "cli"}, {"client": "desktop"}])
        assert [p["user_agent"] for p in base] == [f"{_OFFICIAL}cli", f"{_OFFICIAL}desktop"]
        assert all(p["impersonate"] == "chrome131" for p in base)
        assert all(p["extra_headers"] == {} for p in base)

    def test_client_wins_over_browser_ua_and_headers(self):
        base = vm._normalize_identity_profiles(
            [
                {
                    "client": "cli",
                    "user_agent": "Mozilla/5.0 (X11; Linux x86_64) Firefox/144.0",
                    "extra_headers": {
                        "sec-ch-ua": '"Firefox";v="144"',
                        "Accept-Language": "fr-FR,fr;q=0.9",
                        "X-Keep": "1",
                    },
                }
            ]
        )
        assert base[0]["user_agent"] == f"{_OFFICIAL}cli"
        assert base[0]["extra_headers"] == {"X-Keep": "1"}

    def test_client_with_custom_tls_carrier(self):
        base = vm._normalize_identity_profiles([{"client": "desktop", "impersonate": "safari184"}])
        assert base[0]["impersonate"] == "safari184"
        assert base[0]["user_agent"] == f"{_OFFICIAL}desktop"

    def test_unknown_client_skipped(self):
        assert vm._normalize_identity_profiles([{"client": "app"}])[0]["user_agent"] is None

    def test_profile_without_client_or_carrier_skipped(self):
        assert vm._normalize_identity_profiles([{"user_agent": "custom/1.0"}])[0]["user_agent"] is None


class TestOfficialUA:
    def test_official_ua_per_client(self):
        for client in vm.OFFICIAL_CLIENT_NAMES:
            assert vm._official_ua_for_client(client) == f"{_OFFICIAL}{client}"
            assert vm._OFFICIAL_UA_BY_CLIENT[client] == f"{_OFFICIAL}{client}"

    def test_unknown_client_falls_back_to_cli(self):
        assert vm._official_ua_for_client("nope") == f"{_OFFICIAL}cli"
        assert vm._official_ua_for_client("") == f"{_OFFICIAL}cli"
