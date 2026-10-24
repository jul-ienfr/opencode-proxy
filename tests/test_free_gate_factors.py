"""V6.1.3 — la porte free, facteur par facteur.

**Pourquoi ce fichier existe.** `config/free_gate.py` encode le modele d'acces de
la jambe free : `egress AND identity AND body`. C'est la theorie qui explique
pourquoi une requete passe ou non — et elle n'avait **aucun test**. Un modele
non teste n'est pas un modele, c'est une intention.

**La regle de conception de ces tests : l'independance.** Chaque facteur a son
test, et chaque test echoue seul si son facteur regresse. Un test unique
« passed == False » ne dirait pas *lequel* a casse — or c'est exactement
l'information dont on a besoin pour diagnostiquer un 403.

**Ce que ces tests ne pretendent pas.** Que la porte fonctionne aujourd'hui :
le module n'est cable nulle part dans le proxy. Ils verrouillent la theorie,
pour qu'elle soit utilisable le jour ou on la branche (V6.1.2).
"""

import pytest

from config.free_gate import (
    MIN_SHIM_DESCRIPTION,
    REQUIRED_SHIM_TOOL,
    body_ok,
    diagnose,
    identity_ok,
    legacy_identity_present,
    session_identity,
    shim_tools,
)

#: Un shim « realiste » : description longue et au moins un parametre. Un stub
#: `{"name": "read"}` ne suffit pas — l'amont le verrait comme un faux.
REAL_TOOL = {
    "type": "function",
    "function": {
        "name": REQUIRED_SHIM_TOOL,
        "description": "Reads a file from the filesystem and returns its contents, with paging.",
        "parameters": {
            "type": "object",
            "properties": {"filePath": {"type": "string"}},
            "required": ["filePath"],
        },
    },
}

IDENTITY = {"x-opencode-session-id": "ses_abc123"}


def _body(**over):
    body = {"stream": True, "tools": [REAL_TOOL]}
    body.update(over)
    return body


class TestIdentityFactor:
    """Facteur 1 : l'amont veut une identite de session lisible."""

    def test_a_session_id_is_recognised(self):
        assert session_identity(IDENTITY) == ["x-opencode-session-id"]
        assert identity_ok(IDENTITY)

    def test_header_names_are_case_insensitive(self):
        assert identity_ok({"X-OpenCode-Session-Id": "ses_abc123"})

    def test_an_empty_value_is_not_an_identity(self):
        assert not identity_ok({"x-opencode-session-id": "   "})

    def test_no_header_means_failure(self):
        assert session_identity({}) == []
        assert not identity_ok(None)

    def test_legacy_headers_are_informational_only(self):
        """Le quartet historique ne doit JAMAIS suffire a passer la porte."""
        legacy = {"x-opencode-client": "cli", "x-opencode-request": "req_1"}
        assert legacy_identity_present(legacy), "le quartet doit rester detectable"
        assert not identity_ok(legacy), "mais il ne doit pas ouvrir la porte"

    def test_identity_factor_fails_alone(self):
        """Corps valide + egress ok : seul `identity` doit manquer."""
        verdict = diagnose({}, _body(), egress="ok")
        assert verdict.failed_factors == ["identity"]
        assert not verdict.passed


class TestBodyFactor:
    """Facteur 2 : `stream: true` ET un shim `read` realiste."""

    def test_a_realistic_shim_is_recognised(self):
        assert shim_tools(_body()) == [REQUIRED_SHIM_TOOL]
        assert body_ok(_body())

    def test_streaming_is_mandatory(self):
        assert not body_ok(_body(stream=False))
        assert not body_ok(_body(stream="true")), "la chaine 'true' n'est pas le booleen"

    def test_a_stub_tool_is_not_a_shim(self):
        stub = {"type": "function", "function": {"name": "read", "description": "read"}}
        assert shim_tools({"stream": True, "tools": [stub]}) == []
        assert not body_ok({"stream": True, "tools": [stub]})

    def test_a_short_description_is_not_realistic(self):
        tool = {
            "type": "function",
            "function": {
                "name": "read",
                "description": "x" * (MIN_SHIM_DESCRIPTION - 1),
                "parameters": {"type": "object", "properties": {"a": {"type": "string"}}},
            },
        }
        assert not body_ok({"stream": True, "tools": [tool]})

    def test_another_tool_does_not_satisfy_the_gate(self):
        other = {"type": "function", "function": dict(REAL_TOOL["function"], name="write")}
        assert not body_ok({"stream": True, "tools": [other]})

    def test_the_anthropic_shape_is_also_admitted(self):
        """`input_schema` au lieu de `parameters` : le client officiel emet les deux."""
        anthropic = {
            "name": "read",
            "description": "Reads a file from the filesystem and returns its contents, with paging.",
            "input_schema": {"type": "object", "properties": {"filePath": {"type": "string"}}},
        }
        assert body_ok({"stream": True, "tools": [anthropic]})

    def test_body_factor_fails_alone(self):
        verdict = diagnose(IDENTITY, {"stream": False, "tools": []}, egress="ok")
        assert verdict.failed_factors == ["body"]
        assert not verdict.passed


class TestEgressFactor:
    """Facteur 3 : une sortie constatee comme refusee ne passe pas."""

    def test_rejected_egress_fails_alone(self):
        verdict = diagnose(IDENTITY, _body(), egress="rejected")
        assert verdict.failed_factors == ["egress"]
        assert not verdict.passed

    def test_unknown_egress_is_not_a_rejection(self):
        """Hors tunnel, on ne peut pas prouver la reputation : on ne la nie pas."""
        verdict = diagnose(IDENTITY, _body(), egress="unknown")
        assert verdict.passed
        assert verdict.failed_factors == []


class TestVerdict:
    def test_all_three_factors_together_pass(self):
        verdict = diagnose(IDENTITY, _body(), egress="ok")
        assert verdict.passed
        assert verdict.failed_factors == []
        assert verdict.identity_headers == ("x-opencode-session-id",)
        assert verdict.shim_tools == (REQUIRED_SHIM_TOOL,)

    def test_all_three_can_fail_together(self):
        verdict = diagnose({}, {"stream": False}, egress="rejected")
        assert verdict.failed_factors == ["egress", "identity", "body"]

    def test_the_log_form_names_every_factor(self):
        log = diagnose({}, {"stream": False}, egress="rejected").as_log()["gate"]
        assert log["passed"] is False
        assert set(log["failed"]) == {"egress", "identity", "body"}
        assert log["egress"] == "rejected"
        assert log["identity"] is False
        assert log["body"] is False

    @pytest.mark.parametrize("egress", ["ok", "unknown", "rejected", "autre-chose"])
    def test_any_other_egress_value_is_treated_as_not_rejected(self, egress):
        verdict = diagnose(IDENTITY, _body(), egress=egress)
        assert verdict.passed is (egress != "rejected")
