#!/usr/bin/env python3
"""Capture automatique au reset de quota opencode.

Objectif : au premier instant ou le provider re-accepte le tier gratuit,
enregistrer INTEGRALEMENT la requete acceptee (en-tetes dans l'ordre, corps,
empreinte TLS, identite, IP d'egress) et la reponse, puis mesurer le quota
observe (combien de 200 avant le premier 429, sur combien d'IPs).

Modes :
  --until-reset          attend le prochain 00:00 UTC puis capture
  --now [secondes]       capture tout de suite (validation / diagnostic)
  --dry-run              n'ecrit pas, affiche seulement

Sorties (logs/quota_reset_capture/) :
  evidence.jsonl   une ligne JSON par tentative (requete + reponse completes)
  rapport.md       synthese lisible : identifiants, quota observe, diff 200/429
"""
from __future__ import annotations

import argparse
import asyncio
import datetime as dt
import hashlib
import io
import json
import os
import sys
import time

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)

OUT_DIR = os.path.join(ROOT, "logs", "quota_reset_capture")
EVIDENCE = os.path.join(OUT_DIR, "evidence.jsonl")
REPORT = os.path.join(OUT_DIR, "rapport.md")

MODELS = [
    "deepseek-v4.1-flash-free",
    "mimo-v2.6-flash-free",
    "glm-5.3-flash-free",
    "big-pickle",
]

# stations -> ports SOCKS du pool
STATION_PORTS = {i: 1080 + (i - 1) for i in range(1, 7)}


def log(msg: str) -> None:
    ts = dt.datetime.now(dt.timezone.utc).strftime("%H:%M:%S")
    print(f"[{ts}Z] {msg}", flush=True)


def next_utc_midnight(now: dt.datetime | None = None) -> dt.datetime:
    now = now or dt.datetime.now(dt.timezone.utc)
    tomorrow = (now + dt.timedelta(days=1)).date()
    return dt.datetime(
        tomorrow.year, tomorrow.month, tomorrow.day, tzinfo=dt.timezone.utc
    )


def _write_line(payload: dict) -> None:
    os.makedirs(OUT_DIR, exist_ok=True)
    with open(EVIDENCE, "a", encoding="utf-8") as f:
        f.write(json.dumps(payload, ensure_ascii=False, default=str) + "\n")
        f.flush()
        os.fsync(f.fileno())


async def pick_targets(oc) -> list[tuple[int, str]]:
    """(station, ip) reellement servables, via l'API du proxy."""
    import urllib.request

    url = "http://127.0.0.1:4000/api/pool-status"
    try:
        with urllib.request.urlopen(url, timeout=10) as r:
            data = json.loads(r.read().decode())
    except Exception as e:
        log(f"pool-status indisponible: {e}")
        return []
    out = []
    for s in data.get("stations", []):
        ip = s.get("current_ip")
        status = s.get("vpn_status")
        if ip and status in ("connected", "degraded"):
            out.append((int(s.get("station")), str(ip)))
    return out


async def identity_for(oc, ip: str, fresh: bool):
    """Force une installation coherente pour cette IP (option: nouvelle)."""
    import free.identities as fi

    vault = oc._ip_vault()
    if vault is None:
        return {}
    if fresh:
        vault.rotate_installation(ip)
    ident = vault.get(ip)
    return {
        "version": ident.version,
        "client": ident.client,
        "project": ident.project,
        "ses": ident.session_for(None),
        "msg": ident.new_msg(),
        "generation": getattr(ident, "generation", 0),
    }


