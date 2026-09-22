"""[Lot L4 — Tools : matrice complète des 6 chemins] Matrice `tools[]`.

Le plan (PLAN_AUDIT_CONVERSIONS_2026-09-10.md §L4) exige, **pour les 6 chemins**,
la couverture de : `tools[]` / `tool_choice` / `strict` / **nom long** /
`input_schema` invalide — et que « chaque perte silencieuse soit soit corrigée,
soit **déclarée** ».

Ce fichier est le livrable manquant de L4 : `test_tools_matrix.py` n'existait pas
dans le dépôt. Il couvre les 6 chemins au niveau **convertisseur de requête**,
c'est-à-dire l'objet qui part réellement vers l'amont :

    P1  /v1/messages         → anthropic  passthrough
    P2  /v1/messages         → openai     `anthropic_to_openai`
    P3  /v1/chat/completions → openai     passthrough
    P4  /v1/chat/completions → anthropic  `openai_to_anthropic_request`
    P5  /v1/responses        → openai     `anthropic_to_openai` + `_chat_to_responses_request`
    P6  /v1/responses        → anthropic  `openai_responses_to_anthropic`

Limite de longueur des noms, par cible — c'est le cœur de l'axe « nom long » :
Anthropic accepte **200** caractères, OpenAI n'en accepte que **64** (et la
convention Responses également 64). Un nom valide côté client Anthropic peut donc
être **invalide** une fois routé vers une cible Chat.

**Écart A8 — CORRIGÉ (lot L4).** Le nom long n'était sanitizé que sur les chemins
**Responses** (`sanitize_tool_names` appelé en `_sanitize_native_responses_request`
et `_chat_to_responses_request`) ; sur **P2**, un nom de 80 caractères partait **tel
quel** vers la cible Chat, qui n'en accepte que 64. La correction câble la même
sanitize sur les chemins **Chat** :

1. `_sanitize_chat_tools` projette les noms (`function.name`) vers la forme
   attendue par `sanitize_tool_names`, réutilise la **même** logique (digest,
   collisions, exclusions `web_*`) et réécrit les noms — une seule source de
   vérité, aucune duplication de la règle de raccourcissement ;
2. `_remap_chat_history_names` fait suivre l'historique
   (`assistant.tool_calls[].function.name`), sinon le tour N+1 rejouerait le nom
   long et l'amont répondrait 400 ;
3. `_remap_chat_tool_choice` fait suivre le `tool_choice` nommé, en **conservant**
   la forme Chat ;
4. la map `{short: original}` repart au handler via `_TOOL_NAME_MAP_KEY` et le
   retour restaure le nom d'origine — non-stream (`openai_to_anthropic`) **et**
   streaming (branche `tool_calls` du flux P2).

Le test de l'axe portait un ``xfail(strict=True)`` tant que le défaut était là ;
il est passé en **XPASS** dès le câblage, ce qui a forcé la levée du marqueur —
le garde-fou a fonctionné comme prévu. Les tests de ce fichier couvrent désormais
aussi le **retour** (restauration), pas seulement l'aller.
"""

from __future__ import annotations

import json

import pytest

from protocol_mapping import (
    _TOOL_NAME_MAP_KEY,
    _chat_to_responses_request,
    anthropic_to_openai,
    openai_responses_to_anthropic,
    openai_to_anthropic,
    openai_to_anthropic_request,
    sanitize_tool_names,
)

# Longueurs de référence des deux contrats.
ANTHROPIC_NAME_MAX = 200
OPENAI_NAME_MAX = 64

LONG_NAME = "a" * 80  # valide Anthropic (≤200), invalide Chat (>64)
VALID_NAME = "get_weather"


def anthro_tool(name: str = VALID_NAME, schema: dict | None = None) -> dict:
    """Un outil au format Anthropic (contrat client `/v1/messages`)."""
    return {
        "name": name,
        "description": "Météo d'une ville.",
        "input_schema": schema
        if schema is not None
        else {
            "type": "object",
            "properties": {"city": {"type": "string"}},
            "required": ["city"],
        },
    }


