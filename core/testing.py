"""Module for detecting test vs production mode.

Provides ``under_test()`` — a single, centralised gate replacing all direct
``PYTEST_CURRENT_TEST`` reads across the codebase.

Rules (the function returns True ONLY when we are *not* safely in production):
  * False if ``PYTEST_CURRENT_TEST`` environment variable absent.
  * False if ``debug.force_production_paths`` is True in config.yaml
    or ``OPENCODE_FORCE_PRODUCTION_PATHS=1`` is set.
  * True otherwise.
"""

import os as _os

# Import yaml_get lazily to avoid circular imports (core -> config -> yaml).
# The import happens at call time, wrapped in try/except. Never import
# ``opencode`` here (cycle).

# Values accepted as "true" for the OPENCODE_FORCE_PRODUCTION_PATHS env var.
_TRUTHY = ("1", "true", "yes", "on")


def _env_forces_production_paths() -> bool:
    """True when OPENCODE_FORCE_PRODUCTION_PATHS is set to a truthy value."""
    return (
        _os.getenv("OPENCODE_FORCE_PRODUCTION_PATHS", "").strip().lower()
        in _TRUTHY
    )


def _yaml_forces_production_paths() -> bool:
    """Read debug.force_production_paths from the in-memory YAML config."""
    try:
        from config.settings import yaml_get  # lazy, no cycle
        return bool(yaml_get("debug", "force_production_paths", False))
    except Exception:
        return False


def under_test() -> bool:
    """Return True when running inside pytest (or test mode).

    Returns:
        False  — we are in production (safe to write/persist real state), or a
                 test explicitly forced production paths.
        True   — we are in a test context (avoid side-effects on real state).

    Detection order:
      1. If PYTEST_CURRENT_TEST is not set → production → False.
      2. If ``debug.force_production_paths`` is True (config.yaml) or
         ``OPENCODE_FORCE_PRODUCTION_PATHS`` is truthy → False.
      3. Otherwise → test mode → True.

    This function must never call itself: it is the single source of the
    detection logic (no global replacement / delegation loop).
    """
    if not _os.getenv("PYTEST_CURRENT_TEST"):
        return False
    # PYTEST_CURRENT_TEST is set — now check the explicit escape hatch.
    if _env_forces_production_paths():
        return False
    if _yaml_forces_production_paths():
        return False
    return True