from free import ip_state
from free import wire_admission as wa


def _st(state, fp="fp"):
    return ip_state.IPState(provider="nordvpn", ip="8.8.8.8", state=state, wire_fp=fp)


def test_proven_same_fingerprint_routes():
    decision = wa.admit("fp", _st(ip_state.PROVEN))
    assert decision.allowed and decision.action == "route"


def test_proven_different_fingerprint_is_refused():
    decision = wa.admit("other", _st(ip_state.PROVEN))
    assert not decision.allowed and decision.action == "refuse"
    assert "mismatch" in decision.reason


def test_compat_never_targets_a_proven_bucket():
    assert not wa.admit("fp", _st(ip_state.SERVING), mode=wa.MODE_COMPAT).allowed
    assert wa.admit("fp", _st(ip_state.UNPROVEN), mode=wa.MODE_COMPAT).allowed


def test_observe_mode_never_blocks():
    decision = wa.admit("other", _st(ip_state.PROVEN), mode=wa.MODE_OBSERVE)
    assert decision.allowed and decision.action == "observe"
    assert "observe" in decision.reason


def test_spent_and_burned_are_refused():
    assert not wa.admit("fp", _st(ip_state.SPENT)).allowed
    assert not wa.admit("fp", _st(ip_state.BURNED)).allowed


def test_admit_many_picks_the_first_allowed():
    candidates = [_st(ip_state.SPENT), _st(ip_state.PROVEN, fp="other"), _st(ip_state.PROVEN)]
    picked = wa.admit_many("fp", candidates)
    assert picked is candidates[2]
