"""Machine à états des IP d'egress — 023-client-usable-egress (T004).

Une IP a un état unique, porté par ``registry.db`` :

    unproven -> proven -> serving
                    \\-> spent   (429 quota, reset_at fournisseur)
                    \\-> burned  (429 format sur IP non prouvée, >= 24 h)
                    \\-> mismatch (deux lecteurs concordants)

Invariants (constitution I, IV, V, VII, XVIII) :

* ``burned`` interdit toute sélection et toute sonde avant
  ``max(burned_at + 24 h, reset_at)`` ;
* ``spent`` interdit la sélection avant ``reset_at`` ;
* ``proven``/``serving`` exigent un ``wire_fp`` et ``proven_at`` ;
* aucun passage à ``proven`` sans preuve 200 conservée.

Le module est volontairement sans I/O réseau : il ne lit que SQLite.
"""

from __future__ import annotations

import sqlite3
import threading
import time
from contextlib import contextmanager
from dataclasses import dataclass
from pathlib import Path
from typing import Any

UNPROVEN = "unproven"
PROVEN = "proven"
SERVING = "serving"
SPENT = "spent"
BURNED = "burned"
MISMATCH = "mismatch"

STATES: frozenset[str] = frozenset(
    {UNPROVEN, PROVEN, SERVING, SPENT, BURNED, MISMATCH}
)

#: Une IP brûlée par un format non conforme reste exclue au moins 24 h.
BURN_SECONDS = 24 * 3600.0

_SELECTABLE = frozenset({PROVEN, SERVING})
_TERMINAL = frozenset({SPENT, BURNED, MISMATCH})

_SCHEMA = """
CREATE TABLE IF NOT EXISTS egress_ip_state (
    provider TEXT NOT NULL,
    ip TEXT NOT NULL,
    bucket_key TEXT NOT NULL DEFAULT '',
    state TEXT NOT NULL,
    wire_fp TEXT NOT NULL DEFAULT '',
    proven_at REAL,
    serving_at REAL,
    reset_at REAL,
    burned_at REAL,
    reason TEXT NOT NULL DEFAULT '',
    evidence_ref TEXT NOT NULL DEFAULT '',
    probe_count INTEGER NOT NULL DEFAULT 0,
    updated_at REAL NOT NULL,
    PRIMARY KEY (provider, ip)
)
"""

_SCHEMA_INDEXES = (
    "CREATE INDEX IF NOT EXISTS idx_egress_ip_state_state ON egress_ip_state(state)",
    "CREATE INDEX IF NOT EXISTS idx_egress_ip_state_bucket ON egress_ip_state(provider, bucket_key)",
    "CREATE INDEX IF NOT EXISTS idx_egress_ip_state_reset ON egress_ip_state(reset_at)",
)

_SCHEMA_EVENTS = """
CREATE TABLE IF NOT EXISTS egress_ip_state_events (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    created_at REAL NOT NULL,
    kind TEXT NOT NULL,
    provider TEXT NOT NULL DEFAULT '',
    ip TEXT NOT NULL DEFAULT '',
    bucket_key TEXT NOT NULL DEFAULT '',
    wire_fp TEXT NOT NULL DEFAULT '',
    reason TEXT NOT NULL DEFAULT '',
    evidence_ref TEXT NOT NULL DEFAULT '',
    payload TEXT NOT NULL DEFAULT ''
)
"""

_SCHEMA_EVENTS_INDEX = (
    "CREATE INDEX IF NOT EXISTS idx_egress_ip_state_events_time "
    "ON egress_ip_state_events(created_at)",
)


