"""Tests du classifieur Gratuit / Payant (``config/model_tier.py``).

Aucune I/O, aucun import de ``opencode`` : les règles sont pures et les
signaux injectés. Les cas verrouillent les pièges mesurés sur l'instance
locale : ``big-pickle`` free sans suffixe, mapping explicite ``glm-5.1``
toujours honoré, ``jev-1.13`` alias payant routé sur la base anonyme.
"""

from __future__ import annotations

from config.free_policy import is_free_subject
from config.model_tier import (
    FREE,
    PAID,
    ROUTE_FREE,
    ROUTE_INTERNAL,
    ROUTE_PAID,
    ROUTE_UNKNOWN,
    UNKNOWN,
    catalog_legs_from_entry,
    classify,
    classify_model,
    classify_route_alias,
    route_leg_for_endpoint,
    tier_counts,
)

FREE_EP = "https://opencode.ai/inference/openai/v1/chat/completions"
PAID_EP = "https://opencode.ai/inference/go/openai/v1/chat/completions"
INTERNAL_EP = "https://opencode.ai/inference/v1/systemone"


def test_route_leg_for_endpoint():
    assert route_leg_for_endpoint(FREE_EP) == ROUTE_FREE
    assert route_leg_for_endpoint(PAID_EP) == ROUTE_PAID
    assert route_leg_for_endpoint("https://opencode.ai/inference/go/openai/v1") == ROUTE_PAID
    assert route_leg_for_endpoint(INTERNAL_EP) == ROUTE_FREE
    assert route_leg_for_endpoint("https://example.test/v1") == ROUTE_INTERNAL
    assert route_leg_for_endpoint("") == ROUTE_UNKNOWN


def test_catalog_legs_from_entry_dedupes_and_orders():
    assert catalog_legs_from_entry({"legs": ["opencode", "opencode-go", "opencode"]}) == [
        "free",
        "paid",
    ]
    assert catalog_legs_from_entry({"leg": "opencode"}) == ["free"]
    assert catalog_legs_from_entry({}) == []


def test_suffix_free_is_free_even_if_misrouted_paid():
    verdict = classify("mimo-v2.6-flash-free", endpoint=PAID_EP)
    assert verdict["tier"] == FREE
    assert verdict["tier_source"] == "suffix"
    assert verdict["route_leg"] == ROUTE_PAID
    assert verdict["route_mismatch"] is True


def test_big_pickle_is_free_by_discovery_without_suffix():
    verdict = classify(
        "big-pickle",
        endpoint=FREE_EP,
        free_models={"big-pickle"},
        catalog_legs=["free"],
        free_verified=True,
    )
    assert verdict["tier"] == FREE
    assert verdict["tier_source"] == "discovery"
    assert verdict["free_first"] is True
    assert verdict["free_target"] is None
    assert verdict["catalog_mismatch"] is False


def test_verified_free_stays_free_even_misrouted_paid():
    verdict = classify(
        "big-pickle",
        endpoint=PAID_EP,
        free_models={"big-pickle"},
        free_verified=True,
    )
    assert verdict["tier"] == FREE
    assert verdict["tier_source"] == "discovery"
    assert verdict["route_mismatch"] is True


def test_free_endpoint_without_registry_proof_stays_paid():
    verdict = classify(
        "gpt-5.5",
        endpoint=FREE_EP,
        free_models=set(),
        catalog_legs=["free"],
    )
    assert verdict["tier"] == PAID
    assert verdict["tier_source"] == "unproven"
    assert verdict["route_leg"] == ROUTE_FREE
    assert verdict["free_first"] is False
    assert verdict["free_target"] is None
    assert verdict["route_mismatch"] is False


def test_probed_registry_member_is_free_even_on_free_endpoint():
    verdict = classify(
        "future-model",
        endpoint=FREE_EP,
        free_models={"future-model"},
        catalog_legs=["free"],
    )
    assert verdict["tier"] == FREE
    assert verdict["tier_source"] == "discovery"
    assert verdict["route_leg"] == ROUTE_FREE
    assert verdict["free_first"] is True
    assert verdict["catalog_mismatch"] is False


def test_probed_registry_member_with_paid_endpoint_is_free_and_mismatched():
    verdict = classify(
        "future-model",
        endpoint=PAID_EP,
        free_models={"future-model"},
        catalog_legs=["free"],
    )
    assert verdict["tier"] == FREE
    assert verdict["tier_source"] == "discovery"
    assert verdict["route_leg"] == ROUTE_PAID
    assert verdict["route_mismatch"] is True
    assert verdict["free_first"] is True
    assert verdict["catalog_mismatch"] is True


def test_active_homonym_mapping_stays_paid_with_free_target():
    verdict = classify(
        "mimo-v2.6-flash",
        endpoint=PAID_EP,
        free_model_map={"mimo-v2.6-flash": "mimo-v2.6-flash-free"},
        policy="homonym",
    )
    assert verdict["tier"] == PAID
    assert verdict["tier_source"] == "mapped"
    assert verdict["free_first"] is True
    assert verdict["free_target"] == "mimo-v2.6-flash-free"
    assert verdict["free_mapping_active"] is True


def test_explicit_mapping_is_always_active_not_hidden_by_policy():
    """Un mapping explicite est souverain, même hors homonyme.

    Régression 2026-10-29 : la politique ``homonym`` filtrait
    ``glm-5.1 → glm-5.3-flash-free`` ; les alias Claude partaient payant et
    rendaient 401 avec une clé morte alors que le free était configuré.
    """
    verdict = classify(
        "glm-5.1",
        endpoint=PAID_EP,
        free_model_map={"glm-5.1": "glm-5.3-flash-free"},
        policy="homonym",
    )
    assert verdict["tier"] == PAID
    assert verdict["tier_source"] == "mapped"
    assert verdict["free_first"] is True
    assert verdict["free_target"] == "glm-5.3-flash-free"
    assert verdict["free_mapping"] == "glm-5.3-flash-free"
    assert verdict["free_mapping_active"] is True


