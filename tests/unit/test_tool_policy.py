from free.clients import tool_policy as tp


def test_aliases_map_to_official_names():
    assert tp.map_tool_name("Read") == "read"
    assert tp.map_tool_name("Bash") == "shell"
    assert tp.map_tool_name("MultiEdit") == "edit"
    assert tp.map_tool_name("unknown_tool") == "unknown_tool"


def test_grid_contains_every_official_tool():
    result = tp.build_grid([])
    names = [tp.tool_name(t) for t in result["tools"]]
    assert names == tp.official_grid()
    assert len(names) == 13


def test_client_tool_preserves_its_schema_and_maps_name():
    client_tools = [
        {
            "type": "function",
            "function": {
                "name": "Read",
                "description": "read a file",
                "parameters": {"type": "object", "properties": {"path": {"type": "string"}}},
            },
        }
    ]
    result = tp.build_grid(client_tools)
    by_name = {tp.tool_name(t): t for t in result["tools"]}
    assert "read" in by_name
    assert by_name["read"]["function"]["parameters"]["properties"]["path"]["type"] == "string"
    assert result["mapping"]["Read"] == "read"
    assert tp.reverse_name("read", result["mapping"]) == "Read"


def test_forbidden_tool_is_removed():
    client_tools = [
        {"type": "function", "function": {"name": "subagent_fork", "parameters": {}}}
    ]
    result = tp.build_grid(client_tools)
    assert "subagent_fork" in result["forbidden_removed"]
    assert all(tp.tool_name(t) != "subagent_fork" for t in result["tools"])


def test_extras_are_capped():
    client_tools = [
        {"type": "function", "function": {"name": f"mcp_{i}", "parameters": {}}}
        for i in range(20)
    ]
    result = tp.build_grid(client_tools, max_extras=3)
    assert len(result["extras"]) == 3


def test_unknown_call_is_healed_or_dropped():
    assert tp.heal_unknown_call("bogus", {"read"}) == "text_heal"
    assert tp.heal_unknown_call("read", {"read"}) == "keep"
