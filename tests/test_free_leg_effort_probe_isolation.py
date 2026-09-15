"""test_free_leg_effort_probe_isolation.py — la sonde d'effort de la jambe free
ne doit porter QUE la décision payante.

DÉFAUT VISÉ (``_try_free_model_first``, ``opencode.py`` ~6300) — le recalcul
d'effort free construisait sa sonde par **recopie du corps entier** ::

    _free_effort_body = dict(body)                    # forme FAUTIVE
    _free_effort_body["reasoning_effort"] = _paid_effort

puis la passait à ``_resolve_effort(_free_effort_body, free_model)``.

MOTIF MESURÉ — ``config.effort_policy._EXPLICIT_LEVEL_FIELDS`` vaut
``("output_config.effort", "effort", "reasoning_effort", "reasoning.effort")`` :
``resolve_effort`` donne la **priorité** à ``effort`` (et à
``output_config.effort``) sur ``reasoning_effort``. Mesures sur la config réelle ::

    resolve_effort({"reasoning_effort": "high"}, "deepseek-v4-flash")
        -> level='high'
    resolve_effort({"reasoning_effort": "high", "effort": "max"}, "deepseek-v4-flash")
        -> level='max'          # la forme plus prioritaire l'emporte

Conséquence : dès qu'un client envoie ``effort`` (forme historique top-level,
SANS ``reasoning_effort``), le handler ``chat_completions`` (P3) résout la
décision payante, la **plafonne** au plafond du modèle payant, et l'écrit dans
``body["reasoning_effort"]`` — mais le ``effort`` **brut du client** reste dans
le corps. Avec l'ancienne sonde, ce ``effort`` brut l'emportait sur la décision
payante déjà plafonnée et faisait donc **REMONTER** le niveau au-dessus de cette
décision : la garantie annoncée (« la décision free ne peut que rabaisser »)
était fausse, elle n'était masquée que par le fait qu'aucun couple de modèles
vivant n'a un plafond free supérieur au payant.

Ce témoin vise exactement cette levée de masque : il construit un couple
payant/free à **plafonds réellement divergents** (``deepseek-v4-flash`` plafond
``max`` → sa décision vaut ``high`` ; ``deepseek-v4-flash-free`` plafond déclaré
``max``, donc STRICTEMENT supérieur à la décision payante) et asserte le
``reasoning_effort`` **réellement remis à la couche réseau** pour l'amont free.

Harnais : identique à ``tests/test_free_leg_effort_recompute.py`` (même fixture
``free_env``, même double réseau ``_Seam`` sur ``_do_free_request_curl_cffi``,
même chemin de clés payantes indisponibles qui active le free-first réel). Aucun
espion sur la forme d'un argument : la seule chose asserte est le corps wire.

Vocabulaire : TESTÉ = un test qui échoue sans le correctif. Les faits seulement
raisonnés sont marqués DÉDUIT.
"""

import json
from typing import Any

import pytest

import opencode as oc

#: Couple vivant : ``FREE_MODEL_MAP["deepseek-v4-flash"] == "deepseek-v4-flash-free"``
#: et les deux modèles parlent Chat (endpoint ``/v1/chat/completions``).
PAID_CHAT = "deepseek-v4-flash"
FREE_CHAT = "deepseek-v4-flash-free"

#: Le client demande le maximum, forme historique top-level (PAS de
#: ``reasoning_effort``) : c'est la forme que l'ancienne sonde recopiait.
CLIENT_EFFORT_FORM = "max"


# ── harnais amont (repris de tests/test_free_leg_effort_recompute.py) ──
class _Station:
    def __init__(self, n, ip, socks5_url):
        self._station = n
        self.status = "connected"
        self.current_ip = ip
        self.socks5_url = socks5_url
        self._quota_per_ip = 15
        self.current_identity = {
            "impersonate": "chrome131",
            "user_agent": None,
            "extra_headers": {},
        }


class _StubVpn:
    proxy_mode = "vpn"
    current_ip = "10.0.0.1"


class _Pool:
    enabled = True
    socks5_mode = False
    active_station = None

    def __init__(self, stations):
        self._stations = list(stations)

    async def on_request(self):
        best = self._best_station()
        return best.socks5_url if best else None, best

    def _best_station(self):
        for st_ in self._stations:
            if st_.status == "connected":
                return st_
        return None

    def _best_station_excluding_many(self, excluded, forced_pool=None):
        for st_ in self._stations:
            if st_ not in excluded and st_.status == "connected":
                return st_
        return None

    def _station_usable(self, st_, exclude_approaching=False, forced_pool=None):
        return st_.status == "connected"

    def pick_candidates(self, forced_pool=None):
        return [s for s in self._stations if s.status == "connected"]

    def on_quota_exhausted(self, station):
        pass

    def note_hedge_winner(self, winner, primary=None):
        pass


