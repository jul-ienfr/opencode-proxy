"""Images / vision : non-régression du transfert vers la Responses API.

Contexte : les routes muse-*/spark-* partent vers .../v1/responses via
_chat_to_responses_request ; avant le correctif, les blocs image_url du
format chat étaient silencieusement ignorés (seul le texte passait).
"""

from app.protocol.mapping import (
    _anthropic_to_responses_request,
    _chat_to_responses_request,
    _extract_text,
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
        b for item in inp for b in (item.get("content") or []) if isinstance(b, dict) and b.get("type") == "input_image"
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
    # l'ancienne clé image_base64/mime_type est normalisé vers image_url.
    # input_file.file_url EXISTE dans le schéma officiel → KEEP tel quel.
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
    assert out[0]["content"] == [
        {
            "type": "input_image",
            "image_url": f"data:image/png;base64,{_B64}",
        },
        {"type": "input_file", "file_url": "https://example.com/d.pdf"},
    ]
    # Idempotent : une seconde passe ne change plus rien.
    assert _normalize_responses_input_items(out) == out
    # Le caller n'est jamais muté.
    assert "image_base64" in inp[0]["content"][0]


def test_normalize_keeps_file_url_only():
    inp = [
        {
            "role": "user",
            "content": [
                {"type": "input_file", "file_url": "https://example.com/d.pdf"},
            ],
        }
    ]
    out = _normalize_responses_input_items(inp)
    assert out[0]["content"] == [{"type": "input_file", "file_url": "https://example.com/d.pdf"}]


def test_normalize_folds_file_data_url_to_file_url():
    inp = [
        {
            "role": "user",
            "content": [
                {"type": "input_file", "file_data": "https://example.com/d.pdf"},
            ],
        }
    ]
    out = _normalize_responses_input_items(inp)
    assert out[0]["content"] == [{"type": "input_file", "file_url": "https://example.com/d.pdf"}]


def test_normalize_validates_input_audio_format():
    # Set large Responses : wav conservé, format inconnu → placeholder.
    inp = [
        {
            "role": "user",
            "content": [
                {"type": "input_audio", "input_audio": {"data": "AAA=", "format": "wav"}},
                {"type": "input_audio", "input_audio": {"data": "AAA=", "format": "midi"}},
            ],
        }
    ]
    out = _normalize_responses_input_items(inp)
    assert out[0]["content"][0] == {
        "type": "input_audio",
        "input_audio": {"data": "AAA=", "format": "wav"},
    }
    assert out[0]["content"][1] == {"type": "input_text", "text": "[audio:midi]"}
    # Idempotent.
    assert _normalize_responses_input_items(out) == out


# ── Plan section 1 : Anthropic → Chat ─────────────────────────────────────


def test_anthropic_file_image_becomes_placeholder_not_drop():
    body = {
        "model": "muse-spark-1.3-contributor",
        "messages": [
            {
                "role": "user",
                "content": [
                    {"type": "text", "text": "vois"},
                    {
                        "type": "image",
                        "source": {"type": "file", "file_id": "file-abc"},
                    },
                ],
            }
        ],
    }
    chat = anthropic_to_openai(body, body["model"])
    msg = chat["messages"][0]
    assert msg["role"] == "user"
    assert "[image:file]" in msg["content"]


def test_anthropic_document_url_placeholder_never_fake_bytes():
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
                            "type": "url",
                            "url": "https://example.com/d.pdf",
                        },
                    }
                ],
            }
        ],
    }
    chat = anthropic_to_openai(body, body["model"])
    msg = chat["messages"][0]
    assert "[document:url:https://example.com/d.pdf]" in msg["content"]
    # Jamais de faux octets : aucun part file ne porte l'URL en file_data.
    for part in msg["content"] if isinstance(msg["content"], list) else []:
        if isinstance(part, dict) and part.get("type") == "file":
            assert (part.get("file") or {}).get("file_data") != "https://example.com/d.pdf"


