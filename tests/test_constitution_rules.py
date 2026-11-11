"""Constitution 1.2.0 — les règles multiplateforme et silencieuses existent.

Un principe non testé dérive.  Ce test vérifie que les règles XV/XVI/XVII
restent dans la constitution et que leurs gates exécutables existent.
"""

from __future__ import annotations

from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
CONSTITUTION = ROOT / ".specify" / "memory" / "constitution.md"


def test_constitution_declares_version_1_2_0():
    text = CONSTITUTION.read_text(encoding="utf-8")
    assert "**Version** : 1.2.1" in text


def test_constitution_declares_the_new_principles():
    text = CONSTITUTION.read_text(encoding="utf-8")
    for heading in (
        "### XV. Multiplateforme par construction",
        "### XVI. Aucune fenêtre intempestive, exécution silencieuse",
        "### XVII. Fiabilité, performance, qualité, modularité, maintenabilité",
    ):
        assert heading in text
    assert "Windows, Linux et macOS" in text
    assert "CREATE_NO_WINDOW" in text
    assert "core.process.run_hidden" in text


def test_the_constitution_has_executable_gates():
    assert (ROOT / "core" / "process.py").is_file()
    assert (ROOT / "scripts" / "platform_audit.py").is_file()
    assert (ROOT / "tests" / "test_platform_audit.py").is_file()
    assert (ROOT / "tests" / "test_option_map.py").is_file()
