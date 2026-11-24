"""
Protocol mapping — relais de champs optionnels et remap d'historique/outils (chat & responses).

Découpe de app.protocol.mapping (T213).
"""

import copy
import hashlib
import json
import re
import time
import uuid
from collections import OrderedDict
from typing import Any

# [Plan perf-fiabilité Lot 1, décision 2026-09-06] FAIL FAST : orjson est
# épinglé en dur (requirements.txt:19 orjson==3.11.7) — import direct, crash
# net au boot si absent. Un fallback silencieux sur stdlib json rendrait le
# proxy 5-10x plus lent sans aucun signal.
import orjson as _orjson  # type: ignore

from config import CACHE_MIN_PROMPT_SIZE, yaml_get

# [Lot L2] Source unique de vérité pour l'effort (A1/A2/A13/A14/A15/A23).
# ``config.effort_policy`` n'importe que ``config.effort_caps`` (lui-même
# réduit à ``yaml_get``) + stdlib : aucun coût de boot (contrainte plan §7.4),
# aucun risque de cycle ``config/*``.
from config.effort_policy import resolve_effort as _resolve_effort
from config.limits import clamp_output_token_limit as _clamp_output_token_limit
from dashboard.display import debug as _debug
from dashboard.display import log as _log

from .tool_aliases import adapt_tool_call, dropped_tool_call_text, tool_call_supported

_cfg_settings: Any
try:
    import config.settings as _cfg_settings  # type: ignore[assignment]
except ImportError:  # pragma: no cover

    class _CfgFallback:
        DEBUG = False

    _cfg_settings = _CfgFallback()

# tiktoken — LAZY (Phase 3 chantier boot) : ce module n'utilise plus
# _encoding (doublon mort du singleton d'opencode.py) ; get_encoding()
# coûtait 1-2 s à froid à CHAQUE import. Nom conservé pour compat.
_encoding: Any = None


def _get_encoding() -> Any:
    global _encoding
    if _encoding is None:
        try:
            import tiktoken

            _encoding = tiktoken.get_encoding("cl100k_base")
        except Exception:
            _encoding = None
    return _encoding


# ── orjson fast-path (5-10x vs stdlib json on large bodies) ──
# (import fail-fast en tête de fichier — cf. note Lot 1 ci-dessus)


def _json_loads(b: bytes | str, **kw):
    if isinstance(b, str):
        b = b.encode()
    return _orjson.loads(b)


def _json_dumps(obj, **kw) -> bytes:
    if kw.get("indent") is not None:
        return json.dumps(obj, ensure_ascii=False, indent=kw.get("indent"), default=str).encode()
    return _orjson.dumps(obj)


def _json_dumps_str(obj, **kw) -> str:
    if kw.get("indent") is not None:
        return json.dumps(obj, ensure_ascii=False, indent=kw.get("indent"), default=str)
    if kw:
        return _orjson.dumps(obj).decode()
    return _orjson.dumps(obj).decode()


_JSON_LIB = "orjson"

def filter_anthropic_request_body(body: dict) -> dict:
    """Filtre vide sur un corps requête Anthropic (port normalizeMessages).

    Retire les parts ``text:""`` / ``thinking`` vides non signés et les
    messages vidés ; ``system`` liste traité pareil (chaîne vide = absent).
    Ne mute JAMAIS l'entrée (copie défensive) et ne renvoie jamais un corps
    sans messages (repli entrée intacte). No-op si ``_PARITY_DISABLED``.
    """
    if _PARITY_DISABLED or not isinstance(body, dict):
        return body
    try:
        msgs = body.get("messages")
        system = body.get("system")
        new_msgs = filter_empty_anthropic_messages(msgs) if isinstance(msgs, list) else msgs
        new_system: object = system
        if isinstance(system, list):
            _kept = filter_empty_anthropic_messages([{"role": "system", "content": system}])
            new_system = _kept[0]["content"] if _kept else None
        elif isinstance(system, str) and system == "":
            new_system = None
        if new_msgs is msgs and (new_system is system or new_system == system):
            return body
        if isinstance(msgs, list) and not new_msgs:
            return body
        out = dict(body)
        if isinstance(msgs, list):
            out["messages"] = new_msgs
        if new_system is None:
            out.pop("system", None)
        elif new_system is not system:
            out["system"] = new_system
        return out
    except Exception:
        return body


