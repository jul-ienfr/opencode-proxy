"""[PLAN_AUDIT_CONVERSIONS Lot L4] Contrat d'estimation de tokens — A10.

Constat du plan : *« `_extract_text` réduit image/document à `[image:type]` /
`[document:type]` : `count_tokens` et l'estimation d'entrée en stream
sous-estiment structurellement le vision/PDF. »*

Ce fichier verrouille la correction : l'estimation tient compte de la **taille**
de la charge utile média, tout en restant bornée et robuste.

Choix assumé (documenté dans `_media_token_cost`) : le tokenizer vision réel
n'est pas documenté et varie par upstream, donc on n'essaie pas de l'égaler —
on rend le compte **monotone en la taille**, ce qui suffit à corriger la
sous-estimation structurelle et la décision de compaction.
"""

import pytest

from protocol.tokens import _media_token_cost, estimate_input_tokens

PNG_B64 = "iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAYAAAAfFcSJAAAADUlEQVR42mP8z8BQDwAEhQGAhKmMIQAAAABJRU5ErkJggg=="
PDF_B64 = "JVBERi0xLjQKJcOkw7zDtsOfCg=="


def _body(content):
    return {"model": "t", "max_tokens": 256, "messages": [{"role": "user", "content": content}]}


# ───────────── le principe : la taille compte ─────────────


def test_larger_image_costs_more_than_smaller():
    """A10 — LE test de la correction : le coût croît avec la taille."""
    small = _body([{"type": "image", "source": {"type": "base64", "data": PNG_B64}}])
    big = _body([{"type": "image", "source": {"type": "base64", "data": PNG_B64 * 300}}])
    assert estimate_input_tokens(big) > estimate_input_tokens(small)


def test_larger_document_costs_more_than_smaller():
    """A10 : même propriété pour un PDF (le cas métier principal)."""
    small = _body([{"type": "document", "source": {"type": "base64", "data": PDF_B64}}])
    big = _body([{"type": "document", "source": {"type": "base64", "data": PDF_B64 * 2000}}])
    assert estimate_input_tokens(big) > estimate_input_tokens(small)


def test_estimate_is_monotonic_in_media_size():
    """Propriété : le compte ne redescend jamais quand la charge grandit."""
    sizes = [1, 10, 100, 1000, 10000]
    counts = [
        estimate_input_tokens(
            _body([{"type": "image", "source": {"type": "base64", "data": PDF_B64 * n}}])
        )
        for n in sizes
    ]
    assert counts == sorted(counts), f"estimation non monotone : {counts}"


def test_media_is_never_counted_as_zero():
    """Une image minuscule coûte au moins quelques tokens — jamais zéro."""
    assert _media_token_cost({"type": "image", "source": {"type": "base64", "data": "AA"}}) >= 4


def test_text_only_estimation_is_unchanged_by_the_media_cost():
    """Pas de régression : un corps sans média garde son estimation textuelle."""
    plain = _body("Bonjour, comment vas-tu ?")
    assert estimate_input_tokens(plain) > 0
    assert _media_token_cost({"type": "text", "text": "hi"}) == 0


# ───────────── couverture des formes réelles ─────────────


@pytest.mark.parametrize(
    "block",
    [
        {"type": "image", "source": {"type": "base64", "data": PDF_B64 * 50}},
        {"type": "image", "source": {"type": "url", "url": "https://ex.com/a.png"}},
        {"type": "document", "source": {"type": "base64", "data": PDF_B64 * 50}},
        {"type": "document", "source": {"type": "url", "url": "https://ex.com/a.pdf"}},
        {"type": "file", "file": {"file_data": f"data:application/pdf;base64,{PDF_B64 * 50}"}},
        {"type": "input_file", "file_data": f"data:application/pdf;base64,{PDF_B64 * 50}"},
        {"type": "input_image", "image_url": f"data:image/png;base64,{PNG_B64 * 50}"},
        {"type": "input_audio", "input_audio": {"data": "AA" * 500, "format": "mp3"}},
    ],
)
def test_every_media_form_is_costed(block):
    """Les 8 formes de média réellement rencontrées sont toutes comptées."""
    assert _media_token_cost(block) > 0, f"forme non comptée : {block.get('type')}"


def test_media_inside_tool_result_is_counted():
    """A10 : une capture d'écran renvoyée par un OUTIL (dans un tool_result)
    comptait pour zéro car l'extracteur la réduit à un marqueur.

    C'est un cas fréquent (outil de navigateur/vision), pas un cas limite.
    """
    content_with_media = [
        {
            "type": "tool_result",
            "tool_use_id": "t1",
            "content": [
                {"type": "text", "text": "voici la capture"},
                {"type": "image", "source": {"type": "base64", "data": PNG_B64 * 500}},
            ],
        }
    ]
    content_without_media = [
        {
            "type": "tool_result",
            "tool_use_id": "t1",
            "content": [{"type": "text", "text": "voici la capture"}],
        }
    ]
    with_media = estimate_input_tokens(_body(content_with_media))
    without_media = estimate_input_tokens(_body(content_without_media))
    assert with_media > without_media, "le média dans tool_result n'est pas compté"


def test_data_uri_header_is_not_billed_as_payload():
    """L'en-tête `data:...;base64,` n'est pas de la charge utile facturée."""
    raw = PDF_B64 * 100
    with_header = _media_token_cost(
        {"type": "input_file", "file_data": f"data:application/pdf;base64,{raw}"}
    )
    without_header = _media_token_cost({"type": "input_file", "file_data": raw})
    assert with_header == without_header, "l'en-tête du data URI est compté à tort"


# ───────────── bornes et robustesse ─────────────


def test_media_cost_is_capped():
    """Un média démesuré ne doit pas saturer à lui seul tous les compteurs."""
    enorme = {"type": "document", "source": {"type": "base64", "data": "A" * 10_000_000}}
    assert _media_token_cost(enorme) <= 200_000


@pytest.mark.parametrize(
    "broken",
    [
        {},
        {"type": "image"},
        {"type": "image", "source": None},
        {"type": "image", "source": "pas un dict"},
        {"type": "document", "source": {}},
        {"type": "file", "file": None},
        {"type": "input_file"},
        {"type": "input_audio", "input_audio": "pas un dict"},
        {"type": "inconnu", "data": "x"},
    ],
)
def test_media_cost_never_raises(broken):
    """Robustesse : le corps vient du réseau — jamais d'exception."""
    assert _media_token_cost(broken) >= 0


def test_estimation_survives_malformed_body():
    """Robustesse globale de `estimate_input_tokens` sur corps malformés."""
    for broken in [
        {"messages": "pas une liste"},
        {"messages": [{"role": "user", "content": 42}]},
        {"messages": [{"content": [None, 42, {"type": "image"}]}]},
        {},
    ]:
        estimate_input_tokens(broken)  # ne doit pas lever


def test_document_costs_more_than_image_for_same_size():
    """Le facteur PDF > image est assumé (texte + mise en page vs image
    compressée) : il est documenté, donc verrouillé."""
    payload = PDF_B64 * 1000
    img = _media_token_cost({"type": "image", "source": {"type": "base64", "data": payload}})
    doc = _media_token_cost({"type": "document", "source": {"type": "base64", "data": payload}})
    assert doc > img
