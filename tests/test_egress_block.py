"""EgressBlock / QuotaObservation 007 : modèles provider-aware."""

from __future__ import annotations

from free.quota_ledger import EgressBlock, QuotaObservation


def test_egress_block_roundtrip():
    block = EgressBlock(
        block="2a09:bac5:32d3:14f0::/64",
        provider="warp",
        first_seen_at=100.0,
        last_seen_at=120.0,
        reset_at=200.0,
        exhausted=True,
        source="observed",
        last_error="FreeUsageLimitError",
        evidence_ref="docs/evidence/x.json",
    )
    restored = EgressBlock.from_dict(block.as_dict())
    assert restored == block


def test_egress_block_invalid_is_none():
    assert EgressBlock.from_dict(None) is None
    assert EgressBlock.from_dict({}) is None
    assert EgressBlock.from_dict({"block": ""}) is None


def test_egress_block_bad_numbers_are_zero():
    block = EgressBlock.from_dict(
        {"block": "104.28.1.2/32", "first_seen_at": "bad", "reset_at": None}
    )
    assert block is not None
    assert block.first_seen_at == 0.0
    assert block.reset_at == 0.0


def test_quota_observation_shape():
    obs = QuotaObservation(
        model="mimo-v2.6-flash-free",
        block="2a09:bac5:32d3:14f0::/64",
        provider="warp",
        status=429,
        error_class="QUOTA_429",
        retry_after_s=1200.0,
        reset_at=200.0,
        observed_at=100.0,
        evidence_ref="docs/evidence/x.json",
    )
    assert obs.as_dict()["provider"] == "warp"
    assert obs.as_dict()["error_class"] == "QUOTA_429"
