#!/usr/bin/env python3
"""Archive and hash the feature-014 release evidence.

Refuses to produce a manifest while the canary has not passed or while the
release checklist still has unchecked items.
"""

from __future__ import annotations

import argparse
import hashlib
import json
from datetime import UTC, datetime
from pathlib import Path

from free.qualification.release import build_release_manifest

ROOT = Path(__file__).resolve().parent.parent


def _read(path: Path) -> bytes:
    return path.read_bytes()


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--canary", default=str(ROOT / "logs" / "canary-014.json"))
    parser.add_argument(
        "--checklist",
        default=str(
            ROOT
            / "specs"
            / "014-nordvpn-exhaustive-rotation"
            / "release-checklist.md"
        ),
    )
    parser.add_argument(
        "--report",
        default=str(
            ROOT
            / "specs"
            / "014-nordvpn-exhaustive-rotation"
            / "release-report.md"
        ),
    )
    parser.add_argument("--output", default=str(ROOT / "logs" / "release-014-evidence.json"))
    args = parser.parse_args(argv)

    canary_path = Path(args.canary)
    checklist_path = Path(args.checklist)
    report_path = Path(args.report)
    canary = json.loads(canary_path.read_text(encoding="utf-8"))
    checklist_text = checklist_path.read_text(encoding="utf-8")
    files = {
        canary_path.name: _read(canary_path),
        checklist_path.name: _read(checklist_path),
        report_path.name: _read(report_path),
    }
    manifest = build_release_manifest(
        canary=canary,
        checklist_text=checklist_text,
        files=files,
        generated_at=datetime.now(UTC).isoformat(),
    )
    payload = json.dumps(manifest, ensure_ascii=False, indent=2).encode("utf-8")
    output = Path(args.output)
    output.parent.mkdir(parents=True, exist_ok=True)
    output.write_bytes(payload)
    digest = hashlib.sha256(payload).hexdigest()
    output.with_suffix(output.suffix + ".sha256").write_text(
        f"{digest}  {output.name}\n",
        encoding="utf-8",
    )
    print(json.dumps(manifest, ensure_ascii=False, indent=2))
    print(f"manifest sha256: {digest}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
