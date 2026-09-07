"""upstream.clients — pool curl + clients HTTP par rôle (Phase 3 refonte).

Déplacement PUR depuis ``opencode.py`` (§ « Curl TLS pool » + « Clients HTTP
partagés par rôle »). AUCUN import du projet :

* ``CurlSessionSlot`` / ``CurlSessionPool`` : asyncio seul, déplacés tels
  quels (zéro global projet) ;
* ``evict_later`` / ``evict_idle_pools`` / ``close_all_pools`` /
  ``swap_pools_for_proxy`` : opèrent sur un dict de pools PASSÉ EN PARAMÈTRE
  (l'hôte possède ``_curl_pool`` — inspecté directement par
  ``test_curl_session_pool.py`` — et le passe À L'APPEL) ;
* ``RoleClientStore`` : le dict ``clients`` est exposé tel quel — l'hôte
  l'alias (``_role_clients = store.clients``, MÊME objet, muté par
  ``test_role_clients.py``) ; l'URL SOCKS liée et la grâce de close sont
  passées À L'APPEL (l'hôte les lit depuis ses globaux
  ``_role_tunnel_url()`` / ``_ROLE_CLIENT_CLOSE_GRACE_S``, patchés par tests) ;
* ``aclose_role_client_after`` / ``build_fresh_client`` : asyncio + httpx
  seuls, paramètres injectés.

NON déplacés (décision, autre phase) : ``_ensure_http_client`` + globaux
``_client``/``_transport`` (seam d'identité ``oc._client``,
``test_http_client_self_heal.py`` — Phase 7), ``_get_pooled_curl_session``
(wrapper : factory ``curl_cffi`` + ``_curl_proxy_url`` + métrique checkout —
Phase 6/observabilité), ``_execute_web_fetch`` (Phase 7 websearch),
``_role_tunnel_url`` (couplage VPN/free — Phase 6).
"""

from __future__ import annotations

import asyncio
import threading
import time
from collections.abc import Callable

import httpx

DEFAULT_POOL_SIZE = 3  # = config.yaml upstream.curl_sessions_per_station
DEFAULT_IDLE_TTL_S = 600.0
DEFAULT_ROLE_TIMEOUT = httpx.Timeout(connect=5.0, read=30.0, write=10.0, pool=5.0)
DEFAULT_CLOSE_GRACE_S = 60.0
CHECKOUT_WAIT_S = 5.0  # attente bornée d'une restitution avant overflow
POOL_SESSION_TIMEOUT = (10, 600)  # (connect, read) imposé aux sessions poolées


class CurlSessionSlot:
    __slots__ = ("sess", "busy", "overflow")

    def __init__(self, sess, overflow: bool = False):
        self.sess = sess
        self.busy = False
        # overflow=True : session créée AU-DELA de max_size quand le pool
        # est saturé (checkout timeout) — retirée du pool dès restitution.
        self.overflow = overflow


# Alias historique (opencode._CurlSessionSlot).
_CurlSessionSlot = CurlSessionSlot


class CurlSessionPool:
    """Pool FIFO de M sessions curl pour une clé (proxy, impersonate).

    checkout() réutilise une session libre, sinon crée dans la limite M,
    sinon attend une restitution (Condition — rare : concurrence > M).
    checkin() restitue ; evict() ferme une session fautive et libère sa
    place (l'éviction de la session fautive est conservée de l'ancien code).
    """

    # [P2.2] last_used/closing : éviction TTL des pools orphelins + drainage
    # swap-and-close à la rotation IP. busy_count est une propriété (classe).
    __slots__ = ("slots", "_cond", "max_size", "last_used", "closing")

    def __init__(self, max_size: int = DEFAULT_POOL_SIZE):
        self.slots: list[CurlSessionSlot] = []
        self._cond = asyncio.Condition()
        self.max_size = max(1, int(max_size))
        self.last_used = time.monotonic()
        self.closing = False

    @property
    def busy_count(self) -> int:
        """Slots empruntés (les slots restent listés dans self.slots même
        busy — le garde busy est VITAL avant tout close_all())."""
        return sum(1 for s in self.slots if s.busy)

    def _try_checkout(self) -> CurlSessionSlot | None:
        for slot in self.slots:
            if not slot.busy:
                slot.busy = True
                return slot
        return None

    async def checkout(self, factory) -> CurlSessionSlot:
        async with self._cond:
            self.last_used = time.monotonic()
            slot = self._try_checkout()
            while slot is None:
                if len(self.slots) < self.max_size:
                    slot = CurlSessionSlot(factory())
                    slot.busy = True
                    self.slots.append(slot)
                    return slot
                # M/M occupées → attendre une restitution BORNÉE (5 s). Au-delà,
                # créer une session overflow hors quota plutôt que bloquer la
                # requête indéfiniment (head-of-line blocking réintroduit).
                try:
                    await asyncio.wait_for(self._cond.wait(), timeout=CHECKOUT_WAIT_S)
                except TimeoutError:
                    slot = CurlSessionSlot(factory(), overflow=True)
                    slot.busy = True
                    self.slots.append(slot)
                    return slot
                slot = self._try_checkout()
            return slot

    async def checkin(self, slot: CurlSessionSlot) -> None:
        async with self._cond:
            if self.closing:
                # [P2.3] pool en drainage (swap post-rotation) : fermer la
                # session au lieu de la restocker — le drain se termine
                # naturellement au dernier checkin.
                try:
                    self.slots.remove(slot)
                except ValueError:
                    pass
                try:
                    await slot.sess.close()
                except Exception:
                    pass
                self._cond.notify()
                return
            if slot in self.slots:
                slot.busy = False
                if slot.overflow:
                    # Auto-réduction : l'overflow quitte le pool dès sa
                    # restitution (retour au quota M en régime stable).
                    self.slots.remove(slot)
                    try:
                        await slot.sess.close()
                    except Exception:
                        pass
            self._cond.notify()

    async def evict(self, slot: CurlSessionSlot) -> None:
        """Ferme une session fautive et la retire du pool."""
        async with self._cond:
            try:
                self.slots.remove(slot)
            except ValueError:
                pass
            try:
                await slot.sess.close()
            except Exception:
                pass
            self._cond.notify()

    def discard(self, slot: CurlSessionSlot) -> None:
        """Retire sans fermer (chemin annulation : pas d'await possible)."""
        try:
            self.slots.remove(slot)
        except ValueError:
            pass

    async def close_all(self) -> None:
        for slot in list(self.slots):
            try:
                await slot.sess.close()
            except Exception:
                pass
        self.slots.clear()


