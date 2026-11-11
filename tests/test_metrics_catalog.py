"""Metrics: egress catalog gauges."""

from __future__ import annotations

from observability.metrics import render_catalog_section


def test_catalog_metrics_expose_each_provider_status():
    lines = render_catalog_section(
        {
            "public": {"known": 10, "usable": 3, "quota": 4, "unique_blocks": 6},
            "warp": {"known": 12, "usable": 12, "unique_blocks": 12},
        },
        {"exact": 100, "networks": 5},
    )
    text = "\n".join(lines)
    assert 'egress_catalog_options{provider="public",status="usable"} 3' in text
    assert 'egress_catalog_options{provider="public",status="quota"} 4' in text
    assert 'egress_catalog_options{provider="warp",status="unique_blocks"} 12' in text
    assert 'egress_catalog_threat_indicators{kind="exact"} 100' in text
    assert 'egress_catalog_threat_indicators{kind="cidr"} 5' in text


def test_missing_catalog_is_skipped():
    assert render_catalog_section(None) == []
