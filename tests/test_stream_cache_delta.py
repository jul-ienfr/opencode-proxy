"""[PLAN_AUDIT_CLIENT_OFFICIEL V6 · F2] Cache relu au ``message_delta``.

**Le défaut.** Le suivi de tokens d'un flux ne lisait
``cache_read_input_tokens`` qu'au ``message_start``
(``opencode.py stream_cache = usage.get("cache_read_input_tokens", 0)``).
Or l'API Anthropic rend **0** au ``message_start`` et le vrai chiffre au
``message_delta`` final : la DB affichait donc 0 cache là où le client
affichait le bon nombre. Même chose sur la jambe libre convertie — notre
convertisseur porte le cache dans le ``message_delta`` qu'il émet
(``app/protocol/chat_sse_to_anthropic.py`` ``finalize_events``).

**Le piège.** Plusieurs gateways répètent un ``usage`` **cumulatif** d'un
bout à l'autre du flux. Il faut donc ÉCRASER le courant (et ne créditer
que la **différence**), jamais ADDITIONNER la valeur lue : l'addition
compterait deux fois le même cache.

Ces tests couvrent le helper pur ``opencode._stream_cache_delta`` que les
deux sites d'appel partagent, plus la preuve que les deux sites
l'appellent bien (garde anti-révert partiel).
"""

import json

import pytest

import opencode as oc
from app.protocol.chat_sse_to_anthropic import ChatSseToAnthropicState

# ─────────── Sémantique du helper : écrasement, jamais addition ───────────


def _credit_replay(stream):
    """Rejoue un flux comme le fait le site A et rend le cache crédité.

    Modèle volontairement fidèle : ``message_start`` pose le courant et
    crédite sa valeur, chaque ``message_delta`` crédite la DIFFÉRENCE
    retournée par le helper (donc rien quand le helper rend ``None``).
    """
    courant = 0
    credit = 0
    for etype, usage in stream:
        res = oc._stream_cache_delta(usage, courant)
        if res is None:
            continue
        courant, delta = res
        credit += delta
    return courant, credit


def test_helper_absent_ou_non_utilisable_ne_change_rien():
    """Une valeur manquante ou non entière ne doit ni créditer ni écraser."""
    for usage in (
        {},
        {"cache_read_input_tokens": None},
        {"cache_read_input_tokens": "850"},
        {"cache_read_input_tokens": 850.0},
        {"cache_read_input_tokens": 850.5},
        # bool est un `int` en Python : True vaudrait 1 token de cache.
        {"cache_read_input_tokens": True},
        {"cache_read_input_tokens": False},
        None,
        "pas un dict",
    ):
        assert oc._stream_cache_delta(usage, 100) is None, usage


def test_helper_identique_au_courant_ne_credite_rien():
    """Un ``usage`` qui répète la valeur déjà connue ne crédite rien — c'est
    le cas le plus fréquent (gateway cumulatif)."""
    assert oc._stream_cache_delta({"cache_read_input_tokens": 850}, 850) is None
    assert oc._stream_cache_delta({"cache_read_input_tokens": 0}, 0) is None


def test_helper_montee_credite_la_difference():
    assert oc._stream_cache_delta({"cache_read_input_tokens": 400}, 100) == (400, 300)


def test_helper_baisse_credite_un_delta_negatif():
    """Une valeur qui DIMINUE corrige un excédent : le delta doit être
    négatif, sinon un compteur déjà gonflé le resterait."""
    assert oc._stream_cache_delta({"cache_read_input_tokens": 120}, 500) == (120, -380)


def test_helper_courant_none_est_traite_comme_zero():
    assert oc._stream_cache_delta({"cache_read_input_tokens": 7}, None) == (7, 7)


def test_pas_de_double_comptage_sur_usage_cumulatif():
    """F2 — LE test. Flux cumulatif réaliste : ``message_start`` porte déjà
    1200, puis le ``message_delta`` final répète 1200 en entier. Le crédit
    total doit valoir 1200 UNE fois, pas 2400."""
    courant, credit = _credit_replay(
        [
            ("message_start", {"cache_read_input_tokens": 1200}),
            ("message_delta", {"cache_read_input_tokens": 1200}),
        ]
    )
    assert courant == 1200
    assert credit == 1200, "le cache cumulatif répété au delta serait compté deux fois"