def _remap_responses_history_names(inp: list, name_map: dict) -> dict:
    """P0-1 [msg_18d502b1e0b40-e] : l'historique multi-tours
    (input[].function_call.name) suit le même rename que tools[] — sinon le
    tour N+1 rejoue le nom original (>64) et l'upstream répond 400
    `name must be at most 64 characters` sur chaque station.

    Mute les items function_call de `inp` + complète `name_map` (noms hors
    tools[] → short défensif enregistré pour le restore-retour). Idempotent :
    un nom déjà short/valide est laissé inchangé.
    """
    if not isinstance(inp, list) or not inp or not isinstance(name_map, dict):
        return name_map
    inv: dict[str, str] = {o: s for s, o in name_map.items() if isinstance(s, str) and isinstance(o, str)}
    for item in inp:
        if not isinstance(item, dict) or item.get("type") != "function_call":
            continue
        name = item.get("name", "")
        if not isinstance(name, str) or not name:
            continue
        short = inv.get(name)
        if short is None and (len(name) > TOOL_NAME_MAX_LEN or _TOOL_NAME_RE.search(name)):
            short = _register_defensive_short(name, name_map)
            inv[name] = short
        if short is not None:
            item["name"] = short
    return name_map


def _remap_responses_tool_choice(tc, name_map: dict | None):
    """P0-2 : toute forme nommée de tool_choice suit le rename aller —
    OpenAI {"type": "function", "function": {"name"}}, Responses natif
    {"type": "function", "name"}, string. Copie défensive (jamais de mutation
    du caller) ; nom >64 hors map → short défensif enregistré."""
    if isinstance(tc, str):
        if not isinstance(name_map, dict) or not tc:
            return tc
        short = _inverse_tool_name_lookup(name_map, tc)
        if short is not None:
            return short
        if len(tc) > TOOL_NAME_MAX_LEN or _TOOL_NAME_RE.search(tc):
            return _register_defensive_short(tc, name_map)
        return tc
    if not isinstance(tc, dict):
        return tc
    fn = tc.get("function")
    if isinstance(fn, dict) and isinstance(fn.get("name"), str):
        # Forme OpenAI {"type": "function", "function": {"name"}} (produite par
        # anthropic_to_openai) → normalisée Responses {"type": "function",
        # "name"} comme historiquement (format wire attendu par l'upstream).
        name = fn.get("name", "")
        if not name or not isinstance(name_map, dict):
            return {"type": "function", "name": name}
        short = _inverse_tool_name_lookup(name_map, name)
        if short is None and (len(name) > TOOL_NAME_MAX_LEN or _TOOL_NAME_RE.search(name)):
            short = _register_defensive_short(name, name_map)
        return {"type": "function", "name": short if short is not None else name}
    if isinstance(tc.get("name"), str):
        # Forme Responses native {"type": "function", "name"} (+ tool_choice
        # verbatim) : copie défensive, remap en place.
        tc = dict(tc)
        name = tc.get("name", "")
        if not name or not isinstance(name_map, dict):
            return tc
        short = _inverse_tool_name_lookup(name_map, name)
        if short is None and (len(name) > TOOL_NAME_MAX_LEN or _TOOL_NAME_RE.search(name)):
            short = _register_defensive_short(name, name_map)
        if short is not None:
            tc["name"] = short
        return tc
    return tc


# [E3] Rangs de ponctuation au niveau primaire de la collation du client officiel.
# « _ » avant « - » (mesuré : `"_x".localeCompare("-x") === -1`), et toute la
# ponctuation avant les alphanumériques (impliqué par le (0, …) de la clé).
_TOOL_PUNCT_RANK = {"_": 1, "-": 2}


def _tool_sort_key(name: str):
    """[E3] Clé de tri reproduisant ``localeCompare`` (collation ICU du client).

    ``request.ts:184`` trie avec ``toSorted(localeCompare)``, pas avec ``<`` sur
    les code points. La différence n'est pas cosmétique, elle change l'ordre du fil
    pour les jeux de noms à casse mixte — c'est-à-dire pour tous les jeux réels
    (``Bash``/``bash_tool``, ``Zed``/``alpha``) :

    - **ponctuation d'abord** : ``_`` < ``-`` < chiffres < lettres ;
    - **lettres insensibles à la casse** au niveau primaire ;
    - **la casse ne départage qu'à égalité**, minuscule avant majuscule
      (``"a" < "A"``, ``"m1" < "M1"``), et **seulement** quand tout le niveau
      primaire est égal.

    Ce troisième point est le piège : la casse est un *niveau*, pas un poids par
    caractère. Un tri qui compare la casse position par position met ``a_b`` avant
    ``A`` (le ``a`` minuscule gagne au premier caractère), alors que ICU met ``A``
    avant ``a_b`` — le primaire de ``A`` est un **préfixe** de celui de ``a_b``, et
    un préfixe se décide avant que la casse ne soit même consultée.

    D'où la forme à deux niveaux. Le primaire porte, par caractère, un poids
    ``(0, rang)`` pour la ponctuation et ``(1, casefold)`` pour un alphanumérique :
    le ``0``/``1`` garantit ponctuation < alphanumérique, et ``casefold`` donne
    l'ordre alphabétique insensible à la casse. Le tertiaire ne transporte que la
    casse, et n'est regardé qu'une fois le primaire entièrement égal.

    Validé contre le vrai collateur (``node -e "[...].sort((a,b)=>a.localeCompare(b))``)
    sur 395 noms tirés de l'alphabet complet du contrat d'outils
    ``[A-Za-z0-9_-]`` : ordre identique, y compris les chaînes aléatoires. Pour un
    caractère hors de cet alphabet la clé reste **totale** (ponctuation inconnue
    classée par code point) — aucune exception, aucun ordre non déterministe.
    """
    # (0, rang entier) pour la ponctuation, (1, casefold str) pour le reste —
    # le 0/1 tranche avant que str et int ne se rencontrent jamais.
    prim: list[tuple[int, str | int]] = []
    tert: list[int] = []
    for ch in name:
        if ch.isalnum():
            prim.append((1, ch.casefold()))
        else:
            prim.append((0, _TOOL_PUNCT_RANK.get(ch, 3 + ord(ch))))
        tert.append(1 if ch.isupper() else 0)
    return (tuple(prim), tuple(tert))


