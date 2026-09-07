"""[plan-perf Lot 1] Clients HTTP partagés par rôle — réutilisation + rebuild.

Contrats (opencode._role_client) :
- même instance retournée pour un même rôle (pas de handshake/pool par appel) ;
- rôles isolés (direct != tunnel) ;
- rebuild du client tunnel quand l'URL SOCKS change (rotation NordVPN),
  détecté par comparaison string à chaque acquire ;
- aucun I/O réseau dans ces tests (construction seule, jamais de send).

Never touches the live system: pas de requête, pas de VPN, pas de DB.
"""

import opencode as oc


def _snapshot():
    return dict(oc._role_clients)


def _restore(saved):
    oc._role_clients.clear()
    oc._role_clients.update(saved)


def test_direct_client_reused():
    """Deux acquires direct -> le MÊME objet (connexion réutilisée)."""
    saved = _snapshot()
    try:
        assert oc._role_client("direct") is oc._role_client("direct")
    finally:
        _restore(saved)


def test_roles_isolated(monkeypatch):
    """direct et tunnel sont deux clients distincts (transports séparés)."""
    saved = _snapshot()
    monkeypatch.setattr(oc, "_role_tunnel_url", lambda: "socks5://tunnel:1080")
    try:
        assert oc._role_client("direct") is not oc._role_client("tunnel")
    finally:
        _restore(saved)


def test_tunnel_rebuild_on_socks_change(monkeypatch):
    """Changement d'URL SOCKS -> nouveau client (l'ancien n'est plus servi)."""
    saved = _snapshot()
    monkeypatch.setattr(oc, "_role_tunnel_url", lambda: "socks5://a:1080")
    try:
        c1 = oc._role_client("tunnel")
        assert oc._role_client("tunnel") is c1
        monkeypatch.setattr(oc, "_role_tunnel_url", lambda: "socks5://b:1080")
        c2 = oc._role_client("tunnel")
        assert c2 is not c1
        assert oc._role_client("tunnel") is c2
    finally:
        _restore(saved)


def test_web_fetch_uses_role_client(monkeypatch):
    """_execute_web_fetch passe par _role_client (pas de AsyncClient inline)."""
    import asyncio

    seen = {}

    class FakeSharedClient:
        async def get(self, url, headers=None, follow_redirects=False, timeout=None):
            seen["follow_redirects"] = follow_redirects
            seen["timeout"] = timeout

            class R:
                status_code = 200
                headers = {"content-type": "text/plain", "content-length": "2"}
                content = b"ok"
                text = "ok"

                def raise_for_status(self):
                    pass

            return R()

    def fake_role(role="direct"):
        seen["role"] = role
        return FakeSharedClient()

    async def _safe(url):
        return True

    monkeypatch.setattr(oc, "_role_client", fake_role)
    monkeypatch.setattr(oc, "_is_safe_fetch_url", _safe)
    out = asyncio.run(oc._execute_web_fetch("https://example.com", "", timeout=7, via_vpn=False))
    assert seen["role"] == "direct"
    assert seen["follow_redirects"] is False
    assert seen["timeout"] == 7
    assert out.startswith("Content of https://example.com")