def test_cumul_qui_progresse_ne_compte_que_les_increments():
    """Flux où le cache monte par paliers (certains gateways découpent) :
    chaque palier ne crédite que sa rotation."""
    courant, credit = _credit_replay(
        [
            ("message_start", {"cache_read_input_tokens": 100}),
            ("message_delta", {"cache_read_input_tokens": 100}),
            ("message_delta", {"cache_read_input_tokens": 100}),
            ("message_delta", {"cache_read_input_tokens": 700}),
            ("message_delta", {"cache_read_input_tokens": 700}),
        ]
    )
    assert courant == 700
    assert credit == 700
    # La addition naïve (somme de tout ce qui a été lu) donnerait 2700.
    assert credit < 100 + 100 + 100 + 700 + 700


def test_zero_au_start_puis_chiffre_reel_au_delta():
    """La vraie forme Anthropic : 0 au start, 850 au delta final. Avant F2 la
    DB restait à 0 alors que le client affichait 850."""
    courant, credit = _credit_replay(
        [
            ("message_start", {"cache_read_input_tokens": 0}),
            ("message_delta", {"cache_read_input_tokens": 850}),
        ]
    )
    assert courant == 850
    assert credit == 850


def test_usage_sans_cache_puis_delta_qui_l_apporte():
    """Variante : le ``message_start`` ne porte pas la clé du tout (absent),
    c'est le delta qui l'apporte — rien ne doit être perdu."""
    courant, credit = _credit_replay(
        [
            ("message_start", {}),
            ("message_delta", {"cache_read_input_tokens": 425}),
        ]
    )
    assert courant == 425
    assert credit == 425


# ─────────── La jambe libre convertie : le cache arrive au delta ───────────


def _data_lines(events):
    out = []
    for ev in events:
        text = ev.decode() if isinstance(ev, bytes) else str(ev)
        for line in text.splitlines():
            if line.startswith("data: "):
                out.append(json.loads(line[6:]))
    return out


def test_convertisseur_libre_porte_le_cache_dans_le_message_delta():
    """F2 — la jambe libre convertie émet le cache au ``message_delta``. Si
    un jour il remontait au ``message_start`` seulement, ce test casse et il
    faudrait alors relire le site A à deux endroits."""
    st = ChatSseToAnthropicState(model="glm-5.1")
    st.feed_chunk(
        {
            "choices": [{"delta": {"content": "bonjour"}}],
            "usage": {
                "prompt_tokens": 30,
                "completion_tokens": 5,
                "prompt_tokens_details": {"cached_tokens": 850},
            },
        }
    )
    deltas = [
        p
        for p in _data_lines(st.finalize_events())
        if p.get("type") == "message_delta"
    ]
    assert len(deltas) == 1
    assert deltas[0]["usage"]["cache_read_input_tokens"] == 850


def test_delta_emis_par_le_convertisseur_est_credite_sans_double_compte():
    """Bout en bout côté lecture : le ``message_delta`` que le convertisseur
    libre émet alimente le suivi exactement comme un flux Anthropic natif."""
    st = ChatSseToAnthropicState(model="glm-5.1")
    st.feed_chunk(
        {
            "choices": [{"delta": {"content": "bonjour"}}],
            "usage": {"completion_tokens": 5, "prompt_tokens_details": {"cached_tokens": 850}},
        }
    )
    md = [
        p
        for p in _data_lines(st.finalize_events())
        if p.get("type") == "message_delta"
    ][0]

    # Le message_start converti annonce 0 (comme l'API Anthropic)…
    courant, credit = _credit_replay(
        [
            ("message_start", {"cache_read_input_tokens": 0}),
            ("message_delta", md["usage"]),
        ]
    )
    assert courant == 850
    assert credit == 850


# ─────────── Les deux sites d'appel utilisent bien le helper ───────────


def test_les_deux_sites_appellent_le_helper():
    """Garde anti-révert partiel : le correctif doit couvrir la boucle de
    suivi partagée ET la boucle de conversion P4. Un seul appel laisserait
    l'autre jambe à 0."""
    src = oc.__dict__["_stream_cache_delta"].__code__.co_filename
    with open(src, encoding="utf-8") as fh:
        body = fh.read()
    # Deux appels (les deux boucles) + la définition.
    assert body.count("_stream_cache_delta(") >= 3, "un site d'appel a perdu le helper F2"


@pytest.mark.parametrize("bad", [None, {}, {"cache_read_input_tokens": []}])
def test_helper_ne_leve_jamais(bad):
    """Le helper est appelé sur des données réseau : jamais d'exception."""
    assert oc._stream_cache_delta(bad, 10) is None