def _sorted_chat_tools(tools: list) -> list:
    """[E3 — parité client officiel v1] Trie ``tools[]`` par ``function.name``.

    Re-mesuré sur le client 1.19.0 le 2026-10-15 (`opencode run`, provider
    `opencodeproxy`, 4 requêtes) : outils triés sur le fil
    (`toSorted(localeCompare)`, collation ICU). La lignée v2-dev
    (`flattenTools`, aucun tri) n'est PAS ce que le client déployé envoie.
    Renvoie une copie : l'entrée du caller n'est jamais mutée.
    """
    if not isinstance(tools, list) or len(tools) < 2:
        return tools
    for t in tools:
        if not isinstance(t, dict):
            return tools
        fn = t.get("function")
        if not isinstance(fn, dict):
            return tools
        name = fn.get("name")
        if not isinstance(name, str) or not name:
            return tools
    out = list(tools)
    out.sort(key=lambda t: _tool_sort_key(t["function"]["name"]))
    return out


def _sanitize_chat_tools(oai_tools: list, name_map: dict) -> list:
    """[Lot L4 — A8] Sanitize-aller des noms d'outils d'un corps **Chat**.

    ``sanitize_tool_names`` attend des dicts au format Responses (``name`` à
    plat) ; dans un corps Chat le nom vit sous ``function.name`` — il ne voyait
    donc rien et laissait passer un nom > 64 caractères, que l'amont Chat refuse
    en 400. On projette les noms vers la forme attendue, on réutilise la MÊME
    logique de sanitize (une seule source de vérité : digest, collisions,
    exclusions ``web_*``), puis on réécrit les noms dans les entrées Chat.

    Copie des seules entrées modifiées : le caller n'est jamais muté.
    Retourne la nouvelle liste ; complète ``name_map`` ``{short: original}``.
    Fast-path intégral (aucun nom à raccourcir) : map vide — mais la liste est
    malgré tout **triée** (E3, cf. ``_sorted_chat_tools``) : l'ordre officiel
    (mesuré 1.19.0) est une propriété du fil, pas un effet de bord du
    raccourcissement.
    """
    if not isinstance(oai_tools, list) or not oai_tools or not isinstance(name_map, dict):
        return oai_tools
    # E3 : on trie AVANT de projeter, pour que les index de ``flat``/``sanitized``
    # décrivent la liste telle qu'elle partira — l'alignement positionnel reste
    # garanti, et la sortie est triée même quand rien n'est à raccourcir.
    src = _sorted_chat_tools(oai_tools)
    idx_of: list[int] = []
    flat: list[dict] = []
    for i, t in enumerate(src):
        if not isinstance(t, dict):
            continue
        fn = t.get("function")
        if not isinstance(fn, dict):
            continue
        name = fn.get("name")
        if not isinstance(name, str) or not name:
            continue
        idx_of.append(i)
        flat.append({"name": name})
    if not flat:
        return src
    sanitized, fresh = sanitize_tool_names(flat)
    if not fresh:
        return src
    name_map.update(fresh)
    # ``sanitize_tool_names`` préserve ordre et longueur (cf. son ``out``), donc
    # l'alignement positionnel flat[i] ↔ sanitized[i] est garanti.
    out = list(src)
    for pos, i in enumerate(idx_of):
        new_name = sanitized[pos].get("name") if isinstance(sanitized[pos], dict) else None
        if isinstance(new_name, str) and new_name and new_name != flat[pos]["name"]:
            entry = dict(out[i])
            entry["function"] = dict(entry["function"], name=new_name)
            out[i] = entry
    return out


