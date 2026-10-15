"""[PLAN ALIGNEMENT CLIENT OFFICIEL — V3, écart E2] Borne de sortie par modèle.

**Le divergence.** Le client officiel envoie ``min(model.limit.output,
32000)`` sur les trois protocoles (``transform.ts:18,1481`` →
``anthropic-messages.ts:546``, ``openai-chat.ts:361``,
``openai-responses.ts:493``). Notre proxy, lui, ne bornait rien : un client
qui demandait 200 000 tokens de sortie les obtenait tels quels, et l'upstream
tronquait ou refusait — le code d'erreur remontait alors comme une panne
alors que la demande était simplement hors catalogue.

**Ce qu'on prend, ce qu'on laisse.** Le plafond dur à 32 000 est un défaut
**réglable** de ce client (§14.3 du plan) : le copier régresserait nos autres
clients, notamment Claudley Code, qui compte sur des sorties longues. La
limite, elle, est une loi de l'amont. D'où ``config/limits.py`` : une table
``models.<id>.output_limit`` alimentée par ``limit.output`` du catalogue
live, lue en live, et un ``min()`` appliqué dans
``_set_output_token_limit`` **après** résolution client/16384 et **avant**
la branche ``max_completion_tokens`` — donc la valeur écrite est déjà bornée,
quelle que soit la forme émise.

**L'invariant de sécurité.** Table vide, modèle absent, ou valeur
invalide ⇒ comportement d'origine bit pour bit. Et la lecture se fait sur
``_yaml_data`` brut (donc invisible à ``MODELS``, qui n'expose qu'un jeu de
clés fixe) : ajouter la clé ne peut rien casser dans la résolution
d'endpoint, la découverte ou le dashboard.

Ces tests ne dépendent pas du contenu de ``config.yaml`` (même pattern
d'isolation que ``tests/test_effort_caps.py``), sauf la dernière section,
explicitement marquée, qui vérifie l'hygiène de la table réelle.
"""

import copy

import pytest

import config.limits as cl
import config.settings as cfg
from app.protocol.mapping import _set_output_token_limit, anthropic_to_openai

# ─────────────────── isolation de la section `models:` ───────────────────


@pytest.fixture
def isolated_models(monkeypatch):
    """Isole ``_yaml_data["models"]`` et laisse le test injecter sa table.

    Lecture live (pas de snapshot à l'import) : réécrire la section entre
    deux appels prend effet immédiatement — exactement ce que fait le
    hot-reload existant sur mtime de ``config.yaml``.
    """
    saved = copy.deepcopy(cfg._yaml_data.get("models"))
    table: dict = {}
    monkeypatch.setitem(cfg._yaml_data, "models", table)

    def _load(entries: dict) -> None:
        table.clear()
        table.update(entries)

    yield _load
    if saved is None:
        cfg._yaml_data.pop("models", None)
    else:
        cfg._yaml_data["models"] = saved


# ─────────────────────────── unitaire : `output_limit` ───────────────────────────


def test_output_limit_exact_then_case_insensitive(isolated_models):
    isolated_models({"kimi-k2.6": {"output_limit": 65536}})
    assert cl.output_limit("kimi-k2.6") == 65536
    assert cl.output_limit("KIMI-K2.6") == 65536
    assert cl.output_limit("  kimi-k2.6  ") == 65536


def test_output_limit_none_when_model_absent(isolated_models):
    isolated_models({"kimi-k2.6": {"output_limit": 65536}})
    assert cl.output_limit("kimi-k2.7-code") is None
    assert cl.output_limit("") is None
    assert cl.output_limit(None) is None


def test_output_limit_none_when_entry_is_not_a_dict(isolated_models):
    """Une entrée mal formée ne doit pas faire exploser la lecture."""
    isolated_models({"kimi-k2.6": "65536", "glm-5.1": None, "hy3": 12})
    assert cl.output_limit("kimi-k2.6") is None
    assert cl.output_limit("glm-5.1") is None
    assert cl.output_limit("hy3") is None