class _Resp:
    """Réponse amont free minimale mais exploitable par le handler (Chat 200)."""

    def __init__(self, payload, status=200):
        self.status_code = status
        self.headers = {"content-type": "application/json"}
        self._payload = payload
        self.content = json.dumps(payload).encode()
        self.text = self.content.decode()
        self._resp = self

    def json(self):
        return self._payload


def _chat_reply(model):
    return {
        "id": "chatcmpl-witness-probe-isolation",
        "object": "chat.completion",
        "model": model,
        "choices": [{"index": 0, "finish_reason": "stop", "message": {"role": "assistant", "content": "OK"}}],
        "usage": {"prompt_tokens": 7, "completion_tokens": 2, "total_tokens": 9},
    }


class _Seam:
    """Capture le corps **réellement** remis à la couche réseau pour l'amont free."""

    def __init__(self):
        self.calls = []

    async def __call__(self, body, headers, proxy_url=None, station=None, endpoint=None, **kw):
        self.calls.append({"endpoint": endpoint, "body": json.loads(json.dumps(body))})
        return _Resp(_chat_reply(body.get("model") or FREE_CHAT))

    @property
    def wire(self):
        assert self.calls, (
            "le double amont n'a jamais été atteint : la jambe free n'a pas été exercée, "
            "le test ne prouve rien"
        )
        return self.calls[0]["body"]


def _rank(level):
    """Rang du niveau sur l'ordre total configuré de ``thinking.effort_order``."""
    from config.effort_caps import get_effort_order

    return get_effort_order().index(level)


# ── injection du déséquilibre de plafonds (asymétrie exigée par le défaut) ──
class _CapAsymmetry:
    """Déclare un couple de plafonds à écart STRICT, sans toucher ``config.yaml``.

    Pourquoi une injection est nécessaire (MESURÉ sur la config réelle) : les
    couples vivants n'ont **jamais** ``cap(free) > cap(paid)`` —
    ``deepseek-v4-flash`` → ``max`` face à ``deepseek-v4-flash-free`` → ``high``,
    ``mimo-v2.5`` → ``max`` face à ``mimo-v2.5-free`` → ``max``. Or la levée de
    masque de l'ancienne sonde n'est **observable** que dans l'autre sens : quand
    le free plafonne plus haut que la décision payante. Le plafond free est donc
    relevé dans ``config.settings._yaml_data`` (lecture live relue à chaque appel,
    aucune écriture disque, ``config.yaml`` intact) :

    * ``deepseek-v4-flash`` → ``high`` : décision payante pour ``effort: max`` = ``high``
    * ``deepseek-v4-flash-free`` → ``max`` : plafond free STRICTEMENT supérieur

    Les deux clés sont posées explicitement car ``get_max_effort_for_model`` fait
    un matching longest-prefix (``deepseek-v4`` → ``max`` masquerait sinon le
    plafond payant). L'asymétrie est *construite* : chaque test l'asserte comme
    prémisse, et ``test_probe_isolation_is_what_makes_the_witness_bite`` échoue si
    elle disparaît (fin de la vacuité).
    """

    def __init__(self, paid: str, free: str, paid_cap: str, free_cap: str):
        self.paid = paid
        self.free = free
        self.paid_cap = paid_cap
        self.free_cap = free_cap

    def __enter__(self):
        import config.settings as cs

        self._data = cs._yaml_data
        self._block = self._data["thinking"]
        self._saved = dict(self._block["effort_caps"])
        patched = dict(self._saved)
        patched[self.paid] = self.paid_cap
        patched[self.free] = self.free_cap
        self._block["effort_caps"] = patched
        return self

    def __exit__(self, *exc):
        self._block["effort_caps"] = self._saved
        return False


@pytest.fixture
def cap_asymmetry():
    """Plafond free STRICTEMENT au-dessus du plafond payant (prémisse du défaut)."""
    with _CapAsymmetry(PAID_CHAT, FREE_CHAT, "high", "max") as asym:
        yield asym


