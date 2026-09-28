"""FIX « tour vide » (2/2) : un placeholder de texte suit le rôle du message.

L'upstream ``/v1/responses`` refuse ``input_text`` sur un message ``assistant`` :

    content type `input_text` is not valid on `assistant` messages

Or le fallback de conversion émettait un placeholder ``input_text`` pour tout
bloc non reconnu — y compris dans un message ``assistant``. Un message assistant
Anthropic contenant ``tool_use`` après un ``text`` produisait donc :

    {"role": "assistant",
     "content": [{"type": "output_text", ...}, {"type": "input_text", "[tool_use]"}]}

→ 400 systématique, avalé en **tour vide** sur la jambe free (input ~46 000,
output ~40 tokens) : le compactage client recevait un résumé vide et la tâche se
figeait. Constaté sur les corps réels de ``logs/requests.db``.

Ces tests verrouillent la règle « le type de texte suit le rôle ».
"""
import json

import pytest

from app.protocol import mapping as pm


def _chat(role, blocks):
    return {
        "model": "muse-spark-1.3-contributor",
        "messages": [{"role": role, "content": blocks}],
    }


def _parts(role, blocks):
    """Parts émises pour le message — lève si le message a disparu."""
    out = pm._chat_to_responses_request(_chat(role, blocks))
    for it in out.get("input", []):
        if isinstance(it, dict) and it.get("role") == role:
            return it.get("content") or []
    raise AssertionError(f"aucun item {role!r} émis : {out.get('input')!r}")


# ── 1 : le cas réel — tool_use dans un message assistant ─────────────────
def test_assistant_tool_use_never_becomes_input_text():
    """Le bloc `tool_use` d'un assistant ne doit JAMAIS produire `input_text`."""
    parts = _parts(
        "assistant",
        [
            {"type": "text", "text": "Je regarde."},
            {"type": "tool_use", "id": "c1", "name": "read", "input": {"path": "a"}},
        ],
    )
    bad = [p for p in parts if p.get("type") == "input_text"]
    assert not bad, f"input_text interdit sur un assistant : {bad!r}"


@pytest.mark.parametrize("role", ["assistant"])
def test_assistant_placeholders_are_output_text(role):
    """Tout placeholder de texte d'un assistant est en `output_text`."""
    parts = _parts(role, [{"type": "text", "text": "ok"}, {"type": "tool_use", "id": "c1", "name": "read", "input": {}}])
    placeholders = [p for p in parts if p.get("type") in ("input_text", "output_text")]
    # Le texte légitime + le placeholder : tous deux output_text.
    assert all(p["type"] == "output_text" for p in placeholders), placeholders


# ── 2 : symétrie — un user garde bien input_text ─────────────────────────
def test_user_placeholders_stay_input_text():
    """Non-régression : sur un `user`, le placeholder reste `input_text`."""
    parts = _parts("user", [{"type": "video_url", "video_url": {"url": "x"}}])
    assert parts and parts[0]["type"] == "input_text", parts


def test_user_text_part_is_input_text():
    """Non-régression : le texte d'un `user` reste `input_text`."""
    parts = _parts("user", [{"type": "text", "text": "salut"}])
    assert parts == [{"type": "input_text", "text": "salut"}], parts


def test_assistant_text_part_is_output_text():
    """Non-régression : le texte d'un `assistant` reste `output_text`."""
    parts = _parts("assistant", [{"type": "text", "text": "salut"}])
    assert parts == [{"type": "output_text", "text": "salut"}], parts


# ── 3 : les trois placeholders sont concernés ────────────────────────────
@pytest.mark.parametrize(
    "block",
    [
        {"type": "video_url", "video_url": {"url": "http://x/v.mp4"}},      # vidéo
        {"type": "input_audio", "input_audio": {"data": "zz", "format": "ogg"}},  # audio hors set
        {"type": "document", "source": {"type": "base64"}},                # inconnu
        {"type": "image_url", "image_url": {"url": "https://x/i.png"}},    # image
        {"type": "input_audio", "input_audio": {"data": "AAA", "format": "wav"}},  # audio DANS le set
        {"type": "file", "file": {"file_data": "data:application/pdf;base64,AAA", "filename": "d.pdf"}},
        {"type": "file", "file": {"file_id": "f_1"}},
    ],
)
def test_every_assistant_placeholder_follows_the_role(block):
    """Aucun des placeholders ne doit violer la règle du rôle.

    Couvre aussi les parts multimodales : ``input_image`` / ``input_file`` /
    ``input_audio`` ne sont valides que sur un message **non-assistant**.
    """
    parts = _parts("assistant", [block])
    assert parts, "le bloc doit produire un placeholder (jamais de drop silencieux)"
    forbidden = {"input_text", "input_image", "input_file", "input_audio"}
    assert all(p["type"] not in forbidden for p in parts), parts


