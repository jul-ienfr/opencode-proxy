"""
Protocol mapping — outils : sanitize des noms, scrubbing d'IDs, modalités de modèle, défauts d'échantillonnage.

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

# ── Tool-name sanitization (fix 400 `name must be at most 64 characters`) ──
# L'upstream /responses refuse les noms d'outils >64 chars (ex. tools MCP
# `mcp__plugin_...`, 65 chars) : le free tombait en 400 systématique, le retry
# « station fraîche » rejouant le MÊME body 4-5×. Sanitize-aller + restore-retour,
# par requête (clé privée `_tool_name_map`, jamais globale) — uniforme free+paid,
# jamais de branche au nom de modèle. Server tools `web_*` exclus.
TOOL_NAME_MAX_LEN = 64
_TOOL_NAME_MAP_KEY = "_tool_name_map"
_TOOL_NAME_RE = re.compile(r"[^A-Za-z0-9_-]")
# [P2-12] précompilée (chemin chaud _resolve_schema_profile, par requête).
_TRAILING_DIGITS_RE = re.compile(r"\d+$")
# Marqueur interne retry-once (items reasoning synthétiques) — posé dans le
# dict converti, consommé par la logique retry du caller, JAMAIS envoyé sur
# le wire (l'upstream /responses le rejette `unknown parameter` en 400).
_HAS_SYNTHETIC_REASONING_KEY = "_has_synthetic_reasoning_items"


def _short_tool_name(name: str) -> str:
    """Raccourci déterministe : `name[:57] + "-" + sha1(name)[:6]` (=64)."""
    digest = hashlib.sha1(name.encode("utf-8")).hexdigest()[:6]
    return f"{name[:57]}-{digest}"


def sanitize_tool_names(tools: list) -> tuple[list, dict]:
    """Sanitize les noms d'outils d'une liste `tools` Responses (dicts avec `name`).

    Retourne (tools_sanitized, name_map) où name_map = {short: original}.
    Fast-path : noms ≤64 + charset OK → payload inchangé, map vide.
    Collisions résiduelles résolues par ordre alphabétique trié
    (`name[:54] + "-" + sha1[:6] + "-02"`). Server tools `web_*` jamais renommés.
    """
    if not isinstance(tools, list) or not tools:
        return tools, {}
    used_valid: set[str] = set()
    todo: list[tuple[int, dict, str]] = []  # (index, entry, original)
    for i, t in enumerate(tools):
        if not isinstance(t, dict):
            continue
        name = t.get("name", "")
        if not isinstance(name, str) or not name or name.startswith("web_"):
            if isinstance(name, str) and name:
                used_valid.add(name)
            continue
        if len(name) <= TOOL_NAME_MAX_LEN and not _TOOL_NAME_RE.search(name):
            used_valid.add(name)
            continue
        todo.append((i, t, name))
    if not todo:
        return tools, {}
    # Passe 1 : candidat déterministe par outil (re-charset défensif d'abord,
    # digest sha1 sur l'original — cas courant long-mais-valide = plan-literal).
    cand_of: dict[int, str] = {}
    for i, _t, name in todo:
        clean = _TOOL_NAME_RE.sub("_", name)
        if len(clean) <= TOOL_NAME_MAX_LEN:
            cand_of[i] = clean
        else:
            digest = hashlib.sha1(name.encode("utf-8")).hexdigest()[:6]
            cand_of[i] = f"{clean[:57]}-{digest}"
    # Passe 2 : collisions résiduelles, ordre alphabétique trié (indépendant
    # de l'ordre d'arrivée des tools → déterministe inter-requêtes).
    by_cand: dict[str, list[tuple[int, str]]] = {}
    for i, _t, name in todo:
        by_cand.setdefault(cand_of[i], []).append((i, name))
    name_map: dict[str, str] = {}
    final: dict[int, str] = {}
    used = set(used_valid)
    for cand, members in sorted(by_cand.items()):
        originals = sorted({name for _, name in members})
        first_keeps = len(originals) == 1 and cand not in used
        n = 2
        for orig in originals:
            if first_keeps and orig == originals[0]:
                short = cand
            else:
                digest = hashlib.sha1(orig.encode("utf-8")).hexdigest()[:6]
                clean = _TOOL_NAME_RE.sub("_", orig)
                short = f"{clean[:54]}-{digest}-{n:02d}"
                while short in used:
                    n += 1
                    short = f"{clean[:54]}-{digest}-{n:02d}"
                n += 1
            used.add(short)
            name_map[short] = orig
            for i, name in members:
                if name == orig:
                    final[i] = short
    out = [dict(t) if idx in final else t for idx, t in enumerate(tools)]
    for idx, short in final.items():
        out[idx]["name"] = short
    return out, name_map


def restore_tool_name(short: str, name_map: dict | None) -> str:
    """Restore le nom original d'un outil sanitizé (retour aller→client)."""
    if not name_map or not isinstance(short, str):
        return short
    return name_map.get(short, short)


