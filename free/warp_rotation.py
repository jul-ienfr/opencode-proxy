"""Rotation automatique des stations WARP managées.

Un `Socks5Endpoint` WARP est un conteneur `caomingjun/warp` : quand son IP est
épuisée (429/quota), le pool le marque `bad`. Ce module recrée le conteneur,
attend son healthcheck, résout le nouvel egress et met à jour l'endpoint et la
config. Il ne touche jamais un proxy non `managed`.
"""

from __future__ import annotations

import ipaddress
import logging
import subprocess
import sys
import threading
import time
from dataclasses import dataclass
from typing import Any

logger = logging.getLogger(__name__)

DEFAULT_IMAGE = "caomingjun/warp:latest"
HEALTH_TIMEOUT_S = 90.0
EGRESS_TIMEOUT_S = 30.0
# Windows : pythonw.exe n'a pas de console, mais chaque `subprocess.run`
# (docker, curl de secours) en ouvrait une brièvement → fenêtres MS-DOS
# intempestives à chaque rotation. CREATE_NO_WINDOW les supprime.
CREATE_NO_WINDOW = 0x08000000 if sys.platform == "win32" else 0
INFLIGHT: set[str] = set()
LAST_ROTATION: dict[str, float] = {}
_LOCK = threading.Lock()
_ROTATE_LOCK = threading.Lock()
MIN_ROTATION_INTERVAL_S = 300.0
# Un restart peut rendre le même /64 (WARP réutilise les blocs Cloudflare).
# On retente la recréation pour trouver un bloc frais, sans boucler à l'infini.
MAX_ROTATION_ATTEMPTS = 4
# Egress forcé par famille : ifconfig.me renvoie l'IPv4 partagée WARP sur un
# endpoint « ipv6 », ce qui faisait tracker la mauvaise IP et condamnait la
# station pour le quota d'une autre famille.
EGRESS_URL_V4 = "https://api.ipify.org"
EGRESS_URL_V6 = "https://api6.ipify.org"


def block_for_ledger_key(raw: str) -> str | None:
    """Convertit une clé ledger (4 premiers groupes IPv6) en bloc /64.

    ``server_ip_key`` tronque une IPv6 à ``a:b:c:d`` sans ``::``, ce qui n'est
    pas une adresse complète pour ``ipaddress``. On reconstitue le préfixe.
    """
    value = str(raw or "").strip()
    if ":" in value and "::" not in value and value.count(":") == 3:
        value = f"{value}::"
    return block_for_ip(value)


def block_for_ip(ip: str) -> str | None:
    try:
        parsed = ipaddress.ip_address(str(ip).strip())
    except ValueError:
        return None
    if parsed.version == 6:
        return str(ipaddress.ip_network(f"{parsed}/64", strict=False))
    return str(ipaddress.ip_network(f"{parsed}/32", strict=False))


@dataclass(frozen=True, slots=True)
class WarpRotationResult:
    label: str
    ok: bool
    old_ip: str | None
    new_ip: str | None
    block: str | None
    attempts: int
    reason: str

    def as_dict(self) -> dict[str, Any]:
        return {
            "label": self.label,
            "ok": self.ok,
            "old_ip": self.old_ip,
            "new_ip": self.new_ip,
            "block": self.block,
            "attempts": self.attempts,
            "reason": self.reason,
        }