def chat_tool(name: str = VALID_NAME, schema: dict | None = None) -> dict:
    """Un outil au format Chat Completions (contrat client `/v1/chat/completions`)."""
    return {
        "type": "function",
        "function": {
            "name": name,
            "description": "Météo d'une ville.",
            "parameters": schema
            if schema is not None
            else {"type": "object", "properties": {"city": {"type": "string"}}, "required": ["city"]},
        },
    }


def responses_tool(name: str = VALID_NAME, schema: dict | None = None) -> dict:
    """Un outil au format Responses (contrat client `/v1/responses`)."""
    return {
        "type": "function",
        "name": name,
        "description": "Météo d'une ville.",
        "parameters": schema
        if schema is not None
        else {"type": "object", "properties": {"city": {"type": "string"}}, "required": ["city"]},
    }


def _anthro_body(tools=None, tool_choice=None) -> dict:
    body: dict = {
        "model": "m",
        "max_tokens": 256,
        "stream": False,
        "messages": [{"role": "user", "content": "Quel temps à Paris ?"}],
    }
    if tools is not None:
        body["tools"] = tools
    if tool_choice is not None:
        body["tool_choice"] = tool_choice
    return body


def _chat_body(tools=None, tool_choice=None) -> dict:
    body: dict = {
        "model": "m",
        "max_tokens": 256,
        "stream": False,
        "messages": [{"role": "user", "content": "Quel temps à Paris ?"}],
    }
    if tools is not None:
        body["tools"] = tools
    if tool_choice is not None:
        body["tool_choice"] = tool_choice
    return body


def _responses_body(tools=None, tool_choice=None) -> dict:
    body: dict = {
        "model": "m",
        "input": [{"role": "user", "content": "Quel temps à Paris ?"}],
    }
    if tools is not None:
        body["tools"] = tools
    if tool_choice is not None:
        body["tool_choice"] = tool_choice
    return body


# ───────────────────────────── helpers par chemin ─────────────────────────────
# Chaque helper retourne (outils_vus_par_l_amont, body_conserve).
# `None` pour les outils signifie « le chemin n'expose pas cette notion ».


def _p1(tools, tool_choice=None):
    """P1 — passthrough Anthropic : le corps part inchangé."""
    body = _anthro_body(tools, tool_choice)
    return body.get("tools"), body


def _p2(tools, tool_choice=None):
    """P2 — Anthropic → Chat."""
    out = anthropic_to_openai(_anthro_body(tools, tool_choice), "glm-5")
    return out.get("tools"), out


def _p3(tools, tool_choice=None):
    """P3 — passthrough Chat : le corps part inchangé."""
    body = _chat_body(tools, tool_choice)
    return body.get("tools"), body


def _p4(tools, tool_choice=None):
    """P4 — Chat → Anthropic."""
    out = openai_to_anthropic_request(_chat_body(tools, tool_choice))
    return out.get("tools"), out


def _p5(tools, tool_choice=None):
    """P5 — Chat (interne) → Responses."""
    out = _chat_to_responses_request(_chat_body(tools, tool_choice))
    return out.get("tools"), out


def _p6(tools, tool_choice=None):
    """P6 — Responses → Anthropic."""
    out = openai_responses_to_anthropic(_responses_body(tools, tool_choice))
    return out.get("tools"), out


PATHS = {
    "p1": (_p1, anthro_tool, "anthropic"),
    "p2": (_p2, anthro_tool, "openai"),
    "p3": (_p3, chat_tool, "openai"),
    "p4": (_p4, chat_tool, "anthropic"),
    "p5": (_p5, chat_tool, "openai"),
    "p6": (_p6, responses_tool, "anthropic"),
}

# Nom du champ qui porte le nom d'outil dans le format de SORTIE du chemin.
_OUT_NAME_FIELD = {
    "p1": "name",
    "p2": "function.name",
    "p3": "function.name",
    "p4": "name",
    "p5": "name",
    "p6": "name",
}


def tool_name_of(entry: dict, path: str) -> str:
    """Extrait le nom d'outil d'une entrée, quel que soit le format du chemin."""
    field = _OUT_NAME_FIELD[path]
    if field == "function.name":
        return (entry.get("function") or {}).get("name", "")
    return entry.get("name", "")


def inner_of(entry: dict, path: str) -> dict:
    """Le dict qui porte description/schéma, selon le format de sortie du chemin."""
    return entry.get("function", entry) if path in ("p2", "p3") else entry


