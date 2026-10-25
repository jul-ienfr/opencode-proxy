#!/usr/bin/env python3
"""Capture prioritaire du retour de quota opencode.

Objectif : detecter AVANT les autres consommateurs le premier instant ou le
provider re-accepte, capturer la requete gagnante en integralite, puis
mesurer le quota accorde (combien de 200 avant le premier 429).

Modes :
  --watch [--max-seconds N]   veille continue (recommande), concurrence moderee
  --until-reset               attend le prochain 00:00 UTC puis veille
  --now SECONDE              capture limitee dans le temps (validation)
  --dry-run                   n'ecrit pas sur disque

Sorties : logs/quota_reset_capture/
  accepted/accepted_<ts>.json   la requete gagnante, complete (fsync immediat)
  attempts.log                  journal compact des refus (bufferise)
  evidence.jsonl                historique complet (uniquement si --full)
  rapport.md                    synthese lisible
  watch.log                     journal des executions planifiees

Budget de sondage : concurrency 3, intervalle adaptatif 0.5-3 s. Assez rapide
pour gagner la course, assez sobre pour ne pas consommer soi-meme le quota
qu'on veut mesurer.
"""
from __future__ import annotations

import argparse
import asyncio
import datetime as dt
import hashlib
import json
import os
import sys
import time

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)

OUT_DIR = os.path.join(ROOT, "logs", "quota_reset_capture")
ACCEPTED_DIR = os.path.join(OUT_DIR, "accepted")
ATTEMPTS = os.path.join(OUT_DIR, "attempts.log")
EVIDENCE = os.path.join(OUT_DIR, "evidence.jsonl")
REPORT = os.path.join(OUT_DIR, "rapport.md")
LOCK = os.path.join(OUT_DIR, ".watch.lock")

MODELS = [
    "deepseek-v4.1-flash-free",
    "mimo-v2.6-flash-free",
    "glm-5.3-flash-free",
    "big-pickle",
]

STATION_PORTS = {i: 1080 + (i - 1) for i in range(1, 7)}
CONCURRENCY = 3
SEM = asyncio.Semaphore(CONCURRENCY)
# Paliers de ralentissement quand le refus persiste : 2 s -> 30 s.
# 2 s = detection quasi immediate ; 30 s = veille de fond sobre.
_BACKOFF = (2.0, 3.0, 5.0, 8.0, 12.0, 18.0, 25.0, 30.0)


def log(msg: str) -> None:
    ts = dt.datetime.now(dt.timezone.utc).strftime("%H:%M:%S")
    print(f"[{ts}Z] {msg}", flush=True)


def next_utc_midnight(now: dt.datetime | None = None) -> dt.datetime:
    now = now or dt.datetime.now(dt.timezone.utc)
    d = (now + dt.timedelta(days=1)).date()
    return dt.datetime(d.year, d.month, d.day, tzinfo=dt.timezone.utc)


def _ensure_dirs() -> None:
    os.makedirs(ACCEPTED_DIR, exist_ok=True)


def _append_attempt(line: str) -> None:
    _ensure_dirs()
    try:
        with open(ATTEMPTS, "a", encoding="utf-8") as f:
            f.write(line + "\n")
    except Exception:
        pass


def _rotate_attempts(max_bytes: int = 2_000_000) -> None:
    """Borne le journal de refus : 160k lignes/jour sinon."""
    try:
        if os.path.getsize(ATTEMPTS) <= max_bytes:
            return
    except Exception:
        return
    try:
        os.replace(ATTEMPTS, ATTEMPTS + ".1")
    except Exception:
        pass


def _save_accepted(record: dict) -> str:
    """Ecrit la requete gagnante en fsync immediat. Rend le chemin."""
    _ensure_dirs()
    ts = dt.datetime.now(dt.timezone.utc).strftime("%Y%m%dT%H%M%SZ")
    path = os.path.join(ACCEPTED_DIR, f"accepted_{ts}.json")
    with open(path, "w", encoding="utf-8") as f:
        json.dump(record, f, ensure_ascii=False, indent=2, default=str)
        f.flush()
        os.fsync(f.fileno())
    return path


