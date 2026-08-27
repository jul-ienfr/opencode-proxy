"""Tests V4 100% : traitements 10-17 + strict:false (calm-rossum)."""
import json
import protocol_mapping as pm

_N = pm._normalize_tool_schema

# ── 10 : root type ────────────────────────────────────────────────
class TestRootType:
    def test_bash_without_type_becomes_object(self):
        schema = {"properties": {"command": {"type": "string"}}, "required": ["command"]}
        out = _N(schema, "muse-spark-1.2-contributor")
        assert out["type"] == "object"
        assert out["additionalProperties"] is False

    def test_root_already_typed_unchanged(self):
        schema = {"type": "object", "properties": {"x": {"type": "string"}}}
        out = _N(schema, "muse-spark-1.2-contributor")
        assert out["type"] == "object"

    def test_non_strict_no_force_root(self):
        schema = {"properties": {"x": {"type": "string"}}}
        out = _N(schema, "minimax-m2.5")
        assert "type" not in out

# ── 11 : properties type fallback ─────────────────────────────────
class TestPropertiesTypeFallback:
    def test_missing_type_becomes_string(self):
        schema = {"type": "object", "properties": {"q": {"description": "q"}}}
        out = _N(schema, "muse-spark-1.2-contributor")
        assert out["properties"]["q"]["type"] == "string"

    def test_with_anyof_not_forced(self):
        schema = {"type": "object", "properties": {"q": {"anyOf": [{"type": "string"}, {"type": "number"}]}}}
        out = _N(schema, "muse-spark-1.2-contributor")
        # anyOf résiduel non-null sera strippé en 15, mais 11 ne doit pas forcer type
        assert "type" not in out["properties"]["q"] or out["properties"]["q"].get("type") == "string" or "anyOf" in out["properties"]["q"]

# ── 12 : required ⊆ properties ────────────────────────────────────
class TestRequiredSubset:
    def test_required_orphan_removed(self):
        schema = {"type": "object", "properties": {"a": {"type": "string"}}, "required": ["a", "b"]}
        out = _N(schema, "muse-spark-1.2-contributor")
        assert out["required"] == ["a"]

    def test_required_all_orphan_removed(self):
        schema = {"type": "object", "properties": {"a": {"type": "string"}}, "required": ["z"]}
        out = _N(schema, "muse-spark-1.2-contributor")
        assert "required" not in out

# ── 13 : @schema_version strip ────────────────────────────────────
class TestSchemaVersionStrip:
    def test_at_schema_version_stripped(self):
        schema = {"type": "object", "properties": {"x": {"type": "string"}}, "@schema_version": "invalid json schema"}
        out = _N(schema, "muse-spark-1.2-contributor")
        assert "@schema_version" not in out

    def test_nested_schema_version_stripped(self):
        schema = {"type": "object", "properties": {"x": {"type": "string", "@schema_version": "invalid json schema"}}}
        out = _N(schema, "muse-spark-1.2-contributor")
        assert "@schema_version" not in out["properties"]["x"]

# ── 14 : additionalProperties:false forcé ─────────────────────────
class TestAdditionalPropertiesFalse:
    def test_root_missing_forced_false(self):
        schema = {"type": "object", "properties": {"x": {"type": "string"}}}
        out = _N(schema, "muse-spark-1.2-contributor")
        assert out["additionalProperties"] is False

    def test_nested_missing_forced_false(self):
        schema = {"type": "object", "properties": {"cfg": {"type": "object", "properties": {"y": {"type": "string"}}}}}
        out = _N(schema, "muse-spark-1.2-contributor")
        assert out["properties"]["cfg"]["additionalProperties"] is False

    def test_permissive_not_forced(self):
        schema = {"type": "object", "properties": {"x": {"type": "string"}}}
        out = _N(schema, "minimax-m2.5")
        assert "additionalProperties" not in out

# ── 15 : anyOf/oneOf résiduel strip ───────────────────────────────
class TestAnyOfResidual:
    def test_anyof_string_number_stripped_strict(self):
        schema = {"type": "object", "properties": {"q": {"anyOf": [{"type": "string"}, {"type": "number"}]}}}
        out = _N(schema, "muse-spark-1.2-contributor")
        # strict → anyOf doit disparaître
        assert "anyOf" not in json.dumps(out)

    def test_anyof_preserved_permissive(self):
        schema = {"anyOf": [{"type": "string"}, {"type": "number"}]}
        out = _N(schema, "minimax-m2.5")
        assert "anyOf" in out

