"""Registry hostname ordering for the fresh-chain repair (T162 slice).

Extracted from ``scripts/fresh_chain.py``: reads the shared registry and
returns hostnames with live egress evidence, optionally restricted to the
transport actually being tried.  No network, no Docker.
"""

from __future__ import annotations

import sqlite3
import time
from pathlib import Path


def registry_hosts(root: str | Path, transport: str | None = None) -> list[str]:
    """Known hostnames from the quota inventory, best quota first.

    ``transport`` limits the 200 evidence to the transport actually being
    tried: a host proven only on WireGuard must not outrank the field for an
    OpenVPN-TCP repair.
    """

    path = Path(root) / "logs" / "registry.db"
    if not path.exists():
        return []
    now = time.time()
    wanted = (
        None
        if str(transport or "").strip().lower() in ("", "auto")
        else str(transport).strip().lower()
    )
    try:
        with sqlite3.connect(path) as conn:
            rows = list(
                conn.execute(
                    "select s.hostname, "
                    "max(case when e.last_ok_at is not null then 1 else 0 end) as has_200, "
                    "max(e.last_ok_at) as ok_at "
                    "from servers s "
                    "left join ip_server_map m on m.hostname = s.hostname "
                    "left join egress_ips e on e.ip = m.ip "
                    "where s.hostname != '' "
                    "and (? is null or m.transport = ?) "
                    "and (e.last_ok_at is not null "
                    "or e.state in ('fresh','candidate','assigned')) "
                    "and coalesce(e.reset_at, 0) <= ? "
                    "group by s.hostname "
                    "order by has_200 desc, ok_at desc",
                    (wanted, wanted, now),
                )
            )
        return [str(row[0]) for row in rows if row and row[0]]
    except Exception:
        return []


__all__ = ["registry_hosts"]
