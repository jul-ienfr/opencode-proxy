"""[024 T216 / INV-10] Gate de taille : aucun module ne grossit au-delà du
baseline gelé, aucun nouveau module ne dépasse 1 500 lignes.

Le baseline (`scripts/module_size_baseline.json`) fige les 9 monolithes
hérités (`opencode.py` 34 k, `vpn/manager.py` 11 k, …). Un fichier déjà
au-dessus de la borne ne peut plus **grossir** ; un fichier sous la borne
reste libre jusqu'à 1 500 ; un fichier inconnu est jugé sur la borne seule.

Régénérer le baseline est une décision explicite :
`python scripts/module_size_audit.py --write-baseline scripts/module_size_baseline.json`
"""

from __future__ import annotations

from pathlib import Path

from scripts.module_size_audit import (
    DEFAULT_MAX_LINES,
    _load_baseline,
    check,
    scan,
)

ROOT = Path(__file__).resolve().parents[1]
BASELINE = ROOT / "scripts" / "module_size_baseline.json"


def test_the_baseline_exists_and_is_not_empty():
    baseline = _load_baseline(BASELINE)
    assert baseline, "module_size_baseline.json manquant ou vide"
    assert "opencode.py" in baseline


def test_no_module_grows_beyond_the_frozen_baseline():
    sizes = scan(ROOT, DEFAULT_MAX_LINES)
    baseline = _load_baseline(BASELINE)
    violations = check(sizes, max_lines=DEFAULT_MAX_LINES, baseline=baseline)
    assert violations == [], violations


def test_the_gate_sees_a_new_monolith():
    violations = check(
        {"legacy.py": 1600, "brand_new.py": DEFAULT_MAX_LINES + 1},
        max_lines=DEFAULT_MAX_LINES,
        baseline={"legacy.py": 1600},
    )
    assert [v["file"] for v in violations] == ["brand_new.py"]
    assert violations[0]["reason"] == "over-max"


def test_the_gate_sees_a_legacy_file_growth():
    violations = check(
        {"legacy.py": 1601},
        max_lines=DEFAULT_MAX_LINES,
        baseline={"legacy.py": 1600},
    )
    assert violations[0]["reason"] == "grew"