@pytest.mark.parametrize("bad", [True, False, "65536", 65536.0, 0, -1, None])
def test_output_limit_rejects_non_positive_and_non_int(isolated_models, bad):
    """Piège du booléen : ``True`` est un ``int`` en Python. Une limite de 1
    token tronquerait toute réponse — d'où ``None`` et non ``1``."""
    isolated_models({"kimi-k2.6": {"output_limit": bad}})
    assert cl.output_limit("kimi-k2.6") is None, f"{bad!r} accepté comme limite"


def test_empty_table_behaves_like_no_table(isolated_models):
    """Rollback documenté : vider la table ramène le comportement d'origine."""
    isolated_models({})
    assert cl.output_limit("kimi-k2.6") is None
    assert cl.clamp_output_token_limit(200000, "kimi-k2.6") == 200000


def test_missing_models_section_is_tolerated(isolated_models):
    """Une section absente ne casse rien (table vide interne)."""
    cfg._yaml_data.pop("models", None)
    assert cl.output_limit("kimi-k2.6") is None
    assert cl.clamp_output_token_limit(16384, "kimi-k2.6") == 16384


# ─────────────────────────── unitaire : `clamp` ───────────────────────────


def test_clamp_lowers_only_above_the_model_limit(isolated_models):
    isolated_models({"kimi-k2.6": {"output_limit": 65536}})
    assert cl.clamp_output_token_limit(200000, "kimi-k2.6") == 65536
    assert cl.clamp_output_token_limit(65536, "kimi-k2.6") == 65536  # égal ⇒ inchangé
    assert cl.clamp_output_token_limit(16384, "kimi-k2.6") == 16384  # plus court ⇒ respecté


def test_clamp_never_invents_a_default_for_unknown_model(isolated_models):
    """Pas d'entrée ⇒ la valeur demandée repart telle quelle, y compris
    ``None`` et les formes non entières : ce n'est pas à cette couche de
    valider la demande du client."""
    isolated_models({"kimi-k2.6": {"output_limit": 65536}})
    for value in (200000, 16384, 1, None, "4096", 3.5, True):
        assert cl.clamp_output_token_limit(value, "modele-inconnu-zzz") == value


def test_clamp_leaves_a_bool_even_when_a_limit_exists(isolated_models):
    """``True`` vaut 1 : le clamper à la limite du modèle le ramènerait à 1
    de toute façon, mais on ne veut surtout pas le laisser valoir « limite
    du modèle » si un jour la comparaison change. La forme est rendue telle
    quelle — c'est au helper appelant de la rejeter."""
    isolated_models({"kimi-k2.6": {"output_limit": 65536}})
    assert cl.clamp_output_token_limit(True, "kimi-k2.6") is True
    assert cl.clamp_output_token_limit(False, "kimi-k2.6") is False


# ─────────────────── intégration : `_set_output_token_limit` ───────────────────


def test_helper_clips_the_client_value(isolated_models):
    """Le cœur du correctif : 200 000 demandé, 65 536 écrit."""
    isolated_models({"kimi-k2.6": {"output_limit": 65536}})
    target = {}
    limit = _set_output_token_limit(target, {"max_tokens": 200000}, "kimi-k2.6")
    assert limit == 65536
    assert target["max_tokens"] == 65536


def test_helper_leaves_the_default_alone_without_entry(isolated_models):
    """Non-régression : sans entrée, le défaut officiel 32000 reste 32000."""
    isolated_models({})
    target = {}
    assert _set_output_token_limit(target, {}, "kimi-k2.6") == 32000
    assert target["max_tokens"] == 32000


def test_helper_respects_a_client_asking_less(isolated_models):
    """Borner n'est pas imposer : un client volontairement plus court est respecté."""
    isolated_models({"kimi-k2.6": {"output_limit": 65536}})
    target = {}
    assert _set_output_token_limit(target, {"max_tokens": 4096}, "kimi-k2.6") == 4096
    assert target["max_tokens"] == 4096