# Alias historique (opencode._CurlSessionPool, test_curl_session_pool.py).
_CurlSessionPool = CurlSessionPool


def evict_later(pool: CurlSessionPool, slot: CurlSessionSlot) -> None:
    """Fire-and-forget eviction — utilisable depuis un except CancelledError."""

    async def _do():
        try:
            await pool.evict(slot)
        except Exception:
            pass

    try:
        asyncio.get_running_loop().create_task(_do())
    except RuntimeError:
        pool.discard(slot)


# Alias historique (opencode._evict_later, test_curl_session_pool.py).
_evict_later = evict_later


async def evict_idle_pools(
    pools: dict[str, CurlSessionPool], ttl: float = DEFAULT_IDLE_TTL_S
) -> int:
    """Éviction TTL/LRU des pools curl orphelins — appelée par le tick
    background existant (30 s). Retourne le nombre de pools fermés.

    Protocole : pop de la clé du dict AVANT close_all() — un checkout
    concurrent reçoit None → crée un pool neuf. Garde busy_count == 0
    VITAL : les slots empruntés restent listés dans self.slots et
    close_all() les toucherait. La double vérification se fait SOUS la
    Condition du pool pour fermer la course avec un checkout en vol.
    """
    now = time.monotonic()
    freed = 0
    for key, pool in list(pools.items()):
        if pool.closing or (now - pool.last_used) < ttl:
            continue
        async with pool._cond:
            # Re-vérification sous verrou : un checkout qui a gagné la course
            # a posé last_used à jour ET rendu un slot busy.
            if pool.busy_count != 0 or (time.monotonic() - pool.last_used) < ttl:
                continue
            pools.pop(key, None)
            await pool.close_all()
            freed += 1
    return freed


async def close_all_pools(pools: dict[str, CurlSessionPool]) -> None:
    """Ferme tous les pools (lifespan shutdown) — vide le dict hôte."""
    for pool in list(pools.values()):
        await pool.close_all()
    pools.clear()


def swap_pools_for_proxy(
    pools: dict[str, CurlSessionPool],
    proxy_url: str | None,
    *,
    pool_size: int = DEFAULT_POOL_SIZE,
    debug_fn: Callable[..., None] | None = None,
) -> None:
    """[P2.3 perf] Swap-and-close des pools curl d'un proxy après rotation
    IP réussie — appelé par free_ip_pool depuis la branche ``alive:`` de
    _rotate_station (via le wrapper hôte).

    Économise 1 aller-retour échoué (+1-3 s) sur la première requête
    post-rotation : les sessions de l'ancien tunnel ne sont plus jamais
    réempruntées. Protocole SANS casser les requêtes en vol :
      - pop de la clé + pool NEUF immédiat dans le dict ;
      - busy_count == 0 → close_all() fire-and-forget (task) ;
      - sinon pool.closing = True : les checkins ferment leurs sessions au
        lieu de restocker, et une task de drainage ferme le reste dès que
        busy_count retombe à 0.
    """
    if not proxy_url:
        return
    prefix = f"{proxy_url}|"
    flushed = 0
    for key, old_pool in list(pools.items()):
        if not key.startswith(prefix):
            continue
        new_pool = CurlSessionPool(pool_size)
        pools[key] = new_pool  # remplace AVANT tout close
        old_pool.closing = True
        if old_pool.busy_count == 0:

            async def _close_now(p=old_pool):
                await p.close_all()

            try:
                asyncio.get_running_loop().create_task(_close_now())
            except RuntimeError:
                pass
        else:

            async def _drain(p=old_pool):
                while True:
                    async with p._cond:
                        if p.busy_count == 0:
                            break
                    await asyncio.sleep(0.5)
                await p.close_all()

            try:
                asyncio.get_running_loop().create_task(_drain())
            except RuntimeError:
                pass
        flushed += 1
    if flushed and debug_fn is not None:
        debug_fn(f"  [curl-pool] {flushed} pool(s) swapped after IP rotation")


