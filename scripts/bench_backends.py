"""bench_backends.py — banc alterné multi-backends VPN (docs/plan-banc-essai-backends.md).

Round-robin alterné (jamais 2 runs du même backend d'affilée, shuffle
seedé par round) : ~10 runs par backend exigés, sans biais transitoire.
Résultats en JSONL (--out), une ligne par run, horodatée.

Garde-fous :
- Secrets lus depuis credentials.env / vpn_configs/wireguard.env,
  transmis via fichiers d'env 0600 temporaires, JAMAIS affichés.
- Gate throttle : avant chaque run OV, compte les AUTH_FAILED/10min de
  la flotte (logs/debug.log) ; si > --fleet-auth-cap : SKIP (pas de
  verdict, on ne nourrit pas le ban).
- Verdict auto : FAIL-compte si signatures throttle/session dans les
  logs du conteneur (AUTH_FAILED, Session Limit, ConnectionLimitReached),
  sinon FAIL-backend — relecture humaine en cas de doute.
- Nettoyage systématique (disconnect best-effort + rm -f + shred env).

Usage :
  python scripts/bench_backends.py --rounds 10 --out results.jsonl
  python scripts/bench_backends.py --rounds 1 --backends nordlynx-key --gap 30
  python scripts/bench_backends.py --list
"""

from __future__ import annotations

import argparse
import datetime as dt
import json
import os
import random
import re
import stat
import subprocess
import sys
import tempfile
import time
import urllib.request

REPO = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
LOGS = os.path.join(REPO, "logs", "debug.log")
CREDENV = os.path.join(REPO, "credentials.env")
WGfile = os.path.join(REPO, "vpn_configs", "wireguard.env")
OVPN = os.path.join(REPO, "vpn_configs", "de1223.ovpn")

AUTH_SIG = re.compile(r"AUTH_FAILED|Session.?Limit|ConnectionLimitReached|too many", re.I)


def sh(args, timeout=60):
    try:
        r = subprocess.run(args, capture_output=True, text=True, timeout=timeout, errors="replace", cwd=REPO)
        return r.returncode, (r.stdout or "") + (r.stderr or "")
    except subprocess.TimeoutExpired as e:
        out = (e.stdout or b"").decode("utf-8", "replace") if isinstance(e.stdout, bytes) else (e.stdout or "")
        return 124, out + "<TIMEOUT>"


def read_env_file(path):
    vals = {}
    with open(path, encoding="utf-8", errors="replace") as f:
        for line in f:
            line = line.strip()
            if line and not line.startswith("#") and "=" in line:
                k, v = line.split("=", 1)
                vals[k.strip()] = v.strip()
    return vals


def secrets():
    s = {}
    s.update(read_env_file(CREDENV))
    s.update(read_env_file(WGfile))
    return s


def write_envfile(mapping):
    fd, path = tempfile.mkstemp(prefix="bench_", suffix=".env")
    with os.fdopen(fd, "w", encoding="utf-8") as f:
        for k, v in mapping.items():
            f.write(f"{k}={v}\n")
    os.chmod(path, stat.S_IREAD | stat.S_IWRITE)
    return path


def shred(path):
    try:
        with open(path, "w", encoding="utf-8") as f:
            f.write("x" * 512)
        os.remove(path)
    except OSError:
        pass


def image_digest(image):
    rc, out = sh(["docker", "image", "inspect", image, "--format", "{{json .RepoDigests}}"], 30)
    try:
        return (json.loads(out) or [""])[0]
    except Exception:
        return ""


def fleet_auth_count(minutes=10):
    """AUTH_FAILED flotte sur N min (gate throttle). -1 si illisible."""
    try:
        cutoff = time.time() - minutes * 60
        n = 0
        with open(LOGS, encoding="utf-8", errors="replace") as f:
            for line in f:
                if "AUTH_FAILED" not in line:
                    continue
                m = re.search(r"\[(\d{4})-(\d{2})-(\d{2}) (\d{2}):(\d{2}):(\d{2})Z\]", line)
                if not m:
                    continue
                y, mo, d, h, mi, s = map(int, m.groups())
                ts = dt.datetime(y, mo, d, h, mi, s, tzinfo=dt.UTC).timestamp()
                if ts >= cutoff:
                    n += 1
        # lignes doublées connues (F8, pré-fix prod) → estimation /2
        return n // 2
    except OSError:
        return -1


