"""
Protocol mapping — socle: IO (orjson/json), ids d'outils, usage/cache tokens, effort, schémas, cache-control Anthropic.

Découpe de app.protocol.mapping (T213). Le corps des fonctions est identique à
l'octet près au monolithe d'origine ; la façade app.protocol.mapping ré-exporte
ces symboles.
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


def _stable_tool_id(prefix: str, *parts: Any) -> str:
    """Id d'outil de repli : DÉTERMINISTE, jamais vide.

    Deux exigences client (invariant structurel Anthropic) :
    1. l'id ne doit jamais être vide/None — sinon le client ne peut pas
       apparier son tool_result au tool_use et la conversation reste
       définitivement déséquilibrée ;
    2. l'id doit être STABLE pour un même call : si une conversation est
       reconvertie (retry, relecture de cache, second passage), un id
       aléatoire change à chaque fois et casse l'appariement avec le
       tool_result déjà émis par le client.

    D'où un hachage du contexte du call plutôt qu'un uuid4 : même entrée →
    même id. ``prefix`` garde la forme attendue par l'amont (``call_``/``toolu_``).
    """
    seed = "\x1f".join(str(p) for p in parts)
    digest = hashlib.sha1(seed.encode("utf-8", errors="replace")).hexdigest()
    return f"{prefix}{digest[:24]}"


def _tool_id_or_stable(raw: Any, prefix: str, *parts: Any) -> str:
    """Id d'outil existant s'il est exploitable, sinon id stable dérivé.

    ``dict.get(k, default)`` ne suffit PAS : il rend la valeur présente
    (None, "", 0) au lieu du défaut. Test de véracité obligatoire.
    Parité officielle v2 (`scrubToolCallID`) : tout id relayé vers
    Anthropic (`toolu_`) est scrubbé `[^a-zA-Z0-9_-]→_` ; les ids stables
    générés (`prefix+sha1`) sont déjà valides.
    """
    if isinstance(raw, str) and raw.strip():
        if prefix.startswith("toolu_"):
            try:
                return scrub_claude_tool_id(raw)
            except Exception:
                return raw
        return raw
    return _stable_tool_id(prefix, *parts)


def _has_server_side_history(body) -> bool:
    """True si l'historique vit côté serveur (chaînage Responses).

    Avec `previous_response_id`/`conversation`, un output sans call VISIBLE
    dans le payload est légitime (le call est dans la réponse chaînée) : le
    filtre orphelin local ne doit pas amputer. L'officiel ne strippe jamais.
    """
    if not isinstance(body, dict):
        return False
    return bool(body.get("previous_response_id") or body.get("conversation"))


def _drop_orphan_tool_messages(messages: list[dict], body: dict | None = None) -> list[dict]:
    """Filter role:tool messages whose tool_call_id has no preceding tool_calls id.

    Garde : si `body` porte un chaînage serveur (`previous_response_id` /
    `conversation`), aucun filtrage — l'appariement vit côté upstream.
    """
    if _has_server_side_history(body):
        _debug("  [orphan] chaînage serveur → filtre orphelin désactivé (pas d'amputation)")
        return messages
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


def _drop_orphan_responses_input(inp: list[dict], body: dict | None = None) -> list[dict]:
    """Filter function_call_output items whose call_id has no preceding function_call.

    Garde : si `body` porte un chaînage serveur (`previous_response_id` /
    `conversation`), aucun filtrage — l'appariement vit côté upstream.
    """
    if _has_server_side_history(body):
        _debug("  [orphan] chaînage serveur → filtre orphelin désactivé (pas d'amputation)")
        return [it for it in inp if isinstance(it, dict)] if isinstance(inp, list) else inp
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


def _usage_int(value) -> int:
    """Entier positif d'un champ `usage` — 0 pour tout le reste (bool inclus)."""
    if isinstance(value, bool) or not isinstance(value, int):
        return 0
    return max(0, value)


def anthropic_input_tokens(total_input, cache_read=0, cache_creation=0) -> int:
    """`input_tokens` Anthropic = input total OpenAI − parts servies par le cache.

    OpenAI compte **tout** l'input dans `prompt_tokens` / `input_tokens`, cache
    inclus ; Anthropic expose l'input non caché dans `input_tokens` et le cache
    dans `cache_read_input_tokens` / `cache_creation_input_tokens`. Les clients
    Anthropic additionnent les trois : sans cette soustraction, ils voient
    ~2× le contexte réel et auto-compactent bien trop tôt (thrashing).
    """
    return max(
        0,
        _usage_int(total_input) - _usage_int(cache_read) - _usage_int(cache_creation),
    )


