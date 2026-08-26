"""
app — extraction progressive du god file opencode.py 8767l (Phase3 F-H2)

opencode.py reste l'entrypoint (re-export) — chaque sous-package est extrait
PR par PR, tests verts entre, DI via app.state (FastAPI state).

Structure:
- app/router      → _route_for, get_model_config @lru_cache 512
- app/protocol    → protocol_mapping.py converters (re-export)
- app/streaming   → SSE handlers, StreamingResponse, _sse_keepalive
- app/db          → SQLite WAL, requests.db batch Queue 10000/batch32/timeout50ms
- app/quotas      → quota tracking, _free_cooldown, _free_attempt

Voir docs/adr/ADR-004 et docs/perf.md
"""

__all__ = []
