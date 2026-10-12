"""[PLAN ALIGNEMENT CLIENT OFFICIEL — C1] Allow-list d'effort par modèle.

**La divergence.** Le client officiel dérive les variantes d'effort de
``reasoning_options`` (catalogue) et n'émet que celles-là
(``transform.ts:1717-1762``). Nous n'avions qu'un plafond
(``config/effort_caps.py``) : d'où deux erreurs symétriques — un effort
envoyable chez nous mais refusé par le modèle (400), et un effort autorisé
chez lui mais inatteignable chez nous (plan §5).

**Le correctif.** ``config/reasoning_options.py`` : table
``thinking.reasoning_options`` (modèle → variantes autorisées), matching
longest-prefix insensible à la casse, et le clamp

    niveau_émis = plus grande autorisée ≤ demandée
                  (repli : la plus petite autorisée)

appliqué dans ``resolve_effort`` **après** ``clamp_level``, car l'allow-list
est la déclaration de ce que l'amont accepte : elle gagne quand les deux
parlent.

**Ce que ces tests protègent d'abord.** Le flag.
``thinking.allow_list_enabled`` vaut ``false`` par défaut et le clamp est
alors un passage à l'identique — y compris avec la table déjà peuplée. C'est
la garantie « sans rien casser » de la vague : la première section est un
test **différentiel**, flag off VS table absente, sur une matrice de modèles
et de niveaux, tous champs de ``EffortDecision`` comparés.

Les sections 2 et 3 testent le mécanisme flag armé (table injectée, donc
indépendant du ``config.yaml`` du jour) ; la dernière vérifie l'hygiène de la
table réelle et surtout que le flag est bien à ``false`` dans le fichier
versionné.
"""

import copy

import pytest

import config.effort_caps as ec
import config.effort_policy as ep
import config.reasoning_options as ro
import config.settings as cfg

ORDER = ["minimal", "low", "medium", "high", "xhigh", "max"]

# Modèles du §5, avec leur liste officielle et ce qu'elle donne sur `high`/`max`.
SECTION5 = {
    "deepseek-v4-pro": ["high", "max"],
    "glm-5.3": ["low", "high", "max"],
    "kimi-k3": ["max"],
    "muse-spark": ["minimal", "low", "medium", "high", "xhigh"],
    "space-bunny": ["low", "medium", "high", "xhigh", "max"],
    "hy4-preview": ["none", "high"],
    "hy3": ["none", "low", "high"],
    "kimi-k2.6": [],
    "minimax-m2.7": [],
}


# ─────────────────────────── isolation de `thinking:` ───────────────────────────


@pytest.fixture
def isolated_thinking(monkeypatch):
    """Copie la section ``thinking:`` réelle et laisse le test la réécrire.

    Lecture live (pas de snapshot à l'import) : réécrire la section entre
    deux appels prend effet immédiatement — exactement ce que fait le
    hot-reload sur mtime de ``config.yaml``.
    """
    saved = copy.deepcopy(cfg._yaml_data.get("thinking"))

    def _set(options=None, *, flag=False, order=ORDER, caps=None):
        section = {"effort_order": list(order)}
        if caps is not None:
            section["effort_caps"] = dict(caps)
        if flag is not None:
            section[ro.FLAG_KEY] = flag
        if options is not None:
            section[ro.OPTIONS_KEY] = copy.deepcopy(options)
        cfg._yaml_data["thinking"] = section

    yield _set
    if saved is None:
        cfg._yaml_data.pop("thinking", None)
    else:
        cfg._yaml_data["thinking"] = saved


# ─────────────── 1. flag off = comportement historique, bit pour bit ───────────────


