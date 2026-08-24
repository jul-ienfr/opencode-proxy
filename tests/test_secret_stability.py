"""[plan v10 §14.0.2] Stabilité des secrets dans config.yaml.

Contrat v9 : le secret en clair est ASSUMÉ ; `save_yaml_config` doit être
idempotent sur `control_api_key` — jamais de régénération à la sauvegarde.
(La génération au chargement n'a lieu QUE si la clé est vide/absente,
garde `if not str(key).strip()` — vérifié vpn §config/settings.py:92.)
"""

import os

import yaml

import config.settings as st


def _read_key(path):
    with open(path, encoding="utf-8") as f:
        data = yaml.safe_load(f) or {}
    ir = data.get("ip_rotation") or {}
    return ir.get("control_api_key")


def test_save_yaml_config_preserves_control_api_key(tmp_path, monkeypatch):
    cfg = tmp_path / "config.yaml"
    cfg.write_text(
        yaml.dump({"ip_rotation": {"control_api_key": "KEY-SHOULD-SURVIVE"}}, sort_keys=False),
        encoding="utf-8",
    )
    monkeypatch.setattr(st, "CONFIG_PATH", str(cfg))
    monkeypatch.setattr(
        st,
        "_yaml_data",
        {"ip_rotation": {"control_api_key": "KEY-SHOULD-SURVIVE"}},
    )
    st.save_yaml_config()
    st.save_yaml_config()  # idempotent
    assert _read_key(str(cfg)) == "KEY-SHOULD-SURVIVE"
    assert os.path.exists(str(cfg) + ".tmp") is False, "atomic replace left no .tmp behind"


def test_save_does_not_inject_or_rotate_missing_key(tmp_path, monkeypatch):
    cfg = tmp_path / "config.yaml"
    monkeypatch.setattr(st, "CONFIG_PATH", str(cfg))
    monkeypatch.setattr(st, "_yaml_data", {"server": {"port": 4000}})
    st.save_yaml_config()
    with open(str(cfg), encoding="utf-8") as f:
        data = yaml.safe_load(f) or {}
    ir = data.get("ip_rotation")
    assert not ir or not str(ir.get("control_api_key") or "").strip(), (
        "save_yaml_config ne génère JAMAIS de clé — seule la garde de chargement le fait"
    )
