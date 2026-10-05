import os
import tempfile

import pytest

pytest_plugins = ["pytest_asyncio"]

_MKDIR_0700_BROKEN = None  # cache de sonde (None = pas encore sondé)


def _fix_mkdir_0700_enumeration() -> bool:
    """[FIX env] ``os.mkdir(path, mode=0o700)`` → dossier NON énumérable.

    ``_pytest/tmpdir.py`` crée ses dossiers via ``mkdir(mode=0o700)`` (lignes
    133/152/162/166) puis ``cleanup_dead_symlinks()`` fait ``root.iterdir()``.
    Sur certaines couches de fichiers (bac à sable Windows), le mode 0o700 est
    traduit en refus d'accès : le dossier existe, ``st_mode`` vaut bien 0o40777
    et ses attributs Windows sont identiques à ceux d'un ``mkdir(p)`` normal,
    mais toute énumération lève ``PermissionError`` — et ``chmod`` échoue aussi.

    Résultat : 13 ``ERROR at setup`` sur le premier test réclamant ``tmp_path``
    (``tests/test_official_client_parity.py``), sans rapport avec le code testé.

    Correctif : neutraliser le mode sur les SEULS appels ``os.mkdir`` dont le
    mode vaut exactement 0o700, et uniquement si la sonde prouve que
    l'environnement est touché. Read-only ailleurs, no-op sur une machine saine.

    Sonde unique (résultat mis en cache module-level). Elle crée un dossier
    0o700 puis tente de l'énumérer. Si le bug est présent, ce dossier est
    indestructible (``rmdir``/``chmod`` refusés) : il reste alors un dossier vide
    dans le temp du bac à sable. C'est le prix de la détection — préférable à un
    patch inconditionnel qui modifierait le comportement d'une machine saine.
    """
    global _MKDIR_0700_BROKEN
    if _MKDIR_0700_BROKEN is not None:
        return _MKDIR_0700_BROKEN
    try:
        _probe = os.path.join(tempfile.gettempdir(), f"_p_{os.getpid()}")
        os.mkdir(_probe, 0o700)
    except OSError:
        _MKDIR_0700_BROKEN = False
        return False
    try:
        os.listdir(_probe)
        _MKDIR_0700_BROKEN = False  # environnement sain : ne rien patcher
    except PermissionError:
        _MKDIR_0700_BROKEN = True  # confirmé : le mode 0o700 casse l'énumération
    except OSError:
        _MKDIR_0700_BROKEN = False
    finally:
        try:
            os.rmdir(_probe)
        except OSError:
            pass
    return _MKDIR_0700_BROKEN


if _fix_mkdir_0700_enumeration():
    _real_mkdir = os.mkdir

    def _mkdir_enum_safe(path, mode=0o777, *args, **kwargs):
        """``os.mkdir`` sans le mode 0o700 (cf. ``_fix_mkdir_0700_enumeration``)."""
        if mode == 0o700:
            mode = 0o777
        return _real_mkdir(path, mode, *args, **kwargs)

    os.mkdir = _mkdir_enum_safe

    # Un dossier ``pytest-of-<user>`` empoisonné par un run PRÉCÉDENT (créé en
    # 0o700 avant ce correctif) reste indestructible : ``rmdir`` et ``chmod`` y
    # sont refusés. Il faut donc aussi changer de racine, sinon pytest échoue
    # toujours sur son `iterdir()`. Surchargeable via PYTEST_DEBUG_TEMPROOT.
    if not os.environ.get("PYTEST_DEBUG_TEMPROOT"):
        try:
            _user = __import__("getpass").getuser()
        except Exception:
            _user = ""
        _stale = os.path.join(tempfile.gettempdir(), f"pytest-of-{_user or 'unknown'}")
        if os.path.isdir(_stale):
            try:
                os.listdir(_stale)  # accessible : rien à faire
            except OSError:
                _fresh = os.path.join(tempfile.gettempdir(), "pytest-dshroot")
                try:
                    os.makedirs(_fresh, exist_ok=True)
                    os.environ["PYTEST_DEBUG_TEMPROOT"] = _fresh
                except OSError:
                    pass


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


@pytest.fixture(autouse=True)
def _reset_web_caches():
    """[P2-12] Caches web hermétiques entre tests.

    ``_FETCH_CACHE`` (singleflight fetch) et ``_DDG_CACHE`` sont des états
    module-level persistants : sans reset, un succès caché par un test
    masque le comportement du test suivant sur la même URL (ex. succès
    text/html en cache → ``test_fetch_rejected_content_type`` ne voit plus
    son octet-stream). Même pattern que ``_reset_curl_pool``.
    """
    import opencode as oc

    def _neuf() -> None:
        for name in ("_FETCH_CACHE", "_FETCH_LOCKS", "_DDG_CACHE", "_DDG_LOCKS"):
            try:
                getattr(oc, name, {}).clear()
            except Exception:
                pass

    _neuf()
    yield
    _neuf()
