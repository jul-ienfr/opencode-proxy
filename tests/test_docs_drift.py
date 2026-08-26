"""[plan v10 §11.6 v6] test_docs_drift — lit docs/_drift_manifest.json.

Bloquant dès Lot D pour les familles v1 / conversion / clients ; docker /
gluetun / provider passent en warning puis bloquant au Lot 1 (décision §11.6).
Le test ne parse JAMAIS la prose : il exécute les checks déclarés dans le
manifeste machine-lisible (ajouter un check = éditer le JSON, pas le test).
"""

import json
import re
from pathlib import Path

import yaml

ROOT = Path(__file__).resolve().parent.parent
MANIFEST = ROOT / "docs" / "_drift_manifest.json"


def _load_manifest() -> dict:
    assert MANIFEST.exists(), "docs/_drift_manifest.json absent — socle doc Lot D requis"
    return json.loads(MANIFEST.read_text(encoding="utf-8"))


def _doc_path(rel: str) -> Path:
    p = ROOT / rel
    assert p.exists(), f"{rel} manquant (manifeste drift)"
    return p


def _check_doc_must_mention(check: dict) -> list[str]:
    text = _doc_path(check["doc"]).read_text(encoding="utf-8")
    missing = [needle for needle in check["must_contain"] if needle not in text]
    return [f"{check['doc']}: '{n}' absent" for n in missing]


def _check_golden_dir(check: dict) -> list[str]:
    d = _doc_path(check["dir"])
    files = sorted(d.glob("*.json"))
    errors: list[str] = []
    if len(files) < check.get("min_count", 1):
        errors.append(f"{d.name}: {len(files)} fixtures < min_count {check['min_count']}")
    for f in files:
        case = json.loads(f.read_text(encoding="utf-8"))
        for field in check.get("required_fields", []):
            if field not in case:
                errors.append(f"{f.name}: champ '{field}' manquant")
    return errors


def _check_conversion_fns_documented(check: dict) -> list[str]:
    """Chaque fn listée doit exister réellement dans protocol_mapping ET être
    mentionnée dans la doc — lie code ↔ doc dans les deux sens."""
    import protocol_mapping as pm

    errors = _check_doc_must_mention(check)
    text = _doc_path(check["doc"]).read_text(encoding="utf-8")
    for name in check["must_contain"]:
        fn = getattr(pm, name, None)
        if not callable(fn):
            errors.append(f"protocol_mapping.{name} n'existe plus — retirer de la matrice ou restaurer")
        elif f"`{name}`" not in text and name not in text:
            errors.append(f"{check['doc']}: fonction '{name}' non documentée")
    return errors


def _check_config_keys(check: dict) -> list[str]:
    cfg_path = _doc_path(check["config_file"])
    data = yaml.safe_load(cfg_path.read_text(encoding="utf-8")) or {}
    doc_text = _doc_path(check["doc"]).read_text(encoding="utf-8")
    errors: list[str] = []
    for dotted in check["keys"]:
        node: object = data
        for part in dotted.split("."):
            if isinstance(node, dict) and part in node:
                node = node[part]
            else:
                errors.append(f"config.yaml: clé '{dotted}' absente mais documentée")
                node = None
                break
        leaf = dotted.split(".")[-1]
        if leaf not in doc_text:
            errors.append(f"{check['doc']}: clé '{leaf}' non documentée")
    return errors


def _check_clients_doc(check: dict) -> list[str]:
    return _check_doc_must_mention(check)


_CHECKERS = {
    "golden_dir": _check_golden_dir,
    "doc_must_mention": _check_doc_must_mention,
    "conversion_fns_documented": _check_conversion_fns_documented,
    "config_keys_exist_and_documented": _check_config_keys,
    "clients_doc_exists": _check_clients_doc,
}


def test_manifest_valid():
    manifest = _load_manifest()
    assert manifest.get("version") >= 1
    assert set(manifest["bloquant_familles"]) <= {
        "v1",
        "conversion",
        "clients",
        "docker",
        "gluetun",
        "provider",
    }
    for check in manifest["checks"]:
        assert check["type"] in _CHECKERS, f"type de check inconnu: {check['type']}"


def test_drift_bloquant_familles():
    manifest = _load_manifest()
    errors: list[str] = []
    for check in manifest["checks"]:
        if check.get("famille") not in manifest["bloquant_familles"]:
            continue  # familles warning → bloquantes au Lot 1 (§11.6)
        errors.extend(_CHECKERS[check["type"]](check))
    assert not errors, (
        "DRIFT DOC/CODE détecté (bloquant v1/conversion/clients):\n- "
        + "\n- ".join(errors)
    )


def test_no_stale_regex_markers_left():
    """Garde-fou anti-brittle : le manifeste ne doit pas contenir de regex
    ad-hoc contre la prose (décision v6 — checks déclaratifs uniquement)."""
    raw = MANIFEST.read_text(encoding="utf-8")
    assert not re.search(r'"regex"', raw), "utiliser must_contain/keys, pas des regex"