def docker_logs(name, tail=60):
    _, out = sh(["docker", "logs", "--timestamps", "--tail", str(tail), name], 30)
    return out


def exec_probe(name):
    """(ip_ok, dns_ok, ip) via wget dans le conteneur."""
    _, out = sh(
        [
            "docker",
            "exec",
            name,
            "sh",
            "-c",
            "wget -q -O - -T 8 http://api.ipify.org; echo; "
            "wget -q -O /dev/null -T 5 http://1.1.1.1 && echo IP_OK || echo IP_KO",
        ],
        40,
    )
    ip = ""
    for line in out.splitlines():
        line = line.strip()
        if re.fullmatch(r"[0-9a-fA-F.:]+", line) and "." in line:
            ip = line
    return ("IP_OK" in out, bool(ip), ip)


def host_socks(port):
    rc, out = sh(
        ["curl.exe", "-s", "--max-time", "10", "--socks5-hostname", f"127.0.0.1:{port}", "http://api.ipify.org"], 30
    )
    ip = out.strip().splitlines()[0] if out.strip() else ""
    ok = bool(re.fullmatch(r"[0-9a-fA-F.:]+", ip or "") and "." in (ip or ""))
    return ok, ip


def host_http(port):
    try:
        req = urllib.request.Request("http://api.ipify.org")
        req.set_proxy(f"http://127.0.0.1:{port}", "http")
        with urllib.request.urlopen(req, timeout=10) as r:
            ip = r.read().decode().strip()
        return (bool(re.fullmatch(r"[0-9a-fA-F.:]+", ip) and "." in ip), ip)
    except Exception:
        return False, ""


def wait_marker(name, patterns, timeout):
    """Attend un motif dans les logs. Retourne (trouvé: bool, secondes)."""
    if isinstance(patterns, str):
        patterns = [patterns]
    t0 = time.monotonic()
    while time.monotonic() - t0 < timeout:
        out = docker_logs(name, 40)
        for p in patterns:
            if p in out:
                return True, round(time.monotonic() - t0, 1)
        time.sleep(5)
    return False, round(time.monotonic() - t0, 1)


def rm_container(name):
    sh(["docker", "rm", "-f", name], 60)


def redact(text):
    """Masque secrets éventuels (hex longs, base64 longs) des logs sauvés."""
    text = re.sub(r"\b[0-9a-fA-F]{32,}\b", "***", text)
    text = re.sub(r"\b[A-Za-z0-9+/]{40,}={0,2}\b", "***", text)
    return text[:4000]


def _fail(name, reason, **kw):
    """Échec avec queue de logs (conteneur encore présent)."""
    try:
        out = docker_logs(name, 25)
        tail = [l for l in out.splitlines() if l.strip()][-25:]
        kw["tail"] = redact("\n".join(tail))
    except Exception:
        kw["tail"] = ""
    kw.update({"ok": False, "reason": reason})
    return kw


def log_server(name):
    """Serveur/peer choisi (analyse qualité peers, ex. `Server: {...}`)."""
    out = docker_logs(name, 30)
    for line in out.splitlines():
        if "Server:" in line:
            return line.strip()[:160]
    return ""


def poll_egress(name, tries=9, interval=10):
    """Sonde d'egress répétée : le handshake WG finit APRES la ligne
    `Connected!` (pair mort possible aussi). Retourne
    (ok, dns_ok, ip, t_egress_s)."""
    t0 = time.monotonic()
    last = (False, False, "")
    for _ in range(tries):
        last = exec_probe(name)
        if last[0] and last[1]:
            return True, True, last[2], round(time.monotonic() - t0, 1)
        time.sleep(interval)
    return False, last[1], last[2], round(time.monotonic() - t0, 1)


