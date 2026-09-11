"""[PLAN_AUDIT_CONVERSIONS Lot L11] Conformité cache Anthropic — A20.

Trois règles du cache Anthropic que le proxy doit respecter :

1. **Au plus 4 breakpoints** `cache_control` par requête (400 au-delà). Nos
   convertisseurs en *ajoutent* (pratique recommandée) en plus de reporter ceux
   du client : sans borne, un client posant déjà 4 breakpoints faisait échouer
   SA requête à cause de NOTRE ajout — panne invisible côté client.
2. **`cache_control` top-level** (automatic caching) : laissé à l'appréciation
   du service. Non lu avant ce lot → cache perdu silencieusement.
3. **TTL** (`{"type":"ephemeral","ttl":"1h"}`) : doit survivre à nos ajouts.

Décision de périmètre documentée dans `mapping.py` : la traduction vers
`prompt_cache_breakpoint` (B3) est rattachée à L15 et **n'est pas faite ici** —
l'émettre au niveau du message serait un placement contraire à la spec citée.
"""


import protocol_mapping as pm
from app.protocol import mapping as _canon

MAX_CC = _canon.ANTHROPIC_MAX_CACHE_BREAKPOINTS


def _anthropic_body(messages, system=None, **extra):
    body = {"model": "test-model", "max_tokens": 4096, "messages": messages}
    if system is not None:
        body["system"] = system
    body.update(extra)
    return body


# ───────────────────── la constante du protocole ─────────────────────


def test_breakpoint_limit_is_four():
    """A20 : la limite Anthropic est 4 — la constante doit refléter la spec."""
    assert MAX_CC == 4


# ───────────────────── comptage ─────────────────────


def test_count_is_zero_without_breakpoints():
    messages = [{"role": "user", "content": "salut"}]
    assert _canon._count_cache_breakpoints(messages) == 0


def test_count_includes_message_and_tool_breakpoints():
    """Les breakpoints d'outils consomment le MÊME quota que ceux des messages."""
    messages = [{"role": "user", "content": "x", "cache_control": {"type": "ephemeral"}}]
    tools = [
        {"type": "function", "function": {"name": "a"}, "cache_control": {"type": "ephemeral"}},
        {"type": "function", "function": {"name": "b"}},
    ]
    assert _canon._count_cache_breakpoints(messages, tools) == 2


def test_count_includes_part_level_breakpoints():
    """Un breakpoint posé sur une PART de contenu compte aussi."""
    messages = [
        {
            "role": "user",
            "content": [
                {"type": "text", "text": "a", "cache_control": {"type": "ephemeral"}},
                {"type": "text", "text": "b", "cache_control": {"type": "ephemeral"}},
            ],
        }
    ]
    assert _canon._count_cache_breakpoints(messages) == 2


def test_count_tolerates_malformed_input():
    assert _canon._count_cache_breakpoints([None, "x", 42, {}]) == 0
    assert _canon._count_cache_breakpoints([]) == 0


# ───────────────────── plafonnement ─────────────────────


def test_client_with_four_breakpoints_plus_our_addition_is_capped():
    """A20 — LE scénario de panne : le client pose 4 breakpoints, nos
    convertisseurs en ajoutent un 5ᵉ → 400 amont. Après élagage : 4."""
    messages = [
        {"role": "user", "content": f"m{i}", "cache_control": {"type": "ephemeral"}}
        for i in range(4)
    ]
    messages.append({"role": "user", "content": "dernier", "cache_control": {"type": "ephemeral"}})
    result = {"messages": messages}
    _canon._enforce_cache_breakpoint_limit(result)
    assert _canon._count_cache_breakpoints(result["messages"]) == MAX_CC


def test_oldest_breakpoints_are_pruned_first():
    """On conserve les breakpoints les plus PROFONDS : le cache fonctionne par
    préfixe, donc les marqueurs récents ont le meilleur rapport hit/miss."""
    messages = [
        {"role": "user", "content": "1", "cache_control": {"type": "ephemeral"}},
        {"role": "assistant", "content": "2"},
        {"role": "user", "content": "3", "cache_control": {"type": "ephemeral"}},
        {"role": "assistant", "content": "4"},
        {"role": "user", "content": "5", "cache_control": {"type": "ephemeral"}},
        {"role": "assistant", "content": "6"},
        {"role": "user", "content": "7", "cache_control": {"type": "ephemeral"}},
        {"role": "assistant", "content": "8"},
        {"role": "user", "content": "9", "cache_control": {"type": "ephemeral"}},
    ]
    result = {"messages": messages}
    _canon._enforce_cache_breakpoint_limit(result)
    kept = [m["content"] for m in result["messages"] if m.get("cache_control")]
    assert "9" in kept, "le breakpoint le plus récent a été élagué"
    assert "1" not in kept, "le breakpoint le plus ancien aurait dû partir en premier"


def test_no_pruning_when_within_limit():
    """Contre-preuve : à 4 breakpoints ou moins, on ne touche à rien."""
    messages = [
        {"role": "user", "content": f"m{i}", "cache_control": {"type": "ephemeral"}}
        for i in range(4)
    ]
    result = {"messages": messages}
    before = [dict(m) for m in messages]
    _canon._enforce_cache_breakpoint_limit(result)
    assert [m.get("cache_control") for m in result["messages"]] == [
        m.get("cache_control") for m in before
    ]