# ─────────────────────────────── axes de la matrice ───────────────────────────


@pytest.mark.parametrize("path", sorted(PATHS))
def test_axis_tool_name_and_schema_reach_upstream(path):
    """Axe `tools[]` — nom, description et schéma survivent à la conversion.

    Le schéma doit arriver sous la clé du **format cible** (`parameters` vers
    Chat, `input_schema` vers Anthropic) : envoyer la mauvaise clé est un 400
    silencieux côté client (le modèle ne voit simplement aucun outil).
    """
    convert, make_tool, _target = PATHS[path]
    tools, _body = convert([make_tool()])

    assert tools, f"{path}: les outils ont disparu de la requête amont"
    assert len(tools) == 1
    assert tool_name_of(tools[0], path) == VALID_NAME
    assert inner_of(tools[0], path).get("description") == "Météo d'une ville."

    # Le schéma est présent sous la bonne clé, et pas sous l'autre.
    if path in ("p2", "p3", "p5"):
        assert "parameters" in tools[0].get("function", tools[0])
    else:
        assert "input_schema" in tools[0]


@pytest.mark.parametrize("path", sorted(PATHS))
def test_axis_input_schema_invalid_does_not_crash(path):
    """Axe `input_schema` invalide — aucun chemin ne doit lever.

    Un client peut envoyer un schéma vide, `None`, ou une chaîne. Le proxy doit
    **ne jamais lever** : une exception ici donne un 500 opaque au lieu d'une
    erreur exploitable.

    Sur les chemins de **conversion** (P2/P4/P5/P6), le schéma doit ressortir
    **structurellement valide** (un dict) : un upstream strict rejette un
    `parameters` qui n'est pas un objet. Sur les chemins de **passthrough**
    (P1/P3), le proxy n'a pas à réparer le corps du client — il le transmet tel
    quel, c'est le contrat du passthrough ; on vérifie donc seulement l'absence
    de crash et la survie de l'outil.
    """
    convert, make_tool, _target = PATHS[path]
    for bad in ({}, None, "not-a-schema", []):
        tools, _body = convert([make_tool(schema=bad)])
        assert tools, f"{path}: outil perdu avec un schéma {bad!r}"
        if path in ("p1", "p3"):
            continue  # passthrough : transmis tel quel, par contrat
        field = "parameters" if path in ("p2", "p5") else "input_schema"
        value = inner_of(tools[0], path).get(field)
        assert isinstance(value, dict), (
            f"{path}: le schéma {bad!r} est ressorti en {type(value).__name__}, "
            "attendu dict — un upstream strict rejetterait la requête"
        )


@pytest.mark.parametrize("path", ["p2", "p4", "p5", "p6"])
def test_axis_tool_choice_named_tool_is_translated(path):
    """Axe `tool_choice` — le choix d'outil nommé suit le format cible.

    Un `tool_choice` nommé qui ne suit pas la conversion désigne un outil
    inexistant côté amont : l'upstream répond 400, ou pire, laisse le modèle
    choisir librement alors que le client avait contraint le choix.
    """
    convert, make_tool, _target = PATHS[path]
    if path in ("p2", "p5"):
        choice = {"type": "tool", "name": VALID_NAME}
    elif path == "p4":
        choice = {"type": "function", "function": {"name": VALID_NAME}}
    else:  # p6 — Responses
        choice = {"type": "function", "name": VALID_NAME}

    _tools, body = convert([make_tool()], choice)
    emitted = body.get("tool_choice")
    assert emitted is not None, f"{path}: tool_choice a été perdu"

    if path == "p2":
        assert emitted == {"type": "function", "function": {"name": VALID_NAME}}
    elif path == "p4":
        assert emitted == {"type": "tool", "name": VALID_NAME}
    else:
        # P5/P6 : le choix nommé doit désigner le même outil, quel que soit le
        # nom de la clé retenue par le format Responses.
        assert VALID_NAME in json.dumps(emitted), f"{path}: le nom d'outil a disparu du tool_choice"