def test_anthropic_document_text_source_lossless_to_chat():
    # source.text (text/plain natif Anthropic) → file_data data-URI, lossless.
    body = {
        "model": "muse-spark-1.3-contributor",
        "messages": [
            {
                "role": "user",
                "content": [
                    {
                        "type": "document",
                        "name": "note.txt",
                        "source": {"type": "text", "text": "hello"},
                    }
                ],
            }
        ],
    }
    chat = anthropic_to_openai(body, body["model"])
    msg = chat["messages"][0]
    files = [p for p in msg["content"] if p.get("type") == "file"]
    assert len(files) == 1
    assert files[0]["file"]["file_data"] == "data:text/plain;base64,aGVsbG8="
    assert files[0]["file"]["filename"] == "note.txt"


def test_anthropic_document_url_responses_end_to_end_is_file_url():
    # Anthropic document-URL → Chat (placeholder) NE porte PAS le file_par-URL,
    # mais le chemin natif Responses garde file_url via _chat_to_responses_request.
    chat = {
        "model": "muse-spark-1.3-contributor",
        "messages": [
            {
                "role": "user",
                "content": [
                    {
                        "type": "file",
                        "file": {
                            "file_data": "https://example.com/d.pdf",
                            "filename": "d.pdf",
                        },
                    }
                ],
            }
        ],
    }
    req = _chat_to_responses_request(chat)
    files = [
        b
        for item in req["input"]
        for b in (item.get("content") or [])
        if isinstance(b, dict) and b.get("type") == "input_file"
    ]
    assert files == [
        {
            "type": "input_file",
            "file_url": "https://example.com/d.pdf",
            "filename": "d.pdf",
        }
    ]


# ── Plan section 2 : Chat → Anthropic ─────────────────────────────────────


def test_chat_file_base64_to_anthropic_document_with_name():
    body = {
        "model": "x",
        "messages": [
            {
                "role": "user",
                "content": [
                    {
                        "type": "file",
                        "file": {
                            "file_data": f"data:application/pdf;base64,{_PDF_B64}",
                            "filename": "doc.pdf",
                        },
                    }
                ],
            }
        ],
    }
    out = openai_to_anthropic_request(body)
    docs = [b for b in out["messages"][0]["content"] if b.get("type") == "document"]
    assert len(docs) == 1
    assert docs[0]["source"] == {
        "type": "base64",
        "media_type": "application/pdf",
        "data": _PDF_B64,
    }
    assert docs[0]["name"] == "doc.pdf"


def test_chat_file_id_to_anthropic_document_file():
    body = {
        "model": "x",
        "messages": [
            {
                "role": "user",
                "content": [
                    {"type": "file", "file": {"file_id": "file-abc"}},
                ],
            }
        ],
    }
    out = openai_to_anthropic_request(body)
    docs = [b for b in out["messages"][0]["content"] if b.get("type") == "document"]
    assert docs == [{"type": "document", "source": {"type": "file", "file_id": "file-abc"}}]


def test_chat_file_url_pdf_to_anthropic_document_url():
    body = {
        "model": "x",
        "messages": [
            {
                "role": "user",
                "content": [
                    {
                        "type": "file",
                        "file": {
                            "file_data": "https://example.com/d.pdf",
                            "filename": "d.pdf",
                        },
                    }
                ],
            }
        ],
    }
    out = openai_to_anthropic_request(body)
    docs = [b for b in out["messages"][0]["content"] if b.get("type") == "document"]
    assert len(docs) == 1
    assert docs[0]["source"] == {"type": "url", "url": "https://example.com/d.pdf"}
    assert docs[0]["name"] == "d.pdf"


def test_chat_file_url_non_pdf_becomes_placeholder():
    # Anthropic ne garantit document-URL que pour les PDF : autre type →
    # placeholder honnête, pas de mapping aveugle.
    body = {
        "model": "x",
        "messages": [
            {
                "role": "user",
                "content": [
                    {
                        "type": "file",
                        "file": {
                            "file_data": "https://example.com/a.txt",
                            "filename": "a.txt",
                        },
                    }
                ],
            }
        ],
    }
    out = openai_to_anthropic_request(body)
    texts = [b for b in out["messages"][0]["content"] if b.get("type") == "text"]
    assert texts == [{"type": "text", "text": "[document:url:https://example.com/a.txt]"}]