def _all_decisions(model):
    """Toutes les décisions d'effort possibles pour ``model``."""
    bodies = [{}]
    bodies += [{"effort": lvl} for lvl in ("none", "off", "minimal", "low", "medium", "high", "xhigh", "max")]
    bodies += [
        {"thinking": {"type": "enabled"}},
        {"thinking": {"type": "adaptive"}},
        {"thinking": {"type": "enabled", "budget_tokens": 16000}},
        {"output_config": {"effort": "max"}},
    ]
    return [(b, ep.resolve_effort(b, model)) for b in bodies]


def test_flag_off_is_bit_for_bit_the_table_absent(isolated_thinking):
    """LE test de non-régression de la vague.

    Même entrée, même modèle : ``resolve_effort`` avec la table **peuplée**
    et le flag à ``false`` doit rendre exactement ce qu'il rend avec la
    table **absente**. Tous les champs de ``EffortDecision`` sont comparés,
    pas seulement ``level`` — sinon un ``wants`` ou une ``source`` qui
    bougerait passerait inaperçu.
    """
    options = {k: {"variants": list(v)} for k, v in SECTION5.items()}
    for model in list(SECTION5) + ["opus", "totally-unknown-xyz", "glm-5.1", "minimax-m2.5"]:
        with_flag_off, without_table = [], []
        isolated_thinking(options, flag=False)
        for body, decision in _all_decisions(model):
            with_flag_off.append((body, decision))
        isolated_thinking(None, flag=False)  # table absente
        for body, decision in _all_decisions(model):
            without_table.append((body, decision))
        assert with_flag_off == without_table, f"{model}: le flag off change le résultat"


def test_flag_absent_means_off(isolated_thinking):
    """Pas de clé = désarmé (défaut sûr), table peuplée ou non."""
    cfg._yaml_data["thinking"] = {"effort_order": list(ORDER)}
    assert ro.allow_list_enabled() is False
    assert ro.clamp_effort_to_allow_list("high", "kimi-k3") == "high"


def test_section_absent_means_off(monkeypatch):
    monkeypatch.setitem(cfg._yaml_data, "thinking", None)
    assert ro.allow_list_enabled() is False
    assert ro.clamp_effort_to_allow_list("max", "kimi-k3") == "max"
    assert ro.get_allowed_variants("kimi-k3") is None


@pytest.mark.parametrize("raw", [True, 1, "true", "yes", "on"])
def test_truthy_flag_values_arm_the_clamp(isolated_thinking, raw):
    """Le flag est lu au sens booléen de Python ; les formes YAML courantes
    d'un « vrai » arment toutes le clamp. Une chaîne vide ou ``none`` reste
    falsy — rien à tester de plus, c'est la règle du langage, pas la nôtre.
    """
    isolated_thinking({"kimi-k3": {"variants": ["max"]}}, flag=raw)
    assert ro.allow_list_enabled() is True
    assert ro.clamp_effort_to_allow_list("high", "kimi-k3") == "max"


# ─────────────────────────── 2. lecture de la table ───────────────────────────


def test_lookup_exact_prefix_and_case_insensitive(isolated_thinking):
    isolated_thinking({"muse-spark": ["low", "high"]})
    assert ro.get_allowed_variants("muse-spark-1.3-contributor-free") == ["low", "high"]
    assert ro.get_allowed_variants("MUSE-SPARK-1.3") == ["low", "high"]
    assert ro.get_allowed_variants("  muse-spark-1.3  ") == ["low", "high"]


def test_longest_prefix_wins(isolated_thinking):
    isolated_thinking({"glm-5": ["max"], "glm-5.3": ["low"]})
    assert ro.get_allowed_variants("glm-5.3-flash") == ["low"]
    assert ro.get_allowed_variants("glm-5-air") == ["max"]


def test_entry_shapes_are_all_accepted(isolated_thinking):
    isolated_thinking(
        {
            "a-one": ["low", "high"],  # liste directe
            "b-two": "max",  # chaîne unique
            "c-three": {"variants": ["low"]},  # mapping
            "d-four": {"variants": []},  # liste vide = aucune info
            "e-five": {"variants": "high"},  # chaîne dans le mapping
        }
    )
    assert ro.get_allowed_variants("a-one") == ["low", "high"]
    assert ro.get_allowed_variants("b-two") == ["max"]
    assert ro.get_allowed_variants("c-three") == ["low"]
    assert ro.get_allowed_variants("d-four") is None
    assert ro.get_allowed_variants("e-five") == ["high"]


