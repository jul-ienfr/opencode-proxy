"""[canari WG 25/08] Aucun flip vers WireGuard sans egress prouvé.

Le bug « stations 2-3/4 persistant » : en mode auto, après auto_ov_return_min
minutes saines sur OpenVPN, le retour « WG préféré » se faisait SANS valider
que le chemin WG passait réellement du trafic (le fournisseur peut black-holer
WG en silence : handshake local OK, DNS interne mort). La flotte flappait
OV → WG mort → OV → WG… indéfiniment.

Contrats testés :
  - ``_cancel_wg_flip_if_canary_dead`` annule un flip wireguard quand le
    canari n'a PAS d'egress (pending remis à None, stack courant conservé) ;
  - laisse passer le flip quand le canari valide ;
  - ne touche JAMAIS aux flips vers openvpn ;
  - le verdict canari est TTL-caché (une seule validation par fenêtre) ;
  - la décision pure ``_auto_flip_decision`` reste inchangée (le verrou est
    à l'APPLICATION, pas à la décision).
"""

import os

import pytest

import vpn_manager as vm


class _CanaryMgr(vm.VPNManager):
    """VPNManager réel + frontières docker/réseau remplacées."""

    _WG_CANARY_POLL_INTERVAL_S = 0.0  # tests : zéro attente réelle

    def __init__(self, cfg, tmp_path):
        cfg = dict(cfg)
        cfg.setdefault("state_file", str(tmp_path / "vpn_state.json"))
        super().__init__(cfg, station=1)
        self._wg_key_file = str(tmp_path / "wireguard.env")
        self.compose_calls: list[list[str]] = []
        self.probe_calls = 0

    def _docker_run(self, args, timeout=30, env=None):  # noqa: D102
        self.compose_calls.append(args)

        class _R:
            returncode = 0
            stdout = ""
            stderr = ""

        return _R()

    async def _canary_probe_once(self) -> bool:  # noqa: D102
        self.probe_calls += 1
        return self._canary_probe_result

    # injecté par les tests
    _canary_probe_result = False


class _Clock:
    """Horloge monotone de test — avance d'1 s PAR APPEL (le poll du canari
    doit toujours voir le temps passer) + sauts explicites pour le TTL.
    Supporte clock["t"] pour compatibilité avec les anciens tests."""

    def __init__(self, t=0.0):
        self.t = t
        self._step = 0.0

    def set_step(self, step):
        self._step = step

    def advance(self, dt):
        self.t += dt

    def __call__(self):
        self.t += self._step
        return self.t

    def __getitem__(self, key):
        assert key == "t"
        return self.t


def _mgr(tmp_path, **over):
    from config.settings import yaml_get  # noqa: F401 — import coût nul

    cfg = {
        "enabled": True,
        "vpn_stack": "auto",
        "auto_wg_egress_ticks": 3,
        "auto_flip_cooldown_min": 5,
        "auto_ov_return_min": 60,
        "auto_ov_fail_threshold": 3,
    }
    cfg.update(over)
    return _CanaryMgr(cfg, tmp_path)


# ── Le verrou d'application ──────────────────────────────────────────


@pytest.mark.asyncio
async def test_flip_to_wg_cancelled_when_canary_dead(tmp_path):
    m = _mgr(tmp_path)
    m._stack_effective = "openvpn"
    m._pending_flip = ("wireguard", "OV healthy 60 min — return to WG")
    m._canary_probe_result = False  # chemin WG muet

    cancelled = await m._cancel_wg_flip_if_canary_dead()

    assert cancelled is True
    assert m._pending_flip is None, "le flip doit être annulé"
    assert m._stack_effective == "openvpn", "la station reste sur OV"
    assert any("up" in c for c in m.compose_calls), "le canari a été monté"
    assert any("rm" in c for c in m.compose_calls), "le canari a été démonté"


@pytest.mark.asyncio
async def test_flip_to_wg_applied_when_canary_alive(tmp_path):
    m = _mgr(tmp_path)
    m._stack_effective = "openvpn"
    m._pending_flip = ("wireguard", "OV healthy 60 min — return to WG")
    m._canary_probe_result = True  # chemin WG vivant

    cancelled = await m._cancel_wg_flip_if_canary_dead()

    assert cancelled is False
    assert m._pending_flip == ("wireguard", "OV healthy 60 min — return to WG"), (
        "le flip doit rester en attente d'application normale"
    )


@pytest.mark.asyncio
async def test_openvpn_flips_never_blocked(tmp_path):
    m = _mgr(tmp_path)
    m._stack_effective = "wireguard"
    m._pending_flip = ("openvpn", "egress dead 3 ticks")
    m._canary_probe_result = False  # même canari mort, OV n'est pas concerné

    cancelled = await m._cancel_wg_flip_if_canary_dead()

    assert cancelled is False
    assert m._pending_flip == ("openvpn", "egress dead 3 ticks")
    assert m.compose_calls == [], "aucun docker appelé pour un flip OV"


@pytest.mark.asyncio
async def test_no_pending_flip_is_noop(tmp_path):
    m = _mgr(tmp_path)
    m._pending_flip = None
    assert await m._cancel_wg_flip_if_canary_dead() is False
    assert m.compose_calls == []


# ── TTL du verdict canari ────────────────────────────────────────────


