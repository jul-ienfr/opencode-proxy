"""traffic_capture — SHIM Phase 2 refonte (compatibilité, ne pas étendre).

Domicile canonique : ``observability.capture`` (déplacement pur, contenu
identique). Ce module re-exporte TOUT (y compris le singleton ``capture`` —
même objet, identité partagée avec ``opencode.py`` / ``dashboard/api.py``)
pour les consommateurs historiques : ``tests/test_traffic_capture.py``,
``tests/test_coverage_boost.py`` (façade gelée ADR-006 §5).

Suppression prévue Phase 9 après preuve de non-usage (``grep``).
"""

from observability.capture import (
    TrafficCapture,
    TrafficCaptureMiddleware,
    _Frame,
    capture,
    hex_dump,
)

__all__ = [
    "TrafficCapture",
    "TrafficCaptureMiddleware",
    "_Frame",
    "capture",
    "hex_dump",
]