def test_clamp_applies_before_the_max_completion_branch(isolated_models):
    """Le clamp est posé AVANT la branche ``max_completion_tokens`` : vers un
    modèle qui exige la forme moderne, c'est bien la valeur **bornée** qui
    est écrite, pas la valeur brute."""
    isolated_models({"gpt-5.6-luna": {"output_limit": 128000}})
    target = {}
    _set_output_token_limit(target, {"max_tokens": 200000}, "gpt-5.6-luna")
    assert target.get("max_completion_tokens") == 128000
    assert "max_tokens" not in target


def test_clamp_applies_on_the_anthropic_target_too(isolated_models):
    """Anthropic → Anthropic (protocole cible anthropic) : même clamp, même
    champ unique."""
    isolated_models({"kimi-k2.6": {"output_limit": 65536}})
    target = {}
    _set_output_token_limit(
        target, {"max_tokens": 200000}, "kimi-k2.6", target_protocol="anthropic"
    )
    assert target.get("max_tokens") == 65536
    assert "max_completion_tokens" not in target


# ─────────────────── bout en bout : `anthropic_to_openai` ───────────────────


def test_end_to_end_clips_the_upstream_body(isolated_models):
    """200 000 tokens de sortie demandés à un modèle qui plafonne à 131 072 :
    le corps envoyé à l'upstream porte 131 072."""
    isolated_models({"glm-5.1": {"output_limit": 131072}})
    out = anthropic_to_openai(
        {"model": "x", "max_tokens": 200000, "messages": [{"role": "user", "content": "hi"}]},
        "glm-5.1",
    )
    assert out.get("max_tokens") == 131072


def test_end_to_end_keeps_the_default_for_unlisted_models(isolated_models):
    """Un modèle hors table : défaut officiel 32000 en `max_tokens` (mesuré 1.19.0)."""
    isolated_models({"glm-5.1": {"output_limit": 131072}})
    out = anthropic_to_openai(
        {"model": "x", "messages": [{"role": "user", "content": "hi"}]},
        "minimax-m2.5",
    )
    assert out.get("max_tokens") == 32000


def test_end_to_end_a_short_request_is_not_inflated(isolated_models):
    """Le clamp ne fait que descendre : il ne monte jamais une limite courte."""
    isolated_models({"grok-4.7": {"output_limit": 500000}})
    out = anthropic_to_openai(
        {"model": "x", "max_tokens": 512, "messages": [{"role": "user", "content": "hi"}]},
        "grok-4.7",
    )
    assert out.get("max_tokens") == 512


# ─────────────── garde structurelle sur la config réelle ───────────────


def test_real_config_table_is_well_formed():
    """Hygiène de ``config.yaml`` (table réelle, relevé du 2026-10-09).

    Ne teste aucune valeur précise — le catalogue bouge — mais que chaque
    entrée porte un entier strictement positif lisible par
    ``config.limits``. Un ``output_limit`` non entier ne borne rien
    silencieusement : ce test le dit.
    """
    models = cfg._yaml_data.get("models")
    assert isinstance(models, dict) and models, "section models: absente ou vide"
    with_limit = 0
    for mid, entry in models.items():
        assert isinstance(entry, dict), f"models.{mid} n'est pas un mapping"
        if cl.LIMIT_KEY not in entry:
            continue
        with_limit += 1
        assert cl.output_limit(mid) is not None, f"models.{mid}.{cl.LIMIT_KEY} illisible"
        assert cl.output_limit(mid) > 0, f"models.{mid}.{cl.LIMIT_KEY} <= 0"
    assert with_limit, "aucune limite renseignée — le correctif E2 est désarmé"


def test_real_config_clamp_is_never_an_increase():
    """Filet de sécurité global : quelle que soit la table du jour, le clamp
    ne peut que descendre (ou laisser) une limite, jamais l'élever."""
    models = cfg._yaml_data.get("models") or {}
    for mid in models:
        cap = cl.output_limit(mid)
        if cap is None:
            continue
        for asked in (1, 4096, 16384, 200000):
            clamped = cl.clamp_output_token_limit(asked, mid)
            assert clamped <= asked, f"{mid}: {asked} → {clamped}"
            assert clamped <= cap, f"{mid}: {asked} → {clamped} > limite {cap}"