async def probe(oc, station: int, ip: str, model: str, fresh: bool, label: str) -> dict:
    """UNE tentative complete, avec capture integrale."""
    port = STATION_PORTS.get(station, 1080)
    endpoint = oc._cfg_settings._free_endpoint_for(model) or oc.API_BASE_FREE
    ident = await identity_for(oc, ip, fresh)

    # identity headers reconstruits par la machinerie du proxy
    headers = oc._official_free_headers(endpoint, "capture-conv", ip)
    header_items = list(headers.items())
    imp, fp = oc._get_free_fp_kwargs()

    if "/systemone" in endpoint:
        body = {"model": model, "state": "reply OK", "questions": {"ok": {"type": "score"}}}
        stream_wire = False
    else:
        raw_body = {
            "model": model,
            "messages": [{"role": "user", "content": "Dis OK"}],
            "max_tokens": 16,
        }
        body, stream_wire = oc._free_wire_body(raw_body, force_stream=True, egress_ip=ip)

    body_bytes = oc._serialize_json_body(body)
    record = {
        "ts_utc": dt.datetime.now(dt.timezone.utc).isoformat(),
        "label": label,
        "station": station,
        "egress_ip": ip,
        "model": model,
        "endpoint": endpoint,
        "identity": ident,
        "request": {
            "method": "POST",
            "url": endpoint,
            "headers_order": header_items,
            "body_sha256": hashlib.sha256(body_bytes).hexdigest(),
            "body_len": len(body_bytes),
            "body": body_bytes.decode("utf-8", "replace"),
            "tls": {"impersonate": imp, "fp_keys": sorted((fp or {}).keys())},
        },
    }

    pool = slot = None
    try:
        pool, slot = await oc._get_pooled_curl_session(
            f"socks5://127.0.0.1:{port}", imp, fp, egress_ip=ip
        )
        t0 = time.monotonic()
        resp = await slot.sess.post(
            endpoint, content=body_bytes, headers=headers, timeout=(10, 60), stream=True
        )
        elapsed = round((time.monotonic() - t0) * 1000)
        chunks = []
        try:
            async for ln in resp.aiter_lines():
                chunks.append(
                    ln.decode("utf-8", "replace")
                    if isinstance(ln, (bytes, bytearray))
                    else str(ln)
                )
                if sum(len(c) for c in chunks) > 4000:
                    break
        finally:
            try:
                await resp.aclose()
            except Exception:
                pass
        record["response"] = {
            "status": resp.status_code,
            "headers": list(dict(resp.headers).items()),
            "elapsed_ms": elapsed,
            "body": "\n".join(chunks)[:4000],
        }
    except Exception as e:
        record["response"] = {"status": None, "error": f"{type(e).__name__}: {e}"}
    finally:
        if pool is not None and slot is not None:
            try:
                await pool.checkin(slot)
            except Exception:
                pass

    st = (record.get("response") or {}).get("status")
    ra = None
    for k, v in (record.get("response") or {}).get("headers") or []:
        if str(k).lower() == "retry-after":
            ra = v
    log(f"{label} st={station} ip={ip} model={model} -> {st} ra={ra}")
    return record


async def measure_quota(oc, station: int, ip: str, model: str, max_probes: int = 200) -> dict:
    """Apres une acceptation : compter les 200 avant le premier 429 sur CETTE IP."""
    counts = {"200": 0, "429": 0, "other": 0, "first_429_at": None, "statuses": []}
    for i in range(max_probes):
        rec = await probe(oc, station, ip, model, fresh=False, label=f"quota-{i + 1}")
        st = (rec.get("response") or {}).get("status")
        counts["statuses"].append(st)
        if st == 200:
            counts["200"] += 1
        elif st == 429:
            counts["429"] += 1
            if counts["first_429_at"] is None:
                counts["first_429_at"] = i + 1
            break
        else:
            counts["other"] += 1
            break
        await asyncio.sleep(1.0)
    return counts