@pytest.mark.asyncio
async def test_canary_verdict_is_ttl_cached(tmp_path):
    m = _mgr(tmp_path)
    m._canary_probe_result = True
    clock = _Clock()
    m._now_fn = clock

    assert await m._wg_canary_alive("r1") is True
    first_calls = len(m.compose_calls)
    # Verdict re-consulté dans la fenêtre TTL (horloge figée) → zéro docker
    assert await m._wg_canary_alive("r2") is True
    assert len(m.compose_calls) == first_calls
    # Expiration du TTL → re-validation complète (probe passe encore)
    clock.advance(m._WG_CANARY_TTL_S + 1)
    assert await m._wg_canary_alive("r3") is True
    assert len(m.compose_calls) > first_calls


@pytest.mark.asyncio
async def test_canary_failure_throttles_docker_calls(tmp_path):
    """WG cassé : cinq flips rapprochés ne déclenchent qu'UNE validation
    docker — le verdict négatif vit son TTL, le marteau-docker est impossible."""
    m = _mgr(tmp_path)
    clock = _Clock()
    clock.set_step(1.0)  # le poll doit voir le temps passer pour finir
    m._now_fn = clock
    m._canary_probe_result = False

    for _ in range(5):
        clock.set_step(0.0)  # tentatives rapprochées (même instant)
        m._pending_flip = ("wireguard", "return")
        assert await m._cancel_wg_flip_if_canary_dead() is True
    ups = [c for c in m.compose_calls if "up" in c]
    rms = [c for c in m.compose_calls if "rm" in c]
    assert len(ups) == 1 and len(rms) == 1


# ── La décision pure reste inchangée ────────────────────────────────


@pytest.mark.asyncio
async def test_auto_decision_return_flip_is_the_one_blocked(tmp_path):
    """Bout-en-bout décision → verrou : la décision pure propose le retour
    WG (« OV healthy … »), et c'est exactement CE flip que le canari bloque
    quand le chemin WireGuard est muet."""
    m = _mgr(tmp_path)
    clock = _Clock()
    m._now_fn = clock
    m._stack_effective = "openvpn"
    m._stack_since = clock["t"] - 61 * 60  # OV sain > auto_ov_return_min
    m._auth_failed_window = []
    m._egress_failures = 0
    m._last_auto_flip_at = None
    # Le retour WG exige la clé NordLynx présente (_wg_key_present)
    from pathlib import Path

    Path(m._wg_key_file).write_text("[Interface]\n", encoding="utf-8")
    decision = m._auto_flip_decision()
    assert decision is not None and decision[0] == "wireguard"

    m._pending_flip = decision
    m._canary_probe_result = False  # provider WG black-hole (cas réel du 25/08)
    assert await m._cancel_wg_flip_if_canary_dead() is True
    assert m._pending_flip is None


# ── [drift-fix 25/08] boot auto : la stack suit le .env, pas la clé ──


def test_boot_auto_resolves_stack_from_env_not_key(tmp_path):
    """Le moteur du bug persistant : au boot en mode auto, l'heuristique
    « clé WG présente ⇒ WG » réimposait WireGuard via les recovery compose
    même après un passage OpenVPN sain. La résolution doit suivre le .env
    persisté par station ; la clé ne fait plus autorité."""
    import os

    compose = tmp_path / "docker-compose.yml"
    compose.write_text("services: {}\n", encoding="utf-8")
    (tmp_path / ".env").write_text("VPN_TYPE_STATION1=openvpn\n", encoding="utf-8")
    key_file = tmp_path / "wireguard.env"
    key_file.write_text("WIREGUARD_PRIVATE_KEY=abc\n", encoding="utf-8")

    cfg = {
        "enabled": True,
        "vpn_stack": "auto",
        "state_file": str(tmp_path / "s1.json"),
        "docker_compose_file": str(compose),
    }
    m = _CanaryMgr(cfg, tmp_path)
    assert m._wg_key_file == str(key_file)
    assert os.path.exists(m._wg_key_file), "précondition : la clé WG existe bien"
    assert m._stack_effective == "openvpn", (
        "le .env par station prime sur la présence de la clé WG"
    )


def test_boot_auto_falls_back_to_key_when_env_missing(tmp_path):
    compose = tmp_path / "docker-compose.yml"
    compose.write_text("services: {}\n", encoding="utf-8")
    cfg = {
        "enabled": True,
        "vpn_stack": "auto",
        "state_file": str(tmp_path / "s1.json"),
        "docker_compose_file": str(compose),
    }
    m = _CanaryMgr(cfg, tmp_path)
    # Pas de .env du tout → fallback heuristique historique (clé absente → OV)
    assert not os.path.exists(m._wg_key_file)
    assert m._stack_effective == "openvpn"


def test_churn_loop_triggers_flip_to_openvpn(tmp_path):
    """La boucle healthcheck gluetun prouve le tunnel mort même quand le
    compteur egress ne monte pas (sondes dans les fenêtres vivantes) :
    churn confirmé + 1 heal raté ⇒ décision de flip vers OpenVPN."""
    m = _mgr(tmp_path)
    clock = _Clock()
    m._now_fn = clock
    m._stack_effective = "wireguard"
    m._egress_failures = 0  # jamais 3 ticks consécutifs (fenêtres vivantes)
    m._restart_churn = True
    m._watchdog_backoff._consecutive_failures = 1  # un heal déjà raté
    m._last_auto_flip_at = None

    decision = m._auto_flip_decision()
    assert decision is not None and decision[0] == "openvpn"
    assert "restart loop" in decision[1]
