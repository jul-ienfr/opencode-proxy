"""Per-IP OpenCode instance vault (free.identities + opencode wiring).

1 IP = 1 installation : UA versionnée, ses_/msg_/project isolés, pool TLS
isolé par IP. Non-régression du repli global (egress_ip=None).
"""

import re

import pytest

import opencode as oc
from free.identities import KNOWN_CLIENT_VERSIONS, IdentityVault


@pytest.fixture(autouse=True)
def _reset_identity_singletons(monkeypatch):
    """Isole le vault global entre les tests (sinon les IpIdentity cachées
    d'un test précédent — avec leurs chemins de fichiers — fuiraient)."""
    import free.identities as _fi

    _fi._VAULT = None
    oc._FREE_SESSION_CACHE = None
    oc._FREE_SESSION_TS = 0.0
    yield
    _fi._VAULT = None
    oc._FREE_SESSION_CACHE = None
    oc._FREE_SESSION_TS = 0.0


def _isolated_vault(tmp_path, monkeypatch):
    monkeypatch.setenv("OPENCODE_IDENTITIES_DIR", str(tmp_path / "identities"))
    return IdentityVault(base_project="a24ad984335289d5bb037e290b55f7fb5e04ce6b")


def test_vault_isolates_sessions_per_ip(tmp_path, monkeypatch):
    monkeypatch.setenv("PER_IP_IDENTITY", "1")
    v = _isolated_vault(tmp_path, monkeypatch)
    a = v.session_for("1.1.1.1", None)
    b = v.session_for("2.2.2.2", None)
    assert re.fullmatch(r"ses_[0-9a-f]{12}[0-9A-Za-z]{14}", a)
    assert re.fullmatch(r"ses_[0-9a-f]{12}[0-9A-Za-z]{14}", b)
    assert a != b
    # stabilité par IP
    assert v.session_for("1.1.1.1", None) == a
    # conversation scopée à l'IP : même clé → ses différents
    assert v.session_for("1.1.1.1", "K") != v.session_for("2.2.2.2", "K")
    assert v.session_for("1.1.1.1", "K") == v.session_for("1.1.1.1", "K")


def test_vault_msg_fresh_per_send(tmp_path, monkeypatch):
    v = _isolated_vault(tmp_path, monkeypatch)
    m1 = v.new_msg("1.1.1.1")
    m2 = v.new_msg("1.1.1.1")
    m3 = v.new_msg("2.2.2.2")
    for m in (m1, m2, m3):
        assert re.fullmatch(r"msg_[0-9a-f]{12}[0-9A-Za-z]{14}", m), m
    assert len({m1, m2, m3}) == 3


def test_vault_version_client_in_measured_pool(tmp_path, monkeypatch):
    v = _isolated_vault(tmp_path, monkeypatch)
    for ip in ("86.104.249.192", "186.247.163.125", "9.9.9.9"):
        ident = v.get(ip)
        assert ident.version in KNOWN_CLIENT_VERSIONS
        assert ident.client in ("cli", "desktop")
        assert ident.project == "global" or len(ident.project) >= 8
        assert "opencode/%s" % ident.version in ident.ua()
    # distribution : les 2 versions et 2 clients sont représentés sur 20 IPs
    ips = [f"10.9.9.{i}" for i in range(20)]
    assert {v.get(i).version for i in ips} <= set(KNOWN_CLIENT_VERSIONS)
    assert len({v.get(i).version for i in ips}) >= 1


def test_official_headers_differ_per_ip(monkeypatch, tmp_path):
    monkeypatch.setenv("PER_IP_IDENTITY", "1")
    monkeypatch.setenv("OPENCODE_IDENTITIES_DIR", str(tmp_path / "identities"))
    # 86.104.249.192 → 1.19.3 (trio), 186.247.163.125 → 1.18.31 (quartet) :
    # distribution mesurée du vault (cf. test_vault_version_client...).
    h19 = oc._official_free_headers("https://opencode.ai/inference/openai/v1/chat/completions", None, "86.104.249.192")
    h18 = oc._official_free_headers("https://opencode.ai/inference/openai/v1/chat/completions", None, "186.247.163.125")
    h19b = oc._official_free_headers("https://opencode.ai/inference/openai/v1/chat/completions", None, "86.104.249.192")
    assert "opencode/1.19.3" in h19["User-Agent"]
    assert "opencode/1.18.31" in h18["User-Agent"]
    # [wire exact provider opencode, capture mitm 2026-11-01]
    # 1.19.3+ → quartet + x-opencode-session-id ; 1.18.31 → quartet sans.
    # Aucun x-session-affinity/x-session-id sur le provider opencode.
    assert "x-opencode-session-id" in h19
    assert "x-opencode-session-id" not in h18
    for _h in ("x-opencode-client", "x-opencode-project", "x-opencode-request", "x-opencode-session"):
        assert _h in h18, f"{_h} absent du wire 1.18.31 (client incomplet)"
        assert _h in h19, f"{_h} absent du wire 1.19.3 (client incomplet)"
    assert "x-session-affinity" not in h19 and "x-session-id" not in h19
    assert h19["x-opencode-session"] == h19["x-opencode-session-id"]
    # Même IP = même installation (ses stable), IPs distinctes = ses distincts.
    assert h19b["x-opencode-session-id"] == h19["x-opencode-session-id"]
    assert h18["x-opencode-session"] != h19["x-opencode-session-id"]