def _remap_chat_history_names(messages: list, name_map: dict) -> dict:
    """[Lot L4 — A8] L'historique Chat (``assistant.tool_calls[].function.name``)
    suit le même rename que ``tools[]`` — sinon le tour N+1 rejoue le nom
    original (>64) et l'amont répond 400 ``name must be at most 64 characters``.

    Mute les seuls conteneurs concernés (dicts neufs : jamais ceux du caller) et
    complète ``name_map`` (nom hors ``tools[]`` → short défensif enregistré, donc
    restore-retour préservé). Idempotent : un nom déjà short/valide est laissé
    inchangé. Même discipline que ``_remap_responses_history_names``.
    """
    if not isinstance(messages, list) or not messages or not isinstance(name_map, dict):
        return name_map
    inv: dict[str, str] = {o: s for s, o in name_map.items() if isinstance(s, str) and isinstance(o, str)}
    for i, m in enumerate(messages):
        if not isinstance(m, dict):
            continue
        calls = m.get("tool_calls")
        if not isinstance(calls, list) or not calls:
            continue
        new_calls = None
        for j, tc in enumerate(calls):
            if not isinstance(tc, dict):
                continue
            fn = tc.get("function")
            if not isinstance(fn, dict):
                continue
            name = fn.get("name")
            if not isinstance(name, str) or not name:
                continue
            short = inv.get(name)
            if short is None and (len(name) > TOOL_NAME_MAX_LEN or _TOOL_NAME_RE.search(name)):
                short = _register_defensive_short(name, name_map)
                inv[name] = short
            if short is not None and short != name:
                if new_calls is None:
                    new_calls = list(calls)
                new_calls[j] = dict(tc, function=dict(fn, name=short))
        if new_calls is not None:
            messages[i] = dict(m, tool_calls=new_calls)
    return name_map


def _remap_chat_tool_choice(tc, name_map: dict | None):
    """[Lot L4 — A8] Toute forme **nommée** de ``tool_choice`` suit le rename
    aller, en CONSERVANT la forme Chat ``{"type": "function", "function":
    {"name": …}}`` — c'est cette forme que l'amont Chat attend ; la variante
    Responses est traitée par ``_remap_responses_tool_choice``, qui normalise
    vers l'autre forme (ne pas confondre les deux).

    Les formes chaîne (``auto`` / ``required`` / ``none``) ne portent aucun nom :
    laissées telles quelles. Copie défensive ; nom hors map et non raccourci
    possible → inchangé.
    """
    if not isinstance(tc, dict) or not isinstance(name_map, dict):
        return tc
    fn = tc.get("function")
    if not isinstance(fn, dict):
        return tc
    name = fn.get("name")
    if not isinstance(name, str) or not name:
        return tc
    short = _inverse_tool_name_lookup(name_map, name)
    if short is None and (len(name) > TOOL_NAME_MAX_LEN or _TOOL_NAME_RE.search(name)):
        short = _register_defensive_short(name, name_map)
    if short is None or short == name:
        return tc
    return dict(tc, function=dict(fn, name=short))


def sanitize_chat_tool_names(body: dict) -> dict:
    """[TROU 2 — A8] Point d'entrée unique « sanitize-aller » d'un corps **Chat**.

    Une seule source de vérité pour les trois sites qui portent un nom d'outil
    dans un corps Chat : ``tools[].function.name`` (≤64), l'historique
    (``messages[].tool_calls[].function.name``) et ``tool_choice.function.name``.
    Sans les deux derniers, l'amont Chat répond 400 ``unknown tool`` dès le
    tour N+1 : un tool_choice ou un historique qui nomme l'outil original (>64)
    ne correspond plus à ``tools[]`` raccourci.

    Mute `body` (comme ``_remap_chat_history_names``) et y dépose la map
    ``{short: original}`` sous ``_TOOL_NAME_MAP_KEY`` pour le restore-retour.
    Le wire ne la voit jamais : ``_serialize_json_body`` la strippe, et les
    callers la poppent en local. Idempotent ; fast-path sans copie quand rien
    n'est à raccourcir.
    """
    if not isinstance(body, dict):
        return body
    name_map: dict = {}
    tools = body.get("tools")
    if isinstance(tools, list):
        body["tools"] = _sanitize_chat_tools(tools, name_map)
    messages = body.get("messages")
    if isinstance(messages, list):
        _remap_chat_history_names(messages, name_map)
    if "tool_choice" in body:
        body["tool_choice"] = _remap_chat_tool_choice(body.get("tool_choice"), name_map)
    if name_map:
        body[_TOOL_NAME_MAP_KEY] = name_map
    return body


