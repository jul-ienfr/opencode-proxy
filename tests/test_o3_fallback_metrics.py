"""[Étape 2 — O3] Compteurs fallback/failover par cause + exposition Prometheus.

Contrats :
  - `_log_fallback` incrémente `proxy_fallback_total{leg,cause}` avec une cause
    bornée (`quota_429` / `payload_400` / `upstream_5xx` / `tunnel_vide` /
    `other`) — bucketisation via `_fallback_cause` ;
  - les sites failover paid (`_do_request_with_retry` 429/401/403,
    `_handle_429` stream) incrémentent `proxy_failover_total{leg,cause,outcome}`
    (`alt_key` vs `guard_skip` datapolicy) — testé via `_bump_*` direct
    (les sites eux-mêmes exigent un upstream mocké) ;
  - `_build_metrics_text` expose les 2 familles en fail-soft (zéro baseline
    pour quota_429, jamais de raise) ;
  - logging-only : aucun effet routage (counters = registres mémoire).
"""

import opencode as oc


def _fresh():
    oc._reset_fallback_metrics()


def test_fallback_cause_bucketisation():
    _fresh()
    assert oc._fallback_cause(429) == "quota_429"
    assert oc._fallback_cause(400) == "payload_400"
    assert oc._fallback_cause(502) == "upstream_5xx"
    assert oc._fallback_cause(503) == "upstream_5xx"
    assert oc._fallback_cause(None) == "tunnel_vide"
    assert oc._fallback_cause("") == "tunnel_vide"
    assert oc._fallback_cause(0) == "tunnel_vide"
    assert oc._fallback_cause(403) == "other"


def test_log_fallback_incremente_compteur_par_cause():
    _fresh()
    oc._log_fallback("req-o3a", "free→paid", "m-free", 429, "m-paid")
    oc._log_fallback("req-o3b", "free→paid", "m-free", 429, "m-paid")
    oc._log_fallback("req-o3c", "free→paid", "m-free", 400, "m-paid")
    assert oc._FB_FALLBACK_COUNTS.get(("free_to_paid", "quota_429")) == 2
    assert oc._FB_FALLBACK_COUNTS.get(("free_to_paid", "payload_400")) == 1


def test_bump_failover_outcomes():
    _fresh()
    oc._bump_failover_counter("paid", "quota_429", "alt_key")
    oc._bump_failover_counter("paid", "datapolicy_403", "guard_skip")
    oc._bump_failover_counter("paid", "datapolicy_403", "guard_skip")
    assert oc._FB_FAILOVER_COUNTS[("paid", "quota_429", "alt_key")] == 1
    assert oc._FB_FAILOVER_COUNTS[("paid", "datapolicy_403", "guard_skip")] == 2


def test_metrics_text_expose_familles_o3_avec_baseline_zero():
    _fresh()
    oc._bump_fallback_counter("free_to_paid", "payload_400")
    txt = oc._build_metrics_text()
    assert "proxy_fallback_total" in txt
    assert "proxy_failover_total" in txt
    assert "# TYPE proxy_fallback_total counter" in txt
    assert "# TYPE proxy_failover_total counter" in txt
    # Baseline zéro : la série quota_429 existe même sans hit.
    assert 'proxy_fallback_total{leg="free_to_paid",cause="quota_429"} 0' in txt
    assert 'proxy_fallback_total{leg="free_to_paid",cause="payload_400"} 1' in txt


def test_metrics_o3_fail_soft_registres_absents(monkeypatch):
    """Registres O3 sabotés → /metrics reste servable (familles VPN intactes)."""
    _fresh()
    monkeypatch.setattr(oc, "_FB_FALLBACK_COUNTS", None)
    monkeypatch.setattr(oc, "_FB_FAILOVER_COUNTS", None)
    txt = oc._build_metrics_text()
    assert isinstance(txt, str) and txt.endswith("\n")