@pytest.fixture
def free_env(monkeypatch):
    monkeypatch.setattr(oc, "_vpn_manager", _StubVpn())
    monkeypatch.setattr(oc, "_get_cached_public_ip", lambda: "127.0.0.1")
    monkeypatch.setattr(oc, "_debug", lambda *a, **k: None)
    monkeypatch.setattr(oc, "_log", lambda *a, **k: None)
    monkeypatch.setattr(oc, "_log_free_model_usage", lambda *a, **k: None)
    monkeypatch.setattr(oc, "_free_ip_pool", _Pool([_Station(1, "1.1.1.1", "socks5://127.0.0.1:1080")]))
    saved = {
        k: oc.IP_ROTATION.get(k)
        for k in (
            "strict_free",
            "max_free_attempts",
            "auto_max_free_attempts",
            "station_count",
            "on_429_action",
            "free_exception_fallback",
            "free_parallel",
        )
    }
    oc.IP_ROTATION.update(
        {
            "strict_free": False,
            "max_free_attempts": 1,
            "auto_max_free_attempts": False,
            "station_count": 1,
            "on_429_action": "both",
            "free_exception_fallback": "station-first",
            "free_parallel": {
                "enabled": False,
                "routing": "round-robin",
                "mode": "load-balance",
                "hedge_delay_ms": 300,
                "hedge_max_attempts": 1,
            },
        }
    )
    oc._free_model_cooldowns.clear()
    yield oc
    for k, v in saved.items():
        if v is None:
            oc.IP_ROTATION.pop(k, None)
        else:
            oc.IP_ROTATION[k] = v


def _install_no_paid_key(monkeypatch):
    """Active le free-first réel du handler P3 (aucune clé payante utilisable)."""
    from core.keys import AllKeysPausedError  # chemin réel du handler (opencode.py:135)

    def _raise(*a, **k):
        raise AllKeysPausedError(retry_after=5.0)

    monkeypatch.setattr(oc, "_get_auth_headers", _raise)
    monkeypatch.setattr(oc, "_has_usable_paid_key", lambda: False)


# ── 0. PRÉMISSES : sans asymétrie de plafonds, le témoin ne prouverait rien ──
def test_witness_premises_free_cap_above_paid_decision(free_env, cap_asymmetry):
    """Le couple doit porter : plafond free > décision payante pour ``effort=max``.

    Sans cette inégalité stricte, la forme héritée se ferait re-plafonner par la
    jambe free et le défaut resterait invisible (c'est exactement pourquoi il est
    passé inaperçu : aucun couple vivant n'a ``cap(free) > cap(paid)``).
    """
    from config.effort_caps import get_max_effort_for_model

    assert oc.FREE_MODEL_MAP.get(PAID_CHAT) == FREE_CHAT, (
        f"prémisse : {PAID_CHAT} n'est plus routé vers {FREE_CHAT} dans cet environnement"
    )

    paid_cap = get_max_effort_for_model(PAID_CHAT)
    free_cap = get_max_effort_for_model(FREE_CHAT)
    assert (paid_cap, free_cap) == ("high", "max"), (paid_cap, free_cap)

    # La décision PAYANTE pour ``effort: max`` est plafonnée à ``high``.
    paid = oc._resolve_effort({"effort": CLIENT_EFFORT_FORM}, PAID_CHAT)
    assert paid.wants and paid.level == "high", paid

    # La sonde CORRECTE (niveau payant seul) reste à ``high`` chez le free…
    probe_correct = oc._resolve_effort({"reasoning_effort": paid.level}, FREE_CHAT)
    assert probe_correct.wants and probe_correct.level == "high", probe_correct

    # … tandis que l'ANCIENNE sonde (corps recopié, ``effort`` brut prioritaire)
    # REMONTE à ``max`` : c'est la levée de masque que le témoin exploite.
    probe_legacy = oc._resolve_effort({"reasoning_effort": paid.level, "effort": CLIENT_EFFORT_FORM}, FREE_CHAT)
    assert probe_legacy.wants and probe_legacy.level == CLIENT_EFFORT_FORM, probe_legacy
    assert _rank(probe_legacy.level or "") > _rank(paid.level or ""), (
        "prémisse : la forme héritée ne remonte pas au-dessus de la décision payante"
    )


# ── 1. E2E : le wire free ne dépasse JAMAIS la décision payante ────────────
def test_e2e_free_wire_never_exceeds_paid_decision(free_env, cap_asymmetry, monkeypatch):
    """``POST /v1/chat/completions`` avec ``effort: max`` → wire free ≤ décision payante.

    Le handler P3 réel résout la décision payante (``high``) et l'écrit dans
    ``reasoning_effort``, mais le ``effort: "max"`` **du client** reste dans le
    corps. Le plafond du free étant ici déclaré ``max`` (> ``high``), l'ancienne
    sonde ``dict(body)`` laissait ce ``effort`` brut l'emporter et l'amont free
    recevait ``max`` — au-dessus de la décision payante.
    """
    from fastapi.testclient import TestClient

    seam = _Seam()
    monkeypatch.setattr(oc, "_do_free_request_curl_cffi", seam)
    _install_no_paid_key(monkeypatch)

    payload = {
        "model": PAID_CHAT,
        "messages": [{"role": "user", "content": "salut"}],
        "effort": CLIENT_EFFORT_FORM,
        "max_tokens": 4096,
    }
    resp = TestClient(oc.app).post("/v1/chat/completions", json=payload)
    assert resp.status_code == 200, resp.text[:500]

    # La décision payante, telle que le handler réel l'a posée :
    paid_decision = oc._resolve_effort({"effort": CLIENT_EFFORT_FORM}, PAID_CHAT)
    assert paid_decision.wants and paid_decision.level == "high", paid_decision

    wire = seam.wire
    assert wire.get("model") == FREE_CHAT, f"modèle free non ciblé : {wire.get('model')!r}"
    assert "/responses" not in (seam.calls[0]["endpoint"] or ""), "ce témoin vise l'endpoint free Chat"

    received = wire.get("reasoning_effort")
    assert received is not None, f"aucun reasoning_effort sur le fil free : {wire!r}"
    assert _rank(received) <= _rank(paid_decision.level), (
        f"l'amont free a reçu reasoning_effort={received!r} alors que la décision payante "
        f"({PAID_CHAT}) valait {paid_decision.level!r} : le niveau a été RELEVÉ au-dessus de la "
        "décision payante, car la sonde d'effort free a recopié la forme cliente plus "
        "prioritaire (`effort`) au lieu de ne porter que la décision payante"
    )