def test_family_policy_activates_the_same_mapping():
    verdict = classify(
        "glm-5.1",
        endpoint=PAID_EP,
        free_model_map={"glm-5.1": "glm-5.3-flash-free"},
        policy="family",
    )
    assert verdict["tier"] == PAID
    assert verdict["tier_source"] == "mapped"
    assert verdict["free_first"] is True
    assert verdict["free_target"] == "glm-5.3-flash-free"


def test_paid_alias_on_free_route_is_flagged():
    verdict = classify(
        "jev-1.13",
        endpoint=INTERNAL_EP,
        free_model_map={"jev-1.13": "jev-1.13-free"},
        policy="homonym",
    )
    assert verdict["tier"] == PAID
    assert verdict["tier_source"] == "mapped"
    assert verdict["route_leg"] == ROUTE_FREE
    assert verdict["route_mismatch"] is True
    assert verdict["free_target"] == "jev-1.13-free"


def test_catalog_is_only_a_fallback():
    assert classify("x", endpoint="", catalog_legs=["free"])["tier"] == FREE
    assert classify("x", endpoint="", catalog_legs=["paid"])["tier"] == PAID
    assert classify("x", endpoint="", catalog_legs=["free", "paid"])["tier"] == PAID


def test_catalog_mismatch_is_informational():
    verdict = classify("claude-opus-5-5", endpoint=PAID_EP, catalog_legs=["free"])
    assert verdict["tier"] == PAID
    assert verdict["route_leg"] == ROUTE_PAID
    assert verdict["catalog_mismatch"] is True
    assert verdict["route_mismatch"] is False


def test_internal_endpoint_without_marker_is_unknown():
    verdict = classify("custom-model", endpoint="https://example.test/v1/chat/completions")
    assert verdict["tier"] == UNKNOWN
    assert verdict["tier_source"] == "unknown"
    assert verdict["route_leg"] == ROUTE_INTERNAL
    assert verdict["free_first"] is False


def test_classify_model_reads_live_entry_legs():
    verdict = classify_model(
        "mimo-v2.6-flash",
        {"endpoint": PAID_EP},
        {"legs": ["opencode"]},
        free_model_map={"mimo-v2.6-flash": "mimo-v2.6-flash-free"},
        policy="homonym",
    )
    assert verdict["catalog_legs"] == ["free"]
    assert verdict["catalog_mismatch"] is True


def test_route_alias_to_free_target_is_paid_with_free_substitute():
    verdict = classify_route_alias(
        "glm-5.1",
        "glm-5.3-flash-free",
        cfg={"endpoint": PAID_EP},
        target_cfg={"endpoint": FREE_EP},
        free_models={"glm-5.3-flash-free"},
    )
    assert verdict["tier"] == PAID
    assert verdict["tier_source"] == "route"
    assert verdict["route_target"] == "glm-5.3-flash-free"
    assert verdict["free_target"] == "glm-5.3-flash-free"
    assert verdict["free_first"] is True
    assert verdict["route_leg"] == ROUTE_PAID


def test_route_alias_to_paid_target_keeps_target_verdict():
    verdict = classify_route_alias(
        "claude-opus-5-5",
        "mimo-v2.6-flash",
        cfg={"endpoint": PAID_EP},
        target_cfg={"endpoint": PAID_EP},
        free_model_map={"mimo-v2.6-flash": "mimo-v2.6-flash-free"},
    )
    assert verdict["tier"] == PAID
    assert verdict["tier_source"] == "route"
    assert verdict["route_target"] == "mimo-v2.6-flash"
    assert verdict["free_target"] == "mimo-v2.6-flash-free"


def test_tier_counts_exposes_the_gui_categories():
    rows = [
        {"tier": FREE, "free_mapping_active": False, "free_mapping": None},
        {"tier": PAID, "free_mapping_active": True, "free_mapping": "a-free"},
        {"tier": PAID, "free_mapping_active": False, "free_mapping": "b-free"},
        {"tier": UNKNOWN, "free_mapping_active": False, "free_mapping": None},
    ]
    counts = tier_counts(rows)
    assert counts["total"] == 4
    assert counts["free"] == 1
    assert counts["paid"] == 2
    assert counts["paid_free_fallback"] == 1
    assert counts["paid_free_ignored"] == 1
    assert counts["unknown"] == 1


def test_is_free_subject_matches_routing_semantics():
    free_map = {
        "mimo-v2.6-flash": "mimo-v2.6-flash-free",
        "glm-5.1": "glm-5.3-flash-free",
        "jev-1.13-free": "jev-1.13-free",
    }
    free_models = {"big-pickle"}
    assert is_free_subject("mimo-v2.6-flash-free", free_models, free_map)
    assert is_free_subject("big-pickle", free_models, free_map)
    assert is_free_subject("mimo-v2.6-flash", free_models, free_map)
    assert is_free_subject("jev-1.13-free", free_models, free_map)
    # Mapping explicite : souverain quel que soit le mode.
    assert is_free_subject("glm-5.1", free_models, free_map)
    assert not is_free_subject("claude-opus-5-5", free_models, free_map)
    assert is_free_subject("glm-5.1", free_models, free_map, policy="family")
