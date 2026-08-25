"""[plan v10 §12.3.10] Coût & économies — calcul pur (sans DB ni app)."""

import pytest

from dashboard.api import _compute_costs


def test_paid_and_free_split():
    rows = [
        {"model": "claude-sonnet-4-5", "tokens_input": 1_000_000, "tokens_output": 100_000},
        {"model": "mimo-v2.5-free", "tokens_input": 2_000_000, "tokens_output": 200_000},
    ]
    out = _compute_costs(rows, {"currency": "USD",
                                "defaults": {"input_per_mtok": 3.0, "output_per_mtok": 15.0}})
    # payé : 1M×3 + 0.1M×15 = 3 + 1.5 = 4.5
    assert out["paid_usd"] == pytest.approx(4.5, abs=0.01)
    # économisé : 2M×3 + 0.2M×15 = 6 + 3 = 9
    assert out["free_saved_usd"] == pytest.approx(9.0, abs=0.01)
    assert out["currency"] == "USD"
    assert len(out["per_model"]) == 2
    free_row = next(r for r in out["per_model"] if r["free"])
    assert free_row["model"] == "mimo-v2.5-free"


def test_per_model_rates_override_defaults():
    rows = [{"model": "glm-5.1", "tokens_input": 1_000_000, "tokens_output": 0}]
    out = _compute_costs(rows, {
        "defaults": {"input_per_mtok": 3.0, "output_per_mtok": 15.0},
        "per_model": {"glm-5.1": {"input_per_mtok": 0.6, "output_per_mtok": 2.0}},
    })
    assert out["paid_usd"] == pytest.approx(0.6, abs=0.001)


def test_empty_rows_zero_cost():
    out = _compute_costs([], None)
    assert out["paid_usd"] == 0 and out["free_saved_usd"] == 0


def test_dedup_identical_rows():
    rows = [
        {"model": "x", "tokens_input": 100, "tokens_output": 10},
        {"model": "x", "tokens_input": 100, "tokens_output": 10},
    ]
    out = _compute_costs(rows, {})
    assert len(out["per_model"]) == 1, "doublons dédupliqués"



