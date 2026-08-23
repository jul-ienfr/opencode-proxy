"""test_review_fixes.py — [Revue 19/08] tests offline des 4 correctifs F1-F4.

Suite d'adversarial review sur les 4 zones candidates trouvées en relecture
(suite Axe 1-4, commit de regroupement) — chaque test isole le correctif sur
le code RÉEL (pool + manager réels, seules les frontières docker/réseau
sont fausses — le pattern FakeVPNManager établi de test_vpn_freshness).
Aucun docker réel, aucun réseau (invariant de la suite).

  * F1a (free_ip_pool.cancel_rotations — retire eager) : après cancel +
    await, les ids sont RETIRÉS du pool (guards fermés immédiatement) — un
    429 arrivant pendant la fenêtre stop()/stop_container() du downscale ne
    peut plus re-queuer une rotation sur un conteneur en cours de
    suppression ; ``_launch_rotation`` ignore les managers retirés.
  * F1b (vpn_manager.stop — _enabled=False) : le downscale bascule
    ``_enabled`` à False → le gate ``_connect_next_impl`` (RotationFailed
    "VPN disabled") devient effectif pour les managers retirés (orphelin
    shieldé ou 429 en course).
  * F2 (free_ip_pool._rotation_threshold — plancher 1) : la tête de quota
    ne dégénère jamais à 0 (station 4+ avec quota 15 / stagger 2 → 1, pas
    0) ; un seuil dégénéré (== 1) route ``on_request`` vers le kick
    THROTTLÉ au lieu du _launch_rotation non throttlé (pas de hot-loop
    docker), la requête passant quand même à l'autre station.
  * F3 (socks5 enabled) : un proxy désactivé n'est jamais ``_socks5_usable``
    ; le rebuild (hot-reload du proxy courant) re-résout par pid vers un
    endpoint ACTIVÉ (round-robin sinon), jamais vers un désactivé.
  * F4 (free_ip_pool._await_rotation — re-check bad_until) : une rotation
    dont la probe post-commit [Axe 1.2] a bad-marqué la nouvelle IP n'est
    PAS un succès — la requête retombe sur paid au lieu de servir une IP
    qui 429 à coup sûr.
"""

import asyncio
import time

import pytest

import free_ip_pool as fip
import vpn_manager as vm
from test_downscale_rotation_cancel import _blocking_switch, _pool, _wait_registered
from test_vpn_freshness import FakeVPNManager, _cfg


# ── F1a — cancel_rotations retire eager (guards fermés immédiatement) ──


@pytest.mark.asyncio
async def test_cancel_rotations_retires_state_before_teardown(tmp_path):
    """Downscale: après cancel + await, la station retirée disparaît de
    TOUTES les structures du pool — un 429 en pleine fenêtre stop()/
    stop_container() ne peut plus la re-queuer via le guard sid."""
    pool, ms = _pool(tmp_path, 2)
    gate = asyncio.Event()
    switched = []
    cancelled_at = []
    _blocking_switch(pool, gate, switched, cancelled_at)

    pool._launch_rotation(ms[1])  # station 2 tourne (bloquée)
    await _wait_registered(pool, 2)  # in-flight registration posée

    await pool.cancel_rotations([2])

    assert cancelled_at == [2], "CancelledError délivré à la rotation"
    assert 2 not in pool._station_ids, "sid retiré"
    assert [s._station for s in pool._stations] == [1], "station retirée de _stations"
    assert 2 not in pool._pending
    assert 2 not in pool._rotation_tasks, "registration popée par le finally"
    assert 2 not in pool._per, "état per-station pruné"

    # un handler qui tient encore le manager retiré ne peut plus le re-queuer
    pool._launch_rotation(ms[1])
    assert pool._pending == set()
    assert pool._rotation_queue.empty()
    gate.set()  # release (déjà déroulé, inoffensif)


# ── F1b — stop() bascule _enabled → le gate RotationFailed prend ──