def anthropic_usage_from_openai(usage: dict) -> dict:
    """Convertit un `usage` OpenAI (Chat ou Responses) en `usage` Anthropic.

    Formes acceptées :
    - Chat : `prompt_tokens` + `completion_tokens` + `prompt_tokens_details` ;
    - Responses : `input_tokens` + `output_tokens` + `input_tokens_details`.

    Les détails de cache sont lus dans les deux conteneurs. Les champs
    `cache_creation_*` ne sont soustraits que lorsqu'ils sont imbriqués dans les
    détails du prompt (donc inclus dans le total) : un `cache_creation` top-level
    peut venir d'un upstream Anthropic-compatible où il est déjà hors total.
    """
    if not isinstance(usage, dict):
        return {
            "input_tokens": 0,
            "output_tokens": 0,
            "cache_creation_input_tokens": 0,
            "cache_read_input_tokens": 0,
        }

    total_raw = usage.get("prompt_tokens")
    if total_raw is None:
        total_raw = usage.get("input_tokens")
    out_raw = usage.get("completion_tokens")
    if out_raw is None:
        out_raw = usage.get("output_tokens")
    total_in = _usage_int(total_raw)
    out = _usage_int(out_raw)

    details = usage.get("prompt_tokens_details")
    if not isinstance(details, dict):
        details = usage.get("input_tokens_details")
    details = details if isinstance(details, dict) else {}

    cache_read = _usage_int(details.get("cached_tokens"))
    if not cache_read:
        cache_read = _usage_int(usage.get("cached_tokens"))
    if not cache_read:
        cache_read = _usage_int(usage.get("cache_read_input_tokens"))

    # Création de cache *imbriquée* dans les détails du prompt : elle fait
    # partie de `prompt_tokens`, donc on la soustrait de l'input Anthropic.
    nested_creation = _usage_int(details.get("cache_creation_tokens"))
    if not nested_creation:
        nested_creation = _usage_int(details.get("cache_creation_input_tokens"))
    if not nested_creation:
        nested_creation = _usage_int(details.get("cache_write_tokens"))
    # Champ déclaré au client : conserve la lecture top-level Anthropic-compat
    # (`cache_creation_input_tokens`) pour ne pas casser les upstreams qui
    # l'exposent, même si ce n'est pas toujours un sous-ensemble du prompt.
    reported_creation = _extract_cache_creation_tokens(usage)

    return {
        "input_tokens": anthropic_input_tokens(total_in, cache_read, nested_creation),
        "output_tokens": out,
        "cache_creation_input_tokens": reported_creation,
        "cache_read_input_tokens": cache_read,
    }


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

    Mesuré sur le client déployé 1.19.0 (2026-10-15) : Chat émet TOUJOURS
    ``max_tokens`` (défaut 32000), y compris glm/mimo/muse-spark — la
    détection `detectMaxTokensField` n'existe que sur la lignée v2-dev,
    pas dans le client qui parle aujourd'hui aux providers.
    Le basculement reste donc limité aux préfixes o-series/gpt-5
    (config-driven), qui rejettent ``max_tokens`` en 400.
    """
    if not isinstance(model, str) or not model:
        return False
    cfg = yaml_get("thinking", "max_completion_models", None)
    prefixes = cfg if isinstance(cfg, list) and cfg else _MAX_COMPLETION_MODELS_DEFAULT
    name = model.lower()
    return any(name.startswith(str(p).lower()) for p in prefixes if p)


#: Plafonds de sortie connus par famille (au-delà → 400 upstream).
#: Famille absente = plafond inconnu → passthrough (pas de borne inventée).
_MAX_OUTPUT_TOKENS_CAPS = (("muse", 131072), ("spark", 131072))


def _clamp_max_output_tokens(value: int, model_id: str) -> int:
    """Borne haute de sortie (parité capacité modèle — ex. 128k muse-spark).

    Une demande au-delà de la capacité part telle quelle officiellement… et
    revient en 400. Le proxy clamp explicitement (log) au lieu de laisser
    l'upstream rejeter tout le tour.
    """
    try:
        lid = str(model_id or "").lower()
    except Exception:
        return value
    for prefix, cap in _MAX_OUTPUT_TOKENS_CAPS:
        if prefix in lid and value > cap:
            _debug(f"  [convert] max_output_tokens {value} > cap {cap} ({prefix}) → clampé")
            return cap
    return value


def _set_output_token_limit(
    target: dict,
    source: dict,
    model: str,
    default: int | None = 32000,
    target_protocol: str = "openai",
) -> int:
    """[Lot L14 — B2/A17] Écrit la limite de sortie dans ``target``, forme adaptée.

    Défaut 32000 = défaut officiel mesuré (Chat 1.19.0 : `max_tokens: 32000` ;
    Anthropic : `DEFAULT_MAX_TOKENS = 32_000`).

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

    **[V3 — écart E2]** Après résolution client/défaut, la limite est
    bornée par ``models.<id>.output_limit`` (catalogue live) quand le
    modèle en a une. Le plafond dur à 32 000 du client officiel n'est
    **pas** recopié (§14.3 : défaut réglable de ce client, pas une loi de
    l'amont) ; ce qui est borné, c'est la limite réelle du modèle. Table
    vide ou modèle absent ⇒ comportement d'origine, inchangé.
    """
    limit = None
    for key in ("max_tokens", "max_completion_tokens", "max_output_tokens"):
        candidate = source.get(key)
        # `True`/`False` sont des `int` en Python : sans ce garde-fou, un
        # `max_tokens: true` deviendrait une limite de 1 token et tronquerait
        # toute réponse. `0` reste exclu comme avant : une limite nulle n'est
        # pas une limite, c'est une absence — le défaut s'applique (le passage
        # de `0` en valeur explicite avait été introduit le 2026-10-10 au nom
        # d'une « parité officielle / pre-warm cache » qu'aucune source du
        # dépôt n'atteste : `prewarm_after_rotate` de free/rotation.py concerne
        # larotation de clé, pas une limite à 0. Un `max_tokens: 0` relayé à
        # l'amont est un 400 garanti sur les upstreams stricts).
        if isinstance(candidate, int) and candidate > 0 and not isinstance(candidate, bool):
            limit = candidate
            break
    if limit is None:
        # Le client n'a rien demandé : ne pas inventer de champ, sauf défaut
        # explicitement voulu par l'appelant (comportement historique 16384).
        if default is None:
            return 0
        limit = default

    # [V3 — écart E2] Borne par la limite de sortie réelle du modèle cible.
    # Placé AVANT l'écriture dans `target`, donc aussi avant la branche
    # `max_completion_tokens` : la valeur écrite est déjà bornée. Aucun effet
    # si `models.<id>.output_limit` est absent (module pur, lecture live).
    limit = _clamp_output_token_limit(limit, model)

    if target_protocol == "anthropic":
        target["max_tokens"] = limit
    elif _wants_max_completion_tokens(model):
        target["max_completion_tokens"] = limit
        _debug(f"  [convert] {model}: max_tokens→max_completion_tokens={limit} (modèle de raisonnement, B2)")
    else:
        target["max_tokens"] = limit
    return limit


def _extract_cache_creation_tokens(usage: dict) -> int:
    """[Lot H1] Tokens écrits en cache côté upstream OpenAI, si le champ existe.

    OpenAI n'expose pas de champ standard unique : certains upstreams renvoient
    `prompt_tokens_details.cache_creation_tokens`, d'autres un top-level
    `cache_creation_input_tokens` (passthrough Anthropic-compat). Fallback 0
    quand absent.

    ``prompt_cache_miss_tokens`` (DeepSeek & compatibles) n'est **pas** de la
    création de cache : c'est l'input non caché, déjà compté dans
    ``prompt_tokens - cached_tokens``. Le compter ici le faisait additionner
    une seconde fois par les clients Anthropic (contexte ~2× → autocompact
    prématuré).
    """
    details = usage.get("prompt_tokens_details") or {}
    if "cache_creation_tokens" in details:
        return details["cache_creation_tokens"]
    if "cache_creation_input_tokens" in details:
        return details["cache_creation_input_tokens"]
    if "cache_write_tokens" in details:
        return details["cache_write_tokens"]
    if "cache_creation_input_tokens" in usage:
        return usage["cache_creation_input_tokens"]
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
                    _src = i.get("source")
                    _stype = _src.get("type", "unknown") if isinstance(_src, dict) else "unknown"
                    parts.append(f"[image:{_stype}]")
                elif i.get("type") == "document":
                    _src_d = i.get("source")
                    _stype_d = _src_d.get("type", "unknown") if isinstance(_src_d, dict) else "unknown"
                    parts.append(f"[document:{_stype_d}]")
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


def _effort_to_reasoning(effort_level: str, model: str) -> str | None:
    """Map generic effort to model-specific reasoning_effort (config-driven).

    Délègue à ``config.effort_caps.clamp_effort`` : plafond par modèle lu en
    live depuis ``thinking.effort_order`` / ``thinking.effort_caps``
    (``config.yaml``). Signature inchangée (coquille fine — réexportée,
    utilisée par tests + opencode.py).

    Robustesse : ``None``/``""``/``"none"``/``"off"`` → ``None`` (raisonnement
    désactivé, parité thinkingLevel `off` — les appelants ne posent alors AUCUN
    bloc `reasoning`) ; niveau inconnu non vide → passthrough inchangé
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
            clamped = "__error__"
        if isinstance(clamped, str) and clamped and clamped != "__error__":
            return clamped
        if clamped is None:
            # Désactivé (None/""/"none"/"off") : pas de raisonnement.
            return None
        # Erreur de clamp → repli historique ci-dessous.
        _lvl = str(effort_level or "").strip().lower()
        if _lvl in ("", "none", "off"):
            return None
        if _lvl in ("medium", "high", "xhigh", "max"):
            return "high" if _lvl in ("high", "xhigh", "max") else "medium"
        return "low"
    # Fallback hardcodé = comportement pré-patch (branche défaut historique).
    _lvl0 = str(effort_level or "").strip().lower()
    if _lvl0 in ("", "none", "off"):
        return None
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
_ANTHROPIC_EFFORT_ALIASES = {"minimal": "low", "none": "", "off": "", "": ""}


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
        if not _PARITY_DISABLED:
            # Parité OpenCodeClient : `display: summarized` sur adaptive moderne
            # (défaut `omitted` = blocs vides), `blockBinding: drop_block` sur
            # 5.1+ (préfixe re-rendu entre tours, sinon 400). Opt-out : un
            # `thinking.blockBinding: false` explicite du client est respecté.
            try:
                _explicit = thinking.get("blockBinding")
                if _explicit is False:
                    # Opt-out OpenCode-only : clé consommée, pas de blockBinding.
                    del thinking["blockBinding"]
                    if thinking.get("type") == "adaptive" and anthropic_modern_adaptive_thinking(model):
                        thinking.setdefault("display", "summarized")
                elif _explicit is None:
                    apply_anthropic_thinking_display(thinking, model)
                # Sinon : blockBinding explicite du client conservé tel quel.
            except Exception:
                pass
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

    # C2 : un breakpoint client `ttl: 1h` (cache long, coûteux à réchauffer)
    # survit à un breakpoint auto 5 min — on sacrifie d'abord les breakpoints
    # sans TTL explicite, à ancienneté égale.
    def _has_long_ttl(holder: dict) -> bool:
        cc = holder.get("cache_control")
        return isinstance(cc, dict) and str(cc.get("ttl", "")).strip().lower() == "1h"

    victims = [h for h in slots[:excess] if not _has_long_ttl(h)]
    victims += [h for h in slots[:excess] if _has_long_ttl(h)]
    if len(slots) > excess:
        # Les suivants (profonds) ne sont élagués qu'en dernier recours, même
        # ordre de préférence TTL.
        victims += [h for h in slots[excess:] if not _has_long_ttl(h)]
        victims += [h for h in slots[excess:] if _has_long_ttl(h)]
    for holder in victims[:excess]:
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
    # [audit 2026-10-30] Preserve the client's cache_control (notably ttl:1h)
    # instead of replacing it with a fresh default ephemeral breakpoint.
    _sys_cc = messages[sys_idx].get("cache_control")
    _static_cc = dict(_sys_cc) if isinstance(_sys_cc, dict) else {"type": "ephemeral"}
    _static_cc.setdefault("type", "ephemeral")
    new_messages = [
        {
            "role": "system",
            "content": static_part,
            "cache_control": _static_cc,
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
    prefix = _TRAILING_DIGITS_RE.sub("", prefix)
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
        # 14. Parité officielle v2 (objectRoot, jamais additionalProperties:false) :
        # l'officiel est en passthrough sur openai/responses et ne force jamais
        # `false` (commentaire : providers stricts 400 sur champs inconnus).
        # On ne force donc rien ; le strip True/dict ci-dessus suffit.
        # (ancien forçage `node["additionalProperties"] = False` retiré 2026-10-15)
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


def _system_cache_control(system_val: list) -> dict:
    """Breakpoint de cache du système : préserve celui du client (dont `ttl`).

    Sans cela, un `cache_control: {ttl: 1h}` posé par le client sur le système
    était aplati par `_extract_text` puis remplacé par un breakpoint nu =
    rétrogradé silencieusement à 5 min. Dernier breakpoint client gagnant ;
    défaut nu seulement si le client n'en a posé aucun.
    """
    cc: dict = {"type": "ephemeral"}
    for block in system_val or []:
        if isinstance(block, dict) and isinstance(block.get("cache_control"), dict):
            cc = dict(block["cache_control"])
            cc.setdefault("type", "ephemeral")
    return cc