@pytest.mark.parametrize("path", ["p4", "p6"])
def test_axis_long_name_is_legal_towards_anthropic(path):
    """Axe « nom long » — vers Anthropic, 200 caractères sont légitimes.

    Anthropic accepte jusqu'à 200 caractères : **ne rien raccourcir** ici est le
    comportement correct, et raccourcir serait une perte de contrat inutile.
    """
    convert, make_tool, _target = PATHS[path]
    tools, _body = convert([make_tool(name=LONG_NAME)])

    assert tool_name_of(tools[0], path) == LONG_NAME, (
        f"{path}: le nom a été altéré alors que la cible Anthropic accepte {ANTHROPIC_NAME_MAX} caractères"
    )
    assert len(LONG_NAME) <= ANTHROPIC_NAME_MAX


@pytest.mark.parametrize("path", ["p5"])
def test_axis_long_name_is_sanitized_towards_chat_via_responses(path):
    """Axe « nom long » — les chemins Responses sanitizent bien à 64 caractères.

    C'est le comportement de référence : `sanitize_tool_names` raccourcit et
    **retourne une map** pour que le nom d'origine soit restitué au client.
    """
    convert, make_tool, _target = PATHS[path]
    tools, body = convert([make_tool(name=LONG_NAME)])

    emitted = tool_name_of(tools[0], path)
    assert len(emitted) <= OPENAI_NAME_MAX, f"{path}: nom non sanitizé ({len(emitted)} caractères)"
    assert emitted != LONG_NAME
    # La map de restauration doit exister pour que le client revoie SON nom.
    assert body.get(_TOOL_NAME_MAP_KEY), f"{path}: sanitize sans map de restauration"


def test_axis_long_name_is_sanitized_towards_chat_on_p2():
    """Axe « nom long » sur P2 — écart A8 **corrigé** (lot L4).

    Un nom de 80 caractères est valide côté Anthropic (limite 200) mais dépasse
    la limite Chat de 64 : il doit être ramené à ≤64 vers une cible Chat, **avec**
    une map de restauration — sans quoi le client recevrait un nom raccourci
    qu'il ne reconnaît pas (perte silencieuse).

    Historique : ce test portait un ``xfail(strict=True)`` tant que la sanitize
    n'était pas câblée sur les chemins Chat. Il est passé en XPASS au câblage,
    ce qui a forcé la levée du marqueur — c'est le garde-fou qui a fonctionné.
    """
    tools, body = _p2([anthro_tool(name=LONG_NAME)])

    emitted = tool_name_of(tools[0], "p2")
    assert len(emitted) <= OPENAI_NAME_MAX, (
        f"nom de {len(emitted)} caractères envoyé à une cible Chat (limite {OPENAI_NAME_MAX})"
    )
    assert body.get(_TOOL_NAME_MAP_KEY), "sanitize sans map : le nom client serait irrécupérable"
    # La map doit permettre de retrouver EXACTEMENT le nom envoyé par le client.
    assert body[_TOOL_NAME_MAP_KEY][emitted] == LONG_NAME


def test_a8_p2_tool_choice_follows_the_rename():
    """A8 — un `tool_choice` nommé doit désigner le nom **réellement envoyé**.

    Si les outils sont raccourcis mais que `tool_choice` garde le nom long,
    l'amont cherche un outil inexistant (400/422). La forme Chat
    ``{"function": {"name"}}`` doit être conservée, pas convertie en forme
    Responses.
    """
    tools, body = _p2([anthro_tool(name=LONG_NAME)], tool_choice={"type": "tool", "name": LONG_NAME})

    emitted = tool_name_of(tools[0], "p2")
    choice = body.get("tool_choice")
    assert isinstance(choice, dict) and isinstance(choice.get("function"), dict), (
        f"tool_choice doit rester en forme Chat, reçu : {choice!r}"
    )
    assert choice["function"]["name"] == emitted, (
        "tool_choice désigne un nom différent de celui envoyé — l'amont répondrait 400"
    )


