#!/usr/bin/env python3
"""Rejoue une requete capturee, OCTET POUR OCTET.

Une capture n'a de valeur que si elle est rejouable. Ce script reprend un
enregistrement complet (evidence.jsonl ou accepted/*.json) et renvoie
exactement la meme chose que la capture :

  - memes en-tetes, dans le meme ordre (Authorization, UA, x-opencode-*) ;
  - meme corps, octet pour octet (sha256 verifie AVANT l'envoi) ;
  - meme empreinte TLS (impersonate / JA3 Bun) ;
  - meme IP de sortie : la station dont l'IP courante egale l'IP capturee.

Puis il compare la reponse obtenue a la reponse enregistree (statut,
retry-after, cf-ray, corps) et ecrit le resultat dans replay/.

Usage :
  python scripts/quota_replay.py --list
  python scripts/quota_replay.py --record evidence.jsonl --index 0
  python scripts/quota_replay.py --record accepted/accepted_X.json
  options :
    --any-egress    rejouer malgre tout si l'IP capturee n'est plus dispo
    --repeat N      rejouer N fois (mesure de stabilite)
    --dry-run       reconstruire et verifier, sans envoyer
"""
from __future__ import annotations

import argparse
import asyncio
import datetime as dt
import hashlib
import json
import os
import sys

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)

OUT_DIR = os.path.join(ROOT, "logs", "quota_reset_capture")
REPLAY_DIR = os.path.join(OUT_DIR, "replay")
STATION_PORTS = {i: 1080 + (i - 1) for i in range(1, 7)}


def log(msg: str) -> None:
    ts = dt.datetime.now(dt.timezone.utc).strftime("%H:%M:%S")
    print(f"[{ts}Z] {msg}", flush=True)


# ---------------------------------------------------------------- chargement


def _load_jsonl(path: str) -> list[dict]:
    out = []
    with open(path, encoding="utf-8", errors="replace") as f:
        for line in f:
            line = line.strip()
            if not line:
                continue
            try:
                out.append(json.loads(line))
            except Exception:
                continue
    return out


def load_records(record: str) -> list[dict]:
    record = os.path.expanduser(record)
    if not os.path.exists(record):
        # accepte un nom court : evidence.jsonl, accepted/xxx.json
        alt = os.path.join(OUT_DIR, record)
        if os.path.exists(alt):
            record = alt
        else:
            raise SystemExit(f"introuvable: {record}")
    if record.endswith(".jsonl"):
        return _load_jsonl(record)
    with open(record, encoding="utf-8", errors="replace") as f:
        return [json.load(f)]


def list_records() -> None:
    sources = []
    ev = os.path.join(OUT_DIR, "evidence.jsonl")
    if os.path.exists(ev):
        sources.append(ev)
    acc = os.path.join(OUT_DIR, "accepted")
    if os.path.isdir(acc):
        sources += [os.path.join(acc, n) for n in sorted(os.listdir(acc)) if n.endswith(".json")]

    total = 0
    for src in sources:
        recs = load_records(src)
        print(f"\n{os.path.relpath(src, ROOT)}  ({len(recs)} enregistrement(s))")
        for i, r in enumerate(recs):
            resp = r.get("response") or {}
            st = resp.get("status")
            ra = next(
                (v for k, v in resp.get("headers") or [] if str(k).lower() == "retry-after"),
                None,
            )
            body_ok = "corps" if (r.get("request") or {}).get("body") else "SANS CORPS"
            print(
                f"  [{i:3}] {r.get('ts_utc')}  {st}  {r.get('model'):30} "
                f"ip={r.get('egress_ip'):16} st={r.get('station')} ra={ra} {body_ok}"
            )
            total += 1
    print(f"\ntotal rejouable : {total}")


# ---------------------------------------------------------------- reconstruction


def rebuild(rec: dict) -> tuple[str, dict, bytes, str, str]:
    """Rend (endpoint, headers, body_bytes, impersonate, ip) exactement comme capture."""
    req = rec.get("request") or {}
    endpoint = req.get("url") or rec.get("endpoint")
    body = req.get("body")
    if body is None:
        raise SystemExit("cet enregistrement n'a pas de corps : non rejouable")
    body_bytes = body.encode("utf-8")
    sha = hashlib.sha256(body_bytes).hexdigest()
    stored = req.get("body_sha256")
    if stored and sha != stored:
        raise SystemExit(f"corps altere : sha256 {sha} != {stored} — refus de rejouer")
    headers = [(k, v) for k, v in (req.get("headers_order") or [])]
    if not headers:
        raise SystemExit("cet enregistrement n'a pas d'en-tetes : non rejouable")
    imp = ((req.get("tls") or {}).get("impersonate") or "") or None
    ip = rec.get("egress_ip") or ""
    return endpoint, dict(headers), body_bytes, imp, ip


async def station_for_ip(oc, ip: str) -> tuple[int, str] | None:
    """Station dont l'IP courante egale l'IP capturee (= meme sortie)."""
    import urllib.request

    try:
        with urllib.request.urlopen("http://127.0.0.1:4000/api/pool-status", timeout=8) as r:
            data = json.loads(r.read().decode())
    except Exception as e:
        log(f"pool-status indisponible: {e}")
        return None
    for s in data.get("stations", []):
        if str(s.get("current_ip") or "") == ip and s.get("vpn_status") in (
            "connected",
            "degraded",
        ):
            return int(s.get("station")), ip
    return None


# ---------------------------------------------------------------- envoi