@dataclass
class IPState:
    provider: str
    ip: str
    state: str = UNPROVEN
    bucket_key: str = ""
    wire_fp: str = ""
    proven_at: float | None = None
    serving_at: float | None = None
    reset_at: float | None = None
    burned_at: float | None = None
    reason: str = ""
    evidence_ref: str = ""
    probe_count: int = 0
    updated_at: float = 0.0

    def is_selectable(self, now: float | None = None, *, wire_fp: str | None = None) -> bool:
        """True si l'IP peut servir du trafic client à ``now``."""
        if self.state not in _SELECTABLE:
            return False
        if wire_fp and self.wire_fp and wire_fp != self.wire_fp:
            return False
        return True

    def reset_elapsed(self, now: float | None = None) -> bool:
        """True si le reset/burn est écoulé (re-preuve possible)."""
        ts = time.time() if now is None else now
        if self.state == SPENT:
            return bool(self.reset_at and ts >= self.reset_at)
        if self.state == BURNED:
            floor = max((self.burned_at or 0.0) + BURN_SECONDS, self.reset_at or 0.0)
            return ts >= floor
        return self.state == MISMATCH


def validate_transition(
    current: str,
    new: str,
    *,
    wire_fp: str = "",
    reset_at: float | None = None,
    evidence_ref: str = "",
) -> None:
    """Lève ``ValueError`` si la transition viole un invariant."""
    if new not in STATES:
        raise ValueError(f"unknown state: {new!r}")
    if current not in STATES:
        raise ValueError(f"unknown current state: {current!r}")
    if new in (PROVEN, SERVING) and not wire_fp:
        raise ValueError("proven/serving requires a wire_fp")
    if new == SPENT and not reset_at:
        raise ValueError("spent requires reset_at")
    if new == MISMATCH and not evidence_ref:
        raise ValueError("mismatch requires an evidence_ref")
    if current in _TERMINAL and new != UNPROVEN:
        raise ValueError(f"terminal state {current} can only return to unproven")


def _row_to_state(row: sqlite3.Row | tuple) -> IPState:
    return IPState(
        provider=row[0],
        ip=row[1],
        bucket_key=row[2],
        state=row[3],
        wire_fp=row[4],
        proven_at=row[5],
        serving_at=row[6],
        reset_at=row[7],
        burned_at=row[8],
        reason=row[9],
        evidence_ref=row[10],
        probe_count=row[11],
        updated_at=row[12],
    )


