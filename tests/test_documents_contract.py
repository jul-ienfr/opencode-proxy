"""[PLAN_AUDIT_CONVERSIONS Lot L4] Contrat DOCUMENTS — A9.

Constat du plan (§2) : *« Un seul fichier de test contient le mot `document`
(test_conversion_images.py) : aucun verrou golden documents. »*

Autrement dit, A9 n'était pas une conversion cassée mais une **couverture
absente** : les quatre directions document ↔ file existaient sans qu'aucun test
ne fixe leur contrat. Ce fichier est ce verrou.

Matrice verrouillée (source de vérité = `mapping.py`) :

| Direction        | base64            | file_id / fichier amont | url                        |
|------------------|-------------------|-------------------------|----------------------------|
| Anthropic → Chat | `file_data` data: | `file.file_id`          | placeholder `[document:url:` |
| Chat → Anthropic | `document` base64 | `document` file_id      | `[document:url:`           |
| Responses → Anth | `document` base64 | `document` file_id      | `document` file_url        |
| Chat → Responses | `input_file`      | `input_file` file_id    | `input_file` file_url      |

Les placeholders `[document:...]` sont **volontaires** : là où le protocole
cible n'a pas d'équivalent, on émet un marqueur textuel honnête + un log debug,
jamais un faux data URI ni une suppression silencieuse. C'est le contrat à
verrouiller — pas un défaut à corriger.
"""


import protocol_mapping as pm

PDF_B64 = "JVBERi0xLjQKJcOkw7zDtsOfCg=="
DATA_URI = f"data:application/pdf;base64,{PDF_B64}"


# ─────────────────── Anthropic → Chat (P2) ───────────────────


def test_p2_document_base64_becomes_file_data():
    """A9 : un `document` base64 devient une part `file` avec data URI."""
    out = pm.anthropic_to_openai(
        {
            "model": "test",
            "max_tokens": 1024,
            "messages": [
                {
                    "role": "user",
                    "content": [
                        {"type": "text", "text": "résume"},
                        {
                            "type": "document",
                            "source": {
                                "type": "base64",
                                "media_type": "application/pdf",
                                "data": PDF_B64,
                            },
                            "name": "rapport.pdf",
                        },
                    ],
                }
            ],
        },
        "deepseek-v4-flash",
    )
    parts = out["messages"][0]["content"]
    files = [p for p in parts if isinstance(p, dict) and p.get("type") == "file"]
    assert files, "le document base64 n'a pas produit de part file"
    assert files[0]["file"]["file_data"] == DATA_URI
    assert files[0]["file"]["filename"] == "rapport.pdf"


def test_p2_document_base64_defaults_media_type_to_pdf():
    """Sans `media_type`, on suppose PDF (défaut du protocole Anthropic)."""
    out = pm.anthropic_to_openai(
        {
            "model": "test",
            "max_tokens": 1024,
            "messages": [
                {
                    "role": "user",
                    "content": [
                        {"type": "document", "source": {"type": "base64", "data": PDF_B64}}
                    ],
                }
            ],
        },
        "deepseek-v4-flash",
    )
    parts = out["messages"][0]["content"]
    files = [p for p in parts if isinstance(p, dict) and p.get("type") == "file"]
    assert files[0]["file"]["file_data"].startswith("data:application/pdf;base64,")
    # Nom par défaut : un fichier sans nom doit rester identifiable.
    assert files[0]["file"]["filename"] == "document.pdf"


def test_p2_document_title_is_the_client_field_and_survives():
    """[A25] Le champ client est ``title``, pas ``name``.

    ``DocumentBlockParam`` du SDK Anthropic expose exactement :
    ``source, type, cache_control, citations, context, title``. Le code ne
    lisait que ``name`` → la condition était toujours fausse et le nom de
    fichier du client était **perdu en silence** (retombée sur
    ``document.pdf``). Ce test verrouille la valeur réelle, pas la clé.
    """
    out = pm.anthropic_to_openai(
        {
            "model": "test",
            "max_tokens": 1024,
            "messages": [
                {
                    "role": "user",
                    "content": [
                        {
                            "type": "document",
                            "source": {
                                "type": "base64",
                                "media_type": "application/pdf",
                                "data": PDF_B64,
                            },
                            "title": "rapport-annuel.pdf",
                        }
                    ],
                }
            ],
        },
        "deepseek-v4-flash",
    )
    parts = out["messages"][0]["content"]
    files = [p for p in parts if isinstance(p, dict) and p.get("type") == "file"]
    assert files, "le document base64 n'a pas produit de part file"
    assert files[0]["file"]["filename"] == "rapport-annuel.pdf", (
        "le titre du document doit survivre jusqu'à `filename` côté Chat"
    )


