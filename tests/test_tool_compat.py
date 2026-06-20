#!/usr/bin/env python3
"""
Tool Compatibility Tester for opencode-proxy.

Sends minimal requests to each model via the running proxy and records
which tools each model accepts and can invoke correctly.

Usage:
    python tests/test_tool_compat.py [--proxy http://localhost:4000] [--output tool_compat_results.json]
"""
import asyncio
import json
import sys
import time
import argparse
from datetime import datetime, timezone

try:
    import httpx
except ImportError:
    print("ERROR: httpx is required. Install with: pip install httpx")
    sys.exit(1)


# ── Tool definitions (Anthropic format) ──────────────────────────────

TOOL_DEFINITIONS = {
    "Read": {
        "name": "Read",
        "description": "Read a file from the filesystem.",
        "input_schema": {
            "type": "object",
            "properties": {
                "file_path": {"type": "string", "description": "Path to the file"}
            },
            "required": ["file_path"]
        }
    },
    "Write": {
        "name": "Write",
        "description": "Write content to a file.",
        "input_schema": {
            "type": "object",
            "properties": {
                "file_path": {"type": "string"},
                "content": {"type": "string"}
            },
            "required": ["file_path", "content"]
        }
    },
    "Edit": {
        "name": "Edit",
        "description": "Edit a file by replacing text.",
        "input_schema": {
            "type": "object",
            "properties": {
                "file_path": {"type": "string"},
                "old_string": {"type": "string"},
                "new_string": {"type": "string"}
            },
            "required": ["file_path", "old_string", "new_string"]
        }
    },
    "Bash": {
        "name": "Bash",
        "description": "Execute a bash command.",
        "input_schema": {
            "type": "object",
            "properties": {
                "command": {"type": "string", "description": "The command to execute"}
            },
            "required": ["command"]
        }
    },
    "Grep": {
        "name": "Grep",
        "description": "Search for a pattern in files.",
        "input_schema": {
            "type": "object",
            "properties": {
                "pattern": {"type": "string"},
                "path": {"type": "string"}
            },
            "required": ["pattern"]
        }
    },
    "Glob": {
        "name": "Glob",
        "description": "Find files matching a glob pattern.",
        "input_schema": {
            "type": "object",
            "properties": {
                "pattern": {"type": "string"}
            },
            "required": ["pattern"]
        }
    },
    "WebSearch": {
        "name": "WebSearch",
        "description": "Search the web.",
        "input_schema": {
            "type": "object",
            "properties": {
                "query": {"type": "string"}
            },
            "required": ["query"]
        }
    },
    "WebFetch": {
        "name": "WebFetch",
        "description": "Fetch content from a URL.",
        "input_schema": {
            "type": "object",
            "properties": {
                "url": {"type": "string"},
                "prompt": {"type": "string"}
            },
            "required": ["url", "prompt"]
        }
    },
    "TodoWrite": {
        "name": "TodoWrite",
        "description": "Create or update a todo list.",
        "input_schema": {
            "type": "object",
            "properties": {
                "todos": {
                    "type": "array",
                    "items": {
                        "type": "object",
                        "properties": {
                            "content": {"type": "string"},
                            "status": {"type": "string"}
                        }
                    }
                }
            },
            "required": ["todos"]
        }
    },
}

# ── Test prompts per tool ────────────────────────────────────────────

TEST_PROMPTS = {
    "Read":     "Read the file /etc/hostname",
    "Write":    "Write 'hello' to /tmp/test_tool_compat.txt",
    "Edit":     "In /tmp/test_tool_compat.txt, replace 'hello' with 'world'",
    "Bash":     "Run the command: echo hello",
    "Grep":     "Search for 'error' in /tmp/test_tool_compat.log",
    "Glob":     "Find all .py files in /tmp",
    "WebSearch": "Search for Python documentation online",
    "WebFetch": "Fetch the content of https://example.com",
    "TodoWrite": "Create a todo item: Buy groceries",
}

# Expected keys in tool call arguments
EXPECTED_KEYS = {
    "Read":     ["file_path"],
    "Write":    ["file_path", "content"],
    "Edit":     ["file_path", "old_string", "new_string"],
    "Bash":     ["command"],
    "Grep":     ["pattern"],
    "Glob":     ["pattern"],
    "WebSearch": ["query"],
    "WebFetch": ["url"],
    "TodoWrite": ["todos"],
}