class IPStateStore:
    """Persistance des états d'IP dans ``registry.db``."""

    def __init__(self, path: str | Path | None = None) -> None:
        if path is None:
            from free.ip_registry.registry_db import registry_db_path

            path = registry_db_path()
        self.path = Path(path)
        self._lock = threading.RLock()
        self.path.parent.mkdir(parents=True, exist_ok=True)
        repaired: list[tuple[str, str]] = []
        with self._conn() as conn:
            conn.execute(_SCHEMA)
            for stmt in _SCHEMA_INDEXES:
                conn.execute(stmt)
            conn.execute(_SCHEMA_EVENTS)
            for stmt in _SCHEMA_EVENTS_INDEX:
                conn.execute(stmt)
            repaired = self._repair_invalid_burns(conn)
        if repaired:
            try:
                from free.probe_budget import ProbeBudget

                budget = ProbeBudget(self.path)
                for provider, bucket in repaired:
                    budget.reset(provider, bucket)
            except Exception:
                pass

    @contextmanager
    def _conn(self):
        conn = self._connect()
        try:
            yield conn
            conn.commit()
        finally:
            conn.close()

    def _connect(self) -> sqlite3.Connection:
        conn = sqlite3.connect(str(self.path), timeout=10)
        conn.execute("PRAGMA journal_mode=WAL")
        conn.execute("PRAGMA busy_timeout=5000")
        # [024 T310] Rename legacy ip_state (023 provider/ip schema) to egress_ip_state.
        try:
            cols = [row[1] for row in conn.execute("PRAGMA table_info(ip_state)")]
            if "provider" in cols:
                conn.execute("ALTER TABLE ip_state RENAME TO egress_ip_state")
        except Exception:
            pass
        return conn

    @staticmethod
    def _repair_invalid_burns(conn: sqlite3.Connection) -> list[tuple[str, str]]:
        """[2026-11-18] Corrige les faux burns « 429 non classé ».

        Le classifieur d'hier marquait `format` tout 429 sans motif ; 181 IP
        ont été brûlées à tort et bloquées 24 h, alors qu'un quota journalier
        se libère à minuit UTC. Ces lignes redeviennent `unproven` (re-preuve
        possible) ; un burn explicite (`FreeTierError`, rejet de forme) reste.
        """
        rows = conn.execute(
            "SELECT provider, ip, bucket_key FROM egress_ip_state WHERE state=? AND "
            "(reason LIKE 'unclassified 429%' OR reason LIKE "
            "'FreeUsageLimitError on an IP without%')",
            (BURNED,),
        ).fetchall()
        if not rows:
            return []
        ts = time.time()
        for provider, ip, bucket_key in rows:
            conn.execute(
                "UPDATE egress_ip_state SET state=?, burned_at=NULL, reason=?, updated_at=? "
                "WHERE provider=? AND ip=?",
                (
                    UNPROVEN,
                    "reclassified: quota 429, not a format burn",
                    ts,
                    provider,
                    ip,
                ),
            )
            conn.execute(
                "INSERT INTO egress_ip_state_events (created_at, kind, provider, ip, "
                "bucket_key, reason) VALUES (?,?,?,?,?,?)",
                (
                    ts,
                    "reclassified_burn",
                    provider,
                    ip,
                    bucket_key or "",
                    "false format burn corrected to unproven",
                ),
            )
        buckets = {(str(p), str(b)) for p, _ip, b in rows if b}
        return sorted(buckets)

    # -- lecture ---------------------------------------------------------
    def get(self, provider: str, ip: str) -> IPState | None:
        with self._lock, self._connect() as conn:
            row = conn.execute(
                "SELECT provider, ip, bucket_key, state, wire_fp, proven_at, "
                "serving_at, reset_at, burned_at, reason, evidence_ref, "
                "probe_count, updated_at FROM egress_ip_state "
                "WHERE provider=? AND ip=?",
                (provider, ip),
            ).fetchone()
        return _row_to_state(row) if row else None

    def get_any(self, ip: str) -> list[IPState]:
        """All rows for an IP across providers, newest-first. Indexed by PK."""
        with self._lock, self._connect() as conn:
            rows = conn.execute(
                "SELECT provider, ip, bucket_key, state, wire_fp, proven_at, "
                "serving_at, reset_at, burned_at, reason, evidence_ref, "
                "probe_count, updated_at FROM egress_ip_state "
                "WHERE ip=? ORDER BY updated_at DESC",
                (ip,),
            ).fetchall()
        return [_row_to_state(r) for r in rows]

    def list(self, provider: str | None = None) -> list[IPState]:
        sql = (
            "SELECT provider, ip, bucket_key, state, wire_fp, proven_at, "
            "serving_at, reset_at, burned_at, reason, evidence_ref, "
            "probe_count, updated_at FROM egress_ip_state"
        )
        args: tuple[Any, ...] = ()
        if provider:
            sql += " WHERE provider=?"
            args = (provider,)
        with self._lock, self._connect() as conn:
            rows = conn.execute(sql + " ORDER BY updated_at DESC", args).fetchall()
        return [_row_to_state(r) for r in rows]

    def counts(self, provider: str | None = None) -> dict[str, int]:
        sql = "SELECT state, COUNT(*) FROM egress_ip_state"
        args: tuple[Any, ...] = ()
        if provider:
            sql += " WHERE provider=?"
            args = (provider,)
        with self._lock, self._connect() as conn:
            rows = conn.execute(sql + " GROUP BY state", args).fetchall()
        counts = dict.fromkeys(STATES, 0)
        for state, n in rows:
            counts[str(state)] = int(n)
        return counts

    def selectable(
        self,
        provider: str | None = None,
        *,
        wire_fp: str | None = None,
        now: float | None = None,
    ) -> list[IPState]:
        return [
            st
            for st in self.list(provider)
            if st.is_selectable(now, wire_fp=wire_fp)
        ]

    # -- écriture --------------------------------------------------------
    def transition(
        self,
        provider: str,
        ip: str,
        new_state: str,
        *,
        wire_fp: str = "",
        bucket_key: str = "",
        reason: str = "",
        evidence_ref: str = "",
        reset_at: float | None = None,
        probe_count: int | None = None,
        now: float | None = None,
    ) -> IPState:
        ts = time.time() if now is None else now
        with self._lock:
            current = self.get(provider, ip)
            cur_state = current.state if current else UNPROVEN
            validate_transition(
                cur_state,
                new_state,
                wire_fp=wire_fp,
                reset_at=reset_at,
                evidence_ref=evidence_ref,
            )
            # [fix 2026-11-21] Un burn frais repart TOUJOURS de `ts`. Apres un
            # revive (terminal -> unproven), l'ancien `burned_at` survivait ;
            # comme `reset_elapsed()` calcule `burned_at + 24 h`, le nouveau
            # burn etait immediatement revivable (protection 24 h annulee).
            burned_at = (
                ts if new_state == BURNED else (current.burned_at if current else None)
            )
            # Une preuve fraiche rafraichit `proven_at` (fenetre de
            # reutilisation ``OPENCODE_PROOF_MAX_AGE_S``). SERVING ne re-mesure
            # rien : il conserve la preuve d'origine.
            proven_at = current.proven_at if current else None
            if new_state == PROVEN:
                proven_at = ts
            elif new_state == SERVING and proven_at is None:
                proven_at = ts
            serving_at = current.serving_at if current else None
            if new_state == SERVING:
                serving_at = ts
            eff_wire_fp = wire_fp or (current.wire_fp if current else "")
            eff_bucket = bucket_key or (current.bucket_key if current else "")
            eff_probe = (
                int(probe_count)
                if probe_count is not None
                else (current.probe_count if current else 0)
            )
            with self._conn() as conn:
                conn.execute(
                    """
                    INSERT INTO egress_ip_state (provider, ip, bucket_key, state,
                        wire_fp, proven_at, serving_at, reset_at, burned_at,
                        reason, evidence_ref, probe_count, updated_at)
                    VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?)
                    ON CONFLICT(provider, ip) DO UPDATE SET
                        bucket_key=excluded.bucket_key,
                        state=excluded.state,
                        wire_fp=excluded.wire_fp,
                        proven_at=excluded.proven_at,
                        serving_at=excluded.serving_at,
                        reset_at=excluded.reset_at,
                        burned_at=excluded.burned_at,
                        reason=excluded.reason,
                        evidence_ref=excluded.evidence_ref,
                        probe_count=excluded.probe_count,
                        updated_at=excluded.updated_at
                    """,
                    (
                        provider,
                        ip,
                        eff_bucket,
                        new_state,
                        eff_wire_fp,
                        proven_at,
                        serving_at,
                        reset_at if reset_at is not None else (current.reset_at if current else None),
                        burned_at,
                        reason,
                        evidence_ref,
                        eff_probe,
                        ts,
                    ),
                )
        return self.get(provider, ip)  # type: ignore[return-value]

    def revive_if_elapsed(
        self,
        provider: str,
        ip: str,
        *,
        now: float | None = None,
        reason: str = "reset elapsed: re-proof allowed",
    ) -> IPState | None:
        """Repasse une IP terminale à ``unproven`` quand son reset est écoulé.

        ``spent``/``burned``/``mismatch`` ne peuvent revenir qu'à ``unproven``
        (data-model 023). Sans cette étape, une tentative ``-> proven`` est
        refusée par ``validate_transition`` et l'IP reste bloquée après son
        reset, alors que ``reset_elapsed`` la déclare re-prouvable.
        """
        current = self.get(provider, ip)
        if current is None:
            return None
        if current.state in _TERMINAL and current.reset_elapsed(now):
            return self.transition(provider, ip, UNPROVEN, reason=reason, now=now)
        return current

    def note_quota(
        self,
        provider: str,
        ip: str,
        *,
        reset_at: float,
        reason: str = "quota 429: bucket exhausted",
        evidence_ref: str = "",
        now: float | None = None,
    ) -> IPState | None:
        """[024 Phase 1.5 / T316] Marque une IP ``spent`` jusqu'à ``reset_at``.

        Un ``429 FreeUsageLimitError`` est un quota d'IP, pas une brûlure de
        format : l'IP revient ``unproven`` à ``reset_at`` (principe IV). Idempotent
        et non raccourcissant : une punition en cours avec un ``reset_at`` plus
        tardif n'est jamais réduite. Retourne l'état résultant (ou ``None`` si
        les arguments sont insuffisants). Fail-soft côté appelant.
        """
        prov = str(provider or "").strip()
        ip_s = str(ip or "").strip()
        if not prov or not ip_s or not reset_at:
            return None
        ts = time.time() if now is None else now
        current = self.get(prov, ip_s)
        if current is not None and current.state in _TERMINAL:
            if not current.reset_elapsed(ts):
                # Punition en cours : on ne fait que l'étendre, jamais la réduire.
                if current.state == SPENT and float(current.reset_at or 0.0) < float(reset_at):
                    return self.transition(
                        prov, ip_s, SPENT,
                        reset_at=reset_at, reason=reason,
                        evidence_ref=evidence_ref, now=ts,
                    )
                return current
            # Reset écoulé : la machine à états exige un passage par ``unproven``.
            self.transition(
                prov, ip_s, UNPROVEN,
                reason="reset elapsed before re-quota", now=ts,
            )
        return self.transition(
            prov, ip_s, SPENT,
            reset_at=reset_at, reason=reason,
            evidence_ref=evidence_ref, now=ts,
        )

    def note_probe(self, provider: str, ip: str, *, now: float | None = None) -> int:
        ts = time.time() if now is None else now
        with self._lock, self._connect() as conn:
            conn.execute(
                "UPDATE egress_ip_state SET probe_count=probe_count+1, updated_at=? "
                "WHERE provider=? AND ip=?",
                (ts, provider, ip),
            )
            row = conn.execute(
                "SELECT probe_count FROM egress_ip_state WHERE provider=? AND ip=?",
                (provider, ip),
            ).fetchone()
        return int(row[0]) if row else 0

    def record_event(
        self,
        kind: str,
        *,
        provider: str = "",
        ip: str = "",
        bucket_key: str = "",
        wire_fp: str = "",
        reason: str = "",
        evidence_ref: str = "",
        payload: str = "",
        now: float | None = None,
    ) -> int:
        ts = time.time() if now is None else now
        with self._lock, self._connect() as conn:
            cur = conn.execute(
                "INSERT INTO egress_ip_state_events (created_at, kind, provider, ip, "
                "bucket_key, wire_fp, reason, evidence_ref, payload) "
                "VALUES (?,?,?,?,?,?,?,?,?)",
                (
                    ts,
                    kind,
                    provider,
                    ip,
                    bucket_key,
                    wire_fp,
                    reason,
                    evidence_ref,
                    payload[:20000],
                ),
            )
            return int(cur.lastrowid or 0)

    def events(self, *, since: float = 0.0, limit: int = 200) -> list[dict[str, Any]]:
        with self._lock, self._connect() as conn:
            rows = conn.execute(
                "SELECT id, created_at, kind, provider, ip, bucket_key, "
                "wire_fp, reason, evidence_ref, payload FROM egress_ip_state_events "
                "WHERE created_at >= ? ORDER BY id DESC LIMIT ?",
                (since, max(1, int(limit))),
            ).fetchall()
        keys = (
            "id", "created_at", "kind", "provider", "ip", "bucket_key",
            "wire_fp", "reason", "evidence_ref", "payload",
        )
        return [dict(zip(keys, r)) for r in rows]


_STORE: IPStateStore | None = None
_STORE_LOCK = threading.Lock()


def store(path: str | Path | None = None) -> IPStateStore:
    """Singleton paresseux (chemin par défaut : ``registry.db``)."""
    global _STORE
    with _STORE_LOCK:
        if _STORE is None:
            _STORE = IPStateStore(path)
        return _STORE


def reset_store_for_tests() -> None:
    global _STORE
    with _STORE_LOCK:
        _STORE = None
