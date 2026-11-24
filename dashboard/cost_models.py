"""[T153] Modèle de coût : `_free_model_id_set`, `_pricing_config`, `_compute_costs`.

Extrait de `dashboard/api.py` — comportement identique, imports lazy/fail-soft
préservés pour éviter les cycles et garder le repli `FREE_MODELS`.
"""

from __future__ import annotations

from typing import Any


def _free_model_id_set() -> set[str]:
    """Ensemble canonique des ids gratuits (tier == free), fail-soft.

    Remplace les heuristiques ``-free`` : ``big-pickle`` est gratuit sans
    suffixe, et un id ``-free`` routé payant reste sémantiquement gratuit.
    Les ids historiques absents du catalogue restent couverts par le suffixe
    dans les appelants.
    """
    try:
        from config.live_models import get_model_entry
        from config.model_tier import classify_model
        from config.settings import (
            FREE_MODEL_MAP,
            FREE_MODELS,
            MODELS,
            get_free_substitution_policy,
            get_model_config,
        )

        policy = get_free_substitution_policy()
        ids = {str(mid) for mid in (FREE_MODELS or ())}
        for mid in MODELS:
            try:
                verdict = classify_model(
                    mid,
                    get_model_config(mid),
                    get_model_entry(mid),
                    free_models=FREE_MODELS,
                    free_model_map=FREE_MODEL_MAP,
                    policy=policy,
                )
            except Exception:
                if str(mid).endswith("-free"):
                    ids.add(str(mid))
                continue
            if verdict.get("tier") == "free":
                ids.add(str(mid))
        return ids
    except Exception:
        try:
            from config.settings import FREE_MODELS

            return {str(mid) for mid in (FREE_MODELS or ())}
        except Exception:
            return set()


def _pricing_config() -> dict:
    try:
        import config.settings as config_settings

        value = config_settings._yaml_data.get("pricing")
        return value if isinstance(value, dict) else {}
    except Exception:
        return {}


def _compute_costs(rows, pricing: dict | None = None) -> dict:
    """[v10 §12.3.10] Coût payant + économies free à partir de lignes agrégées.

    ``rows`` : iterable de dicts {model, tokens_input, tokens_output}.
    ``pricing`` : {"currency", "defaults": {input/output_per_mtok},
    "per_model": {modèle: {...}}} — sinon défauts Anthropic Sonnet-class.

    Les modèles gratuits (tier canonique, pas seulement le suffixe ``-free``)
    ne coûtent rien mais génèrent ``free_saved_usd`` (ce qu'ils auraient coûté
    au tarif par défaut)."""
    p = pricing or {}
    currency = str(p.get("currency", "USD"))
    defaults = p.get("defaults") if isinstance(p.get("defaults"), dict) else {}
    rate_in = float(defaults.get("input_per_mtok", 3.0)) if isinstance(defaults, dict) else 3.0
    rate_out = float(defaults.get("output_per_mtok", 15.0)) if isinstance(defaults, dict) else 15.0
    per_model_cfg = p.get("per_model") if isinstance(p.get("per_model"), dict) else {}
    free_ids = _free_model_id_set()

    paid_usd = 0.0
    saved_usd = 0.0
    per_model_out: list[dict[str, Any]] = []
    seen = set()
    for row in rows:
        model = str(row.get("model") or "?")
        ti = float(row.get("tokens_input") or 0)
        to_ = float(row.get("tokens_output") or 0)
        key = (model, ti, to_)
        if key in seen:
            continue
        seen.add(key)
        rates = per_model_cfg.get(model) if isinstance(per_model_cfg, dict) else {}
        ri = float(rates.get("input_per_mtok", rate_in)) if isinstance(rates, dict) else rate_in
        ro_ = float(rates.get("output_per_mtok", rate_out)) if isinstance(rates, dict) else rate_out
        cost = ti / 1e6 * ri + to_ / 1e6 * ro_
        is_free = model in free_ids or model.endswith("-free")
        if is_free:
            saved_usd += cost
        else:
            paid_usd += cost
        per_model_out.append(
            {
                "model": model,
                "tokens_input": int(ti),
                "tokens_output": int(to_),
                "cost_usd": round(cost, 4),
                "free": is_free,
            }
        )
    per_model_out.sort(key=lambda x: -x["cost_usd"])
    return {
        "currency": currency,
        "paid_usd": round(paid_usd, 4),
        "free_saved_usd": round(saved_usd, 4),
        "per_model": per_model_out,
    }


__all__ = ["_free_model_id_set", "_pricing_config", "_compute_costs"]
