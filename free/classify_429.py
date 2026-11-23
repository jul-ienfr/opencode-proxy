"""Classification d'un 429 amont — 023-client-usable-egress (T005).

Distinguer ``format`` (IP brûlée par un corps non conforme), ``quota``
(bucket épuisé avec reset fournisseur), ``session`` (limite NordVPN) et
``upstream`` (surcharge/timeout) est indispensable : les remédiations sont
opposées (principe XVIII, FR-005).

Fonctions pures : aucun I/O réseau, aucun état global.
"""

from __future__ import annotations

import email.utils
import json
import math
import re
import time
from collections.abc import Mapping
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from typing import Any

FORMAT = "format"
QUOTA = "quota"
SESSION = "session"
UPSTREAM = "upstream"

CLASSES: frozenset[str] = frozenset({FORMAT, QUOTA, SESSION, UPSTREAM})

#: Borne haute d'un ``Retry-After``. Un en-tete/corps aberrant (``inf``,
#: epoch-secondes, 400 chiffres) ne doit jamais exclure une IP plus de 24 h —
#: meme convention que ``free/ip_quota.py`` (« jamais plus de 24 h »).
_MAX_RETRY_AFTER_S = 24 * 3600.0

_SESSION_HINTS = (
    "session",
    "auth",
    "credential",
    "too many connections",
    "maximum number of",
)
_QUOTA_TYPES = ("FreeUsageLimitError",)
_RATE_TYPES = ("RateLimitError", "RateLimitExceeded")
_FORMAT_HINTS = (
    "free tier can only be used from within",
    "only be used from within opencode",
    "unsupported tool",
    "invalid tool",
    "tool_choice",
)


@dataclass(frozen=True)
class Classification:
    cls: str
    error_type: str = ""
    retry_after_s: float | None = None
    reset_at: float | None = None
    message: str = ""
    reason: str = ""

    @property
    def is_quota(self) -> bool:
        return self.cls == QUOTA

    def as_dict(self) -> dict[str, Any]:
        return {
            "class": self.cls,
            "error_type": self.error_type,
            "retry_after_s": self.retry_after_s,
            "reset_at": self.reset_at,
            "message": self.message,
            "reason": self.reason,
        }


def next_utc_midnight(now: float | None = None) -> float:
    """Prochain minuit UTC (borne par défaut d'un bucket journalier)."""
    ts = time.time() if now is None else now
    current = datetime.fromtimestamp(ts, tz=UTC)
    nxt = (current + timedelta(days=1)).replace(
        hour=0, minute=0, second=0, microsecond=0
    )
    return nxt.timestamp()


def parse_retry_after(value: str | None, *, now: float | None = None) -> float | None:
    """Secondes depuis un ``Retry-After`` (nombre ou date HTTP)."""
    if value is None:
        return None
    raw = str(value).strip()
    if not raw:
        return None
    try:
        numeric = float(raw)
    except (TypeError, ValueError):
        numeric = None
    if numeric is not None:
        # inf/nan (header « inf », 400 chiffres…) -> pas de reset exploitable :
        # on rejette plutot que de produire un ``reset_at`` infini qui
        # exclurait l'IP a perpetuite. Les valeurs finies sont plafonnees a 24 h.
        if not math.isfinite(numeric):
            return None
        return min(max(0.0, numeric), _MAX_RETRY_AFTER_S)
    try:
        when = email.utils.parsedate_to_datetime(raw)
    except (TypeError, ValueError, IndexError):
        return None
    if when is None:
        return None
    if when.tzinfo is None:
        when = when.replace(tzinfo=UTC)
    ts = time.time() if now is None else now
    return min(max(0.0, when.timestamp() - ts), _MAX_RETRY_AFTER_S)


def _headers_lower(headers: Mapping[str, Any] | None) -> dict[str, str]:
    if not headers:
        return {}
    return {str(k).lower(): str(v) for k, v in headers.items()}


def _body_details(body: Any) -> tuple[str, str, str]:
    """Retourne ``(error_type, message, raw_text)`` depuis un corps varié."""
    if body is None:
        return "", "", ""
    if isinstance(body, bytes):
        try:
            body = body.decode("utf-8", "replace")
        except Exception:
            body = str(body)
    text = body if isinstance(body, str) else json.dumps(body, ensure_ascii=False)
    error_type = message = ""
    payload: Any = body
    if isinstance(body, str):
        try:
            payload = json.loads(body)
        except Exception:
            payload = None
    if isinstance(payload, dict):
        err = payload.get("error")
        if isinstance(err, dict):
            error_type = str(err.get("type") or err.get("code") or "")
            message = str(err.get("message") or "")
        elif isinstance(err, str):
            message = err
        if not error_type:
            error_type = str(payload.get("type") or payload.get("error_type") or "")
        if not message:
            message = str(payload.get("message") or "")
        detail = payload.get("detail")
        if isinstance(detail, dict):
            error_type = error_type or str(detail.get("error_type") or "")
            message = message or str(detail.get("message") or "")
    return error_type.strip(), message.strip(), text


