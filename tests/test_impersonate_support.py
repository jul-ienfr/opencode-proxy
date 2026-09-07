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
  * T3 : `safari184` (conservé) offre exactement la même face que
    l'alias supprimé (même UA curée, mêmes variantes de headers) — la
    suppression ne coûte aucune diversité.
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
            f"cibles _KNOWN_IMPERSONATIONS non supportées par curl_cffi "
            f"{curl_cffi.__version__}: {broken}"
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
        assert len(pool) > 150  # la grille de diversité est intacte
        bad = {p["impersonate"] for p in pool} - {
            t for t in vm._KNOWN_IMPERSONATIONS if _supported(t)
        }
        assert not bad, f"profils non supportés dans le pool diversité : {sorted(bad)}"

    def test_normalize_rejects_safari18_4(self):
        """Un config explicite avec l'alias mort retombe sur le défaut,
        jamais sur un profil qui échoue à la requête."""
        base = vm._normalize_identity_profiles(
            [{"impersonate": "safari18_4", "user_agent": None, "extra_headers": {}}]
        )
        assert all(p["impersonate"] != "safari18_4" for p in base)
        assert base[0]["impersonate"] == "chrome131"


class TestSafari184Equivalence:
    def test_same_curated_ua(self):
        """T3 — même User-Agent curé : aucune diversité perdue."""
        assert vm._UA_BY_IMPERSONATE["safari184"].startswith("Mozilla/5.0")
        assert "Version/18.4" in vm._UA_BY_IMPERSONATE["safari184"]

    def test_same_header_variants(self):
        """Mêmes variantes (5 Accept-Language, pas de client hints)."""
        variants = vm._identity_header_variants("safari184")
        assert len(variants) == 5
        assert all("sec-ch-ua" not in v for v in variants)
        assert len({v["Accept-Language"] for v in variants}) == 5
