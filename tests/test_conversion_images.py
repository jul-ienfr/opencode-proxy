"""Images / vision : non-régression du transfert vers la Responses API.

Contexte : les routes muse-*/spark-* partent vers .../v1/responses via
_chat_to_responses_request ; avant le correctif, les blocs image_url du
format chat étaient silencieusement ignorés (seul le texte passait).
"""

from app.protocol.mapping import (
    _anthropic_to_responses_request,
    _chat_to_responses_request,
    _normalize_responses_input_items,
    anthropic_to_openai,
    openai_responses_to_anthropic,
    openai_to_anthropic_request,
)

_B64 = "iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAYAAAAfFcSJAAAADUlEQVR42mP8z8BQDwAEhQGAhKmMIQAAAABJRU5ErkJggg=="


def _input_texts(inp):
    return [
        b.get("text", "")
        for item in inp
        for b in (item.get("content") or [])
        if isinstance(b, dict) and b.get("type") in ("input_text", "output_text")
    ]


def _input_images(inp):
    return [
        b
        for item in inp
        for b in (item.get("content") or [])
        if isinstance(b, dict) and b.get("type") == "input_image"
    ]


def test_anthropic_base64_image_survives_to_responses():
    body = {
        "model": "muse-spark-1.3-contributor",
        "messages": [
            {
                "role": "user",
                "content": [
                    {"type": "text", "text": "que vois-tu ?"},
                    {
                        "type": "image",
                        "source": {
                            "type": "base64",
                            "media_type": "image/png",
                            "data": _B64,
                        },
                    },
                ],
            }
        ],
    }
    req = _anthropic_to_responses_request(body)
    assert _input_texts(req["input"]) == ["que vois-tu ?"]
    imgs = _input_images(req["input"])
    assert len(imgs) == 1
    # Schéma Responses officiel : image_url (data URI), jamais image_base64.
    assert imgs[0]["image_url"] == f"data:image/png;base64,{_B64}"
    assert "image_base64" not in imgs[0]
    assert "mime_type" not in imgs[0]


def test_anthropic_url_image_survives_to_responses():
    body = {
        "model": "muse-spark-1.3-contributor",
        "messages": [
            {
                "role": "user",
                "content": [
                    {
                        "type": "image",
                        "source": {
                            "type": "url",
                            "url": "https://example.com/shot.png",
                        },
                    }
                ],
            }
        ],
    }
    req = _anthropic_to_responses_request(body)
    imgs = _input_images(req["input"])
    assert len(imgs) == 1
    assert imgs[0]["image_url"] == "https://example.com/shot.png"


def test_empty_image_does_not_break_text():
    body = {
        "model": "muse-spark-1.3-contributor",
        "messages": [
            {
                "role": "user",
                "content": [
                    {"type": "text", "text": "hello"},
                    {"type": "image", "source": {"type": "base64", "data": ""}},
                ],
            }
        ],
    }
    req = _anthropic_to_responses_request(body)
    assert _input_texts(req["input"]) == ["hello"]
    assert _input_images(req["input"]) == []


def test_chat_image_url_list_maps_to_input_image():
    chat = {
        "model": "muse-spark-1.3-contributor",
        "messages": [
            {
                "role": "user",
                "content": [
                    {"type": "text", "text": "décris"},
                    {
                        "type": "image_url",
                        "image_url": {"url": f"data:image/jpeg;base64,{_B64}"},
                    },
                ],
            }
        ],
    }
    req = _chat_to_responses_request(chat)
    assert _input_texts(req["input"]) == ["décris"]
    imgs = _input_images(req["input"])
    assert len(imgs) == 1
    assert imgs[0]["image_url"] == f"data:image/jpeg;base64,{_B64}"
    assert "image_base64" not in imgs[0]


def test_responses_input_image_back_to_anthropic():
    body = {
        "model": "muse-spark-1.3-contributor",
        "input": [
            {
                "role": "user",
                "content": [
                    {"type": "input_text", "text": "regarde"},
                    {
                        "type": "input_image",
                        "image_url": f"data:image/png;base64,{_B64}",
                    },
                    {
                        "type": "input_image",
                        "image_url": "https://example.com/a.png",
                    },
                    # Forme historique pré-correctif : encore lue.
                    {
                        "type": "input_image",
                        "image_base64": _B64,
                        "mime_type": "image/png",
                    },
                ],
            }
        ],
    }
    out = openai_responses_to_anthropic(body)
    blocks = out["messages"][0]["content"]
    images = [b for b in blocks if b.get("type") == "image"]
    assert len(images) == 3
    assert images[0]["source"] == {
        "type": "base64",
        "media_type": "image/png",
        "data": _B64,
    }
    assert images[1]["source"] == {
        "type": "url",
        "url": "https://example.com/a.png",
    }
    assert images[2]["source"] == {
        "type": "base64",
        "media_type": "image/png",
        "data": _B64,
    }


_PDF_B64 = "JVBERi0xLjQKJeLjz9MKMSAwIG9iago8PC9UeXBlL0NhdGFsb2c+PgplbmRvYmo="


