"""Free-leg console credentials.

The free tier moved from the anonymous ``Bearer public`` token to the
OpenCode Console credential used by the 2.2.x client.  The proxy reads the
access token and organisation from the environment so it can replay the
official free request without a paid key.

Values are populated from ``opencode auth export`` (integration ``opencode``).
When they are absent the historical ``Bearer public`` behaviour is kept.
"""

from __future__ import annotations

import os

DEFAULT_CONSOLE_UA = (
    "opencode/2.2.5 ai-sdk/provider-utils/4.0.23 runtime/bun/1.4.4"
)


def console_token() -> str:
    return (os.getenv("OPENCODE_CONSOLE_TOKEN") or "").strip()


def console_org() -> str:
    return (os.getenv("OPENCODE_CONSOLE_ORG") or "").strip()


def console_ua() -> str:
    return (os.getenv("OPENCODE_CONSOLE_UA") or "").strip() or DEFAULT_CONSOLE_UA


def _console_allowed() -> bool:
    flag = (os.getenv("OPENCODE_ALLOW_CONSOLE_CREDENTIAL") or "").strip().lower()
    return flag in ("1", "true", "yes", "on")


def apply_console_auth(headers: dict) -> dict:
    """Overwrite the free-leg auth/UA with the Console credential, in place.

    Disabled by default: the free leg must not depend on a personal account.
    Set ``OPENCODE_ALLOW_CONSOLE_CREDENTIAL=1`` only for an explicit,
    temporary diagnostic.
    """
    if not _console_allowed():
        return headers
    token = console_token()
    if not token:
        return headers
    headers["Authorization"] = "Bearer " + token
    org = console_org()
    if org:
        headers["x-opencode-org-id"] = org
    headers["User-Agent"] = console_ua()
    return headers