@pytest.mark.asyncio
async def test_stop_flips_enabled_and_connect_refuses(tmp_path):
    """stop() coupe le robinet docker : _enabled=False rend le gate
    _connect_next_impl effectif — toute rotation visant un manager retiré
    (orphelin shieldé, 429 en course) lève RotationFailed au lieu de
    travailler sur un conteneur en cours de suppression."""
    mgr = FakeVPNManager(_cfg(tmp_path), station=1, tmp_path=tmp_path)
    mgr._enabled = True

    await mgr.stop()

    assert mgr._enabled is False, "stop() bascule _enabled"
    with pytest.raises(vm.RotationFailed):
        await mgr.connect_next()


# ── F2 — plancher _rotation_threshold + embrayage dégénéré ─────────


def test_rotation_threshold_floor_never_zero(tmp_path):
    """Quota 15 / stagger 2 : stations 4-6 → tête de quota négative →
    plancher 1 (les requêtes croisent toujours le seuil, mais le callable
    ne dégénère jamais à 0 — la hot-reload de quota ne peut pas neutraliser
    la rotation)."""
    pool, ms = _pool(tmp_path, 4)
    pool._rotation_stagger = 2
    for m in ms:
        m._quota_per_ip = 15
    assert pool._rotation_threshold(ms[0]) == 5  # 15-10-0
    assert pool._rotation_threshold(ms[1]) == 3  # 15-10-2
    assert pool._rotation_threshold(ms[2]) == 1  # 15-10-4
    assert pool._rotation_threshold(ms[3]) == 1  # 15-10-6 → max(1, -1)


@pytest.mark.asyncio
async def test_on_request_degenerate_threshold_uses_throttled_kick(tmp_path, monkeypatch):
    """Seuil dégénéré (== 1, marge de quota effondrée) : on_request route
    vers le kick THROTTLÉ (last_connect_attempt / connect_retry_interval) —
    pas de _launch_rotation non throttlé par requête (churn, hot-loop
    docker). La requête passe quand même à l'autre station, zéro attente."""
    pool, ms = _pool(tmp_path, 2)
    for m in ms:
        m._status = vm.VPNState.CONNECTED
    ms[0]._quota_per_ip = 11  # seuil station 1 = max(1, 1)
    pool._per_station(ms[0])["request_count"] = 1  # 1 >= 1 → croise

    kicked, launched = [], []
    monkeypatch.setattr(pool, "_kick_connect", lambda st: kicked.append(st))
    monkeypatch.setattr(pool, "_launch_rotation", lambda st: launched.append(st))

    url, station = await pool.on_request()

    assert kicked == [ms[0]], "embrayage dégénéré = kick throttlé"
    assert launched == [], "jamais de _launch_rotation non throttlé"
    assert station is ms[1], "requête servie par l'autre station (0 wait)"


@pytest.mark.asyncio
async def test_on_request_healthy_threshold_uses_launch(tmp_path, monkeypatch):
    """Seuil sain (> 1) : l'embrayage dual-clutch classique — launch de la
    rotation du départ et service immédiat de l'autre station."""
    pool, ms = _pool(tmp_path, 2)
    for m in ms:
        m._status = vm.VPNState.CONNECTED
    ms[0]._quota_per_ip = 15  # seuil station 1 = 5
    pool._per_station(ms[0])["request_count"] = 5  # 5 >= 5 → croise

    kicked, launched = [], []
    monkeypatch.setattr(pool, "_kick_connect", lambda st: kicked.append(st))
    monkeypatch.setattr(pool, "_launch_rotation", lambda st: launched.append(st))

    url, station = await pool.on_request()

    assert launched == [ms[0]], "rotation lancée en arrière-plan"
    assert kicked == []
    assert station is ms[1]


# ── F3 — un proxy socks5 désactivé n'est jamais serviable ──────────


