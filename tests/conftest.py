import os

import pytest

pytest_plugins = ["pytest_asyncio"]


@pytest.fixture(autouse=True)
def _reset_disable_mapping():
    """Ensure DISABLE_MAPPING is False during tests unless explicitly set."""
    os.environ.pop("DISABLE_MAPPING", None)
    import config.settings as s

    s.DISABLE_MAPPING = False
    yield


@pytest.fixture(autouse=True)
def _reset_curl_pool():
    """[A1] Hermetic curl session pool between tests.

    Le pool de sessions curl_cffi est un état module-level persistant :
    sans reset, une session factice (mock) posée par un test fuite dans le
    test suivant (ex. _FakeSession 429 de test_pool_connection_failure
    resservie par test_invariant_a0 Path C). L'ancien schéma 1-session avait
    la même failence latente ; on la ferme hermétiquement ici.
    """
    yield
    try:
        import asyncio as _aio

        pool = getattr(__import__("opencode"), "_curl_pool", None)
        if pool:
            # Les sessions réelles ne doivent PAS être fermées ici si elles
            # partagent le loop du proxy ; en tests chaque loop meurt avec le
            # test — on vide seulement le registre (les fakes n'ont pas de
            # socket ; les vraies sont GC-ées).
            pool.clear()
    except Exception:
        pass
