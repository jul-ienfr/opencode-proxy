"""Constitution XII/XVII — aucune option GUI/config décorative.

Le gate lit `config.yaml`, `contracts/option-map.md` et les pages Svelte.  Il
échoue si une clé n'a pas de consommateur déclaré, si un test référencé
n'existe pas, ou si un contrôle interactif de catalogue n'a pas de contrat.
"""

from __future__ import annotations

import re
from pathlib import Path

import yaml

ROOT = Path(__file__).resolve().parent.parent
CONFIG = ROOT / "config.yaml"
OPTION_MAP = ROOT / "specs" / "013-egress-catalog" / "contracts" / "option-map.md"
PAGES = ROOT / "dashboard-ui" / "src" / "pages"
SCOPED_SECTIONS = ("egress_catalog",)
SCOPED_IP_ROTATION = ("public_proxies", "egress_adaptive")
INTERACTIVE_TAG = re.compile(r"<(button|input|select|textarea)\b[^>]*>", re.IGNORECASE | re.DOTALL)
DATA_OPTION_MAP = re.compile(r'data-option-map="([^"]+)"')
TEST_REF = re.compile(r"^(?P<file>[^:]+\.py)::(?P<name>test_[A-Za-z0-9_]+)$")


def _leaves(value, prefix: str):
    if isinstance(value, dict):
        for key, child in value.items():
            child_prefix = f"{prefix}.{key}" if prefix else str(key)
            yield from _leaves(child, child_prefix)
    elif isinstance(value, list):
        for child in value:
            yield from _leaves(child, f"{prefix}[]")
    else:
        yield prefix


def _config_keys() -> set[str]:
    config = yaml.safe_load(CONFIG.read_text(encoding="utf-8")) or {}
    keys: set[str] = set()
    for section in SCOPED_SECTIONS:
        if section in config:
            keys.update(_leaves(config[section], section))
    rotation = config.get("ip_rotation") if isinstance(config.get("ip_rotation"), dict) else {}
    for section in SCOPED_IP_ROTATION:
        if section in rotation:
            keys.update(_leaves(rotation[section], f"ip_rotation.{section}"))
    return keys


def _rows() -> list[dict[str, str]]:
    headers = ("surface", "selector", "default", "consumer", "effect", "test", "status")
    rows: list[dict[str, str]] = []
    for line in OPTION_MAP.read_text(encoding="utf-8").splitlines():
        if not line.startswith("|"):
            continue
        cells = [cell.strip() for cell in line.strip().strip("|").split("|")]
        if len(cells) != len(headers) or cells[0] in ("Surface", "---"):
            continue
        if cells[0] not in ("config", "GUI"):
            continue
        rows.append(dict(zip(headers, cells)))
    return rows


def _clean(value: str) -> str:
    return value.strip().strip("`").strip()


def _covers(key: str, selector: str) -> bool:
    selector = _clean(selector)
    if selector.startswith("..."):
        return key.endswith(selector[3:])
    if key == selector:
        return True
    # Une liste de scalaires (`denylist`) est aplatie en `denylist[]` :
    # la ligne de contrat décrit la liste, pas chaque valeur.
    return key == f"{selector}[]"


def _assert_test_reference(reference: str) -> None:
    match = TEST_REF.match(_clean(reference))
    assert match, f"référence de test invalide: {reference!r}"
    path = ROOT / "tests" / match.group("file")
    assert path.is_file(), f"test absent: {reference}"
    content = path.read_text(encoding="utf-8", errors="replace")
    pattern = rf"(?:async\s+)?def\s+{re.escape(match.group('name'))}\s*\("
    assert re.search(pattern, content), f"fonction de test absente: {reference}"


def test_every_scoped_config_key_has_a_contract():
    keys = _config_keys()
    rows = [row for row in _rows() if row["surface"] == "config"]
    missing = [
        key
        for key in sorted(keys)
        if not any(
            row["status"] in ("active", "display-only") and _covers(key, row["selector"])
            for row in rows
        )
    ]
    assert missing == [], f"clé(s) sans contrat option-map: {missing}"


def test_every_active_contract_points_to_a_real_test():
    for row in _rows():
        if row["status"] != "active":
            continue
        _assert_test_reference(row["test"])


def _catalog_pages() -> list[Path]:
    pages: list[Path] = []
    for path in PAGES.rglob("*.svelte"):
        text = path.read_text(encoding="utf-8", errors="replace")
        if "data-option-map" in text or "Catalogue egress" in text:
            pages.append(path)
    return pages


def test_gui_controls_have_a_contract():
    rows = [row for row in _rows() if row["surface"] == "GUI"]
    active_selectors = {_clean(row["selector"]) for row in rows if row["status"] == "active"}
    declared_attrs: set[str] = set()
    for page in _catalog_pages():
        text = page.read_text(encoding="utf-8", errors="replace")
        declared_attrs.update(DATA_OPTION_MAP.findall(text))
        for match in INTERACTIVE_TAG.finditer(text):
            tag = match.group(0)
            assert "data-option-map" in tag, (
                f"contrôle interactif sans contrat dans {page.name}: {tag[:120]}"
            )
    assert declared_attrs <= active_selectors, (
        f"contrôle(s) GUI sans ligne option-map: {sorted(declared_attrs - active_selectors)}"
    )
    assert active_selectors <= declared_attrs, (
        f"ligne(s) GUI active(s) sans contrôle réel: {sorted(active_selectors - declared_attrs)}"
    )


def test_no_active_contract_is_planned_in_disguise():
    for row in _rows():
        if row["status"] == "active":
            assert row["consumer"].strip(), row
            assert row["effect"].strip(), row