async def send_once(oc, rec: dict, ip: str, station: int) -> dict:
    endpoint, headers, body_bytes, imp, _ = rebuild(rec)
    port = STATION_PORTS.get(station, 1080)
    _, default_fp = oc._get_free_fp_kwargs()
    # empreinte TLS : celle de la capture si elle a ete stockee, sinon defaut
    stored_fp = (rec.get("request") or {}).get("tls", {}).get("fp")
    fp = stored_fp if isinstance(stored_fp, dict) and stored_fp else default_fp
    pool = slot = None
    t0 = 0.0
    try:
        pool, slot = await oc._get_pooled_curl_session(
            f"socks5://127.0.0.1:{port}", imp, fp, egress_ip=ip
        )
        t0 = __import__("time").monotonic()
        resp = await slot.sess.post(
            endpoint, content=body_bytes, headers=headers, timeout=(10, 60), stream=True
        )
        elapsed = round((__import__("time").monotonic() - t0) * 1000)
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
        return {
            "status": resp.status_code,
            "headers": list(dict(resp.headers).items()),
            "elapsed_ms": elapsed,
            "body": "\n".join(chunks)[:4000],
        }
    except Exception as e:
        return {"status": None, "error": f"{type(e).__name__}: {e}"}
    finally:
        if pool is not None and slot is not None:
            try:
                await pool.checkin(slot)
            except Exception:
                pass


def diff(rec: dict, new: dict) -> list[str]:
    old = rec.get("response") or {}
    out = []
    out.append(f"statut  : capture={old.get('status')}  rejoue={new.get('status')}")
    if old.get("status") != new.get("status"):
        out.append("  -> STATUT DIFFERENT")
    for name in ("retry-after", "cf-ray", "x-opencode-log-id", "date"):
        ov = next((v for k, v in old.get("headers") or [] if str(k).lower() == name), None)
        nv = next((v for k, v in new.get("headers") or [] if str(k).lower() == name), None)
        if ov is not None or nv is not None:
            flag = "" if ov == nv else "  -> DIFFERENT"
            out.append(f"{name:18}: capture={ov}  rejoue={nv}{flag}")
    ob = (old.get("body") or "").strip()
    nb = (new.get("body") or "").strip()
    out.append(f"corps   : identique={'oui' if ob == nb else 'non'} (capture {len(ob)} o, rejoue {len(nb)} o)")
    return out


async def replay(record: str, index: int, any_egress: bool, repeat: int, dry_run: bool) -> None:
    import opencode as oc

    recs = load_records(record)
    if not recs:
        raise SystemExit("aucun enregistrement")
    if index >= len(recs):
        raise SystemExit(f"index {index} hors bornes (0..{len(recs) - 1})")
    rec = recs[index]

    endpoint, headers, body_bytes, imp, ip = rebuild(rec)
    log(f"rejoue : {rec.get('ts_utc')} {rec.get('model')} ip={ip} body={len(body_bytes)} o")
    log(f"en-tetes reconstruits : {len(headers)} (ordre preserve), TLS impersonate={imp!r}")
    log(f"prochaine etape : verification IP -> station")

    target = await station_for_ip(oc, ip)
    if target is None:
        log(f"IP {ip} absente du pool (rotation depuis la capture)")
        if not any_egress:
            raise SystemExit("--any-egress pour rejouer sur une autre sortie (ce n'est plus exact)")
        import urllib.request

        with urllib.request.urlopen("http://127.0.0.1:4000/api/pool-status", timeout=8) as r:
            data = json.loads(r.read().decode())
        stations = [
            (int(s["station"]), str(s["current_ip"]))
            for s in data.get("stations", [])
            if s.get("current_ip") and s.get("vpn_status") in ("connected", "degraded")
        ]
        if not stations:
            raise SystemExit("aucune station servable")
        target = stations[0]
        log(f"sortie substituee : station {target[0]} ip {target[1]}")

    station, egress = target
    log(f"sortie : station {station} ip {egress} (port {STATION_PORTS.get(station, 1080)})")

    if dry_run:
        log("dry-run : rien envoye")
        return

    os.makedirs(REPLAY_DIR, exist_ok=True)
    for i in range(repeat):
        new = await send_once(oc, rec, egress, station)
        log(f"rejoue #{i + 1} -> {new.get('status')}")
        for line in diff(rec, new):
            log("  " + line)
        payload = {
            "replayed_at": dt.datetime.now(dt.timezone.utc).isoformat(),
            "source": os.path.abspath(record),
            "index": index,
            "exact_egress": egress == ip,
            "recorded": rec,
            "response": new,
            "diff": diff(rec, new),
        }
        ts = dt.datetime.now(dt.timezone.utc).strftime("%Y%m%dT%H%M%SZ")
        path = os.path.join(REPLAY_DIR, f"replay_{ts}_{i + 1}.json")
        with open(path, "w", encoding="utf-8") as f:
            json.dump(payload, f, ensure_ascii=False, indent=2, default=str)
            f.flush()
            os.fsync(f.fileno())
        log(f"  enregistre: {os.path.relpath(path, ROOT)}")
        await asyncio.sleep(1.0)


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--record", help="evidence.jsonl, accepted/*.json ou chemin complet")
    ap.add_argument("--index", type=int, default=0)
    ap.add_argument("--any-egress", action="store_true")
    ap.add_argument("--repeat", type=int, default=1)
    ap.add_argument("--dry-run", action="store_true")
    ap.add_argument("--list", action="store_true")
    args = ap.parse_args()

    if args.list or not args.record:
        list_records()
        return
    asyncio.run(replay(args.record, args.index, args.any_egress, args.repeat, args.dry_run))


if __name__ == "__main__":
    main()
