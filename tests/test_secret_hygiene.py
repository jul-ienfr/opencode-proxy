"""Secret hygiene: env files ignored, no literal keys in source."""

from __future__ import annotations

import re
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
_SKIP = {".git", "logs", "node_modules", "__pycache__", ".venv", "venv"}
_LITERAL_KEY = re.compile(
    r"(?:VPN_CONTROL_API_KEY|OPENVPN_PASSWORD|WIREGUARD_PRIVATE_KEY)"
    r"=[A-Za-z0-9+/=_-]{16,}"
)


def test_secret_env_files_are_gitignored():
    ignored = (ROOT / ".gitignore").read_text(encoding="utf-8")
    for name in (".env", "credentials.env", "vpn/credentials.txt"):
        assert name in ignored, name


def test_no_literal_secret_in_source_files():
    hits: list[str] = []
    for path in ROOT.rglob("*.py"):
        if any(part in _SKIP for part in path.relative_to(ROOT).parts):
            continue
        try:
            text = path.read_text(encoding="utf-8", errors="replace")
        except OSError:
            continue
        if _LITERAL_KEY.search(text):
            hits.append(str(path.relative_to(ROOT)))
    assert hits == []