def test_installation_anchor_is_per_ip(monkeypatch, tmp_path):
    """Le provider `opencode` ne reçoit PAS x-opencode-directory/workspace :
    la capture live 1.18.31/1.19.3/1.19.5 n'émet que le quartet d'identité.
    L'ancrage local est réservé aux appels API locaux, pas à l'inférence."""
    monkeypatch.setenv("PER_IP_IDENTITY", "1")
    monkeypatch.setenv("OPENCODE_IDENTITIES_DIR", str(tmp_path / "identities"))
    seen = {"project": set(), "session": set()}
    for ip in ("186.247.163.125", "187.13.8.129", "86.104.21.99"):
        h = oc._official_free_headers("https://opencode.ai/inference/openai/v1/chat/completions", None, ip)
        assert "x-opencode-directory" not in h
        assert "x-opencode-workspace" not in h
        project = h.get("x-opencode-project", "")
        session = h.get("x-opencode-session", "")
        assert project and session, f"identité incomplète pour {ip}"
        assert project not in seen["project"], f"project partagé entre IPs : {project}"
        assert session not in seen["session"], f"session partagée entre IPs : {session}"
        seen["project"].add(project)
        seen["session"].add(session)
    # Hors chemin per-IP (sondes/tests) : aucun ancrage inventé.
    h0 = oc._official_free_headers("https://opencode.ai/inference/openai/v1/chat/completions", None)
    assert "x-opencode-directory" not in h0
    assert "x-opencode-workspace" not in h0


def test_proxy_headers_use_exact_1195_generator(monkeypatch, tmp_path):
    """Le proxy et le générateur partagent la même primitive : headers 1.19.5
    exacts (quartet + session-id, Bun 1.4.4, zéro header hors client)."""
    import free.identities as fi

    monkeypatch.setenv("PER_IP_IDENTITY", "1")
    vault = fi.IdentityVault(base_project="global", forced_client="desktop", forced_version="1.19.5")
    monkeypatch.setattr(oc, "_ip_vault", lambda: vault)
    ip = "10.99.0.55"
    h = oc._official_free_headers("https://opencode.ai/inference/openai/v1/chat/completions", None, ip)
    assert list(h.keys()) == [
        "Authorization",
        "Content-Type",
        "User-Agent",
        "x-opencode-client",
        "x-opencode-project",
        "x-opencode-request",
        "x-opencode-session",
        "x-opencode-session-id",
        "Connection",
        "Accept",
        "Host",
        "Accept-Encoding",
    ]
    assert h["User-Agent"] == "opencode/1.19.5 ai-sdk/provider-utils/4.0.23 runtime/bun/1.4.4"
    assert h["x-opencode-client"] == "desktop"
    assert h["x-opencode-session"] == h["x-opencode-session-id"]
    assert "x-session-affinity" not in h
    assert "x-session-id" not in h
    assert "x-opencode-directory" not in h
    assert "x-opencode-workspace" not in h


def test_each_ip_gets_full_distinct_client(monkeypatch, tmp_path):
    """Le provider ne doit pas pouvoir relier deux IPs : chaque client complet
    (version + client + project + request + session) est propre à son IP."""
    monkeypatch.setenv("PER_IP_IDENTITY", "1")
    monkeypatch.setenv("OPENCODE_IDENTITIES_DIR", str(tmp_path / "identities"))
    ips = ["186.247.163.125", "187.13.8.129"]  # 1.18.31 : quartet visible
    seen = {"project": set(), "request": set(), "session": set()}
    for ip in ips:
        h = oc._official_free_headers("https://opencode.ai/inference/openai/v1/chat/completions", None, ip)
        for key in seen:
            col = {"project": "x-opencode-project", "request": "x-opencode-request", "session": "x-opencode-session"}[
                key
            ]
            val = h.get(col, "")
            assert val, f"{col} vide pour {ip} — client amputé"
            assert val not in seen[key], f"{col} partagé entre IPs : {val!r}"
            seen[key].add(val)


def test_global_fallback_unchanged(monkeypatch, tmp_path):
    monkeypatch.setenv("PER_IP_IDENTITY", "1")
    monkeypatch.setenv("OPENCODE_IDENTITIES_DIR", str(tmp_path / "identities"))
    monkeypatch.setattr(oc, "_FREE_SESSION_FILE", str(tmp_path / "_free_session_id"))
    oc._FREE_SESSION_CACHE = None
    oc._FREE_SESSION_TS = 0.0
    h = oc._official_free_headers("https://opencode.ai/inference/openai/v1/chat/completions", None)
    assert h["User-Agent"] == oc._OPENCODE_OFFICIAL_UA
