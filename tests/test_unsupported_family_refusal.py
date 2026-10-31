"""Gemini native family is routed (no longer a 501 unsupported family).

[audit 2026-10-30] The catalogue exposed ``google/v1beta`` models with no
converter; they used to be refused with 501. The proxy now has a native
adapter (``app.protocol.gemini``) and routes them via Chat as an intermediate.
These tests lock the routing contract; the conversion itself is covered by
``tests/test_gemini_adapter.py``.
"""

import pytest

from config import settings as cs


class TestCatalogGemini:
    def test_gemini_is_not_an_unsupported_family(self):
        gemini = {
            model: family
            for model, family in (cs.CATALOG_UNSUPPORTED or {}).items()
            if family == "gemini"
        }
        assert gemini == {}, f"gemini still marked unsupported: {gemini}"

    def test_gemini_catalogue_entries_carry_a_protocol(self):
        found = 0
        for rows in (cs.CATALOG_ROUTES or {}).values():
            for route in (rows or {}).values():
                if isinstance(route, dict) and route.get("family") == "gemini":
                    found += 1
                    assert route.get("protocol") == "gemini"
                    assert "/google/" in str(route.get("base_url") or "")
        assert found > 0, "aucune entrée gemini dans le catalogue chargé"

    def test_known_gemini_ids_are_routable(self):
        import opencode as oc

        for model in ("gemini-3-flash", "gemini-3.8-flash"):
            route = oc._route_for(model)
            assert route and route["model"] == model
            cfg = oc.get_model_config(route["model"])
            assert cfg["protocol"] == "gemini"
            assert "/google/" in cfg["endpoint"]

    def test_a_normal_model_still_routes(self):
        import opencode as oc

        route = oc._route_for("claude-sonnet-4-5")
        assert route, "un modele convertible ne doit pas etre refuse"


@pytest.mark.parametrize("model", ["gpt-6-luna", "claude-opus-4-1"])
def test_non_gemini_models_keep_their_protocol(model):
    import opencode as oc

    route = oc._route_for(model)
    assert route
    cfg = oc.get_model_config(route["model"])
    assert cfg["protocol"] != "gemini"