def _acquire_lock() -> bool:
    """Empeche deux veilles simultanees (tache planifiee qui se chevauche)."""
    _ensure_dirs()
    try:
        if os.path.exists(LOCK):
            pid = (open(LOCK, encoding="ascii", errors="replace").read() or "").strip()
            if pid.isdigit():
                import subprocess

                r = subprocess.run(
                    ["tasklist", "/FI", f"PID eq {pid}"], capture_output=True, text=True, timeout=15
                )
                if pid in (r.stdout or ""):
                    log(f"veille deja active (pid {pid}) — sortie")
                    return False
        with open(LOCK, "w", encoding="ascii") as f:
            f.write(str(os.getpid()))
        return True
    except Exception:
        return True


def _release_lock() -> None:
    try:
        os.remove(LOCK)
    except Exception:
        pass


async def pick_targets(oc) -> list[tuple[int, str]]:
    import urllib.request

    try:
        with urllib.request.urlopen("http://127.0.0.1:4000/api/pool-status", timeout=8) as r:
            data = json.loads(r.read().decode())
    except Exception:
        return []
    out = []
    for s in data.get("stations", []):
        ip, status = s.get("current_ip"), s.get("vpn_status")
        if ip and status in ("connected", "degraded"):
            out.append((int(s.get("station")), str(ip)))
    return out


async def identity_for(oc, ip: str):
    vault = oc._ip_vault()
    if vault is None:
        return {}
    ident = vault.get(ip)
    return {
        "version": ident.version,
        "client": ident.client,
        "project": ident.project,
        "ses": ident.session_for("capture-conv"),
        "msg": ident.new_msg(),
        "generation": getattr(ident, "generation", 0),
    }


