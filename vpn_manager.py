"""vpn_manager — SHIM Phase 6 refonte (compatibilité, ne pas étendre).

Domicile canonique : ``vpn.manager`` (déplacement pur — seuls ajustements :
``ROOT`` ré-ancré racine repo, loggers figés à ``"vpn_manager"``).
Ce module re-exporte toute la surface historique utilisée (MÊMES objets :
classes, fonctions, registres mutés en place) pour les consommateurs
historiques : ``tests/test_vpn_*``, ``tests/test_geo_*``…,
``tests/test_identity_pool.py``, ``config/settings.py``,
``dashboard/api.py``, ``scripts/smoke_todo10.py`` (façade gelée ADR-006 §5,
conservée pendant 2 versions au-delà de la refonte — plan §10).

Non repris (internes canoniques, AUCUN lecteur en repo — vérifié par grep) :
``_AUTH_GLOBAL_COOLDOWN_UNTIL``, ``_AUTH_IN_FLIGHT``,
``_AUTH_LAST_CONNECT_AT``, ``_DOCKER_DESKTOP_LAUNCHED`` — scalaires rebindés
via ``global`` (un re-export figerait une valeur périmée ; on y accède via
``_auth_cooldown_remaining()`` / fonctions du module).

Suppression prévue Phase 9+2 versions après preuve de non-usage (``grep``).
"""

# Modules partagés ré-exportés tels quels (MÊMES objets) : les tests les
# patchent VIA ce namespace (ex. ``monkeypatch.setattr(vm.os, "replace")``,
# ``monkeypatch.setattr(vm.asyncio, "sleep")``) — le canonique utilise les
# mêmes objets, donc le patch reste visible des deux côtés comme avant.
import asyncio as asyncio  # noqa: F401
import os as os  # noqa: F401

from vpn.manager import _AUTH_FAIL_TIMES as _AUTH_FAIL_TIMES
from vpn.manager import _AUTH_THROTTLE_LOCK as _AUTH_THROTTLE_LOCK
from vpn.manager import _COUNTRY_ALIASES as _COUNTRY_ALIASES
from vpn.manager import _DEFAULT_IDENTITY_PROFILE as _DEFAULT_IDENTITY_PROFILE
from vpn.manager import _ENV_RW_LOCK as _ENV_RW_LOCK
from vpn.manager import _KNOWN_IMPERSONATIONS as _KNOWN_IMPERSONATIONS
from vpn.manager import _LANG_VARIANTS as _LANG_VARIANTS
from vpn.manager import _NORDVPN_HOST_RE as _NORDVPN_HOST_RE
from vpn.manager import _UA_BY_IMPERSONATE as _UA_BY_IMPERSONATE
from vpn.manager import CREATE_NO_WINDOW as CREATE_NO_WINDOW
from vpn.manager import ROOT as ROOT
from vpn.manager import BackoffTimer as BackoffTimer
from vpn.manager import CircuitBreaker as CircuitBreaker
from vpn.manager import RotationFailed as RotationFailed
from vpn.manager import VPNManager as VPNManager
from vpn.manager import VPNState as VPNState
from vpn.manager import _auth_connect_done as _auth_connect_done
from vpn.manager import _auth_cooldown_remaining as _auth_cooldown_remaining
from vpn.manager import _auth_gate as _auth_gate
from vpn.manager import _auth_record_failure as _auth_record_failure
from vpn.manager import _build_identity_pool as _build_identity_pool
from vpn.manager import _clamp_cfg_number as _clamp_cfg_number
from vpn.manager import _classify_error_kind as _classify_error_kind
from vpn.manager import _classify_probe_exc as _classify_probe_exc
from vpn.manager import _docker_cli as _docker_cli
from vpn.manager import _env_value_from_inspect as _env_value_from_inspect
from vpn.manager import _extract_current_hostname as _extract_current_hostname
from vpn.manager import _headers_key as _headers_key
from vpn.manager import _host_ttl_seconds as _host_ttl_seconds
from vpn.manager import _identity_header_variants as _identity_header_variants
from vpn.manager import _normalize_country as _normalize_country
from vpn.manager import _normalize_identity_profiles as _normalize_identity_profiles
from vpn.manager import _safari_version as _safari_version
from vpn.manager import _sh_quote as _sh_quote
from vpn.manager import _stack_from_env_file as _stack_from_env_file
from vpn.manager import _ua_for_target as _ua_for_target
from vpn.manager import ensure_docker_running as ensure_docker_running
from vpn.manager import logger as logger
from vpn.manager import reconcile_orphan_containers as reconcile_orphan_containers

__all__ = [
    "BackoffTimer",
    "CircuitBreaker",
    "CREATE_NO_WINDOW",
    "ROOT",
    "RotationFailed",
    "VPNManager",
    "VPNState",
    "_AUTH_FAIL_TIMES",
    "_AUTH_THROTTLE_LOCK",
    "_COUNTRY_ALIASES",
    "_DEFAULT_IDENTITY_PROFILE",
    "_ENV_RW_LOCK",
    "_KNOWN_IMPERSONATIONS",
    "_LANG_VARIANTS",
    "_NORDVPN_HOST_RE",
    "_UA_BY_IMPERSONATE",
    "_auth_connect_done",
    "_auth_cooldown_remaining",
    "_auth_gate",
    "_auth_record_failure",
    "_build_identity_pool",
    "_clamp_cfg_number",
    "_classify_error_kind",
    "_classify_probe_exc",
    "_docker_cli",
    "_env_value_from_inspect",
    "_extract_current_hostname",
    "_headers_key",
    "_host_ttl_seconds",
    "_identity_header_variants",
    "_normalize_country",
    "_normalize_identity_profiles",
    "_safari_version",
    "_sh_quote",
    "_stack_from_env_file",
    "_ua_for_target",
    "ensure_docker_running",
    "logger",
    "reconcile_orphan_containers",
]
