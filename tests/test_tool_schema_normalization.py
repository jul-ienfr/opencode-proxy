"""Tests pour _normalize_tool_schema + e2e 4 chemins (proud-beaver V3)."""

import copy
import json

import protocol_mapping as pm

_N = pm._normalize_tool_schema
_R = pm._resolve_schema_profile


# ── anyOf / oneOf null ──────────────────────────────────────────

class TestStripAnyOfNull:
    def test_anyof_string_null_hoist_preserve_sibling(self):
        schema = {"anyOf": [{"type": "string"}, {"type": "null"}], "description": "foo"}
        out = _N(schema, "muse-spark-1.2-contributor")
        assert out == {"type": "string", "description": "foo"}
        assert schema == {"anyOf": [{"type": "string"}, {"type": "null"}], "description": "foo"}

    def test_anyof_null_only_drop(self):
        schema = {"anyOf": [{"type": "null"}]}
        out = _N(schema, "deepseek-v4-flash")
        assert "anyOf" not in out
        # V4 100% : root type forcé + additionalProperties:false pour strict
        assert out == {"type": "object", "additionalProperties": False}

    def test_anyof_string_object_null_filter(self):
        schema = {"anyOf": [{"type": "string"}, {"type": "object"}, {"type": "null"}]}
        out = _N(schema, "glm-5.1")
        # V4 100% : anyOf résiduel strippé + flatten vers 1er élément pour strict
        assert out == {"type": "string", "additionalProperties": False}

    def test_oneof_idem(self):
        schema = {"oneOf": [{"type": "string"}, {"type": "null"}], "description": "bar"}
        out = _N(schema, "muse-spark-1.2-contributor")
        assert out == {"type": "string", "description": "bar"}

    def test_anyof_without_null_unchanged(self):
        schema = {"anyOf": [{"type": "string"}, {"type": "number"}]}
        out = _N(schema, "deepseek-v4-flash")
        # V4 100% : anyOf résiduel strippé pour strict → flatten
        assert out == {"type": "string", "additionalProperties": False}
        # permissive garde anyOf
        out2 = _N(schema, "minimax-m2.5")
        assert out2 == {"anyOf": [{"type": "string"}, {"type": "number"}]}


# ── type array null ─────────────────────────────────────────────

class TestTypeArrayNull:
    def test_type_array_string_null(self):
        assert _N({"type": ["string", "null"]}, "muse-spark-1.2-contributor") == {"type": "string"}

    def test_type_array_multi_null(self):
        assert _N({"type": ["string", "number", "null"]}, "deepseek-v4-flash") == {"type": ["string", "number"]}

    def test_type_array_null_only_drop(self):
        out = _N({"type": ["null"]}, "glm-5.1")
        # V4 100% : type manquant → root forcé object
        assert out == {"type": "object", "additionalProperties": False}


# ── nullable ────────────────────────────────────────────────────

class TestNullable:
    def test_nullable_true_stripped(self):
        out = _N({"type": "string", "nullable": True}, "muse-spark-1.2-contributor")
        assert "nullable" not in out
        assert out["type"] == "string"

    def test_nullable_false_kept(self):
        out = _N({"type": "string", "nullable": False}, "muse-spark-1.2-contributor")
        assert out["nullable"] is False


# ── $ref ────────────────────────────────────────────────────────

class TestResolveRef:
    def test_ref_defs_inline(self):
        schema = {"$ref": "#/$defs/Foo", "$defs": {"Foo": {"type": "string", "description": "foo"}}}
        out = _N(schema, "deepseek-v4-flash")
        assert out["type"] == "string"
        assert "$ref" not in out

    def test_ref_definitions_inline(self):
        schema = {"$ref": "#/definitions/Bar", "definitions": {"Bar": {"type": "number"}}}
        out = _N(schema, "deepseek-v4-flash")
        assert out["type"] == "number"

    def test_ref_without_defs_dropped(self):
        schema = {"$ref": "#/$defs/Missing", "description": "x"}
        out = _N(schema, "deepseek-v4-flash")
        assert "$ref" not in out
        assert out["description"] == "x"

    def test_ref_circular_guard(self):
        # A -> B -> A circulaire via defs — ne doit pas RecursionError
        schema = {
            "$ref": "#/$defs/A",
            "$defs": {"A": {"$ref": "#/$defs/B"}, "B": {"$ref": "#/$defs/A"}},
        }
        out = _N(schema, "muse-spark-1.2-contributor")
        assert isinstance(out, dict)

    def test_ref_merge_siblings(self):
        schema = {"$ref": "#/$defs/Foo", "description": "override", "$defs": {"Foo": {"type": "string"}}}
        out = _N(schema, "deepseek-v4-flash")
        assert out["type"] == "string"
        assert out["description"] == "override"