def test_tool_result_image_preserved_to_responses():
    body = {
        "model": "muse-spark-1.3-contributor",
        "messages": [
            {
                "role": "assistant",
                "content": [
                    {
                        "type": "tool_use",
                        "id": "toolu_1",
                        "name": "screenshot",
                        "input": {},
                    }
                ],
            },
            {
                "role": "user",
                "content": [
                    {
                        "type": "tool_result",
                        "tool_use_id": "toolu_1",
                        "content": [
                            {"type": "text", "text": "voici l'écran"},
                            {
                                "type": "image",
                                "source": {
                                    "type": "base64",
                                    "media_type": "image/png",
                                    "data": _B64,
                                },
                            },
                        ],
                    }
                ],
            },
        ],
    }
    req = _anthropic_to_responses_request(body)
    fco = [i for i in req["input"] if i.get("type") == "function_call_output"]
    assert len(fco) == 1
    out_parts = fco[0]["output"]
    assert isinstance(out_parts, list)
    texts = [p["text"] for p in out_parts if p.get("type") == "input_text"]
    imgs = [p for p in out_parts if p.get("type") == "input_image"]
    assert texts == ["voici l'écran"]
    assert len(imgs) == 1
    assert imgs[0]["image_url"] == f"data:image/png;base64,{_B64}"
    assert "image_base64" not in imgs[0]


def test_tool_result_image_preserved_openai_to_anthropic():
    body = {
        "model": "x",
        "messages": [
            {
                "role": "assistant",
                "content": "",
                "tool_calls": [
                    {
                        "id": "call_1",
                        "type": "function",
                        "function": {"name": "shot", "arguments": "{}"},
                    }
                ],
            },
            {
                "role": "tool",
                "tool_call_id": "call_1",
                "content": [
                    {"type": "text", "text": "capture"},
                    {
                        "type": "image_url",
                        "image_url": {"url": f"data:image/png;base64,{_B64}"},
                    },
                ],
            },
            {"role": "user", "content": "merci"},
        ],
    }
    out = openai_to_anthropic_request(body)
    tr = out["messages"][-1]["content"][0]
    assert tr["type"] == "tool_result"
    imgs = [b for b in tr["content"] if b.get("type") == "image"]
    assert len(imgs) == 1
    assert imgs[0]["source"]["data"] == _B64


def test_document_base64_to_responses_input_file():
    body = {
        "model": "muse-spark-1.3-contributor",
        "messages": [
            {
                "role": "user",
                "content": [
                    {
                        "type": "document",
                        "name": "doc.pdf",
                        "source": {
                            "type": "base64",
                            "media_type": "application/pdf",
                            "data": _PDF_B64,
                        },
                    }
                ],
            }
        ],
    }
    chat = anthropic_to_openai(body, body["model"])
    req = _chat_to_responses_request(chat)
    files = [
        b
        for item in req["input"]
        for b in (item.get("content") or [])
        if isinstance(b, dict) and b.get("type") == "input_file"
    ]
    assert len(files) == 1
    # Schéma Responses officiel : file_data (data URI) + filename.
    assert files[0]["file_data"] == f"data:application/pdf;base64,{_PDF_B64}"
    assert files[0]["filename"] == "doc.pdf"
    assert "mime_type" not in files[0]


def test_responses_input_file_back_to_anthropic_document():
    body = {
        "model": "muse-spark-1.3-contributor",
        "input": [
            {
                "role": "user",
                "content": [
                    {
                        "type": "input_file",
                        "file_data": f"data:application/pdf;base64,{_PDF_B64}",
                        "filename": "doc.pdf",
                    },
                    # Forme historique pré-correctif : encore lue.
                    {
                        "type": "input_file",
                        "file_data": _PDF_B64,
                        "mime_type": "application/pdf",
                    },
                ],
            }
        ],
    }
    out = openai_responses_to_anthropic(body)
    docs = [b for b in out["messages"][0]["content"] if b.get("type") == "document"]
    assert len(docs) == 2
    assert docs[0]["source"]["data"] == _PDF_B64
    assert docs[1]["source"]["data"] == _PDF_B64


def test_normalize_responses_input_items_legacy_shapes():
    # Garde-fou 400 upstream : un payload déjà au format Responses mais avec
    # les anciennes clés (image_base64/mime_type, file_url) est normalisé
    # vers image_url / file_data+filename avant envoi.
    inp = [
        {
            "role": "user",
            "content": [
                {
                    "type": "input_image",
                    "image_base64": _B64,
                    "mime_type": "image/png",
                },
                {"type": "input_file", "file_url": "https://example.com/d.pdf"},
            ],
        }
    ]
    out = _normalize_responses_input_items(inp)
    # file_url brute : droppée (ni file_data base64 ni file_id) — il ne
    # reste que l'image normalisée.
    assert out[0]["content"] == [
        {
            "type": "input_image",
            "image_url": f"data:image/png;base64,{_B64}",
        }
    ]
    # Idempotent : une seconde passe ne change plus rien.
    assert _normalize_responses_input_items(out) == out
    # Le caller n'est jamais muté.
    assert "image_base64" in inp[0]["content"][0]
