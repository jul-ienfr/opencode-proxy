"""Release evidence manifest for feature 014.

The manifest is built only when the canary passed and every checklist item is
checked.  File hashes make the archived evidence tamper-evident without
requiring a signing key in the repository.
"""

from __future__ import annotations

import hashlib
from typing import Any


def _checklist_counts(text: str) -> tuple[int, int]:
    checked = 0
    unchecked = 0
    for line in text.splitlines():
        stripped = line.lstrip()
        if stripped.startswith("- [x]") or stripped.startswith("- [X]"):
            checked += 1
        elif stripped.startswith("- [ ]"):
            unchecked += 1
    return checked, unchecked


def build_release_manifest(
    *,
    canary: dict[str, Any],
    checklist_text: str,
    files: dict[str, bytes],
    generated_at: str,
) -> dict[str, Any]:
    if not bool(canary.get("passed")):
        raise ValueError("canary non validé : preuves live manquantes")
    checked, unchecked = _checklist_counts(checklist_text)
    if unchecked:
        raise ValueError(f"checklist incomplète : {unchecked} case(s) non cochée(s)")
    if not files:
        raise ValueError("aucune preuve à archiver")

    hashed = {
        name: {
            "sha256": hashlib.sha256(payload).hexdigest(),
            "size": len(payload),
        }
        for name, payload in sorted(files.items())
    }
    return {
        "feature": "014-nordvpn-exhaustive-rotation",
        "generated_at": generated_at,
        "passed": True,
        "checklist": {"checked": checked, "unchecked": unchecked},
        "canary": {
            "passed": True,
            "generated_at": canary.get("generated_at"),
            "api_base": canary.get("api_base"),
            "target": canary.get("target"),
            "evidence": canary.get("evidence") or {},
        },
        "files": hashed,
    }


__all__ = ["build_release_manifest"]
