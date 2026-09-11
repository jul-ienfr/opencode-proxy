"""test_boot_phase3b_lazy.py — contrats du boot phase 3b-1 (httpx différé).

Contexte : ``import httpx`` coûtait ~81 ms sur le chemin import → listen, dont
~63 ms dans ``httpx/__init__.py`` qui importe ``._main`` (CLI) → ``rich`` +
``click`` + ``pygments``. Aucun de ces modules ne sert au proxy.

Ce fichier verrouille :

  1. ``core.lazy.LazyModule`` : délégation, mémoïsation, patch d'attribut
     (monkeypatch des tests), et non-chargement tant qu'on ne touche à rien ;
  2. l'ABSENCE d'import lourd au boot d'``opencode`` (httpx/rich/click/
     pygments/tiktoken) — la régression la plus facile à réintroduire ;
  3. le budget d'import (garde-fou haut, la cible fine est mesurée en CI) ;
  4. que ``config`` ne déclenche plus d'I/O réseau à l'import (le thread
     free-discovery partait pendant la fenêtre import → listen).

Offline : aucun réseau, aucune docker.
"""

from __future__ import annotations

import subprocess
import sys

import pytest

from core.lazy import LazyModule

# Modules dont le chargement au boot est un RÉGRESSION (cf. docstring).
FORBIDDEN_AT_BOOT = ("httpx", "rich", "click", "pygments", "tiktoken", "pystray", "webview")


# ── 1. LazyModule ────────────────────────────────────────────────────


def test_lazy_module_defers_import():
    """Le module n'est chargé qu'au premier accès d'attribut."""
    name = "colorsys"  # stdlib, jamais chargé par pytest
    sys.modules.pop(name, None)
    proxy = LazyModule(name)
    assert name not in sys.modules, "LazyModule a chargé le module à la construction"
    assert "deferred" in repr(proxy)
    # Premier accès → chargement.
    assert hasattr(proxy, "rgb_to_hls")
    assert name in sys.modules
    assert "loaded" in repr(proxy)


def test_lazy_module_delegates_and_memoises():
    proxy = LazyModule("colorsys")
    first = proxy.rgb_to_hls
    second = proxy.rgb_to_hls
    assert first is second, "l'attribut doit être résolu sur le VRAI module (identité stable)"
    # rouge pur → teinte 0, luminosité 0.5, saturation 1 (comportement stdlib).
    assert proxy.rgb_to_hls(1.0, 0.0, 0.0) == (0.0, 0.5, 1.0)


def test_lazy_module_attribute_error_is_the_real_one():
    """Un attribut inexistant doit lever AttributeError (comme un vrai module)."""
    proxy = LazyModule("colorsys")
    missing = "ce_module_nexiste_pas"
    with pytest.raises(AttributeError):
        getattr(proxy, missing)


def test_lazy_module_setattr_patches_the_real_module(monkeypatch):
    """``monkeypatch.setattr(oc.httpx, "AsyncClient", stub)`` doit marcher.

    Régression réelle : les tests (test_free_vpn_required, test_tool_compat)
    patchent l'attribut du module tel qu'il est lié dans ``opencode``. Sans
    ``__setattr__`` délégué, on ne patcherait que le proxy et le code amont
    continuerait d'utiliser le vrai httpx — test vert mais comportement faux.
    """
    import colorsys

    proxy = LazyModule("colorsys")
    monkeypatch.setattr(proxy, "rgb_to_hls", "SENTINEL")
    assert colorsys.rgb_to_hls == "SENTINEL", "le patch doit atteindre le vrai module"
    assert proxy.rgb_to_hls == "SENTINEL"


def test_lazy_module_dir_lists_real_attributes():
    proxy = LazyModule("colorsys")
    assert "rgb_to_hls" in dir(proxy)


def test_lazy_load_helper_returns_module():
    import colorsys

    from core.lazy import load

    assert load("colorsys") is colorsys


# ── 2/3. Contrats du boot (sous-processus : état vierge obligatoire) ──


def _boot_probe(code: str) -> str:
    """Exécute un probe dans un interpréteur NEUF (sys.modules vierge)."""
    proc = subprocess.run(
        [sys.executable, "-c", code],
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
        timeout=180,
    )
    assert proc.returncode == 0, f"probe échoué:\n{proc.stdout}\n{proc.stderr}"
    return (proc.stdout or "") + (proc.stderr or "")


def test_no_heavy_module_loaded_at_boot():
    """Aucun import lourd (httpx/rich/click/tiktoken…) au boot."""
    out = _boot_probe(
        f"import sys, opencode;bad=[m for m in {FORBIDDEN_AT_BOOT!r} if m in sys.modules];print('HEAVY:', bad)"
    )
    assert "HEAVY: []" in out, f"modules lourds chargés au boot → {out}"


def test_httpx_not_loaded_but_still_patchable():
    """httpx absent du boot, mais l'attribut reste utilisable et patchable."""
    out = _boot_probe(
        "import sys, opencode as oc;"
        "print('LOADED:', 'httpx' in sys.modules);"
        "print('ATTR:', type(oc.httpx).__name__);"
        "oc.httpx.AsyncClient = 'STUB';"
        "import httpx;"
        "print('DELEGATED:', httpx.AsyncClient == 'STUB')"
    )
    assert "LOADED: False" in out
    assert "ATTR: LazyModule" in out
    assert "DELEGATED: True" in out


def test_opencode_import_budget():
    """Budget d'import : garde-fou haut (la cible fine <800 ms est en CI).

    Mesuré ~450-500 ms sur le poste de dev ; on tolère jusqu'à 900 ms ici pour
    ne pas dépendre de la charge machine, tout en attrapant le retour d'un
    import lourd (tiktoken seul ajoutait 1-2 s à froid).
    """
    out = _boot_probe(
        "import time;t=time.perf_counter();import opencode;print('MS: %.0f' % ((time.perf_counter()-t)*1000))"
    )
    ms = float([ln for ln in out.splitlines() if ln.startswith("MS:")][0].split(":")[1])
    assert ms < 900, f"import opencode = {ms:.0f} ms (budget 900 ms)"


def test_config_import_does_no_network():
    """``import config`` ne doit plus lancer le fetch free-discovery.

    Avant la Phase 3b-1, ``config.settings`` spawnait un thread qui appelait
    ``fetch_free_models_sync()`` (fetch HTTP timeout 10 s + ``import httpx``)
    PENDANT la fenêtre import → listen. Le fetch est désormais déclenché par
    le lifespan, en tâche de fond post-yield.
    """
    out = _boot_probe(
        "import sys, config, config.settings as st;"
        "print('HTTPX:', 'httpx' in sys.modules);"
        "print('HAS_BOOT_FN:', hasattr(st, 'ensure_free_models_on_boot'))"
    )
    assert "HTTPX: False" in out, f"config importe httpx au boot → {out}"
    assert "HAS_BOOT_FN: True" in out, "le hook de boot free-discovery doit exister"


def test_free_discovery_boot_hook_still_works(monkeypatch):
    """Le hook de boot appelle bien le fetch quand la découverte est activée."""
    import config.settings as st

    calls = {"n": 0}
    monkeypatch.setattr(st, "_ensure_free_models_async", lambda: calls.__setitem__("n", calls["n"] + 1))
    monkeypatch.setattr(st, "FREE_DISCOVERY_ENABLED", True)
    st.ensure_free_models_on_boot()
    assert calls["n"] == 1

    monkeypatch.setattr(st, "FREE_DISCOVERY_ENABLED", False)
    st.ensure_free_models_on_boot()
    assert calls["n"] == 1, "découverte désactivée → aucun fetch"