def test_p2_document_text_source_title_survives():
    """[A25] Même défaut sur la branche ``source.type == "text"``."""
    out = pm.anthropic_to_openai(
        {
            "model": "test",
            "max_tokens": 1024,
            "messages": [
                {
                    "role": "user",
                    "content": [
                        {
                            "type": "document",
                            "source": {"type": "text", "text": "contenu brut"},
                            "title": "notes.txt",
                        }
                    ],
                }
            ],
        },
        "deepseek-v4-flash",
    )
    parts = out["messages"][0]["content"]
    files = [p for p in parts if isinstance(p, dict) and p.get("type") == "file"]
    assert files, "le document texte n'a pas produit de part file"
    assert files[0]["file"]["filename"] == "notes.txt"


def test_p2_document_file_id_is_transported():
    """A9 : un `document` référencé par `file_id` garde sa référence."""
    out = pm.anthropic_to_openai(
        {
            "model": "test",
            "max_tokens": 1024,
            "messages": [
                {
                    "role": "user",
                    "content": [
                        {
                            "type": "document",
                            "source": {"type": "file", "file_id": "file_abc123"},
                        }
                    ],
                }
            ],
        },
        "deepseek-v4-flash",
    )
    parts = out["messages"][0]["content"]
    files = [p for p in parts if isinstance(p, dict) and p.get("type") == "file"]
    assert files[0]["file"]["file_id"] == "file_abc123"


def test_p2_document_url_becomes_honest_placeholder():
    """A9 : Chat n'a pas de part fichier-par-URL. Le contrat est un placeholder
    textuel **explicite** contenant l'URL — jamais un data URI mensonger, jamais
    un drop silencieux."""
    out = pm.anthropic_to_openai(
        {
            "model": "test",
            "max_tokens": 1024,
            "messages": [
                {
                    "role": "user",
                    "content": [
                        {
                            "type": "document",
                            "source": {"type": "url", "url": "https://ex.com/doc.pdf"},
                        }
                    ],
                }
            ],
        },
        "deepseek-v4-flash",
    )
    content = out["messages"][0]["content"]
    text = content if isinstance(content, str) else " ".join(
        p.get("text", "") for p in content if isinstance(p, dict)
    )
    assert "https://ex.com/doc.pdf" in text, "l'URL du document a été perdue sans trace"
    assert "[document" in text, "le placeholder honnête n'a pas été émis"


def test_p2_document_unknown_source_is_never_dropped_silently():
    """A9 : une source non gérée laisse une trace textuelle (pas de perte muette
    qui ferait croire à un document pris en compte)."""
    out = pm.anthropic_to_openai(
        {
            "model": "test",
            "max_tokens": 1024,
            "messages": [
                {
                    "role": "user",
                    "content": [
                        {"type": "document", "source": {"type": "exotique", "data": "xx"}}
                    ],
                }
            ],
        },
        "deepseek-v4-flash",
    )
    content = out["messages"][0]["content"]
    text = content if isinstance(content, str) else " ".join(
        p.get("text", "") for p in content if isinstance(p, dict)
    )
    assert "document" in text, "document non géré disparu sans aucune trace"


# ─────────────────── Chat → Anthropic (P4) ───────────────────


def test_p4_file_data_becomes_document_base64():
    """A9 (sens retour) : une part `file` en data URI redevient un `document`."""
    out = pm.openai_to_anthropic_request(
        {
            "model": "test",
            "max_tokens": 1024,
            "messages": [
                {
                    "role": "user",
                    "content": [
                        {"type": "text", "text": "résume"},
                        {
                            "type": "file",
                            "file": {"file_data": DATA_URI, "filename": "rapport.pdf"},
                        },
                    ],
                }
            ],
        }
    )
    blocks = out["messages"][0]["content"]
    docs = [b for b in blocks if isinstance(b, dict) and b.get("type") == "document"]
    assert docs, "la part file n'a pas produit de document Anthropic"
    assert docs[0]["source"]["type"] == "base64"
    assert docs[0]["source"]["data"] == PDF_B64
    assert docs[0]["source"]["media_type"] == "application/pdf"