def test_socks5_usable_refuses_disabled(tmp_path):
    """_station_usable (bâti pour VPNManager) ne connaît pas `enabled` —
    le correctif F3 met le check AVANT la délégation : un proxy désactivé
    (toggle GUI ou hot-reload) n'est jamais usable."""
    pool, ms = _pool(tmp_path, 1)
    pool.set_socks5_proxies(
        [
            {"host": "127.0.0.1", "port": 1080, "enabled": True},
            {"host": "127.0.0.1", "port": 1081, "enabled": True},
        ]
    )
    eps = pool._socks5_eps
    assert len(eps) == 2

    eps[0].enabled = False
    assert pool._socks5_usable(eps[0], exclude_approaching=False) is False
    assert pool._socks5_usable(eps[1], exclude_approaching=False) is True


def test_socks5_rebuild_resolves_current_to_enabled(tmp_path):
    """Hot-reload du proxy courant désactivé : le rebuild re-résout par pid
    vers un endpoint ACTIVÉ (round-robin) — jamais vers le désactivé ; tout
    désactivé → None (retombée paid), pas de proxy mort servi."""
    pool, ms = _pool(tmp_path, 1)
    pool.set_socks5_proxies(
        [
            {"host": "127.0.0.1", "port": 1080, "enabled": True},
            {"host": "127.0.0.1", "port": 1081, "enabled": True},
        ]
    )
    pool._socks5_current = pool._socks5_eps[0]

    # current (1080) re-poussé désactivé → re-résolution vers 1081
    pool.set_socks5_proxies(
        [
            {"host": "127.0.0.1", "port": 1080, "enabled": False},
            {"host": "127.0.0.1", "port": 1081, "enabled": True},
        ]
    )
    assert pool._socks5_current is not None
    assert pool._socks5_current.pid == "127.0.0.1:1081", "re-resolve saute le proxy désactivé"

    # tout désactivé → aucun courant (retombée paid)
    pool.set_socks5_proxies(
        [
            {"host": "127.0.0.1", "port": 1080, "enabled": False},
            {"host": "127.0.0.1", "port": 1081, "enabled": False},
        ]
    )
    assert pool._socks5_current is None


# ── F4 — _await_rotation ne sert pas une IP fraîche bad-marquée ────


async def _rotation_landing(pool, station, *, bad):
    """In-caractère rotation de fond : pose une IP fraîche, et (bad=True)
    bad-marque cette IP comme le fait la probe post-commit [Axe 1.2] juste
    avant la fin de la tâche."""

    async def fake_rotation():
        station._current_ip = "10.0.0.2"
        if bad:
            pool._per_station(station)["bad_until"] = time.monotonic() + 60

    t = asyncio.create_task(fake_rotation())
    pool._rotation_tasks[station._station] = t


@pytest.mark.asyncio
async def test_await_rotation_rejects_probe_bad_marked_fresh_ip(tmp_path):
    """La probe post-commit a bad-marqué la nouvelle IP avant que le waiter
    se réveille : _await_rotation renvoie False → paid, et la station
    re-rottera. Sans le correctif F4 le waiter servait l'IP morte au
    prochain appel (429 garanti)."""
    pool, ms = _pool(tmp_path, 1)
    st = ms[0]
    st._current_ip = "10.0.0.1"
    st._status = vm.VPNState.CONNECTED

    await _rotation_landing(pool, st, bad=True)
    assert await pool._await_rotation(st) is False


@pytest.mark.asyncio
async def test_await_rotation_accepts_healthy_fresh_ip(tmp_path):
    """Contrôle : IP fraîche sans bad_until → succès (cas nominal inchangé)."""
    pool, ms = _pool(tmp_path, 1)
    st = ms[0]
    st._current_ip = "10.0.0.1"
    st._status = vm.VPNState.CONNECTED

    await _rotation_landing(pool, st, bad=False)
    assert await pool._await_rotation(st) is True
    assert st.current_ip == "10.0.0.2"
