"""[audit 2026-10-30] Sécurité des credentials VPN et du diagnostic.

Deux régressions P0 verrouillées ici :

1. ``_write_credentials_env`` réécrivait ``credentials.env`` avec seulement
   ``OPENVPN_USER``/``OPENVPN_PASSWORD``. Le fichier porte aussi la clé du
   control-server gluetun, son rôle JSON et le token NordVPN : les effacer
   faisait boucler les conteneurs en 401/unhealthy après chaque recréation.
2. ``/api/vpn/diagnostic`` renvoyait le ``docker compose config`` brut, donc
   les clés API, cookies et tokens en clair sur le LAN.
"""

from dashboard.api import _redact_compose_text, _write_credentials_env


def test_write_credentials_preserves_unrelated_keys(tmp_path):
    path = tmp_path / "credentials.env"
    path.write_text(
        "# managed by gluetun\n"
        "OPENVPN_USER=old-user\n"
        "OPENVPN_PASSWORD=old-pass\n"
        "VPN_CONTROL_API_KEY=control-secret\n"
        "HTTP_CONTROL_SERVER_AUTH_DEFAULT_ROLE={\"auth\":\"apikey\",\"apikey\":\"control-secret\"}\n"
        "NORDVPN_TOKEN=nord-token\n",
        encoding="utf-8",
    )

    _write_credentials_env("new-user", "new-pass", path=str(path))

    text = path.read_text(encoding="utf-8")
    assert "OPENVPN_USER=new-user" in text
    assert "OPENVPN_PASSWORD=new-pass" in text
    assert "VPN_CONTROL_API_KEY=control-secret" in text
    assert "HTTP_CONTROL_SERVER_AUTH_DEFAULT_ROLE=" in text
    assert "NORDVPN_TOKEN=nord-token" in text
    assert "# managed by gluetun" in text
    assert "old-user" not in text and "old-pass" not in text


def test_write_credentials_appends_missing_keys(tmp_path):
    path = tmp_path / "credentials.env"
    path.write_text("NORDVPN_TOKEN=nord-token\n", encoding="utf-8")

    _write_credentials_env("u", "p", path=str(path))

    lines = path.read_text(encoding="utf-8").splitlines()
    assert "OPENVPN_USER=u" in lines
    assert "OPENVPN_PASSWORD=p" in lines
    assert "NORDVPN_TOKEN=nord-token" in lines


def test_write_credentials_rejects_line_break_injection(tmp_path):
    path = tmp_path / "credentials.env"
    path.write_text("NORDVPN_TOKEN=nord-token\n", encoding="utf-8")

    try:
        _write_credentials_env("u\nINJECTED=1", "p", path=str(path))
    except ValueError:
        pass
    else:
        raise AssertionError("une valeur avec saut de ligne doit être refusée")

    assert "INJECTED" not in path.read_text(encoding="utf-8")


def test_compose_diagnostic_redacts_secrets():
    raw = """
services:
  opencode-proxy:
    environment:
      OPENCODE_API_KEY: sk-super-secret
      OPENCODE_GO_AUTH_COOKIE: Fe26.2**cookie
      NORMAL_SETTING: keep-me
  vpn-gluetun:
    environment:
      OPENVPN_USER: nord-user
      OPENVPN_PASSWORD: nord-pass
      NORDVPN_TOKEN: nord-token
      HTTP_CONTROL_SERVER_AUTH_DEFAULT_ROLE: '{"auth":"apikey","apikey":"control"}'
      VPN_CONTROL_API_KEY: control
      SERVER_COUNTRIES: Germany,Netherlands
"""

    out = _redact_compose_text(raw)

    for secret in (
        "sk-super-secret",
        "Fe26.2**cookie",
        "nord-user",
        "nord-pass",
        "nord-token",
        "control",
    ):
        assert secret not in out, f"secret non masqué: {secret}"
    assert "OPENCODE_API_KEY" in out
    assert "NORDVPN_TOKEN" in out
    assert "keep-me" in out
    assert "Germany,Netherlands" in out
    assert out.count("***REDACTED***") >= 7