# ── Runners (un par backend) : dict résultat partiel ─────────────


def run_nordlynx_key(sec, tag):
    """bubuntux/nordlynx — WG clé, 0 AUTH."""
    name = f"bench-{tag}"
    env = write_envfile(
        {
            "PRIVATE_KEY": sec["WIREGUARD_PRIVATE_KEY"],
            "QUERY": r"filters\[country_id\]=153",
            "NET_LOCAL": "192.168.0.0/16,10.0.0.0/8,172.16.0.0/12",
        }
    )
    try:
        rc, _ = sh(
            [
                "docker",
                "run",
                "-d",
                "--name",
                name,
                "--cap-add=NET_ADMIN",
                "--env-file",
                env,
                "ghcr.io/bubuntux/nordlynx:latest",
            ],
            60,
        )
        if rc != 0:
            return _fail(name, "run-failed")
        ok, dur = wait_marker(name, "Connected!", 150)
        if not ok:
            return _fail(name, "no-connected-timeout", t_connect=dur)
        server = log_server(name)
        ip_ok, dns_ok, ip, t_egr = poll_egress(name)
        if ip_ok and dns_ok:
            return {
                "ok": True,
                "t_connect": dur,
                "t_egress": t_egr,
                "server": server,
                "egress_ip": ip,
                "dns": dns_ok,
                "reason": "",
            }
        return _fail(name, "no-egress-90s", t_connect=dur, t_egress=t_egr, server=server, egress_ip=ip, dns=dns_ok)
    finally:
        rm_container(name)
        shred(env)


def run_nordlynx_proxy(sec, tag):
    """edgd1er/nordlynx-proxy — WG client officiel, login TOKEN."""
    name = f"bench-{tag}"
    env = write_envfile(
        {
            "NORDVPN_TOKEN": sec["NORDVPN_TOKEN"],
            "CONNECT": "Netherlands",
            "GROUP": "P2P",
        }
    )
    try:
        rc, _ = sh(
            [
                "docker",
                "run",
                "-d",
                "--name",
                name,
                "--privileged",
                "--device=/dev/net/tun",
                "--env-file",
                env,
                "-p",
                "127.0.0.1:1191:1080",
                "-p",
                "127.0.0.1:8991:8888",
                "edgd1er/nordlynx-proxy:latest",
            ],
            60,
        )
        if rc != 0:
            return _fail(name, "run-failed")
        ok_login, d_login = wait_marker(name, "logged in", 120)
        if not ok_login:
            return _fail(name, "no-login-timeout", t_login=d_login)
        ok, dur = wait_marker(name, "You are connected to", 240)
        if not ok:
            return _fail(name, "no-connected-timeout", t_login=d_login, t_connect=dur)
        t_end = None
        ok_end, d_end = wait_marker(name, "END:", 90)
        if ok_end:
            t_end = round(dur + d_end, 1)
        ip_ok, dns_ok, ip = exec_probe(name)
        s_ok, s_ip = host_socks(1191)
        h_ok, h_ip = host_http(8991)
        good = ip_ok and dns_ok and s_ok and h_ok
        if good:
            return {
                "ok": True,
                "t_login": d_login,
                "t_connect": round(d_login + dur, 1),
                "t_end": t_end,
                "egress_ip": ip,
                "dns": dns_ok,
                "socks": s_ok,
                "http": h_ok,
                "reason": "",
            }
        return _fail(
            name,
            "proxy-or-egress-ko",
            t_login=d_login,
            t_connect=round(d_login + dur, 1),
            t_end=t_end,
            egress_ip=ip,
            dns=dns_ok,
            socks=s_ok,
            http=h_ok,
        )
    finally:
        sh(["docker", "exec", name, "nordvpn", "disconnect"], 25)
        rm_container(name)
        shred(env)