async def probe(oc, station: int, ip: str, model: str) -> dict:
    """Une tentative complete. Retourne le record (toujours, meme en erreur)."""
    async with SEM:
        port = STATION_PORTS.get(station, 1080)
        endpoint = oc._cfg_settings._free_endpoint_for(model) or oc.API_BASE_FREE
        ident = await identity_for(oc, ip)
        headers = oc._official_free_headers(endpoint, "capture-conv", ip)
        imp, fp = oc._get_free_fp_kwargs()

        if "/systemone" in endpoint:
            body = {"model": model, "state": "reply OK", "questions": {"ok": {"type": "score"}}}
            raw = body
        else:
            raw = {
                "model": model,
                "messages": [{"role": "user", "content": "Dis OK"}],
                "max_tokens": 16,
            }
            body, _ = oc._free_wire_body(raw, force_stream=True, egress_ip=ip)

        body_bytes = oc._serialize_json_body(body)
        rec = {
            "ts_utc": dt.datetime.now(dt.timezone.utc).isoformat(),
            "station": station,
            "egress_ip": ip,
            "model": model,
            "endpoint": endpoint,
            "identity": ident,
            "request": {
                "method": "POST",
                "url": endpoint,
                "headers_order": list(headers.items()),
                "body_sha256": hashlib.sha256(body_bytes).hexdigest(),
                "body_len": len(body_bytes),
                "body": body_bytes.decode("utf-8", "replace"),
                "tls": {"impersonate": imp, "fp_keys": sorted((fp or {}).keys())},
            },
        }

        pool = slot = None
        t0 = time.monotonic()
        try:
            pool, slot = await oc._get_pooled_curl_session(
                f"socks5://127.0.0.1:{port}", imp, fp, egress_ip=ip
            )
            resp = await slot.sess.post(
                endpoint, content=body_bytes, headers=headers, timeout=(8, 45), stream=True
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
            rec["response"] = {
                "status": resp.status_code,
                "headers": list(dict(resp.headers).items()),
                "elapsed_ms": elapsed,
                "body": "\n".join(chunks)[:4000],
            }
        except Exception as e:
            rec["response"] = {
                "status": None,
                "error": f"{type(e).__name__}: {e}",
                "elapsed_ms": round((time.monotonic() - t0) * 1000),
            }
        finally:
            if pool is not None and slot is not None:
                try:
                    await pool.checkin(slot)
                except Exception:
                    pass
        return rec


async def measure_quota(oc, station: int, ip: str, model: str, max_probes: int = 120) -> dict:
    """Compte les 200 successifs sur CETTE IP avant le premier 429."""
    counts = {"200": 0, "429": 0, "other": 0, "first_429_at": None, "statuses": [], "retry_after": None}
    for i in range(max_probes):
        rec = await probe(oc, station, ip, model)
        resp = rec.get("response") or {}
        st = resp.get("status")
        counts["statuses"].append(st)
        if st == 200:
            counts["200"] += 1
        elif st == 429:
            counts["429"] += 1
            if counts["first_429_at"] is None:
                counts["first_429_at"] = i + 1
                for k, v in resp.get("headers") or []:
                    if str(k).lower() == "retry-after":
                        counts["retry_after"] = v
            break
        else:
            counts["other"] += 1
            break
        await asyncio.sleep(1.0)
    return counts


def write_report(accepted: list[dict], refusals: int, measurements: list[dict]) -> None:
    _ensure_dirs()
    lines = ["# Capture quota opencode", ""]
    lines.append(f"Mis a jour : {dt.datetime.now(dt.timezone.utc).isoformat()}")
    lines.append(f"- refus observes (session courante) : {refusals}")
    lines.append(f"- acceptations capturees : {len(accepted)}")
    lines.append("")
    if accepted:
        a = accepted[-1]
        lines += [
            "## Requete ACCEPTEE capturee",
            "",
            f"- moment : `{a['ts_utc']}`",
            f"- egress : `{a['egress_ip']}` (station {a['station']})",
            f"- modele : `{a['model']}`",
            f"- endpoint : `{a['endpoint']}`",
            f"- identite : `{json.dumps(a['identity'], ensure_ascii=False)}`",
            "",
            "### En-tetes (ordre exact)",
        ]
        for k, v in a["request"]["headers_order"]:
            shown = "Bearer public" if str(k).lower() == "authorization" else v
            lines.append(f"1. `{k}: {shown}`")
        lines += [
            "",
            f"- corps : {a['request']['body_len']} o, sha256 `{a['request']['body_sha256']}`",
            f"- TLS : impersonate={a['request']['tls']['impersonate']!r} fp={a['request']['tls']['fp_keys']}",
            "",
            "### Reponse",
            f"- statut : `{a['response']['status']}` en {a['response']['elapsed_ms']} ms",
        ]
        for k, v in a["response"].get("headers") or []:
            lines.append(f"  - `{k}: {v}`")
        lines += ["```", (a["response"].get("body") or "")[:1500], "```", ""]
    else:
        lines += ["Aucune acceptation pour l'instant — le chat gratuit reste refuse.", ""]

    if measurements:
        lines.append("## Quota observe")
        for m in measurements:
            c = m["counts"]
            lines.append(
                f"- ip `{m['ip']}` modele `{m['model']}` : {c['200']} succes avant 429 "
                f"(position {c['first_429_at']}, retry-after={c['retry_after']})"
            )
        lines.append("")

    with open(REPORT, "w", encoding="utf-8") as f:
        f.write("\n".join(lines) + "\n")


async def watch(max_seconds: float, dry_run: bool, full: bool, until_reset: bool) -> None:
    import opencode as oc

    _ensure_dirs()
    if until_reset:
        target = next_utc_midnight()
        wait = max(0.0, (target - dt.datetime.now(dt.timezone.utc)).total_seconds() - 20.0)
        log(f"reset annonce {target.isoformat()} — attente {wait / 60:.1f} min")
        await asyncio.sleep(wait)

    deadline = time.monotonic() + max_seconds if max_seconds > 0 else float("inf")
    accepted: list[dict] = []
    measurements: list[dict] = []
    refusals = 0
    last_summary = 0.0
    found_this_pass = False
    last_no_target_log = 0.0

    while time.monotonic() < deadline:
        targets = await pick_targets(oc)
        if not targets:
            now_nt = time.monotonic()
            if now_nt - last_no_target_log >= 60.0:
                log("aucune station servable (pool en erreur/cooling) — veille passive")
                last_no_target_log = now_nt
            await asyncio.sleep(3)
            continue

        pairs = [(st, ip, m) for st, ip in targets for m in MODELS]
        tasks = [asyncio.create_task(probe(oc, st, ip, m)) for st, ip, m in pairs]
        results = await asyncio.gather(*tasks, return_exceptions=True)

        hits = []
        for rec in results:
            if not isinstance(rec, dict):
                continue
            resp = rec.get("response") or {}
            st = resp.get("status")
            if st == 200:
                hits.append(rec)
            elif st == 429:
                refusals += 1
                if full and not dry_run:
                    try:
                        with open(EVIDENCE, "a", encoding="utf-8") as f:
                            f.write(json.dumps(rec, ensure_ascii=False, default=str) + "\n")
                    except Exception:
                        pass
                ra = next(
                    (v for k, v in resp.get("headers") or [] if str(k).lower() == "retry-after"),
                    None,
                )
                _append_attempt(
                    f"{rec['ts_utc']} {st} {rec['egress_ip']} {rec['model']} ra={ra}"
                )

        if hits:
            for rec in hits:
                path = "(dry-run)"
                if not dry_run:
                    path = _save_accepted(rec)
                log(f"ACCEPTATION CAPTUREE -> {path}")
                accepted.append(rec)
                # mesure immediate du quota sur cette IP avant les autres
                counts = await measure_quota(
                    oc, rec["station"], rec["egress_ip"], rec["model"]
                )
                measurements.append(
                    {"ip": rec["egress_ip"], "model": rec["model"], "counts": counts}
                )
                log(f"quota mesure ip={rec['egress_ip']} : {counts['200']} succes avant 429")
                found_this_pass = True
            if not dry_run:
                write_report(accepted, refusals, measurements)
            # apres capture, on ralentit pour ne pas gaspiller
            await asyncio.sleep(5.0)
        else:
            now = time.monotonic()
            if now - last_summary >= 60.0:
                log(f"veille — {refusals} refus cumules, pas d'acceptation")
                last_summary = now
                _rotate_attempts()
            # [sobriete 2026-10-25] 160k refus en 17 h = martelage inutile qui
            # peut aggraver le blocage cote fournisseur. On ralentit par palier
            # tant que rien ne change, et on repart vite des qu'une acceptation
            # apparait.
            if found_this_pass:
                delay = 0.5
            else:
                idx = min(len(_BACKOFF), refusals // 60)
                delay = _BACKOFF[idx]
            await asyncio.sleep(delay)

    if not dry_run and (accepted or refusals):
        write_report(accepted, refusals, measurements)
    log(f"fin veille — refus={refusals} acceptations={len(accepted)}")


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--watch", action="store_true")
    ap.add_argument("--until-reset", action="store_true")
    ap.add_argument("--now", type=float, default=0.0, metavar="SECONDES")
    ap.add_argument("--max-seconds", type=float, default=0.0)
    ap.add_argument("--dry-run", action="store_true")
    ap.add_argument("--full", action="store_true", help="ecrire aussi evidence.jsonl")
    args = ap.parse_args()

    if not _acquire_lock():
        return
    try:
        if args.now > 0:
            asyncio.run(watch(args.now, args.dry_run, args.full, False))
        else:
            asyncio.run(
                watch(args.max_seconds, args.dry_run, args.full, args.until_reset)
            )
    finally:
        _release_lock()


if __name__ == "__main__":
    main()
