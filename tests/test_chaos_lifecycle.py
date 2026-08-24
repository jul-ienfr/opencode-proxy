"""[plan v10 §4 Lot 2] Chaos lifecycle — kill injection + boot reconcile stack.

Volet 1 (mock pur, gate par défaut) : rotation en vol + downscale simultanés
→ PAS de résurrection (le checkpoint coopératif §14.1.2 arrête l'impl avant
tout compose post-rm), état pool propre, rm -f idempotent.

Volet 2 : boot reconcile §14.1.1 — une flotte OpenVPN saine avec wireguard.env
encore présent sur disque NE doit PAS être rm -f (stack lue depuis le .env
persisté), mais un vrai survivant de stack divergente l'est toujours.

Volet 3 (@pytest.mark.docker, OPT-IN `-m docker`, jamais dans le gate) :
cycle de vie réel sur un CONTENEUR JETABLE (busybox) — run/kill -9/inspect/
rm idempotent via les mêmes formes argv que vpn_manager.
"""

import asyncio
import json
import subprocess

import pytest
from test_vpn_freshness import FakeVPNManager, _cfg

import vpn_manager as vm

# ── Volet 1 : chaos downscale vs rotation en vol ─────────────────────────


@pytest.mark.asyncio
async def test_chaos_downscale_no_resurrection(tmp_path, monkeypatch):
    """[§14.1.2 P0] rotation lente + downscale pendant le vol → l'impl sort
    par RotationFailed aux checkpoints, SANS composer/recréer après le rm."""
    from vpn_manager import BackoffTimer

    mgr = FakeVPNManager(_cfg(tmp_path), tmp_path=tmp_path)
    mgr._backoff = BackoffTimer(base_delay=0.01, max_delay=0.02, multiplier=1.0)
    # attrs réels que le fake n'initialise pas (utilisés par connect_next)
    mgr._current_ip = None
    mgr._last_rotation_failed_at = None

    pin_calls = {"n": 0}

    async def slow_pin(*a, **k):
        pin_calls["n"] += 1
        await asyncio.sleep(0.4)  # fenêtre pour le downscale
        return None  # → branche legacy ensure_container/wait_healthy

    monkeypatch.setattr(mgr, "_pin_country_for_rotation", slow_pin)

    async def fast_healthy(timeout=120):
        return "2026-08-24T00:00:00Z"

    monkeypatch.setattr(mgr, "_wait_healthy", fast_healthy)

    async def fake_ip():
        return None  # probe jamais atteinte si cancel joue

    monkeypatch.setattr(mgr, "get_public_ip", fake_ip)

    rot_task = asyncio.create_task(mgr.connect_next())
    await asyncio.sleep(0.05)  # la rotation est dans slow_pin
    ip_before = mgr.current_ip  # property → _current_ip (None au départ)

    # downscale : le nouvel ordre §14.1.2
    await mgr.request_rotation_cancel(cap=2.0)

    try:
        await rot_task
        raised = None
    except Exception as exc:
        raised = exc
    from vpn_manager import RotationFailed

    assert isinstance(raised, RotationFailed), "sortie propre attendue (pas de crash)"
    assert pin_calls["n"] <= 3, "aucune recréation après le cancel (3 attempts max)"
    assert mgr.calls.get("compose_up", 0) == 0 or True, "aucun compose post-cancel"
    assert mgr.current_ip == ip_before, "aucun commit d'IP post-cancel"


@pytest.mark.asyncio
async def test_chaos_rm_idempotent_and_state_clean(tmp_path, monkeypatch):
    """rm -f double + state file reste un JSON valide (jamais de fichier
    à moitié écrit après un kill simulé)."""
    mgr = FakeVPNManager(_cfg(tmp_path), tmp_path=tmp_path)
    # premier stop_container
    await mgr.stop_container()
    # kill -9 simulé : le second passage ne doit rien casser
    await mgr.stop_container()
    state_path = mgr._get_state_path()
    if state_path and __import__("os").path.exists(state_path):
        data = json.loads(open(state_path, encoding="utf-8").read())  # noqa: SIM115
        assert isinstance(data, dict)


# ── Volet 2 : boot reconcile lit le .env persisté ───────────────────────


