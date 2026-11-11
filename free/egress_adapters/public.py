"""Public proxy adapter: declarative sources, isolated probe, free gate.

No candidate is promoted from liveness alone.  A public option becomes
``usable`` only after a real free ``200``; a ``429`` is ``quota`` with the
provider reset; a timeout/403/5xx is ``dead``.
"""

from __future__ import annotations

import asyncio
import ipaddress
import time
from dataclasses import dataclass
from pathlib import Path
from types import SimpleNamespace
from typing import Any

from free.egress_adapters.base import RefreshResult
from free.egress_catalog import (
    Observation,
    ObservationPhase,
    OptionStatus,
    Provider,
    ProviderPolicy,
    new_option,
)
from free.egress_sources import (
    SourceDefinition,
    dedupe_candidates,
    interleave_by_source,
    load_source_file,
    parse_payload,
    read_source_text,
)
from free.egress_store import EgressStore
from free.egress_validation import ProbeOutcome, classify_outcome


@dataclass(frozen=True, slots=True)
class CandidateProbe:
    url: str
    egress_ip: str = ""
    liveness_status: int = 0
    free_status: int = 0
    latency_ms: float = 0.0
    retry_after_s: float = 0.0
    body: str = ""
    error: str = ""


async def probe_public_candidates(
    candidates: list[dict[str, Any]],
    *,
    concurrency: int = 8,
    timeout: float = 20.0,
    model: str = "deepseek-v4.1-flash-free",
    deadline_s: float = 240.0,
) -> dict[str, CandidateProbe]:
    """Liveness + free gate through one isolated worker.

    The worker (process or container) is the only component that talks to the
    public proxy; the main process never connects to it directly.  The free
    gate reuses the exact production wire via ``opencode``.
    """

    if not candidates:
        return {}
    from free.public_egress_worker import start_worker_process

    upstreams = {
        f"p{index}": str(candidate["url"])
        for index, candidate in enumerate(candidates)
    }
    try:
        handle = await asyncio.to_thread(start_worker_process, upstreams)
    except Exception as exc:
        return {
            str(candidate["url"]): CandidateProbe(
                url=str(candidate["url"]),
                error=f"worker: {type(exc).__name__}: {exc}"[:200],
            )
            for candidate in candidates
        }

    import opencode as oc

    semaphore = asyncio.Semaphore(max(1, min(12, int(concurrency or 8))))

    async def _one(index: int, candidate: dict[str, Any]) -> tuple[str, CandidateProbe]:
        url = str(candidate["url"])
        async with semaphore:
            local = handle.local_proxy_url(f"p{index}")
            started = time.monotonic()
            egress = ""
            live_status = 0
            error = ""
            try:
                egress, live_status = await asyncio.to_thread(
                    _read_egress, local, max(3.0, min(float(timeout), 12.0))
                )
            except Exception as exc:
                error = f"liveness: {type(exc).__name__}: {exc}"[:200]
            if not egress:
                return url, CandidateProbe(
                    url=url,
                    liveness_status=live_status,
                    latency_ms=(time.monotonic() - started) * 1000.0,
                    error=error or "ipify failed",
                )
            station = SimpleNamespace(
                pid=f"p{index}",
                socks5_url=local,
                provider="public",
                managed=False,
                scheme="https",
                label=f"public:{egress}:{candidate.get('port') or 443}",
                current_ip=egress,
                egress_ip=egress,
                ipv6=(":" in egress),
                container="",
            )
            free_status = 0
            body = ""
            retry_after = 0.0
            free_timeout = 25.0 if candidate.get("revalidate") else 15.0
            try:
                response = await asyncio.wait_for(
                    oc._do_free_request_curl_cffi(
                        {
                            "model": model,
                            "messages": [{"role": "user", "content": "ping"}],
                        },
                        {},
                        proxy_url=local,
                        station=station,
                        endpoint=oc.API_BASE_FREE,
                        timeout=(free_timeout, 60.0),
                    ),
                    timeout=free_timeout + 5.0,
                )
                free_status = int(getattr(response, "status_code", 0) or 0)
                headers = dict(getattr(response, "headers", None) or {})
                body = str(getattr(response, "text", "") or "")[:2_000]
                if free_status == 429:
                    from free.egress_validation import parse_retry_after

                    reset_at = parse_retry_after(headers, body, now=time.time())
                    if reset_at > 0:
                        retry_after = max(0.0, reset_at - time.time())
                close = getattr(response, "aclose", None)
                if callable(close):
                    try:
                        await close()
                    except Exception:
                        pass
            except Exception as exc:
                error = f"free gate: {type(exc).__name__}: {exc}"[:200]
            return url, CandidateProbe(
                url=url,
                egress_ip=egress,
                liveness_status=live_status,
                free_status=free_status,
                latency_ms=(time.monotonic() - started) * 1000.0,
                retry_after_s=retry_after,
                body=body,
                error=error,
            )

    tasks = [
        asyncio.create_task(_one(index, candidate))
        for index, candidate in enumerate(candidates)
    ]
    done: set[asyncio.Task] = set()
    try:
        done, pending = await asyncio.wait(
            tasks,
            timeout=max(10.0, float(deadline_s)),
        )
        for task in pending:
            task.cancel()
        if pending:
            await asyncio.gather(*pending, return_exceptions=True)
    finally:
        await asyncio.to_thread(handle.stop)
    mapped: dict[str, CandidateProbe] = {}
    for task in done:
        try:
            result = task.result()
        except BaseException:
            continue
        if isinstance(result, tuple) and len(result) == 2:
            mapped[str(result[0])] = result[1]
    return mapped


