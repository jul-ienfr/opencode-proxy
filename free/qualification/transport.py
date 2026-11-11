"""Transport planning for automatic and manual NordVPN strategies.

In ``auto`` the planner walks every transport supported by the hostname:

    wireguard -> openvpn-udp -> openvpn-tcp -> wireguard (round 2)

An infrastructure failure advances to the next transport.  A 429 also advances:
another transport can yield a different egress IP and a different quota bucket.
``manual`` keeps exactly one transport.  The cursor and round are persisted by
the store, so a restart resumes mid-cycle.
"""

from __future__ import annotations

from dataclasses import dataclass, replace
from typing import Any

from .domain import AttemptStatus

DEFAULT_TRANSPORT_ORDER: tuple[str, ...] = ("wireguard", "openvpn-udp", "openvpn-tcp")
KNOWN_TRANSPORTS = frozenset(DEFAULT_TRANSPORT_ORDER)
STOP_ATTEMPT_STATUSES = frozenset(
    {
        AttemptStatus.OK,
        AttemptStatus.AUTH,
        AttemptStatus.DEAD,
        AttemptStatus.SKIPPED,
    }
)
ADVANCE_ATTEMPT_STATUSES = frozenset(
    {
        AttemptStatus.QUOTA,
        AttemptStatus.NO_EGRESS,
        AttemptStatus.BIND_ERROR,
        AttemptStatus.TIMEOUT,
    }
)


@dataclass(frozen=True, slots=True)
class TransportPlan:
    """Immutable snapshot of a hostname's transport cycle."""

    order: tuple[str, ...]
    cursor: int = 0
    round: int = 0
    max_rounds: int = 2
    strategy: str = "auto"
    manual: str = ""
    done: bool = False
    exhausted: bool = False

    @property
    def current(self) -> str | None:
        if self.done or self.exhausted:
            return None
        if 0 <= self.cursor < len(self.order):
            return self.order[self.cursor]
        return None

    @property
    def attempts_done(self) -> int:
        return self.round * max(1, len(self.order)) + max(0, self.cursor)

    @property
    def total_attempts(self) -> int:
        return max(0, len(self.order)) * max(1, int(self.max_rounds))

    def advance(self, attempt: AttemptStatus) -> TransportPlan:
        """Return the next plan after ``attempt``.

        ``OK``/``AUTH``/``DEAD``/``SKIPPED`` stop the cycle.  ``QUOTA`` and
        infrastructure failures advance; after the last transport the plan
        wraps to the first for the next round, up to ``max_rounds``.
        """

        if self.done or self.exhausted:
            return self
        if attempt in STOP_ATTEMPT_STATUSES:
            return replace(self, done=True)
        if attempt not in ADVANCE_ATTEMPT_STATUSES:
            # Unknown result: advance safely, never loop forever.
            pass
        cursor = self.cursor + 1
        round_no = self.round
        if cursor >= len(self.order):
            round_no += 1
            cursor = 0
        if not self.order:
            return replace(self, done=True, exhausted=True)
        if round_no >= max(1, int(self.max_rounds)):
            return replace(self, cursor=0, round=round_no, done=True, exhausted=True)
        return replace(self, cursor=cursor, round=round_no)

    def snapshot(self) -> dict[str, Any]:
        return {
            "order": list(self.order),
            "cursor": self.cursor,
            "round": self.round,
            "current": self.current,
            "max_rounds": self.max_rounds,
            "strategy": self.strategy,
            "manual": self.manual,
            "done": self.done,
            "exhausted": self.exhausted,
        }


def build_plan(
    supported: tuple[str, ...] | list[str] | None,
    *,
    strategy: str = "auto",
    manual: str = "",
    order: tuple[str, ...] | list[str] | None = None,
    max_rounds: int = 2,
    cursor: int = 0,
    round_no: int = 0,
) -> TransportPlan:
    """Build a plan from the hostname's supported transports.

    ``supported`` is the set declared by the catalog; an empty set means the
    hostname cannot be qualified and the plan is empty.  ``manual`` is honored
    only in ``manual`` strategy and only if supported.
    """

    declared = tuple(str(item).strip().lower() for item in (supported or ()) if str(item).strip())
    declared = tuple(dict.fromkeys(item for item in declared if item in KNOWN_TRANSPORTS))
    preferred = tuple(str(item).strip().lower() for item in (order or DEFAULT_TRANSPORT_ORDER))
    preferred = tuple(dict.fromkeys(item for item in preferred if item in KNOWN_TRANSPORTS))
    if not preferred:
        preferred = DEFAULT_TRANSPORT_ORDER

    mode = str(strategy or "auto").strip().lower()
    if mode == "manual":
        chosen = str(manual or "").strip().lower()
        selected: tuple[str, ...] = (chosen,) if chosen and chosen in declared else ()
        return TransportPlan(
            order=selected,
            cursor=0,
            round=max(0, int(round_no)),
            max_rounds=max(1, int(max_rounds)),
            strategy="manual",
            manual=chosen,
            done=not bool(selected),
            exhausted=not bool(selected),
        )

    selected = tuple(item for item in preferred if item in declared)
    if not selected:
        return TransportPlan(
            order=(),
            cursor=0,
            round=0,
            max_rounds=max(1, int(max_rounds)),
            strategy="auto",
            done=True,
            exhausted=True,
        )
    safe_round = max(0, int(round_no))
    safe_cursor = max(0, min(int(cursor), len(selected) - 1))
    exhausted = safe_round >= max(1, int(max_rounds))
    return TransportPlan(
        order=selected,
        cursor=safe_cursor,
        round=safe_round,
        max_rounds=max(1, int(max_rounds)),
        strategy="auto",
        done=exhausted,
        exhausted=exhausted,
    )


def next_transport(plan: TransportPlan, attempt: AttemptStatus) -> str | None:
    """Convenience helper: advance and return the next transport."""

    return plan.advance(attempt).current


__all__ = [
    "ADVANCE_ATTEMPT_STATUSES",
    "DEFAULT_TRANSPORT_ORDER",
    "KNOWN_TRANSPORTS",
    "STOP_ATTEMPT_STATUSES",
    "TransportPlan",
    "build_plan",
    "next_transport",
]