def _inverse_tool_name_lookup(name_map: dict | None, original: str) -> str | None:
    """Recherche inversée original → short dans une map {short: original}."""
    if not isinstance(name_map, dict) or not isinstance(original, str):
        return None
    for _short, _orig in name_map.items():
        if _orig == original:
            return _short
    return None


def _register_defensive_short(name: str, name_map: dict) -> str:
    """Short déterministe ≤64 + charset valide pour un nom hors tools[]
    (historique périmé, tool_choice orphelin), enregistré {short: original}
    dans name_map pour le restore-retour. Collisions suffixées."""
    clean = _TOOL_NAME_RE.sub("_", name)
    digest = hashlib.sha1(name.encode("utf-8")).hexdigest()[:6]
    short = f"{clean[:57]}-{digest}"
    n = 2
    while short in name_map and name_map.get(short) != name:
        short = f"{clean[:54]}-{digest}-{n:02d}"
        n += 1
    name_map[short] = name
    return short


# ── Parité OpenCodeClient (sst/opencode provider/transform.ts) ──
# Ports fidèles des normalisations du client officiel, appliquées au corps
# OpenAI-chat CONVERTI (équivalent de normalizeMessages / temperature / topP /
# topK / applyCaching pour notre pont Anthropic → OpenAI-chat) :
#   * sanitize_surrogates — le client nettoie les surrogats solitaires sur TOUS
#     les rôles avant envoi (l'amont rejette sinon) ;
#   * scrub des tool_call_id — claude : ``[^a-zA-Z0-9_-]→"_"`` ; mistral (& co) :
#     alphanumérique, 9 chars, pad "0", + insertion d'un tour assistant "Done."
#     entre tool→user (Mistral refuse tool suivi de user) ;
#   * deepseek — tout message assistant porte ``reasoning_content`` (même " ") :
#     le champ, même vide, DOIT être renvoyé aux tours suivants ;
#   * sampling_defaults_for_model — temperature/top_p/top_k par défaut quand le
#     client n'en envoie pas (claude→inchangé, gemini/glm/minimax/kimi→valeurs
#     officielles) ;
#   * cache — le 2ᵉ breakpoint (avant-dernier message non-système) est posé dans
#     la boucle « last user » plus bas (applyCaching officiel : 2 derniers).
_SURROGATE_RE = re.compile(r"[\ud800-\udbff](?![\udc00-\udfff])|(?<![\ud800-\udbff])[\udc00-\udfff]")
_CLAUDE_TOOL_ID_RE = re.compile(r"[^a-zA-Z0-9_-]")
_MISTRAL_TOOL_ID_RE = re.compile(r"[^a-zA-Z0-9]")
_MISTRAL_FAMILIES = ("mistral", "devstral", "codestral", "pixtral", "mixtral")
# Coupe-circuit de diagnostic (tests) : True = normalisations de parité désactivées.
_PARITY_DISABLED = False


def _anthropic_block_meaningful(block: dict) -> bool:
    """Un bloc Anthropic compte comme contenu non-vide (garde le message)."""
    if not isinstance(block, dict):
        return True
    btype = block.get("type")
    if btype == "text":
        return bool(isinstance(block.get("text"), str) and block["text"] != "")
    if btype == "thinking":
        text = block.get("thinking", "")
        if isinstance(text, str) and text.strip():
            return True
        # Port transform.ts : un reasoning signé/donnée redactée survit vide.
        return bool(block.get("signature") or block.get("redacted_data") or block.get("data"))
    if btype == "redacted_thinking":
        return bool(block.get("data"))
    # tool_use / tool_result / image / document / autres : jamais filtrés ici.
    return True