def test_a8_p2_history_tool_calls_follow_the_rename():
    """A8 — l'historique des tours précédents suit le même rename.

    Un `tool_use` long dans l'historique devient un `tool_calls[].function.name`
    côté Chat : sans rename, le tour N+1 rejoue le nom long et l'amont répond
    400 `name must be at most 64 characters`.
    """
    body_in = _anthro_body([anthro_tool(name=LONG_NAME)])
    body_in["messages"] = [
        {"role": "user", "content": "Quel temps à Paris ?"},
        {"role": "assistant", "content": [{"type": "tool_use", "id": "t1", "name": LONG_NAME, "input": {}}]},
        {"role": "user", "content": [{"type": "tool_result", "tool_use_id": "t1", "content": "ok"}]},
    ]
    out = anthropic_to_openai(body_in, "glm-5")

    emitted = tool_name_of(out["tools"][0], "p2")
    hist = [m for m in out["messages"] if m.get("tool_calls")]
    assert hist, "aucun message assistant.tool_calls dans l'historique converti"
    hist_name = hist[0]["tool_calls"][0]["function"]["name"]
    assert hist_name == emitted, (
        f"historique non remappé ({hist_name!r}) alors que tools[] envoie {emitted!r} — "
        "l'amont répondrait 400 sur le tour suivant"
    )


def test_a8_restore_returns_the_original_name_on_the_way_back():
    """A8 — le retour doit restaurer le nom d'origine, pas le nom raccourci.

    C'est la moitié « restore » de l'écart : sanitizer sans restaurer
    remplacerait une perte (400 amont) par une autre (le client ne reconnaît
    plus son outil).
    """
    tools, body = _p2([anthro_tool(name=LONG_NAME)])
    emitted = tool_name_of(tools[0], "p2")

    upstream = {
        "choices": [
            {
                "message": {
                    "role": "assistant",
                    "content": None,
                    "tool_calls": [{"id": "t1", "type": "function", "function": {"name": emitted, "arguments": "{}"}}],
                },
                "finish_reason": "tool_calls",
            }
        ],
        "usage": {},
    }
    out = openai_to_anthropic(upstream, "glm-5", body[_TOOL_NAME_MAP_KEY])

    names = [b.get("name") for b in out["content"] if b.get("type") == "tool_use"]
    assert names == [LONG_NAME], f"nom non restauré côté client : {names!r}"


def test_a8_no_map_when_nothing_was_renamed():
    """A8 — fast-path : aucun renommage ⇒ aucune map posée.

    Le marqueur interne ne doit pas apparaître sur les requêtes ordinaires : il
    voyagerait sinon inutilement (et un consommateur pourrait croire à un
    renommage qui n'a pas eu lieu).
    """
    tools, body = _p2([anthro_tool(name=VALID_NAME)])

    assert tool_name_of(tools[0], "p2") == VALID_NAME, "un nom valide ne doit pas être renommé"
    assert _TOOL_NAME_MAP_KEY not in body, "map posée alors qu'aucun renommage n'a eu lieu"


def test_a8_map_never_reaches_the_wire():
    """A8 — la map est un détail interne : elle ne doit jamais partir vers l'amont.

    On vérifie sur le corps **sérialisé** (dernier kilomètre), pas sur le dict :
    c'est la seule preuve qui vaille pour un champ interne.
    """
    import opencode as oc

    _tools, body = _p2([anthro_tool(name=LONG_NAME)])
    raw = oc._serialize_json_body(body).decode("utf-8")

    assert _TOOL_NAME_MAP_KEY not in raw, "clé interne sérialisée sur le wire"
    assert LONG_NAME not in raw, "nom long présent sur le wire malgré la sanitize"
    # La requête reste par ailleurs intacte et exploitable.
    assert json.loads(raw)["model"]


def test_axis_long_name_sanitize_is_reversible():
    """La sanitize des noms longs est **réversible** — sinon c'est une perte.

    Vérifie le primitive partagé : le nom d'origine est retrouvable depuis la
    map, et la longueur respecte la limite Chat.
    """
    out, name_map = sanitize_tool_names([{"name": LONG_NAME}])

    short = out[0]["name"]
    assert len(short) <= OPENAI_NAME_MAX
    assert name_map[short] == LONG_NAME
    assert long_name_map_is_total(name_map, [LONG_NAME])


def long_name_map_is_total(name_map: dict, originals: list) -> bool:
    """La map couvre bien chaque original (aucun nom irrécupérable)."""
    return sorted(name_map.values()) == sorted(originals)


