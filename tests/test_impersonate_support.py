"""test_impersonate_support.py — P0/P2 plan safari18_4 (503 trompeur).

Régression : `safari18_4` était listé dans `_KNOWN_IMPERSONATIONS` mais
n'existe ni dans les cibles natives de curl_cffi ni dans son
FingerprintManager. `AsyncSession(impersonate='safari18_4')` se construit
sans erreur — `ImpersonateError("Impersonating safari18_4 is not
supported")` n'est levée qu'au moment du POST — donc tout tirage de ce
profil via `identity_diversity` brûlait 1 essai station et empoisonnait
`_last_tunnel_exc` → 503 « no usable VPN station/tunnel » alors que le
VPN allait bien.

Couvert ici (offline, aucun socket) :
  * T1 : chaque entrée de `_KNOWN_IMPERSONATIONS` est supportée par le
    curl_cffi installé (native OU fingerprint) — casse dès qu'un alias
    meurt après un upgrade curl_cffi.
  * T2 : le pool diversité ne contient que des cibles supportées, ne
    contient plus `safari18_4`, et `_normalize_identity_profiles` rejette
    désormais `safari18_4` (config explicite avec cet alias → fallback).
  * T3 : `safari184` (conservé) reste un porteur TLS valide — les profils
    ne décrivent plus un UA navigateur, seule la cible curl_cffi compte.
"""

import pytest

import vpn_manager as vm

curl_utils = pytest.importorskip(
    "curl_cffi.requests.utils",
    reason="curl_cffi non installé — pas de validation d'empreinte possible",
)
curl_cffi = pytest.importorskip("curl_cffi", reason="curl_cffi non installé")


def _supported(target: str) -> bool:
    """Miroir de la logique d'envoi de curl_cffi (utils.py) : natif OU fingerprint."""
    if curl_utils._is_native_impersonate_target(target):
        return True
    return curl_utils._load_named_fingerprint(target) is not None


class TestKnownTargetsSupported:
    def test_every_known_target_supported_by_installed_curl_cffi(self):
        """T1 — garde-fou upgrade curl_cffi : un alias mort casse ce test
        au lieu de produire des 503 en production."""
        broken = [t for t in sorted(vm._KNOWN_IMPERSONATIONS) if not _supported(t)]
        assert not broken, (
            f"cibles _KNOWN_IMPERSONATIONS non supportées par curl_cffi {curl_cffi.__version__}: {broken}"
        )

    def test_safari18_4_gone(self):
        """Régression directe du 503 observé : l'alias mort est purgé."""
        assert "safari18_4" not in vm._KNOWN_IMPERSONATIONS
        assert "safari184" in vm._KNOWN_IMPERSONATIONS  # la face Safari 18.4 reste


class TestPoolContainsOnlySupported:
    def test_diversity_pool_all_supported(self):
        """T2 — aucun profil générable ne peut lever ImpersonateError."""
        base = vm._normalize_identity_profiles(None)
        pool = vm._build_identity_pool(base, diversity=True, max_profiles=10_000)
        assert len(pool) == len(vm.OFFICIAL_CLIENT_NAMES)
        bad = {p["impersonate"] for p in pool} - {t for t in vm._KNOWN_IMPERSONATIONS if _supported(t)}
        assert not bad, f"profils non supportés dans le pool : {sorted(bad)}"

    def test_normalize_rejects_safari18_4(self):
        """Un config explicite avec l'alias mort retombe sur le défaut,
        jamais sur un profil qui échoue à la requête."""
        base = vm._normalize_identity_profiles([{"impersonate": "safari18_4", "user_agent": None, "extra_headers": {}}])
        assert all(p["impersonate"] != "safari18_4" for p in base)
        assert base[0]["impersonate"] == "chrome131"


class TestSafari184Equivalence:
    def test_safari184_kept_as_valid_carrier(self):
        """T3 — la face Safari 18.4 reste un porteur TLS valide."""
        base = vm._normalize_identity_profiles(
            [{"impersonate": "safari184", "user_agent": None, "extra_headers": {}}]
        )
        assert base[0]["impersonate"] == "safari184"
