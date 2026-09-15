"""
Protocol mapping: Anthropic <-> OpenAI <-> Responses
Extracted from opencode.py (P3.10) - pure move, no behavior change.

[Phase 4 refonte] Domicile CANONIQUE (déplacement pur depuis
``protocol_mapping.py`` — contenu identique à l'octet près, seul cet en-tête
change). ``protocol_mapping.py`` reste un shim de re-export (mêmes objets,
état mutable partagé) jusqu'à la Phase 9 ; le nouveau code importe
``app.protocol.mapping``.
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
from dashboard.display import debug as _debug
from dashboard.display import log as _log

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


def _drop_orphan_tool_messages(messages: list[dict]) -> list[dict]:
    """Filter role:tool messages whose tool_call_id has no preceding tool_calls id."""
    # [P5.2 perf] early-exit sans rebuild quand aucun role=="tool" (99% des requêtes)
    if not any(m.get("role") == "tool" for m in messages):
        return messages
    _seen_ids: set[str] = set()
    filtered: list[dict] = []
    for m in messages:
        if m.get("tool_calls"):
            for tc in m["tool_calls"]:
                tid = tc.get("id")
                if tid:
                    _seen_ids.add(tid)
            filtered.append(m)
        elif m.get("role") == "tool":
            cid = m.get("tool_call_id", "")
            if cid in _seen_ids:
                filtered.append(m)
            else:
                _debug(
                    f"  [orphan] DROP tool output call_id={cid!r} — no preceding tool_call (compaction or empty-name skip)"
                )
        else:
            filtered.append(m)
    return filtered


def _drop_orphan_responses_input(inp: list[dict]) -> list[dict]:
    """Filter function_call_output items whose call_id has no preceding function_call."""
    if not isinstance(inp, list):
        return inp
    inp = [it for it in inp if isinstance(it, dict)]
    # [P5.2 perf] early-exit sans rebuild quand aucun function_call_output
    if not any(it.get("type") == "function_call_output" for it in inp):
        return inp
    _known: set[str] = set()
    _filt: list[dict] = []
    for it in inp:
        t = it.get("type")
        if t == "function_call":
            cid = it.get("call_id") or it.get("id") or ""
            if cid:
                _known.add(cid)
            _filt.append(it)
        elif t == "function_call_output":
            cid = it.get("call_id") or ""
            if cid in _known:
                _filt.append(it)
            else:
                _debug(f"  [orphan] DROP function_call_output call_id={cid!r} — no preceding function_call")
        else:
            _filt.append(it)
    return _filt


def _extract_cache_tokens(usage: dict) -> int:
    details = usage.get("prompt_tokens_details") or {}
    if "cached_tokens" in details:
        return details["cached_tokens"]
    if "cached_tokens" in usage:
        return usage["cached_tokens"]
    if "cache_read_input_tokens" in usage:
        return usage["cache_read_input_tokens"]
    return 0


def _extract_reasoning_tokens(usage: dict, output_tokens: int | None = None) -> int:
    """[Lot L12 — A18] Tokens de raisonnement d'un `usage` amont, toutes formes.

    A18 : les conversions Responses écrivaient ``reasoning_tokens: 0`` **en dur**
    (mapping.py:2459, 2538). Le dashboard affichait donc toujours zéro token de
    raisonnement, alors que c'est précisément la part facturée la plus chère sur
    un modèle de raisonnement — et l'information qui permet à un client de
    détecter qu'un modèle « réfléchit » moins que prévu.

    Formes lues :

    * ``output_tokens_details.reasoning_tokens`` — Responses / Chat OpenAI ;
    * ``completion_tokens_details.reasoning_tokens`` — Chat OpenAI historique ;
    * ``reasoning_tokens`` — certains upstreams compatibles à plat ;
    * ``output_tokens_details.thinking_tokens`` — passthrough Anthropic-compat.

    ``output_tokens`` (optionnel) borne le résultat : la ventilation est un
    SOUS-ENSEMBLE de la sortie. Un amont qui annonce plus de tokens de
    raisonnement que de tokens produits est incohérent, et propager la valeur
    telle quelle afficherait un pourcentage de raisonnement > 100 % dans le
    dashboard. On plafonne plutôt que de relayer une incohérence.

    Fallback 0 quand absent (un upstream sans raisonnement n'écrit pas ce champ).
    """
    value = 0
    if isinstance(usage, dict):
        for container, key in (
            ("output_tokens_details", "reasoning_tokens"),
            ("output_tokens_details", "thinking_tokens"),
            ("completion_tokens_details", "reasoning_tokens"),
            ("prompt_tokens_details", "reasoning_tokens"),
        ):
            holder = usage.get(container)
            if isinstance(holder, dict):
                candidate = holder.get(key)
                if isinstance(candidate, int) and candidate > 0:
                    value = candidate
                    break
        else:
            candidate = usage.get("reasoning_tokens")
            if isinstance(candidate, int) and candidate > 0:
                value = candidate
    if value and isinstance(output_tokens, int) and output_tokens >= 0:
        return min(value, output_tokens)
    return value


#: Modèles dont l'upstream Chat **exige** ``max_completion_tokens`` : la famille
#: o-series / gpt-5 d'OpenAI, où ``max_tokens`` est déprécié et rejeté (B2).
#:
#: Volontairement RESTREINT à cette famille. Les passerelles compatibles tierces
#: que ce proxy utilise (DeepSeek, GLM, MiMo, …) documentent et acceptent
#: ``max_tokens`` : leur envoyer ``max_completion_tokens`` risquerait un 400, ou
#: pire un champ ignoré — donc **aucune limite de sortie appliquée**, un coût non
#: borné (exactement le contraire du but). B2 ne mandate le basculement que pour
#: les modèles o-series.
#:
#: Surchargeable via ``thinking.max_completion_models`` dans ``config.yaml`` :
#: un opérateur qui constate qu'un upstream l'exige l'ajoute sans toucher au code.
_MAX_COMPLETION_MODELS_DEFAULT = ("o1", "o3", "o4", "gpt-5")


def _wants_max_completion_tokens(model: str) -> bool:
    """[Lot L14 — B2/A17] L'upstream Chat de ``model`` exige-t-il ``max_completion_tokens`` ?

    ``max_tokens`` est déprécié et **incompatible avec les modèles o-series**,
    qui exigent ``max_completion_tokens`` : un upstream strict rejette
    ``max_tokens`` par un 400, la requête échoue alors qu'elle est légitime.

    Le basculement est délibérément limité aux préfixes reconnus (config-driven,
    cf. ``_MAX_COMPLETION_MODELS_DEFAULT``) : basculer trop large risquerait
    qu'une passerelle tierce ignore le champ inconnu et n'applique donc **aucune
    limite de sortie** — un coût non borné, soit pire que le problème d'origine.
    """
    if not isinstance(model, str) or not model:
        return False
    cfg = yaml_get("thinking", "max_completion_models", None)
    prefixes = cfg if isinstance(cfg, list) and cfg else _MAX_COMPLETION_MODELS_DEFAULT
    name = model.lower()
    return any(name.startswith(str(p).lower()) for p in prefixes if p)


def _set_output_token_limit(
    target: dict,
    source: dict,
    model: str,
    default: int = 16384,
    target_protocol: str = "openai",
) -> int:
    """[Lot L14 — B2/A17] Écrit la limite de sortie dans ``target``, forme adaptée.

    Lit la limite du client en acceptant **toutes** ses conventions —
    ``max_tokens`` (Anthropic/Chat historique) et ``max_completion_tokens``
    (Chat moderne), ``max_output_tokens`` (Responses) — puis l'écrit sous la
    forme attendue par la **destination** :

    * ``target_protocol="openai"`` + modèle de raisonnement →
      ``max_completion_tokens`` (B2 : ``max_tokens`` y est déprécié, et rejeté
      par les modèles o-series) ;
    * ``target_protocol="openai"`` sinon → ``max_tokens`` ;
    * ``target_protocol="anthropic"`` → toujours ``max_tokens`` : c'est le seul
      champ qu'Anthropic connaît, quelle que soit la forme reçue du client.

    Avant ce lot, P4 lisait uniquement ``max_tokens`` : un client Chat envoyant
    ``max_completion_tokens`` (la forme moderne recommandée) voyait sa limite
    **silencieusement remplacée par le défaut 16384**. Une limite courte posée
    pour maîtriser le coût devenait donc inopérante — perte invisible.

    Retourne la limite retenue.

    **Précédence de lecture** (décision documentée, ex-§11.8) : ``max_tokens`` →
    ``max_completion_tokens`` → ``max_output_tokens``, **première forme valide
    gagnante**. ``max_tokens`` est canonique côté Anthropic et historique côté
    Chat ; les deux autres ne servent que de repli quand il est absent. Un
    client qui enverrait deux formes divergentes suit donc ``max_tokens``.
    """
    limit = None
    for key in ("max_tokens", "max_completion_tokens", "max_output_tokens"):
        candidate = source.get(key)
        if isinstance(candidate, int) and candidate > 0:
            limit = candidate
            break
    if limit is None:
        # Le client n'a rien demandé : ne pas inventer de champ, sauf défaut
        # explicitement voulu par l'appelant (comportement historique 16384).
        if default is None:
            return 0
        limit = default

    if target_protocol == "anthropic":
        target["max_tokens"] = limit
    elif _wants_max_completion_tokens(model):
        target["max_completion_tokens"] = limit
        _debug(
            f"  [convert] {model}: max_tokens→max_completion_tokens={limit} "
            f"(modèle de raisonnement, B2)"
        )
    else:
        target["max_tokens"] = limit
    return limit


def _extract_cache_creation_tokens(usage: dict) -> int:
    """[Lot H1] Tokens écrits en cache côté upstream OpenAI, si le champ existe.

    OpenAI n'expose pas de champ standard unique : certains upstreams renvoient
    `prompt_tokens_details.cache_creation_tokens`, d'autres un top-level
    `cache_creation_input_tokens` (passthrough Anthropic-compat) ou
    `prompt_cache_miss_tokens`. Fallback 0 quand absent.
    """
    details = usage.get("prompt_tokens_details") or {}
    if "cache_creation_tokens" in details:
        return details["cache_creation_tokens"]
    if "cache_creation_input_tokens" in usage:
        return usage["cache_creation_input_tokens"]
    if "prompt_cache_miss_tokens" in usage:
        return usage["prompt_cache_miss_tokens"]
    return 0


# [Lot H3] Cache borné des blocs redacted_thinking retirés des requêtes partant
# vers des upstreams non-Anthropic. Clé = sha256 du champ `data` (blob chiffré
# authentique, non déchiffrable par le proxy). Les blocs restent disponibles
# pour réinjection si la conversation revient vers un upstream Anthropic.
_redacted_thinking_cache: OrderedDict[str, dict] = OrderedDict()
_REDACTED_THINKING_CACHE_MAX = 512


_thinking_cfg = yaml_get("thinking", "min_tokens", {})
THINKING_MODELS = (
    {k: int(v) for k, v in _thinking_cfg.items()}
    if isinstance(_thinking_cfg, dict)
    else {
        "deepseek-v4-flash": 2048,
        "deepseek-v4-pro": 4096,
    }
)


def _extract_text(content) -> str:
    if isinstance(content, str):
        return content
    if isinstance(content, list):
        parts = []
        for i in content:
            if isinstance(i, str):
                parts.append(i)
            elif isinstance(i, dict):
                if i.get("type") == "text":
                    parts.append(i.get("text", ""))
                elif i.get("type") == "thinking":
                    parts.append(i.get("thinking", ""))
                elif i.get("type") == "image":
                    parts.append(f"[image:{i.get('source', {}).get('type', 'unknown')}]")
                elif i.get("type") == "document":
                    parts.append(f"[document:{i.get('source', {}).get('type', 'unknown')}]")
                elif i.get("type") == "input_audio":
                    _af = (
                        (i.get("input_audio") or {}).get("format", "unknown")
                        if isinstance(i.get("input_audio"), dict)
                        else "unknown"
                    )
                    parts.append(f"[audio:{_af}]")
                elif i.get("type") in ("video", "video_url"):
                    parts.append("[video:unsupported]")
                elif i.get("type") == "file":
                    parts.append(
                        f"[file:{(i.get('file') or {}).get('filename', 'unknown') if isinstance(i.get('file'), dict) else 'unknown'}]"
                    )
                else:
                    parts.append(i.get("text", str(i)))
        return "\n".join(parts)
    return str(content) if content else ""


# ── Cache restructuration for models without semantic caching ──
CACHE_REWRITE_MODELS = set(
    yaml_get(
        "cache_rewrite_models",
        default=["mimo-v2.5", "mimo-v2-pro", "mimo-v2-omni", "mimo-v2.5-pro"],
    )
)


def _find_split_point(text: str) -> int:
    """Find the best split point between static instructions and dynamic content.

    Looks for the last double-newline in the first 8000 chars to split cleanly.
    Search up to 75% of text (capped at 8000) so split near boundary (e.g. 8000/13000)
    is found, while still keeping prefix stable for cache.
    """
    search_limit = min(8000, max(2000, len(text) * 3 // 4))
    # +2 to include delimiter starting at exactly search_limit (rfind end is exclusive)
    last_double = text.rfind("\n\n", 0, search_limit + 2)
    if last_double > 500:
        return last_double
    last_newline = text.rfind("\n", 0, search_limit + 1)
    if last_newline > 500:
        return last_newline
    return 0


def _effort_to_reasoning(effort_level: str, model: str) -> str:
    """Map generic effort to model-specific reasoning_effort (config-driven).

    Délègue à ``config.effort_caps.clamp_effort`` : plafond par modèle lu en
    live depuis ``thinking.effort_order`` / ``thinking.effort_caps``
    (``config.yaml``). Signature inchangée (coquille fine — réexportée,
    utilisée par tests + opencode.py).

    Robustesse : ``None``/``""``/``"none"`` → ``"low"`` (repli historique
    de la branche défaut) ; niveau inconnu non vide → passthrough inchangé
    (robustesse forward) ; import config protégé (jamais de 500 si la
    config est absente — fallback hardcodé = comportement pré-patch).
    """
    try:
        from config.effort_caps import clamp_effort
    except ImportError:  # pragma: no cover
        clamp_effort = None  # type: ignore[assignment]
    if clamp_effort is not None:
        try:
            clamped = clamp_effort(effort_level, model)
        except Exception:
            clamped = None
        if isinstance(clamped, str) and clamped:
            return clamped
        # None (désactivé) ou niveau inconnu-vide → repli historique.
        _lvl = str(effort_level or "").strip().lower()
        if _lvl in ("medium", "high", "xhigh", "max"):
            return "high" if _lvl in ("high", "xhigh", "max") else "medium"
        return "low"
    # Fallback hardcodé = comportement pré-patch (branche défaut historique).
    if effort_level in ("xhigh", "max", "high"):
        return "high"
    if effort_level == "medium":
        return "medium"
    return "low"


# [Hotfix 2026-09-10] Niveaux acceptés par ``output_config.effort`` (référence
# API Messages : ``"low" | "medium" | "high" | "xhigh" | "max"``). OpenAI
# accepte en plus ``none`` (pas de raisonnement) et ``minimal``, qui se replie
# sur ``low`` — correspondance de LiteLLM, de facto standard de l'écosystème
# (``minimal→low``, ``xhigh→xhigh``, ``max→max``, jamais d'écrasement en high).
_ANTHROPIC_EFFORT_LEVELS = ("low", "medium", "high", "xhigh", "max")
_ANTHROPIC_EFFORT_ALIASES = {"minimal": "low", "none": "", "": ""}


def _effort_to_anthropic(effort_level: str, model: str) -> str:
    """Niveau d'effort (vocabulaire OpenAI) → ``output_config.effort`` Anthropic.

    Applique le plafond du modèle (``_effort_to_reasoning`` → ``clamp_effort``)
    puis replie sur le vocabulaire Anthropic : ``minimal → low``,
    ``none``/vide → ``""`` (aucun raisonnement demandé). Un niveau hors
    vocabulaire est ramené au défaut documenté ``high`` plutôt que relayé tel
    quel — l'upstream Anthropic rejetterait un niveau inconnu en 400.
    """
    level = str(effort_level or "").strip().lower()
    level = _ANTHROPIC_EFFORT_ALIASES.get(level, level)
    if not level:
        return ""
    mapped = _effort_to_reasoning(level, model)
    if not isinstance(mapped, str) or not mapped:
        return ""
    mapped = _ANTHROPIC_EFFORT_ALIASES.get(mapped.strip().lower(), mapped.strip().lower())
    if not mapped:
        return ""
    if mapped in _ANTHROPIC_EFFORT_LEVELS:
        return mapped
    _debug(f"  [thinking] {model}: effort {mapped!r} hors vocabulaire Anthropic -> high")
    return "high"


def _apply_anthropic_effort(result: dict, effort_level: str, model: str, *, source: str = "") -> bool:
    """Écrit ``output_config.effort`` + ``thinking.adaptive`` sur un body Anthropic.

    Remplace deux formes retirées le 2026-09-10 parce qu'invalides :

    * ``thinking: {type:"enabled", budget_tokens:N}`` — dépréciée sur Claude 4.6,
      **rejetée en 400 à partir de Claude 4.7** ;
    * ``reasoning_effort`` — nom de champ OpenAI, inexistant côté Anthropic.

    ``adaptive`` ne porte pas de ``budget_tokens`` : l'invariant Anthropic
    ``max_tokens > budget_tokens`` ne peut donc plus être violé, quelle que soit
    la valeur de ``max_tokens`` demandée par le client (l'ancien ratio
    16000/10000/4000 émettait 16000 quels que soient les ``max_tokens``).

    Un ``thinking: {type:"disabled"}`` explicite du client est respecté.
    Retourne ``True`` si un effort a effectivement été appliqué.
    """
    level = _effort_to_anthropic(effort_level, model)
    if not level:
        return False
    cfg = result.get("output_config")
    cfg = dict(cfg) if isinstance(cfg, dict) else {}
    cfg["effort"] = level
    result["output_config"] = cfg
    thinking = result.get("thinking")
    thinking = dict(thinking) if isinstance(thinking, dict) else {}
    if thinking.get("type") != "disabled":
        thinking["type"] = "adaptive"
        result["thinking"] = thinking
    _debug(f"  [thinking] {model}: output_config.effort={level} + thinking=adaptive ({source})")
    return True


# ── [Lot L11 — A20] Conformité cache Anthropic ──
#
# Anthropic n'accepte que **4 breakpoints** `cache_control` par requête : au-delà
# l'amont répond 400. Nos convertisseurs en *ajoutent* (pratique recommandée :
# préfixe système + dernier tour utilisateur) tout en reportant ceux du client :
# rien ne bornait le total. Un client qui pose lui-même 4 breakpoints faisait
# donc échouer sa requête à cause de NOTRE ajout — panne invisible côté client.
ANTHROPIC_MAX_CACHE_BREAKPOINTS = 4


def _cc_is_set(holder: Any) -> bool:
    """Vrai si ce message/outil porte déjà un breakpoint."""
    return isinstance(holder, dict) and bool(holder.get("cache_control"))


def _count_cache_breakpoints(messages: list, tools: list | None = None) -> int:
    """Compte les breakpoints `cache_control` d'un corps converti.

    Compte les breakpoints message et outil (les deux consomment le quota
    Anthropic de 4) ainsi que les breakpoints posés sur des **parts** de
    contenu, qui sont une autre façon de les placer.
    """
    total = 0
    for m in messages or []:
        if not isinstance(m, dict):
            continue
        if _cc_is_set(m):
            total += 1
        content = m.get("content")
        if isinstance(content, list):
            total += sum(1 for p in content if _cc_is_set(p))
    for t in tools or []:
        if _cc_is_set(t):
            total += 1
    return total


def _enforce_cache_breakpoint_limit(result: dict) -> dict:
    """Garde-fou A20 : ne jamais partir avec plus de 4 breakpoints.

    On retire les breakpoints **les plus anciens d'abord** (en parcourant le
    prompt du début vers la fin) : le cache Anthropic fonctionne par préfixe,
    donc ce sont les marqueurs profonds dans la conversation — les plus récents —
    qui ont le meilleur rapport hit/miss.

    Les breakpoints d'**outils** consomment le même quota que ceux des messages
    (le préfixe caché inclut les définitions d'outils) : ils sont donc comptés
    ET élagués ici. Les oublier laissait passer 5 breakpoints dans un corps
    « 3 messages + 3 outils », soit exactement le 400 qu'on veut éviter.

    Retourne le corps (muté en place, comme les autres helpers de ce module).
    """
    messages = result.get("messages")
    tools = result.get("tools")
    tools = tools if isinstance(tools, list) else None

    total = _count_cache_breakpoints(messages or [], tools)
    if total <= ANTHROPIC_MAX_CACHE_BREAKPOINTS:
        return result

    _debug(
        f"  [cache] {total} breakpoints cache_control > {ANTHROPIC_MAX_CACHE_BREAKPOINTS} "
        f"→ élagage des plus anciens (400 amont sinon)"
    )

    # Ordre de priorité de conservation : messages d'abord (du plus ancien au
    # plus récent), puis outils. On élague donc en tête de cette liste, ce qui
    # revient à sacrifier les breakpoints les moins profonds dans le préfixe.
    slots: list[dict] = []
    for m in messages or []:
        if not isinstance(m, dict):
            continue
        if _cc_is_set(m):
            slots.append(m)
        content = m.get("content")
        if isinstance(content, list):
            for p in content:
                if _cc_is_set(p):
                    slots.append(p)
    for t in tools or []:
        if _cc_is_set(t):
            slots.append(t)

    excess = total - ANTHROPIC_MAX_CACHE_BREAKPOINTS
    for holder in slots[:excess]:
        holder.pop("cache_control", None)

    remaining = _count_cache_breakpoints(messages or [], tools)
    if remaining > ANTHROPIC_MAX_CACHE_BREAKPOINTS:  # pragma: no cover — sécurité
        # Filet de sécurité : si un emplacement compté n'était pas élagable
        # (structure inattendue), on retire depuis la fin.
        for holder in reversed(slots):
            if remaining <= ANTHROPIC_MAX_CACHE_BREAKPOINTS:
                break
            if holder.pop("cache_control", None):
                remaining -= 1
    return result


def _apply_top_level_cache_control(result: dict, source_body: dict) -> dict:
    """[Lot L11 — A20] Reporte le `cache_control` **top-level** (automatic caching).

    Anthropic accepte un `cache_control` à la racine du corps : le service place
    alors lui-même le breakpoint au dernier bloc cachable. Nos convertisseurs ne
    le lisaient pas — un client qui utilisait cette forme perdait son cache
    silencieusement (aucune erreur, juste plus de hit).

    On le transporte tel quel : c'est la forme la plus simple et celle que
    l'amont comprend nativement.
    """
    if not isinstance(source_body, dict):
        return result
    cc = source_body.get("cache_control")
    if cc:
        result["cache_control"] = cc
    return result


def _cache_control_to_openai_breakpoint(holder: dict) -> dict:
    """[Lot L11 — B3] Réservé — NON ÉMIS (voir décision ci-dessous).

    B3 établit que l'équivalent OpenAI de ``cache_control`` est
    ``prompt_cache_breakpoint: {"mode":"explicit"}`` posé **sur les content
    parts** (text, image_url, input_audio, file, tool messages), et non au
    niveau du message.

    Une première version de L11 l'émettait au niveau du message : c'était un
    placement contraire à la spec citée par le plan. Émettre un champ
    non-standard au mauvais niveau est un risque net (un amont strict peut
    répondre 400, un amont permissif le place au mauvais endroit) pour un
    bénéfice nul tant que le placement par part n'est pas fait.

    Ce travail est explicitement rattaché à **L15** (« traduire `cache_control`
    → `prompt_cache_breakpoint` sur les parts »). On laisse donc le corps
    inchangé ici, et ``cache_control`` continue d'être transporté comme avant
    (comportement préexistant, non modifié par ce lot).
    """
    return holder


def _restructure_for_cache(oai_body: dict, model_id: str) -> dict:
    """For models without semantic caching, split the system prompt.

    Keeps the static part (instructions + tools) as the system message with
    cache_control, and moves the dynamic part (conversation history) into
    the messages array so the prefix stays stable across requests.
    """
    if model_id not in CACHE_REWRITE_MODELS:
        return oai_body

    messages = oai_body.get("messages", [])
    if not messages:
        return oai_body

    # Find the system message
    sys_idx = None
    for i, m in enumerate(messages):
        if m.get("role") == "system":
            sys_idx = i
            break

    if sys_idx is None:
        return oai_body

    sys_content = messages[sys_idx].get("content", "")
    if not isinstance(sys_content, str) or len(sys_content) < CACHE_MIN_PROMPT_SIZE:
        _debug(
            f"  [cache-restructure] skipped: sys_content len={len(sys_content) if isinstance(sys_content, str) else 0} < min={CACHE_MIN_PROMPT_SIZE}"
        )
        return oai_body  # Small prompt, no need to restructure

    split_point = _find_split_point(sys_content)
    if split_point <= 0:
        _debug(f"  [cache-restructure] skipped: no valid split point found in {len(sys_content)} chars")
        return oai_body

    static_part = sys_content[:split_point].strip()
    dynamic_part = sys_content[split_point:].strip()

    # Rebuild: static system message with cache_control + dynamic as user message
    new_messages = [
        {
            "role": "system",
            "content": static_part,
            "cache_control": {"type": "ephemeral"},
        }
    ]

    if dynamic_part:
        new_messages.append({"role": "user", "content": dynamic_part})

    # Append original messages (skip the old system message)
    for i, m in enumerate(messages):
        if i != sys_idx:
            new_messages.append(m)

    oai_body["messages"] = new_messages
    _debug(
        f"  [cache-restructure] split at point={split_point}: static={len(static_part)} dynamic={len(dynamic_part)} chars"
    )
    _log(f"  [cache] split system prompt: static={len(static_part)} dynamic={len(dynamic_part)} chars")
    return oai_body


def _strip_billing_header(text: str) -> str:
    """Remove x-anthropic-billing-header from system prompt.

    Claude Code injects a billing header with a changing hash (cch=...) that
    breaks prompt caching by modifying the prefix on every request.
    """
    if not text.startswith("x-anthropic-billing-header:"):
        return text
    # Strip the first line (the header) and any trailing blank line
    first_nl = text.find("\n")
    if first_nl == -1:
        return text
    rest = text[first_nl + 1 :]
    if rest.startswith("\n"):
        rest = rest[1:]
    return rest


# ── Tool schema normalization (strict-subset) ────────────────────
# En profil strict (muse/spark/...) : AUCUN keyword hors sous-ensemble
# strict n'est émis — `pattern` systématiquement strippé (le validateur
# de la jambe Responses rejette les lookarounds en 400
# invalid_request_error, quel que soit le fragment exact cité),
# `propertyNames`/`prefixItems`/`allOf` également normalisés. `pattern`
# et `propertyNames` ne sont que des hints de validation (le proxy ne
# valide pas les valeurs d'args) → suppression cosmétique seule.
# Profils permissifs (minimax/qwen) : `pattern` conservé si ECMA-safe,
# sinon transpilé via _rewrite_unicode_properties, strippé si non
# transpilable. Pleine fidélité : \p{Cc|Cf|Zl|Zp} intra-classe →
# plages BMP exactes (toutes BMP → \uXXXX par code-unit, identique en
# ECMA sans `u`).
_UNSAFE_PATTERN_RE = re.compile(r"\\[pP]\{")

# Cf BMP uniquement (43 pts, unicodedata 15.0) : le reste de Cf est
# astral (U+110BD, U+110CD, U+13430-3F, U+1BCA0-A3, U+1D173-7A, U+E0001,
# U+E0020-7F — 127 pts), non représentable dans une classe sans flag
# `u` → admis (écart documenté ; jamais présent dans des args d'outils).
_P_CLASS_BMP = {
    "Cc": r"\u0000-\u001F\u007F-\u009F",
    "Cf": (
        r"\u00AD\u0600-\u0605\u061C\u06DD\u070F\u0890-\u0891\u08E2\u180E"
        r"\u200B-\u200F\u202A-\u202E\u2060-\u2064\u2066-\u206F"
        r"\uFEFF\uFFF9-\uFFFB"
    ),
    "Zl": r"\u2028",
    "Zp": r"\u2029",
}
_P_TOKEN_RE = re.compile(r"\\[pP]\{([^}]*)\}")


def _rewrite_unicode_properties(pattern: str) -> "str | None":
    """Transpile les \\p{Cc|Cf|Zl|Zp} intra-classe en plages BMP ECMA-safe.

    Retourne le pattern réécrit, ou None si non transpilable (l'appelant
    strippe alors la clé — ex-comportement étape 18, toujours mieux qu'un
    400). Non transpilable : \\P{...}, propriété inconnue, token hors
    classe, classe non fermée, résultat structurellement invalide.
    Token à backslash échappé ([\\\\p{Cc}] = antislash littéral, pas une
    propriété) : copié verbatim, jamais réécrit.
    """
    toks = list(_P_TOKEN_RE.finditer(pattern))
    if not toks:
        return pattern
    # Spans des classes [...] (échappements honorés).
    spans: list = []
    in_class = False
    start = 0
    i, n = 0, len(pattern)
    while i < n:
        c = pattern[i]
        if c == "\\":
            i += 2
            continue
        if c == "[" and not in_class:
            in_class = True
            start = i
        elif c == "]" and in_class:
            in_class = False
            spans.append((start, i + 1))
        i += 1
    if in_class:  # classe non fermée → pas touche (strip)
        return None
    out: list = []
    last = 0
    for m in toks:
        # Backslash du token lui-même échappé (ex. [\\p{Cc}] = antislash
        # littéral + "p{Cc}", pas une propriété Unicode) → copié verbatim,
        # jamais réécrit (pas de corruption, pas de strip inutile).
        bs, run = m.start() - 1, 0
        while bs >= 0 and pattern[bs] == "\\":
            run += 1
            bs -= 1
        if run % 2 == 1:
            out.append(pattern[last : m.end()])
            last = m.end()
            continue
        inside = any(s <= m.start() and m.end() <= e for s, e in spans)
        bmp = _P_CLASS_BMP.get(m.group(1)) if m.group(0).startswith("\\p") else None
        if not inside or bmp is None:
            return None
        out.append(pattern[last : m.start()])
        out.append(bmp)
        last = m.end()
    out.append(pattern[last:])
    rewritten = "".join(out)
    try:
        re.compile(rewritten)  # garde-fou structurel (crochets/échappements)
    except re.error:
        return None
    return rewritten


_SCHEMA_PROFILES: dict[str, dict] = {
    "muse": {"strip_additional_props": True, "strip_format": True, "max_description_len": 1024, "max_nesting": 8},
    "spark": {"strip_additional_props": True, "strip_format": True, "max_description_len": 1024, "max_nesting": 8},
    "deepseek": {"strip_additional_props": True, "strip_format": True, "max_description_len": 1024, "max_nesting": 8},
    "glm": {"strip_additional_props": True, "strip_format": True, "max_description_len": 1024, "max_nesting": 8},
    "mimo": {"strip_additional_props": True, "strip_format": True, "max_description_len": 1024, "max_nesting": 8},
    "hy": {"strip_additional_props": True, "strip_format": True, "max_description_len": 1024, "max_nesting": 8},
    "minimax": {"strip_additional_props": False, "strip_format": False, "max_description_len": 2048, "max_nesting": 12},
    "qwen": {"strip_additional_props": False, "strip_format": False, "max_description_len": 2048, "max_nesting": 12},
    "_default": {"strip_additional_props": True, "strip_format": True, "max_description_len": 1024, "max_nesting": 8},
}


def _resolve_schema_profile(model: str) -> dict:
    """Résout le profil — même logique que config/settings.py:807 _resolve_protocol."""
    if not isinstance(model, str) or not model:
        return _SCHEMA_PROFILES["_default"]
    low = model.lower()
    if "spark" in low:
        return _SCHEMA_PROFILES["spark"]
    if low.startswith("muse"):
        return _SCHEMA_PROFILES["muse"]
    prefix = low.split("-")[0].split(".")[0]
    prefix = re.sub(r"\d+$", "", prefix)
    return _SCHEMA_PROFILES.get(prefix, _SCHEMA_PROFILES["_default"])


def _normalize_tool_schema(schema: dict, model: str = "") -> dict:
    """Normalise un JSON Schema tool pour compatibilité multi-modèles.

    Copy-on-write : l'input n'est jamais muté (deepcopy).
    Idempotent : _normalize(_normalize(x)) == _normalize(x).
    Récursif avec guard profondeur = profil max_nesting + 10 pour $ref circulaire.
    """
    if not isinstance(schema, dict):
        return {}
    if not schema:
        return {}
    prof = _resolve_schema_profile(model)
    out = copy.deepcopy(schema)

    def _norm(node, depth: int, defs: dict):
        if depth > prof["max_nesting"]:
            _debug(f"  [schema] flatten depth>{prof['max_nesting']} model={model!r}")
            return {"type": "object"}
        if depth > 20:
            return {"type": "object"}
        if not isinstance(node, dict):
            return node
        if "$ref" in node:
            ref = node["$ref"]
            if isinstance(ref, str) and ref.startswith("#/"):
                parts = ref.lstrip("#/").split("/")
                if len(parts) == 2 and parts[0] in ("$defs", "definitions"):
                    target = defs.get(parts[1]) if isinstance(defs, dict) else None
                    if isinstance(target, dict):
                        resolved = copy.deepcopy(target)
                        for k, v in node.items():
                            if k not in ("$ref", "$defs", "definitions"):
                                if k not in resolved:
                                    resolved[k] = copy.deepcopy(v)
                        return _norm(resolved, depth + 1, defs)
            node = {k: v for k, v in node.items() if k != "$ref"}
            if not node:
                return {}
        if node.get("nullable") is True:
            node.pop("nullable")
        if prof["strip_additional_props"] and node == {} and depth > 0:
            return {"type": "string"}
        if isinstance(node.get("type"), list):
            t = [x for x in node["type"] if x != "null"]
            if len(t) == 1:
                node["type"] = t[0]
            elif not t:
                node.pop("type")
            else:
                node["type"] = t
        for key in ("anyOf", "oneOf", "allOf"):
            if key in node and isinstance(node[key], list):
                lst = node[key]
                has_null = any(isinstance(x, dict) and x.get("type") == "null" and len(x) == 1 for x in lst)
                if has_null:
                    filtered = [x for x in lst if not (isinstance(x, dict) and x.get("type") == "null" and len(x) == 1)]
                    if len(filtered) == 1 and isinstance(filtered[0], dict):
                        siblings = {k: v for k, v in node.items() if k != key}
                        merged = copy.deepcopy(filtered[0])
                        for sk, sv in siblings.items():
                            if sk not in merged:
                                merged[sk] = sv
                        return _norm(merged, depth, defs)
                    elif not filtered:
                        node.pop(key)
                        continue
                    else:
                        node[key] = filtered
                if key in node:
                    node[key] = [_norm(x, depth + 1, defs) for x in node[key]]
        if prof["strip_format"] and "format" in node:
            if node["format"] not in ("date-time",):
                node.pop("format")
        if "enum" in node and isinstance(node["enum"], list) and len(node["enum"]) == 0:
            node.pop("enum")
        if "description" in node and isinstance(node["description"], str):
            ml = prof["max_description_len"]
            orig_len = len(node["description"])
            if orig_len > ml:
                node["description"] = node["description"][: ml - 3] + "..."
                _debug(f"  [schema] truncate description {orig_len}>{ml} model={model!r}")
        if prof["strip_additional_props"]:
            ap = node.get("additionalProperties")
            if ap is True or isinstance(ap, dict):
                node.pop("additionalProperties")
            if "unevaluatedProperties" in node:
                node.pop("unevaluatedProperties")
            if "patternProperties" in node:
                node.pop("patternProperties")
            for k in ("if", "then", "else"):
                if k in node and isinstance(node[k], dict):
                    node.pop(k)
        # ── Traitements 10-17 : contrat strict muse/spark → 100% ──
        _is_strict = prof["strip_additional_props"]
        # 10. Force root type:"object" si absent
        if _is_strict and "type" not in node and depth == 0:
            node["type"] = "object"
        # 11. Ensure each properties[k] a un type explicite (sinon string)
        if _is_strict and node.get("type") == "object" and "properties" in node:
            for pk, pv in list(node["properties"].items()):
                if (
                    isinstance(pv, dict)
                    and "type" not in pv
                    and "anyOf" not in pv
                    and "oneOf" not in pv
                    and "allOf" not in pv
                    and "$ref" not in pv
                ):
                    pv["type"] = "string"
        # 12. Enforce required ⊆ properties
        if "required" in node and isinstance(node["required"], list) and "properties" in node:
            props = set(node["properties"].keys()) if isinstance(node["properties"], dict) else set()
            node["required"] = [r for r in node["required"] if r in props]
            if not node["required"]:
                node.pop("required")
        # 13. Strip marker invalide "@schema_version: invalid json schema"
        if "@schema_version" in node:
            node.pop("@schema_version")
        # 14. Enforce additionalProperties:false sur tout object (root + nested)
        if _is_strict and node.get("type") == "object":
            node["additionalProperties"] = False
        # 15. Strip anyOf/oneOf/allOf résiduels pour strict (seul anyOf/allOf null géré en 9)
        if _is_strict:
            for key in ("anyOf", "oneOf", "allOf"):
                if key in node and isinstance(node[key], list):
                    lst = node[key]
                    if lst and isinstance(lst[0], dict) and lst[0].get("type"):
                        first = copy.deepcopy(lst[0])
                        for sk, sv in list(node.items()):
                            if sk not in (key, "type") and sk not in first:
                                first[sk] = sv
                        node.clear()
                        node.update(_norm(first, depth, defs))
                        return node
                    node.pop(key, None)
        # 16. Strip keywords non supportés en strict
        if _is_strict:
            for k in (
                "$schema",
                "$id",
                "title",
                "const",
                "examples",
                "example",
                "exclusiveMaximum",
                "exclusiveMinimum",
            ):
                node.pop(k, None)
        # 17. Si array avec items sans type, forcer items.type
        if (
            _is_strict
            and node.get("type") == "array"
            and "items" in node
            and isinstance(node["items"], dict)
            and "type" not in node["items"]
        ):
            if not any(k in node["items"] for k in ("anyOf", "oneOf", "$ref")):
                node["items"]["type"] = "string"
        # 18. Strip TOTAL de `pattern` en strict — 400
        # invalid_request_error garanti sur la jambe Responses sinon
        # (route free, provider Console). Hors strict (profils permissifs
        # minimax/qwen), rewrite BMP-exact via _rewrite_unicode_properties,
        # strip seulement si non transpilable.
        if _is_strict and isinstance(node.get("pattern"), str):
            _debug(f"  [schema] strip pattern (strict) model={model!r} pattern={node['pattern'][:80]!r}")
            node.pop("pattern", None)
        elif not _is_strict and isinstance(node.get("pattern"), str):
            if _UNSAFE_PATTERN_RE.search(node["pattern"]):
                rewritten = _rewrite_unicode_properties(node["pattern"])
                if rewritten is None:
                    _debug(f"  [schema] strip unsafe pattern model={model!r} pattern={node['pattern'][:80]!r}")
                    node.pop("pattern")
                elif rewritten != node["pattern"]:
                    _debug(f"  [schema] rewrite unsafe pattern model={model!r} pattern={node['pattern'][:80]!r}")
                    node["pattern"] = rewritten
        # 19. Strict-subset : drop propertyNames/prefixItems, sécurise {}.
        # propertyNames non supporté en strict → pop. prefixItems non
        # supporté → pop + items par défaut string si absent/non-dict
        # (dict existant laissé tel quel, la récursion le normalise).
        # Nœud vidé par les strips → {"type": "string"} (jambe Responses
        # rejette les schémas vides). Root exclu : depth 0 a toujours
        # "type" ici (étape 10), donc == {} impossible à depth 0.
        if _is_strict:
            node.pop("propertyNames", None)
            if "prefixItems" in node:
                node.pop("prefixItems")
                if not isinstance(node.get("items"), dict):
                    node["items"] = {"type": "string"}
            if node == {}:
                node["type"] = "string"
        defs_local: dict = {}
        if "$defs" in node and isinstance(node["$defs"], dict):
            defs_local.update(node["$defs"])
        if "definitions" in node and isinstance(node["definitions"], dict):
            defs_local.update(node["definitions"])
        merged_defs: dict = {}
        if isinstance(defs, dict):
            for dk, dv in defs.items():
                if dk not in ("$defs", "definitions") and isinstance(dv, dict):
                    merged_defs[dk] = dv
        for k, v in defs_local.items():
            merged_defs[k] = v
        if "properties" in node and isinstance(node["properties"], dict):
            for k in list(node["properties"].keys()):
                node["properties"][k] = _norm(node["properties"][k], depth + 1, merged_defs)
        for k in ("items", "contains", "propertyNames", "additionalProperties"):
            if k in node and isinstance(node[k], dict):
                node[k] = _norm(node[k], depth + 1, merged_defs)
        if "prefixItems" in node and isinstance(node["prefixItems"], list):
            node["prefixItems"] = [_norm(x, depth + 1, merged_defs) for x in node["prefixItems"]]
        return node

    root_defs: dict = {}
    if "$defs" in out and isinstance(out["$defs"], dict):
        root_defs.update(out["$defs"])
    if "definitions" in out and isinstance(out["definitions"], dict):
        root_defs.update(out["definitions"])
    return _norm(out, 0, root_defs)


def anthropic_to_openai(body: dict, model: str, raw: bytes | None = None) -> dict:
    # ``raw`` : bytes bruts du client, consommés uniquement par le wrapper de
    # cache (plus bas) — l'implémentation d'origine n'en a pas besoin.
    thinking = isinstance(body.get("thinking"), dict) and body["thinking"].get("type") in (
        "enabled",
        "adaptive",
    )
    # GLM-5.x models don't support cache_control — skip it
    supports_cache_control = not model.startswith("glm-5")

    messages: list[dict[str, Any]] = []

    # System prompt — always add cache_control for prefix caching
    system_val = body.get("system", "")
    if isinstance(system_val, list):
        text = _extract_text(system_val)
        if text:
            text = _strip_billing_header(text)
            msg: dict[str, Any] = {"role": "system", "content": text}
            if supports_cache_control:
                msg["cache_control"] = {"type": "ephemeral"}
            messages.append(msg)
    elif system_val:
        msg = {"role": "system", "content": _strip_billing_header(system_val)}
        # Always add cache_control to system messages for prefix caching
        if supports_cache_control:
            msg["cache_control"] = {"type": "ephemeral"}
        messages.append(msg)

    for msg in body.get("messages", []):
        role, content = msg["role"], msg.get("content", "")
        is_asst = role == "assistant"

        # Simple string content
        if isinstance(content, str):
            out = {"role": role, "content": content}
            if thinking and is_asst:
                out["reasoning_content"] = " "
            messages.append(out)
            continue

        if not isinstance(content, list):
            continue

        text_parts, tool_calls, thinking_parts, tool_results, image_parts = [], [], [], [], []
        last_cache_control = None

        for block in content:
            if isinstance(block, str):
                text_parts.append(block)
                continue
            if not isinstance(block, dict):
                continue

            btype = block.get("type")
            if btype == "text":
                text_parts.append(block.get("text", ""))
                if "cache_control" in block:
                    last_cache_control = block["cache_control"]
            elif btype == "thinking":
                # [Correctif parité multi-tours — remplace Phase D.2] les blocs
                # SYNTHÉTIQUES (signature locale) voyagent désormais comme les
                # ORIGINAUX : leur texte devient reasoning_content, exactement
                # ce que l'upstream recevrait sans le proxy. Les signatures ne
                # transitent jamais vers openai-compatible (seul le texte).
                thinking_parts.append(block.get("thinking", ""))
            elif btype == "redacted_thinking":
                # [Lot H3 — remplace Phase D.4] donnée chiffrée authentique :
                # préservée telle quelle vers Anthropic (passthrough). Vers les
                # autres upstreams le bloc ne peut pas transiter (non déchiffrable,
                # non interprétable) mais il est CONSERVÉ dans
                # _redacted_thinking_cache au lieu d'être perdu définitivement.
                _rt_data = block.get("data", "")
                if isinstance(_rt_data, str) and _rt_data:
                    _rt_key = hashlib.sha256(_rt_data.encode("utf-8", "ignore")).hexdigest()
                    _redacted_thinking_cache[_rt_key] = block
                    _redacted_thinking_cache.move_to_end(_rt_key)
                    if len(_redacted_thinking_cache) > _REDACTED_THINKING_CACHE_MAX:
                        _redacted_thinking_cache.popitem(last=False)
                _debug(
                    "  [convert] CACHE redacted_thinking → upstream non-Anthropic "
                    "(conservé pour réinjection vers Anthropic)"
                )
                continue
            elif btype == "image":
                src = block.get("source", {})
                if not isinstance(src, dict):
                    continue
                stype = src.get("type", "")
                if stype == "base64":
                    media_type = src.get("media_type", "image/png")
                    data = src.get("data", "")
                    if not data:
                        continue
                    url = f"data:{media_type};base64,{data}"
                elif stype == "url":
                    url = src.get("url", "")
                    if not url:
                        continue
                else:
                    # type "file" ou inconnu → pas de fidélité OpenAI :
                    # placeholder honnête + debug, jamais de drop silencieux.
                    _debug(f"  [convert] DROP image source type={stype!r} → no OpenAI fidelity")
                    text_parts.append(f"[image:{stype or 'unknown'}]")
                    continue
                image_parts.append({"type": "image_url", "image_url": {"url": url}})
            elif btype == "document":
                src = block.get("source", {})
                if not isinstance(src, dict):
                    continue
                stype = src.get("type", "")
                if stype == "base64" and src.get("data"):
                    media_type = src.get("media_type", "application/pdf")
                    image_parts.append(
                        {
                            "type": "file",
                            "file": {
                                "file_data": f"data:{media_type};base64,{src['data']}",
                                # [A25] Le champ client est ``title`` côté
                                # Anthropic (``DocumentBlockParam`` : source,
                                # type, cache_control, citations, context,
                                # title) — PAS ``name``. Lire ``name`` seul
                                # faisait donc TOUJOURS retomber sur le défaut :
                                # le nom de fichier du client était perdu en
                                # silence (``rapport.pdf`` → ``document.pdf``).
                                # ``name`` reste lu en second pour ne pas casser
                                # les corps non conformes déjà acceptés.
                                "filename": block.get("title") or block.get("name") or "document.pdf",
                            },
                        }
                    )
                elif stype == "url" and src.get("url"):
                    # Chat Completions n'a pas de part file-par-URL : un
                    # file_data=url serait un data URI mensonger → placeholder
                    # honnête + debug, jamais de faux octets.
                    _debug(f"  [convert] DROP document url {src['url']!r} → Chat exige file_data base64 ou file_id")
                    text_parts.append(f"[document:url:{src['url']}]")
                elif stype == "file" and src.get("file_id"):
                    image_parts.append(
                        {
                            "type": "file",
                            "file": {"file_id": src["file_id"]},
                        }
                    )
                elif stype == "text" and src.get("text"):
                    import base64 as _b64mod

                    _raw = src["text"].encode("utf-8", "ignore")
                    _enc = _b64mod.b64encode(_raw).decode("ascii")
                    image_parts.append(
                        {
                            "type": "file",
                            "file": {
                                "file_data": f"data:text/plain;base64,{_enc}",
                                # [A25] même défaut que la branche base64 ci-dessus :
                                # le champ client est ``title``, pas ``name``.
                                "filename": block.get("title") or block.get("name") or "document.txt",
                            },
                        }
                    )
                else:
                    _debug(f"  [convert] DROP document source type={stype!r} → no fidelity")
                    text_parts.append(f"[document:{stype or 'unknown'}]")
                    continue
            elif btype == "tool_use":
                _tool_name = block.get("name", "")
                if not isinstance(_tool_name, str) or not _tool_name.strip():
                    _debug(f"  [convert] SKIP tool_use with empty name id={block.get('id', '?')}")
                    continue
                tool_calls.append(
                    {
                        "id": block.get("id", f"call_{uuid.uuid4().hex[:8]}"),
                        "type": "function",
                        "function": {
                            "name": _tool_name.strip(),
                            "arguments": _json_dumps_str(block.get("input", {})),
                        },
                    }
                )
            elif btype == "tool_result":
                tid = block.get("tool_use_id", "")
                if not tid:
                    # Defensive: skip tool_result with missing/empty tool_use_id
                    # (can happen after context compaction loses the id)
                    _debug("  [compact] SKIP tool_result with missing tool_use_id in anthropic_to_openai")
                    continue
                # tool_result multimodal : texte + images préservés en
                # content-list OpenAI (la Responses API accepte input_text
                # + input_image dans function_call_output.output).
                _tr_texts: list[str] = []
                _tr_images: list[dict] = []
                _tr_raw = block.get("content", "")
                _tr_blocks = _tr_raw if isinstance(_tr_raw, list) else [_tr_raw]
                for _tr_b in _tr_blocks:
                    if isinstance(_tr_b, str):
                        if _tr_b:
                            _tr_texts.append(_tr_b)
                        continue
                    if not isinstance(_tr_b, dict):
                        continue
                    _tr_t = _tr_b.get("type", "")
                    if _tr_t in ("text", "thinking"):
                        _tr_texts.append(_tr_b.get("text" if _tr_t == "text" else "thinking", ""))
                    elif _tr_t == "image":
                        _tr_src = _tr_b.get("source", {})
                        if not isinstance(_tr_src, dict):
                            continue
                        _tr_st = _tr_src.get("type", "")
                        if _tr_st == "base64" and _tr_src.get("data"):
                            _tr_images.append(
                                {
                                    "type": "image_url",
                                    "image_url": {
                                        "url": f"data:{_tr_src.get('media_type', 'image/png')};base64,{_tr_src['data']}"
                                    },
                                }
                            )
                        elif _tr_st == "url" and _tr_src.get("url"):
                            _tr_images.append(
                                {
                                    "type": "image_url",
                                    "image_url": {"url": _tr_src["url"]},
                                }
                            )
                        else:
                            _debug(f"  [convert] DROP tool_result image source type={_tr_st!r} → no fidelity")
                            _tr_texts.append(f"[image:{_tr_st or 'unknown'}]")
                    elif _tr_t == "document":
                        _tr_dsrc = _tr_b.get("source", {})
                        if not isinstance(_tr_dsrc, dict):
                            continue
                        _tr_dst = _tr_dsrc.get("type", "")
                        if _tr_dst == "base64" and _tr_dsrc.get("data"):
                            _tr_images.append(
                                {
                                    "type": "file",
                                    "file": {
                                        "file_data": f"data:{_tr_dsrc.get('media_type', 'application/pdf')};base64,{_tr_dsrc['data']}",
                                        # [A25] champ client ``title`` (cf. branche
                                        # document homologue plus haut).
                                        "filename": _tr_b.get("title") or _tr_b.get("name") or "document.pdf",
                                    },
                                }
                            )
                        elif _tr_dst == "file" and _tr_dsrc.get("file_id"):
                            _tr_images.append({"type": "file", "file": {"file_id": _tr_dsrc["file_id"]}})
                        else:
                            # url / text / inconnu : pas de part file-par-URL en
                            # Chat → placeholder honnête, jamais de faux octets.
                            _debug(f"  [convert] DROP tool_result document source type={_tr_dst!r} → no Chat fidelity")
                            _tr_texts.append(f"[document:{_tr_dst or 'unknown'}]")
                    elif _tr_t == "input_audio":
                        _debug("  [convert] DROP tool_result audio → no Chat tool fidelity (placeholder)")
                        _tr_texts.append("[audio:unsupported-in-chat-tool-result]")
                    else:
                        _debug(f"  [convert] DROP tool_result block type={_tr_t!r} → placeholder")
                        _tr_texts.append(f"[{_tr_t or 'unknown'}]")
                _tr_content: str | list = "\n".join(_tr_texts)
                if _tr_images:
                    _tr_list: list[dict] = []
                    if _tr_content:
                        _tr_list.append({"type": "text", "text": _tr_content})
                    _tr_list.extend(_tr_images)
                    _tr_content = _tr_list
                tool_results.append(
                    {
                        "role": "tool",
                        "tool_call_id": tid,
                        "content": _tr_content,
                    }
                )
                if "cache_control" in block:
                    last_cache_control = block["cache_control"]

        # Emit tool_result messages first (must immediately follow assistant's tool_calls)
        messages.extend(tool_results)

        # Then emit the main message (text + tool_calls + thinking + images)
        joined_thinking = "\n".join(thinking_parts) if thinking_parts else ""

        # Si images présentes → content en liste mixte OpenAI (text + image_url)
        if image_parts:
            content_list: list[dict] = []
            joined_text = "\n".join(text_parts) if text_parts else ""
            if joined_text:
                content_list.append({"type": "text", "text": joined_text})
            content_list.extend(image_parts)
            if tool_calls:
                out = {
                    "role": role,
                    "content": content_list,
                    "tool_calls": tool_calls,
                }
                if joined_thinking:
                    out["reasoning_content"] = joined_thinking
                elif thinking and is_asst:
                    out["reasoning_content"] = " "
                if last_cache_control and not is_asst and supports_cache_control:
                    out["cache_control"] = last_cache_control
                messages.append(out)
            elif content_list or thinking_parts or (thinking and is_asst):
                # Pas de tool_calls mais images (+ éventuellement texte)
                if len(content_list) == 1 and content_list[0].get("type") == "text":
                    # Seul du texte → garder le format string simple (compat)
                    out = {"role": role, "content": content_list[0]["text"]}
                else:
                    out = {"role": role, "content": content_list}
                if joined_thinking:
                    out["reasoning_content"] = joined_thinking
                elif thinking and is_asst:
                    out["reasoning_content"] = " "
                if last_cache_control and not is_asst and supports_cache_control:
                    out["cache_control"] = last_cache_control
                messages.append(out)
        elif tool_calls:
            out = {
                "role": role,
                "content": "\n".join(text_parts) if text_parts else "",
                "tool_calls": tool_calls,
            }
            if joined_thinking:
                out["reasoning_content"] = joined_thinking
            elif thinking and is_asst:
                out["reasoning_content"] = " "
            if last_cache_control and not is_asst and supports_cache_control:
                out["cache_control"] = last_cache_control
            messages.append(out)
        elif text_parts or thinking_parts or (thinking and is_asst):
            out = {"role": role, "content": "\n".join(text_parts) if text_parts else ""}
            if joined_thinking:
                out["reasoning_content"] = joined_thinking
            elif thinking and is_asst:
                out["reasoning_content"] = " "
            if last_cache_control and not is_asst and supports_cache_control:
                out["cache_control"] = last_cache_control
            messages.append(out)

    # ── Orphan filter: drop role:tool without preceding tool_calls id ──
    messages = _drop_orphan_tool_messages(messages)

    # Add cache_control to the last user message for optimal prefix caching
    # (Anthropic best practice: cache system + last user turn)
    if supports_cache_control:
        for i in range(len(messages) - 1, -1, -1):
            if messages[i].get("role") == "user":
                # [Lot L11 — B3] On n'ÉCRASE pas un breakpoint client : le TTL
                # explicite d'un client (`{"ttl":"1h"}`) doit survivre à notre
                # ajout, et il ne doit pas consommer deux fois le quota de 4.
                messages[i].setdefault("cache_control", {"type": "ephemeral"})
                _cache_control_to_openai_breakpoint(messages[i])
                break

    # Build request
    oai = {
        "model": model,
        "messages": messages,
        "stream": body.get("stream", False),
    }
    # [Lot L14 — B2/A17] Forme du champ de limite adaptée à l'upstream : les
    # modèles de raisonnement exigent `max_completion_tokens`.
    _set_output_token_limit(oai, body, model)

    for key, oai_key in [
        ("temperature", "temperature"),
        ("top_p", "top_p"),
        ("stop_sequences", "stop"),
    ]:
        if key in body:
            oai[oai_key] = body[key]

    # [Lot L4 — A8] Map de renommage des outils pour CETTE conversion.
    # Initialisée AVANT le bloc conditionnel ``tools`` : la référencer plus bas
    # (marqueur de retour) sur un chemin sans outils lèverait un UnboundLocalError.
    _chat_name_map: dict = {}
    if "tools" in body:
        # Support both Anthropic format (name at top level) and OpenAI format (function.name)
        # v3.3: preserve server tools (web_search/web_fetch) natively, fix else branch B4
        oai_tools = []

        def _carry_cc(dst: dict, src: dict) -> dict:
            """[Lot L3 — A5] Reporte le breakpoint de cache d'un outil.

            La boucle reconstruisait chaque outil sans reporter ``cache_control`` :
            un client posant un breakpoint sur un outil le perdait
            silencieusement, alors que le même breakpoint posé sur un message est
            bien reporté. On aligne les deux comportements — la perte silencieuse
            est le seul vrai défaut ici (§9.3 : l'amont OpenAI ignore ce champ,
            il n'est donc pas nuisible de le transporter).

            [Lot L11 — B3] Il n'y a PAS d'équivalent OpenAI émis ici, et c'est une
            décision, pas un oubli : ``_cache_control_to_openai_breakpoint`` est un
            no-op assumé (voir sa docstring). B3 place ``prompt_cache_breakpoint``
            sur les **content parts**, jamais sur une définition d'outil — or c'est
            une définition d'outil qu'on traite ici. Inventer le champ à ce niveau
            risquerait un 400 chez un amont strict pour un gain nul. Seul
            ``cache_control`` est donc transporté, ce qui suffit aux amonts
            Anthropic-compatibles ; la traduction par part est rattachée à L15.
            """
            cc = src.get("cache_control") if isinstance(src, dict) else None
            if cc:
                dst["cache_control"] = cc
                _cache_control_to_openai_breakpoint(dst)
            return dst

        for t in body["tools"]:
            # Server tools: preserve natively if type is web_*
            t_type = t.get("type", "")
            if isinstance(t_type, str) and t_type.startswith("web_"):
                # Keep server tool as-is (e.g., web_search_2025_03_05)
                # If converting to OpenAI and target is anthropic-native, preserve; otherwise keep type
                if "name" in t and t.get("name"):
                    oai_tools.append(
                        _carry_cc(
                            {
                                "type": t_type,
                                "name": t.get("name"),
                                "description": t.get("description", ""),
                                "input_schema": t.get("input_schema", {}),
                            },
                            t,
                        )
                    )
                else:
                    # type without name, e.g., {"type":"web_search_2025_03_05"} -> keep
                    oai_tools.append(
                        _carry_cc({"type": t_type, "name": t.get("name", "web_search")}, t)
                    )
                continue
            if "name" in t:
                # Anthropic format: {"name": "...", "description": "...", "input_schema": {...}}
                params = _normalize_tool_schema(t.get("input_schema", {}) or {}, model)
                oai_tools.append(
                    _carry_cc(
                        {
                            "type": "function",
                            "function": {
                                "name": t["name"],
                                "description": t.get("description", ""),
                                "parameters": params,
                            },
                        },
                        t,
                    )
                )
            elif "function" in t:
                # OpenAI format: {"type": "function", "function": {"name": "...", ...}}
                fn = t["function"]
                params = _normalize_tool_schema(fn.get("parameters", {}) or {}, model)
                oai_tools.append(
                    _carry_cc(
                        {
                            "type": "function",
                            "function": {
                                "name": fn.get("name", ""),
                                "description": fn.get("description", ""),
                                "parameters": params,
                            },
                        },
                        t,
                    )
                )
            else:
                # Unknown format: B4 fix - don't produce {"name":""}; check if web_* type without name
                if isinstance(t_type, str) and t_type.startswith("web_"):
                    oai_tools.append(
                        _carry_cc({"type": t_type, "name": t.get("name", "web_search")}, t)
                    )
                elif t.get("name"):
                    params = _normalize_tool_schema(t.get("input_schema", t.get("parameters", {})) or {}, model)
                    oai_tools.append(
                        _carry_cc(
                            {
                                "type": "function",
                                "function": {
                                    "name": t.get("name", ""),
                                    "description": t.get("description", ""),
                                    "parameters": params,
                                },
                            },
                            t,
                        )
                    )
                else:
                    # skip invalid tool without name
                    _debug(f"  [convert] SKIP tool without name type={t_type!r}")
                    continue
        if oai_tools:
            # [Lot L4 — A8] Sanitize vers la limite Chat (64) : sans cela un nom
            # Anthropic valide (>64, jusqu'à 200) partait tel quel et l'amont
            # Chat répondait 400. La map repart au client via _TOOL_NAME_MAP_KEY.
            oai["tools"] = _sanitize_chat_tools(oai_tools, _chat_name_map)
        tc = body.get("tool_choice", "auto")
        if isinstance(tc, dict):
            tc_type = tc.get("type", "auto")
            if tc_type == "tool":
                oai["tool_choice"] = {"type": "function", "function": {"name": tc.get("name", "")}}
            elif tc_type == "any":
                oai["tool_choice"] = "required"
            else:
                oai["tool_choice"] = "auto"
        else:
            oai["tool_choice"] = tc
        # [Lot L4 — A8] Le tool_choice nommé doit désigner le nom RÉELLEMENT
        # envoyé, sinon l'amont cherche un outil inexistant (400/422).
        if isinstance(oai.get("tool_choice"), dict):
            oai["tool_choice"] = _remap_chat_tool_choice(oai["tool_choice"], _chat_name_map)

    # [Lot L4 — A8] Historique : les tool_calls des tours précédents suivent le
    # même rename que tools[] (hors ce bloc : l'historique peut porter des noms
    # même quand cette requête n'envoie aucun outil — short défensif).
    _chat_hist = oai.get("messages")
    if isinstance(_chat_hist, list) and _chat_hist:
        _remap_chat_history_names(_chat_hist, _chat_name_map)

    # Convert Anthropic thinking/effort → OpenAI reasoning parameters.
    # [Lot L2] SOURCE UNIQUE : ``config.effort_policy.resolve_effort`` lit toutes
    # les formes clientes (``output_config.effort`` [A13], ``effort``,
    # ``reasoning_effort`` relais P6, ``thinking.type``/``budget_tokens``) et
    # applique le plafond modèle de la config. Plus de table locale : un
    # ``effort: high`` produit le MÊME résultat sur les 6 chemins (A1).
    _decision = _resolve_effort(body, model)
    if _decision.wants and _decision.level:
        oai["reasoning_effort"] = _decision.level
        _debug(
            f"  [thinking] {model}: reasoning_effort={_decision.level} "
            f"(source={_decision.source}, explicit={_decision.explicit})"
        )

    # ── [Lot L11 — A20] Clôture cache : top-level + plafond 4 ──
    # L'ancien retour anticipé sur la source ``reasoning_effort`` est supprimé :
    # il court-circuitait cette clôture, donc un client relayant
    # ``reasoning_effort`` échappait au plafond de breakpoints ET à la
    # réécriture de cache. Le seul effet visé (« ne pas restructurer deux fois »)
    # est conservé par le fait qu'on ne restructure plus qu'une fois ici.
    def _close_cache(out: dict) -> dict:
        _apply_top_level_cache_control(out, body)
        _enforce_cache_breakpoint_limit(out)
        return out

    # ── [Lot L13 — B1] Marqueur de repli « reasoning_content » ──
    # ``reasoning_content`` n'est dans AUCUNE spec OpenAI : c'est une convention
    # vendeur (DeepSeek/GLM/Kimi…). Un upstream STRICT peut donc le rejeter en
    # 400/422. On pose ici un marqueur interne (jamais sérialisé sur le wire,
    # cf. ``_serialize_body``) pour que le handler puisse rejouer UNE fois la
    # requête sans le champ, au lieu de casser le tour entier (texte + tool
    # calls) pour tous les clients routés sur cet endpoint. Même mécanisme que
    # le retry-once des items ``reasoning`` de ``/responses``.
    # Le raisonnement est un ENRICHISSEMENT, jamais un bloquant : on dégrade
    # (perte de la mémoire du raisonnement) plutôt que d'échouer.
    _final = _restructure_for_cache(_close_cache(oai), model)
    if isinstance(_final, dict) and any(
        isinstance(_m, dict) and _m.get("reasoning_content")
        for _m in _final.get("messages", [])
    ):  # fmt: skip
        _final[_HAS_SYNTHETIC_REASONING_KEY] = True
    # [Lot L4 — A8] Map de restauration aller→client, transportée par requête
    # (clé privée, jamais globale) ; stripée au dernier kilomètre par
    # ``_serialize_json_body`` — l'amont ne la voit jamais. Posée seulement si
    # un renommage a eu lieu (sinon aucune restauration n'est nécessaire).
    if isinstance(_final, dict) and _chat_name_map:
        _final[_TOOL_NAME_MAP_KEY] = _chat_name_map
    return _final


_orig_anthropic_to_openai = anthropic_to_openai
# [P4] LRU borné : OrderedDict move-to-end + drop-oldest (l'ancien dict
# gelerait le contenu à 512 entrées — plus aucun nouveau body mis en cache).
_anthropic_cache: OrderedDict = OrderedDict()
_anthropic_cache_max = 512

# [plan Lot 0] compteurs hit-rate exposition /metrics (fail-soft, zéro lock :
# incréments GIL-atomiques suffisent pour de l'observabilité).
_conversion_hits = 0
_conversion_misses = 0


def conversion_cache_stats() -> dict:
    """Snapshot des compteurs hit/miss du cache de conversion (Lot 0)."""
    hit = _conversion_hits
    miss = _conversion_misses
    total = hit + miss
    return {"hit": hit, "miss": miss, "hit_rate": (hit / total) if total else 0.0}


def _conversion_epoch() -> int:
    """[P4 correctesse] version de routage mélangée à la clé de conversion :
    tout hot-reload touchant les règles (ROUTE_VERSION++ via save_env /
    save_custom_routes / reload mtime) rend les entrées précédentes
    introuvables — fini la staleness permanente après rechargement."""
    try:
        return int(_cfg_settings.ROUTE_VERSION)
    except Exception:
        return 0


def _anthropic_cache_key(model: str, body: dict, raw: bytes | None = None) -> str:
    """[C1 perf/correctesse] clé de cache blake2b(epoch ‖ body_bytes ‖ model).

    Remplace ``hash(json.dumps(body))`` : plus de dumps complet par requête
    quand les bytes bruts sont disponibles chez l'appelant, et surtout plus
    de collisions ``hash()`` Python (randomisé + 64-bit collisionnable →
    une MAUVAISE conversion pouvait être servie). blake2b-128 : collision
    pratiquement impossible ; le model est mélangé séparément (séparateur
    nul) pour éviter toute ambiguïté de concaténation.
    """
    h = hashlib.blake2b(digest_size=16)
    h.update(f"{_conversion_epoch()}\x00".encode("ascii", "replace"))
    if raw is not None:
        h.update(raw)
    else:
        h.update(_json_dumps(body))
    h.update(b"\x00")
    h.update(model.encode("utf-8", "replace"))
    return h.hexdigest()


def anthropic_to_openai(body: dict, model: str, raw: bytes | None = None) -> dict:  # type: ignore[no-redef]
    # ^ [P4] wrapper de cache volontairement rebaptisé du même nom que
    # l'implémentation d'origine (ligne ~308) — pattern décorateur manuel ;
    # l'originale reste joignable via _orig_anthropic_to_openai.
    global _conversion_hits, _conversion_misses
    try:
        # B2c: invalidate cache if body contains role None (poison)
        for _m in body.get("messages", []) or []:
            if isinstance(_m, dict) and _m.get("role") is None:
                _debug("  [cache] skip cache — role None detected")
                return _orig_anthropic_to_openai(body, model)
        key = _anthropic_cache_key(model, body, raw)
        hit = _anthropic_cache.get(key)
        if hit is not None:
            # [P4] LRU : le hit rafraîchit la position (move-to-end).
            _anthropic_cache.move_to_end(key)
            _conversion_hits += 1
            # [C1] shallow copy TOP-LEVEL uniquement : audit plan §3 — les
            # mutations post-conversion des callers touchent des clés racine
            # (model / stream_options / min_tokens), jamais les structures
            # imbriquées partagées. Fini les deepcopy hit ET miss.
            return dict(hit)
        _conversion_misses += 1
        res = _orig_anthropic_to_openai(body, model)
        # Objet stocké JAMAIS exposé tel quel (le caller reçoit une copie
        # racine) → le cache reste pristine sans deepcopy.
        _anthropic_cache[key] = res
        while len(_anthropic_cache) > _anthropic_cache_max:
            _anthropic_cache.popitem(last=False)  # drop-oldest
        return dict(res)
    except Exception:
        return _orig_anthropic_to_openai(body, model)


def _local_signature(text: str) -> str:
    """[v10 PLAN-raisonnement 2.1] Signature LOCALE (HMAC SHA256 base64).

    La signature Anthropic authentique est cryptographique côté modèle — le
    proxy ne peut pas la forger. Les clients Anthropic-compatibles exigent
    néanmoins un champ `signature` non vide sur les blocs thinking (sinon
    abandonnés en multi-tours). On signe localement : les clients stockent et
    re-transmettent sans valider ; on ne renvoie jamais ces blocs synthétiques
    aux upstreams stricts (strip multi-tours, PLAN-raisonnement Phase D)."""
    import base64
    import hashlib
    import hmac

    key = b"opencode-proxy-local-thinking-signature-v1"
    return base64.b64encode(hmac.new(key, text.encode("utf-8"), hashlib.sha256).digest()).decode()


# [P5.1 perf] LRU bornée pour _is_local_signature — les historiques multi-tours
# ré-émettent les mêmes blocs → hit-rate élevé, zéro changement sémantique.
_is_local_sig_cache: OrderedDict[bytes, bool] = OrderedDict()
_IS_LOCAL_SIG_CACHE_MAX = 2048


def _is_local_signature(text: str, signature: str) -> bool:
    """[PLAN-raisonnement Phase D] Détecte une signature FORGÉE par le proxy.

    Provenance stateless : on recalcule le HMAC local du texte et on compare.
    Une signature authentique (Anthropic) ne peut pas correspondre — elle
    n'est pas produite par notre clé. Un bloc thinking re-émis par le client
    avec NOTRE signature est donc identifiable sans table d'état.

    [P5.1 perf] Mémoïsation LRU bornée (clé blake2b(text+signature), 2048 entrées)
    pour éviter le recalcul HMAC par bloc/requête."""
    if not isinstance(signature, str) or not signature:
        return False
    if not isinstance(text, str) or not text:
        return False
    try:
        # clé 128-bit blake2b : collision négligeable, calcul rapide
        h = hashlib.blake2b(digest_size=16)
        h.update(text.encode("utf-8"))
        h.update(b"\x00")
        h.update(signature.encode("utf-8"))
        key = h.digest()
        cached = _is_local_sig_cache.get(key)
        if cached is not None:
            _is_local_sig_cache.move_to_end(key)
            return cached
        import hmac as _hmac

        result = _hmac.compare_digest(_local_signature(text), signature)
        _is_local_sig_cache[key] = result
        _is_local_sig_cache.move_to_end(key)
        if len(_is_local_sig_cache) > _IS_LOCAL_SIG_CACHE_MAX:
            _is_local_sig_cache.popitem(last=False)
        return result
    except Exception:
        return False


def _looks_encrypted_reasoning(text: str) -> bool:
    """Heuristique `reasoning_content` chiffré/binaire → redacted_thinking.

    Un raisonnement réel contient espaces et ponctuation ; un blob chiffré
    (base64) est une longue chaîne sans espace, multiple de 4, charset base64.
    """
    if not isinstance(text, str):
        return False
    t = text.strip()
    if len(t) < 64 or len(t) % 4 != 0 or " " in t:
        return False
    return re.fullmatch(r"[A-Za-z0-9+/]+={0,2}", t) is not None


def strip_synthetic_thinking(body: dict) -> int:
    """[PLAN-raisonnement Phase D.3] Strip sélectif dans l'historique Anthropic.

    Retire des messages les blocs `thinking` dont la signature est une
    signature LOCALE du proxy (blocs synthétisés par conversion reasoning_content).
    Ces blocs ne partent JAMAIS vers un upstream : seul Anthropic direct valide
    cryptographiquement les signatures au tour suivant, et on ne lui ment pas.

    Les blocs ORIGINAUX (signature authentique du modèle source) et
    `redacted_thinking` (donnée chiffrée authentique) passent intacts.
    Retourne le nombre de blocs strippés."""
    stripped = 0
    for msg in body.get("messages", []) or []:
        content = msg.get("content") if isinstance(msg, dict) else None
        if not isinstance(content, list):
            continue
        kept = []
        for block in content:
            if (
                isinstance(block, dict)
                and block.get("type") == "thinking"
                and _is_local_signature(block.get("thinking", ""), block.get("signature", ""))
            ):
                stripped += 1
                _debug(
                    "  [thinking] DROP bloc thinking à signature LOCALE de l'historique "
                    "(jamais transmis aux upstreams, PLAN-raisonnement D)"
                )
                continue
            kept.append(block)
        msg["content"] = kept
    return stripped


def openai_to_anthropic(resp: dict, model: str, name_map: dict | None = None) -> dict:
    """Convertit une réponse Chat en réponse Anthropic.

    ``name_map`` ([Lot L4 — A8]) est la map ``{short: original}`` produite à
    l'aller par ``_sanitize_chat_tools`` : sans elle, un client qui avait envoyé
    un nom d'outil > 64 caractères recevrait le nom *raccourci* dans le bloc
    ``tool_use``, c'est-à-dire un outil qu'il ne reconnaît pas. Restaure le nom
    d'origine tel quel quand aucune map n'est fournie (jamais de renommage).
    """
    choice = resp.get("choices", [{}])[0]
    msg = choice.get("message", {})
    usage = resp.get("usage", {})

    blocks = []
    if reasoning := msg.get("reasoning_content") or msg.get("reasoning"):
        if _looks_encrypted_reasoning(reasoning):
            # [PLAN-raisonnement Phase B.2] raisonnement chiffré/binaire de
            # l'upstream → bloc redacted_thinking (pas de signature forgée sur
            # une donnée qu'on ne peut pas signer)
            blocks.append({"type": "redacted_thinking", "data": reasoning})
        else:
            blocks.append(
                {
                    "type": "thinking",
                    "thinking": reasoning,
                    "signature": _local_signature(reasoning),
                }
            )
    if msg.get("content"):
        blocks.append({"type": "text", "text": msg["content"]})
    for tc in msg.get("tool_calls") or []:
        fn = tc.get("function", {})
        try:
            inp = _json_loads(fn.get("arguments", "{}"))
        except Exception:
            inp = {}
        blocks.append(
            {
                "type": "tool_use",
                "id": tc.get("id", f"toolu_{uuid.uuid4().hex[:8]}"),
                # [Lot L4 — A8] Restore : le client doit retrouver le nom qu'il a
                # envoyé (la limite 64 est une contrainte de l'amont Chat, pas
                # du client Anthropic, qui autorise 200).
                "name": restore_tool_name(fn.get("name", ""), name_map),
                "input": inp,
            }
        )

    if not blocks:
        blocks.append({"type": "text", "text": ""})

    stop = "tool_use" if msg.get("tool_calls") else "end_turn"
    if choice.get("finish_reason") == "length":
        stop = "max_tokens"

    return {
        "id": f"msg_{uuid.uuid4().hex[:24]}",
        "type": "message",
        "role": "assistant",
        "content": blocks,
        "model": model,
        "stop_reason": stop,
        "stop_sequence": None,
        "usage": {
            "input_tokens": usage.get("prompt_tokens", 0),
            "output_tokens": usage.get("completion_tokens", 0),
            "cache_creation_input_tokens": _extract_cache_creation_tokens(usage),
            "cache_read_input_tokens": usage.get("prompt_tokens_details", {}).get("cached_tokens", 0),
        },
    }


def openai_to_anthropic_request(oai_body: dict) -> dict:
    """Convert OpenAI Chat Completions request → Anthropic Messages format."""
    system_text = ""
    pending_tool_results = []
    anthro_messages = []

    for msg in oai_body.get("messages", []):
        role = msg.get("role", "")

        if role == "system":
            system_text = _extract_text(msg.get("content", ""))
            continue

        if role == "tool":
            # tool_result multimodal : texte + images préservés (pas d'aplat
            # _extract_text qui perdait les bytes image).
            _o_tool_content = msg.get("content", "")
            _o_tr_blocks: list = []
            if isinstance(_o_tool_content, str):
                if _o_tool_content:
                    _o_tr_blocks = [{"type": "text", "text": _o_tool_content}]
            elif isinstance(_o_tool_content, list):
                for _o_b in _o_tool_content:
                    if isinstance(_o_b, str):
                        if _o_b:
                            _o_tr_blocks.append({"type": "text", "text": _o_b})
                    elif isinstance(_o_b, dict):
                        _o_bt = _o_b.get("type", "")
                        if _o_bt == "text" and _o_b.get("text"):
                            _o_tr_blocks.append({"type": "text", "text": _o_b["text"]})
                        elif _o_bt == "image_url":
                            _o_url = (_o_b.get("image_url") or {}).get("url", "")
                            if not _o_url:
                                continue
                            if _o_url.startswith("data:"):
                                try:
                                    _o_h, _o_b64 = _o_url.split(",", 1)
                                    _o_m = (
                                        _o_h.split(";")[0].split(":")[1] if ";" in _o_h else "image/png"
                                    ) or "image/png"
                                except ValueError:
                                    _o_m, _o_b64 = "image/png", ""
                                if not _o_b64:
                                    continue
                                _o_tr_blocks.append(
                                    {
                                        "type": "image",
                                        "source": {
                                            "type": "base64",
                                            "media_type": _o_m,
                                            "data": _o_b64,
                                        },
                                    }
                                )
                            else:
                                _o_tr_blocks.append(
                                    {
                                        "type": "image",
                                        "source": {"type": "url", "url": _o_url},
                                    }
                                )
                        elif _o_bt == "image":
                            _o_tr_blocks.append(_o_b)
                        elif _o_bt == "file":
                            _o_f = _o_b.get("file") or {}
                            if not isinstance(_o_f, dict):
                                continue
                            _o_fdata = _o_f.get("file_data", "") or ""
                            _o_ffid = _o_f.get("file_id", "") or ""
                            _o_fname = _o_f.get("filename", "") or ""
                            if isinstance(_o_fdata, str) and _o_fdata.startswith("data:"):
                                try:
                                    _o_fh, _, _o_fd = _o_fdata[5:].partition(",")
                                    _o_fm = (_o_fh.split(";")[0] or "").strip() or "application/pdf"
                                except Exception:
                                    _o_fm, _o_fd = "application/pdf", ""
                                if not _o_fd:
                                    continue
                                _o_doc: dict = {
                                    "type": "document",
                                    "source": {"type": "base64", "media_type": _o_fm, "data": _o_fd},
                                }
                                if _o_fname:
                                    _o_doc["name"] = _o_fname
                                _o_tr_blocks.append(_o_doc)
                            elif _o_ffid:
                                _o_tr_blocks.append(
                                    {"type": "document", "source": {"type": "file", "file_id": _o_ffid}}
                                )
                            elif isinstance(_o_fdata, str) and (
                                _o_fdata.startswith("http://") or _o_fdata.startswith("https://")
                            ):
                                # Anthropic ne garantit document-URL que pour les PDF.
                                if _o_fname.lower().endswith(".pdf"):
                                    _o_doc2: dict = {
                                        "type": "document",
                                        "source": {"type": "url", "url": _o_fdata},
                                    }
                                    _o_doc2["name"] = _o_fname
                                    _o_tr_blocks.append(_o_doc2)
                                else:
                                    _debug(f"  [convert] DROP tool file URL non-PDF {_o_fdata!r} → placeholder")
                                    _o_tr_blocks.append({"type": "text", "text": f"[document:url:{_o_fdata}]"})
                            else:
                                _debug("  [convert] DROP tool file sans file_data ni file_id → skip")
                        elif _o_bt == "input_audio":
                            _debug("  [convert] DROP tool input_audio → Anthropic sans audio (placeholder)")
                            _o_tr_blocks.append({"type": "text", "text": "[audio:unsupported-by-anthropic]"})
            # Contrat historique : texte seul → string (pas de liste à 1 bloc).
            if len(_o_tr_blocks) == 1 and _o_tr_blocks[0].get("type") == "text" and isinstance(_o_tool_content, str):
                _o_tr_content: str | list = _o_tr_blocks[0]["text"]
            else:
                _o_tr_content = _o_tr_blocks or _extract_text(_o_tool_content)
            pending_tool_results.append(
                {
                    "type": "tool_result",
                    "tool_use_id": msg.get("tool_call_id", ""),
                    "content": _o_tr_content,
                }
            )
            continue

        if role not in ("user", "assistant"):
            continue

        blocks = []

        # Prepend pending tool_results to the next user message
        if role == "user" and pending_tool_results:
            blocks.extend(pending_tool_results)
            pending_tool_results = []

        # Convert content
        content = msg.get("content", "")
        if isinstance(content, str):
            if content:
                blocks.append({"type": "text", "text": content})
        elif isinstance(content, list):
            for block in content:
                t = block.get("type", "")
                if t == "text":
                    blocks.append({"type": "text", "text": block.get("text", "")})
                elif t == "image_url":
                    url_obj = block.get("image_url", {})
                    url = url_obj.get("url", "") if isinstance(url_obj, dict) else ""
                    if not url:
                        continue
                    if url.startswith("data:"):
                        try:
                            header, b64 = url.split(",", 1)
                            media_type = header.split(";")[0].split(":")[1] if ";" in header else "image/png"
                            if not media_type:
                                media_type = "image/png"
                        except ValueError:
                            media_type = "image/png"
                            b64 = ""
                        if not b64:
                            continue
                        blocks.append(
                            {
                                "type": "image",
                                "source": {"type": "base64", "media_type": media_type, "data": b64},
                            }
                        )
                    else:
                        blocks.append(
                            {
                                "type": "image",
                                "source": {"type": "url", "url": url},
                            }
                        )
                elif t == "file":
                    _cf = block.get("file") or {}
                    if not isinstance(_cf, dict):
                        continue
                    _cdata = _cf.get("file_data", "") or ""
                    _cfid = _cf.get("file_id", "") or ""
                    _cname = _cf.get("filename", "") or ""
                    if isinstance(_cdata, str) and _cdata.startswith("data:"):
                        try:
                            _ch, _, _cd = _cdata[5:].partition(",")
                            _cm = (_ch.split(";")[0] or "").strip() or "application/pdf"
                        except Exception:
                            _cm, _cd = "application/pdf", ""
                        if _cd:
                            _cb: dict = {
                                "type": "document",
                                "source": {"type": "base64", "media_type": _cm, "data": _cd},
                            }
                            if _cname:
                                _cb["name"] = _cname
                            blocks.append(_cb)
                    elif _cfid:
                        blocks.append({"type": "document", "source": {"type": "file", "file_id": _cfid}})
                    elif isinstance(_cdata, str) and (_cdata.startswith("http://") or _cdata.startswith("https://")):
                        # Anthropic ne garantit document-URL que pour les PDF.
                        if _cname.lower().endswith(".pdf"):
                            _cb2: dict = {
                                "type": "document",
                                "source": {"type": "url", "url": _cdata},
                            }
                            _cb2["name"] = _cname
                            blocks.append(_cb2)
                        else:
                            _debug(f"  [convert] DROP file URL non-PDF {_cdata!r} → placeholder")
                            blocks.append({"type": "text", "text": f"[document:url:{_cdata}]"})
                    else:
                        _debug("  [convert] DROP file sans file_data ni file_id → skip")
                elif t == "input_audio":
                    _debug("  [convert] DROP input_audio → Anthropic sans audio (placeholder)")
                    blocks.append({"type": "text", "text": "[audio:unsupported-by-anthropic]"})
                elif t not in ("video", "video_url"):
                    _debug(f"  [convert] DROP chat part type={t!r} → placeholder")
                    blocks.append({"type": "text", "text": f"[{t or 'unknown'}]"})
                else:
                    _debug("  [convert] DROP video → aucune API (placeholder)")
                    blocks.append({"type": "text", "text": "[video:unsupported]"})

        # Convert tool_calls (assistant only)
        for tc in msg.get("tool_calls") or []:
            fn = tc.get("function", {})
            try:
                inp = _json_loads(fn.get("arguments", "{}"))
            except Exception:
                inp = {}
            blocks.append(
                {
                    "type": "tool_use",
                    "id": tc.get("id", f"toolu_{uuid.uuid4().hex[:8]}"),
                    "name": fn.get("name", ""),
                    "input": inp,
                }
            )

        # [Lot H2 — remplace Phase D.3] reasoning_content historique → bloc
        # thinking avec signature locale, symétrique du sens réponse
        # (openai_to_anthropic). Sécurité : les blocs à signature LOCALE sont
        # reconnus et retirés par strip_synthetic_thinking (appelé sur la voie
        # Anthropic, opencode.py) avant l'envoi à un upstream strict qui valide
        # cryptographiquement les signatures — jamais de 400 forgé ; vers un
        # upstream compatible le raisonnement voyage au lieu d'être perdu.
        _hist_reasoning = msg.get("reasoning_content") or msg.get("reasoning")
        if role == "assistant" and isinstance(_hist_reasoning, str) and _hist_reasoning.strip():
            blocks.insert(
                0,
                {
                    "type": "thinking",
                    "thinking": _hist_reasoning,
                    "signature": _local_signature(_hist_reasoning),
                },
            )

        # Ensure at least one block
        if not blocks:
            blocks.append({"type": "text", "text": ""})

        anthro_messages.append({"role": role, "content": blocks})

    # Trailing tool_results (edge case)
    if pending_tool_results:
        anthro_messages.append({"role": "user", "content": pending_tool_results})

    result = {
        "model": oai_body.get("model", ""),
        "messages": anthro_messages,
        "stream": oai_body.get("stream", False),
    }
    # [Lot L14 — A17] Lit `max_tokens` ET `max_completion_tokens` : la forme
    # moderne était ignorée, la limite du client remplacée par le défaut 16384.
    # Destination Anthropic → toujours `max_tokens`.
    _set_output_token_limit(
        result, oai_body, oai_body.get("model", ""), target_protocol="anthropic"
    )

    if system_text:
        result["system"] = system_text

    # Map simple params
    for key, anthro_key in [
        ("temperature", "temperature"),
        ("top_p", "top_p"),
        ("stop", "stop_sequences"),
    ]:
        if key in oai_body:
            result[anthro_key] = oai_body[key]

    # Convert tools - v3.3: preserve server tools B4
    if "tools" in oai_body:
        model = oai_body.get("model", "")
        anthro_tools = []
        for t in oai_body["tools"]:
            t_type = t.get("type", "")
            # Server tools (web_search/web_fetch) - preserve
            if isinstance(t_type, str) and t_type.startswith("web_"):
                # OpenAI server tool format: {"type":"web_search_2025_03_05", "name":"web_search"} or similar
                name = t.get("name") or t.get("function", {}).get("name", "web_search")
                # normalize
                if "web_search" in t_type:
                    name = "web_search"
                elif "web_fetch" in t_type:
                    name = "web_fetch"
                anthro_tools.append(
                    {
                        "name": name,
                        "description": t.get("description", ""),
                        "input_schema": t.get("input_schema", t.get("parameters", {})),
                    }
                )
                continue
            if t.get("type") == "function":
                fn = t.get("function", {})
                if not fn.get("name"):
                    _debug("  [convert] SKIP function tool without name")
                    continue
                schema = _normalize_tool_schema(fn.get("parameters", {}) or {}, model)
                anthro_tools.append(
                    {
                        "name": fn.get("name", ""),
                        "description": fn.get("description", ""),
                        "input_schema": schema,
                    }
                )
            elif "function" in t:
                fn = t["function"]
                if not fn.get("name"):
                    continue
                schema = _normalize_tool_schema(fn.get("parameters", {}) or {}, model)
                anthro_tools.append(
                    {
                        "name": fn["name"],
                        "description": fn.get("description", ""),
                        "input_schema": schema,
                    }
                )
        if anthro_tools:
            result["tools"] = anthro_tools

        # Convert tool_choice
        tc = oai_body.get("tool_choice", "auto")
        if isinstance(tc, dict):
            tc_type = tc.get("type", "auto")
            if tc_type == "function":
                result["tool_choice"] = {
                    "type": "tool",
                    "name": tc.get("function", {}).get("name", ""),
                }
            elif tc_type == "any":
                result["tool_choice"] = {"type": "any"}
            else:
                result["tool_choice"] = tc_type
        else:
            result["tool_choice"] = tc

    # [Lot H4 / Lot L2] Sens inverse de _effort_to_reasoning : un client OpenAI
    # qui envoie reasoning_effort doit obtenir une config thinking Anthropic
    # quand la requête est routée vers un upstream Anthropic (P4/P26).
    #
    # [Lot L2] On passe par la SOURCE UNIQUE : ``resolve_effort`` lit
    # ``reasoning_effort`` mais aussi ``output_config.effort``, ``effort``,
    # ``reasoning.effort`` et ``thinking.*`` — un client peut donc envoyer
    # n'importe laquelle de ces formes sur cette porte (A1). L'ancien dict codé
    # en dur ``{low:4096, medium:10000, high:16000}`` repliait ``xhigh``/``max``
    # sur 16000 (A2) : il est supprimé.
    #
    # [Hotfix A14/A23] La forme {type:"enabled", budget_tokens:N} est
    # abandonnée : dépréciée sur Claude 4.6, rejetée en 400 à partir de 4.7,
    # et son ratio 16000/10000/4096 pouvait dépasser les max_tokens du client
    # (Anthropic exige max_tokens > budget_tokens). Cible : adaptive +
    # output_config.effort, qui ne porte aucun budget.
    _decision = _resolve_effort(oai_body, result.get("model", ""))
    if _decision.wants and _decision.level:
        _apply_anthropic_effort(
            result, _decision.level, result.get("model", ""), source=_decision.source
        )

    return result


def anthropic_to_openai_response(anthro: dict, model: str) -> dict:
    """Convert Anthropic Messages response → OpenAI Chat Completions format."""
    content_blocks = anthro.get("content", [])
    text_parts = []
    reasoning_text = ""
    tool_calls = []

    for block in content_blocks:
        if not isinstance(block, dict):
            continue
        t = block.get("type", "")
        if t == "text":
            text_parts.append(block.get("text", ""))
        elif t == "thinking":
            reasoning_text = block.get("thinking", "")
        elif t == "tool_use":
            tool_calls.append(
                {
                    "id": block.get("id", f"call_{uuid.uuid4().hex[:12]}"),
                    "type": "function",
                    "function": {
                        "name": block.get("name", ""),
                        "arguments": _json_dumps_str(block.get("input", {}), ensure_ascii=False),
                    },
                }
            )

    # Determine finish_reason
    sr = anthro.get("stop_reason", "")
    if sr == "max_tokens":
        finish = "length"
    elif sr == "tool_use":
        finish = "tool_calls"
    else:
        finish = "stop"

    # Usage mapping
    usage = anthro.get("usage", {})
    prompt_tokens = usage.get("input_tokens", 0)
    completion_tokens = usage.get("output_tokens", 0)
    oai_usage = {
        "prompt_tokens": prompt_tokens,
        "completion_tokens": completion_tokens,
        "total_tokens": prompt_tokens + completion_tokens,
    }
    cache_read = usage.get("cache_read_input_tokens", 0)
    if cache_read:
        oai_usage["prompt_tokens_details"] = {"cached_tokens": cache_read}

    message: dict[str, Any] = {"role": "assistant"}
    if text_parts:
        message["content"] = "\n".join(text_parts)
    else:
        message["content"] = ""
    if reasoning_text:
        message["reasoning_content"] = reasoning_text
    if tool_calls:
        message["tool_calls"] = tool_calls

    return {
        "id": f"chatcmpl-{uuid.uuid4().hex[:24]}",
        "object": "chat.completion",
        "created": int(time.time()),
        "model": model,
        "choices": [
            {
                "index": 0,
                "message": message,
                "finish_reason": finish,
            }
        ],
        "usage": oai_usage,
    }


def _responses_part_to_anthropic(part: dict) -> dict | None:
    """Convertit une part Responses input_image/input_file → bloc Anthropic
    natif (image/document + name). Factorise la logique de
    openai_responses_to_anthropic (boucle principale + function_call_output
    liste). None si inconvertible. Défensif : jamais d'exception."""
    if not isinstance(part, dict):
        return None
    btype = part.get("type", "")
    if btype == "input_image":
        _r_img_url = part.get("image_url", "")
        _r_img_b64 = part.get("image_base64", "")
        _r_img_fid = part.get("file_id", "")
        if not isinstance(_r_img_url, str):
            _r_img_url = ""
        if not isinstance(_r_img_b64, str):
            _r_img_b64 = ""
        if not isinstance(_r_img_fid, str):
            _r_img_fid = ""
        if _r_img_url.startswith("data:"):
            try:
                _r_h, _, _r_d = _r_img_url[5:].partition(",")
                _r_m = (_r_h.split(";")[0] or "").strip() or "image/png"
            except Exception:
                _r_m, _r_d = "image/png", ""
            if _r_d:
                return {
                    "type": "image",
                    "source": {"type": "base64", "media_type": _r_m, "data": _r_d},
                }
            return None
        elif _r_img_url:
            return {"type": "image", "source": {"type": "url", "url": _r_img_url}}
        elif _r_img_b64:
            return {
                "type": "image",
                "source": {
                    "type": "base64",
                    "media_type": part.get("mime_type", "image/png"),
                    "data": _r_img_b64,
                },
            }
        elif _r_img_fid:
            return {"type": "image", "source": {"type": "file", "file_id": _r_img_fid}}
        return None
    if btype == "input_file":
        _r_fdata = part.get("file_data", "")
        _r_ffid = part.get("file_id", "")
        _r_furl = part.get("file_url", "")
        _r_fname = part.get("filename", "") or ""
        if not isinstance(_r_fdata, str):
            _r_fdata = ""
        if not isinstance(_r_ffid, str):
            _r_ffid = ""
        if not isinstance(_r_furl, str):
            _r_furl = ""
        if _r_fdata.startswith("data:"):
            try:
                _r_fh, _, _r_fd = _r_fdata[5:].partition(",")
                _r_fm = (_r_fh.split(";")[0] or "").strip() or "application/pdf"
            except Exception:
                _r_fm, _r_fd = "application/pdf", ""
            if _r_fd:
                _r_doc: dict = {
                    "type": "document",
                    "source": {"type": "base64", "media_type": _r_fm, "data": _r_fd},
                }
                if _r_fname:
                    _r_doc["name"] = _r_fname
                return _r_doc
            return None
        elif _r_fdata:
            _r_doc2: dict = {
                "type": "document",
                "source": {
                    "type": "base64",
                    "media_type": part.get("mime_type", "application/pdf"),
                    "data": _r_fdata,
                },
            }
            if _r_fname:
                _r_doc2["name"] = _r_fname
            return _r_doc2
        elif _r_ffid:
            return {"type": "document", "source": {"type": "file", "file_id": _r_ffid}}
        elif _r_furl:
            _r_doc3: dict = {
                "type": "document",
                "source": {"type": "url", "url": _r_furl},
            }
            if _r_fname:
                _r_doc3["name"] = _r_fname
            return _r_doc3
        return None
    return None


def openai_responses_to_anthropic(body: dict) -> dict:
    """Convert OpenAI Responses API request → Anthropic Messages format."""
    system_text = ""
    pending_tool_results = []
    anthro_messages = []

    for item in body.get("input", []):
        if not isinstance(item, dict):
            continue
        role = item.get("role", item.get("type", "user"))

        if role in ("system", "developer"):
            for block in item.get("content") or []:
                if isinstance(block, dict) and block.get("type") == "input_text":
                    system_text += block.get("text", "")
            continue

        if item.get("type") == "function_call_output":
            # output str → contrat golden (string seule reste string) ;
            # output liste (extension tolérée par Zen/go, pas schéma officiel
            # strict) → chaque part mappée vers le bloc Anthropic natif.
            _fco_out = item.get("output", "")
            if isinstance(_fco_out, list):
                _fco_blocks: list = []
                for _fp in _fco_out:
                    if isinstance(_fp, str):
                        if _fp:
                            _fco_blocks.append({"type": "text", "text": _fp})
                        continue
                    if not isinstance(_fp, dict):
                        continue
                    _ft = _fp.get("type", "")
                    if _ft in ("input_text", "output_text", "text"):
                        _fco_blocks.append({"type": "text", "text": _fp.get("text", "")})
                    elif _ft == "input_image":
                        _fco_mapped = _responses_part_to_anthropic(_fp)
                        _fco_blocks.append(_fco_mapped or {"type": "text", "text": "[image:unmapped]"})
                    elif _ft == "input_file":
                        _fco_mapped = _responses_part_to_anthropic(_fp)
                        _fco_blocks.append(_fco_mapped or {"type": "text", "text": "[document:unmapped]"})
                    elif _ft == "input_audio":
                        _debug("  [convert] DROP fco input_audio → Anthropic sans audio (placeholder)")
                        _fco_blocks.append({"type": "text", "text": "[audio:unsupported-by-anthropic]"})
                    else:
                        _fco_blocks.append({"type": "text", "text": _fp.get("text", str(_fp))})
                _fco_out = _fco_blocks or ""
            pending_tool_results.append(
                {
                    "type": "tool_result",
                    "tool_use_id": item.get("call_id", item.get("id", "")),
                    "content": _fco_out,
                }
            )
            continue

        # Convert previous-turn function_call items to assistant tool_use blocks
        if item.get("type") == "function_call":
            try:
                inp = _json_loads(item.get("arguments", "{}"))
            except Exception:
                inp = {}
            anthro_messages.append(
                {
                    "role": "assistant",
                    "content": [
                        {
                            "type": "tool_use",
                            "id": item.get("call_id") or item.get("id") or f"toolu_{uuid.uuid4().hex[:12]}",
                            "name": item.get("name", ""),
                            "input": inp,
                        }
                    ],
                }
            )
            continue

        if role not in ("user", "assistant"):
            continue

        blocks = []
        if role == "user" and pending_tool_results:
            blocks.extend(pending_tool_results)
            pending_tool_results = []

        for block in item.get("content") or []:
            if not isinstance(block, dict):
                continue
            btype = block.get("type", "")
            if btype in ("input_text", "text"):
                blocks.append({"type": "text", "text": block.get("text", "")})
            elif btype in ("input_image", "input_file"):
                # Schéma Responses : input_image = image_url (data URI ou
                # https) ou file_id ; input_file = file_data (data URI) +
                # filename, file_id ou file_url. Formes historiques encore
                # acceptées en lecture (payloads pré-correctif, tests).
                _mapped = _responses_part_to_anthropic(block)
                if _mapped is not None:
                    blocks.append(_mapped)
            elif btype == "input_audio":
                # Anthropic n'a pas d'audio : placeholder honnête + debug.
                _debug("  [convert] DROP input_audio → Anthropic sans audio (placeholder)")
                blocks.append({"type": "text", "text": "[audio:unsupported-by-anthropic]"})
            elif btype == "reasoning":
                # [PLAN-raisonnement Phase D.3] pas de thinking forgé vers
                # l'upstream Anthropic (signature cryptographique exigée) —
                # le summary est omis de l'historique.
                _summary = block.get("summary") or []
                _has_text = any(isinstance(s, dict) and s.get("text") for s in _summary)
                if _has_text:
                    _debug(
                        "  [convert] DROP reasoning summary historique → upstream Anthropic (pas de signature forgée)"
                    )

        if not blocks:
            blocks.append({"type": "text", "text": ""})
        anthro_messages.append({"role": role, "content": blocks})

    if pending_tool_results:
        anthro_messages.append({"role": "user", "content": pending_tool_results})

    result = {
        "model": body.get("model", ""),
        "messages": anthro_messages,
        "stream": body.get("stream", False),
    }
    # [Lot L15 — B5] NB : `store`/`truncation` ne sont **pas** relayés ici. La
    # sortie de P6 part soit vers un upstream Anthropic (qui rejetterait ces
    # clés inconnues par un 400), soit vers la chaîne P2→P5 du handler
    # `/v1/responses`. C'est donc au handler — seul endroit qui connaît la
    # destination — de les replacer quand la cible est bien un endpoint
    # Responses (cf. `_relay_responses_storage_fields` appelé là-bas).
    # [Lot L14 — A17] Même lecture unifiée des formes de limite ; destination
    # Anthropic → toujours `max_tokens`.
    _set_output_token_limit(
        result, body, body.get("model", ""), target_protocol="anthropic"
    )
    if system_text:
        result["system"] = system_text
    if "temperature" in body:
        result["temperature"] = body["temperature"]
    if "top_p" in body:
        result["top_p"] = body["top_p"]

    # Convert tools (strip "type": "function" wrapper)
    if "tools" in body:
        model = body.get("model", "")
        result["tools"] = []
        for t in body["tools"]:
            if isinstance(t, dict) and t.get("type") == "function":
                tool = {"name": t["name"], "description": t.get("description", "")}
                tool["input_schema"] = _normalize_tool_schema(t.get("input_schema") or t.get("parameters") or {}, model)
                result["tools"].append(tool)
        tc = body.get("tool_choice", "auto")
        if isinstance(tc, dict):
            tc_type = tc.get("type", "auto")
            if tc_type == "function":
                result["tool_choice"] = {"type": "tool", "name": tc.get("name", "")}
            else:
                result["tool_choice"] = tc_type
        else:
            result["tool_choice"] = tc

    # Convert Anthropic thinking/effort -> model-specific reasoning parameter.
    # [Lot L2] SOURCE UNIQUE, comme P2 et P4. ``resolve_effort`` couvre
    # ``reasoning.effort`` (natif Responses, préservé par _sanitize + handler),
    # ``output_config.effort``, ``effort``, ``reasoning_effort`` et
    # ``thinking.*`` — et applique le plafond modèle de la config. La 3ᵉ table
    # budget→niveau locale est supprimée (A1) ainsi que l'écrasement de
    # ``xhigh``/``max`` (A2).
    #
    # BUG drop 2026-09-09 préservé : sur /v1/responses natif, l'effort était
    # perdu ici même — le test de non-régression correspondant reste vert.
    #
    # [Hotfix A15] Le champ de sortie est ``output_config.effort`` : jamais
    # ``reasoning_effort``, nom de champ OPENAI qu'un upstream Anthropic
    # ignorerait au mieux, rejetterait au pire.
    _decision = _resolve_effort(body, result.get("model", ""))
    if _decision.wants and _decision.level:
        _apply_anthropic_effort(
            result, _decision.level, result.get("model", ""), source=_decision.source
        )

    return result


def anthropic_to_openai_responses(anthro: dict, model: str, name_map: dict | None = None) -> dict:
    """Convert Anthropic Messages response → OpenAI Responses API format."""
    content_blocks = anthro.get("content", [])
    output_items: list[dict[str, Any]] = []
    text_content = []
    function_calls = []

    for block in content_blocks:
        if not isinstance(block, dict):
            continue
        btype = block.get("type", "")
        if btype == "text":
            text_content.append({"type": "output_text", "text": block.get("text", "")})
        elif btype == "thinking":
            # [Lot L12 — A19] `display: "omitted"` : le bloc existe et porte une
            # signature, mais son texte est vide. Émettre un item `reasoning`
            # avec un `summary_text` vide ferait afficher à chaque tour un bloc
            # « réflexion » vide côté client. On n'émet l'item que s'il y a
            # réellement quelque chose à résumer.
            _thinking_text = block.get("thinking") or ""
            if _thinking_text.strip():
                output_items.insert(
                    0,
                    {
                        "type": "reasoning",
                        "summary": [{"type": "summary_text", "text": _thinking_text}],
                    },
                )
            else:
                _debug(
                    "  [convert] thinking display=omitted (texte vide) → "
                    "pas d'item reasoning vide émis"
                )
        elif btype == "tool_use":
            function_calls.append(
                {
                    "type": "function_call",
                    "call_id": block.get("id", f"call_{uuid.uuid4().hex[:12]}"),
                    # [TROU 3 — A8] Symétrie avec ``_responses_to_anthropic_response``
                    # / ``_responses_to_chat_response`` : le nom RÉELLEMENT émis au
                    # client est le nom d'origine (la map est construite à l'aller
                    # par ``sanitize_tool_names``). Sans ce restore, un nom
                    # raccourci pour l'amont ressortirait raccourci et le client ne
                    # pourrait plus faire correspondre ses propres outils.
                    "name": restore_tool_name(block.get("name", ""), name_map),
                    "arguments": _json_dumps_str(block.get("input", {}), ensure_ascii=False),
                    "status": "completed",
                }
            )

    if text_content:
        output_items.append({"type": "message", "role": "assistant", "content": text_content})
    output_items.extend(function_calls)

    # Status mapping
    sr = anthro.get("stop_reason", "")
    if sr == "max_tokens":
        status = "incomplete"
    else:
        status = "completed"

    # Usage mapping
    usage = anthro.get("usage", {})
    in_t = usage.get("input_tokens", 0)
    out_t = usage.get("output_tokens", 0)
    oai_usage = {
        "input_tokens": in_t,
        "output_tokens": out_t,
        "total_tokens": in_t + out_t,
        "input_tokens_details": {
            "cached_tokens": usage.get("cache_read_input_tokens", 0),
        },
        "output_tokens_details": {
            # [Lot L12 — A18] Était codé en dur à 0 : la part facturée la plus
            # chère d'un modèle de raisonnement était toujours affichée nulle.
            # Borné par `out_t` : la ventilation est un sous-ensemble de la sortie.
            "reasoning_tokens": _extract_reasoning_tokens(usage, out_t),
        },
    }

    return {
        "id": f"resp_{uuid.uuid4().hex[:24]}",
        "object": "response",
        "status": status,
        "model": model,
        "output": output_items,
        "usage": oai_usage,
    }


def openai_chat_to_responses(chat_resp: dict, model: str, name_map: dict | None = None) -> dict:
    """Convert OpenAI Chat Completions response directly to OpenAI Responses API format.

    Bypasses the intermediate Anthropic format to avoid data loss and unnecessary conversion.

    [TROU 3 — A8] ``name_map`` (``{short: original}`` construit à l'aller par
    ``sanitize_tool_names``, transporté sous ``_TOOL_NAME_MAP_KEY``) restaure le
    nom d'origine des outils RÉELLEMENT émis : sans lui, un nom raccourci pour la
    borne 64 de l'amont ressortait raccourci au client, qui ne pouvait plus faire
    correspondre ses propres outils (asymétrie avec ``_responses_to_chat_response``
    / ``_responses_to_anthropic_response``, qui avaient déjà le paramètre).
    """
    choice = chat_resp.get("choices", [{}])[0]
    msg = choice.get("message", {})
    usage = chat_resp.get("usage", {})

    output_items: list[dict[str, Any]] = []

    # Reasoning content -> reasoning item
    reasoning = msg.get("reasoning_content") or msg.get("reasoning")
    if reasoning:
        output_items.insert(
            0,
            {
                "type": "reasoning",
                "summary": [{"type": "summary_text", "text": reasoning}],
            },
        )

    # Text content -> message item with output_text
    content = msg.get("content", "")
    if content:
        output_items.append(
            {
                "type": "message",
                "role": "assistant",
                "content": [{"type": "output_text", "text": content}],
            }
        )

    # Tool calls -> function_call items
    for tc in msg.get("tool_calls") or []:
        fn = tc.get("function", {})
        output_items.append(
            {
                "type": "function_call",
                "call_id": tc.get("id", f"call_{uuid.uuid4().hex[:12]}"),
                # [TROU 3 — A8] Restore-retour : le client reçoit le nom qu'il a
                # envoyé, jamais le raccourci interne de la borne 64.
                "name": restore_tool_name(fn.get("name", ""), name_map),
                "arguments": fn.get("arguments", "{}"),
                "status": "completed",
            }
        )

    # Status mapping
    finish = choice.get("finish_reason", "")
    if finish == "length":
        status = "incomplete"
    else:
        status = "completed"

    # Usage mapping
    prompt_tokens = usage.get("prompt_tokens", 0)
    completion_tokens = usage.get("completion_tokens", 0)
    cached = _extract_cache_tokens(usage)
    oai_usage = {
        "input_tokens": prompt_tokens,
        "output_tokens": completion_tokens,
        "total_tokens": prompt_tokens + completion_tokens,
        "input_tokens_details": {
            "cached_tokens": cached,
        },
        "output_tokens_details": {
            # [Lot L12 — A18] Idem : remonte la ventilation réelle du raisonnement,
            # bornée par le total de sortie.
            "reasoning_tokens": _extract_reasoning_tokens(usage, completion_tokens),
        },
    }

    return {
        "id": f"resp_{uuid.uuid4().hex[:24]}",
        "object": "response",
        "status": status,
        "model": model,
        "output": output_items,
        "usage": oai_usage,
    }


# ── Tool-name sanitization (fix 400 `name must be at most 64 characters`) ──
# L'upstream /responses refuse les noms d'outils >64 chars (ex. tools MCP
# `mcp__plugin_...`, 65 chars) : le free tombait en 400 systématique, le retry
# « station fraîche » rejouant le MÊME body 4-5×. Sanitize-aller + restore-retour,
# par requête (clé privée `_tool_name_map`, jamais globale) — uniforme free+paid,
# jamais de branche au nom de modèle. Server tools `web_*` exclus.
TOOL_NAME_MAX_LEN = 64
_TOOL_NAME_MAP_KEY = "_tool_name_map"
_TOOL_NAME_RE = re.compile(r"[^A-Za-z0-9_-]")
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
    Fast-path intégral (aucun nom à raccourcir) : liste inchangée, map vide.
    """
    if not isinstance(oai_tools, list) or not oai_tools or not isinstance(name_map, dict):
        return oai_tools
    idx_of: list[int] = []
    flat: list[dict] = []
    for i, t in enumerate(oai_tools):
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
        return oai_tools
    sanitized, fresh = sanitize_tool_names(flat)
    if not fresh:
        return oai_tools
    name_map.update(fresh)
    # ``sanitize_tool_names`` préserve ordre et longueur (cf. son ``out``), donc
    # l'alignement positionnel flat[i] ↔ sanitized[i] est garanti.
    out = list(oai_tools)
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
            if _clamped != _native_effort:
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
        _remap_responses_history_names(req["input"], name_map)
    if req.get("tool_choice") is not None:
        req["tool_choice"] = _remap_responses_tool_choice(req["tool_choice"], name_map)
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


def _chat_to_responses_request(chat: dict) -> dict:
    if "input" in chat and "messages" not in chat:
        # Verbatim natif Responses : sanitize quand même (tools + historique
        # + tool_choice, P0-1/P0-2), sinon fuite sur la jambe payée.
        return _sanitize_native_responses_request(dict(chat))
    inp = []
    _has_reasoning_items = False
    for m in chat.get("messages", []) or []:
        role = m.get("role", "user")
        content = m.get("content", "")
        # Preserve cache_control from the chat message for prefix caching
        cache_ctrl = m.get("cache_control")
        # Tool results must be function_call_output only — never a "role": "tool" input_text (invalid for Responses).
        # output accepte str ou liste de parts (input_text/input_image) :
        # content-list multimodale préservée au lieu d'être str()-ifiée.
        if role == "tool":
            cid = m.get("tool_call_id", "")
            if not cid:
                continue
            if isinstance(content, str):
                _tool_out: str | list = content
            elif isinstance(content, list):
                _tool_out_parts: list[dict] = []
                for _tb in content:
                    if not isinstance(_tb, dict):
                        continue
                    if _tb.get("type") == "text" and _tb.get("text"):
                        _tool_out_parts.append({"type": "input_text", "text": _tb["text"]})
                    elif _tb.get("type") == "image_url":
                        _turl = (_tb.get("image_url") or {}).get("url", "")
                        if not _turl:
                            continue
                        # Schéma Responses : input_image = image_url (URL https
                        # ou data URI base64 tel quel) ou file_id — jamais
                        # image_base64/mime_type (400 upstream sinon).
                        _tool_out_parts.append({"type": "input_image", "image_url": _turl})
                    elif _tb.get("type") == "file":
                        # mypy : `_tb.get("file")` répété ne se narrow pas ; on
                        # passe par un temporaire annoté `dict` pour que les
                        # accès ci-dessous soient typés (union-attr/index).
                        _raw_tf = _tb.get("file")
                        _tf: dict = _raw_tf if isinstance(_raw_tf, dict) else {}
                        if _tf.get("file_id"):
                            _tool_out_parts.append({"type": "input_file", "file_id": _tf["file_id"]})
                        elif isinstance(_tf.get("file_data"), str) and _tf["file_data"]:
                            _tfd = _tf["file_data"]
                            if _tfd.startswith("http://") or _tfd.startswith("https://"):
                                _tfp: dict = {"type": "input_file", "file_url": _tfd}
                                if _tf.get("filename"):
                                    _tfp["filename"] = _tf["filename"]
                                _tool_out_parts.append(_tfp)
                            else:
                                _tfp2: dict = {
                                    "type": "input_file",
                                    "file_data": _tfd,
                                    "filename": _tf.get("filename") or "document.pdf",
                                }
                                _tool_out_parts.append(_tfp2)
                    elif _tb.get("type") == "input_audio":
                        # fco n'a pas de fidélité audio Chat : placeholder.
                        _debug("  [convert] DROP tool input_audio → fco sans audio (placeholder)")
                        _tool_out_parts.append(
                            {"type": "input_text", "text": "[audio:unsupported-in-chat-tool-result]"}
                        )
                _tool_out = _tool_out_parts or ""
            else:
                _tool_out = str(content)
            inp.append(
                {
                    "type": "function_call_output",
                    "call_id": cid,
                    "output": _tool_out,
                }
            )
            continue
        # [Correctif parité multi-tours] raisonnement du tour précédent :
        # re-émis comme item reasoning plaine-texte, même représentation que
        # celle que le proxy produit dans SES réponses Responses et que
        # l'upstream nous renvoie (summary[].text ; pas d'id ni
        # encrypted_content). Inséré IMMÉDIATEMENT AVANT le message assistant
        # porteur du reasoning_content — jamais avant un function_call_output.
        _reasoning_txt = m.get("reasoning_content") or ""
        if role == "assistant" and isinstance(_reasoning_txt, str) and _reasoning_txt.strip():
            inp.append(
                {
                    "type": "reasoning",
                    "summary": [{"type": "summary_text", "text": _reasoning_txt}],
                }
            )
            _has_reasoning_items = True
        if isinstance(content, str):
            if content:
                ctype = "output_text" if role == "assistant" else "input_text"
                item = {"role": role, "content": [{"type": ctype, "text": content}]}
                if cache_ctrl:
                    item["cache_control"] = cache_ctrl
                inp.append(item)
        elif isinstance(content, list):
            parts: list[dict] = []
            for b in content:
                if not isinstance(b, dict):
                    continue
                if b.get("type") == "text" and b.get("text"):
                    parts.append(
                        {
                            "type": "output_text" if role == "assistant" else "input_text",
                            "text": b["text"],
                        }
                    )
                elif b.get("type") == "image_url":
                    _img_url = (b.get("image_url") or {}).get("url", "")
                    if not _img_url:
                        continue
                    # Schéma Responses : input_image porte image_url (URL https
                    # ou data URI base64 tel quel) — jamais image_base64 /
                    # mime_type (rejet 400 upstream : "requires either
                    # image_url or file_id").
                    parts.append({"type": "input_image", "image_url": _img_url})
                elif b.get("type") == "file":
                    _fobj = b.get("file") or {}
                    if not isinstance(_fobj, dict):
                        continue
                    if _fobj.get("file_id"):
                        parts.append({"type": "input_file", "file_id": _fobj["file_id"]})
                    elif _fobj.get("file_data"):
                        _fdata = _fobj["file_data"]
                        if not _fdata:
                            continue
                        # Schéma Responses : input_file = file_data (data URI
                        # tel quel) + filename — jamais mime_type / file_url.
                        _fname = _fobj.get("filename") or "document.pdf"
                        if _fdata.startswith("data:"):
                            _fdata_norm = _fdata
                        elif _fdata.startswith("http://") or _fdata.startswith("https://"):
                            # input_file.file_url EXISTE dans le schéma
                            # Responses officiel → mapping direct, pas de DROP.
                            _fpart: dict = {"type": "input_file", "file_url": _fdata}
                            if _fobj.get("filename"):
                                _fpart["filename"] = _fobj["filename"]
                            parts.append(_fpart)
                            continue
                        else:
                            _fhead, _, _fraw = _fdata.partition(",")
                            _fdata_norm = (
                                f"data:application/pdf;base64,{_fraw or _fdata}"
                                if ";" not in (_fhead or "")
                                else _fdata
                            )
                        parts.append(
                            {
                                "type": "input_file",
                                "file_data": _fdata_norm,
                                "filename": _fname,
                            }
                        )
                elif b.get("type") == "input_audio":
                    # Chat → Responses : input_audio {data: base64 brut,
                    # format: wav|mp3 uniquement} → passthrough validé.
                    _ca = b.get("input_audio") if isinstance(b.get("input_audio"), dict) else {}
                    _cdata = (_ca or {}).get("data", "") or ""
                    _cfmt = str((_ca or {}).get("format", "") or "").lower()
                    if _cdata and _cfmt in ("wav", "mp3"):
                        parts.append({"type": "input_audio", "input_audio": {"data": _cdata, "format": _cfmt}})
                    else:
                        _debug(
                            f"  [convert] DROP input_audio format={_cfmt!r} → Chat n'accepte que wav|mp3 (placeholder)"
                        )
                        parts.append({"type": "input_text", "text": f"[audio:{_cfmt or 'unknown'}]"})
                elif b.get("type") in ("video", "video_url"):
                    # Aucune des trois API n'accepte la vidéo : placeholder
                    # honnête (frames + transcript côté client, cf. plan).
                    _debug("  [convert] DROP video → aucune API (placeholder)")
                    parts.append({"type": "input_text", "text": "[video:unsupported]"})
                elif b.get("type") not in ("text", "image_url", "file"):
                    _debug(f"  [convert] DROP chat part type={b.get('type')!r} → placeholder")
                    parts.append({"type": "input_text", "text": f"[{b.get('type') or 'unknown'}]"})
            if parts:
                item = {"role": role, "content": parts}
                if cache_ctrl:
                    item["cache_control"] = cache_ctrl
                inp.append(item)
        for tc in m.get("tool_calls", []) or []:
            fn = tc.get("function", {}) if isinstance(tc.get("function"), dict) else {}
            _fn_name = fn.get("name", "")
            if not isinstance(_fn_name, str) or not _fn_name.strip():
                _debug(
                    f"  [convert] SKIP function_call with empty name call_id={tc.get('call_id') or tc.get('id', '?')}"
                )
                continue
            _cid = tc.get("call_id") or tc.get("id") or f"call_{uuid.uuid4().hex[:12]}"
            inp.append(
                {
                    "type": "function_call",
                    "call_id": _cid,
                    "name": _fn_name.strip(),
                    "arguments": fn.get("arguments", "{}"),
                }
            )
    # Orphan filter for Responses input
    inp = _drop_orphan_responses_input(inp)

    # Guard: Responses input must be non-empty; log original chat for audit if empty
    if not inp:
        _debug(
            f"  [free] _chat_to_responses_request empty input for model {chat.get('model', '')} — original messages={len(chat.get('messages', []))} — injecting fallback"
        )
        inp.append({"role": "user", "content": [{"type": "input_text", "text": "hello"}]})
    req = {"model": chat.get("model", ""), "input": inp, "stream": bool(chat.get("stream", False))}
    # [Correctif parité multi-tours] marqueur pour le retry-once : si l'upstream
    # rejette les items reasoning synthétiques (400/422), /responses retente
    # une fois sans eux (même payload sinon) au lieu de casser le tour entier.
    # Interne uniquement : stripé au dernier kilomètre (_serialize_json_body),
    # jamais sur le wire (inconnu de l'upstream → 400).
    if _has_reasoning_items:
        req[_HAS_SYNTHETIC_REASONING_KEY] = True
    # [Lot L14 — A17] Lecture unifiée des trois formes de limite. Avant, seule
    # `max_tokens` était relue : un modèle de raisonnement à qui P2 venait
    # d'écrire `max_completion_tokens` (B2) voyait sa limite **disparaître** à
    # cette étape — le client demandait 512 tokens, l'upstream n'en recevait
    # aucune borne, soit exactement le coût non borné que B2 cherche à éviter.
    #
    # Priorité inchangée pour les deux formes historiques : `max_output_tokens`
    # (forme Responses native) l'emporte sur `max_tokens` (héritée). La forme
    # moderne est ajoutée en dernier recours, donc aucun cas existant ne change.
    for _key in ("max_output_tokens", "max_tokens", "max_completion_tokens"):
        _val = chat.get(_key)
        if isinstance(_val, int) and _val > 0:
            req["max_output_tokens"] = _val
            break
    # [Lot L15 — B5] Relaie `store` et `truncation` : les défauts Responses sont
    # `store=true` (rétention ≥30 j côté upstream) et `truncation="disabled"`
    # (400 en dépassement de contexte, pas de troncature silencieuse). Sans
    # relais, le client ne contrôle ni sa confidentialité ni son mode d'échec.
    _relay_responses_storage_fields(req, chat)
    for k in ("temperature", "top_p"):
        if k in chat:
            req[k] = chat[k]
    # Forward reasoning parameters to Responses API format.
    # Le clamp config (thinking.effort_caps) s'applique ici aussi : ce forward
    # verbatim laissait passer un effort au-delà du plafond du modèle
    # (ex. max sur glm-5 → 400 upstream) sur la jambe /v1/responses.
    _chat_model = chat.get("model", "")
    if "reasoning_effort" in chat:
        effort = _effort_to_reasoning(chat["reasoning_effort"], _chat_model)
        # summary:auto is required to get visible reasoning summary; without it
        # upstream returns only encrypted_content and proxy emits placeholder.
        req["reasoning"] = {"summary": "auto", "effort": effort}
    elif "reasoning" in chat:
        _raw_reasoning = chat["reasoning"]
        if isinstance(_raw_reasoning, dict):
            _raw_effort = _raw_reasoning.get("effort")
            if isinstance(_raw_effort, str) and _raw_effort:
                _clamped = dict(_raw_reasoning)
                _clamped["effort"] = _effort_to_reasoning(_raw_effort, _chat_model)
                req["reasoning"] = _clamped
            else:
                req["reasoning"] = _raw_reasoning
        else:
            req["reasoning"] = _raw_reasoning
    if "tools" in chat:
        model = chat.get("model", "")
        prof = _resolve_schema_profile(model)
        _is_strict = prof["strip_additional_props"]

        def _needs_fallback(p: dict) -> bool:
            if not isinstance(p, dict):
                return False
            if any(k in p for k in ("anyOf", "oneOf", "const", "title", "$schema", "$id")):
                return True
            for v in p.values():
                if isinstance(v, dict) and _needs_fallback(v):
                    return True
                if isinstance(v, list) and any(isinstance(x, dict) and _needs_fallback(x) for x in v):
                    return True
            return False

        tools = []
        for t in chat["tools"]:
            if isinstance(t, dict) and "function" in t:
                fn = t["function"]
                raw_params = fn.get("parameters", {}) or {}
                params = _normalize_tool_schema(raw_params, model)
                tool_entry: dict = {
                    "type": "function",
                    "name": fn.get("name", ""),
                    "description": fn.get("description", ""),
                    "parameters": params,
                }
                # V5.1 sémantique : strict:false seulement si fallback nécessaire
                if _is_strict and _needs_fallback(raw_params):
                    tool_entry["strict"] = False
                tools.append(tool_entry)
        # [Lot L4 — A8] FUSION, jamais écrasement : ``anthropic_to_openai`` a pu
        # déjà poser une map (noms Chat raccourcis pour la limite 64). Elle est
        # lue depuis ``chat`` — l'ENTRÉE — et non depuis ``req``, qui est un dict
        # NEUF (cf. sa construction ``{"model":…, "input":…}``) : la chercher dans
        # ``req`` la perdait, et le client recevait un nom raccourci non
        # restaurable (défaut trouvé par test_anthropic_path_funnels_through_chat).
        _prev_map = chat.get(_TOOL_NAME_MAP_KEY)
        _name_map: dict = dict(_prev_map) if isinstance(_prev_map, dict) else {}
        if tools:
            # Sanitize-aller : l'upstream /responses refuse les noms >64 chars
            # (400 systématique → retry station fraîche inutile). La map est
            # transportée par requête (clé privée, jamais globale) pour le
            # restore-retour ; stripée avant sérialisation wire.
            tools, _fresh_map = sanitize_tool_names(tools)
            req["tools"] = tools
            _name_map.update(_fresh_map)
        # P0-1 [msg_18d502b1e0b40-e] : l'historique (input[].function_call.name)
        # suit le même rename — le tour N+1 rejoue sinon le nom original.
        _remap_responses_history_names(inp, _name_map)
        if _name_map:
            req[_TOOL_NAME_MAP_KEY] = _name_map
        tc = chat.get("tool_choice")
        if tc is not None:
            # P0-2 : toutes les formes nommées suivent le rename.
            req["tool_choice"] = _remap_responses_tool_choice(tc, _name_map)
    else:
        # P0-1 sans définitions : l'historique reste remappé en défensif
        # (map locale, restore-retour préservé). [Lot L4 — A8] Fusion avec la
        # map éventuellement déjà posée à l'aller Chat (lue sur ``chat``, cf. supra).
        _prev_alone = chat.get(_TOOL_NAME_MAP_KEY)
        _alone: dict = dict(_prev_alone) if isinstance(_prev_alone, dict) else {}
        _remap_responses_history_names(inp, _alone)
        if _alone:
            req[_TOOL_NAME_MAP_KEY] = _alone
    return req


def _anthropic_to_responses_request(anthro: dict) -> dict:
    if "input" in anthro and "messages" not in anthro:
        # Verbatim natif Responses : même sanitize que le chemin chat (P0-1/P0-2).
        return _sanitize_native_responses_request(dict(anthro))
    chat = anthropic_to_openai(anthro, anthro.get("model", ""))
    req = _chat_to_responses_request(chat)
    # [Lot L15 — B5] `anthropic_to_openai` ne transporte pas `store`/`truncation` :
    # on les relaie depuis le corps d'origine, sinon un client Anthropic ne peut
    # pas refuser la rétention ≥30 j ni choisir son mode de dépassement.
    _relay_responses_storage_fields(req, anthro)
    return req


def _responses_to_chat_response(resp: dict, model: str, name_map: dict | None = None) -> dict:
    out = resp.get("output", []) or []
    texts = []
    reasoning = ""
    tool_calls = []
    for item in out:
        if not isinstance(item, dict):
            continue
        if item.get("type") == "message" and item.get("role") == "assistant":
            for blk in item.get("content", []) or []:
                if isinstance(blk, dict) and blk.get("type") == "output_text":
                    texts.append(blk.get("text", ""))
        elif item.get("type") == "reasoning":
            for s in item.get("summary", []) or []:
                if isinstance(s, dict):
                    reasoning += s.get("text", "")
        elif item.get("type") == "function_call":
            tool_calls.append(
                {
                    "id": item.get("call_id", item.get("id", "")),
                    "type": "function",
                    "function": {
                        "name": restore_tool_name(item.get("name", ""), name_map),
                        "arguments": item.get("arguments", "{}"),
                    },
                }
            )
    # vrai seulement : pas de placeholder si pas de summary visible
    msg: dict[str, Any] = {"role": "assistant", "content": "\n".join(texts)}
    if reasoning:
        msg["reasoning_content"] = reasoning
    if tool_calls:
        msg["tool_calls"] = tool_calls
    usage = resp.get("usage", {}) if isinstance(resp.get("usage"), dict) else {}
    # Cache tokens come from input_tokens_details, NOT output_tokens_details
    _inp_details = usage.get("input_tokens_details") if isinstance(usage.get("input_tokens_details"), dict) else {}
    _cached = _inp_details.get("cached_tokens", 0) if isinstance(_inp_details, dict) else 0
    chat_usage = {
        "prompt_tokens": usage.get("input_tokens", 0),
        "completion_tokens": usage.get("output_tokens", 0),
        "total_tokens": usage.get("total_tokens", usage.get("input_tokens", 0) + usage.get("output_tokens", 0)),
    }
    if _cached:
        chat_usage["prompt_tokens_details"] = {"cached_tokens": _cached}
    status = resp.get("status", "completed")
    finish = "stop" if status == "completed" else "length"
    return {
        "id": resp.get("id", f"chatcmpl-{uuid.uuid4().hex[:8]}"),
        "object": "chat.completion",
        "created": int(time.time()),
        "model": model,
        "choices": [{"index": 0, "message": msg, "finish_reason": finish}],
        "usage": chat_usage,
    }


def _responses_to_anthropic_response(resp: dict, model: str, name_map: dict | None = None) -> dict:
    blocks = []
    for item in resp.get("output", []) or []:
        if not isinstance(item, dict):
            continue
        if item.get("type") == "reasoning":
            for s in item.get("summary", []) or []:
                if isinstance(s, dict) and s.get("text"):
                    blocks.append(
                        {
                            "type": "thinking",
                            "thinking": s.get("text", ""),
                            "signature": _local_signature(s.get("text", "")),
                        }
                    )
        elif item.get("type") == "message":
            for blk in item.get("content", []) or []:
                if isinstance(blk, dict) and blk.get("type") == "output_text" and blk.get("text"):
                    blocks.append({"type": "text", "text": blk.get("text", "")})
        elif item.get("type") == "function_call":
            try:
                inp = _json_loads(item.get("arguments", "{}"))
            except Exception:
                inp = {}
            blocks.append(
                {
                    "type": "tool_use",
                    "id": item.get("call_id", item.get("id", "")),
                    "name": restore_tool_name(item.get("name", ""), name_map),
                    "input": inp,
                }
            )
    if not blocks:
        blocks.append({"type": "text", "text": ""})
    usage = resp.get("usage", {}) if isinstance(resp.get("usage"), dict) else {}
    status = resp.get("status", "completed")
    stop = "end_turn" if status == "completed" else "max_tokens"
    has_tools = any(b.get("type") == "tool_use" for b in blocks)
    if has_tools:
        stop = "tool_use"
    # Cache tokens come from input_tokens_details, NOT output_tokens_details
    _inp_details = usage.get("input_tokens_details") if isinstance(usage.get("input_tokens_details"), dict) else {}
    _cache_read = _inp_details.get("cached_tokens", 0) if isinstance(_inp_details, dict) else 0
    return {
        "id": f"msg_{uuid.uuid4().hex[:24]}",
        "type": "message",
        "role": "assistant",
        "content": blocks,
        "model": model,
        "stop_reason": stop,
        "stop_sequence": None,
        "usage": {
            "input_tokens": usage.get("input_tokens", 0),
            "output_tokens": usage.get("output_tokens", 0),
            "cache_read_input_tokens": _cache_read,
        },
    }


# ── Responses API tool mapping cache (item_id/output_index → tool info) ──
# [P4 correctesse] état PAR STREAM : les trois structures ci-dessous étaient
# des globals partagés entre streams concurrents — un stream B voyait les
# item_ids/out_idx du stream A, et un stream avorté (pas de response.completed)
# fuyait son état vers le suivant. ``ResponsesSseState`` est instancié par
# stream et passé via ``state=`` ; les globals restent le fallback legacy
# (appelants sans state : fixtures golden, compatibilité).
_responses_tool_cache: dict = {}
_responses_tool_index_map: dict = {}  # output_index -> sequential tool index (0,1,2...)
# Track reasoning item_ids for which a delta was already emitted (dedupe delta vs done)
_reasoning_seen_ids: set = set()


class ResponsesSseState:
    """État de conversion SSE Responses-API pour UN stream."""

    __slots__ = ("tool_cache", "tool_index_map", "reasoning_seen", "tool_name_map")

    def __init__(self) -> None:
        self.tool_cache: dict = {}
        self.tool_index_map: dict = {}
        self.reasoning_seen: set = set()
        # Restore-retour stream : {short: original}, posée par requête par le
        # caller (même map que l'aller) ; None = pas de rename sur ce stream.
        self.tool_name_map: dict | None = None

    def reset(self) -> None:
        self.tool_cache.clear()
        self.tool_index_map.clear()
        self.reasoning_seen.clear()
        self.tool_name_map = None


def _responses_sse_to_chat_deltas(raw_line: str, parsed=None, state: "ResponsesSseState | None" = None):
    """Convert one Responses API SSE data line to chat/completions delta chunks.

    Yields 0..N dicts in chat/completions streaming format:
      {"choices": [{"delta": {"content": "...", "reasoning_content": "..."}, "finish_reason": null}]}
    so the existing stream parser in stream_gen() works unchanged.

    [C2 perf] ``parsed``: dict déjà parsé par l'appelant (son propre
    _json_loads(data_str)) — évite un second parse identique par chunk SSE.
    Ignoré quand None (compatibilité appelants legacy / fixtures golden).

    [P4] ``state``: ResponsesSseState du stream courant. None → fallback
    legacy sur les globals module-level (comportement historique).

    Returns None if the line is [DONE] or not parseable.
    """
    if raw_line == "[DONE]":
        return None
    if parsed is not None and isinstance(parsed, dict):
        chunk = parsed
    else:
        try:
            chunk = _json_loads(raw_line)
        except Exception:
            return None
        if not isinstance(chunk, dict):
            return None

    if state is not None:
        tool_cache = state.tool_cache
        tool_index_map = state.tool_index_map
        reasoning_seen = state.reasoning_seen
    else:
        tool_cache = _responses_tool_cache
        tool_index_map = _responses_tool_index_map
        reasoning_seen = _reasoning_seen_ids

    def _clear_state() -> None:
        if state is not None:
            state.reset()
        else:
            _responses_tool_cache.clear()
            _responses_tool_index_map.clear()
            _reasoning_seen_ids.clear()

    etype = chunk.get("type", "")
    if _cfg_settings.DEBUG:
        # [B4 perf] f-string par delta — construite seulement quand DEBUG on
        _debug(f"  [responses-sse] event type={etype!r} keys={list(chunk.keys())[:8]}")

    # response.output_text.delta — direct text delta (Responses API streaming format)
    if etype == "response.output_text.delta":
        text = chunk.get("delta", "")
        if not text:
            return None
        return {"choices": [{"delta": {"content": text}, "finish_reason": None}]}

    # response.content_part.delta — text or reasoning summary delta
    if etype == "response.content_part.delta":
        delta_obj = chunk.get("delta", {})
        dtype = delta_obj.get("type", "")
        text = delta_obj.get("text", "")
        if _cfg_settings.DEBUG:
            # [B4 perf] slice+repr par delta — construits seulement DEBUG on
            _debug(
                f"  [responses-sse] content_part.delta dtype={dtype!r} text={text[:80]!r} full_delta_keys={list(delta_obj.keys())[:10]}"
            )
        if not text:
            return None
        if dtype == "output_text":
            return {"choices": [{"delta": {"content": text}, "finish_reason": None}]}
        elif dtype == "reasoning_summary_text":
            return {"choices": [{"delta": {"reasoning_content": text}, "finish_reason": None}]}
        return None

    # response.reasoning_summary_text.delta — thinking delta (alternative event name)
    if etype == "response.reasoning_summary_text.delta":
        text = chunk.get("delta", "")
        if not text:
            return None
        # per-summary_index dedupe (item_id:summary_index) — two parts of same item must not clobber each other
        _iid = chunk.get("item_id", "")
        _sidx = chunk.get("summary_index", 0)
        _key = f"{_iid}:{_sidx}" if _iid else f"delta:{_sidx}:{text[:8]}"
        if _key:
            reasoning_seen.add(_key)
        return {"choices": [{"delta": {"reasoning_content": text}, "finish_reason": None}]}

    # response.reasoning_summary_text.done — fallback when delta missing (short reasoning)
    if etype == "response.reasoning_summary_text.done":
        _iid = chunk.get("item_id", "")
        _sidx = chunk.get("summary_index", 0)
        _key = f"{_iid}:{_sidx}" if _iid else ""
        if _key and _key in reasoning_seen:
            _debug(f"  [responses-sse] reasoning_summary_text.done deduped (delta already emitted) key={_key!r}")
            return None
        text = chunk.get("text", "") or chunk.get("delta", "")
        if not text:
            return None
        if _key:
            reasoning_seen.add(_key)
        return {"choices": [{"delta": {"reasoning_content": text}, "finish_reason": None}]}

    # response.reasoning_summary_part.done — part-level summary (contains summary_text)
    if etype == "response.reasoning_summary_part.done":
        part = chunk.get("part", {}) if isinstance(chunk.get("part"), dict) else {}
        text = part.get("text", "") if isinstance(part, dict) else ""
        if not text:
            text = chunk.get("text", "")
        if not text:
            return None
        _iid = chunk.get("item_id", "")
        _sidx = chunk.get("summary_index", 0)
        _key = f"{_iid}:{_sidx}" if _iid else f"part:{text[:8]}"
        if _key and _key in reasoning_seen:
            return None
        if _key:
            reasoning_seen.add(_key)
        return {"choices": [{"delta": {"reasoning_content": text}, "finish_reason": None}]}

    # response.output_item.added — start of function_call (tool_use)
    if etype == "response.output_item.added":
        item = chunk.get("item", {}) if isinstance(chunk.get("item"), dict) else {}
        if item.get("type") == "function_call":
            out_idx = chunk.get("output_index", 0)
            # Map output_index to sequential tool index
            if out_idx not in tool_index_map:
                tool_index_map[out_idx] = len(tool_index_map)
            tool_idx = tool_index_map[out_idx]
            call_id = item.get("call_id") or item.get("id") or f"call_{uuid.uuid4().hex[:12]}"
            # Restore-retour : le client reçoit le nom original, jamais le
            # raccourci sanitizé envoyé à l'upstream.
            _sse_name_map = state.tool_name_map if state is not None else None
            name = restore_tool_name(item.get("name", ""), _sse_name_map)
            # Cache for later delta events (by item_id and output_index)
            iid = item.get("id") or call_id
            tool_cache[iid] = {
                "index": tool_idx,
                "call_id": call_id,
                "name": name,
                "output_index": out_idx,
            }
            tool_cache[f"idx_{out_idx}"] = {
                "index": tool_idx,
                "call_id": call_id,
                "name": name,
            }
            _debug(
                f"  [responses-sse] function_call start idx={tool_idx} (out_idx={out_idx}) name={name!r} call_id={call_id}"
            )
            return {
                "choices": [
                    {
                        "delta": {
                            "tool_calls": [
                                {
                                    "index": tool_idx,
                                    "id": call_id,
                                    "type": "function",
                                    "function": {"name": name, "arguments": ""},
                                }
                            ]
                        },
                        "finish_reason": None,
                    }
                ]
            }

    # response.function_call_arguments.delta — streaming tool arguments
    if etype == "response.function_call_arguments.delta":
        delta = chunk.get("delta", "")
        if not delta:
            return None
        out_idx = chunk.get("output_index", 0)
        iid = chunk.get("item_id", "")
        # Lookup tool index
        info = tool_cache.get(iid) or tool_cache.get(f"idx_{out_idx}")
        if info is None:
            # Fallback: create entry if we missed the 'added' event
            if out_idx not in tool_index_map:
                tool_index_map[out_idx] = len(tool_index_map)
            tool_idx = tool_index_map[out_idx]
            _debug(
                f"  [responses-sse] function_call delta without prior added — fallback idx={tool_idx} out_idx={out_idx}"
            )
        else:
            tool_idx = info["index"]
        return {
            "choices": [
                {
                    "delta": {"tool_calls": [{"index": tool_idx, "function": {"arguments": delta}}]},
                    "finish_reason": None,
                }
            ]
        }

    # response.function_call_arguments.done — final tool arguments (optional, ensure completeness)
    if etype == "response.function_call_arguments.done":
        # This contains the full arguments but deltas already streamed; we can skip or emit final check
        # Don't emit extra delta if already streamed via .delta events; just ensure cache is updated
        iid = chunk.get("item_id", "")
        chunk.get("arguments", "")
        name = chunk.get("name", "")
        if iid and iid in tool_cache and name:
            tool_cache[iid]["name"] = name
        return None

    # response.output_item.done — reasoning item final with summary array (fallback + 100% guarantee)
    if etype == "response.output_item.done":
        item = chunk.get("item", {}) if isinstance(chunk.get("item"), dict) else {}
        if isinstance(item, dict) and item.get("type") == "reasoning":
            iid = item.get("id", "") or f"rs_{chunk.get('output_index', 0)}"
            summary = item.get("summary", [])
            # [Correctif B2] Pass-through 200% : même si un index a déjà été
            # streamé en delta (i vu → déjà visible côté client), le fallback
            # intégral doit TOUJOURS remonter la queue perdue (les i NON vus).
            # Pas de early-return ici — la dedup se fait au NIVEAU PART : chaque
            # part non vue est émise ci-dessous, les parts déjà vues sont droppées.
            reasoning = ""
            if isinstance(summary, list):
                for s in summary:
                    if isinstance(s, dict) and s.get("text"):
                        reasoning += s.get("text", "")
                    elif isinstance(s, dict) and s.get("type") == "summary_text":
                        reasoning += s.get("text", "")
            if not reasoning and isinstance(item.get("summary"), dict):
                reasoning = item["summary"].get("text", "")
            # 100% fallback: if no summary text but encrypted_content exists, synthesize placeholder so client always sees thinking
            if not reasoning:
                # vrai seulement : pas de placeholder synthétique — si pas de summary visible, on ne remonte rien (le vrai)
                _debug(
                    f"  [responses-sse] output_item.done no visible summary, skip (vrai seulement) iid={iid!r} encrypted={bool(item.get('encrypted_content'))}"
                )
                return None
            if reasoning:
                # N'émètre QUE les parts non vues (per-index) — évite doublon
                # (cas 1-part où delta déjà vu → on émet rien ; N-parts → on
                # émet seulement les indices jamais vus).
                _unseen = ""
                _seen_any = False
                if iid and isinstance(summary, list) and summary:
                    for i, s in enumerate(summary):
                        if f"{iid}:{i}" not in reasoning_seen:
                            if isinstance(s, dict):
                                _unseen += s.get("text", "") or ""
                    _seen_any = any(f"{iid}:{i}" in reasoning_seen for i in range(len(summary)))
                    for i in range(len(summary)):
                        reasoning_seen.add(f"{iid}:{i}")
                elif iid:
                    if iid in reasoning_seen:
                        return None
                    reasoning_seen.add(iid)
                    _unseen = reasoning
                else:
                    _unseen = reasoning
                # si tout le summary avait déjà été delta-streamé → rien à émettre
                if iid and isinstance(summary, list) and summary and not _unseen:
                    _debug(f"  [responses-sse] output_item.done fully deduped iid={iid!r}")
                    return None
                # cas particulier single-part : le client a déjà le texte complet
                # via delta — on ne renvoie PAS d'intégral redondant (même per-index
                # présent → done fully dedupé ci-dessus l'a déjà absorbé).
                _emit = _unseen if (iid and isinstance(summary, list) and summary and _seen_any) else reasoning
                # mais si le seen_any était seulement partiel (N>1), _emit==_unseen
                # (queue seule) ; si single-part seen → déjà return None ci-dessus.
                _debug(
                    f"  [responses-sse] output_item.done reasoning fallback len={len(_emit)} iid={iid!r} unseen={len(_unseen)} seen_any={_seen_any}"
                )
                return {"choices": [{"delta": {"reasoning_content": _emit}, "finish_reason": None}]}
        return None

    # response.completed — final event with usage
    if etype == "response.completed":
        # Clear tool cache + reasoning dedupe for next request
        _clear_state()
        resp = chunk.get("response", {})
        usage = resp.get("usage", {})
        # Cache tokens come from input_tokens_details, NOT output_tokens_details
        _inp_details = usage.get("input_tokens_details") if isinstance(usage.get("input_tokens_details"), dict) else {}
        _cached = _inp_details.get("cached_tokens", 0)
        chat_usage = {
            "prompt_tokens": usage.get("input_tokens", 0),
            "completion_tokens": usage.get("output_tokens", 0),
            "total_tokens": usage.get("total_tokens", usage.get("input_tokens", 0) + usage.get("output_tokens", 0)),
        }
        if _cached:
            chat_usage["prompt_tokens_details"] = {"cached_tokens": _cached}
        return {"choices": [], "usage": chat_usage}

    # response.incomplete — model didn't generate output, treat as stream end
    if etype == "response.incomplete":
        _clear_state()
        _debug("  [responses-sse] response.incomplete received — model produced no output")
        resp = chunk.get("response", {})
        usage = resp.get("usage", {}) if isinstance(resp.get("usage"), dict) else {}
        chat_usage = {
            "prompt_tokens": usage.get("input_tokens", 0),
            "completion_tokens": usage.get("output_tokens", 0),
            "total_tokens": usage.get("total_tokens", usage.get("input_tokens", 0) + usage.get("output_tokens", 0)),
        }
        return {"choices": [], "usage": chat_usage, "_incomplete": True}

    # All other event types (response.created, response.in_progress,
    # response.output_item.done, response.content_part.added/done, etc.) — skip
    return None


# ── [Lot L5 — A11/A21] Émission incrémentale des événements Responses ──
#
# A21 (confirmé par la spec) : l'ensemble minimal consommé par un client est
# ``response.created`` → ``response.output_text.delta``* → ``response.completed``.
# Nous n'émettions QUE ``response.completed`` — sans même le ``response.created``
# initial. Un client qui attend ``response.created`` avant d'afficher quoi que ce
# soit reste donc bloqué jusqu'à la fin de la génération : la réponse n'apparaît
# pas progressivement, elle apparaît d'un coup à la fin.
#
# Ce module fournit les briques d'émission ; l'appelant (opencode.py) décide du
# moment. Chaque événement porte son ``sequence_number`` (B7), strictement
# croissant dans le stream — un client qui détecte un trou ou un doublon
# réinitialise son état.

# Champs exacts par type d'événement (B7) — rappel de la spec, appliqué par les
# méthodes ci-dessous :
#   - `output_text.delta` : {content_index, delta, item_id, logprobs[], output_index, sequence_number}
#   - `function_call_arguments.delta` : NI `content_index` NI `name`
#   - `reasoning_summary_text.delta` : `summary_index` (pas `content_index`)


class ResponsesStreamEmitter:
    """Construit la séquence d'événements SSE Responses d'une réponse.

    État PAR STREAM (comme ``ResponsesSseState``) : ``sequence_number`` et
    identifiants sont propres à un stream. Deux streams concurrents ne doivent
    jamais partager ces compteurs — c'était le défaut déjà corrigé pour le cache
    d'outils.

    ``output_index`` est **explicite** sur chaque méthode qui le concerne : le
    déduire d'un compteur interne produisait un index faux sur
    ``output_item.done`` (le compteur avait déjà été incrémenté par ``.added``),
    et un client qui corrèle les deux événements par index aurait perdu l'item.
    """

    __slots__ = ("response_id", "model", "sequence", "created_sent")

    def __init__(self, model: str, response_id: str | None = None) -> None:
        self.model = model
        self.response_id = response_id or f"resp_{uuid.uuid4().hex[:24]}"
        self.sequence = 0
        self.created_sent = False

    def _next_seq(self) -> int:
        seq = self.sequence
        self.sequence += 1
        return seq

    def _wrap(self, event_type: str, payload: dict) -> dict:
        return {"type": event_type, **payload, "sequence_number": self._next_seq()}

    # ── Cycle de vie ──

    def created(self, created_at: int | None = None, status: str = "in_progress") -> dict:
        """`response.created` — premier événement, OBLIGATOIRE (A21).

        Un client qui attend cet événement avant d'afficher resterait sinon
        bloqué jusqu'à la fin de la génération : le texte n'apparaît pas
        progressivement, il apparaît d'un bloc à la fin.
        """
        self.created_sent = True
        now = created_at if created_at is not None else int(time.time())
        return self._wrap(
            "response.created",
            {
                "response": {
                    "id": self.response_id,
                    "object": "response",
                    "created_at": now,
                    "status": status,
                    "model": self.model,
                    "output": [],
                }
            },
        )

    def in_progress(self) -> dict:
        """`response.in_progress` — transition, juste après ``created``."""
        return self._wrap(
            "response.in_progress",
            {
                "response": {
                    "id": self.response_id,
                    "object": "response",
                    "status": "in_progress",
                    "model": self.model,
                    "output": [],
                }
            },
        )

    def output_item_added(self, item: dict, output_index: int) -> dict:
        """`response.output_item.added` — ouvre un item de sortie, **vide**.

        Le contenu ne doit **pas** figurer ici : il arrive par les deltas, puis
        se referme complet sur ``.done``. Un client conforme qui initialise son
        accumulateur avec ``item`` et y ajoute ensuite les deltas obtiendrait le
        contenu **en double** si ``.added`` portait déjà tout le texte — cas
        d'autant plus pernicieux qu'un test qui ne relit que les deltas (la vue
        la plus naturelle) ne le voit jamais.

        C'est aussi la convention déjà supposée par notre propre parseur SSE :
        ``_responses_chunk_to_chat`` émet ``"arguments": ""`` sur l'événement
        ``.added`` d'un ``function_call`` puis accumule les deltas.
        """
        return self._wrap(
            "response.output_item.added",
            {"output_index": output_index, "item": _empty_item_for_added(item, output_index)},
        )

    def output_item_done(self, item: dict, output_index: int) -> dict:
        """`response.output_item.done` — ferme un item.

        C'est à ce moment que l'``encrypted_content`` d'un reasoning item est
        complet (B7) : le lire depuis ``.added`` donne une valeur partielle.
        """
        return self._wrap(
            "response.output_item.done",
            {"output_index": output_index, "item": {**item, "index": output_index}},
        )

    def content_part_added(self, item_id: str, output_index: int, content_index: int = 0) -> dict:
        """`response.content_part.added` — ouvre une part de contenu texte."""
        return self._wrap(
            "response.content_part.added",
            {
                "item_id": item_id,
                "output_index": output_index,
                "content_index": content_index,
                "part": {"type": "output_text", "text": "", "annotations": []},
            },
        )

    def content_part_done(
        self, item_id: str, text: str, output_index: int, content_index: int = 0
    ) -> dict:
        """`response.content_part.done` — ferme une part de contenu texte."""
        return self._wrap(
            "response.content_part.done",
            {
                "item_id": item_id,
                "output_index": output_index,
                "content_index": content_index,
                "part": {"type": "output_text", "text": text, "annotations": []},
            },
        )

    # ── Texte ──

    def text_delta(
        self, delta: str, item_id: str, output_index: int = 0, content_index: int = 0
    ) -> dict:
        """`response.output_text.delta` — un fragment de texte.

        ``logprobs`` est présent (liste vide) : le champ est documenté dans le
        payload, et un client strict qui le lit ne doit pas recevoir ``None``.
        """
        return self._wrap(
            "response.output_text.delta",
            {
                "item_id": item_id,
                "output_index": output_index,
                "content_index": content_index,
                "delta": delta,
                "logprobs": [],
            },
        )

    def text_done(
        self, text: str, item_id: str, output_index: int = 0, content_index: int = 0
    ) -> dict:
        """`response.output_text.done` — texte complet de la part."""
        return self._wrap(
            "response.output_text.done",
            {
                "item_id": item_id,
                "output_index": output_index,
                "content_index": content_index,
                "text": text,
            },
        )

    # ── Raisonnement ──

    def reasoning_summary_delta(
        self, delta: str, item_id: str, output_index: int = 0, summary_index: int = 0
    ) -> dict:
        """`response.reasoning_summary_text.delta`.

        B7 : ce type utilise ``summary_index`` — PAS ``content_index``. Émettre
        ``content_index`` ici ferait ignorer le fragment par un client conforme.
        """
        return self._wrap(
            "response.reasoning_summary_text.delta",
            {
                "item_id": item_id,
                "output_index": output_index,
                "summary_index": summary_index,
                "delta": delta,
            },
        )

    # ── Appels d'outils ──

    def function_call_arguments_delta(self, delta: str, item_id: str, output_index: int = 0) -> dict:
        """`response.function_call_arguments.delta`.

        B7 : NI ``content_index`` NI ``name`` dans ce payload — les ajouter
        contredit la spec.
        """
        return self._wrap(
            "response.function_call_arguments.delta",
            {"item_id": item_id, "output_index": output_index, "delta": delta},
        )

    def function_call_arguments_done(
        self, arguments: str, item_id: str, output_index: int = 0
    ) -> dict:
        """`response.function_call_arguments.done` — arguments complets."""
        return self._wrap(
            "response.function_call_arguments.done",
            {
                "item_id": item_id,
                "output_index": output_index,
                "arguments": arguments,
            },
        )

    # ── Terminaison ──

    def completed(self, response: dict) -> dict:
        """`response.completed` — terminal, porte l'``usage`` final."""
        return self._wrap("response.completed", {"response": response})

    def failed(self, response: dict | None = None, message: str = "") -> dict:
        """`response.failed` — terminal cohérent pour un stream avorté.

        L5 exige qu'un stream en erreur garde un **terminal cohérent** : un
        client qui ne reçoit jamais d'événement terminal laisse sa connexion (et
        son UI) bloquée jusqu'au timeout.
        """
        payload = response or {
            "id": self.response_id,
            "object": "response",
            "status": "failed",
            "model": self.model,
            "output": [],
            "error": {"code": "stream_error", "message": message or "upstream stream failed"},
        }
        return self._wrap("response.failed", {"response": payload})


# Taille des fragments émis quand on rejoue une réponse complète sous forme
# d'événements. Assez petit pour que le client voie une progression réelle
# (l'objectif d'A11 : ne pas tout livrer d'un bloc), assez grand pour ne pas
# multiplier les trames SSE sur une longue réponse.
_RESPONSES_REPLAY_CHUNK = 64


def _empty_item_for_added(item: dict, output_index: int) -> dict:
    """Version **vide** d'un item, pour ``response.output_item.added``.

    Conserve l'identité de l'item (``type``, ``id``/``call_id``, ``name``,
    ``role``, ``status``) — ce qu'un client utilise pour décider *comment*
    accumuler — mais vide le **contenu** (``content``, ``arguments``,
    ``summary``, ``encrypted_content``), livré par les deltas puis complet sur
    ``.done``.

    Vider ``content`` plutôt que de l'omettre : un client strict qui itère
    ``item["content"]`` sans garde ne doit pas recevoir de ``KeyError``, et la
    liste vide est la valeur que la spec documente pour un item qui démarre.
    """
    if not isinstance(item, dict):
        return {"index": output_index}

    empty = {k: v for k, v in item.items() if k not in ("content", "arguments", "summary", "encrypted_content")}
    empty["index"] = output_index

    itype = item.get("type")
    if itype == "message":
        # Une part vide par part d'origine : le nombre de parts (et donc les
        # `content_index` à venir) reste annoncé dès `.added`.
        parts = item.get("content") or []
        empty["content"] = [
            {"type": "output_text", "text": "", "annotations": []}
            if isinstance(p, dict) and p.get("type") in ("output_text", "text")
            else (dict(p) if isinstance(p, dict) else p)
            for p in parts
        ]
    elif itype == "function_call":
        # Convention OpenAI (et de notre propre parseur) : arguments vides.
        empty["arguments"] = ""
    elif itype == "reasoning":
        summary = item.get("summary") or []
        empty["summary"] = [
            {"type": "summary_text", "text": ""} if isinstance(s, dict) else s for s in summary
        ]

    return empty


def responses_stream_events(
    response: dict,
    model: str,
    emitter: ResponsesStreamEmitter | None = None,
) -> list[dict]:
    """[Lot L5 — A11/A21] Séquence d'événements Responses conforme pour `response`.

    Transforme une réponse Responses **complète** (déjà convertie) en la
    séquence d'événements qu'un client conforme attend :

        response.created → response.in_progress
        → (par item) output_item.added → … deltas … → output_item.done
        → response.completed (avec `usage`)

    L'ensemble minimal documenté (A21) est
    ``response.created`` → ``response.output_text.delta``* → ``response.completed``.
    Nous n'émettions que ``response.completed`` : un client qui attend
    ``response.created`` avant d'afficher restait bloqué, et rien ne s'affichait
    progressivement.

    Les fragments de texte sont découpés en tranches de
    ``_RESPONSES_REPLAY_CHUNK`` caractères : l'objectif est la **progression
    perçue**, pas de simuler un vrai tokenizer. Le contenu final est identique à
    celui de ``response["output"]``, seul le découpage diffère.

    ``usage`` est reporté tel quel sur ``response.completed`` — c'est là que le
    client lit la consommation finale.
    """
    emitter = emitter or ResponsesStreamEmitter(model, response.get("id"))
    events: list[dict] = [emitter.created(), emitter.in_progress()]

    for output_index, item in enumerate(response.get("output") or []):
        if not isinstance(item, dict):
            continue
        itype = item.get("type")

        if itype == "reasoning":
            # B7 : le raisonnement ne coule que si `reasoning.summary` est opt-in
            # côté client ; on émet les fragments de résumé disponibles.
            events.append(emitter.output_item_added(item, output_index))
            summary_dir = item.get("summary") or []
            text = "".join(
                s.get("text", "") for s in summary_dir if isinstance(s, dict) and s.get("text")
            )
            item_id = item.get("id") or f"rs_{uuid.uuid4().hex[:16]}"
            for start in range(0, len(text), _RESPONSES_REPLAY_CHUNK):
                chunk = text[start : start + _RESPONSES_REPLAY_CHUNK]
                if chunk:
                    events.append(emitter.reasoning_summary_delta(chunk, item_id, output_index))
            # `encrypted_content` n'est complet qu'ici (B7), donc `.done` après
            # tous les fragments.
            events.append(emitter.output_item_done(item, output_index))

        elif itype == "function_call":
            events.append(emitter.output_item_added(item, output_index))
            item_id = item.get("id") or item.get("call_id") or f"fc_{uuid.uuid4().hex[:16]}"
            args = item.get("arguments") or ""
            for start in range(0, len(args), _RESPONSES_REPLAY_CHUNK):
                chunk = args[start : start + _RESPONSES_REPLAY_CHUNK]
                if chunk:
                    events.append(emitter.function_call_arguments_delta(chunk, item_id, output_index))
            events.append(emitter.function_call_arguments_done(args, item_id, output_index))
            events.append(emitter.output_item_done(item, output_index))

        elif itype == "message":
            events.append(emitter.output_item_added(item, output_index))
            item_id = item.get("id") or f"msg_{uuid.uuid4().hex[:16]}"
            content_index = 0
            for part in item.get("content") or []:
                if not isinstance(part, dict):
                    continue
                if part.get("type") not in ("output_text", "text"):
                    content_index += 1
                    continue
                text = part.get("text", "") or ""
                events.append(emitter.content_part_added(item_id, output_index, content_index))
                for start in range(0, len(text), _RESPONSES_REPLAY_CHUNK):
                    chunk = text[start : start + _RESPONSES_REPLAY_CHUNK]
                    if chunk:
                        events.append(
                            emitter.text_delta(chunk, item_id, output_index, content_index)
                        )
                events.append(emitter.text_done(text, item_id, output_index, content_index))
                events.append(emitter.content_part_done(item_id, text, output_index, content_index))
                content_index += 1
            events.append(emitter.output_item_done(item, output_index))

        else:
            # Type inconnu : on le transporte sans le perdre (un item qu'on ne
            # sait pas découper reste livré en un seul bloc).
            events.append(emitter.output_item_added(item, output_index))
            events.append(emitter.output_item_done(item, output_index))

    events.append(emitter.completed(response))
    return events


def responses_stream_sse(events: list[dict]) -> bytes:
    """Sérialise des événements Responses en corps SSE (``data: …`` + ``[DONE]``).

    ``[DONE]`` est conservé en fin de flux : nos clients existants s'en servent
    comme sentinelle de fin, et la spec Responses ne l'interdit pas.
    """
    parts = [f"data: {_json_dumps_str(ev, ensure_ascii=False)}\n\n" for ev in events]
    parts.append("data: [DONE]\n\n")
    return "".join(parts).encode()