def test_variants_are_deduplicated_and_lowercased(isolated_thinking):
    isolated_thinking({"kimi-k3": ["MAX", " max ", "Max"]})
    assert ro.get_allowed_variants("kimi-k3") == ["max"]


@pytest.mark.parametrize("entry", [None, 12, 3.5, True, {"variants": None}, {"variants": 7}])
def test_malformed_entry_yields_no_information(isolated_thinking, entry):
    """Une entrée cassée ne fait pas exploser la lecture : elle se tait."""
    isolated_thinking({"kimi-k3": entry, "default": ["low"]})
    assert ro.get_allowed_variants("kimi-k3") == ["low"]


def test_default_key_catches_unlisted_models(isolated_thinking):
    isolated_thinking({"default": ["low", "max"], "kimi-k3": ["max"]})
    assert ro.get_allowed_variants("n-importe-quoi") == ["low", "max"]
    assert ro.get_allowed_variants("kimi-k3") == ["max"]


def test_empty_list_is_not_a_forbidden_list(isolated_thinking):
    """Décision documentée : ``variants: []`` = « aucune information ».

    Désactiver le raisonnement serait une décision forte et **silencieuse** ;
    la faire sur une liste vide — qui dans le catalogue signifie plutôt
    « champ non renseigné » — coûterait plus cher qu'un 400 visible.
    """
    isolated_thinking({"kimi-k2.6": {"variants": []}}, flag=True)
    assert ro.get_allowed_variants("kimi-k2.6") is None
    assert ro.clamp_effort_to_allow_list("high", "kimi-k2.6") == "high"


# ─────────────────────── 3. le clamp, flag armé ───────────────────────


@pytest.mark.parametrize(
    "model,asked,expected",
    [
        # §5, ligne par ligne.
        ("deepseek-v4-pro", "low", "high"),
        ("deepseek-v4-pro", "medium", "high"),
        ("deepseek-v4-pro", "high", "high"),
        ("deepseek-v4-pro", "max", "max"),
        ("glm-5.3", "low", "low"),
        ("glm-5.3", "medium", "low"),  # plus grande autorisée ≤ medium
        ("glm-5.3", "max", "max"),
        ("glm-5.3-flash", "medium", "low"),  # préfixe glm-5.3
        ("kimi-k3", "low", "max"),  # repli sur la plus petite autorisée
        ("kimi-k3", "high", "max"),  # ...même quand elle dépasse le demandé
        ("kimi-k3", "max", "max"),
        ("muse-spark-1.3", "max", "xhigh"),  # pas de `max` dans la liste
        ("muse-spark-1.3", "xhigh", "xhigh"),
        ("muse-spark-1.3", "minimal", "minimal"),
        ("space-bunny", "max", "max"),
        ("space-bunny", "xhigh", "xhigh"),
        ("hy4-preview", "low", "high"),  # none ignoré : pas un niveau d'ordre
        ("hy4-preview", "max", "high"),
        ("hy3", "medium", "low"),
        ("hy3", "max", "high"),
    ],
)
def test_section5_clamp_matrix(isolated_thinking, model, asked, expected):
    isolated_thinking({k: {"variants": list(v)} for k, v in SECTION5.items()}, flag=True)
    assert ro.clamp_effort_to_allow_list(asked, model) == expected


