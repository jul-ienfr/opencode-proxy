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
    pool._launch_rotation = lambda station, forced_pool=None: pool.rotated.append(station)
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