def test_chat_input_audio_to_anthropic_placeholder():
    body = {
        "model": "x",
        "messages": [
            {
                "role": "user",
                "content": [
                    {
                        "type": "input_audio",
                        "input_audio": {"data": "AAA=", "format": "wav"},
                    }
                ],
            }
        ],
    }
    out = openai_to_anthropic_request(body)
    texts = [b for b in out["messages"][0]["content"] if b.get("type") == "text"]
    assert texts == [{"type": "text", "text": "[audio:unsupported-by-anthropic]"}]


# ── Plan section 3 : Chat → Responses ─────────────────────────────────────


def test_chat_input_audio_passthrough_validated_wav_mp3_only():
    chat = {
        "model": "muse-spark-1.3-contributor",
        "messages": [
            {
                "role": "user",
                "content": [
                    {
                        "type": "input_audio",
                        "input_audio": {"data": "AAA=", "format": "mp3"},
                    },
                    {
                        "type": "input_audio",
                        "input_audio": {"data": "AAA=", "format": "flac"},
                    },
                ],
            }
        ],
    }
    req = _chat_to_responses_request(chat)
    parts = req["input"][0]["content"]
    assert parts[0] == {
        "type": "input_audio",
        "input_audio": {"data": "AAA=", "format": "mp3"},
    }
    # Chat n'accepte que wav|mp3 en entrée : flac → placeholder.
    assert parts[1] == {"type": "input_text", "text": "[audio:flac]"}


def test_chat_video_becomes_placeholder_in_responses():
    chat = {
        "model": "muse-spark-1.3-contributor",
        "messages": [{"role": "user", "content": [{"type": "video_url"}]}],
    }
    req = _chat_to_responses_request(chat)
    assert req["input"][0]["content"] == [{"type": "input_text", "text": "[video:unsupported]"}]


def test_chat_tool_file_and_audio_to_function_call_output():
    chat = {
        "model": "muse-spark-1.3-contributor",
        "messages": [
            {
                "role": "assistant",
                "content": "",
                "tool_calls": [
                    {
                        "id": "call_1",
                        "type": "function",
                        "function": {"name": "read", "arguments": "{}"},
                    }
                ],
            },
            {
                "role": "tool",
                "tool_call_id": "call_1",
                "content": [
                    {
                        "type": "file",
                        "file": {"file_id": "file-abc"},
                    },
                    {
                        "type": "input_audio",
                        "input_audio": {"data": "AAA=", "format": "wav"},
                    },
                ],
            },
        ],
    }
    req = _chat_to_responses_request(chat)
    fco = [i for i in req["input"] if i.get("type") == "function_call_output"]
    assert len(fco) == 1
    assert fco[0]["output"] == [
        {"type": "input_file", "file_id": "file-abc"},
        {"type": "input_text", "text": "[audio:unsupported-in-chat-tool-result]"},
    ]


# ── Plan section 4 : Responses → Anthropic ────────────────────────────────


def test_responses_input_file_filename_becomes_document_name():
    body = {
        "model": "muse-spark-1.3-contributor",
        "input": [
            {
                "role": "user",
                "content": [
                    {
                        "type": "input_file",
                        "file_data": f"data:application/pdf;base64,{_PDF_B64}",
                        "filename": "rapport.pdf",
                    },
                ],
            }
        ],
    }
    out = openai_responses_to_anthropic(body)
    docs = [b for b in out["messages"][0]["content"] if b.get("type") == "document"]
    assert len(docs) == 1
    assert docs[0]["name"] == "rapport.pdf"
    assert docs[0]["source"]["data"] == _PDF_B64