def filter_empty_anthropic_messages(messages: list) -> list:
    """Port du filtre vide Anthropic/Bedrock (normalizeMessages).

    Retire les parts ``text:""`` et les ``thinking`` vides non signés ; un
    message sans plus aucun contenu significatif est retiré (l'amont Anthropic
    rejette les contenus vides en 400). Retourne une NOUVELLE liste.
    """
    if not isinstance(messages, list):
        return messages
    out = []
    for m in messages:
        if not isinstance(m, dict):
            out.append(m)
            continue
        content = m.get("content")
        if isinstance(content, str):
            if content == "":
                continue
            out.append(m)
            continue
        if not isinstance(content, list):
            out.append(m)
            continue
        kept = [b for b in content if not isinstance(b, dict) or _anthropic_block_meaningful(b)]
        if not kept:
            continue
        if len(kept) != len(content):
            m = dict(m)
            m["content"] = kept
        out.append(m)
    return out


# ── Modalités d'entrée par modèle (port de unsupportedParts / capabilities) ──
# Table explicite pour les modèles du proxy ; défaut = texte seul (un modèle
# inconnu ne se voit jamais attribuer une modalité qu'il n'a pas).
_MODEL_INPUT_MODALITIES: dict[str, set[str]] = {}


def _register_model_modalities() -> dict[str, set[str]]:
    table: dict[str, set[str]] = {}
    full = {"text", "image", "pdf"}
    for fam in (
        "claude",
        "gpt-",
        "kimi",
        "glm",
        "minimax",
        "muse-spark",
        "qwen",
        "deepseek",
        "mistral",
        "gemini",
        "grok",
    ):
        table[fam] = set(full)
    table["audio_in"] = {"text", "image", "pdf", "audio"}
    return table


_MODEL_INPUT_MODALITIES = _register_model_modalities()


def model_supports_modality(model: str, modality: str) -> bool:
    """True si le modèle accepte la modalité d'entrée.

    Source unique : ``config.settings.get_model_capabilities`` (catalogue
    explicite + défauts par famille) ; la table locale ne sert que de repli
    si settings est indisponible. Modèle inconnu → texte seul.
    """
    try:
        _caps_fn = getattr(_cfg_settings, "get_model_capabilities", None)
        if callable(_caps_fn):
            caps = _caps_fn(model)
            if isinstance(caps, dict):
                inputs = caps.get("input", [])
                if isinstance(inputs, (list, tuple, set, frozenset)):
                    return modality in set(inputs)
    except Exception:
        pass
    mid = str(model or "").lower()
    for fam, mods in _MODEL_INPUT_MODALITIES.items():
        if fam in mid:
            return modality in mods
    return modality == "text"


def unsupported_modality_error_text(modality: str, filename: str | None = None) -> str:
    """Port de unsupportedParts : texte d'erreur explicite à la place d'un
    contenu qu'un modèle ne sait pas lire (le modèle en informe l'utilisateur)."""
    name = f'"{filename}"' if filename else modality
    return f"ERROR: Cannot read {name} (this model does not support {modality} input). Inform the user."


# ── Thinking moderne Anthropic (ports de transform.ts) ──
_ANTHROPIC_VERSION_RE = re.compile(
    r"claude-(?:([a-z]+)-)?(\d+)(?:[.-](\d{1,2}))?(?:-([a-z]+))?(?:[.@-]|$)", re.IGNORECASE
)


def anthropic_modern_adaptive_thinking(model: str) -> bool:
    """Claude 4.7+ (pensée adaptive moderne, `display` défaut `omitted`)."""
    m = _ANTHROPIC_VERSION_RE.search(str(model or ""))
    if not m:
        return "claude" in str(model or "").lower()
    major, minor = int(m.group(2)), int(m.group(3) or 0)
    return major > 4 or (major == 4 and minor >= 7)


def anthropic_binds_thinking(model: str) -> bool:
    """Claude 5.1+ lie la signature au préfixe (sauf Mythos 5.1)."""
    m = _ANTHROPIC_VERSION_RE.search(str(model or ""))
    if not m:
        return False
    major, minor = int(m.group(2)), int(m.group(3) or 0)
    fam = (m.group(1) or m.group(4) or "").lower()
    if major == 5 and minor == 1 and fam == "mythos":
        return False
    return major > 5 or (major == 5 and minor >= 1)


ANTHROPIC_BLOCK_BINDING = {"prefixMismatchBehavior": "drop_block"}


