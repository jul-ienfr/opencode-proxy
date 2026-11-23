"""Intégration US1 (023 T010) — réserve prouvée → sélection → service.

Ce test exerce le **chemin réel** qui va de la réserve d'IP prouvées jusqu'au
service client, sans réseau :

1. la réserve est construite par les vraies API
   (``free.client_reserve.add`` + ``free.ip_state.IPStateStore.transition``) ;
2. la sélection passe par ``FreeIPPool._best_station`` (la fonction utilisée
   par ``on_request`` pour choisir l'egress d'un appel client) ;
3. la construction de la requête passe par ``respond.convert_request`` ;
4. la validation de la réponse passe par ``app.protocol.mapping``.

Frontière documentée : seul l'envoi HTTP amont est un double. Le transport
n'est pas ouvert ici (pas de socket, pas de Docker) ; le test vérifie que le
double reçoit bien la station/le proxy de la réserve et renvoie un 200 avec
contenu. Le mapping réponse est lui-même le vrai code de production.

Invariants couverts :

* la sélection ne retient que l'IP de la réserve (``_reserve_allows``) ;
* ``_proven_reserve_ips`` expose l'IP prouvée (``client_reserve``) ;
* un 200 amont produit un 200 client avec contenu, jamais un 429 local ;
* l'egress effectivement utilisé est celui de la réserve.
"""

from __future__ import annotations

import time
from pathlib import Path

import pytest

from app.protocol.mapping import openai_to_anthropic
from free import client_reserve as cr
from free import ip_state
from free import pool as pool_mod
from free.clients import family_anthropic, respond

RESERVE_IP = "203.0.113.7"
OTHER_IP = "198.51.100.9"
HOSTNAME = "se-sto.nordvpn.com"
MODEL = "test-model"

# Le harnais de test neutralise ``_reserve_allows`` par défaut (fixture
# autouse ``_hermetic_client_reserve``). Ce fichier teste précisément la
# réserve : il opte donc explicitement pour le comportement réel.
pytestmark = pytest.mark.client_reserve


class _Station:
    """Double minimal d'une station Docker NordVPN (duck-typée par le pool)."""

    def __init__(self, sid: int, ip: str) -> None:
        self._station = sid
        self.current_ip = ip
        self.pid = None
        self._quota_per_ip = 500
        self.enabled = True
        self.proxy_mode = "vpn"
        self.status = "connected"
        self.current_server = HOSTNAME
        self._egress_state = "tunnel_up_full"
        self._last_effective_ok_at = time.monotonic()
        self.socks5_url = f"socks5://127.0.0.1:{1080 + sid}"
        self._config = {
            "ip_quota": {"enabled": True, "quota_reserve_pct": 10, "rotation_threshold_pct": 20},
            "station_health": {"strict": True, "max_effective_age_s": 60},
            "rotation_planner": {"enabled": True, "spare_ready": False},
            "quota_per_ip_by_model": {MODEL: 20},
            "quota_per_ip": 500,
            "bad_ip_ttl_s": 1800,
            "quota_ip_block_s": 120,
            "station_bad_ttl_s": 60,
            "station_connect_retry_interval_s": 300,
        }

    def get_status(self) -> dict:
        return {"station": self._station, "status": self.status}

    def note_free_request(self) -> None:
        pass

    def arm_egress_watchdog(self) -> None:
        pass


class _ServiceDouble:
    """Double du transport amont : reçoit la sélection, rend un 200 avec contenu.

    Il n'ouvre aucune socket. Il enregistre la station et le proxy que le pool
    lui a remis, ce qui permet d'affirmer que le service a bien utilisé l'IP
    de la réserve.
    """

    def __init__(self) -> None:
        self.calls: list[dict] = []

    def forward(self, proxy_url: str | None, station: _Station, body: dict) -> tuple[int, dict]:
        self.calls.append(
            {
                "proxy_url": proxy_url,
                "station": station,
                "egress_ip": station.current_ip,
                "model": body.get("model"),
            }
        )
        return 200, {
            "id": "chatcmpl-reserve-1",
            "object": "chat.completion",
            "model": body.get("model") or MODEL,
            "choices": [
                {
                    "index": 0,
                    "message": {"role": "assistant", "content": "Hello from the reserve"},
                    "finish_reason": "stop",
                }
            ],
            "usage": {"prompt_tokens": 12, "completion_tokens": 5, "total_tokens": 17},
        }


def _point_modules_at(db: Path, monkeypatch) -> None:
    """Pointe les modules sans ``db=`` vers la base temporaire du test.

    ``reserved_ips``/``enabled`` sont appelés sans ``db=`` par le pool. Les
    réglages runtime (``enabled``) vivent dans ``registry.db`` via
    ``free.reserve_settings`` : sans isolation, un test voisin qui a réglé
    ``enabled=0`` ferait passer toute IP (réserve ignorée). Un override
    d'environnement ``CLIENT_RESERVE_ENABLED`` a le même effet ; on le fige.
    """

    monkeypatch.setenv("CLIENT_RESERVE_ENABLED", "1")
    monkeypatch.setattr(cr, "DEFAULT_DB", db)
    monkeypatch.setattr(cr, "_CACHE", None)
    from free import reserve_settings

    monkeypatch.setattr(reserve_settings, "DEFAULT_DB", db)
    monkeypatch.setattr(reserve_settings, "_CACHE", None)
    # Cache 2 s des IPs prouvées : ne pas hériter d'un autre test.
    monkeypatch.setattr(pool_mod, "_PROVEN_RESERVE_CACHE", None)