def test_responses_input_audio_to_anthropic_placeholder():
    body = {
        "model": "muse-spark-1.3-contributor",
        "input": [
            {
                "role": "user",
                "content": [
                    {
                        "type": "input_audio",
                        "input_audio": {"data": "AAA=", "format": "flac"},
                    },
                ],
            }
        ],
    }
    out = openai_responses_to_anthropic(body)
    texts = [b for b in out["messages"][0]["content"] if b.get("type") == "text"]
    assert texts == [{"type": "text", "text": "[audio:unsupported-by-anthropic]"}]


def test_responses_function_call_output_list_multimodal():
    body = {
        "model": "muse-spark-1.3-contributor",
        "input": [
            {
                "type": "function_call_output",
                "call_id": "call_1",
                "output": [
                    {"type": "input_text", "text": "voici"},
                    {
                        "type": "input_image",
                        "image_url": f"data:image/png;base64,{_B64}",
                    },
                    {
                        "type": "input_file",
                        "file_data": f"data:application/pdf;base64,{_PDF_B64}",
                        "filename": "doc.pdf",
                    },
                    {
                        "type": "input_audio",
                        "input_audio": {"data": "AAA=", "format": "wav"},
                    },
                ],
            },
            {"role": "user", "content": [{"type": "input_text", "text": "ok"}]},
        ],
    }
    out = openai_responses_to_anthropic(body)
    tr = out["messages"][0]["content"][0]
    assert tr["type"] == "tool_result"
    by_type = {}
    for b in tr["content"]:
        by_type.setdefault(b["type"], []).append(b)
    assert by_type["text"][0]["text"] == "voici"
    assert by_type["image"][0]["source"]["data"] == _B64
    assert by_type["document"][0]["name"] == "doc.pdf"
    assert by_type["document"][0]["source"]["data"] == _PDF_B64
    assert {"type": "text", "text": "[audio:unsupported-by-anthropic]"} in tr["content"]


def test_responses_function_call_output_string_stays_string():
    # Contrat golden : output string seule reste string (pas de liste).
    body = {
        "model": "muse-spark-1.3-contributor",
        "input": [
            {
                "type": "function_call_output",
                "call_id": "call_1",
                "output": "pong",
            },
            {"role": "user", "content": [{"type": "input_text", "text": "ok"}]},
        ],
    }
    out = openai_responses_to_anthropic(body)
    tr = out["messages"][0]["content"][0]
    assert tr["type"] == "tool_result"
    assert tr["content"] == "pong"


# ── Plan section 6 : transverse ───────────────────────────────────────────


def test_extract_text_placeholders_audio_video_file():
    assert _extract_text([{"type": "text", "text": "hi"}]) == "hi"
    assert _extract_text([{"type": "input_audio", "input_audio": {"data": "AAA=", "format": "mp3"}}]) == "[audio:mp3]"
    assert _extract_text([{"type": "video_url"}]) == "[video:unsupported]"
    assert _extract_text([{"type": "file", "file": {"filename": "d.pdf"}}]) == "[file:d.pdf]"


def test_anthropic_tool_result_document_and_audio_to_chat():
    body = {
        "model": "muse-spark-1.3-contributor",
        "messages": [
            {
                "role": "assistant",
                "content": [
                    {
                        "type": "tool_use",
                        "id": "toolu_1",
                        "name": "read",
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
                            {
                                "type": "document",
                                "name": "doc.pdf",
                                "source": {
                                    "type": "base64",
                                    "media_type": "application/pdf",
                                    "data": _PDF_B64,
                                },
                            },
                            {
                                "type": "input_audio",
                                "input_audio": {"data": "AAA=", "format": "wav"},
                            },
                        ],
                    }
                ],
            },
        ],
    }
    chat = anthropic_to_openai(body, body["model"])
    tool_msg = next(m for m in chat["messages"] if m["role"] == "tool")
    content = tool_msg["content"]
    assert isinstance(content, list)
    files = [p for p in content if p.get("type") == "file"]
    assert len(files) == 1
    assert files[0]["file"]["filename"] == "doc.pdf"
    texts = [p["text"] for p in content if p.get("type") == "text"]
    assert texts == ["[audio:unsupported-in-chat-tool-result]"]
