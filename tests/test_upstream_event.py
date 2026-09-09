"""test_upstream_event.py — [phase 1] breaker « 429 corrélés » tous modes.

Contexte : en strict round-robin, 1 requête peut frapper 5 stations
(max_free_attempts) en bad-markant chacune + rotation à chaque 429 —
une vague amont (capacité/policy, pas quota) rase la flotte entière alors
que les tunnels vont bien. Le breaker G4 n'existait qu'en failover.

Règle : 3+ 429 sur des couples (station, IP) DISTINCTS / 60 s = événement
amont → gel des rotations 429 (180 s), cooldowns conservés (C1), refus
honnête via le chemin strict_free existant. 429 répétés sur le MÊME
bucket = quota per-IP réel → rotation normale.

Offline : doubles _Station (même forme que test_pool_station_set),
_launch_rotation spybée, jamais de docker.
"""

import time

import pytest

from free_ip_pool import FreeIPPool


class _Station:
    def __init__(self, sid, *, status="connected", current_ip=None, quota=500):
        self._station = sid
        self.enabled = True
        self.proxy_mode = "vpn"
        self.status = status
        self.current_ip = current_ip
        self.current_server = None
        self._quota_per_ip = quota
        self.socks5_url = f"socks5://127.0.0.1:1{sid}080"

    def get_status(self):
        return {"station": self._station, "status": self.status}

    def arm_egress_watchdog(self):
        pass


def _pool3():
    s = [_Station(1, current_ip="1.1.1.1"), _Station(2, current_ip="2.2.2.2"),
         _Station(3, current_ip="3.3.3.3")]
    pool = FreeIPPool(s[0], s[1])
    pool.set_stations(s)
    pool.rotated = []
    # NOTE : le gel centralisé vit dans _launch_rotation (réel) — le stub
    # le court-circuite, donc les tests ci-dessous exercent le gel via les
    # branches on_quota_exhausted/on_disconnect_retry, pas via ce stub.
    pool._launch_rotation = lambda station, forced_pool=None, **kw: pool.rotated.append(station)
    return pool, s