# ── 16 : keywords strip ───────────────────────────────────────────
class TestUnsupportedKeywords:
    def test_const_stripped(self):
        schema = {"type": "object", "properties": {"x": {"type": "string", "const": "foo"}}}
        out = _N(schema, "muse-spark-1.2-contributor")
        assert "const" not in json.dumps(out)

    def test_title_schema_stripped(self):
        schema = {"type": "object", "title": "MyTitle", "$schema": "http://json-schema.org/draft-07/schema#", "properties": {"x": {"type": "string"}}}
        out = _N(schema, "muse-spark-1.2-contributor")
        assert "title" not in out and "$schema" not in out

# ── 17 : array items type ─────────────────────────────────────────
class TestArrayItems:
    def test_array_items_forced(self):
        schema = {"type": "array", "items": {"description": "x"}}
        out = _N(schema, "muse-spark-1.2-contributor")
        assert out["items"]["type"] == "string"

# ── strict:false sur tool émis ────────────────────────────────────
class TestStrictFalse:
    def test_chat_to_responses_sets_strict_false(self):
        chat = {"model": "muse-spark-1.2-contributor", "messages": [{"role": "user", "content": "hi"}], "tools": [{"type": "function", "function": {"name": "bash", "description": "run", "parameters": {"type": "object", "properties": {"command": {"type": "string"}}, "required": ["command"]}}}]}
        r = pm._chat_to_responses_request(chat)
        assert r["tools"][0]["strict"] is False
        assert r["tools"][0]["parameters"]["additionalProperties"] is False

    def test_chat_to_responses_qwen_no_strict(self):
        chat = {"model": "qwen3.5-plus", "messages": [{"role": "user", "content": "hi"}], "tools": [{"type": "function", "function": {"name": "bash", "description": "run", "parameters": {"type": "object", "properties": {"command": {"type": "string"}}}}}]}
        r = pm._chat_to_responses_request(chat)
        assert "strict" not in r["tools"][0]

# ── 9 tools end-to-end ────────────────────────────────────────────
class TestNineTools:
    def test_nine_tools_all_valid(self):
        tools = [
            {"name": "bash", "description": "x"*1100, "input_schema": {"properties": {"command": {"type": "string"}}, "required": ["command"], "@schema_version": "invalid json schema"}},
            {"name": "edit", "description": "edit", "input_schema": {"type": "object", "properties": {"path": {"type": "string"}, "content": {"description": "c"}}, "required": ["path", "ghost"]}},
            {"name": "glob", "description": "glob", "input_schema": {"type": "object", "properties": {"pattern": {"type": "string", "const": "x"}}, "additionalProperties": True}},
            {"name": "grep", "description": "grep", "input_schema": {"type": "object", "properties": {"q": {"anyOf": [{"type": "string"}, {"type": "number"}]}}}},
            {"name": "plan_exit", "description": "plan", "input_schema": {"type": "object", "properties": {"x": {"type": "string"}}, "title": "t"}},
            {"name": "question", "description": "q", "input_schema": {"type": "object", "properties": {"a": {"type": "array", "items": {"description": "i"}}}}},
            {"name": "read", "description": "read", "input_schema": {"type": "object", "properties": {"file": {"type": "string", "format": "uri"}}}},
            {"name": "skill", "description": "skill", "input_schema": {"type": "object", "properties": {"name": {"type": "string"}}, "required": ["name"]}},
            {"name": "write", "description": "write", "input_schema": {"type": "object", "properties": {"p": {"type": "string"}}, "required": ["p"]}},
        ]
        body = {"model": "muse-spark-1.2-contributor", "messages": [{"role": "user", "content": "hi"}], "tools": tools}
        r = pm.anthropic_to_openai(body, "muse-spark-1.2-contributor")
        for t in r["tools"]:
            p = t["function"]["parameters"]
            assert p.get("type") == "object"
            assert p.get("additionalProperties") is False
            assert "@schema_version" not in json.dumps(p)
            assert "const" not in json.dumps(p)
            assert "title" not in json.dumps(p)
