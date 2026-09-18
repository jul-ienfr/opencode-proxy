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
        pool = getattr(__import__("opencode"), "_curl_pool", None)
        if pool:
            # Les sessions réelles ne doivent PAS être fermées ici si elles
            # partagent le loop du proxy ; en tests chaque loop meurt avec le
            # test — on vide seulement le registre (les fakes n'ont pas de
            # socket ; les vraies sont GC-ées).
            pool.clear()
    except Exception:
        pass


@pytest.fixture(autouse=True)
def _reset_global_429():
    """[FIX EOF nu] Coupe-circuit 429 global remis à neuf entre tests.

    ``_g429`` est un état module-level persistant (seuil 10 / fenêtre 30 s /
    backoff 15 s). ``test_free_multi_attempt.py`` appelle ``_on_free_429_stream``
    plus de dix fois directement : le coupe-circuit s'OUVRE alors pour 15 s et
    ``Global429BackoffMiddleware`` répond 503 à TOUTES les requêtes des tests
    suivants — d'où 30 échecs ``assert 503 == 200`` dans la suite complète.

    Preuve de l'ordre d'exécution :

    * ``pytest tests/test_free_multi_attempt.py tests/test_responses_stream_e2e.py``
      → échecs dans ``test_responses_stream_e2e`` ;
    * ordre inverse → 35/35 verts.

    Sans ce reset, le résultat de la suite dépend de l'ordre des fichiers.
    """
    import opencode as oc

    def _neuf() -> None:
        try:
            oc._g429._hits.clear()
            oc._g429._open_until = 0.0
        except Exception:
            pass

    _neuf()
    yield
    _neuf()
