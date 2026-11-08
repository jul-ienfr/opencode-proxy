"""Middleware SSE : guérit les alias de chemin et de nom des tool_calls.

Placé sur les routes d'inférence, il réécrit chaque ligne ``data: {...}``
complète avant qu'elle ne parte au client. Il ne touche jamais aux lignes
non-SSE, aux erreurs, ni aux réponses JSON (leurs content-length seraient
invalides) ; le chemin non-stream est guéri dans les handlers.
"""

from __future__ import annotations

from .tool_paths import heal_sse_data_line

_RETRY_PATHS = (
    "/v1/chat/completions",
    "/v1/messages",
    "/anthropic/v1/messages",
    "/v1/responses",
)


class ToolPathHealingMiddleware:
    def __init__(self, app):
        self.app = app

    @staticmethod
    def _candidate(scope: dict) -> bool:
        if scope.get("type") != "http":
            return False
        if str(scope.get("method") or "").upper() != "POST":
            return False
        path = str(scope.get("path") or "")
        return any(path.startswith(prefix) for prefix in _RETRY_PATHS)

    @staticmethod
    def _content_type(headers) -> str:
        for key, value in headers or []:
            try:
                if bytes(key).lower() == b"content-type":
                    return bytes(value).decode("latin-1", "replace").lower()
            except Exception:
                continue
        return ""

    async def __call__(self, scope, receive, send):
        if not self._candidate(scope):
            await self.app(scope, receive, send)
            return

        content_type = ""
        buffer = b""
        tool_names: dict[int, str] = {}

        async def send_wrapper(message):
            nonlocal content_type, buffer
            mtype = message.get("type")
            if mtype == "http.response.start":
                content_type = self._content_type(message.get("headers"))
                await send(message)
                return
            if mtype != "http.response.body":
                await send(message)
                return
            body = bytes(message.get("body") or b"")
            more = bool(message.get("more_body"))
            if "text/event-stream" not in content_type:
                await send(message)
                return
            buffer += body
            lines = buffer.split(b"\n")
            buffer = lines.pop() if lines else b""
            if lines:
                out = b"\n".join(
                    heal_sse_data_line(line, tool_names) for line in lines
                ) + b"\n"
                await send(
                    {"type": "http.response.body", "body": out, "more_body": more or bool(buffer)}
                )
                if not more and not buffer:
                    return
            if not more:
                await send(
                    {
                        "type": "http.response.body",
                        "body": heal_sse_data_line(buffer, tool_names) if buffer else b"",
                        "more_body": False,
                    }
                )
                buffer = b""

        await self.app(scope, receive, send_wrapper)


__all__ = ["ToolPathHealingMiddleware"]