class RoleClientStore:
    """Clients HTTP partagés par rôle (« direct » / « tunnel ») — [plan-perf Lot 1].

    PAS de client unique : « direct » (jamais de proxy : sondes IP/geo,
    dashboard) vs « tunnel » (proxy SOCKS du VPN actif). Rebuild automatique
    si l'URL SOCKS liée change (rotation) ou si le client a été fermé.
    L'ancien client est soldé APRÈS une grâce (requêtes en vol protégées).

    État (``clients``) exposé tel quel : l'hôte l'alias (MÊME objet —
    ``test_role_clients.py`` le snapshot/mute directement). ``bound_url`` et
    ``grace_s`` passés À L'APPEL (lus depuis les globaux hôtes, patchés par
    tests). Lock jamais tenu sur I/O (construction µs, double vérification
    impossible ici — le store sérialise sous lock threading comme l'hôte).
    """

    def __init__(
        self,
        *,
        timeout: httpx.Timeout | None = None,
        lock: threading.Lock | None = None,
    ):
        self.clients: dict[str, tuple[httpx.AsyncClient, str | None]] = {}
        self._lock = lock or threading.Lock()
        self._timeout = timeout or DEFAULT_ROLE_TIMEOUT

    def acquire(
        self,
        role: str = "direct",
        *,
        bound_url: str | None = None,
        grace_s: float = DEFAULT_CLOSE_GRACE_S,
    ) -> httpx.AsyncClient:
        """Client partagé du rôle. ``bound_url`` = proxy attendu (None =
        direct, jamais de proxy). Retourne toujours un client ouvert."""
        with self._lock:
            want = bound_url
            entry = self.clients.get(role)
            if entry is not None and entry[1] == want and not entry[0].is_closed:
                return entry[0]
            old = entry[0] if entry is not None else None
            if want:
                transport = httpx.AsyncHTTPTransport(proxy=want, retries=0)
            else:
                transport = httpx.AsyncHTTPTransport(retries=0)
            client = httpx.AsyncClient(transport=transport, timeout=self._timeout)
            self.clients[role] = (client, want)
        if old is not None and not old.is_closed:
            try:
                loop = asyncio.get_running_loop()
                loop.create_task(aclose_role_client_after(old, grace_s))
            except RuntimeError:
                pass  # pas de boucle ici — keepalive-expiry purge le résiduel
        return client


async def aclose_role_client_after(client: httpx.AsyncClient, delay: float) -> None:
    """Solde un ancien client de rôle après la période de grâce.

    Fail-soft intégral : un cancel ou une erreur de close ne doit jamais
    remonter (tâche fire-and-forget)."""
    try:
        await asyncio.sleep(delay)
    except asyncio.CancelledError:
        pass
    try:
        await client.aclose()
    except Exception:
        pass


# Alias historique (opencode._aclose_role_client_after, test_role_clients.py).
_aclose_role_client_after = aclose_role_client_after


def build_fresh_client(
    *,
    proxy: str | None,
    limits: httpx.Limits,
    timeout: httpx.Timeout,
) -> httpx.AsyncClient:
    """Client jetable « connexion neuve » (retentative stage-1 du watchdog) —
    même config que le client partagé, garanti sans connexion demi-morte
    réutilisée. Le caller doit l'aclose()."""
    _t = (
        httpx.AsyncHTTPTransport(proxy=proxy, limits=limits, http2=True, retries=0)
        if proxy
        else httpx.AsyncHTTPTransport(limits=limits, http2=True, retries=0)
    )
    return httpx.AsyncClient(transport=_t, timeout=timeout)


__all__ = [
    "CHECKOUT_WAIT_S",
    "DEFAULT_CLOSE_GRACE_S",
    "DEFAULT_IDLE_TTL_S",
    "DEFAULT_POOL_SIZE",
    "DEFAULT_ROLE_TIMEOUT",
    "POOL_SESSION_TIMEOUT",
    "CurlSessionPool",
    "CurlSessionSlot",
    "RoleClientStore",
    "aclose_role_client_after",
    "build_fresh_client",
    "close_all_pools",
    "evict_idle_pools",
    "evict_later",
    "swap_pools_for_proxy",
    "_CurlSessionPool",
    "_CurlSessionSlot",
    "_aclose_role_client_after",
    "_evict_later",
]
