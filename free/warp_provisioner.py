"""Provisioning déclaratif des endpoints WARP (feature 011).

Le rotator ne doit plus être le seul chemin qui crée un conteneur. Ce module
fournit un ``plan`` idempotent et un ``apply`` contrôlé :

- noms, ports et buckets uniques ;
- bind ``127.0.0.1`` ;
- options de conteneur réappliquées (limites, labels, env) ;
- aucune suppression sans action explicite ;
- rollback signalé si ``docker run`` échoue.

Il ne fait aucune I/O réseau : le runner Docker est injectable.
"""

from __future__ import annotations

import subprocess
import sys
from collections.abc import Callable
from dataclasses import dataclass, field
from typing import Any

DEFAULT_IMAGE = "caomingjun/warp:latest"
CREATE_NO_WINDOW = 0x08000000 if sys.platform == "win32" else 0


@dataclass(frozen=True, slots=True)
class WarpEndpointSpec:
    label: str
    container: str
    port: int
    image: str = DEFAULT_IMAGE
    ipv6: bool = True
    managed: bool = True
    provider: str = "warp"
    block: str | None = None
    enabled: bool = True
    memory: str = "512m"
    pids_limit: int = 128
    env: dict[str, str] = field(default_factory=dict)

    def __post_init__(self) -> None:
        if not self.label or not self.container:
            raise ValueError("label and container are required")
        if not (1 <= int(self.port) <= 65535):
            raise ValueError("port must be 1..65535")


@dataclass(frozen=True, slots=True)
class ProvisionAction:
    action: str  # create | recreate | noop | delete
    label: str
    container: str
    reason: str = ""

    def as_dict(self) -> dict[str, str]:
        return {
            "action": self.action,
            "label": self.label,
            "container": self.container,
            "reason": self.reason,
        }


def _default_runner(args: list[str], timeout: int = 120) -> str:
    proc = subprocess.run(
        ["docker", *args],
        capture_output=True,
        text=True,
        timeout=timeout,
        check=False,
        creationflags=CREATE_NO_WINDOW,
    )
    if proc.returncode != 0:
        raise RuntimeError((proc.stderr or proc.stdout or "docker failed").strip()[:300])
    return (proc.stdout or "").strip()


def _validate(desired: list[WarpEndpointSpec]) -> None:
    enabled = [spec for spec in desired if spec.enabled]
    labels = [spec.label for spec in enabled]
    if len(labels) != len(set(labels)):
        raise ValueError("duplicate label in desired WARP endpoints")
    # Deux endpoints logiques (IPv4/IPv6) peuvent partager un conteneur et son
    # port : c'est la même station physique. En revanche un conteneur ne peut
    # pas écouter sur deux ports, et un port ne peut pas servir deux conteneurs.
    ports_by_container: dict[str, int] = {}
    containers_by_port: dict[int, str] = {}
    for spec in enabled:
        port = int(spec.port)
        previous_port = ports_by_container.get(spec.container)
        if previous_port is not None and previous_port != port:
            raise ValueError("duplicate container in desired WARP endpoints")
        previous_container = containers_by_port.get(port)
        if previous_container is not None and previous_container != spec.container:
            raise ValueError("duplicate port in desired WARP endpoints")
        ports_by_container[spec.container] = port
        containers_by_port[port] = spec.container
    # IPv6 /64 sont uniques par tunnel. Les IPv4 WARP viennent d'un pool NAT
    # partagé : deux stations peuvent légitimement avoir le même /32, le
    # ledger partage alors le bucket.
    v6_blocks = [spec.block for spec in enabled if spec.ipv6 and spec.block]
    if len(v6_blocks) != len(set(v6_blocks)):
        raise ValueError("duplicate block in desired WARP endpoints")


def _physical(desired: list[WarpEndpointSpec]) -> list[WarpEndpointSpec]:
    """Une spec par conteneur : les variantes IPv4/IPv6 partagent le tunnel."""
    seen: set[str] = set()
    physical: list[WarpEndpointSpec] = []
    for spec in desired:
        if not spec.enabled or spec.container in seen:
            continue
        seen.add(spec.container)
        physical.append(spec)
    return physical


class WarpProvisioner:
    def __init__(
        self,
        *,
        runner: Callable[[list[str], int], str] | None = None,
        image: str = DEFAULT_IMAGE,
    ) -> None:
        self._runner = runner or _default_runner
        self.image = image

    def plan(
        self,
        desired: list[WarpEndpointSpec],
        actual: dict[str, dict[str, Any]] | None = None,
    ) -> list[ProvisionAction]:
        _validate(desired)
        actual = actual or {}
        actions: list[ProvisionAction] = []
        for spec in _physical(desired):
            current = actual.get(spec.container)
            if current is None:
                actions.append(
                    ProvisionAction("create", spec.label, spec.container, "missing")
                )
                continue
            image = str(current.get("image") or "")
            ports = str(current.get("ports") or "")
            if image and spec.image and image != spec.image:
                actions.append(
                    ProvisionAction("recreate", spec.label, spec.container, "image")
                )
            elif f"127.0.0.1:{spec.port}->1080" not in ports:
                actions.append(
                    ProvisionAction("recreate", spec.label, spec.container, "bind")
                )
            else:
                actions.append(
                    ProvisionAction("noop", spec.label, spec.container, "conforme")
                )
        return actions

    def apply(
        self,
        desired: list[WarpEndpointSpec],
        actual: dict[str, dict[str, Any]] | None = None,
    ) -> dict[str, Any]:
        actions = self.plan(desired, actual)
        applied: list[dict[str, str]] = []
        try:
            for action, spec in zip(actions, _physical(desired), strict=True):
                if action.action == "noop":
                    applied.append(action.as_dict())
                    continue
                if action.action in ("create", "recreate"):
                    if action.action == "recreate":
                        self._runner(["rm", "-f", spec.container], 60)
                    self._runner(self._run_args(spec), 120)
                applied.append(action.as_dict())
        except Exception as exc:
            return {"ok": False, "error": str(exc), "applied": applied, "actions": actions}
        return {"ok": True, "applied": applied, "actions": actions}

    def delete(self, container: str, *, confirm: bool = False) -> dict[str, Any]:
        if not confirm:
            return {"ok": False, "error": "confirmation_required"}
        try:
            self._runner(["rm", "-f", container], 60)
        except Exception as exc:
            return {"ok": False, "error": str(exc)}
        return {"ok": True, "removed": container}

    def _run_args(self, spec: WarpEndpointSpec) -> list[str]:
        args = [
            "run",
            "-d",
            "--name",
            spec.container,
            "--cap-add",
            "NET_ADMIN",
            "-p",
            f"127.0.0.1:{spec.port}:1080",
            "--restart",
            "unless-stopped",
            "--memory",
            spec.memory,
            "--pids-limit",
            str(spec.pids_limit),
            "--label",
            f"opencode.provider={spec.provider}",
            "--label",
            f"opencode.label={spec.container}",
            "--label",
            "opencode.managed=true",
        ]
        for key, value in sorted(
            {"OPENCODE_STATION_LABEL": spec.container, **spec.env}.items()
        ):
            args.extend(["--env", f"{key}={value}"])
        args.append(spec.image or self.image)
        return args