def restore_chat_response_tool_names(data: dict, name_map: dict | None) -> dict:
    """[TROU 2 — A8] Restore-retour Chat : ``choices[].message.tool_calls`` (et
    la forme ``delta`` du stream) reprennent le nom **original** du client.

    Copie des seuls nœuds modifiés — jamais de mutation du caller. No-op si
    ``name_map`` est vide. Utilisé par le passthrough P3 (client Chat → amont
    Chat) en non-stream et en stream.
    """
    if not name_map or not isinstance(data, dict):
        return data
    choices = data.get("choices")
    if not isinstance(choices, list):
        return data
    for ch in choices:
        if not isinstance(ch, dict):
            continue
        for slot in ("message", "delta"):
            holder = ch.get(slot)
            if not isinstance(holder, dict):
                continue
            calls = holder.get("tool_calls")
            if not isinstance(calls, list) or not calls:
                continue
            new_calls = None
            for j, tc in enumerate(calls):
                if not isinstance(tc, dict):
                    continue
                fn = tc.get("function")
                if not isinstance(fn, dict):
                    continue
                short = fn.get("name")
                if not isinstance(short, str) or short not in name_map:
                    continue
                if new_calls is None:
                    new_calls = list(calls)
                new_calls[j] = dict(tc, function=dict(fn, name=name_map[short]))
            if new_calls is not None:
                ch[slot] = dict(holder, tool_calls=new_calls)
    return data


def _normalize_responses_input_items(inp: list) -> list:
    """Normalise les parts input_image/input_file vers le schéma Responses
    officiel (cf. docs/api-reference/responses) : un client (ou un payload
    pré-correctif) peut envoyer image_base64/mime_type au lieu de image_url,
    ou file_url/mime_type au lieu de file_data+filename — l'upstream rejette
    en 400 ("input_image ... requires either image_url or file_id").
    Copie défensive : jamais de mutation du caller. Idempotent."""
    out = []
    for item in inp:
        if not isinstance(item, dict):
            out.append(item)
            continue
        content = item.get("content")
        if not isinstance(content, list):
            out.append(item)
            continue
        changed = False
        new_content = []
        for b in content:
            if not isinstance(b, dict):
                new_content.append(b)
                continue
            btype = b.get("type", "")
            if btype == "input_image" and "image_url" not in b and "file_id" not in b:
                nb = dict(b)
                if b.get("image_base64"):
                    _nm = (b.get("mime_type", "") or "").strip() or "image/png"
                    if not _nm.startswith("image/"):
                        _nm = "image/png"
                    nb["image_url"] = f"data:{_nm};base64,{b['image_base64']}"
                    nb.pop("image_base64", None)
                    nb.pop("mime_type", None)
                    changed = True
                new_content.append(nb)
            elif btype == "input_file" and "file_id" not in b:
                nb = dict(b)
                if b.get("file_url") and not b.get("file_data"):
                    # input_file.file_url EXISTE dans le schéma Responses
                    # officiel → KEEP tel quel (fini le DROP).
                    new_content.append(nb)
                    continue
                if nb.get("file_data") and not str(nb["file_data"]).startswith("data:"):
                    _raw = str(nb["file_data"])
                    if _raw.startswith("http://") or _raw.startswith("https://"):
                        # file_data par URL → replié en file_url officiel.
                        nb = {"type": "input_file", "file_url": _raw}
                        if b.get("filename"):
                            nb["filename"] = b["filename"]
                        changed = True
                        new_content.append(nb)
                        continue
                    _head, _, _rest = _raw.partition(",")
                    if ";" not in (_head or ""):
                        nb["file_data"] = f"data:application/pdf;base64,{_rest or _raw}"
                        changed = True
                if nb.get("mime_type") and "filename" not in nb:
                    nb["filename"] = "document.pdf"
                    nb.pop("mime_type", None)
                    changed = True
                elif "mime_type" in nb and "filename" in nb:
                    nb.pop("mime_type", None)
                    changed = True
                new_content.append(nb)
            elif btype == "input_audio":
                # Schéma Responses : input_audio {data, format} — set large
                # mp3, wav, flac, ogg, m4a, mp4, webm (+ transcript optionnel).
                # Format inconnu → placeholder input_text, jamais de drop
                # silencieux.
                _na = b.get("input_audio") if isinstance(b.get("input_audio"), dict) else {}
                _nfmt = str((_na or {}).get("format", "") or "").lower()
                if _nfmt not in ("mp3", "wav", "flac", "ogg", "m4a", "mp4", "webm"):
                    _debug(f"  [convert] DROP input_audio format={_nfmt!r} → hors set Responses (placeholder)")
                    nb = {"type": "input_text", "text": f"[audio:{_nfmt or 'unknown'}]"}
                    changed = True
                    new_content.append(nb)
                else:
                    new_content.append(b)
            else:
                new_content.append(b)
        if changed:
            item = dict(item)
            item["content"] = new_content
        out.append(item)
    return out