def apply_anthropic_thinking_display(thinking: dict, model: str) -> dict:
    """Force `display: summarized` sur adaptive moderne (défaut `omitted`
    rendrait des blocs thinking vides), et `blockBinding: drop_block` sur
    5.1+ (le préfixe re-rendu entre tours serait sinon rejeté en 400)."""
    if not isinstance(thinking, dict):
        return thinking
    ttype = thinking.get("type")
    if ttype not in ("adaptive", "enabled"):
        return thinking
    if ttype == "adaptive" and anthropic_modern_adaptive_thinking(model):
        thinking.setdefault("display", "summarized")
    if anthropic_binds_thinking(model) and "blockBinding" not in thinking:
        thinking["blockBinding"] = dict(ANTHROPIC_BLOCK_BINDING)
    return thinking


def sanitize_surrogates(text: str) -> str:
    """Port de ``sanitizeSurrogates`` (transform.ts) : surrogats solitaires → U+FFFD."""
    if not isinstance(text, str) or not text:
        return text
    return _SURROGATE_RE.sub("\ufffd", text)


def scrub_claude_tool_id(tid: str) -> str:
    """Port du scrub claude (transform.ts) : ``[^a-zA-Z0-9_-]`` → ``"_"``."""
    if not isinstance(tid, str) or not tid:
        return tid
    return _CLAUDE_TOOL_ID_RE.sub("_", tid)


def scrub_mistral_tool_id(tid: str) -> str:
    """Port du scrub mistral (transform.ts) : alphanum, 9 chars, pad "0"."""
    if not isinstance(tid, str) or not tid:
        return tid
    return _MISTRAL_TOOL_ID_RE.sub("", tid)[:9].ljust(9, "0")


def _is_mistral_model(model: str) -> bool:
    mid = str(model or "").lower()
    return "mistral" in str(model or "").lower() or any(f in mid for f in _MISTRAL_FAMILIES)


def _is_deepseek_model(model: str) -> bool:
    return "deepseek" in str(model or "").lower()


def _is_claude_model(model: str) -> bool:
    return "claude" in str(model or "").lower()


def scrub_openai_tool_ids(messages: list, model: str) -> int:
    """Scrub les tool_call_id d'un corps OpenAI-chat selon la famille du modèle.

    Parité officielle v2 (`openai-chat.ts` lowering `toolCallID`) :
    - mistral/devstral/codestral/pixtral/mixtral → alnum 9 chars pad `0` ;
    - id contenant `claude` → `[^a-zA-Z0-9_-]→_` ;
    - provider openai/azure ou id `openai/` → slice 40 ;
    - sinon → inchangé (pas de scrub inventé).
    No-op sur les IDs déjà valides. Retourne le nombre d'IDs modifiés.
    """
    if not isinstance(messages, list) or not messages:
        return 0
    mistral = _is_mistral_model(model)
    claude = _is_claude_model(model)
    mid = str(model or "").lower()
    openai_slice = ("openai" in mid) or ("azure" in mid) or mid.startswith("openai/")
    if not mistral and not claude and not openai_slice:
        return 0

    def _scrub_openai_slice(tid: str) -> str:
        return tid[:40] if isinstance(tid, str) else tid

    scrub = scrub_mistral_tool_id if mistral else (scrub_claude_tool_id if claude else _scrub_openai_slice)
    changed = 0
    for m in messages:
        if not isinstance(m, dict):
            continue
        for tc in m.get("tool_calls") or []:
            if isinstance(tc, dict) and isinstance(tc.get("id"), str):
                new = scrub(tc["id"])
                if new != tc["id"]:
                    tc["id"] = new
                    changed += 1
        if m.get("role") == "tool" and isinstance(m.get("tool_call_id"), str):
            new = scrub(m["tool_call_id"])
            if new != m["tool_call_id"]:
                m["tool_call_id"] = new
                changed += 1
    if changed:
        _debug(f"  [parity] scrub tool_ids model={model!r} changed={changed}")
    return changed


def fix_mistral_tool_user_sequence(messages: list, model: str) -> int:
    """Port du fix de séquence mistral : un message tool suivi d'un user reçoit
    un tour assistant ``"Done."`` intercalé. Retourne le nombre d'insertions."""
    if not _is_mistral_model(model) or not isinstance(messages, list):
        return 0
    inserted = 0
    out: list = []
    for i, m in enumerate(messages):
        out.append(m)
        nxt = messages[i + 1] if i + 1 < len(messages) else None
        if isinstance(m, dict) and m.get("role") == "tool" and isinstance(nxt, dict) and nxt.get("role") == "user":
            out.append({"role": "assistant", "content": "Done."})
            inserted += 1
    if inserted:
        messages[:] = out
        _debug(f"  [parity] mistral tool→user fix model={model!r} inserted={inserted}")
    return inserted