def write_report(records: list[dict], measurements: list[dict]) -> None:
    os.makedirs(OUT_DIR, exist_ok=True)
    lines = ["# Capture reset de quota — opencode", ""]
    lines.append(f"Genere le {dt.datetime.now(dt.timezone.utc).isoformat()}")
    lines.append("")
    accepted = [r for r in records if (r.get("response") or {}).get("status") == 200]
    refused = [r for r in records if (r.get("response") or {}).get("status") == 429]
    lines.append(f"- tentatives : {len(records)}")
    lines.append(f"- acceptees (200) : {len(accepted)}")
    lines.append(f"- refusees (429) : {len(refused)}")
    lines.append("")

    if accepted:
        a = accepted[0]
        lines.append("## Premiere requete ACCEPTEE (reference complete)")
        lines.append("")
        lines.append(f"- moment : `{a['ts_utc']}`")
        lines.append(f"- egress : `{a['egress_ip']}` (station {a['station']})")
        lines.append(f"- modele : `{a['model']}`")
        lines.append(f"- endpoint : `{a['endpoint']}`")
        lines.append(f"- identite : `{json.dumps(a['identity'], ensure_ascii=False)}`")
        lines.append("")
        lines.append("### En-tetes envoyes (ordre exact)")
        for k, v in a["request"]["headers_order"]:
            shown = v if str(k).lower() != "authorization" else "Bearer public"
            lines.append(f"1. `{k}: {shown}`")
        lines.append("")
        lines.append(f"- corps : {a['request']['body_len']} octets, sha256 `{a['request']['body_sha256']}`")
        lines.append(f"- TLS : impersonate={a['request']['tls']['impersonate']!r} fp={a['request']['tls']['fp_keys']}")
        lines.append("")
        lines.append("### Reponse")
        lines.append(f"- statut : `{a['response']['status']}` en {a['response']['elapsed_ms']} ms")
        for k, v in a["response"].get("headers") or []:
            lines.append(f"  - `{k}: {v}`")
        lines.append("```")
        lines.append((a["response"].get("body") or "")[:1200])
        lines.append("```")
        lines.append("")
    else:
        # Aucune acceptation : on documente quand meme la requete de reference
        # et la reponse, pour analyser le refus.
        r0 = records[0]
        lines.append("## Aucune acceptation — requete de reference (refusee)")
        lines.append("")
        lines.append(f"- moment : `{r0['ts_utc']}`")
        lines.append(f"- egress : `{r0['egress_ip']}` (station {r0['station']})")
        lines.append(f"- modele : `{r0['model']}`")
        lines.append(f"- endpoint : `{r0['endpoint']}`")
        lines.append(f"- identite : `{json.dumps(r0['identity'], ensure_ascii=False)}`")
        lines.append("")
        lines.append("### En-tetes envoyes (ordre exact)")
        for k, v in r0["request"]["headers_order"]:
            shown = v if str(k).lower() != "authorization" else "Bearer public"
            lines.append(f"1. `{k}: {shown}`")
        lines.append("")
        lines.append(f"- corps : {r0['request']['body_len']} octets, sha256 `{r0['request']['body_sha256']}`")
        lines.append(f"- TLS : impersonate={r0['request']['tls']['impersonate']!r} fp={r0['request']['tls']['fp_keys']}")
        lines.append("")
        lines.append("### Reponse du provider")
        lines.append(f"- statut : `{(r0.get('response') or {}).get('status')}`")
        for k, v in (r0.get("response") or {}).get("headers") or []:
            lines.append(f"  - `{k}: {v}`")
        lines.append("```")
        lines.append(((r0.get("response") or {}).get("body") or "")[:1200])
        lines.append("```")
        lines.append("")
        if len(records) > 1:
            lines.append("### Variabilite observee sur les tentatives")
            statuses = {}
            for rec in records:
                s = (rec.get("response") or {}).get("status")
                statuses[s] = statuses.get(s, 0) + 1
            lines.append(f"- statuts : {statuses}")
            ips = sorted({rec["egress_ip"] for rec in records})
            lines.append(f"- IPs d'egress testees : {len(ips)} -> {ips[:8]}")
            lines.append(
                f"- corps differents entre tentatives : "
                f"{'oui' if len({rec['request']['body_sha256'] for rec in records}) > 1 else 'non'}"
            )
            lines.append("")

    if accepted and refused:
        a0, r0 = accepted[0], refused[0]
        lines.append("## Comparaison ACCEPTEE vs REFUSEE")
        lines.append(f"- acceptee : `{a0['ts_utc']}` egress `{a0['egress_ip']}` modele `{a0['model']}`")
        lines.append(f"- refusee  : `{r0['ts_utc']}` egress `{r0['egress_ip']}` modele `{r0['model']}`")
        lines.append("")
        acc = dict(a0["request"]["headers_order"])
        ref = dict(r0["request"]["headers_order"])
        for k in sorted(set(acc) | set(ref)):
            if acc.get(k) != ref.get(k):
                lines.append(f"- `{k}` : accepte=`{str(acc.get(k))[:70]}` refuse=`{str(ref.get(k))[:70]}`")
        d_acc = a0["request"]["body_sha256"]
        d_ref = r0["request"]["body_sha256"]
        lines.append(f"- corps identique : {'oui' if d_acc == d_ref else 'non'}")
        lines.append("")

    if measurements:
        lines.append("## Quota observe")
        for m in measurements:
            lines.append(
                f"- ip `{m['ip']}` modele `{m['model']}` : "
                f"{m['counts']['200']} succes avant le premier 429 "
                f"(position {m['counts']['first_429_at']}), statuts={m['counts']['statuses'][:15]}"
            )
        lines.append("")

    with open(REPORT, "w", encoding="utf-8") as f:
        f.write("\n".join(lines) + "\n")
    log(f"rapport ecrit: {REPORT}")