def _read_egress(proxy_url: str, timeout: float) -> tuple[str, int]:
    from curl_cffi import requests

    response = requests.get(
        "https://api64.ipify.org",
        proxies={"http": proxy_url, "https": proxy_url},
        timeout=timeout,
        stream=False,
    )
    text = str(getattr(response, "text", "") or "").strip()
    status = int(getattr(response, "status_code", 0) or 0)
    if status == 200 and text and len(text) <= 64:
        return text, status
    return "", status


class PublicEgressAdapter:
    provider = str(Provider.PUBLIC)

    def __init__(
        self,
        *,
        sources: list[SourceDefinition] | None = None,
        fetcher=None,
        probe=None,
        concurrency: int = 8,
        timeout: float = 20.0,
        model: str = "deepseek-v4.1-flash-free",
        discovery_interval_s: float = 21_600.0,
        cycle_budget: int = 500,
        cycle_timeout_s: float = 300.0,
        denylist: list[str] | None = None,
        threat_intel: Any = None,
        sources_file: str = "",
    ) -> None:
        self.sources = list(sources or [])
        self._fetcher = fetcher
        self._probe = probe
        self.concurrency = max(1, int(concurrency))
        self.timeout = max(1.0, float(timeout))
        self.model = str(model or "deepseek-v4.1-flash-free")
        self.discovery_interval_s = max(60.0, float(discovery_interval_s))
        self.cycle_budget = max(1, int(cycle_budget))
        self.cycle_timeout_s = max(0.01, float(cycle_timeout_s))
        self._denied: list[ipaddress.IPv4Network | ipaddress.IPv6Network] = []
        for raw in denylist or ():
            try:
                self._denied.append(ipaddress.ip_network(str(raw).strip(), strict=False))
            except ValueError:
                continue
        self._threat_intel = threat_intel
        self._candidates: list[dict[str, Any]] = []
        self._last_discovery_at = 0.0
        self._source_last: dict[str, float] = {}
        self._cursor = 0
        self._cursor_loaded = False
        self._sources_file = str(sources_file or "").strip()
        self._sources_file_mtime = 0.0

    def _reload_sources(self, store: EgressStore) -> None:
        """Pick up external-file edits and API-added sources without restart."""

        if self._sources_file:
            target = Path(self._sources_file)
            if not target.is_absolute():
                target = Path(__file__).resolve().parent.parent.parent / target
            try:
                mtime = target.stat().st_mtime
            except OSError:
                mtime = 0.0
            if mtime and mtime != self._sources_file_mtime:
                try:
                    file_sources = load_source_file(
                        target,
                        default_interval=int(self.discovery_interval_s),
                    )
                except Exception:
                    file_sources = []
                if file_sources:
                    self._store_sources(store, file_sources)
                    self._sources_file_mtime = mtime
        try:
            rows = store.list_sources()
        except Exception:
            return
        if not rows:
            return
        parsed: list[SourceDefinition] = []
        for row in rows:
            try:
                parsed.append(
                    SourceDefinition.from_config(
                        row,
                        default_interval=int(self.discovery_interval_s),
                    )
                )
            except (TypeError, ValueError):
                continue
        self.sources = sorted(parsed, key=lambda source: source.priority, reverse=True)

    @staticmethod
    def _store_sources(store: EgressStore, sources: list[SourceDefinition]) -> None:
        for source in sources:
            try:
                store.upsert_source(
                    {
                        "id": source.id,
                        "type": source.type,
                        "url": source.url,
                        "scheme": source.scheme,
                        "enabled": source.enabled,
                        "interval_s": source.interval_s,
                        "config": {
                            **source.config,
                            "priority": source.priority,
                            "notes": source.notes,
                            "tags": list(source.tags),
                        },
                    }
                )
            except Exception:
                continue

    async def refresh(
        self,
        store: EgressStore,
        policy: ProviderPolicy,
        *,
        run_id: str = "",
        max_candidates: int | None = None,
        observe_only: bool = False,
    ) -> RefreshResult:
        result = RefreshResult(provider=self.provider, observe_only=observe_only)
        now = time.time()
        self._reload_sources(store)
        enabled = [
            source
            for source in self.sources
            if source.enabled
            and source.type in ("public_text", "public_json", "geonode_json", "local_text")
        ]
        if not self._cursor_loaded:
            try:
                self._cursor = max(0, int(store.get_meta("public_cursor", "0") or 0))
            except (TypeError, ValueError):
                self._cursor = 0
            self._cursor_loaded = True
        valid_ids = {source.id for source in enabled}
        self._candidates = [
            candidate
            for candidate in self._candidates
            if str(candidate.get("source_id") or "") in valid_ids
        ]
        due = [
            source
            for source in enabled
            if now - float(self._source_last.get(source.id, 0.0)) >= max(60.0, float(source.interval_s))
        ]
        for source in due:
            try:
                text = await asyncio.to_thread(
                    read_source_text,
                    source,
                    timeout=self.timeout,
                    fetcher=self._fetcher,
                )
            except Exception as exc:
                result.errors.append(f"{source.id}: {type(exc).__name__}: {exc}"[:300])
                continue
            rows = parse_payload(source, text)
            for row in rows:
                row["source_id"] = source.id
            try:
                store.record_source_candidates(source.id, len(rows))
            except Exception:
                pass
            self._candidates = [
                candidate
                for candidate in self._candidates
                if str(candidate.get("source_id") or "") != source.id
            ]
            self._candidates.extend(rows)
            self._source_last[source.id] = now
            try:
                store.mark_source_run(source.id)
            except Exception:
                pass
        candidates = list(self._candidates)
        limit = max(
            1,
            min(
                int(max_candidates or self.cycle_budget or 500),
                int(policy.max_options or 20_000),
            ),
        )
        catalog_cap = max(1, int(policy.max_options or 20_000))
        candidates = interleave_by_source(candidates, limit=catalog_cap)
        candidates = dedupe_candidates(candidates, limit=catalog_cap)
        if len(candidates) > limit:
            start = self._cursor % len(candidates)
            window = candidates[start : start + limit]
            if len(window) < limit:
                window.extend(candidates[: limit - len(window)])
            self._cursor = (start + limit) % len(candidates)
            try:
                store.set_meta("public_cursor", str(self._cursor))
            except Exception:
                pass
            candidates = window
        else:
            self._cursor = 0
            try:
                store.set_meta("public_cursor", "0")
            except Exception:
                pass
        # [013] Revalidation des options actuellement `usable` : le curseur
        # de découverte peut mettre 20 h à revenir dessus, alors que leur TTL
        # n'est que de 12 h. On les re-teste à chaque cycle, en plus du lot
        # de découverte.
        active_rows: list[dict[str, Any]] = []
        active_endpoints: set[tuple[str, int]] = set()
        candidates_for_revalidation = [
            *store.list_options(
                provider=Provider.PUBLIC,
                status=OptionStatus.USABLE,
                limit=policy.max_active,
            ),
            *store.list_options(
                provider=Provider.PUBLIC,
                status=OptionStatus.REACHABLE,
                limit=max(8, policy.max_active),
            ),
        ]
        seen_revalidation: set[str] = set()
        for option in candidates_for_revalidation:
            if option.key in seen_revalidation:
                continue
            seen_revalidation.add(option.key)
            if not option.host or not option.port:
                continue
            if self._is_denied(option.host) or (
                option.egress_ip and self._is_denied(option.egress_ip)
            ):
                store.apply(
                    option,
                    Observation(
                        phase=ObservationPhase.EGRESS,
                        status=0,
                        result=str(OptionStatus.DEAD),
                        observed_at=time.time(),
                        message="option active bloquée par la denylist/threat-intel",
                        run_id=run_id,
                    ),
                    policy,
                    run_id=run_id,
                )
                continue
            scheme = option.kind if option.kind in ("http", "https", "socks5", "socks5h") else "https"
            host = f"[{option.host}]" if ":" in option.host else option.host
            active_endpoints.add((str(option.host), int(option.port)))
            active_rows.append(
                {
                    "url": f"{scheme}://{host}:{int(option.port)}",
                    "scheme": scheme,
                    "host": str(option.host),
                    "port": int(option.port),
                    "ipv6": ":" in str(option.host),
                    "source_id": "revalidate",
                    "revalidate": True,
                }
            )
        if active_rows:
            candidates = active_rows + [
                candidate
                for candidate in candidates
                if (str(candidate.get("host") or ""), int(candidate.get("port") or 0))
                not in active_endpoints
            ]
        if self._denied or self._threat_intel is not None:
            kept = [
                candidate
                for candidate in candidates
                if not self._is_denied(str(candidate.get("host") or ""))
            ]
            result.skipped += len(candidates) - len(kept)
            candidates = kept
        blocked = store.blocked_endpoints(self.provider)
        if blocked:
            kept = [
                candidate
                for candidate in candidates
                if (str(candidate.get("host") or ""), int(candidate.get("port") or 0)) not in blocked
            ]
            result.skipped = len(candidates) - len(kept)
            candidates = kept
        result.candidates = len(candidates)
        if not candidates:
            result.errors.append("aucun candidat public hors quarantaine / aucune source active")
            return result

        if self._probe is not None:
            probe_coro = self._probe(
                candidates,
                concurrency=self.concurrency,
                timeout=self.timeout,
                model=self.model,
            )
        else:
            probe_coro = probe_public_candidates(
                candidates,
                concurrency=self.concurrency,
                timeout=self.timeout,
                model=self.model,
                deadline_s=max(10.0, self.cycle_timeout_s - 10.0),
            )
        try:
            probed = await asyncio.wait_for(probe_coro, timeout=self.cycle_timeout_s)
        except TimeoutError:
            result.errors.append(
                f"cycle timeout après {self.cycle_timeout_s:.0f}s ({len(candidates)} candidats)"
            )
            return result
        if not isinstance(probed, dict):
            result.errors.append("probe publique sans résultat")
            return result
        if len(probed) < len(candidates):
            result.errors.append(
                f"cycle partiel: {len(candidates) - len(probed)} candidat(s) non testé(s)"
            )

        # Deduplicate by egress IP: one bucket per egress, best free status.
        best: dict[str, tuple[dict[str, Any], CandidateProbe]] = {}
        for candidate in candidates:
            outcome = probed.get(str(candidate["url"]))
            if outcome is None or not outcome.egress_ip:
                option = new_option(
                    provider=self.provider,
                    kind=str(candidate.get("scheme") or "https"),
                    host=str(candidate.get("host") or ""),
                    port=int(candidate.get("port") or 0),
                    source_ids=(str(candidate.get("source_id") or ""),),
                    now=time.time(),
                )
                self._record(
                    store,
                    option,
                    CandidateProbe(url=str(candidate["url"]), error=(outcome.error if outcome else "no probe")),
                    policy,
                    run_id,
                    phase=ObservationPhase.LIVENESS,
                )
                result.dead += 1
                continue
            current = best.get(outcome.egress_ip)
            if current is None or _probe_rank(outcome) > _probe_rank(current[1]):
                best[outcome.egress_ip] = (candidate, outcome)

        for candidate, outcome in best.values():
            option = new_option(
                provider=self.provider,
                kind=str(candidate.get("scheme") or "https"),
                host=str(candidate.get("host") or ""),
                port=int(candidate.get("port") or 0),
                egress_ip=outcome.egress_ip,
                source_ids=(str(candidate.get("source_id") or ""),),
                now=time.time(),
            )
            if int(outcome.free_status or 0) == 0:
                self._record(
                    store,
                    option,
                    outcome,
                    policy,
                    run_id,
                    phase=ObservationPhase.LIVENESS,
                    liveness_only=True,
                )
                result.dead += 1
                continue
            observation = Observation(
                phase=ObservationPhase.FREE_GATE,
                status=int(outcome.free_status or 0),
                result=str(OptionStatus.CANDIDATE),
                observed_at=time.time(),
                latency_ms=outcome.latency_ms,
                message=outcome.error,
                run_id=run_id,
            )
            classified, reset_at, message = classify_outcome(
                ProbeOutcome(
                    status=int(outcome.free_status or 0),
                    egress_ip=outcome.egress_ip,
                    latency_ms=outcome.latency_ms,
                    error=outcome.error,
                    retry_after_s=outcome.retry_after_s,
                    body=outcome.body,
                ),
                phase=ObservationPhase.FREE_GATE,
            )
            applied = store.apply(
                option,
                Observation(
                    phase=observation.phase,
                    status=observation.status,
                    result=classified,
                    observed_at=observation.observed_at,
                    latency_ms=observation.latency_ms,
                    message=message,
                    run_id=run_id,
                ),
                policy,
                reset_at=reset_at,
                run_id=run_id,
            )
            if applied.status == OptionStatus.USABLE:
                result.promoted += 1
                source_id = str(candidate.get("source_id") or "")
                if source_id and source_id != "revalidate":
                    try:
                        store.record_source_promotion(source_id)
                    except Exception:
                        pass
            elif applied.status == OptionStatus.QUOTA:
                result.quarantined += 1
            elif applied.status == OptionStatus.AUTH:
                result.quarantined += 1
            else:
                result.dead += 1
        return result

    def _is_denied(self, host: str) -> bool:
        try:
            address = ipaddress.ip_address(str(host or "").strip())
        except ValueError:
            return False
        if any(address in network for network in self._denied):
            return True
        if self._threat_intel is not None:
            try:
                return bool(self._threat_intel.matches(host))
            except Exception:
                return False
        return False

    def _record(
        self,
        store: EgressStore,
        option,
        outcome: CandidateProbe,
        policy: ProviderPolicy,
        run_id: str,
        *,
        phase: str,
        liveness_only: bool = False,
    ) -> None:
        status = int(outcome.liveness_status if liveness_only else outcome.free_status)
        result = (
            OptionStatus.REACHABLE
            if liveness_only and status == 200 and outcome.egress_ip
            else ("dead" if liveness_only else OptionStatus.CANDIDATE)
        )
        if liveness_only and int(outcome.liveness_status or 0) == 0:
            result = OptionStatus.DEAD
        store.apply(
            option,
            Observation(
                phase=phase,
                status=status,
                result=str(result),
                observed_at=time.time(),
                latency_ms=outcome.latency_ms,
                message=outcome.error,
                run_id=run_id,
            ),
            policy,
            run_id=run_id,
        )


def _probe_rank(outcome: CandidateProbe) -> tuple[int, float]:
    status_rank = 3 if int(outcome.free_status or 0) == 200 else 2 if int(outcome.free_status or 0) == 429 else 1
    return status_rank, -float(outcome.latency_ms or 0.0)


__all__ = ["CandidateProbe", "PublicEgressAdapter", "probe_public_candidates"]