class TestUpstreamEvent:
    def test_three_distinct_pairs_declare_event_and_freeze_rotation(self):
        pool, (s1, s2, s3) = _pool3()
        pool.on_quota_exhausted(s1)
        assert pool._upstream_event_active() is False
        assert pool.rotated == [s1]  # 429 isolé → rotation normale
        pool.on_quota_exhausted(s2)
        assert pool._upstream_event_active() is False
        pool.on_quota_exhausted(s3)  # 3ᵉ couple distinct → événement
        assert pool._upstream_event_active() is True
        assert pool._upstream_event_remaining_s() > 0
        # Pendant l'événement : cooldown oui, rotation non.
        s1.current_ip = "1.1.1.2"
        n_before = len(pool.rotated)
        pool.on_quota_exhausted(s1)
        assert len(pool.rotated) == n_before
        st = pool.get_status()
        assert st["upstream_event_active"] is True
        assert st["upstream_event_remaining_s"] > 0

    def test_same_bucket_repeats_never_declare(self):
        """3× 429 sur le même (station, IP) = quota per-IP → rotations."""
        pool, (s1, _, _) = _pool3()
        for _ in range(4):
            pool.on_quota_exhausted(s1)
        assert pool._upstream_event_active() is False
        assert pool.rotated == [s1] * 4

    def test_event_expires_then_rotations_resume(self):
        pool, (s1, s2, s3) = _pool3()
        pool.on_quota_exhausted(s1)
        pool.on_quota_exhausted(s2)
        pool.on_quota_exhausted(s3)
        assert pool._upstream_event_active() is True
        # Temps écoulé : événement expiré + fenêtre vidée de la rafale.
        pool._upstream_event_until = time.monotonic() - 1.0
        pool._upstream_429_log.clear()
        assert pool._upstream_event_active() is False
        pool.on_quota_exhausted(s1)
        assert pool.rotated[-1] is s1  # rotation normale à nouveau

    def test_hot_window_redeclares_after_expiry(self):
        """Fenêtre encore chaude à l'expiration → le 429 suivant redéclare
        (l'amont rejette toujours : pas de rotation dans le mur)."""
        pool, (s1, s2, s3) = _pool3()
        pool.on_quota_exhausted(s1)
        pool.on_quota_exhausted(s2)
        pool.on_quota_exhausted(s3)
        pool._upstream_event_until = time.monotonic() - 1.0
        n = len(pool.rotated)
        pool.on_quota_exhausted(s1)
        assert pool._upstream_event_active() is True
        assert len(pool.rotated) == n

    def test_threshold_999_disables(self):
        """Rollback : upstream_event_threshold=999 → jamais d'événement."""
        pool, (s1, s2, s3) = _pool3()
        pool.update_config({"upstream_event_threshold": 999})
        pool.on_quota_exhausted(s1)
        pool.on_quota_exhausted(s2)
        pool.on_quota_exhausted(s3)
        assert pool._upstream_event_active() is False
        assert pool.rotated == [s1, s2, s3]

    def test_update_config_hot_reload(self):
        pool, _ = _pool3()
        pool.update_config({
            "upstream_event_threshold": 5,
            "upstream_event_window_s": 120,
            "upstream_event_cool_s": 300,
        })
        assert pool._upstream_event_threshold == 5
        assert pool._upstream_event_window_s == 120.0
        assert pool._upstream_event_cool_s == 300.0
        pool.update_config({"upstream_event_threshold": "n'importe quoi"})
        assert pool._upstream_event_threshold == 5  # invalide → inchangé

    def test_cooldown_still_applied_during_event(self):
        """Le gel ne concerne que la rotation : le cooldown C1 reste posé."""
        pool, (s1, s2, s3) = _pool3()
        pool.on_quota_exhausted(s1)
        pool.on_quota_exhausted(s2)
        pool.on_quota_exhausted(s3)
        assert pool._upstream_event_active() is True
        # s1 et s2 bad-markées (une autre restait utilisable à chaque fois).
        assert pool._per_station(s1)["bad_until"] is not None
        assert pool._per_station(s2)["bad_until"] is not None

    def test_last_servable_never_force_rotated_during_event(self):
        """Pendant l'événement, même la dernière-servable ne rotationne pas
        (elle continue de servir ; un 429 isolé hors événement garde le
        comportement historique : rotation en fond)."""
        pool, (s1, s2, s3) = _pool3()
        s2.status = "error"
        s3.status = "error"
        pool.on_quota_exhausted(s1)  # seule servable, pas d'événement
        assert pool.rotated == [s1]  # historique : rotation en fond
        # Force un événement via 3 couples distincts (s1 change d'IP).
        pool._upstream_429_log.clear()
        pool._upstream_event_until = 0.0
        s1.current_ip = "9.9.9.1"
        pool.on_quota_exhausted(s2)  # error mais loggé quand même
        s1.current_ip = "9.9.9.2"
        pool.on_quota_exhausted(s3)
        s1.current_ip = "9.9.9.3"
        pool.on_quota_exhausted(s1)
        assert pool._upstream_event_active() is True
        n = len(pool.rotated)
        pool.on_quota_exhausted(s1)  # dernière-servable, en événement
        assert len(pool.rotated) == n  # aucune rotation forcée
        assert pool._station_usable(s1, exclude_approaching=False) is True

    # ── [fiab 09/09 phase 2] multi-causes : timeouts + AUTH ──────────

    def test_mixed_429_timeout_auth_declares_event(self):
        """Une vague MIXTE (429 + timeout + AUTH sur couples distincts)
        déclare comme une vague homogène — c'est le cas réel d'un throttle
        amont (storm auto-infligé : les retries timeoutent ET les AUTH
        suivants sont refusés)."""
        pool, (s1, s2, s3) = _pool3()
        pool._note_upstream_429(s1)
        assert pool._upstream_event_active() is False
        pool._note_upstream_timeout(s2)
        assert pool._upstream_event_active() is False
        pool._note_upstream_auth(s3)  # 3ᵉ couple distinct, 3ᵉ cause → événement
        assert pool._upstream_event_active() is True

    @pytest.mark.asyncio
    async def test_disconnect_retry_feeds_breaker_and_freezes_when_declared(self):
        """on_disconnect_retry alimente le log timeout (vrai même hors
        événement : le bad-mark + la rotation URGENTE partent normalement),
        et gèle la rotation dès que CET appel déclare l'événement."""
        pool, (s1, s2, s3) = _pool3()
        # 1er couple distinct → pas d'événement, rotation URGENTE normale.
        await pool.on_disconnect_retry(s1)
        assert pool._upstream_event_active() is False
        assert pool.rotated == [s1]
        # 2ᵉ couple distinct → toujours pas d'événement (seuil 3).
        await pool.on_disconnect_retry(s2)
        assert pool._upstream_event_active() is False
        n = len(pool.rotated)
        # 3ᵉ couple distinct → CET appel déclare → gel de SA rotation.
        await pool.on_disconnect_retry(s3)
        assert pool._upstream_event_active() is True
        assert len(pool.rotated) == n  # s3 gelée, pas de rotation neuve

    def test_notify_genuine_failure_feeds_timeout(self):
        """notify_connection_failure n'alimente que sur bad-mark genuine
        (ni late-signal absorbé ni refresh d'ancre) — ici s1 échoue pour de
        vrai (autre station utilisable, session hors grâce) → 1 signal."""
        pool, (s1, s2, s3) = _pool3()
        pool.notify_connection_failure(s1)
        assert len(pool._upstream_429_log) == 1
        assert pool._upstream_429_log[0][3] == "timeout"
        assert pool._upstream_event_active() is False

    def test_notify_late_signal_does_not_feed(self):
        """Un late-signal (session_start < grâce 20 s, IP inchangée) est
        absorbé sans bad-mark → AUCUN signal timeout (pas de faux corrélé)."""
        pool, (s1, s2, s3) = _pool3()
        per = pool._per_station(s1)
        per["last_confirmed_ip"] = s1.current_ip
        per["session_start"] = time.monotonic()  # frais → dans la grâce
        pool.notify_connection_failure(s1)
        assert len(pool._upstream_429_log) == 0

    def test_launch_rotation_central_gel_blocks_all_producers(self):
        """Le gel centralisé dans _launch_rotation (réel, pas stubé) bloque
        TOUS les producteurs pendant l'événement — y compris un appel direct
        (re-queue post-commit, futur producteur)."""
        pool, (s1, s2, s3) = _pool3()
        pool._note_upstream_429(s1)
        pool._note_upstream_timeout(s2)
        pool._note_upstream_auth(s3)
        assert pool._upstream_event_active() is True
        # Dé-stubbe : le vrai _launch_rotation doit refuser pendant l'event.
        # _ensure_workers créerait des workers orphelins (coroutine jamais
        # attendue) — on le neutralise : le gel est AVANT, c'est lui qu'on
        # teste ici, pas la file.
        del pool._launch_rotation
        pool._ensure_workers = lambda: None
        n_pending = len(pool._pending)
        pool._launch_rotation(s1)
        assert len(pool._pending) == n_pending  # rien n'a été filé
        # Hors événement : le chemin réel re-file (pending +1, queue
        # alimentée — _ensure_workers neutralisé, pas de worker créé).
        pool._upstream_event_until = time.monotonic() - 1.0
        pool._launch_rotation(s1)
        assert len(pool._pending) == n_pending + 1
        # Nettoie la file pour ne pas laisser d'entrée orpheline.
        try:
            while True:
                pool._rotation_queue.get_nowait()
        except Exception:
            pass
        pool._pending.discard(s1._station)