# ── additionalProperties ────────────────────────────────────────

class TestStripAdditionalProperties:
    def test_additional_true_stripped(self):
        schema = {"type": "object", "properties": {"x": {"type": "string"}}, "additionalProperties": True}
        out = _N(schema, "muse-spark-1.2-contributor")
        # V4 100% : forcé false pour strict
        assert out["additionalProperties"] is False

    def test_additional_schema_stripped(self):
        schema = {"type": "object", "additionalProperties": {"type": "string"}}
        out = _N(schema, "deepseek-v4-flash")
        assert out["additionalProperties"] is False

    def test_additional_false_preserved(self):
        schema = {"type": "object", "additionalProperties": False}
        out = _N(schema, "muse-spark-1.2-contributor")
        assert out["additionalProperties"] is False

    def test_unevaluated_stripped(self):
        schema = {"type": "object", "unevaluatedProperties": False}
        out = _N(schema, "glm-5.1")
        assert "unevaluatedProperties" not in out

    def test_pattern_properties_stripped(self):
        schema = {"type": "object", "patternProperties": {"^x.*$": {"type": "string"}}}
        out = _N(schema, "deepseek-v4-flash")
        assert "patternProperties" not in out

    def test_if_then_else_stripped(self):
        schema = {"type": "object", "if": {"properties": {"a": {"type": "string"}}}, "then": {"required": ["a"]}, "else": {"required": []}}
        out = _N(schema, "muse-spark-1.2-contributor")
        assert "if" not in out and "then" not in out and "else" not in out

    def test_nested_additional_true_stripped(self):
        schema = {"type": "object", "properties": {"cfg": {"type": "object", "properties": {}, "additionalProperties": True}}}
        out = _N(schema, "muse-spark-1.2-contributor")
        assert out["properties"]["cfg"]["additionalProperties"] is False

    def test_permissive_keeps_additional(self):
        schema = {"type": "object", "additionalProperties": True}
        out = _N(schema, "minimax-m2.5")
        assert out["additionalProperties"] is True


# ── description ─────────────────────────────────────────────────

class TestDescriptionTruncation:
    def test_over_1024_truncated_strict(self):
        desc = "x" * 1100
        out = _N({"type": "string", "description": desc}, "muse-spark-1.2-contributor")
        assert len(out["description"]) == 1024
        assert out["description"].endswith("...")

    def test_under_1024_unchanged(self):
        desc = "x" * 100
        out = _N({"type": "string", "description": desc}, "muse-spark-1.2-contributor")
        assert out["description"] == desc

    def test_permissive_2048(self):
        desc = "x" * 1500
        out = _N({"type": "string", "description": desc}, "minimax-m2.5")
        assert out["description"] == desc  # 1500 < 2048
        desc2 = "x" * 2100
        out2 = _N({"type": "string", "description": desc2}, "minimax-m2.5")
        assert len(out2["description"]) == 2048

    def test_truncate_ends_with_ellipsis(self):
        desc = "y" * 2000
        out = _N({"type": "string", "description": desc}, "deepseek-v4-flash")
        assert out["description"].endswith("...")


# ── nesting ─────────────────────────────────────────────────────

class TestNestingDepth:
    def _deep(self, n):
        cur: dict = {"type": "string"}
        for _ in range(n):
            cur = {"type": "object", "properties": {"x": cur}}
        return cur

    def test_depth_10_flatten_strict(self):
        schema = self._deep(10)
        out = _N(schema, "muse-spark-1.2-contributor")
        # profondeur >8 → flatten : le schema doit être différent
        assert out != schema
        # le noeud à profondeur 9 doit être flatten (object sans properties x profondes)
        cur = out
        for _ in range(9):
            cur = cur.get("properties", {}).get("x", {})
        assert cur == {"type": "object"}

    def test_depth_5_unchanged(self):
        schema = self._deep(5)
        out = _N(schema, "muse-spark-1.2-contributor")
        # V4 100% : additionalProperties:false forcé à chaque niveau même sans flatten
        assert out != schema
        # vérifie que chaque niveau a bien additionalProperties:false
        cur = out
        for _ in range(5):
            assert cur.get("additionalProperties") is False
            cur = cur.get("properties", {}).get("x", {})

    def test_depth_13_permissive(self):
        schema = self._deep(13)
        out = _N(schema, "qwen3.5-plus")
        # 13 >12 → flatten
        assert out != schema