def test_p4_file_id_becomes_document_file_id():
    """A9 : `file.file_id` → `document.source.file_id` (passthrough de référence)."""
    out = pm.openai_to_anthropic_request(
        {
            "model": "test",
            "max_tokens": 1024,
            "messages": [
                {
                    "role": "user",
                    "content": [
                        {"type": "file", "file": {"file_id": "file_xyz789"}}
                    ],
                }
            ],
        }
    )
    docs = [
        b
        for b in out["messages"][0]["content"]
        if isinstance(b, dict) and b.get("type") == "document"
    ]
    assert docs[0]["source"] == {"type": "file", "file_id": "file_xyz789"}


def test_p4_file_url_becomes_placeholder():
    """A9 : une URL de fichier Chat → placeholder Anthropic (pas d'équivalent
    direct sans `file_id`)."""
    out = pm.openai_to_anthropic_request(
        {
            "model": "test",
            "max_tokens": 1024,
            "messages": [
                {
                    "role": "user",
                    "content": [
                        {"type": "file", "file": {"file_data": "https://ex.com/a.pdf"}}
                    ],
                }
            ],
        }
    )
    blocks = out["messages"][0]["content"]
    text = " ".join(b.get("text", "") for b in blocks if isinstance(b, dict))
    assert "https://ex.com/a.pdf" in text, "l'URL a été perdue en P4"


# ─────────── Aller-retour : la fidélité qui compte ───────────


def test_document_base64_roundtrip_is_faithful():
    """A9 — LE test qui manquait : un document base64 qui fait
    Anthropic → Chat → Anthropic revient **identique**.

    C'est la propriété métier : un proxy qui perd ou corrompt un PDF au passage
    casse le client de façon invisible.
    """
    original_part = {
        "type": "document",
        "source": {
            "type": "base64",
            "media_type": "application/pdf",
            "data": PDF_B64,
        },
        "name": "rapport.pdf",
    }
    via_chat = pm.anthropic_to_openai(
        {
            "model": "test",
            "max_tokens": 1024,
            "messages": [{"role": "user", "content": [original_part]}],
        },
        "deepseek-v4-flash",
    )
    back = pm.openai_to_anthropic_request(
        {"model": "test", "max_tokens": 1024, "messages": via_chat["messages"]}
    )
    docs = [
        b
        for b in back["messages"][0]["content"]
        if isinstance(b, dict) and b.get("type") == "document"
    ]
    assert docs, "le document a disparu lors de l'aller-retour"
    assert docs[0]["source"]["data"] == PDF_B64, "les octets du document ont été altérés"
    assert docs[0]["source"]["media_type"] == "application/pdf"


def test_document_file_id_roundtrip_is_faithful():
    """Symétriquement : une référence `file_id` survit à l'aller-retour."""
    via_chat = pm.anthropic_to_openai(
        {
            "model": "test",
            "max_tokens": 1024,
            "messages": [
                {
                    "role": "user",
                    "content": [
                        {"type": "document", "source": {"type": "file", "file_id": "file_rt1"}}
                    ],
                }
            ],
        },
        "deepseek-v4-flash",
    )
    back = pm.openai_to_anthropic_request(
        {"model": "test", "max_tokens": 1024, "messages": via_chat["messages"]}
    )
    docs = [
        b
        for b in back["messages"][0]["content"]
        if isinstance(b, dict) and b.get("type") == "document"
    ]
    assert docs[0]["source"]["file_id"] == "file_rt1"


# ─────────── Responses (direction amont Anthropic) ───────────


def test_responses_input_file_url_becomes_document_url():
    """L15 (document URL → Responses) : `input_file.file_url` doit produire un
    `document` par URL côté Anthropic, pas un placeholder — ici l'équivalent
    EXISTE."""
    out = pm.openai_responses_to_anthropic(
        {
            "model": "test",
            "max_output_tokens": 1024,
            "input": [
                {
                    "role": "user",
                    "content": [
                        {
                            "type": "input_file",
                            "file_url": "https://ex.com/rapport.pdf",
                            "filename": "rapport.pdf",
                        }
                    ],
                }
            ],
        }
    )
    blocks = out["messages"][0]["content"]
    docs = [b for b in blocks if isinstance(b, dict) and b.get("type") == "document"]
    assert docs, "input_file par URL n'a pas produit de document"
    src = docs[0]["source"]
    assert src.get("type") == "url" and src.get("url") == "https://ex.com/rapport.pdf", (
        f"source inattendue : {src}"
    )