# Models to test
MODELS_TO_TEST = [
    ("deepseek-v4-pro",  "openai"),
    ("deepseek-v4-flash","openai"),
    ("kimi-k2.6",        "openai"),
    ("glm-5.1",          "openai"),
    ("mimo-v2.5",        "openai"),
    ("mimo-v2-pro",      "openai"),
    ("minimax-m2.5",     "anthropic"),
    ("minimax-m2.7",     "anthropic"),
    ("qwen3.5-plus",     "anthropic"),
    ("qwen3.6-plus",     "anthropic"),
]


class ToolCompatibilityTester:
    """Test which tools each model accepts and can invoke."""

    def __init__(self, proxy_base: str = "http://localhost:4000", delay: float = 1.0):
        self.proxy_base = proxy_base.rstrip("/")
        self.delay = delay
        self.client = httpx.AsyncClient(timeout=60)

    async def _send_request(self, model: str, tools: list, prompt: str) -> dict:
        """Send an Anthropic-format request to /v1/messages."""
        body = {
            "model": model,
            "max_tokens": 1024,
            "tools": tools,
            "messages": [
                {"role": "user", "content": prompt}
            ],
        }
        try:
            resp = await self.client.post(
                f"{self.proxy_base}/v1/messages",
                json=body,
                headers={
                    "content-type": "application/json",
                    "x-api-key": "test-key",
                    "anthropic-version": "2023-06-01",
                },
            )
            return {"status": resp.status_code, "body": resp.json() if resp.status_code == 200 else {"error": resp.text[:500]}}
        except httpx.TimeoutException:
            return {"status": 408, "body": {"error": "timeout"}}
        except Exception as e:
            return {"status": 0, "body": {"error": str(e)}}

    def _check_tool_use(self, response_body: dict, tool_name: str) -> dict:
        """Check if the response contains a valid tool_use call."""
        content = response_body.get("content", [])
        if not isinstance(content, list):
            return {"called": False, "args_valid": None, "error": "No content array in response"}

        for block in content:
            if not isinstance(block, dict):
                continue
            if block.get("type") == "tool_use" and block.get("name") == tool_name:
                # Found a tool_use block for our tool
                try:
                    args = block.get("input", {})
                    if not isinstance(args, dict):
                        return {"called": True, "args_valid": False, "error": "input is not a dict"}

                    expected = EXPECTED_KEYS.get(tool_name, [])
                    missing = [k for k in expected if k not in args]
                    if missing:
                        return {"called": True, "args_valid": False, "error": f"missing keys: {missing}"}
                    return {"called": True, "args_valid": True, "error": None}
                except Exception as e:
                    return {"called": True, "args_valid": False, "error": str(e)}

        # Check stop_reason
        stop = response_body.get("stop_reason", "")
        if stop == "tool_use":
            # There might be tool_use blocks we didn't match by name
            for block in content:
                if isinstance(block, dict) and block.get("type") == "tool_use":
                    return {"called": True, "args_valid": None, "error": f"tool called but name mismatch: got '{block.get('name')}' expected '{tool_name}'"}

        return {"called": False, "args_valid": None, "error": "No tool_use block found in response"}

    async def test_single_tool(self, model: str, tool_name: str) -> dict:
        """Test a single tool with a model."""
        tool_def = TOOL_DEFINITIONS.get(tool_name)
        prompt = TEST_PROMPTS.get(tool_name)
        if not tool_def or not prompt:
            return {"tool": tool_name, "accepted": False, "called": False, "args_valid": None, "error": "Unknown tool"}

        result = await self._send_request(model, [tool_def], prompt)

        if result["status"] != 200:
            return {
                "tool": tool_name,
                "accepted": False,
                "called": False,
                "args_valid": None,
                "error": f"HTTP {result['status']}: {result['body'].get('error', '')[:200]}",
            }

        body = result["body"]
        # Check for API-level errors
        if "error" in body:
            return {
                "tool": tool_name,
                "accepted": True,
                "called": False,
                "args_valid": None,
                "error": f"API error: {body['error']}",
            }

        tool_check = self._check_tool_use(body, tool_name)
        return {
            "tool": tool_name,
            "accepted": True,
            "called": tool_check["called"],
            "args_valid": tool_check["args_valid"],
            "error": tool_check.get("error"),
        }

    async def test_model(self, model: str, tools: list[str] | None = None) -> dict:
        """Test all tools (or a subset) with a model."""
        tool_names = tools or list(TOOL_DEFINITIONS.keys())
        results = []
        for i, tool_name in enumerate(tool_names):
            print(f"    [{i+1}/{len(tool_names)}] Testing {tool_name}...", end=" ", flush=True)
            result = await self.test_single_tool(model, tool_name)
            status = "OK" if result["called"] else ("PARTIAL" if result["accepted"] else "FAIL")
            print(f"{status} (accepted={result['accepted']}, called={result['called']}, args={result['args_valid']})")
            if result.get("error"):
                print(f"         error: {result['error'][:100]}")
            results.append(result)
            if i < len(tool_names) - 1:
                await asyncio.sleep(self.delay)
        return {"model": model, "results": results}

    async def run_all(self, models: list[tuple[str, str]] | None = None, output_path: str = "tool_compat_results.json") -> dict:
        """Run tests for all models and save results."""
        models = models or MODELS_TO_TEST
        all_results = {
            "timestamp": datetime.now(timezone.utc).isoformat(),
            "proxy": self.proxy_base,
            "models": {},
        }

        for model, protocol in models:
            print(f"\n{'='*60}")
            print(f"Testing model: {model} (protocol: {protocol})")
            print(f"{'='*60}")
            try:
                result = await self.test_model(model)
                all_results["models"][model] = {
                    "protocol": protocol,
                    "results": result["results"],
                }
            except Exception as e:
                print(f"  ERROR: {e}")
                all_results["models"][model] = {
                    "protocol": protocol,
                    "results": [],
                    "error": str(e),
                }
            await asyncio.sleep(2)  # pause between models

        # Save results
        with open(output_path, "w", encoding="utf-8") as f:
            json.dump(all_results, f, indent=2, ensure_ascii=False)
        print(f"\n{'='*60}")
        print(f"Results saved to {output_path}")

        # Print summary
        self._print_summary(all_results)
        return all_results

    def _print_summary(self, results: dict):
        """Print a summary matrix."""
        print(f"\n{'='*60}")
        print("SUMMARY: Tool Compatibility Matrix")
        print(f"{'='*60}")

        # Collect all tool names
        all_tools = set()
        for model_data in results["models"].values():
            for r in model_data.get("results", []):
                all_tools.add(r["tool"])
        all_tools = sorted(all_tools)

        # Header
        header = f"{'Model':<25}" + "".join(f"{t[:8]:>9}" for t in all_tools)
        print(header)
        print("-" * len(header))

        # Rows
        for model_name, model_data in results["models"].items():
            row = f"{model_name:<25}"
            tool_results = {r["tool"]: r for r in model_data.get("results", [])}
            for tool in all_tools:
                r = tool_results.get(tool)
                if r is None:
                    cell = "   -"
                elif r.get("called"):
                    cell = "   OK"
                elif r.get("accepted"):
                    cell = "   ~"  # accepted but didn't call
                else:
                    cell = "   NO"
                row += f"{cell:>9}"
            print(row)


def main():
    parser = argparse.ArgumentParser(description="Test tool compatibility across models via opencode-proxy")
    parser.add_argument("--proxy", default="http://localhost:4000", help="Proxy base URL")
    parser.add_argument("--output", default="tool_compat_results.json", help="Output file path")
    parser.add_argument("--delay", type=float, default=1.0, help="Delay between requests (seconds)")
    parser.add_argument("--model", action="append", help="Test only specific model(s)")
    parser.add_argument("--tool", action="append", help="Test only specific tool(s)")
    args = parser.parse_args()

    models = MODELS_TO_TEST
    if args.model:
        models = [(m, p) for m, p in MODELS_TO_TEST if m in args.model]
        if not models:
            print(f"ERROR: No matching models found for: {args.model}")
            print(f"Available: {[m for m, _ in MODELS_TO_TEST]}")
            sys.exit(1)

    tester = ToolCompatibilityTester(proxy_base=args.proxy, delay=args.delay)
    asyncio.run(tester.run_all(models=models, output_path=args.output))


if __name__ == "__main__":
    main()