@pytest.mark.parametrize(
    "block,expected",
    [
        ({"type": "image_url", "image_url": {"url": "https://x/i.png"}}, "input_image"),
        ({"type": "input_audio", "input_audio": {"data": "AAA", "format": "wav"}}, "input_audio"),
        ({"type": "file", "file": {"file_id": "f_1"}}, "input_file"),
    ],
)
def test_multimodal_parts_still_pass_on_a_user(block, expected):
    """Non-régression : sur un `user` les parts multimodales passent intactes."""
    parts = _parts("user", [block])
    assert parts and parts[0]["type"] == expected, parts


# ── 4 : invariant global sur une conversation complète ───────────────────
def test_no_input_text_on_any_assistant_across_a_conversation():
    """Invariant : sur toute la conversation, aucun assistant ne porte input_text."""
    chat = {
        "model": "muse-spark-1.3-contributor",
        "messages": [
            {"role": "user", "content": "fais un truc"},
            {"role": "assistant", "content": [
                {"type": "text", "text": "Je regarde."},
                {"type": "tool_use", "id": "c1", "name": "read", "input": {}},
                {"type": "tool_use", "id": "c2", "name": "bash", "input": {}},
            ]},
            {"role": "user", "content": [{"type": "tool_result", "tool_use_id": "c1", "content": "ok"}]},
            {"role": "assistant", "content": [{"type": "video_url", "video_url": {"url": "x"}}]},
        ],
    }
    out = pm._chat_to_responses_request(chat)
    offenders = []
    for it in out.get("input", []):
        if isinstance(it, dict) and it.get("role") == "assistant":
            for b in it.get("content") or []:
                if isinstance(b, dict) and b.get("type") == "input_text":
                    offenders.append(it)
    assert not offenders, f"l'upstream renverrait 400 : {offenders!r}"


def test_emitted_body_is_wire_serializable():
    """Le corps final reste sérialisable après le fix."""
    out = pm._chat_to_responses_request(
        _chat("assistant", [{"type": "text", "text": "a"}, {"type": "tool_use", "id": "c", "name": "read", "input": {}}])
    )
    assert json.loads(json.dumps(out))["input"]


# ── 5 : invariant de schéma sur TOUTE la matrice role × type ─────────────
# Règle amont : un assistant n'accepte que du texte de sortie ; les parts
# `input_*` sont réservées aux messages non-assistant.
_ALLOWED = {
    "user": {"input_text", "input_image", "input_file", "input_audio"},
    "developer": {"input_text", "input_image", "input_file", "input_audio"},
    "system": {"input_text", "input_image", "input_file", "input_audio"},
    "assistant": {"output_text", "refusal"},
}

_ALL_BLOCKS = [
    {"type": "text", "text": "hello"},
    {"type": "image_url", "image_url": {"url": "https://x/i.png"}},
    {"type": "input_audio", "input_audio": {"data": "AAA", "format": "wav"}},
    {"type": "input_audio", "input_audio": {"data": "AAA", "format": "ogg"}},
    {"type": "video_url", "video_url": {"url": "https://x/v.mp4"}},
    {"type": "tool_use", "id": "c1", "name": "read", "input": {}},
    {"type": "document", "source": {"type": "base64"}},
    {"type": "thinking", "thinking": "..."},
    {"type": "redacted_thinking", "data": "..."},
]


@pytest.mark.parametrize("role", ["user", "assistant"])
def test_schema_invariant_holds_for_every_block(role):
    """Aucune combinaison role × type ne doit violer le schéma /responses."""
    offenders = []
    for blk in _ALL_BLOCKS:
        out = pm._chat_to_responses_request(_chat(role, [blk]))
        for it in out.get("input", []):
            if not isinstance(it, dict) or it.get("role") != role:
                continue
            for p in it.get("content") or []:
                if isinstance(p, dict) and p.get("type") not in _ALLOWED[role]:
                    offenders.append((blk.get("type"), p.get("type")))
    assert not offenders, f"role={role} : combinaisons invalides {offenders!r}"