def test_allow_list_wins_over_the_local_cap(isolated_thinking):
    """L'ordre des deux clamps est le cœur du correctif.

    ``kimi-k3`` ne catalogue que ``max`` ; notre plafond local vaut ``high``.
    Allow-list **en dernier** ⇒ ``max`` (accepté par l'amont). Dans l'autre
    ordre on repartirait avec ``high``, hors liste, donc 400 — exactement la
    divergence à corriger.
    """
    isolated_thinking({"kimi-k3": {"variants": ["max"]}}, flag=True, caps={"default": "high"})
    assert ec.get_max_effort_for_model("kimi-k3") == "high"
    assert ro.clamp_effort_to_allow_list("high", "kimi-k3") == "max"


@pytest.mark.parametrize("value", [None, "none", "off", "NONE", " Off "])
def test_disabled_levels_pass_through(isolated_thinking, value):
    isolated_thinking({"kimi-k3": {"variants": ["max"]}}, flag=True)
    assert ro.clamp_effort_to_allow_list(value, "kimi-k3") == value


def test_unknown_level_passes_through(isolated_thinking):
    """Robustesse forward : un futur niveau n'est pas écrasé par la table."""
    isolated_thinking({"kimi-k3": {"variants": ["max"]}}, flag=True)
    assert ro.clamp_effort_to_allow_list("niveau-futur", "kimi-k3") == "niveau-futur"


def test_allow_list_outside_the_known_order_is_ignored(isolated_thinking):
    """Rien à comparer sans ordre commun : on n'invente pas de niveau.

    Même si la table est peuplée, si **aucune** de ses entrées n'est un
    niveau de ``effort_order``, le clamp n'écrase pas le niveau demandé.
    La table est muette, pas contraignante.
    """
    isolated_thinking({"kimi-k3": {"variants": ["banana", "cherry"]}}, flag=True, order=ORDER)
    assert ro.clamp_effort_to_allow_list("high", "kimi-k3") == "high"


def test_allow_list_partially_inside_the_order_ignores_the_rest(isolated_thinking):
    """Symétriquement, une entrée hors ordre n'invalide pas les autres."""
    isolated_thinking({"kimi-k3": {"variants": ["banana", "max"]}}, flag=True, order=ORDER)
    assert ro.clamp_effort_to_allow_list("high", "kimi-k3") == "max"


def test_unknown_model_gets_no_clamp(isolated_thinking):
    isolated_thinking({"kimi-k3": {"variants": ["max"]}}, flag=True)
    assert ro.clamp_effort_to_allow_list("high", "modele-inconnu-zzz") == "high"


def test_empty_table_is_inert(isolated_thinking):
    isolated_thinking({}, flag=True)
    assert ro.get_allowed_variants("kimi-k3") is None
    assert ro.clamp_effort_to_allow_list("max", "kimi-k3") == "max"


# ─────────────────── 4. bout en bout : `resolve_effort` ───────────────────


def test_resolve_effort_clamps_through_the_unique_entry_point(isolated_thinking):
    options = {k: {"variants": list(v)} for k, v in SECTION5.items()}
    isolated_thinking(options, flag=True, caps={"default": "high"})
    # Plafond local `high` : l'allow-list descend **sous** le plafond quand son
    # niveau n'est pas listé (glm-5.3 n'a pas `medium`), et l'y laisse sinon.
    assert ep.resolve_effort({"effort": "low"}, "deepseek-v4-pro").level == "high"
    assert ep.resolve_effort({"effort": "medium"}, "glm-5.3").level == "low"
    # kimi-k3 : plafond `high`, allow-list `[max]` — intersection vide ⇒ repli
    # sur la plus petite autorisée, qui **dépasse** le plafond. C'est le cœur du
    # correctif (§5) : renvoyer `high` ici serait un 400 garanti.
    assert ep.resolve_effort({"effort": "high"}, "kimi-k3").level == "max"
    # muse-spark : plafond `high`, allow-list contient `high` ⇒ on y reste.
    # L'allow-list ne monte jamais au-dessus du plafond local.
    assert ep.resolve_effort({"effort": "max"}, "muse-spark-1.2").level == "high"
    # ...mais si le plafond monte à `max`, l'allow-list redescend : `xhigh`.
    isolated_thinking(options, flag=True, caps={"default": "max"})
    assert ep.resolve_effort({"effort": "max"}, "muse-spark-1.2").level == "xhigh"