def _sanitize_native_responses_request(req: dict) -> dict:
    """Sanitize-aller pour un body déjà au format Responses (verbatim) :
    tools[] + historique input[].function_call + tool_choice, map réunie sous
    _TOOL_NAME_MAP_KEY (jamais globale). Clamp config de ``reasoning.effort``
    (``thinking.effort_caps``) : un effort natif au-delà du plafond du modèle
    (ex. max sur un modèle plafonné high) serait sinon refusé en 400 upstream.
    Copie les conteneurs mutés pour ne jamais muter le caller. Idempotent
    (double conversion)."""
    if not isinstance(req, dict):
        return req
    req = dict(req)
    _native_reasoning = req.get("reasoning")
    if isinstance(_native_reasoning, dict):
        _native_effort = _native_reasoning.get("effort")
        if isinstance(_native_effort, str) and _native_effort:
            _req_model = req.get("model", "")
            _clamped = _effort_to_reasoning(_native_effort, _req_model)
            if _clamped is None:
                # Désactivé (none/off) : la clé reasoning entière sort
                # (le SDK n'émet aucun reasoning dans ce cas).
                req.pop("reasoning", None)
                _debug(f"  [thinking] {_req_model}: reasoning désactivé (effort={_native_effort!r}) → clé retirée")
            elif _clamped != _native_effort:
                req["reasoning"] = dict(_native_reasoning, effort=_clamped)
                _debug(f"  [thinking] {_req_model}: reasoning.effort clampé {_native_effort} → {_clamped}")
    name_map = req.get(_TOOL_NAME_MAP_KEY)
    name_map = dict(name_map) if isinstance(name_map, dict) else {}
    tools = req.get("tools")
    if isinstance(tools, list) and tools:
        tools, fresh = sanitize_tool_names(tools)
        req["tools"] = tools
        for _s, _o in fresh.items():
            name_map.setdefault(_s, _o)
    inp = req.get("input")
    if isinstance(inp, list) and inp:
        req["input"] = [dict(it) if isinstance(it, dict) else it for it in inp]
        req["input"] = _normalize_responses_input_items(req["input"])
        # Parité client officiel : strip des `id` d'items (comme Codex).
        req["input"] = _strip_responses_item_ids(req["input"])
        _remap_responses_history_names(req["input"], name_map)
    if req.get("tool_choice") is not None:
        # [FIX tour vide] {"type":"auto"} → "auto" (cf. _normalize_responses_tool_choice).
        req["tool_choice"] = _remap_responses_tool_choice(
            _normalize_responses_tool_choice(req["tool_choice"]), name_map
        )
    # Parité SDK : store:false + reasoning → include encrypted_content (rejeu).
    _ensure_encrypted_content_include(req)
    if name_map:
        req[_TOOL_NAME_MAP_KEY] = name_map
    else:
        req.pop(_TOOL_NAME_MAP_KEY, None)
    return req


def _relay_responses_storage_fields(req: dict, source: dict) -> None:
    """[Lot L15 — B5] Relaie ``store`` et ``truncation`` vers Responses.

    Les deux ont un défaut upstream qui surprend, et que le client doit pouvoir
    piloter :

    * ``store`` vaut ``true`` par défaut côté OpenAI — la réponse est **conservée
      ≥30 jours**, sans que le client l'ait demandé. Notre proxy ne relayait pas
      le champ : un client qui envoyait ``store: false`` (exigence de
      confidentialité) voyait sa consigne **ignorée**, et rien ne le signalait.
    * ``truncation`` vaut ``"disabled"`` : un dépassement de contexte produit un
      **400 explicite** au lieu d'une troncature silencieuse. Ne pas le relayer
      empêche le client de choisir ``"auto"``.

    On ne pose **pas** de valeur par défaut : n'émettre le champ que si le client
    l'a envoyé préserve la sémantique upstream, et évite de transformer un
    ``store`` absent en décision que le proxy n'a pas à prendre. Les valeurs sont
    validées — un ``store`` non booléen est rejeté par l'upstream, mieux vaut ne
    pas le propager pour un 400 évitable.
    """
    store = source.get("store")
    if isinstance(store, bool):
        req["store"] = store
    elif store is not None:
        _debug(f"  [convert] DROP store non booléen invalide: {store!r}")

    truncation = source.get("truncation")
    if isinstance(truncation, str) and truncation in ("auto", "disabled"):
        req["truncation"] = truncation
    elif truncation is not None:
        _debug(f"  [convert] DROP truncation invalide: {truncation!r}")