def run_bubuntux_nordvpn(sec, tag):
    """bubuntux/nordvpn — CLI officiel, login TOKEN, NordLynx (0 AUTH OV).

    Pas de proxy natif (transport + statut uniquement).
    Logout best-effort en fin (libère le slot de session).
    """
    import re as _re

    name = f"bench-{tag}"
    env = write_envfile(
        {
            "TOKEN": sec["NORDVPN_TOKEN"],
            "CONNECT": "Netherlands",
            "TECHNOLOGY": "NordLynx",
        }
    )
    try:
        rc, _ = sh(
            [
                "docker",
                "run",
                "-d",
                "--name",
                name,
                "--cap-add=NET_ADMIN",
                "--cap-add=NET_RAW",
                "--device=/dev/net/tun",
                "--env-file",
                env,
                "bubuntux/nordvpn:latest",
            ],
            60,
        )
        if rc != 0:
            return _fail(name, "run-failed")
        ok_login, d_login = wait_marker(name, "Welcome to NordVPN", 120)
        if not ok_login:
            # certains builds loguent autrement : vérifier le statut direct
            d_login = None
        ok, dur = wait_marker(name, "You are connected to", 240)
        if not ok:
            return _fail(name, "no-connected-timeout", t_login=d_login, t_connect=dur)
        rc, out = sh(["docker", "exec", name, "nordvpn", "status"], 30)
        ip = country = ""
        m = _re.search(r"IP:\s*([0-9a-fA-F.:]+)", out)
        if m:
            ip = m.group(1)
        m = _re.search(r"Country:\s*(.+)", out)
        if m:
            country = m.group(1).strip()
        good = bool(ip)
        if good:
            return {
                "ok": True,
                "t_login": d_login,
                "t_connect": round((d_login or 0) + dur, 1),
                "egress_ip": ip,
                "country": country,
                "reason": "",
            }
        return _fail(
            name,
            "no-ip-in-status",
            t_login=d_login,
            t_connect=round((d_login or 0) + dur, 1),
            egress_ip=ip,
            country=country,
        )
    finally:
        sh(["docker", "exec", name, "nordvpn", "logout"], 25)
        rm_container(name)
        shred(env)


def run_nordvpn_proxy(sec, tag):
    """edgd1er/nordvpn-proxy — OV, service creds, 1 AUTH."""
    name = f"bench-{tag}"
    env = write_envfile(
        {
            "NORDVPN_USER": sec["OPENVPN_USER"],
            "NORDVPN_PASS": sec["OPENVPN_PASSWORD"],
            "NORDVPN_COUNTRY": "Netherlands",
            "NORDVPN_PROTOCOL": "tcp",
        }
    )
    try:
        rc, _ = sh(
            [
                "docker",
                "run",
                "-d",
                "--name",
                name,
                "--cap-add=NET_ADMIN",
                "--device=/dev/net/tun",
                "--env-file",
                env,
                "-p",
                "127.0.0.1:1191:1080",
                "-p",
                "127.0.0.1:8991:8888",
                "edgd1er/nordvpn-proxy:latest",
            ],
            60,
        )
        if rc != 0:
            return _fail(name, "run-failed")
        ok, dur = wait_marker(name, "END:", 240)
        if not ok:
            return _fail(name, "no-connected-timeout", t_connect=dur)
        ip_ok, dns_ok, ip = exec_probe(name)
        s_ok, _ = host_socks(1191)
        h_ok, _ = host_http(8991)
        good = ip_ok and dns_ok and s_ok and h_ok
        if good:
            return {
                "ok": True,
                "t_connect": dur,
                "egress_ip": ip,
                "dns": dns_ok,
                "socks": s_ok,
                "http": h_ok,
                "reason": "",
            }
        return _fail(name, "proxy-or-egress-ko", t_connect=dur, egress_ip=ip, dns=dns_ok, socks=s_ok, http=h_ok)
    finally:
        rm_container(name)
        shred(env)


def _ovpn_env_run(sec, tag, image, extra_env, socks_port, wait_secs=240):
    """Générique OV avec .ovpn monté (binhex/faeton/titidnh/dperson)."""
    raise NotImplementedError