def test_resolve_effort_clamps_the_model_max_sentinel(isolated_thinking):
    """``thinking.enabled`` sans budget = « le maximum du modèle ». Ce maximum
    doit rester **dans** l'allow-list, sinon on renvoie le client au 400."""
    isolated_thinking(
        {k: {"variants": list(v)} for k, v in SECTION5.items()},
        flag=True,
        caps={"default": "high", "muse-spark": "max"},
    )
    # kimi-k3 : plafond local `high`, allow-list `[max]` → max.
    assert ep.resolve_effort({"thinking": {"type": "enabled"}}, "kimi-k3").level == "max"
    # muse-spark : plafond local `max`, allow-list sans `max` → xhigh.
    assert (
        ep.resolve_effort({"thinking": {"type": "adaptive"}}, "muse-spark-1.3").level
        == "xhigh"
    )


def test_resolve_effort_leaves_a_disabled_request_disabled(isolated_thinking):
    """L'allow-list ne ranime jamais un raisonnement désactivé."""
    isolated_thinking({"kimi-k3": {"variants": ["max"]}}, flag=True)
    assert ep.resolve_effort({"effort": "none"}, "kimi-k3").wants is False
    assert ep.resolve_effort({"thinking": {"type": "disabled"}}, "kimi-k3").wants is False
    assert ep.resolve_effort({}, "kimi-k3").wants is False


# ─────────────── garde structurelle sur la config réelle ───────────────


def test_real_config_flag_is_off():
    """Le fichier versionné doit livrer la vague **désarmée**.

    Si cette assertion tombe, quelqu'un a armé le clamp sans passer par la
    vérif live : tout modèle couvert par la table change de comportement.
    """
    assert ro.allow_list_enabled() is False, (
        "thinking.allow_list_enabled est à true dans config.yaml — "
        "la bascule doit se faire en vérif live, pas dans un commit"
    )


def test_real_config_table_is_well_formed():
    """Hygiène de la table réelle : chaque entrée doit être lisible, et au
    moins une doit exister — sinon le flag ne sert à rien et C1 est un
    coup d'épée dans l'eau."""
    table = (cfg._yaml_data.get("thinking") or {}).get(ro.OPTIONS_KEY)
    assert isinstance(table, dict) and table, "table reasoning_options absente ou vide"
    readable = 0
    for model in table:
        if ro.get_allowed_variants(model) is None:
            continue  # entrée volontairement vide (aucune information)
        readable += 1
    assert readable, "aucune entrée exploitable dans reasoning_options"


def test_real_config_clamp_never_exits_the_allow_list():
    """Filet global, flag armé artificiellement : quelle que soit la table du
    jour, un niveau émis est toujours soit inchangé, soit dans l'allow-list."""
    section = cfg._yaml_data.get("thinking")
    if not isinstance(section, dict):
        pytest.skip("config.yaml sans section thinking: lisible")
    table = section.get(ro.OPTIONS_KEY)
    if not isinstance(table, dict) or not table:
        pytest.skip("table reasoning_options absente")
    saved = section.get(ro.FLAG_KEY)
    try:
        section[ro.FLAG_KEY] = True
        for model in list(table):
            allowed = ro.get_allowed_variants(model) or []
            for asked in ORDER:
                out = ro.clamp_effort_to_allow_list(asked, model)
                if not allowed:
                    assert out == asked, f"{model}: {asked} → {out} sans allow-list"
                else:
                    assert out in allowed or out == asked, f"{model}: {asked} → {out} hors {allowed}"
    finally:
        if saved is None:
            section.pop(ro.FLAG_KEY, None)
        else:
            section[ro.FLAG_KEY] = saved