def _is_responses_reasoning_model(model_id: str) -> bool:
    """True si le modèle passe par /v1/responses en mode raisonnement (muse/spark).

    Miroir de la règle d'endpoint de ``config/settings.py`` (muse/spark →
    Responses). Le SDK ``@ai-sdk/openai`` applique à ces modèles :
    system→developer, suppression temperature/top_p, summary 'detailed' par défaut.
    """
    try:
        lid = str(model_id or "").lower()
    except Exception:
        return False
    return ("muse" in lid) or ("spark" in lid)


def _strip_responses_item_ids(inp: list) -> list:
    """Strip les ``id`` des items d'historique Responses (parité client officiel).

    Le client officiel (``packages/opencode/src/provider/provider.ts``, wrapper
    fetch « Strip openai itemId metadata following what codex does ») supprime
    ``id`` de chaque item de ``input`` avant envoi à Zen. ``call_id``
    (appariement function_call/output) et ``encrypted_content`` (rejeu reasoning
    sans store) sont TOUJOURS conservés. Exception : ``item_reference`` n'est
    QUE son ``id`` — le stripper = supprimer l'item, on le garde tel quel.
    Idempotent, copie défensive (jamais de mutation du caller).
    """
    if not isinstance(inp, list):
        return inp
    out = []
    for item in inp:
        if not isinstance(item, dict) or "id" not in item:
            out.append(item)
            continue
        if item.get("type") == "item_reference":
            out.append(item)
            continue
        cp = dict(item)
        cp.pop("id", None)
        out.append(cp)
    return out


_RESPONSES_OPTIONAL_STR_FIELDS = (
    "instructions",
    "conversation",
    "previous_response_id",
    "prompt_cache_key",
    "prompt_cache_retention",
    "user",
    "safety_identifier",
    "service_tier",
)


def _relay_responses_optional_fields(req: dict, source: dict) -> None:
    """Relaie les champs Responses optionnels présents chez le client (parité SDK).

    Le SDK ``@ai-sdk/openai`` les émet (conversation, previous_response_id,
    prompt_cache_key, service_tier, metadata, parallel_tool_calls, max_tool_calls,
    user, instructions, include, text, top_logprobs...) ; le proxy les droppait
    silencieusement sur les jambes converties (le passthrough natif les garde
    déjà par copie intégrale). Ne pose AUCUN défaut : absent → absent
    (sémantique upstream préservée). ``conversation`` + ``previous_response_id``
    ensemble → le SDK refuse (warning) : on garde previous_response_id, on
    droppe conversation (log). N'écrase jamais une valeur déjà posée par le
    convertisseur (double conversion P6 sûre).
    """
    if not isinstance(req, dict) or not isinstance(source, dict):
        return
    for k in _RESPONSES_OPTIONAL_STR_FIELDS:
        v = source.get(k)
        if isinstance(v, str) and v and k not in req:
            req[k] = v
    if isinstance(source.get("metadata"), dict) and source["metadata"] and "metadata" not in req:
        req["metadata"] = source["metadata"]
    if isinstance(source.get("parallel_tool_calls"), bool) and "parallel_tool_calls" not in req:
        req["parallel_tool_calls"] = source["parallel_tool_calls"]
    if isinstance(source.get("max_tool_calls"), int) and "max_tool_calls" not in req:
        req["max_tool_calls"] = source["max_tool_calls"]
    if isinstance(source.get("top_logprobs"), int) and "top_logprobs" not in req:
        req["top_logprobs"] = source["top_logprobs"]
    if isinstance(source.get("text"), dict) and source["text"] and "text" not in req:
        req["text"] = source["text"]
    inc = source.get("include")
    if isinstance(inc, list) and inc and "include" not in req:
        _clean = [x for x in inc if isinstance(x, str) and x]
        if _clean:
            req["include"] = _clean
    if req.get("previous_response_id") and req.get("conversation"):
        _debug("  [convert] conversation + previous_response_id → conversation droppé (SDK: mutuellement exclusifs)")
        del req["conversation"]


def _relay_chat_optional_fields(req: dict, source: dict) -> None:
    """Relaie `metadata`/`user`/`service_tier` vers une cible Chat (parité SDK).

    Champs valides de l'API Chat Completions, droppés par nos conversions.
    Relayés, jamais inventés.
    """
    if not isinstance(req, dict) or not isinstance(source, dict):
        return
    if isinstance(source.get("metadata"), dict) and source["metadata"] and "metadata" not in req:
        req["metadata"] = source["metadata"]
    for k in ("user", "service_tier", "prompt_cache_key"):
        v = source.get(k)
        if isinstance(v, str) and v and k not in req:
            req[k] = v


