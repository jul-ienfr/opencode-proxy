"""Rotation automatique des stations WARP managées.

Un `Socks5Endpoint` WARP est un conteneur `caomingjun/warp` : quand son IP est
épuisée (429/quota), le pool le marque `bad`. Ce module recrée le conteneur,
attend son healthcheck, résout le nouvel egress et met à jour l'endpoint et la
config. Il ne touche jamais un proxy non `managed`.
"""

from __future__ import annotations

import logging
import os
import sqlite3
import subprocess
import sys
import threading
import time
from dataclasses import dataclass
from typing import Any

from free.egress_provider import EgressRotationResult
from free.quota_ledger import quota_block
from ops.lifecycle_evidence import Evidence, log_evidence

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
ROTATION_TIMES: dict[str, list[float]] = {}
_LOCK = threading.Lock()
_ROTATE_LOCK = threading.Lock()
MIN_ROTATION_INTERVAL_S = 300.0
# Un restart peut rendre le même /64 (WARP réutilise les blocs Cloudflare).
# On retente la recréation pour trouver un bloc frais, sans boucler à l'infini.
MAX_ROTATION_ATTEMPTS = 4
ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))


class RotationBudgetStore:
    """Budget horaire de rotation persisté dans ``registry.db``.

    Table légère ``rotation_jobs`` créée à la demande. Un enregistrement par
    tentative acceptée ; les entrées de plus d'une heure sont purgées à la
    lecture. Le store ne remplace pas le ledger de quota : il compte les
    rotations, pas les requêtes.
    """

    def __init__(self, path: str | None = None) -> None:
        self.path = path or os.path.join(ROOT, "logs", "registry.db")
        self._lock = threading.Lock()
        self._ensure()

    def _connect(self) -> sqlite3.Connection:
        conn = sqlite3.connect(self.path, timeout=5.0)
        conn.execute("PRAGMA busy_timeout=5000")
        return conn

    def _ensure(self) -> None:
        with self._lock:
            try:
                with self._connect() as conn:
                    conn.execute(
                        """
                        CREATE TABLE IF NOT EXISTS rotation_jobs (
                            id INTEGER PRIMARY KEY AUTOINCREMENT,
                            provider TEXT NOT NULL DEFAULT 'warp',
                            label TEXT NOT NULL,
                            trigger TEXT NOT NULL DEFAULT 'auto',
                            result TEXT NOT NULL DEFAULT 'started',
                            reason TEXT,
                            started_at REAL NOT NULL,
                            finished_at REAL
                        )
                        """
                    )
                    cols = {
                        str(row[1])
                        for row in conn.execute("PRAGMA table_info(rotation_jobs)")
                    }
                    if "finished_at" not in cols:
                        conn.execute(
                            "ALTER TABLE rotation_jobs ADD COLUMN finished_at REAL"
                        )
                    conn.execute(
                        "CREATE INDEX IF NOT EXISTS idx_rotation_jobs_label_time "
                        "ON rotation_jobs(label, started_at)"
                    )
            except Exception as exc:
                logger.warning("[warp-rotate] budget store init failed: %s", exc)

    def allow(self, label: str, max_per_hour: int) -> bool:
        if max_per_hour <= 0:
            return True
        cutoff = time.time() - 3600.0
        try:
            with self._lock, self._connect() as conn:
                row = conn.execute(
                    "SELECT COUNT(*) FROM rotation_jobs "
                    "WHERE label=? AND started_at>=?",
                    (str(label), cutoff),
                ).fetchone()
                return int(row[0] if row else 0) < int(max_per_hour)
        except Exception as exc:
            logger.warning("[warp-rotate] budget check failed: %s", exc)
            return True

    def record(
        self,
        label: str,
        *,
        provider: str = "warp",
        trigger: str = "auto",
        result: str = "started",
        reason: str | None = None,
    ) -> int | None:
        try:
            with self._lock, self._connect() as conn:
                cur = conn.execute(
                    "INSERT INTO rotation_jobs "
                    "(provider,label,trigger,result,reason,started_at) "
                    "VALUES (?,?,?,?,?,?)",
                    (
                        str(provider),
                        str(label),
                        str(trigger),
                        str(result),
                        reason,
                        time.time(),
                    ),
                )
                return int(cur.lastrowid) if cur.lastrowid is not None else None
        except Exception as exc:
            logger.warning("[warp-rotate] budget record failed: %s", exc)
            return None

    def finish(self, job_id: int | None, result: str, reason: str | None = None) -> None:
        """Clôt un job de rotation avec son résultat réel (ok/failed)."""
        if job_id is None:
            return
        try:
            with self._lock, self._connect() as conn:
                conn.execute(
                    "UPDATE rotation_jobs SET result=?, reason=?, finished_at=? "
                    "WHERE id=?",
                    (str(result), reason, time.time(), int(job_id)),
                )
        except Exception as exc:
            logger.warning("[warp-rotate] budget finish failed: %s", exc)


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
    return quota_block(ip)


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
        max_rotations_per_hour: int = 10,
        min_interval_s: float | None = None,
        budget_store: Any = None,
    ) -> None:
        self.image = image
        self.health_timeout_s = float(health_timeout_s)
        self.egress_timeout_s = float(egress_timeout_s)
        self.max_rotations_per_hour = max(0, int(max_rotations_per_hour or 0))
        self.min_interval_s = float(
            MIN_ROTATION_INTERVAL_S if min_interval_s is None else min_interval_s
        )
        self.budget_store = budget_store

    def request_rotation(self, endpoint, pool, persist) -> bool:
        label = str(getattr(endpoint, "label", "") or "").strip()
        container = str(getattr(endpoint, "container", "") or "").strip()
        if not label or not container or not bool(getattr(endpoint, "managed", False)):
            return False
        # La rotation d'un endpoint IPv4 WARP ne peut pas garantir un /32
        # frais (pool NAT) et recrée inutilement le tunnel IPv6 de la station.
        if not bool(getattr(endpoint, "ipv6", True)):
            return False
        # IPv4 et IPv6 d'une même station partagent le conteneur : le budget,
        # le cooldown et l'inflight doivent être keyés sur le conteneur, sinon
        # les deux familles déclenchent deux rotations pour un seul tunnel.
        key = container
        with _LOCK:
            if key in INFLIGHT:
                return False
            now = time.monotonic()
            last = float(LAST_ROTATION.get(key, 0.0) or 0.0)
            if now - last < self.min_interval_s:
                return False
            recent = [
                ts
                for ts in ROTATION_TIMES.get(key, [])
                if now - float(ts) < 3600.0
            ]
            if self.budget_store is not None:
                if not self.budget_store.allow(key, self.max_rotations_per_hour):
                    logger.warning(
                        "[warp-rotate] %s: budget horaire atteint (store)",
                        label,
                    )
                    return False
            elif self.max_rotations_per_hour and len(recent) >= self.max_rotations_per_hour:
                logger.warning(
                    "[warp-rotate] %s: budget horaire atteint (%d/%d)",
                    label,
                    len(recent),
                    self.max_rotations_per_hour,
                )
                return False
            job_id: int | None = None
            if self.budget_store is not None:
                job_id = self.budget_store.record(key)
            else:
                ROTATION_TIMES[key] = [*recent, now]
            LAST_ROTATION[key] = now
            INFLIGHT.add(key)
        thread = threading.Thread(
            target=self._run,
            args=(endpoint, pool, persist, job_id),
            name=f"warp-rotate-{label}",
            daemon=True,
        )
        thread.start()
        return True

    def _run(self, endpoint, pool, persist, job_id: int | None = None) -> None:
        label = str(getattr(endpoint, "label", "") or "")
        outcome_result = "failed"
        outcome_reason = "unknown"
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
                probe = getattr(pool, "_post_rotation_probe_cb", None)
                result = self.rotate(
                    endpoint,
                    forbidden_blocks=forbidden,
                    probe=probe,
                )
                outcome_result = "ok" if result.ok else "failed"
                outcome_reason = str(result.reason or "") or None
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
                # Une rotation recrée le tunnel : l'autre famille (IPv4/IPv6)
                # de la même station a forcément une nouvelle IP, elle aussi.
                self._refresh_siblings(endpoint, pool, persist)
        except Exception as exc:
            outcome_result = "failed"
            outcome_reason = f"exception:{type(exc).__name__}"
            logger.warning("[warp-rotate] %s failed: %s", label, exc)
            log_evidence(
                Evidence(
                    provider="warp",
                    slot=abs(int(getattr(endpoint, "_station", 0) or 0)),
                    cause="rotation",
                    action="rotate",
                    result="failed",
                    evidence=str(exc)[:200],
                    extra={"label": label},
                ),
                level=logging.WARNING,
            )
        finally:
            if job_id is not None and self.budget_store is not None:
                try:
                    self.budget_store.finish(job_id, outcome_result, outcome_reason)
                except Exception:
                    pass
            with _LOCK:
                INFLIGHT.discard(
                    str(getattr(endpoint, "container", "") or label)
                )

    def _refresh_siblings(self, endpoint, pool, persist) -> None:
        """Réconcilie les endpoints qui partagent le conteneur tourné.

        WARP expose IPv4 et IPv6 sur le même tunnel : les deux egress changent
        ensemble. Sans cette passe, la famille non tournée garde une IP et un
        bucket périmés jusqu'au prochain refresh périodique.
        """
        container = str(getattr(endpoint, "container", "") or "")
        if not container:
            return
        from free.egress_reconcile import reconcile_endpoint

        try:
            endpoints = list(pool._socks5_enabled_eps())
        except Exception:
            return
        for other in endpoints:
            if other is endpoint:
                continue
            if str(getattr(other, "container", "") or "") != container:
                continue
            try:
                result = reconcile_endpoint(
                    other,
                    persist=persist,
                    timeout_s=self.egress_timeout_s,
                )
                if result.get("ok"):
                    try:
                        health = pool._station_health.get(
                            int(getattr(other, "_station", 0))
                        )
                        if health is not None:
                            health.repair_succeeded()
                    except Exception:
                        pass
            except Exception as exc:
                logger.debug(
                    "[warp-rotate] sibling refresh %s failed: %s",
                    getattr(other, "label", "?"),
                    exc,
                )

    def rotate(
        self,
        endpoint,
        *,
        forbidden_blocks: set[str] | None = None,
        probe=None,
    ) -> WarpRotationResult:
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
                if ipv6:
                    last_reason = "duplicate_block"
                    continue
                # WARP IPv4 sort d'un petit pool NAT partagé : un /32 déjà
                # utilisé par une autre station est normal, et le ledger
                # partage le bucket. Recréer quatre fois pour chercher une
                # adresse libre ne fait que churner le tunnel.
                logger.info(
                    "[warp-rotate] %s: shared IPv4 block %s accepted (NAT pool)",
                    label,
                    new_block,
                )
            # Le probe quota (modèle réel) doit tourner AVANT d'accepter le
            # bloc : une IP fraîche peut déjà être épuisée upstream. On pose
            # l'egress candidat pour que le ledger et l'identité pointent sur
            # la bonne IP ; le probe 429 rejette et on recrée.
            endpoint.egress_ip = new_ip
            endpoint.current_ip = new_ip
            if new_block:
                endpoint.block = new_block
            endpoint._egress_probe_attempted = True
            if callable(probe):
                try:
                    verdict = probe(endpoint)
                except Exception:
                    verdict = None
                if verdict is False:
                    last_reason = "quota_exhausted"
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
        # [011] La rotation a validé l'egress (et le quota via la sonde
        # post-rotation) : remettre la station saine tout de suite. Sans cela
        # elle reste « suspect/repairing » jusqu'au probe de fond et la
        # capacité affichée tombe à 2/6 après une rotation de flotte.
        try:
            sid = pool._station_id(endpoint)
            health = pool._station_health.get(sid)
            if health is not None:
                health.repair_succeeded()
            pool._note_health_probe(endpoint, True, cause="rotation")
            pool._note_health_probe(endpoint, True, cause="rotation")
        except Exception:
            pass
        try:
            persist(str(getattr(endpoint, "label", "") or ""), str(result.new_ip or ""), result.block or "")
        except Exception as exc:
            logger.warning("[warp-rotate] persist failed: %s", exc)
        # Le probe a déjà validé l'egress et le quota dans ``rotate()`` (il
        # doit pouvoir rejeter un bloc et relancer la rotation). Ne pas le
        # rejouer ici : chaque probe consomme une requête free.
        logger.info("[warp-rotate] %s: %s", result.label, result.as_dict())
        log_evidence(
            Evidence(
                provider="warp",
                slot=abs(int(getattr(endpoint, "_station", 0) or 0)),
                cause="rotation",
                action="rotate",
                result="ok",
                evidence=f"{result.old_ip}->{result.new_ip}",
                extra={
                    "label": result.label,
                    "old_block": quota_block(result.old_ip),
                    "new_block": result.block,
                    "attempts": result.attempts,
                },
            )
        )

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
        provider = str(getattr(endpoint, "provider", "") or "warp")
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
                f"127.0.0.1:{port}:1080",
                "--restart",
                "unless-stopped",
                "--memory",
                "512m",
                "--pids-limit",
                "128",
                "--label",
                f"opencode.provider={provider}",
                "--label",
                f"opencode.label={container}",
                "--label",
                "opencode.managed=true",
                "--env",
                f"OPENCODE_STATION_LABEL={container}",
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
        from free.egress_probe import resolve_egress

        deadline = time.monotonic() + self.egress_timeout_s
        while time.monotonic() < deadline:
            ip = resolve_egress(
                proxy_url=f"socks5h://127.0.0.1:{port}",
                ipv6=bool(ipv6),
                timeout_s=10.0,
            )
            if ip:
                return ip
            time.sleep(3)
        return None


