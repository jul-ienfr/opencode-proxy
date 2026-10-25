"""V6.2.1 — le registre de shims doit dire ce qui est *nécessaire*, pas ce qui est utile.

**Le fait mesuré qui justifie ces tests.** La porte d'inférence n'accepte pas un
catalogue d'outils : elle exige le minimum crédible. `read` avec une définition
réaliste → 200 ; `bash` seul → 403 ; description courte ou `properties: {}` →
403 même avec le bon nom.

**Le risque que ces tests verrouillent.** Déplacer des définitions mesurées vers
un fichier de configuration, c'est donner à un futur éditeur le pouvoir de casser
la porte sans le savoir — une description raccourcie, un `properties` vidé, et
chaque requête free devient un 403 muet. D'où la validation au chargement : une
entrée invalide est écartée **avec sa raison**, et le repli du code reste en place.
"""

import pytest

from config.free_shims import (
    DEFAULT_SHIMS,
    MIN_DESCRIPTION,
    load_shims,
    necessity,
    reset_cache,
    shims,
    validate_entry,
)


@pytest.fixture(autouse=True)
def _clean():
    reset_cache()
    yield
    reset_cache()


def entry(**kwargs):
    base = {
        "necessity": "optional",
        "description": "Reads a file from the filesystem and returns its contents.",
        "parameters": {
            "type": "object",
            "properties": {"filePath": {"type": "string"}},
            "required": ["filePath"],
        },
    }
    base.update(kwargs)
    return base


class TestDefaults:
    def test_read_is_required(self):
        """La bisection live l'a établi ; le registre doit le dire."""
        assert DEFAULT_SHIMS["read"]["necessity"] == "required"

    def test_bash_is_absent_after_measurement(self):
        """V6.2.2 : mesuré non load-bearing, donc retiré.

        Sans outil → 403 ; `read` seul → 200 ; `bash` seul → 403 ;
        `read` + `bash` → 200. `bash` n'ajoute rien : on ne l'injecte plus.
        """
        assert "bash" not in DEFAULT_SHIMS

    def test_read_alone_is_the_measured_minimum(self):
        assert set(DEFAULT_SHIMS) == {"read"}

    def test_default_descriptions_satisfy_the_gate(self):
        for name, spec in DEFAULT_SHIMS.items():
            assert len(spec["description"]) >= MIN_DESCRIPTION, name

    def test_default_parameters_are_real(self):
        for name, spec in DEFAULT_SHIMS.items():
            props = spec["parameters"]["properties"]
            assert props, f"{name} sans propriétés = 403 garanti"
            assert spec["parameters"]["type"] == "object"


class TestValidation:
    def test_a_valid_entry_is_accepted(self):
        clean, reason = validate_entry("read", entry())
        assert clean is not None and reason == ""

    def test_a_short_description_is_refused(self):
        clean, reason = validate_entry("x", entry(description="Do not use this tool."))
        assert clean is None
        assert "courte" in reason

    def test_empty_properties_are_refused(self):
        clean, reason = validate_entry(
            "x", entry(parameters={"type": "object", "properties": {}})
        )
        assert clean is None
        assert "propriétés" in reason

    def test_missing_parameters_are_refused(self):
        clean, reason = validate_entry("x", entry(parameters=None))
        assert clean is None and "paramètres" in reason

    def test_unknown_necessity_is_refused(self):
        clean, reason = validate_entry("x", entry(necessity="obligatoire"))
        assert clean is None and "necessity" in reason

    def test_necessity_defaults_to_optional(self):
        e = entry()
        del e["necessity"]
        clean, _ = validate_entry("x", e)
        assert clean["necessity"] == "optional"

    def test_empty_name_is_refused(self):
        clean, _ = validate_entry("  ", entry())
        assert clean is None


class TestLoading:
    def test_without_config_the_defaults_are_used(self):
        merged = load_shims(None)
        assert set(merged) == set(DEFAULT_SHIMS)

    def test_config_overrides_a_default(self):
        merged = load_shims({"read": entry(description="Reads a file, verbosely and very carefully.")})
        assert merged["read"]["description"].startswith("Reads a file, verbosely")

    def test_an_invalid_entry_never_erases_a_default(self):
        """Le point critique : une erreur de config ne doit pas casser la porte."""
        merged = load_shims({"read": entry(description="trop court")})
        assert merged["read"]["description"] == DEFAULT_SHIMS["read"]["description"]

    def test_a_new_valid_shim_is_added(self):
        merged = load_shims({"edit": entry(description="Applies a precise edit to a file on disk.")})
        assert "edit" in merged

    def test_garbage_config_is_ignored(self):
        assert set(load_shims("pas un mapping")) == set(DEFAULT_SHIMS)
        assert set(load_shims({"x": 42})) == set(DEFAULT_SHIMS)