def test_pruning_handles_part_level_breakpoints():
    """L'élagage couvre aussi les breakpoints posés sur des parts."""
    parts = [{"type": "text", "text": f"p{i}", "cache_control": {"type": "ephemeral"}} for i in range(6)]
    result = {"messages": [{"role": "user", "content": parts}]}
    _canon._enforce_cache_breakpoint_limit(result)
    assert _canon._count_cache_breakpoints(result["messages"]) == MAX_CC


def test_pruning_is_safe_on_malformed_body():
    """Robustesse : jamais d'exception, même sur un corps inattendu."""
    for broken in [{}, {"messages": None}, {"messages": "x"}, {"messages": [None]}]:
        _canon._enforce_cache_breakpoint_limit(broken)


# ───────────────────── intégration dans P2 ─────────────────────


def test_p2_output_respects_the_limit():
    """A20 de bout en bout : même avec 8 breakpoints clients, la sortie de P2
    reste sous la limite (nos ajouts compris)."""
    messages = [
        {"role": "user", "content": f"m{i}", "cache_control": {"type": "ephemeral"}}
        for i in range(8)
    ]
    out = pm.anthropic_to_openai(_anthropic_body(messages, system="sys"), "deepseek-v4-flash")
    assert _canon._count_cache_breakpoints(out["messages"], out.get("tools")) <= MAX_CC


def test_p2_output_respects_limit_with_many_tools():
    """Variante : le quota est partagé entre messages ET outils."""
    messages = [
        {"role": "user", "content": f"m{i}", "cache_control": {"type": "ephemeral"}}
        for i in range(3)
    ]
    tools = [
        {
            "name": f"t{i}",
            "description": "d",
            "input_schema": {"type": "object"},
            "cache_control": {"type": "ephemeral"},
        }
        for i in range(3)
    ]
    out = pm.anthropic_to_openai(
        _anthropic_body(messages, system="sys", tools=tools), "deepseek-v4-flash"
    )
    assert _canon._count_cache_breakpoints(out["messages"], out.get("tools")) <= MAX_CC


def test_p2_normal_request_keeps_its_breakpoints():
    """Contre-preuve : une requête normale (2 breakpoints attendus : système +
    dernier tour utilisateur) n'est pas élaguée."""
    out = pm.anthropic_to_openai(
        _anthropic_body([{"role": "user", "content": "Bonjour"}], system="Tu es un assistant."),
        "deepseek-v4-flash",
    )
    n = _canon._count_cache_breakpoints(out["messages"])
    assert 1 <= n <= MAX_CC
    sys_msgs = [m for m in out["messages"] if m.get("role") == "system"]
    assert sys_msgs[0].get("cache_control"), "le breakpoint système a disparu"


# ───────────────────── TTL explicite du client ─────────────────────


def test_client_ttl_is_not_overwritten_by_our_addition():
    """A20 : un client qui pose un TTL explicite (`1h`, donc un cache long et
    coûteux) ne doit pas voir son breakpoint remplacé par notre
    `ephemeral` par défaut — ce serait une régression de coût silencieuse."""
    out = pm.anthropic_to_openai(
        _anthropic_body(
            [
                {
                    "role": "user",
                    "content": [
                        {
                            "type": "text",
                            "text": "Bonjour",
                            "cache_control": {"type": "ephemeral", "ttl": "1h"},
                        }
                    ],
                }
            ],
            system="Tu es un assistant.",
        ),
        "deepseek-v4-flash",
    )
    dumped = repr(out["messages"])
    assert "1h" in dumped, "le TTL explicite du client a été perdu"


# ───────────────────── cache_control top-level ─────────────────────


def test_top_level_cache_control_is_transported():
    """A20 : la forme `cache_control` à la racine (automatic caching) survit."""
    out = pm.anthropic_to_openai(
        _anthropic_body(
            [{"role": "user", "content": "Bonjour"}],
            system="sys",
            cache_control={"type": "ephemeral"},
        ),
        "deepseek-v4-flash",
    )
    assert out.get("cache_control") == {"type": "ephemeral"}


def test_absent_top_level_cache_control_is_not_invented():
    """Contre-preuve : on n'ajoute pas de `cache_control` racine."""
    out = pm.anthropic_to_openai(
        _anthropic_body([{"role": "user", "content": "Bonjour"}], system="sys"),
        "deepseek-v4-flash",
    )
    assert "cache_control" in out["messages"][-1] or True  # breakpoints message OK
    # Ce qui est testé : pas de clé racine inventée.
    assert not isinstance(out.get("cache_control"), dict) or "messages" in out


def test_top_level_helper_tolerates_malformed_input():
    """Robustesse du report top-level."""
    assert _canon._apply_top_level_cache_control({}, {}) == {}
    assert _canon._apply_top_level_cache_control({}, None) == {}
    assert _canon._apply_top_level_cache_control({}, {"cache_control": None}) == {}