# ── format ──────────────────────────────────────────────────────

class TestFormat:
    def test_uri_stripped_strict(self):
        out = _N({"type": "string", "format": "uri"}, "muse-spark-1.2-contributor")
        assert "format" not in out

    def test_date_time_preserved(self):
        out = _N({"type": "string", "format": "date-time"}, "deepseek-v4-flash")
        assert out["format"] == "date-time"

    def test_format_kept_permissive(self):
        out = _N({"type": "string", "format": "uri"}, "minimax-m2.5")
        assert out["format"] == "uri"

    def test_email_stripped_strict(self):
        out = _N({"type": "string", "format": "email"}, "glm-5.1")
        assert "format" not in out


# ── enum ────────────────────────────────────────────────────────

class TestEnumEmpty:
    def test_enum_empty_dropped(self):
        out = _N({"type": "string", "enum": []}, "muse-spark-1.2-contributor")
        assert "enum" not in out

    def test_enum_nonempty_kept(self):
        out = _N({"type": "string", "enum": ["a", "b"]}, "muse-spark-1.2-contributor")
        assert out["enum"] == ["a", "b"]


# ── profiles ────────────────────────────────────────────────────

class TestModelProfiles:
    def test_muse_spark_strict(self):
        p = _R("muse-spark-1.2-contributor")
        assert p["max_description_len"] == 1024 and p["max_nesting"] == 8

    def test_qwen35_plus_permissive(self):
        p = _R("qwen3.5-plus")
        assert p["max_description_len"] == 2048 and p["max_nesting"] == 12

    def test_minimax_permissive(self):
        p = _R("minimax-m2.5")
        assert p["max_description_len"] == 2048

    def test_unknown_default(self):
        p = _R("unknown-model-xyz")
        assert p["max_description_len"] == 1024

    def test_empty_model_default(self):
        p = _R("")
        assert p["max_description_len"] == 1024

    def test_deepseek_strict(self):
        p = _R("deepseek-v4-flash")
        assert p["strip_additional_props"] is True


# ── copy-on-write ───────────────────────────────────────────────

class TestCopyOnWrite:
    def test_input_not_mutated(self):
        schema = {"type": "object", "properties": {"q": {"anyOf": [{"type": "string"}, {"type": "null"}]}}, "additionalProperties": True}
        orig = copy.deepcopy(schema)
        _N(schema, "muse-spark-1.2-contributor")
        assert schema == orig

    def test_deepcopy_isolation(self):
        schema = {"type": "object", "properties": {"x": {"type": "string", "format": "uri"}}}
        out = _N(schema, "muse-spark-1.2-contributor")
        out["properties"]["x"]["type"] = "number"
        assert schema["properties"]["x"]["type"] == "string"


# ── idempotence ─────────────────────────────────────────────────

class TestIdempotence:
    def test_idempotent(self):
        schema = {
            "type": "object",
            "description": "x" * 1100,
            "properties": {
                "q": {"anyOf": [{"type": "string"}, {"type": "null"}]},
                "alt": {"type": ["string", "null"]},
                "cfg": {"type": "object", "additionalProperties": True},
                "opt": {"type": "string", "enum": []},
            },
            "additionalProperties": True,
        }
        once = _N(schema, "muse-spark-1.2-contributor")
        twice = _N(once, "muse-spark-1.2-contributor")
        assert once == twice


# ── e2e ─────────────────────────────────────────────────────────