def ensure_deepseek_reasoning(messages: list, model: str) -> int:
    """Port deepseek (transform.ts) : chaque message assistant porte
    ``reasoning_content`` (même vide) pour rejouabilité multi-tours."""
    if not _is_deepseek_model(model) or not isinstance(messages, list):
        return 0
    fixed = 0
    for m in messages:
        if isinstance(m, dict) and m.get("role") == "assistant" and "reasoning_content" not in m:
            m["reasoning_content"] = " "
            fixed += 1
    if fixed:
        _debug(f"  [parity] deepseek reasoning_content default model={model!r} fixed={fixed}")
    return fixed


def sanitize_openai_texts(messages: list) -> int:
    """Applique ``sanitize_surrogates`` aux contenus texte d'un corps OpenAI-chat."""
    if not isinstance(messages, list):
        return 0
    fixed = 0
    for m in messages:
        if not isinstance(m, dict):
            continue
        content = m.get("content")
        if isinstance(content, str):
            new = sanitize_surrogates(content)
            if new != content:
                m["content"] = new
                fixed += 1
        elif isinstance(content, list):
            for part in content:
                if isinstance(part, dict) and isinstance(part.get("text"), str):
                    new = sanitize_surrogates(part["text"])
                    if new != part["text"]:
                        part["text"] = new
                        fixed += 1
    return fixed


def sampling_defaults_for_model(model: str) -> dict:
    """Port de temperature/topP/topK (transform.ts) : défauts d'échantillonnage
    par modèle, {} quand le client ne doit pas être touché (claude→undefined)."""
    mid = str(model or "").lower()
    out: dict = {}
    if "north-mini-code" in mid:
        return {"temperature": 1.0}
    if "claude" in mid:
        return {}
    if "gemini" in mid:
        import re as _re

        if any(
            _re.search(p, mid)
            for p in (
                r"gemini-2[.-]5(?:[.-]|$)",
                r"gemini-3-(?:flash|pro)(?:[.-]|$)",
                r"gemini-3[.-]1(?:[.-]|$)",
            )
        ):
            return {"temperature": 1.0, "top_p": 0.95, "top_k": 64}
        return {}
    if "glm-4.6" in mid or "glm-4.7" in mid:
        return {"temperature": 1.0}
    if "minimax-m2" in mid:
        out = {"temperature": 1.0, "top_p": 0.95}
        out["top_k"] = 40 if any(s in mid for s in ("m2.", "m25", "m21")) else 20
        return out
    if "kimi-k2" in mid:
        if any(s in mid for s in ("thinking", "k2.", "k2p", "k2-5", "k2.5")):
            return {"temperature": 1.0, "top_p": 0.95}
        return {"temperature": 0.6}
    if "kimi" in mid or "k2p" in mid:
        return {}
    return {}


def apply_opencode_sampling_defaults(oai: dict, model: str) -> bool:
    """Pose les défauts d'échantillonnage UNIQUEMENT si le client n'a rien envoyé.

    Ne touche jamais une valeur cliente explicite (le proxy reste un pont).
    Retourne True si au moins un défaut a été posé."""
    if not isinstance(oai, dict):
        return False
    defaults = sampling_defaults_for_model(model)
    if not defaults:
        return False
    applied = False
    for key, val in defaults.items():
        if key not in oai:
            oai[key] = val
            applied = True
    if applied:
        _debug(f"  [parity] sampling defaults model={model!r} {defaults}")
    return applied


def _apply_opencode_client_parity(oai: dict, body: dict, model: str) -> dict:
    """Orchestre les normalisations OpenCodeClient sur un corps converti (muté).

    Appelé en fin de ``_orig_anthropic_to_openai``, AVANT la clôture cache pour
    que le plafond 4 couvre tous les breakpoints. No-op sur entrées propres."""
    if not isinstance(oai, dict):
        return oai
    messages = oai.get("messages")
    if isinstance(messages, list):
        sanitize_openai_texts(messages)
        scrub_openai_tool_ids(messages, model)
        fix_mistral_tool_user_sequence(messages, model)
        ensure_deepseek_reasoning(messages, model)
    apply_opencode_sampling_defaults(oai, model)
    return oai


