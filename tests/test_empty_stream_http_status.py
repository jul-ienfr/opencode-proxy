"""V5.1 — un flux amont vide ne doit plus sortir en HTTP 200.

**Le defaut (mesure en production).** Un amont repond 200 puis ne produit aucun
octet : le proxy rendait un `200 text/event-stream` contenant un evenement
`error`. Le client lisait le 200 comme un succes.

**Le mecanisme du correctif.** Le proxy fait deja un peek, mais il attend les
*en-tetes* (`__aenter__` sous `_TTFBWatchdogTimeout`) : un 200 suivi d'un corps
vide le franchit. Plutot que de changer le contrat des quatre chemins de
streaming, on exploite un fait deja etabli par la garde du chemin Anthropic :
`not _bytes_flushed` signifie qu'**aucun octet n'a ete emis**, donc que
Starlette n'a pas encore envoye les en-tetes, donc que le statut est encore
libre. Dans ce cas on **leve** `EmptyUpstreamStream` (502) au lieu de produire
un evenement d'erreur dans une reponse 200.

**Ce que ces tests verrouillent.**
1. l'exception porte bien un statut et un message client maitrises ;
2. la condition « on peut encore echouer proprement » est exactement
   « rien n'a ete envoye » — pas « le flux a l'air vide » ;
3. le handler global respecte le statut porte par l'exception (sinon le 502
   serait aplati en 500 et le correctif serait invisible).

Le chemin OpenAI (passthrough) garde volontairement l'evenement `error` : son
generateur ne tient pas de sentinelle « rien n'a ete envoye », et lever la
couperait un flux qui livrait du contenu. C'est documente dans le code.
"""

import inspect

import opencode as oc


def test_exception_carries_a_502_and_a_safe_message():
    exc = oc.EmptyUpstreamStream("detail interne")
    assert exc.status_code == 502
    assert "empty stream" in exc.client_message.lower()
    # Le message client ne doit jamais refleter l'amont : il est porte par la
    # classe, pas par l'exception.
    assert "detail interne" not in exc.client_message


def test_can_fail_only_when_nothing_was_sent():
    assert oc._can_fail_with_http_status(False) is True, (
        "aucun octet emis -> en-tetes pas partis -> le statut est encore libre"
    )
    assert oc._can_fail_with_http_status(True) is False, "des octets sont partis -> on ne coupe jamais un flux commence"


def test_raised_exception_subclasses_runtime_error():
    """Catchable par le `except Exception` du handler sans import circulaire."""
    assert issubclass(oc.EmptyUpstreamStream, RuntimeError)


def test_global_handler_honours_an_explicit_status():
    """Le handler doit lire `status_code` : sinon le 502 devient un 500."""
    source = inspect.getsource(oc.debug_exception)
    assert "status_code" in source
    assert "client_message" in source


def test_anthropic_stream_path_raises_instead_of_yielding_error():
    """Le chemin Anthropic doit lever, la ou il produisait un evenement."""
    source = inspect.getsource(oc)
    needle = "raise EmptyUpstreamStream("
    assert needle in source, "le correctif V5.1 a disparu du chemin Anthropic"


def test_counter_is_incremented_before_raising():
    """Un incident qui echoue proprement doit rester comptable."""
    source = inspect.getsource(oc)
    idx_counter = source.find("_note_empty_stream(")
    idx_raise = source.find("raise EmptyUpstreamStream(")
    assert 0 < idx_counter < idx_raise, (
        "le compteur doit etre incremente AVANT la levee, sinon l'incident "
        "devient invisible des qu'on corrige le symptome"
    )
