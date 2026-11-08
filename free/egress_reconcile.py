"""Réconciliation de l'egress des endpoints WARP/proxys.

Le pool ne doit jamais faire confiance à `egress_ip`/`block` persistés : un
redémarrage externe du conteneur peut changer l'IP sans passer par le rotator.
Ce module fournit :

- ``bucket_for_endpoint`` : le bucket réel, avec la même normalisation que le
  ledger ;
- ``duplicate_bucket_groups`` : les endpoints qui partagent un bucket ;
- ``reconcile_endpoint`` : mesure l'egress et met à jour l'endpoint + la config.
"""

from __future__ import annotations

import logging
from collections.abc import Callable
from typing import Any

from free.quota_ledger import quota_block

logger = logging.getLogger(__name__)


def bucket_for_endpoint(endpoint: Any) -> str | None:
    # Le live gagne toujours sur le cache : un redémarrage externe peut avoir
    # changé l'IP sans passer par le rotator.
    block = quota_block(getattr(endpoint, "egress_ip", None))
    if block:
        return block
    block = str(getattr(endpoint, "block_key", "") or "").strip()
    if block:
        return block
    block = str(getattr(endpoint, "block", "") or "").strip()
    if block:
        return block
    return None


def duplicate_bucket_groups(endpoints: list[Any]) -> dict[str, list[str]]:
    """Retourne ``{bucket: [labels]}`` pour les buckets partagés."""
    groups: dict[str, list[str]] = {}
    for endpoint in endpoints:
        if not bool(getattr(endpoint, "enabled", True)):
            continue
        bucket = bucket_for_endpoint(endpoint)
        if not bucket:
            continue
        label = str(
            getattr(endpoint, "label", "")
            or getattr(endpoint, "pid", "")
            or getattr(endpoint, "container", "")
        )
        groups.setdefault(bucket, []).append(label)
    return {bucket: labels for bucket, labels in groups.items() if len(labels) > 1}


def unique_bucket_count(endpoints: list[Any]) -> int:
    buckets = {
        bucket
        for endpoint in endpoints
        if bool(getattr(endpoint, "enabled", True))
        for bucket in [bucket_for_endpoint(endpoint)]
        if bucket
    }
    return len(buckets)


def reconcile_endpoint(
    endpoint: Any,
    *,
    persist: Callable[[str, str, str], None] | None = None,
    timeout_s: float = 6.0,
) -> dict[str, Any]:
    """Mesure l'egress live et corrige l'endpoint si nécessaire.

    Retourne un résultat sérialisable. Ne lève jamais : une sonde en échec
    laisse l'endpoint inchangé et est signalée.
    """
    label = str(getattr(endpoint, "label", "") or getattr(endpoint, "pid", ""))
    old_ip = str(getattr(endpoint, "egress_ip", "") or "").strip() or None
    old_block = bucket_for_endpoint(endpoint)
    refresh = getattr(endpoint, "refresh_egress", None)
    if not callable(refresh):
        return {
            "label": label,
            "ok": False,
            "reason": "no_refresh",
            "old_ip": old_ip,
            "new_ip": None,
            "old_block": old_block,
            "new_block": None,
        }
    try:
        ip = str(refresh(timeout_s) or "").strip() or None
    except Exception as exc:  # pragma: no cover - defensive
        logger.warning("[egress-reconcile] %s probe failed: %s", label, exc)
        ip = None
    if not ip:
        return {
            "label": label,
            "ok": False,
            "reason": "egress_unresolved",
            "old_ip": old_ip,
            "new_ip": None,
            "old_block": old_block,
            "new_block": None,
        }
    new_block = quota_block(ip)
    changed = ip != old_ip or (new_block or "") != (old_block or "")
    if changed:
        try:
            endpoint.egress_ip = ip
            endpoint.current_ip = ip
            if new_block:
                endpoint.block = new_block
                endpoint.block_key = new_block
            if persist is not None:
                persist(label, ip, new_block or "")
        except Exception as exc:  # pragma: no cover - defensive
            logger.warning("[egress-reconcile] %s persist failed: %s", label, exc)
    return {
        "label": label,
        "ok": True,
        "reason": "changed" if changed else "unchanged",
        "old_ip": old_ip,
        "new_ip": ip,
        "old_block": old_block,
        "new_block": new_block,
    }