async def run_capture(duration_s: float, dry_run: bool, until_reset: bool) -> None:
    import opencode as oc

    os.makedirs(OUT_DIR, exist_ok=True)
    if until_reset:
        target = next_utc_midnight()
        wait = (target - dt.datetime.now(dt.timezone.utc)).total_seconds()
        # demarre 20 s avant le reset pour avoir un refus de reference
        wait = max(0.0, wait - 20.0)
        log(f"reset UTC prevu a {target.isoformat()} — attente {wait / 60:.1f} min")
        await asyncio.sleep(wait)

    deadline = time.monotonic() + duration_s
    records: list[dict] = []
    measurements: list[dict] = []
    winner: tuple[int, str, str] | None = None

    while time.monotonic() < deadline:
        targets = await pick_targets(oc)
        if not targets:
            log("aucune station servable — attente 10 s")
            await asyncio.sleep(10)
            continue
        for station, ip in targets:
            if time.monotonic() >= deadline:
                break
            for model in MODELS:
                if time.monotonic() >= deadline:
                    break
                rec = await probe(oc, station, ip, model, fresh=False, label="capture")
                records.append(rec)
                if not dry_run:
                    _write_line(rec)
                if (rec.get("response") or {}).get("status") == 200 and winner is None:
                    winner = (station, ip, model)
                    log(f"ACCEPTATION DETECTEE: station={station} ip={ip} model={model}")
                    # mesure du quota sur cette IP
                    counts = await measure_quota(oc, station, ip, model)
                    measurements.append({"ip": ip, "model": model, "counts": counts})
                    deadline = min(deadline, time.monotonic() + 30)
                    break
            if winner:
                break
        if winner:
            break
        await asyncio.sleep(2)

    if not dry_run and records:
        write_report(records, measurements)
    log(f"fin — {len(records)} tentatives, acceptations={sum(1 for r in records if (r.get('response') or {}).get('status') == 200)}")


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--until-reset", action="store_true", help="attendre le prochain 00:00 UTC")
    ap.add_argument("--now", type=float, default=0.0, metavar="SECONDES", help="capturer maintenant pendant N secondes")
    ap.add_argument("--dry-run", action="store_true", help="ne rien ecrire sur disque")
    args = ap.parse_args()

    duration = args.now if args.now > 0 else 900.0
    asyncio.run(run_capture(duration, args.dry_run, args.until_reset))


if __name__ == "__main__":
    main()