@pytest.mark.parametrize("path", ["p2", "p4", "p5", "p6"])
def test_axis_strict_is_never_fabricated(path):
    """Axe `strict` — aucun chemin ne doit INVENTER `strict: true`.

    `strict` est une garantie de conformité de schéma côté OpenAI. L'émettre sans
    que le client l'ait demandé change la sémantique de la requête (et peut faire
    rejeter un schéma pourtant accepté en mode souple). Le proxy ne doit le
    transmettre que s'il l'a reçu.
    """
    convert, make_tool, _target = PATHS[path]
    tools, _body = convert([make_tool()])
    inner = tools[0].get("function", tools[0])

    assert inner.get("strict") is not True, (
        f"{path}: `strict: true` fabriqué alors que le client ne l'a pas demandé"
    )


# ── Parité tool_choice (toutes formes, tous chemins) ──────────────────────


def test_tool_choice_none_survives_anthropic_to_chat():
    """`none` ≠ `auto` : l'interdiction d'outil ne doit pas devenir un choix libre."""
    _tools, body = _p2([anthro_tool()], tool_choice={"type": "none"})
    assert body.get("tool_choice") == "none"


def test_tool_choice_chat_form_passthrough_anthropic_to_chat():
    """Forme Chat déjà : passthrough (le rename suit séparément)."""
    tc = {"type": "function", "function": {"name": "get_weather"}}
    _tools, body = _p2([anthro_tool()], tool_choice=tc)
    assert body.get("tool_choice") == tc


@pytest.mark.parametrize(
    ("incoming", "expected"),
    [("auto", "auto"), ("any", "required"), ("none", "none"), ("required", "required")],
)
def test_tool_choice_strings_mapped_anthropic_to_chat(incoming, expected):
    """Strings : `any` n'existe pas côté Chat → `required`."""
    _tools, body = _p2([anthro_tool()], tool_choice=incoming)
    assert body.get("tool_choice") == expected


@pytest.mark.parametrize(
    ("incoming", "expected"),
    [
        ({"type": "function", "function": {"name": "w"}}, {"type": "tool", "name": "w"}),
        ({"type": "any"}, {"type": "any"}),
        ({"type": "auto"}, {"type": "auto"}),
        ({"type": "none"}, {"type": "none"}),
        ("required", {"type": "any"}),
        ("auto", {"type": "auto"}),
        ("none", {"type": "none"}),
    ],
)
def test_tool_choice_mapped_chat_to_anthropic(incoming, expected):
    """Vers Anthropic : objets {type}, jamais de string nue (400 amont sinon)."""
    _tools, body = _p4([chat_tool()], tool_choice=incoming)
    assert body.get("tool_choice") == expected


def test_tool_choice_nested_name_fallback_responses_to_anthropic():
    """Forme Chat imbriquée arrivant sur la voie Responses→Anthropic : nom retrouvé."""
    from app.protocol.mapping import openai_responses_to_anthropic

    out = openai_responses_to_anthropic(
        _responses_body(
            tools=[responses_tool("w")],
            tool_choice={"type": "function", "function": {"name": "w"}},
        )
    )
    assert out.get("tool_choice") == {"type": "tool", "name": "w"}


def test_developer_message_merged_into_system_chat_to_anthropic():
    """`developer` = `system` sous un autre nom : contenu préservé, pas droppé."""
    body = _chat_body()
    body["messages"] = [
        {"role": "developer", "content": "consigne dev"},
        {"role": "user", "content": "hi"},
    ]
    from app.protocol.mapping import openai_to_anthropic_request

    out = openai_to_anthropic_request(body)
    assert "consigne dev" in out.get("system", "")
    assert all(m.get("role") in ("user", "assistant") for m in out.get("messages", []))


# ── Parité tool_choice : formes objet vers Anthropic ──────────────────────


@pytest.mark.parametrize(
    ("incoming", "expected"),
    [
        ({"type": "auto"}, {"type": "auto"}),
        ({"type": "none"}, {"type": "none"}),
        ({"type": "any"}, {"type": "any"}),
    ],
)
def test_tool_choice_dict_forms_mapped_p6(incoming, expected):
    """P6 : dict auto/any/none → objet {type}, jamais de string nue."""
    from app.protocol.mapping import openai_responses_to_anthropic

    out = openai_responses_to_anthropic(
        _responses_body(tools=[responses_tool()], tool_choice=incoming)
    )
    assert out.get("tool_choice") == expected