def _body_retry_seconds(text: str) -> float | None:
    if not text:
        return None
    match = re.search(r'"retry_after"\s*:\s*"?([0-9]+(?:\.[0-9]+)?)', text, re.I)
    if match:
        try:
            value = float(match.group(1))
        except ValueError:
            return None
        return min(max(0.0, value), _MAX_RETRY_AFTER_S) if math.isfinite(value) else None
    match = re.search(r"retry after\s+([0-9]+(?:\.[0-9]+)?)", text, re.I)
    if match:
        try:
            value = float(match.group(1))
        except ValueError:
            return None
        return min(max(0.0, value), _MAX_RETRY_AFTER_S) if math.isfinite(value) else None
    return None


def classify_429(
    status: int,
    *,
    headers: Mapping[str, Any] | None = None,
    body: Any = None,
    ip_proven: bool = False,
    wire_fp_match: bool = True,
    now: float | None = None,
) -> Classification:
    """Classe un 429 selon la preuve disponible sur l'IP.

    ``ip_proven`` : un 200 avec contenu existe pour cette IP.
    ``wire_fp_match`` : le ``wire_fp`` de la requête égale celui de la preuve.

    Un rejet de FORME ne brûle l'IP que si l'on a la PREUVE que la requête
    était conforme (IP déjà prouvée ET même ``wire_fp``). Sans cette preuve,
    le rejet peut venir de notre propre corps — brûler l'IP 24 h serait une
    conclusion non mesurée (principe VII : aucune preuve, aucun burn).
    """
    ts = time.time() if now is None else now
    if int(status or 0) != 429:
        return Classification(
            cls=UPSTREAM,
            reason=f"status {status} is not a 429",
        )
    hl = _headers_lower(headers)
    ra_header = parse_retry_after(hl.get("retry-after"), now=ts)
    error_type, message, text = _body_details(body)
    ra_body = _body_retry_seconds(text)
    retry_after = ra_header if ra_header is not None else ra_body

    low_type = error_type.lower()
    low_text = text.lower()
    is_quota_error = any(t.lower() in low_type for t in _QUOTA_TYPES) or (
        "freeusagelimiterror" in low_text or "free usage limit" in low_text
    )
    is_rate_error = any(t.lower() in low_type for t in _RATE_TYPES) or (
        "rate limit" in low_text or "too many requests" in low_text
    )

    if is_quota_error:
        # [2026-11-18] `FreeUsageLimitError` = bucket d'IP épuisé, prouvée ou
        # non : c'est un QUOTA, pas une brûlure de format. Classer un 429 de
        # quota comme `format` a marqué 181 IP `burned` à tort pendant la nuit
        # et bloqué leur re-preuve 24 h. Un burn exige la preuve explicite d'un
        # rejet de forme (`_FORMAT_HINTS`).
        reset_at = (ts + retry_after) if retry_after else next_utc_midnight(ts)
        return Classification(
            cls=QUOTA,
            error_type=error_type or "FreeUsageLimitError",
            retry_after_s=retry_after,
            reset_at=reset_at,
            message=message,
            reason="bucket exhausted (FreeUsageLimitError)",
        )

    if any(hint in low_text for hint in _FORMAT_HINTS):
        if not (ip_proven and wire_fp_match):
            # [fix 2026-11-21] Les paramètres de preuve étaient documentés mais
            # ignorés : un 429 de forme sur une IP non prouvée (ou avec un
            # wire_fp différent) était quand même classé `format` puis brûlé
            # 24 h. C'est ce qui a produit les 181 burns nocturnes à tort.
            return Classification(
                cls=UPSTREAM,
                error_type=error_type or "FreeTierError",
                retry_after_s=retry_after,
                reset_at=(ts + retry_after) if retry_after else None,
                message=message,
                reason=(
                    "form rejection without a matching proof: transient, no "
                    f"burn (ip_proven={ip_proven}, wire_fp_match={wire_fp_match})"
                ),
            )
        return Classification(
            cls=FORMAT,
            error_type=error_type or "FreeTierError",
            retry_after_s=retry_after,
            reset_at=(ts + retry_after) if retry_after else next_utc_midnight(ts),
            message=message,
            reason="explicit free-tier form rejection",
        )

    if is_rate_error:
        return Classification(
            cls=UPSTREAM,
            error_type=error_type or "RateLimitError",
            retry_after_s=retry_after,
            reset_at=(ts + retry_after) if retry_after else None,
            message=message,
            reason="short rate limit",
        )

    if any(h in low_text for h in _SESSION_HINTS):
        return Classification(
            cls=SESSION,
            error_type=error_type or "SessionLimit",
            retry_after_s=retry_after,
            reset_at=(ts + retry_after) if retry_after else None,
            message=message,
            reason="provider session limit",
        )

    # Un 429 sans motif identifiable n'est ni un burn ni un quota prouvé :
    # il est transitoire. Ne jamais brûler une IP sur une absence de preuve
    # (principe VII : aucune preuve, aucun succès — et aucun burn).
    cls = UPSTREAM
    return Classification(
        cls=cls,
        error_type=error_type or "Unknown429",
        retry_after_s=retry_after,
        reset_at=(ts + retry_after) if retry_after else None,
        message=message,
        reason="unclassified 429 treated as transient (no format evidence)",
    )