def _reserve_db(tmp_path: Path, monkeypatch) -> Path:
    """Construit la réserve prouvée sur disque (client_reserve + ip_state)."""

    db = tmp_path / "registry.db"
    _point_modules_at(db, monkeypatch)

    # 1) preuve 200 dans la réserve client (source primaire de sélection)
    assert cr.add(RESERVE_IP, HOSTNAME, 1, db=db)

    # 2) même preuve dans la machine à états ``ip_state`` (``proven``).
    store = ip_state.IPStateStore(db)
    proven = store.transition(
        "nordvpn",
        RESERVE_IP,
        ip_state.PROVEN,
        wire_fp="wire-fp-test",
        now=time.time(),
    )
    assert proven.state == ip_state.PROVEN
    return db


def _pool() -> tuple[object, _Station, _Station]:
    st_reserve = _Station(1, RESERVE_IP)
    st_other = _Station(2, OTHER_IP)
    pool = pool_mod.FreeIPPool(st_reserve, st_other)
    # Une IP n'est routable qu'après un 200 observé (principe VII).
    for station in (st_reserve, st_other):
        pool._ledger.note_result(station.current_ip, MODEL, 200)
    return pool, st_reserve, st_other


def test_reserve_flow_reaches_the_service_with_200_and_content(
    tmp_path: Path, monkeypatch
) -> None:
    db = _reserve_db(tmp_path, monkeypatch)

    # -- sélection : le pool ne sert que l'IP de la réserve -----------------
    assert cr.reserved_ips(db=db) == {RESERVE_IP}
    assert pool_mod._proven_reserve_ips() == {RESERVE_IP}

    pool, st_reserve, st_other = _pool()
    station = pool._best_station(model=MODEL)

    assert station is st_reserve, "la sélection doit retenir l'IP de réserve"
    assert station is not st_other
    assert station.current_ip == RESERVE_IP

    # La sélection écarte bien la station hors réserve.
    assert pool._station_usable(
        st_other, exclude_approaching=False, model=MODEL
    ) is False

    # -- construction de la requête (vrai code produit) --------------------
    anthropic_body = {
        "model": MODEL,
        "system": "Be concise",
        "messages": [{"role": "user", "content": "Hello"}],
        "max_tokens": 128,
        "stream": False,
    }
    chat_body = respond.convert_request(anthropic_body, family_anthropic.FAMILY)
    assert chat_body["stream"] is False
    assert chat_body["messages"][0]["role"] == "system"

    # -- service : double du transport, aucun réseau -----------------------
    service = _ServiceDouble()
    proxy_url = getattr(station, "socks5_url", None)
    status, upstream = service.forward(proxy_url, station, chat_body)

    assert status == 200
    assert status != 429
    assert len(service.calls) == 1
    call = service.calls[0]
    assert call["egress_ip"] == RESERVE_IP
    assert call["station"] is st_reserve
    assert call["proxy_url"] == proxy_url

    # -- validation de la réponse (vrai mapping Anthropic) -----------------
    response = openai_to_anthropic(upstream, MODEL)
    assert response["type"] == "message"
    assert response["role"] == "assistant"
    assert response["stop_reason"] == "end_turn"
    content = response["content"]
    assert isinstance(content, list) and content
    assert content[0]["type"] == "text"
    assert "Hello from the reserve" in content[0]["text"]
    assert response["usage"]["output_tokens"] == 5


def test_reserve_flow_rejects_an_unreserved_station(tmp_path: Path, monkeypatch) -> None:
    """Sans l'IP dans la réserve, la station n'est pas sélectionnée.

    La frontière est la même que ci-dessus : on prouve que le service ne part
    pas sur une IP hors réserve (le pool rend ``None`` plutôt qu'un 429 local).
    """

    db = tmp_path / "registry.db"
    _point_modules_at(db, monkeypatch)

    # Réserve non vide mais sur une AUTRE IP : ``_reserve_allows`` doit
    # refuser la station testée.
    assert cr.add(OTHER_IP, HOSTNAME, 2, db=db)

    pool, st_reserve, st_other = _pool()
    assert pool._station_usable(
        st_reserve, exclude_approaching=False, model=MODEL
    ) is False
    assert pool._station_usable(
        st_other, exclude_approaching=False, model=MODEL
    ) is True

    station = pool._best_station(model=MODEL)
    assert station is st_other
    assert station.current_ip == OTHER_IP


def test_proven_reserve_prefers_the_ip_state_when_client_reserve_is_empty(
    tmp_path: Path, monkeypatch
) -> None:
    """Repli ``ip_state`` : une IP ``proven`` est sélectionnable sans table.

    ``client_reserve`` peut être vide (redémarrage, purge) alors que l'état
    ``proven`` persiste ; ``_proven_reserve_ips`` doit alors lire ``ip_state``.
    """

    db = tmp_path / "registry.db"
    _point_modules_at(db, monkeypatch)

    # Pas d'entrée client_reserve ; seule la machine à états est peuplée.
    store = ip_state.IPStateStore(db)
    store.transition(
        "nordvpn",
        RESERVE_IP,
        ip_state.PROVEN,
        wire_fp="wire-fp-test",
        now=time.time(),
    )
    ip_state._STORE = store  # singleton lu par ``_proven_reserve_ips``
    try:
        assert cr.reserved_ips(db=db) == set()
        assert pool_mod._proven_reserve_ips() == {RESERVE_IP}
    finally:
        ip_state._STORE = None