class WarpRotator:
    def __init__(
        self,
        *,
        image: str = DEFAULT_IMAGE,
        health_timeout_s: float = HEALTH_TIMEOUT_S,
        egress_timeout_s: float = EGRESS_TIMEOUT_S,
    ) -> None:
        self.image = image
        self.health_timeout_s = float(health_timeout_s)
        self.egress_timeout_s = float(egress_timeout_s)

    def request_rotation(self, endpoint, pool, persist) -> bool:
        label = str(getattr(endpoint, "label", "") or "").strip()
        container = str(getattr(endpoint, "container", "") or "").strip()
        if not label or not container or not bool(getattr(endpoint, "managed", False)):
            return False
        with _LOCK:
            if label in INFLIGHT:
                return False
            now = time.monotonic()
            last = float(LAST_ROTATION.get(label, 0.0) or 0.0)
            if now - last < MIN_ROTATION_INTERVAL_S:
                return False
            LAST_ROTATION[label] = now
            INFLIGHT.add(label)
        thread = threading.Thread(
            target=self._run,
            args=(endpoint, pool, persist),
            name=f"warp-rotate-{label}",
            daemon=True,
        )
        thread.start()
        return True

    def _run(self, endpoint, pool, persist) -> None:
        label = str(getattr(endpoint, "label", "") or "")
        try:
            with _ROTATE_LOCK:
                forbidden = set()
                try:
                    for other in pool._socks5_enabled_eps():
                        if other is endpoint:
                            continue
                        other_ip = str(getattr(other, "egress_ip", "") or "").strip()
                        other_block = block_for_ip(other_ip) if other_ip else None
                        if other_block:
                            forbidden.add(other_block)
                except Exception:
                    forbidden = set()
                # [mimo 2026-11-06] Ne pas recréer un conteneur dans un /64
                # dont le bucket journalier est déjà épuisé : WARP réutilise
                # souvent les mêmes blocs Cloudflare, et la station revient
                # immédiatement en 429 sans que le ledger puisse l'expliquer.
                try:
                    ledger = pool._get_ledger() if hasattr(pool, "_get_ledger") else None
                    if ledger is not None and hasattr(ledger, "exhausted_ips"):
                        for raw in ledger.exhausted_ips():
                            blocked = block_for_ledger_key(str(raw or ""))
                            if blocked:
                                forbidden.add(blocked)
                except Exception:
                    pass
                result = self.rotate(endpoint, forbidden_blocks=forbidden)
                # [fiab 2026-11-06] Appliquer l'egress SOUS le verrou global :
                # sinon deux rotations concurrentes voyaient toutes deux
                # l'ancien état et pouvaient choisir le même /64. Le probe
                # post-rotation est inclus — il est désactivé par défaut.
                if result.ok and result.new_ip:
                    self._apply(endpoint, pool, persist, result)
                else:
                    # Le conteneur a bien été recréé : même si le bloc est
                    # refusé (same_block/duplicate/exhausted), l'IP réelle a
                    # changé. La laisser stale fausse le ledger, la détection
                    # de doublons et la rotation suivante.
                    if result.new_ip:
                        self._apply_tracking(endpoint, pool, persist, result)
                    logger.warning("[warp-rotate] %s: %s", label, result.as_dict())
        except Exception as exc:
            logger.warning("[warp-rotate] %s failed: %s", label, exc)
        finally:
            with _LOCK:
                INFLIGHT.discard(label)

    def rotate(self, endpoint, *, forbidden_blocks: set[str] | None = None) -> WarpRotationResult:
        forbidden = set(forbidden_blocks or ())
        label = str(getattr(endpoint, "label", "") or "")
        container = str(getattr(endpoint, "container", "") or "")
        port = int(getattr(endpoint, "port", 0) or 0)
        ipv6 = bool(getattr(endpoint, "ipv6", False))
        old_ip = str(getattr(endpoint, "egress_ip", "") or "").strip() or None
        old_block = block_for_ip(old_ip) if old_ip else None
        attempts = 0
        last_reason = "not_started"
        last_ip: str | None = None
        last_block: str | None = None
        max_attempts = max(2, int(MAX_ROTATION_ATTEMPTS))
        for attempt in range(1, max_attempts + 1):
            attempts = attempt
            try:
                if attempt == 1:
                    self._docker(["restart", container], timeout=60)
                else:
                    self._recreate(container, port, endpoint)
            except Exception as exc:
                last_reason = f"docker:{exc}"
                continue
            if not self._wait_healthy(container):
                last_reason = "health_timeout"
                continue
            new_ip = self._resolve_egress(port, ipv6)
            if not new_ip:
                last_reason = "egress_unresolved"
                continue
            new_block = block_for_ip(new_ip)
            last_ip, last_block = new_ip, new_block
            if old_block and new_block == old_block:
                last_reason = "same_block"
                continue
            if new_block and new_block in forbidden:
                last_reason = "duplicate_block"
                continue
            return WarpRotationResult(
                label=label,
                ok=True,
                old_ip=old_ip,
                new_ip=new_ip,
                block=new_block,
                attempts=attempts,
                reason="rotated",
            )
        return WarpRotationResult(
            label=label,
            ok=False,
            old_ip=old_ip,
            # Conserver la dernière IP réellement observée : le conteneur
            # tourne dessus même si son bloc est refusé.
            new_ip=last_ip,
            block=last_block,
            attempts=attempts,
            reason=last_reason,
        )

    def _apply(self, endpoint, pool, persist, result: WarpRotationResult) -> None:
        endpoint.egress_ip = result.new_ip
        endpoint.current_ip = result.new_ip
        endpoint._egress_probe_attempted = True
        if result.block:
            endpoint.block = result.block
        try:
            per = pool._per_station(endpoint)
            per["bad_until"] = 0.0
            per["bad_cause"] = None
            per["degraded_override"] = False
            per["consec_429"] = 0
        except Exception:
            pass
        try:
            persist(str(getattr(endpoint, "label", "") or ""), str(result.new_ip or ""), result.block or "")
        except Exception as exc:
            logger.warning("[warp-rotate] persist failed: %s", exc)
        # [GUI post_rotation_probe] Valide le nouvel egress avant de rendre la
        # station au pool. Le callback est injecté par opencode (identité,
        # profil et transport officiels) ; absent en test/offline.
        probe = getattr(pool, "_post_rotation_probe_cb", None)
        if callable(probe):
            try:
                probe(endpoint)
            except Exception as exc:
                logger.warning("[warp-rotate] post-rotation probe failed: %s", exc)
        logger.info("[warp-rotate] %s: %s", result.label, result.as_dict())

    def _apply_tracking(self, endpoint, pool, persist, result: WarpRotationResult) -> None:
        """Met à jour l'egress même quand le bloc est refusé.

        Le conteneur a été recréé et possède une nouvelle IP ; ne pas la
        tracker laisse ``current_ip``/``block`` sur l'ancienne valeur, ce qui
        fausse la détection de doublons et l'imputation du quota. On marque la
        station brièvement bad pour qu'elle ne serve pas ce bloc avant la
        prochaine rotation.
        """
        ip = str(result.new_ip or "").strip()
        if not ip:
            return
        endpoint.egress_ip = ip
        endpoint.current_ip = ip
        endpoint._egress_probe_attempted = True
        if result.block:
            endpoint.block = result.block
        try:
            per = pool._per_station(endpoint)
            per["bad_until"] = max(
                float(per.get("bad_until") or 0.0),
                time.monotonic() + 60.0,
            )
            per["bad_cause"] = "rotation_rejected"
        except Exception:
            pass
        try:
            persist(str(getattr(endpoint, "label", "") or ""), ip, result.block or "")
        except Exception as exc:
            logger.warning("[warp-rotate] persist failed: %s", exc)

    def _recreate(self, container: str, port: int, endpoint) -> None:
        image = str(getattr(endpoint, "image", "") or self.image)
        self._docker(["rm", "-f", container], timeout=60)
        self._docker(
            [
                "run",
                "-d",
                "--name",
                container,
                "--cap-add",
                "NET_ADMIN",
                "-p",
                f"{port}:1080",
                "--restart",
                "unless-stopped",
                image,
            ],
            timeout=120,
        )

    def _docker(self, args: list[str], *, timeout: float = 60) -> str:
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

    def _wait_healthy(self, container: str) -> bool:
        deadline = time.monotonic() + self.health_timeout_s
        while time.monotonic() < deadline:
            try:
                state = self._docker(
                    ["inspect", "-f", "{{if .State.Health}}{{.State.Health.Status}}{{else}}{{.State.Status}}{{end}}", container],
                    timeout=10,
                )
            except Exception:
                state = ""
            if state == "healthy":
                return True
            if state in ("running", "unhealthy"):
                time.sleep(3)
                continue
            time.sleep(2)
        return False

    def _resolve_egress(self, port: int, ipv6: bool) -> str | None:
        if port <= 0:
            return None
        url = EGRESS_URL_V6 if ipv6 else EGRESS_URL_V4
        deadline = time.monotonic() + self.egress_timeout_s
        while time.monotonic() < deadline:
            try:
                from curl_cffi import CurlOpt
                from curl_cffi import requests as curl_requests

                kwargs: dict[str, Any] = {
                    "proxy": f"socks5h://127.0.0.1:{port}",
                    "impersonate": "chrome131",
                    "timeout": 10,
                }
                if ipv6:
                    kwargs["curl_options"] = {CurlOpt.IPRESOLVE: 2}
                with curl_requests.Session(**kwargs) as session:
                    response = session.get(url)
                ip = str(getattr(response, "text", "") or "").strip()
                parsed = ipaddress.ip_address(ip)
                if bool(parsed.version == 6) != bool(ipv6):
                    raise ValueError(f"egress family mismatch: {ip}")
                return ip
            except Exception:
                try:
                    cmd = ["curl", "-s", "--max-time", "10", "--socks5-hostname", f"127.0.0.1:{port}"]
                    if ipv6:
                        cmd.insert(1, "-6")
                    proc = subprocess.run(
                        [*cmd, url],
                        capture_output=True,
                        text=True,
                        timeout=15,
                        check=False,
                        creationflags=CREATE_NO_WINDOW,
                    )
                    ip = str(proc.stdout or "").strip()
                    parsed = ipaddress.ip_address(ip)
                    if bool(parsed.version == 6) != bool(ipv6):
                        raise ValueError(f"egress family mismatch: {ip}")
                    return ip
                except Exception:
                    time.sleep(3)
        return None


__all__ = ["WarpRotator", "WarpRotationResult", "block_for_ip", "block_for_ledger_key"]
