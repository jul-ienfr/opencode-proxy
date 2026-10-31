"""[audit 2026-10-30] Quotas — régressions P0 verrouillées.

Le défaut principal : ``_per_station`` remettait le compteur à zéro à chaque
appel quand l'IP courante différait de l'ancre, sans jamais mettre l'ancre à
jour. Le seuil proactif n'était donc jamais atteint et un 429 pouvait être
effacé par une simple lecture ``/api/vpn-status``.
"""

import time

from free_ip_pool import FreeIPPool


class _Station:
    def __init__(self, sid=1, ip="1.1.1.1", *, enabled=True, status="connected"):
        self._station = sid
        self.enabled = enabled
        self.proxy_mode = "vpn"
        self.status = status
        self._quota_per_ip = 500
        self.current_ip = ip
        self.current_server = {"name": "se-1"}
        self.socks5_url = f"socks5://127.0.0.1:1{sid}080"
        self._last_rotation_error = None

    def note_free_request(self):
        pass

    def arm_egress_watchdog(self):
        pass

    def get_status(self):
        return {"station": self._station, "status": self.status}


def _pool(st):
    pool = FreeIPPool(st)
    pool._bad_ttl = 60.0
    pool._launch_rotation = lambda *a, **k: None
    return pool


def test_ip_change_resets_once_and_reanchors():
    st = _Station(ip="1.1.1.1")
    pool = _pool(st)
    per = pool._per_station(st)
    per["request_count"] = 42
    per["bad_until"] = time.monotonic() + 100
    per["bad_cause"] = "rate_limit"
    per["last_confirmed_ip"] = "1.1.1.1"

    # The manager repairs/rotates the tunnel outside pool.switch_ip().
    st.current_ip = "2.2.2.2"
    per2 = pool._per_station(st)
    assert per2["request_count"] == 0
    assert per2["bad_until"] is None
    assert per2["bad_cause"] is None
    assert per2["last_confirmed_ip"] == "2.2.2.2"

    # Subsequent calls must NOT reset the fresh bucket again.
    per2["request_count"] = 5
    per3 = pool._per_station(st)
    assert per3["request_count"] == 5
    assert per3["last_confirmed_ip"] == "2.2.2.2"


def test_legacy_state_without_anchor_is_not_attributed_to_current_ip():
    st = _Station(ip="3.3.3.3")
    pool = _pool(st)
    pool._per[1] = {
        "request_count": 499,
        "session_start": None,
        "last_confirmed_ip": None,
        "ip_stats": {},
    }
    per = pool._per_station(st)
    assert per["request_count"] == 0
    assert per["last_confirmed_ip"] == "3.3.3.3"
