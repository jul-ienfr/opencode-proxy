"""T021 — les credentials des proxys authentifies survivent jusqu'a l'URL.

[P0 2026-11-20] `new_option` ne recevait que host/port et `rows_from_options`
reconstruisait `scheme://host:port` : un candidat
`http://user:pa55@1.2.3.4:8080` (credentials conserves par
`normalize_proxy`, free/egress_sources.py:113-116) perdait son
authentification, pouvait etre promu `usable` puis echouait en **407** a
chaque usage reel. Ces tests verrouillent la chaine
candidat -> option -> URL -> worker.
"""

from __future__ import annotations

from free.egress_catalog import Provider, credentials_from_url, new_option
from free.egress_rotation import rows_from_options
from free.egress_sources import normalize_proxy
from free.public_egress_worker import Upstream


def _option_from_candidate(candidate: dict, **changes):
    base = {
        "provider": Provider.PUBLIC,
        "kind": candidate["scheme"],
        "host": candidate["host"],
        "port": candidate["port"],
        "egress_ip": candidate["host"],
        "url": candidate["url"],
    }
    base.update(changes)
    return new_option(**base)


def test_candidate_with_credentials_produces_a_url_that_carries_them():
    """Le critere d'acceptation T021 : candidat authentifie -> URL authentifiee.

    [P1 2026-11-20] T137 — le schema est ``https`` : le sujet ici est la
    PROPAGATION des credentials (candidat -> option -> URL -> worker). Un
    ``http://`` authentifie serait refuse par la politique de clair, ce qui
    testerait T137 au lieu de T021.
    """

    candidate = normalize_proxy("https://user:pa55@1.2.3.4:8080")
    assert candidate["url"] == "https://user:pa55@1.2.3.4:8080"
    option = _option_from_candidate(candidate)
    assert (option.username, option.password) == ("user", "pa55")
    assert rows_from_options([option])[0]["url"] == "https://user:pa55@1.2.3.4:8080"


def test_explicit_username_password_fields_win_over_the_url():
    option = new_option(
        provider=Provider.PUBLIC,
        kind="https",
        host="5.6.7.8",
        port=443,
        egress_ip="5.6.7.8",
        username="alice",
        password="s3cr3t",
    )
    assert rows_from_options([option])[0]["url"] == "https://alice:s3cr3t@5.6.7.8:443"


def test_without_credentials_the_url_is_unchanged():
    """Non-regression : le chemin anonyme garde exactement l'ancienne URL."""

    option = new_option(
        provider=Provider.PUBLIC,
        kind="https",
        host="5.6.7.8",
        port=443,
        egress_ip="5.6.7.8",
    )
    assert option.username == "" and option.password == ""
    assert rows_from_options([option])[0]["url"] == "https://5.6.7.8:443"


def test_special_characters_round_trip_byte_identical_to_the_candidate():
    """La row du pool doit rester identique au candidat sonde.

    Le parseur re-encode deja les caracteres speciaux (comportement
    pre-existant) ; notre couche ne doit ni perdre ni encoder une seconde
    fois : le worker doit parser la MEME identite depuis les deux chemins.
    """

    candidate = normalize_proxy("socks5://us%40er:p%3Ass@9.9.9.9:1080")
    row = rows_from_options([_option_from_candidate(candidate)])[0]
    assert row["url"] == candidate["url"]
    assert Upstream.parse("p0", row["url"]).username == Upstream.parse("p0", candidate["url"]).username


def test_ipv6_host_is_bracketed_with_credentials():
    option = new_option(
        provider=Provider.PUBLIC,
        kind="https",
        host="2001:db8::1",
        port=8443,
        egress_ip="2001:db8::1",
        username="u",
        password="p",
    )
    assert rows_from_options([option])[0]["url"] == "https://u:p@[2001:db8::1]:8443"


def test_worker_rebuilds_proxy_authorization_from_the_row():
    """Preuve que la chaine aboutit : le worker sait les porter."""

    candidate = normalize_proxy("https://user:pa55@1.2.3.4:8080")
    row = rows_from_options([_option_from_candidate(candidate)])[0]
    upstream = Upstream.parse("p0", row["url"])
    assert upstream.username == "user"
    assert upstream.auth_header == "Basic dXNlcjpwYTU1"


def test_option_repr_never_publishes_the_password():
    option = new_option(
        provider=Provider.PUBLIC,
        kind="https",
        host="5.6.7.8",
        port=443,
        egress_ip="5.6.7.8",
        username="alice",
        password="s3cr3t",
    )
    assert "s3cr3t" not in repr(option)
    assert "alice" not in repr(option)


def test_credentials_from_url_does_not_invent_credentials():
    assert credentials_from_url("http://1.2.3.4:8080") == ("", "")
    assert credentials_from_url("") == ("", "")