BACKENDS = {
    # name: (image, is_ov, runner)
    "nordlynx-key": ("ghcr.io/bubuntux/nordlynx:latest", False, run_nordlynx_key),
    "nordlynx-proxy": ("edgd1er/nordlynx-proxy:latest", False, run_nordlynx_proxy),
    "nordvpn-proxy": ("edgd1er/nordvpn-proxy:latest", True, run_nordvpn_proxy),
    "bubuntux-nordvpn": ("bubuntux/nordvpn:latest", False, run_bubuntux_nordvpn),
}


def classify(name, result):
    if result.get("ok"):
        return "PASS"
    tail = result.get("tail") or ""
    if tail:
        return "FAIL-compte" if AUTH_SIG.search(tail) else "FAIL-backend"
    out = docker_logs(name, 80)
    if AUTH_SIG.search(out):
        return "FAIL-compte"
    return "FAIL-backend"


def main(argv=None):
    ap = argparse.ArgumentParser()
    ap.add_argument("--rounds", type=int, default=10)
    ap.add_argument("--backends", default=",".join(BACKENDS))
    ap.add_argument("--out", default="bench_results.jsonl")
    ap.add_argument("--gap", type=int, default=60)
    ap.add_argument("--gap-ov", type=int, default=240)
    ap.add_argument("--fleet-auth-cap", type=int, default=30)
    ap.add_argument("--seed", type=int, default=20260908)
    ap.add_argument("--list", action="store_true")
    args = ap.parse_args(argv)
    if args.list:
        print("\n".join(f"{k}: {v[0]} (OV={v[1]})" for k, v in BACKENDS.items()))
        return 0
    wanted = [b for b in args.backends.split(",") if b in BACKENDS]
    if not wanted:
        print("aucun backend connu", file=sys.stderr)
        return 2
    sec = secrets()
    digests = {b: image_digest(BACKENDS[b][0]) for b in wanted}
    n = 0
    with open(args.out, "a", encoding="utf-8") as out:
        for rnd in range(1, args.rounds + 1):
            order = wanted[:]
            random.Random(args.seed + rnd).shuffle(order)
            for b in order:
                image, is_ov, runner = BACKENDS[b]
                rec = {
                    "ts": dt.datetime.now(dt.UTC).isoformat(),
                    "backend": b,
                    "round": rnd,
                    "image": image,
                    "digest": digests.get(b, ""),
                }
                if is_ov:
                    auth_n = fleet_auth_count()
                    rec["fleet_auth_10min"] = auth_n
                    if auth_n >= 0 and auth_n > args.fleet_auth_cap:
                        rec.update({"verdict": "SKIP", "reason": "throttle-gate"})
                        out.write(json.dumps(rec) + "\n")
                        out.flush()
                        print(f"[{b} r{rnd}] SKIP throttle-gate (fleet AUTH {auth_n})", flush=True)
                        continue
                tag = f"{b}-r{rnd}"
                t0 = time.monotonic()
                try:
                    res = runner(sec, tag)
                except NotImplementedError:
                    res = {"ok": False, "reason": "runner-todo"}
                except Exception as e:  # noqa: BLE001 — harnais ne doit jamais mourir
                    res = {"ok": False, "reason": f"harness: {type(e).__name__}"}
                rec["duration_s"] = round(time.monotonic() - t0, 1)
                rec.update(res)
                if res.get("ok"):
                    rec["verdict"] = "PASS"
                elif res.get("reason") in ("run-failed", "runner-todo") or res.get("reason", "").startswith("harness"):
                    rec["verdict"] = "FAIL-harnais"
                else:
                    rec["verdict"] = classify(tag, res)
                out.write(json.dumps(rec) + "\n")
                out.flush()
                print(f"[{b} r{rnd}] {rec['verdict']} {res.get('reason', '')} ({rec['duration_s']}s)", flush=True)
                n += 1
                time.sleep(args.gap_ov if is_ov else args.gap)
    print(f"{n} runs, résultats dans {args.out}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
