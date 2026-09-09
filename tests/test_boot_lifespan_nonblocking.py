"""test_boot_lifespan_nonblocking.py — le lifespan ne bloque plus sur docker.

Régression du chantier "vitesse max" : le lifespan attendait en ligne
  * ``ensure_docker_running(timeout=60)`` (jusqu'à 65 s au cold-start Docker),
  * ``reconcile_orphan_containers()`` (``docker ps -a`` + inspects),
  * ``DockerEventWatcher.start()`` (spawn ``docker events`` si daemon DOWN).

Désormais seule une sonde rapide (≤ 8 s) reste en ligne ; le reste part en
tâches de fond (``_docker_ensure_bg`` / ``_reconcile_bg`` /
``_watcher_start_bg``), synchronisées via ``shared_state.docker_ready`` /
``shared_state.reconcile_task`` — l'ordre historique reconcile → start est
préservé DANS ``_boot_fanout``, pas dans le lifespan.

Offline (aucun docker/réseau) : gardes sur le source du lifespan + contrat
des attributs shared_state.
"""

import inspect

import opencode as oc
import shared_state


def _lifespan_src():
    return inspect.getsource(oc.lifespan)


def test_lifespan_no_blocking_docker_awaits():
    """Aucun await docker long au niveau lifespan (indentation 4)."""
    src = _lifespan_src()
    # ensure long historique : disparu du chemin en ligne (quick-probe 5 s).
    assert "ensure_docker_running(timeout=60)" not in src or "_docker_ensure_bg" in src
    # reconcile : plus d'await en ligne — vit dans _reconcile_bg.
    assert "\n    _removed = await reconcile_orphan_containers(" not in src
    # watcher : plus d'await start() en ligne — vit dans _watcher_start_bg.
    assert "\n            await _watcher.start()" not in src


def test_lifespan_spawns_background_tasks():
    """Les trois tâches de fond existent et sont créées au boot."""
    src = _lifespan_src()
    assert "async def _docker_ensure_bg():" in src
    assert "async def _reconcile_bg():" in src
    assert "async def _watcher_start_bg():" in src
    assert "shared_state.reconcile_task = asyncio.create_task(_reconcile_bg())" in src
    assert "asyncio.create_task(_docker_ensure_bg())" in src
    assert "asyncio.create_task(_watcher_start_bg())" in src


def test_boot_fanout_waits_reconcile_and_docker_gate():
    """_boot_fanout préserve l'ordre : reconcile puis gate docker_ready."""
    src = _lifespan_src()
    assert 'getattr(shared_state, "reconcile_task", None)' in src
    assert 'getattr(shared_state, "docker_ready", None)' in src


def test_shared_state_boot_contract():
    """Les attributs du contrat boot existent (gate + handles)."""
    assert hasattr(shared_state, "docker_ready")
    assert hasattr(shared_state, "reconcile_task")
    assert hasattr(shared_state, "boot_fanout_task")
    assert hasattr(shared_state, "boot_error")
