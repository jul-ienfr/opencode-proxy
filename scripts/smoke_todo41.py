"""In-process smoke test for Vague 2 finding [41]: _CurlCffiStreamResponse.aiter_lines.

curl_cffi 0.14.0's aiter_lines() yields BYTES lines (and the consumers call
line.startswith("data:") with a str -> TypeError "startswith first arg must
be bytes"). The wrapper now splits SSE lines manually from aiter_content()
and yields httpx-style STR lines.

Run: python scripts/smoke_todo41.py
"""

import asyncio
import json
import sys

sys.path.insert(0, ".")


def _chunks(payload: bytes, cut: list) -> list:
    """Split payload at arbitrary byte offsets (mid-token, mid-\r\n...)."""
    out, prev = [], 0
    for c in cut:
        out.append(payload[prev:c])
        prev = c
    out.append(payload[prev:])
    return [c for c in out if c]


async def main():
    import opencode as oc

    payload = (
        b'data: {"id":"x","choices":[{"delta":{"content":"Hel"}}]}\r\n'
        b"\r\n"
        b'data: {"choices":[{"delta":{"content":"lo"}}]}\r\n'
        b"\r\n"
        b"data: [DONE]\r\n"
        b"\r\n"
    )

    class _FakeResp:
        def __init__(self, chunks):
            self._chunks = chunks
            self.status_code = 200
            self.headers = {}

        async def aiter_content(self):
            for c in self._chunks:
                yield c

    # Chunk boundaries land mid-token and BETWEEN \r and \n.
    mid = [
        i for i in range(3, len(payload) - 3) if payload[i - 1 : i + 1] == b"\r\n"
    ]  # cut between \r and \n
    mid += [3, 15, 40, 52, 66, 79, 90, 105]
    chunks = _chunks(payload, sorted(set(mid)))
    assert any(c.endswith(b"\r") and n.startswith(b"\n") for c, n in zip(chunks, chunks[1:], strict=False)), (
        "test must split \\r from \\n across chunks"
    )

    wrapped = oc._CurlCffiStreamResponse(_FakeResp(chunks))
    lines = [l async for l in wrapped.aiter_lines()]

    # ---- every line is a str (the TypeError fix) ----
    assert all(isinstance(l, str) for l in lines), f"non-str line: {lines!r}"
    print("PASS all lines are str")

    # ---- SSE framing intact ----
    data_lines = [l for l in lines if l.startswith("data:")]
    assert len(data_lines) == 3, f"expected 3 data: lines, got {len(data_lines)}: {lines!r}"
    assert json.loads(data_lines[0][5:].strip())["choices"][0]["delta"]["content"] == "Hel"
    assert json.loads(data_lines[1][5:].strip())["choices"][0]["delta"]["content"] == "lo"
    assert data_lines[2][5:].strip() == "[DONE]"
    print("PASS SSE framing + JSON payloads + [DONE]")

    # ---- trailing partial line without final newline is yielded ----
    partial = b'data: {"choices":[{"delta":{"content":"unfinished'
    wrapped2 = oc._CurlCffiStreamResponse(_FakeResp([partial]))
    lines2 = [l async for l in wrapped2.aiter_lines()]
    assert len(lines2) == 1 and lines2[0].startswith("data:") and "unfinished" in lines2[0], lines2
    print("PASS trailing partial line")

    # ---- empty chunk boundaries (double \n\n) yield empty lines, consumers skip them ----
    assert lines[1] == "" and lines[3] == "" and lines[5] == "", lines
    print("PASS blank separator lines")

    # ---- consumer contract: what 4075/4619 do with each line ----
    consumed = 0
    for line in lines:
        if not line.startswith("data:"):
            continue
        d = line[5:].strip()
        if d == "[DONE]":
            consumed += 1
            continue
        chunk = json.loads(d)  # 4107-style parse
        if chunk.get("choices"):
            consumed += 1
    assert consumed == 3, f"consumer parse failed: {consumed}/3"
    print("PASS consumer parse contract (startswith/json.loads/[DONE])")

    print("\nALL TODO-41 SMOKE TESTS PASSED")


if __name__ == "__main__":
    asyncio.run(main())