# ── 2. Même garantie sur le contrat direct de ``_try_free_model_first`` ────
@pytest.mark.asyncio
async def test_direct_call_free_wire_never_exceeds_paid_decision(free_env, cap_asymmetry, monkeypatch):
    """Caller interne : corps portant ``effort`` brut ET ``reasoning_effort`` plafonné.

    Ce contrat est celui de **tout** appelant de ``_try_free_model_first`` : la
    garantie « la décision free ne peut que rabaisser » doit tenir sans dépendre
    de la normalisation faite par le handler P3.
    """
    seam = _Seam()
    monkeypatch.setattr(oc, "_do_free_request_curl_cffi", seam)

    paid_decision = oc._resolve_effort({"effort": CLIENT_EFFORT_FORM}, PAID_CHAT)
    assert paid_decision.wants and paid_decision.level == "high", paid_decision

    body = {
        "model": PAID_CHAT,
        "messages": [{"role": "user", "content": "salut"}],
        "effort": CLIENT_EFFORT_FORM,
        "reasoning_effort": paid_decision.level,
        "max_tokens": 4096,
    }
    await oc._try_free_model_first(dict(body), {}, "openai", PAID_CHAT)

    received = seam.wire.get("reasoning_effort")
    assert received is not None, f"aucun reasoning_effort sur le fil free : {seam.wire!r}"
    assert _rank(received) <= _rank(paid_decision.level), (
        f"l'amont free a reçu reasoning_effort={received!r} > décision payante "
        f"{paid_decision.level!r} : le recalcul ne doit que rabaisser"
    )


# ── 3. Garde anti-complicité : le vrai ``_try_free_model_first`` est exercé ──
def test_free_leg_is_the_real_function():
    """Si la suite remplaçait la jambe free par un stub global, 1/2 ne prouveraient rien."""
    import inspect

    src = inspect.getsource(oc._try_free_model_first)
    assert "_do_free_request_curl_cffi" in src, "le seam réseau attendu n'est plus appelé par la jambe free"
    assert "_free_effort_body" in src, "le recalcul d'effort de la jambe free a disparu"
    assert "_resolve_effort(_free_effort_body, free_model)" in src, (
        "le recalcul d'effort ne passe plus par la source unique ``config.effort_policy``"
    )


# ── 4. Sensibilité du témoin : la sonde doit être la SEULE entrée du recalcul ──
def test_probe_isolation_is_what_makes_the_witness_bite(free_env, cap_asymmetry):
    """Vérifie que le déséquilibre de plafonds est bien celui exploité (garde de vacuité).

    Si un jour ``cap(paid) >= cap(free)`` redevenait vrai partout, ce test le dit
    au lieu de laisser 1/2 passer pour de mauvaises raisons.
    """
    from config.effort_caps import get_max_effort_for_model

    paid_cap = get_max_effort_for_model(PAID_CHAT)
    free_cap = get_max_effort_for_model(FREE_CHAT)
    assert _rank(free_cap) > _rank(paid_cap), (
        f"plafonds non divergents ({paid_cap!r} vs {free_cap!r}) : le témoin end-to-end "
        "serait vacu (la forme héritée serait re-plafonnée à la valeur payante)"
    )

    # Et la remontée est bien portée par la seule présence de la forme prioritaire
    # dans la sonde — pas par autre chose.
    alone: dict[str, Any] = {"reasoning_effort": "high"}
    polluted: dict[str, Any] = {"reasoning_effort": "high", "effort": "max"}
    assert oc._resolve_effort(alone, FREE_CHAT).level == "high"
    assert oc._resolve_effort(polluted, FREE_CHAT).level == "max"