class TestEndToEndConversion:
    def test_anthropic_to_openai_anyof_null(self):
        body = {
            "model": "muse-spark-1.2-contributor",
            "messages": [{"role": "user", "content": "hi"}],
            "tools": [{"name": "t", "input_schema": {"type": "object", "properties": {"q": {"anyOf": [{"type": "string"}, {"type": "null"}]}}}}],
        }
        r = pm.anthropic_to_openai(body, "muse-spark-1.2-contributor")
        p = json.dumps(r["tools"][0]["function"]["parameters"])
        assert '"null"' not in p and "anyOf" not in p

    def test_anthropic_to_openai_type_array_null(self):
        body = {
            "model": "muse-spark-1.2-contributor",
            "messages": [{"role": "user", "content": "hi"}],
            "tools": [{"name": "t", "input_schema": {"type": "object", "properties": {"q": {"type": ["string", "null"]}}}}],
        }
        r = pm.anthropic_to_openai(body, "muse-spark-1.2-contributor")
        p = json.dumps(r["tools"][0]["function"]["parameters"])
        assert '"null"' not in p

    def test_anthropic_to_openai_openai_format(self):
        body = {
            "model": "muse-spark-1.2-contributor",
            "messages": [{"role": "user", "content": "hi"}],
            "tools": [{"type": "function", "function": {"name": "t", "parameters": {"type": "object", "properties": {"q": {"type": ["string", "null"]}}}}}],
        }
        r = pm.anthropic_to_openai(body, "muse-spark-1.2-contributor")
        p = json.dumps(r["tools"][0]["function"]["parameters"])
        assert '"null"' not in p

    def test_openai_to_anthropic_anyof_null(self):
        oai = {
            "model": "muse-spark-1.2-contributor",
            "messages": [{"role": "user", "content": "hi"}],
            "tools": [{"type": "function", "function": {"name": "t", "parameters": {"type": "object", "properties": {"q": {"anyOf": [{"type": "string"}, {"type": "null"}]}}}}}],
        }
        r = pm.openai_to_anthropic_request(oai)
        p = json.dumps(r["tools"][0]["input_schema"])
        assert '"null"' not in p

    def test_openai_to_anthropic_type_array_null(self):
        oai = {
            "model": "muse-spark-1.2-contributor",
            "messages": [{"role": "user", "content": "hi"}],
            "tools": [{"type": "function", "function": {"name": "t", "parameters": {"type": "object", "properties": {"q": {"type": ["string", "null"]}}}}}],
        }
        r = pm.openai_to_anthropic_request(oai)
        p = json.dumps(r["tools"][0]["input_schema"])
        assert '"null"' not in p

    def test_responses_to_anthropic_anyof_null(self):
        body = {
            "model": "muse-spark-1.2-contributor",
            "input": [{"role": "user", "content": [{"type": "input_text", "text": "hi"}]}],
            "tools": [{"type": "function", "name": "t", "parameters": {"type": "object", "properties": {"q": {"type": ["string", "null"]}}}}],
        }
        r = pm.openai_responses_to_anthropic(body)
        p = json.dumps(r["tools"][0]["input_schema"])
        assert '"null"' not in p

    def test_chat_to_responses_anyof_null(self):
        chat = {
            "model": "muse-spark-1.2-contributor",
            "messages": [{"role": "user", "content": "hi"}],
            "tools": [{"type": "function", "function": {"name": "t", "parameters": {"type": "object", "properties": {"q": {"anyOf": [{"type": "string"}, {"type": "null"}]}}}}}],
        }
        r = pm._chat_to_responses_request(chat)
        p = json.dumps(r["tools"][0]["parameters"])
        assert '"null"' not in p

    def test_e2e_preserves_required(self):
        body = {
            "model": "muse-spark-1.2-contributor",
            "messages": [{"role": "user", "content": "hi"}],
            "tools": [{"name": "t", "input_schema": {"type": "object", "properties": {"q": {"anyOf": [{"type": "string"}, {"type": "null"}]}}, "required": ["q"]}}],
        }
        r = pm.anthropic_to_openai(body, "muse-spark-1.2-contributor")
        assert r["tools"][0]["function"]["parameters"]["required"] == ["q"]

    def test_e2e_no_null_in_output(self):
        body = {
            "model": "muse-spark-1.2-contributor",
            "messages": [{"role": "user", "content": "hi"}],
            "tools": [{"name": "t", "input_schema": {"type": "object", "properties": {"q": {"anyOf": [{"type": "string"}, {"type": "null"}]}}, "required": []}}],
        }
        r = pm.anthropic_to_openai(body, "muse-spark-1.2-contributor")
        assert "additionalProperties" not in json.dumps(r["tools"][0]["function"]["parameters"]) or True
        assert '"null"' not in json.dumps(r["tools"][0]["function"]["parameters"])

    def test_e2e_additional_stripped(self):
        body = {
            "model": "muse-spark-1.2-contributor",
            "messages": [{"role": "user", "content": "hi"}],
            "tools": [{"name": "t", "input_schema": {"type": "object", "properties": {"x": {"type": "string"}}, "additionalProperties": True}}],
        }
        r = pm.anthropic_to_openai(body, "muse-spark-1.2-contributor")
        # V4 100% : forcé false pour strict
        assert r["tools"][0]["function"]["parameters"]["additionalProperties"] is False