def _relay_anthropic_optional_fields(req: dict, source: dict) -> None:
    """Relaie `metadata`/`service_tier` vers une cible Anthropic + `user`(Chat)
    → `metadata.user_id` (forme Anthropic). Relayés, jamais inventés.
    """
    if not isinstance(req, dict) or not isinstance(source, dict):
        return
    meta = dict(req["metadata"]) if isinstance(req.get("metadata"), dict) else {}
    if isinstance(source.get("metadata"), dict):
        for k, v in source["metadata"].items():
            meta.setdefault(k, v)
    user = source.get("user")
    if isinstance(user, str) and user:
        meta.setdefault("user_id", user)
    if meta and "metadata" not in req:
        req["metadata"] = meta
    st = source.get("service_tier")
    if isinstance(st, str) and st and "service_tier" not in req:
        req["service_tier"] = st


def _relay_response_format(req: dict, source: dict) -> None:
    """Relaie `response_format` (Chat) → `text.format` (Responses), parité SDK.

    Le SDK émet `text: {format: {type: json_object|json_schema, ...}}` ; le
    proxy droppait la consigne → réponse non contrainte, échec silencieux du
    structured output. Formes Chat reconnues :
      {"type": "json_object"} → {"type": "json_object"} ;
      {"type": "json_schema", "json_schema": {"name","schema","strict",...}} →
        {"type": "json_schema", "name", "schema", "strict"?} (+description?).
    N'écrase jamais un `text` déjà posé ; absent → absent.
    """
    if not isinstance(req, dict) or not isinstance(source, dict):
        return
    if "text" in req:
        return
    rf = source.get("response_format")
    if not isinstance(rf, dict):
        return
    rtype = rf.get("type")
    if rtype == "json_object":
        req["text"] = {"format": {"type": "json_object"}}
    elif rtype == "json_schema" and isinstance(rf.get("json_schema"), dict):
        js = rf["json_schema"]
        fmt: dict = {"type": "json_schema"}
        for k in ("name", "description", "schema", "strict"):
            if js.get(k) is not None:
                fmt[k] = js[k]
        if isinstance(fmt.get("schema"), dict):
            req["text"] = {"format": fmt}
        else:
            _debug("  [convert] DROP response_format json_schema sans schema → non relayé")
    elif rtype == "text" or rtype is None:
        return
    else:
        _debug(f"  [convert] DROP response_format type={rtype!r} → inconnu côté Responses")


def _normalize_responses_tool_choice(tc):
    """[FIX tour vide] Formes ``{"type": ...}`` → ``"auto"``, seule valeur admise.

    L'upstream ``/v1/responses`` refuse en 400 tout ``tool_choice`` non nommé :
    « tool_choice did not match any supported type ». Un client Anthropic/Chat
    envoie ``{"type": "auto"}`` ; recopié verbatim par la conversion, il faisait
    échouer la requête. Sur la jambe free l'échec était avalé en **tour vide**
    (input ~50 000, output ~40 tokens) : le compactage client recevait un résumé
    vide et la tâche se figeait.

    Preuve live (``muse-spark-1.3-contributor-free``, via ``/v1/responses``) :

    ==============================  ===========================
    ``tool_choice`` envoyé          résultat
    ==============================  ===========================
    absent / ``"auto"``             200, contenu normal
    ``"none"``, ``"required"``      refus amont
    ``"any"``                       refus amont
    ``{"type": <auto|none|…>}``     refus amont
    ==============================  ===========================

    ``"auto"`` est donc la **seule** valeur qui passe. On y ramène toutes les
    formes non nommées ; la perte de sémantique de ``none``/``required`` est
    signalée en debug. Les formes NOMMÉES (``function``/``tool``/``name``) ne
    sont pas touchées ici : elles relèvent de ``_remap_responses_tool_choice``.
    """
    if not isinstance(tc, dict):
        return tc
    _t = tc.get("type")
    if _t in ("auto", "none", "required", "any"):
        if _t != "auto":
            _debug(f"  [convert] tool_choice {{'type': {_t!r}}} → 'auto' (/responses ne l'accepte pas)")
        return "auto"
    return tc


def _ensure_encrypted_content_include(req: dict) -> None:
    """Ajoute ``reasoning.encrypted_content`` à ``include`` quand store===False (parité SDK).

    Le SDK : ``if (store === false && isReasoningModel) addInclude(...)``. Sans
    lui, un client store:false ne reçoit que le summary et ne peut pas rejouer
    le reasoning au tour suivant. N'émet rien si store absent/vrai (défaut
    upstream) ou sans bloc reasoning (pas de raisonnement à rejouer).
    """
    if not isinstance(req, dict) or req.get("store") is not False:
        return
    if not isinstance(req.get("reasoning"), dict):
        return
    inc = req.get("include")
    if inc is None:
        req["include"] = ["reasoning.encrypted_content"]
    elif isinstance(inc, list) and "reasoning.encrypted_content" not in inc:
        req["include"] = [*inc, "reasoning.encrypted_content"]