class WarpDockerProvider:
    """Adapter ``EgressProvider`` historique autour de ``WarpRotator``.

    Aucun changement de comportement : c'est la même rotation Docker, exposée
    via le contrat commun de la feature 007.
    """

    name = "warp"

    def __init__(self, rotator: WarpRotator | None = None) -> None:
        self.rotator = rotator or WarpRotator()

    def rotate(
        self,
        endpoint,
        *,
        forbidden_blocks: set[str] | None = None,
        probe=None,
    ) -> EgressRotationResult:
        result = self.rotator.rotate(
            endpoint,
            forbidden_blocks=forbidden_blocks,
            probe=probe,
        )
        return EgressRotationResult(
            label=result.label,
            ok=result.ok,
            old_ip=result.old_ip,
            new_ip=result.new_ip,
            block=result.block,
            attempts=result.attempts,
            reason=result.reason,
            provider=self.name,
        )

    def refresh_egress(self, endpoint) -> str | None:
        try:
            return endpoint.refresh_egress()
        except Exception:
            return None

    def health(self) -> dict[str, Any]:
        return {"name": self.name, "ok": True, "provider": "warp"}


__all__ = [
    "WarpDockerProvider",
    "WarpRotator",
    "WarpRotationResult",
    "block_for_ip",
    "block_for_ledger_key",
]