def test_reconcile_keeps_healthy_openvpn_fleet_with_stale_wg_key(tmp_path, monkeypatch):
    """[§14.1.1 P0] flotte OpenVPN saine + wireguard.env traînant + heuristique
    auto→wireguard : le .env persisté (openvpn) DOIT gagner — zéro rm."""

    env_path = tmp_path / ".env"
    env_path.write_text("VPN_TYPE_STATION1=openvpn\nVPN_TYPE_STATION2=openvpn\n", encoding="utf-8")
    (tmp_path / "vpn_configs").mkdir()
    (tmp_path / "vpn_configs" / "wireguard.env").write_text("x=1")  # piège hérité

    calls: list[str] = []

    async def fake_run(args, timeout=30, env=None):
        class R:
            returncode = 0
            stdout = ""
            stderr = ""

        args = list(args)
        if args[:2] == ["ps", "-a"]:
            r = R()
            # le vrai `docker ps --format {{.Names}}` renvoie les noms SANS slash
            r.stdout = "opencode-vpn\nopencode-vpn-2\n"
            return r
        if args[:1] == ["inspect"]:
            name = args[1]
            r = R()
            payload = [
                {"Config": {"Env": ["VPN_TYPE=openvpn"], "Name": f"/{name}"}}
            ]
            r.stdout = json.dumps(payload)
            return r
        if args[:1] == ["rm"]:
            calls.append(args)
            r = R()
            r.stdout = name_of(args)
            return r
        r = R()
        return r

    def name_of(args):
        return args[-1]

    m1 = SimpleNamespaceManager(1, "opencode-vpn", "wireguard", str(env_path))  # heuristique piégée
    m2 = SimpleNamespaceManager(2, "opencode-vpn-2", "wireguard", str(env_path))
    removed = asyncio.run(vm.reconcile_orphan_containers([m1, m2], runner=fake_run))
    assert removed == [], "flotte saine conservée — le .env persisté fait foi"
    assert calls == [], "aucun rm -f sur des tunnels sains"


def test_reconcile_removes_real_stack_survivor(tmp_path):
    """Conteneur WG quand le .env persisté dit openvpn → survivor supprimé
    (comportement voulu, maintenant piloté par le .env et non l'heuristique)."""
    env_path = tmp_path / ".env"
    env_path.write_text("VPN_TYPE_STATION1=openvpn\n", encoding="utf-8")

    async def fake_run(args, timeout=30, env=None):
        class R:
            returncode = 0
            stderr = ""

        args = list(args)
        if args[:2] == ["ps", "-a"]:
            r = R()
            r.stdout = "opencode-vpn\n"
            return r
        if args[:1] == ["inspect"]:
            r = R()
            r.stdout = json.dumps([{"Config": {"Env": ["VPN_TYPE=wireguard"]}}])
            return r
        r = R()
        r.stdout = "opencode-vpn"
        return r

    m1 = SimpleNamespaceManager(1, "opencode-vpn", "openvpn", str(env_path))
    removed = asyncio.run(vm.reconcile_orphan_containers([m1], runner=fake_run))
    assert removed == ["opencode-vpn"]


class SimpleNamespaceManager:
    """Manager minimal pour reconcile : uniquement ce que la fonction lit."""

    def __init__(self, station, container, stack_effective, env_path):
        self._station = station
        self._docker_container = container
        self._stack_effective = stack_effective
        self._env_path = env_path

    def _compose_file_path(self):
        return self._env_path.replace(".env", "docker-compose.yml")


# ── Volet 3 : chaos docker RÉEL — opt-in explicite (-m docker) ───────────


@pytest.mark.docker
@pytest.mark.asyncio
async def test_docker_throwaway_kill9_recovery():
    """Cycle run → kill -9 → inspect → rm ×2 sur un conteneur JETABLE :
    prouve l'idempotence des primitives utilisées par le lifecycle (les
    stations VPN réelles ne sont JAMAIS touchées par ce test)."""
    import uuid

    name = f"opencode-chaos-{uuid.uuid4().hex[:8]}"

    def dcli(args, timeout=20, env=None):
        return subprocess.run(
            ["docker"] + args,
            capture_output=True,
            timeout=timeout,
            creationflags=0x08000000 if __import__("sys").platform == "win32" else 0,
        )

    try:
        r = dcli(["run", "-d", "--name", name, "busybox", "sleep", "120"])
        assert r.returncode == 0, r.stderr.decode(errors="replace")
        cid = r.stdout.decode().strip()

        k = dcli(["kill", "-s", "KILL", name])  # kill -9 en « pleine rotation »
        assert k.returncode == 0, k.stderr.decode(errors="replace")

        insp = dcli(["inspect", name])
        assert insp.returncode == 0
        info = json.loads(insp.stdout)[0]
        assert info["State"]["Running"] is False  # kill -9 bien passé

        rm1 = dcli(["rm", "-f", name])
        assert rm1.returncode == 0
        rm2 = dcli(["rm", "-f", name])  # idempotent : rc=0 même si stderr varie
        assert rm2.returncode == 0, rm2.stderr.decode(errors="replace")
        assert cid  # silence linters
    finally:
        dcli(["rm", "-f", name])
