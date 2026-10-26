#!/usr/bin/env python3
"""Calcule le rapport SLO à partir de logs/station_slo.jsonl.

Usage :
    python scripts/station_slo_report.py
    python scripts/station_slo_report.py --markdown
    python scripts/station_slo_report.py --since 2026-10-26T12:00:00Z
"""

from __future__ import annotations

import argparse
import json
import math
import sys
from collections import Counter, defaultdict
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent


def _pct(values: list[float], p: float) -> float | None:
    if not values:
        return None
    ordered = sorted(values)
    if len(ordered) == 1:
        return ordered[0]
    rank = max(1, min(len(ordered), math.ceil(p / 100.0 * len(ordered))))
    return ordered[rank - 1]


def _valid(row: dict) -> bool:
    return "error" not in row and isinstance(row.get("total"), int)


def build_report(
    inp: str | Path,
    since: str | None = None,
    until: str | None = None,
) -> str:
    """Construit le rapport Markdown pour ``inp`` (JSONL) sur la fenêtre."""
    path = Path(inp)
    if not path.is_absolute():
        path = ROOT / path
    if not path.exists():
        return f"absent: {path}"
    rows = []
    with path.open(encoding="utf-8") as f:
        for line in f:
            line = line.strip()
            if not line:
                continue
            try:
                row = json.loads(line)
            except Exception:
                continue
            _at = str(row.get("at") or "")
            if since and _at < since:
                continue
            if until and _at > until:
                continue
            rows.append(row)
    if not rows:
        return "aucun échantillon"

    valid = [r for r in rows if _valid(r)]
    errors = [r for r in rows if not _valid(r)]
    times = [r.get("at") for r in rows if r.get("at")]
    ms = [float(r["ms"]) for r in valid if isinstance(r.get("ms"), (int, float))]
    ages = [
        float(r["status_age_s"])
        for r in valid
        if isinstance(r.get("status_age_s"), (int, float))
    ]
    n = len(valid) or 1
    full = [
        r
        for r in valid
        if r.get("healthy") == r.get("total") and r.get("routable") == r.get("total")
    ]
    routable = [r for r in valid if r.get("routable") == r.get("total")]
    boot_errors = [r for r in valid if r.get("boot_error")]
    stale = [r for r in valid if r.get("stale")]
    refresh_errors = [r for r in valid if r.get("refresh_error")]

    per: dict[int, dict] = defaultdict(
        lambda: {
            "samples": 0,
            "connected": 0,
            "degraded": 0,
            "error": 0,
            "with_ip": 0,
            "secondary": 0,
            "max_flap": 0,
            "switches_first": None,
            "switches_last": None,
        }
    )
    for r in valid:
        for st in r.get("stations") or []:
            sid = st.get("station")
            if sid is None:
                continue
            d = per[int(sid)]
            d["samples"] += 1
            status = str(st.get("status") or "")
            if status == "connected":
                d["connected"] += 1
            elif status == "degraded":
                d["degraded"] += 1
            else:
                d["error"] += 1
            if st.get("ip"):
                d["with_ip"] += 1
            if st.get("secondary"):
                d["secondary"] += 1
            try:
                d["max_flap"] = max(d["max_flap"], int(st.get("flap_1h") or 0))
            except Exception:
                pass
            sw = st.get("total_switches")
            if isinstance(sw, int):
                if d["switches_first"] is None:
                    d["switches_first"] = sw
                d["switches_last"] = sw

    lines = []
    lines.append("# Rapport SLO stations")
    lines.append("")
    if times:
        lines.append(f"- Fenêtre : `{times[0]}` → `{times[-1]}` ({len(rows)} échantillons)")
    lines.append(f"- Échantillons valides : {len(valid)} / erreurs : {len(errors)}")
    if ms:
        lines.append(
            f"- Latence API : p50 {_pct(ms, 50):.0f} ms · p95 {_pct(ms, 95):.0f} ms · max {max(ms):.0f} ms"
        )
    if ages:
        lines.append(
            f"- Âge de l'état : p95 {_pct(ages, 95):.0f} s · max {max(ages):.0f} s"
        )
    lines.append(f"- Flotte complète (healthy+routable) : {len(full)}/{n} = {100*len(full)/n:.1f} %")
    lines.append(f"- Routable 6/6 : {len(routable)}/{n} = {100*len(routable)/n:.1f} %")
    lines.append(f"- `boot_error` : {len(boot_errors)}/{n} = {100*len(boot_errors)/n:.1f} %")
    lines.append(f"- `stale` : {len(stale)}/{n} = {100*len(stale)/n:.1f} %")
    lines.append(f"- `refresh_error` : {len(refresh_errors)}/{n} = {100*len(refresh_errors)/n:.1f} %")
    lines.append("")
    lines.append("| Station | Disponible | Avec IP | Degraded | Error | Secondary | Flap max | Rotations |")
    lines.append("|---:|---:|---:|---:|---:|---:|---:|---:|")
    for sid in sorted(per):
        d = per[sid]
        ok = d["connected"] + d["degraded"]
        rotations = None
        if d["switches_first"] is not None and d["switches_last"] is not None:
            rotations = d["switches_last"] - d["switches_first"]
        lines.append(
            f"| {sid} | {100*ok/d['samples']:.1f} % | {100*d['with_ip']/d['samples']:.1f} % | "
            f"{100*d['degraded']/d['samples']:.1f} % | {100*d['error']/d['samples']:.1f} % | "
            f"{100*d['secondary']/d['samples']:.1f} % | {d['max_flap']} | {rotations if rotations is not None else '—'} |"
        )
    # ── Qualité (TTFB/probe, succès, 429) et compteurs upstream ──
    qper: dict[int, dict] = defaultdict(
        lambda: {
            "probe": [],
            "ewma": [],
            "p95": [],
            "max_429": 0,
            "scores": [],
            "last_success": None,
            "last_errors": None,
        }
    )
    probe_all: list[float] = []
    ewma_all: list[float] = []
    p95_all: list[float] = []
    success_rates: list[float] = []
    for r in valid:
        q = r.get("quality") or {}
        for sid, v in (q.get("stations") or {}).items():
            if not isinstance(v, dict):
                continue
            try:
                d = qper[int(sid)]
            except Exception:
                continue
            for key, bucket, all_bucket in (
                ("probe_ms", d["probe"], probe_all),
                ("ewma_ms", d["ewma"], ewma_all),
                ("p95_ms", d["p95"], p95_all),
            ):
                val = v.get(key)
                if isinstance(val, (int, float)):
                    bucket.append(float(val))
                    all_bucket.append(float(val))
            succ, err = v.get("success"), v.get("errors")
            if isinstance(succ, int) and isinstance(err, int):
                d["last_success"] = succ
                d["last_errors"] = err
                if succ + err > 0:
                    success_rates.append(succ / (succ + err))
            r429 = v.get("recent_429")
            if isinstance(r429, int):
                d["max_429"] = max(d["max_429"], r429)
            score = v.get("score")
            if isinstance(score, (int, float)):
                d["scores"].append(float(score))

    m_first = next(
        (
            r["metrics"]
            for r in valid
            if isinstance(r.get("metrics"), dict) and "error" not in r["metrics"]
        ),
        None,
    )
    m_last = next(
        (
            r["metrics"]
            for r in reversed(valid)
            if isinstance(r.get("metrics"), dict) and "error" not in r["metrics"]
        ),
        None,
    )
    metrics_delta = None
    if isinstance(m_first, dict) and isinstance(m_last, dict):
        metrics_delta = {"rotations": {}, "fallback": {}, "free_status": {}}
        for section in metrics_delta:
            for key in set(m_first.get(section, {})) | set(m_last.get(section, {})):
                try:
                    metrics_delta[section][key] = float(
                        m_last.get(section, {}).get(key, 0.0)
                    ) - float(m_first.get(section, {}).get(key, 0.0))
                except Exception:
                    continue

    if qper or metrics_delta:
        lines.append("")
        lines.append("## Qualité et upstream")
        if probe_all:
            lines.append(
                f"- Probe TTFB (station-échantillon) : p50 {_pct(probe_all, 50):.0f} ms · "
                f"p95 {_pct(probe_all, 95):.0f} ms · max {max(probe_all):.0f} ms"
            )
        if ewma_all:
            lines.append(
                f"- TTFB requêtes (EWMA) : p50 {_pct(ewma_all, 50):.0f} ms · "
                f"p95 {_pct(ewma_all, 95):.0f} ms"
            )
        if p95_all:
            lines.append(
                f"- TTFB requêtes (p95 échantillonné) : p50 {_pct(p95_all, 50):.0f} ms · "
                f"p95 {_pct(p95_all, 95):.0f} ms"
            )
        if success_rates:
            lines.append(
                f"- Taux de succès (snapshots) : moyenne {100*sum(success_rates)/len(success_rates):.1f} % · "
                f"min {100*min(success_rates):.1f} %"
            )
        max_429 = max((d["max_429"] for d in qper.values()), default=0)
        lines.append(f"- 429 récents : max {max_429} / station")
        if metrics_delta:
            rot = metrics_delta["rotations"]
            if rot:
                lines.append(
                    "- Rotations (fenêtre) : "
                    + " · ".join(f"{k}={v:.0f}" for k, v in sorted(rot.items()))
                )
            else:
                lines.append("- Rotations (fenêtre) : 0")
            fb = metrics_delta["fallback"]
            fb_total = sum(fb.values())
            fb_detail = ", ".join(f"{k}={v:.0f}" for k, v in sorted(fb.items()) if v)
            lines.append(
                f"- Fallback payant (fenêtre) : total {fb_total:.0f}"
                + (f" ({fb_detail})" if fb_detail else "")
            )
            free = metrics_delta["free_status"]
            free_detail = ", ".join(f"{k}={v:+.0f}" for k, v in sorted(free.items()) if v)
            lines.append(
                "- Statuts free (fenêtre) : " + (free_detail if free_detail else "aucun changement")
            )
        if qper:
            lines.append("")
            lines.append(
                "| Station | Probe p50 | Probe p95 | EWMA p50 | EWMA p95 | Succès | 429 max | Score moyen |"
            )
            lines.append("|---:|---:|---:|---:|---:|---:|---:|---:|")
            for sid in sorted(qper):
                d = qper[sid]
                succ_rate = None
                if d["last_success"] is not None and d["last_errors"] is not None:
                    tot = d["last_success"] + d["last_errors"]
                    succ_rate = (100.0 * d["last_success"] / tot) if tot else None
                _fmt_ms = lambda v: f"{v:.0f} ms" if v is not None else "—"
                succ_txt = f"{succ_rate:.1f} %" if succ_rate is not None else "—"
                score_txt = (
                    f"{sum(d['scores']) / len(d['scores']):.3f}"
                    if d["scores"]
                    else "—"
                )
                lines.append(
                    f"| {sid} | "
                    f"{_fmt_ms(_pct(d['probe'], 50))} | {_fmt_ms(_pct(d['probe'], 95))} | "
                    f"{_fmt_ms(_pct(d['ewma'], 50))} | {_fmt_ms(_pct(d['ewma'], 95))} | "
                    f"{succ_txt} | {d['max_429']} | {score_txt} |"
                )

    reasons = Counter(
        str(st.get("reason"))
        for r in valid
        for st in (r.get("stations") or [])
        if st.get("reason")
    )
    if reasons:
        lines.append("")
        lines.append("## Motifs non-routables")
        for reason, count in reasons.most_common(10):
            lines.append(f"- `{reason}` : {count}")
    return "\n".join(lines)


def main() -> int:
    try:
        sys.stdout.reconfigure(encoding="utf-8")  # type: ignore[attr-defined]
    except Exception:
        pass
    ap = argparse.ArgumentParser()
    ap.add_argument("--in", dest="inp", default="logs/station_slo.jsonl")
    ap.add_argument("--since", default=None, help="ISO UTC, ex. 2026-10-26T12:00:00Z")
    ap.add_argument("--until", default=None, help="ISO UTC, borne haute inclusive")
    ap.add_argument("--markdown", action="store_true")
    args = ap.parse_args()
    print(build_report(args.inp, args.since, args.until))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
