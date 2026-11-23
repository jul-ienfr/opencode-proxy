from free.probe_budget import ProbeBudget


def test_probe_budget_cooldown_and_cap(tmp_path):
    budget = ProbeBudget(tmp_path / "r.db", per_ip=2, cooldown_s=600.0)
    assert budget.allow("nordvpn", "8.8.8.8", now=1000.0).allowed
    assert budget.record_probe("nordvpn", "8.8.8.8", now=1000.0) == 1
    assert not budget.allow("nordvpn", "8.8.8.8", now=1100.0).allowed
    assert budget.allow("nordvpn", "8.8.8.8", now=1700.0).allowed
    budget.record_probe("nordvpn", "8.8.8.8", now=1700.0)
    assert not budget.allow("nordvpn", "8.8.8.8", now=2500.0).allowed


def test_probe_budget_window_resets(tmp_path):
    budget = ProbeBudget(tmp_path / "r.db", per_ip=1, cooldown_s=0.0)
    budget.record_probe("warp", "2a09:bac1:27c0:2060::/64", now=1000.0)
    assert not budget.allow("warp", "2a09:bac1:27c0:2060::/64", now=2000.0).allowed
    assert budget.allow("warp", "2a09:bac1:27c0:2060::/64", now=1000.0 + 24 * 3600).allowed


def test_client_counter_is_separate(tmp_path):
    budget = ProbeBudget(tmp_path / "r.db")
    budget.record_client("nordvpn", "8.8.8.8", now=1000.0)
    assert budget.record_client("nordvpn", "8.8.8.8", now=1001.0) == 2
    assert budget.allow("nordvpn", "8.8.8.8", now=1002.0).allowed
    snap = budget.snapshot()
    assert snap[0]["probe_count"] == 0 and snap[0]["client_count"] == 2


def test_reset_clears_a_bucket(tmp_path):
    budget = ProbeBudget(tmp_path / "r.db", per_ip=1, cooldown_s=600.0)
    budget.record_probe("nordvpn", "8.8.8.8", now=1000.0)
    assert not budget.allow("nordvpn", "8.8.8.8", now=2000.0).allowed
    budget.reset("nordvpn", "8.8.8.8")
    assert budget.allow("nordvpn", "8.8.8.8", now=2000.0).allowed
