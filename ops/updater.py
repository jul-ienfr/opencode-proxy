"""Mise à jour d'image coordonnée de la flotte.

Le manager historique comparait le digest du tag *partagé* avant/après son
propre `docker compose pull`. Une fois la première station mise à jour, les
suivantes voyaient le même digest et ne recréaient jamais leur conteneur.

Ici : un seul pull pour l'image, puis recréation séquentielle de chaque
service avec SON environnement de stack, vérification santé, rollback global
best-effort sur l'image précédente.
"""

from __future__ import annotations

import asyncio
import logging
import os
from typing import Any

logger = logging.getLogger(__name__)


async def _docker(host: Any, args: list[str], timeout: int = 120, env: dict | None = None):
    return await asyncio.to_thread(host._docker_run, args, timeout, env=env)


async def _image_id(host: Any, image: str = "qmcgaw/gluetun") -> str | None:
    try:
        res = await _docker(host, ["image", "inspect", image, "--format", "{{.Id}}"], 30)
        if res.returncode != 0:
            return None
        return (res.stdout or "").strip() or None
    except Exception:
        return None


async def apply_fleet_update(managers: list[Any], host: Any) -> dict:
    """Pull une fois, recrée les services un par un, vérifie, rollback.

    ``host`` est le manager de la station 1 (il fournit ``_docker_run``,
    ``_compose_file_path`` et ``_compose_env``). Les autres managers ne font
    aucune opération Docker ici, seulement la vérification santé.
    """
    managers = [m for m in (managers or []) if m is not None]
    if not managers or host is None:
        return {"ok": False, "error": "flotte vide"}
    compose = host._compose_file_path()
    image = "qmcgaw/gluetun"
    old_id = await _image_id(host, image)
    services = [str(getattr(m, "_compose_service", "") or "") for m in managers]
    services = [s for s in services if s]
    if not services:
        return {"ok": False, "error": "aucun service compose"}
    try:
        pull = await _docker(
            host,
            ["compose", "-f", compose, "pull", *services],
            300,
            env=dict(os.environ),
        )
        if pull.returncode != 0:
            raise RuntimeError((pull.stderr or pull.stdout or "compose pull failed")[:300])
    except Exception as e:
        return {"ok": False, "error": f"pull: {e}", "updated": []}

    updated: list[int] = []
    errors: list[str] = []
    for m in managers:
        sid = int(getattr(m, "_station", -1))
        try:
            stack = str(getattr(m, "_stack_effective", "openvpn") or "openvpn")
            env = m._compose_env(stations=[sid], stack=stack)
            up = await _docker(
                host,
                [
                    "compose",
                    "-f",
                    compose,
                    "up",
                    "-d",
                    "--force-recreate",
                    "--pull",
                    "never",
                    m._compose_service,
                ],
                240,
                env=env,
            )
            if up.returncode != 0:
                raise RuntimeError((up.stderr or up.stdout or "compose up failed")[:300])
            # [roadmap] vérification réelle du tunnel après recréation.
            try:
                await m._wait_healthy(timeout=120)
            except Exception:
                pass
            await m.refresh_status(force=True)
            if str(getattr(m, "status", "")) not in ("connected", "degraded"):
                raise RuntimeError(f"statut {getattr(m, 'status', '?')} après update")
            m._update_available = False
            m._update_applied_at = __import__("time").strftime("%Y-%m-%dT%H:%M:%SZ", __import__("time").gmtime())
            m.save_state()
            updated.append(sid)
            logger.info("[vpn-update] fleet: station %s mise à jour", sid)
        except Exception as e:
            errors.append(f"s{sid}: {e}")
            logger.error("[vpn-update] fleet: station %s échec: %s", sid, e)
            break

    if errors and old_id:
        logger.warning("[vpn-update] fleet: rollback image %s", old_id[:19])
        try:
            await _docker(host, ["tag", old_id, image], 30)
            for m in managers:
                sid = int(getattr(m, "_station", -1))
                if sid not in updated:
                    continue
                env = host._compose_env(stations=[sid], stack=str(getattr(m, "_stack_effective", "openvpn")))
                await _docker(
                    host,
                    ["compose", "-f", compose, "up", "-d", "--force-recreate", "--pull", "never", m._compose_service],
                    240,
                    env=env,
                )
                try:
                    await m.refresh_status(force=True)
                except Exception:
                    pass
        except Exception as e:
            logger.error("[vpn-update] fleet: rollback failed: %s", e)

    return {
        "ok": not errors,
        "updated": updated,
        "errors": errors,
        "image_id": await _image_id(host, image),
    }


__all__ = ["apply_fleet_update"]
