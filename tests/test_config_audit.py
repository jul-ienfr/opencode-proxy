"""V7.2 — une clé de configuration inerte est un mensonge silencieux.

**Le cas trouvé en écrivant cet audit.** `dashboard_trust.require_token` existait
dans `config.yaml` et n'était lu **par personne** : le code ne consultait que la
variable d'environnement `DASHBOARD_REQUIRE_TOKEN`. Un utilisateur qui réglait ce
booléen croyait exiger un token sur son dashboard — rien ne se passait. Un réglage
de sécurité ignoré en silence est pire qu'un réglage absent : on ne cherche pas
ce qui ne se plaint jamais.

**Ce que ces tests exigent.**

* l'audit ne trouve **aucune** clé morte ni clé lue mais absente (référence figée) ;
* il **échoue** quand une clé morte apparaît (un garde qu'on ne voit pas échouer
  n'est pas un garde) ;
* `dashboard_trust.require_token` est réellement appliqué, avec la précédence
  environnement > YAML.
"""

from __future__ import annotations

import importlib.util
import json
import pathlib
import sys

import pytest

ROOT = pathlib.Path(__file__).resolve().parent.parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

_SPEC = importlib.util.spec_from_file_location(
    "config_audit", ROOT / "scripts" / "config_audit.py"
)
audit = importlib.util.module_from_spec(_SPEC)
assert _SPEC and _SPEC.loader
_SPEC.loader.exec_module(audit)


class TestTheAuditIsCleanRightNow:
    def test_no_dead_keys(self):
        resultat = audit.auditer()
        assert resultat["dead"] == [], (
            "clé(s) de config jamais lue(s) : soit un vestige à supprimer, soit une "
            f"faute de frappe à corriger → {resultat['dead']}"
        )

    def test_no_keys_read_but_absent_from_the_file(self):
        resultat = audit.auditer()
        assert resultat["missing"] == [], (
            "clé(s) lues par yaml_get mais absentes de config.yaml : le défaut "
            f"s'applique et l'utilisateur croit régler quelque chose → {resultat['missing']}"
        )

    def test_the_audit_actually_covers_the_config(self):
        """Un audit qui ne voit rien passerait les tests ci-dessus sans rien faire."""
        resultat = audit.auditer()
        assert resultat["total_cles"] > 150
        assert resultat["cles_lues_par_yaml_get"] > 20


class TestTheAuditDetectsAFreshDeadKey:
    def test_a_key_added_to_the_config_is_reported(self, tmp_path, monkeypatch):
        """Le scénario réel : une faute de frappe dans config.yaml."""
        import yaml

        config = yaml.safe_load((ROOT / "config.yaml").read_text(encoding="utf-8"))
        config["ip_rotation"]["watchdog_intervall"] = 30  # deux « l »
        faux = tmp_path / "config.yaml"
        faux.write_text(yaml.safe_dump(config, allow_unicode=True), encoding="utf-8")

        monkeypatch.setattr(audit, "CONFIG", faux)
        monkeypatch.setattr(audit, "BASELINE", tmp_path / "baseline.json")
        resultat = audit.auditer()
        assert "ip_rotation.watchdog_intervall" in resultat["dead"]

    def test_strict_mode_fails_on_a_new_dead_key(self, tmp_path, monkeypatch):
        import yaml

        config = yaml.safe_load((ROOT / "config.yaml").read_text(encoding="utf-8"))
        config["server"]["prot"] = 4000  # « port » mal orthographié
        faux = tmp_path / "config.yaml"
        faux.write_text(yaml.safe_dump(config, allow_unicode=True), encoding="utf-8")
        baseline = tmp_path / "baseline.json"
        baseline.write_text(json.dumps({"dead": [], "missing": []}), encoding="utf-8")

        monkeypatch.setattr(audit, "CONFIG", faux)
        monkeypatch.setattr(audit, "BASELINE", baseline)
        assert audit.main(["--strict"]) == 1

    def test_strict_mode_passes_when_the_baseline_already_knows_the_key(self, tmp_path, monkeypatch):
        import yaml

        config = yaml.safe_load((ROOT / "config.yaml").read_text(encoding="utf-8"))
        config["server"]["prot"] = 4000
        faux = tmp_path / "config.yaml"
        faux.write_text(yaml.safe_dump(config, allow_unicode=True), encoding="utf-8")
        baseline = tmp_path / "baseline.json"
        baseline.write_text(
            json.dumps({"dead": ["server.prot"], "missing": []}), encoding="utf-8"
        )

        monkeypatch.setattr(audit, "CONFIG", faux)
        monkeypatch.setattr(audit, "BASELINE", baseline)
        assert audit.main(["--strict"]) == 0


class TestDynamicSectionsAreNotAudited:
    def test_model_ids_are_not_reported_as_dead(self):
        """`models.<id>` est une donnée, pas un réglage : l'auditer serait du bruit."""
        resultat = audit.auditer()
        assert not [c for c in resultat["dead"] if c.startswith("models.")]
        assert not [c for c in resultat["dead"] if c.startswith("custom_routes.")]

    def test_per_model_maps_are_not_reported_as_dead(self):
        resultat = audit.auditer()
        for prefixe in ("thinking.effort_caps.", "thinking.min_tokens.", "geo.policies."):
            assert not [c for c in resultat["dead"] if c.startswith(prefixe)]


class TestDashboardRequireTokenIsNowHonoured:
    """Le réglage de sécurité qui ne s'appliquait pas."""

    def _api(self):
        from dashboard import api

        return api

    def test_env_true_wins(self, monkeypatch):
        monkeypatch.setenv("DASHBOARD_REQUIRE_TOKEN", "true")
        assert self._api()._require_token_from_config() is True

    def test_env_false_wins_over_yaml(self, monkeypatch):
        monkeypatch.setenv("DASHBOARD_REQUIRE_TOKEN", "0")
        monkeypatch.setattr(
            self._api().config_settings, "yaml_get", lambda *a, **k: True, raising=False
        )
        assert self._api()._require_token_from_config() is False

    def test_yaml_applies_when_env_is_unset(self, monkeypatch):
        monkeypatch.delenv("DASHBOARD_REQUIRE_TOKEN", raising=False)
        monkeypatch.setattr(
            self._api().config_settings, "yaml_get", lambda *a, **k: True, raising=False
        )
        assert self._api()._require_token_from_config() is True

    def test_a_broken_yaml_never_crashes_the_dashboard(self, monkeypatch):
        monkeypatch.delenv("DASHBOARD_REQUIRE_TOKEN", raising=False)

        def _boom(*a, **k):
            raise RuntimeError("config cassé")

        monkeypatch.setattr(self._api().config_settings, "yaml_get", _boom, raising=False)
        assert self._api()._require_token_from_config() is False