def test_responses_input_file_data_becomes_document_base64():
    """Symétrie : `input_file.file_data` (data URI) → document base64."""
    out = pm.openai_responses_to_anthropic(
        {
            "model": "test",
            "max_output_tokens": 1024,
            "input": [
                {
                    "role": "user",
                    "content": [
                        {"type": "input_file", "file_data": DATA_URI, "filename": "r.pdf"}
                    ],
                }
            ],
        }
    )
    docs = [
        b
        for b in out["messages"][0]["content"]
        if isinstance(b, dict) and b.get("type") == "document"
    ]
    assert docs[0]["source"]["type"] == "base64"
    assert docs[0]["source"]["data"] == PDF_B64


def test_responses_no_invented_mime_type_on_url_file():
    """Le schéma Responses n'a pas de `mime_type` : on n'en invente pas (400
    amont sinon)."""
    out = pm.openai_responses_to_anthropic(
        {
            "model": "test",
            "max_output_tokens": 1024,
            "input": [
                {
                    "role": "user",
                    "content": [
                        {"type": "input_file", "file_url": "https://ex.com/x.pdf"}
                    ],
                }
            ],
        }
    )
    docs = [
        b
        for b in out["messages"][0]["content"]
        if isinstance(b, dict) and b.get("type") == "document"
    ]
    assert "mime_type" not in docs[0]["source"]


# ─────────── Chat → Responses ───────────


def test_chat_file_becomes_input_file():
    """Chat → Responses : `file` → `input_file` (forme officielle du schéma)."""
    out = pm._chat_to_responses_request(
        {
            "model": "test",
            "max_tokens": 1024,
            "messages": [
                {
                    "role": "user",
                    "content": [
                        {"type": "text", "text": "résume"},
                        {"type": "file", "file": {"file_data": DATA_URI, "filename": "r.pdf"}},
                    ],
                }
            ],
        }
    )
    parts = out["input"][0]["content"]
    files = [p for p in parts if isinstance(p, dict) and p.get("type") == "input_file"]
    assert files, "la part file n'est pas devenue input_file"
    assert files[0].get("file_data") == DATA_URI


def test_all_document_directions_keep_a_trace_of_input():
    """Propriété transversale A9 : quelle que soit la forme d'entrée, la
    conversion **n'efface jamais** un document sans laisser de trace (octets,
    file_id, URL ou placeholder textuel).

    C'est le vrai risque métier : un document silencieusement perdu fait
    répondre le modèle à côté sans que rien ne le signale.
    """
    cases = [
        (
            "anthropic→chat base64",
            lambda: pm.anthropic_to_openai(
                {
                    "model": "t",
                    "max_tokens": 512,
                    "messages": [
                        {
                            "role": "user",
                            "content": [
                                {
                                    "type": "document",
                                    "source": {"type": "base64", "data": PDF_B64},
                                }
                            ],
                        }
                    ],
                },
                "deepseek-v4-flash",
            ),
            PDF_B64,
        ),
        (
            "anthropic→chat url",
            lambda: pm.anthropic_to_openai(
                {
                    "model": "t",
                    "max_tokens": 512,
                    "messages": [
                        {
                            "role": "user",
                            "content": [
                                {
                                    "type": "document",
                                    "source": {"type": "url", "url": "https://ex.com/z.pdf"},
                                }
                            ],
                        }
                    ],
                },
                "deepseek-v4-flash",
            ),
            "https://ex.com/z.pdf",
        ),
        (
            "chat→responses base64",
            lambda: pm._chat_to_responses_request(
                {
                    "model": "t",
                    "max_tokens": 512,
                    "messages": [
                        {
                            "role": "user",
                            "content": [
                                {"type": "file", "file": {"file_data": DATA_URI}}
                            ],
                        }
                    ],
                }
            ),
            PDF_B64,
        ),
    ]
    for label, build, needle in cases:
        out = build()
        dumped = repr(out["messages"]) if "messages" in out else repr(out["input"])
        assert needle in dumped, f"{label} : le document a disparu sans trace"