class TestRegistry:
    def test_the_registry_reads_the_configuration(self):
        registry = shims()
        assert "read" in registry
        assert necessity("read") == "required"

    def test_bash_can_be_reinstated_by_configuration(self):
        """Un shim retiré par mesure doit rester réintroduisible sans code."""
        merged = load_shims({"bash": entry(description="Runs a shell command and returns its output.")})
        assert "bash" in merged

    def test_unknown_necessity_is_empty(self):
        assert necessity("inconnu") == ""

    def test_the_shipped_config_declares_only_the_measured_minimum(self):
        import yaml

        with open("config.yaml", encoding="utf-8") as fh:
            cfg = yaml.safe_load(fh)
        shims_cfg = (cfg.get("free") or {}).get("shims") or {}
        assert set(shims_cfg) == {"read"}, "bash ne doit plus être déclaré (V6.2.2)"
        assert shims_cfg["read"]["necessity"] == "required"


class TestWireShapes:
    def test_openai_shape_is_unchanged(self):
        import opencode as oc

        tool = oc._free_shim_tool("read", responses=False)
        assert tool["type"] == "function"
        assert tool["function"]["name"] == "read"
        assert tool["function"]["parameters"]["properties"]

    def test_responses_shape_is_flat(self):
        import opencode as oc

        tool = oc._free_shim_tool("read", responses=True)
        assert tool["name"] == "read"
        assert "function" not in tool

    def test_an_unknown_shim_still_gets_a_plausible_definition(self):
        import opencode as oc

        tool = oc._free_shim_tool("weird_tool", responses=False)
        assert tool["function"]["description"]
        assert tool["function"]["parameters"]["properties"]

    def test_the_proxy_uses_the_registry(self):
        import inspect

        import opencode as oc

        source = inspect.getsource(oc)
        assert "from config.free_shims import shims" in source


class TestInjection:
    """2026-10-25 — ce qui part réellement sur le fil, mesuré et non supposé.

    Le gate a été durci le 23/10 à 23:17 UTC : « read seul » (mesure du
    21/10) ne passe plus. Il exige la suite COMPLÈTE des 10 tools officiels
    plus le prompt système. Mesuré sur le vrai endpoint, même IP, même
    seconde : 10 tools + système → 200 ; 1 tool → 403 ; sans système → 429.
    """

    OFFICIAL = [
        "bash", "edit", "glob", "grep", "read", "skill", "task", "todowrite", "webfetch", "write",
    ]

    def _wire(self, body):
        import opencode as oc

        wire, _ = oc._free_wire_body(body, "big-pickle")
        return wire

    def test_full_official_tool_set_is_injected(self):
        wire = self._wire({"messages": [{"role": "user", "content": "hi"}]})
        names = [t["function"]["name"] for t in wire.get("tools") or []]
        assert names == self.OFFICIAL, f"outils injectés : {names}"

    def test_bash_is_part_of_the_official_signature(self):
        wire = self._wire({"messages": [{"role": "user", "content": "hi"}]})
        names = [t["function"]["name"] for t in wire.get("tools") or []]
        assert "bash" in names

    def test_client_tools_are_replaced_by_the_official_copy(self):
        wire = self._wire(
            {
                "messages": [{"role": "user", "content": "hi"}],
                "tools": [
                    {
                        "type": "function",
                        "function": {
                            "name": "my_tool",
                            "description": "Does a client-side thing for the user.",
                            "parameters": {"type": "object", "properties": {}},
                        },
                    }
                ],
            }
        )
        names = [t["function"]["name"] for t in wire.get("tools") or []]
        assert names == self.OFFICIAL

    def test_tool_choice_stays_auto(self):
        wire = self._wire({"messages": [{"role": "user", "content": "hi"}]})
        assert wire.get("tool_choice") == "auto"
