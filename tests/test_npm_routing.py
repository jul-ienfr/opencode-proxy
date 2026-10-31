"""[PLAN N3] Matrice `npm` → endpoint, et périmètre de fourniture.

**Le module.** `config/models_catalog.py` décide de ce que le proxy fournit, et
dit pourquoi il refuse le reste. Deux règles, toutes deux lues sur le catalogue
live de l'éditeur :

1. le ``npm`` est une clé de **niveau provider** (`provider.npm`), **surchargeable
   au niveau modèle** — et il l'est massivement : 66 des 128 modèles free portent
   leur propre `provider.npm` (mesure du 2026-10-20). La résolution effective se
   fait donc **par modèle** (`model_npm`, `model_api`,
   `api_family_for_model`) ; `provider_npm` reste l'accesseur bas niveau du
   provider seul ;
2. le proxy ne fournit **que** ce que sert un provider opencode, plus l'exception
   interne `jev-1.13*` (`is_servable`, `refusal_reason`).

**Les pièges que ces tests verrouillent.**

- *Le repli implicite mentait.* Pour un id non déclaré, `get_model_config()`
  retombe sur ``{endpoint: API_BASE_OPENAI}`` — la jambe **payée** — et l'amont
  répond 404 sans dire pourquoi. Le refus doit **nommer** l'id, les hôtes tiers
  et le ``npm`` de chacun : c'est ce ``npm`` qui aurait dû choisir l'endpoint.
- *Un ``npm`` hors table n'est pas une erreur à masquer.* Il se dit tel quel
  (« outside the known table: no endpoint can be derived »), jamais deviné en
  ``/chat/completions``.
- *La liste d'hôtes doit rester lisible.* Bornée à `MAX_NAMED_HOSTS`, sinon elle
  noie l'information utile sous le bruit.
- *L'ordre d'insertion du JSON amont n'est pas un contrat.* `hosts_of()` trie :
  deux relevés du même catalogue doivent rendre la même liste.
- *Le périmètre est une partition.* Un id est servi **ou** refusé, jamais les
  deux, jamais aucun des deux.

Aucun réseau ici : les cas que le catalogue live n'expose pas (provider sans
``npm``, plus d'hôtes que la borne, entrée non-dict) sont construits synthétiques.
"""

from __future__ import annotations

from config import models_catalog as mc
from config import settings as cs
from core import errors as ce
from core.errors import ModelOutOfScope

# ─────────── Catalogue synthétique (aucun réseau) ───────────────────────────


def _cat(**providers) -> dict:
    """`{provider: {"npm": ..., "models": {id: {}}}}`, à l'ordre près."""
    return dict(providers)


def _leg(models: tuple[str, ...] = ("m-paye",), *, npm: str = "@ai-sdk/openai-compatible") -> dict:
    return {"npm": npm, "models": {model_id: {} for model_id in models}}


# Nos deux jambes, telles que le live les déclare : même `npm`, ids distincts.
_LEGS = {
    mc.PAID_PROVIDER: _leg(("m-paye",)),
    mc.FREE_PROVIDER: _leg(("m-anonyme",)),
}


def _tiers(npm: str | None, *model_ids: str) -> dict:
    """Une entrée de provider TIERS. ``npm=None`` = provider qui n'en déclare pas."""
    entry: dict = {"models": {model_id: {} for model_id in model_ids}}
    if npm is not None:
        entry["npm"] = npm
    return entry


# ─────────── La matrice `npm` → famille d'API ───────────────────────────────


def test_matrice_npm_connue_et_rien_dautre():
    """Les valeurs connues, et **rien** d'autre : le tableau est fermé.

    Un ``npm`` absent de `NPM_TO_API` ne rend pas une famille par défaut — il
    rend ``None``, ce qui force l'appelant à refuser au lieu de deviner.

    [CORRECTION 2026-10-20] Ce test exigeait « trois valeurs, et rien d'autre ».
    C'était l'expression de la croyance fausse selon laquelle seuls trois
    packages existaient. Le catalogue live en sert **cinq** : `@ai-sdk/google`
    (9 modèles free + 1 payant) et `@ai-sdk/mistral` (1) étaient absents de la
    table, donc traités comme inconnus — d'où 10 modèles sans route dérivable.
    """
    assert mc.NPM_TO_API == {
        "@ai-sdk/anthropic": "messages",
        "@ai-sdk/openai": "responses",
        "@ai-sdk/openai-compatible": "chat",
        "@ai-sdk/google": "gemini",
        "@ai-sdk/mistral": "chat",
    }
    for npm, famille in mc.NPM_TO_API.items():
        assert mc.api_family_for_npm(npm) == famille, npm
        assert mc.api_family_for_npm(f"  {npm}  ") == famille, "espaces non ignorés"
    # Toute famille déclarée doit être connue du routeur.
    assert set(mc.NPM_TO_API.values()) <= set(mc.KNOWN_API_FAMILIES)


def test_npm_hors_table_rend_none_pas_de_deviner():
    """Un ``npm`` réellement inconnu rend ``None`` : on ne devine pas.

    Inventer ``chat`` ici reproduirait exactement le défaut que la matrice existe
    pour éviter — viser ``/chat/completions`` vers un endpoint inconnu.

    [CORRECTION 2026-10-20] Ce test prenait `@ai-sdk/google` et
    `@ai-sdk/mistral` pour des inconnus. Ils sont au catalogue live et
    désormais dans la table ; le témoin d'inconnu doit être un package qui
    n'existe réellement pas chez l'éditeur.
    """
    for npm in ("@aihubmix/ai-sdk-provider", "@ai-sdk/inconnu", "openai", ""):
        assert mc.api_family_for_npm(npm) is None, npm
    assert mc.api_family_for_npm(None) is None


def test_npm_est_lu_au_niveau_provider_puis_surcharge_par_le_modele():
    """Deux accesseurs, deux niveaux — et c'est voulu.

    `provider_npm` lit le ``npm`` du **provider** et rien d'autre : c'est un
    accesseur bas niveau, et il doit rester insensible à un leurre posé sur un
    modèle. La résolution *effective*, elle, passe par `model_npm`, qui applique
    la règle officielle : surcharge du modèle d'abord, provider ensuite.

    [CORRECTION 2026-10-20] Ce test affirmait que « le catalogue live n'en porte
    aucun au niveau modèle ». C'est faux : 66 des 128 modèles free portent leur
    propre `provider.npm`. Le leurre de ce test est donc, dans la réalité, le
    cas **courant** — et c'est `model_npm` qui doit le respecter.
    """
    catalog = _cat(
        tiers=_tiers("@ai-sdk/anthropic", "m"),
        bruit=_tiers(None, "m2"),
    )
    catalog["tiers"]["models"]["m"]["npm"] = "@ai-sdk/google"  # leurre plate
    catalog["tiers"]["models"]["m"]["provider"] = {"npm": "@ai-sdk/google"}  # vrai emplacement
    # Bas niveau : toujours le provider.
    assert mc.provider_npm(catalog, "tiers") == "@ai-sdk/anthropic"
    assert mc.provider_npm(catalog, "bruit") is None
    # Résolution effective : la surcharge de modèle gagne.
    assert mc.model_npm(catalog, "tiers", "m") == "@ai-sdk/google"
    assert mc.model_npm(catalog, "tiers", "absent") == "@ai-sdk/anthropic"
    assert mc.api_family_for_model(catalog, "tiers", "m") == "gemini"


def test_provider_npm_sans_declaration_rend_none():
    """Un provider sans ``npm`` (ou inconnu) ne permet aucune déduction.

    ``None`` plutôt qu'une chaîne vide : une chaîne vide passerait pour une
    valeur et ferait chercher ``NPM_TO_API[""]``.
    """
    catalog = _cat(muet=_tiers(None, "m"), vide=_tiers("", "m"))
    assert mc.provider_npm(catalog, "muet") is None
    assert mc.provider_npm(catalog, "vide") is None
    assert mc.provider_npm(catalog, "inexistant") is None
    assert mc.provider_npm(catalog, None) is None
    assert mc.provider_npm({}, "muet") is None


# ─────────── Le périmètre : ce qui est servi, et rien d'autre ───────────────


def test_aucun_refus_pour_ce_que_nous_servons():
    """Chaque jambe opencode est servie, y compris quand un tiers héberge l'id.

    Le point clé : un id servi par opencode **et** par un provider tiers reste
    servi — la route opencode gagne, le tiers n'est pas une raison de refuser.
    """
    catalog = _cat(**_LEGS, tiers=_tiers("@ai-sdk/google", "m-paye"))
    for model_id in ("m-paye", "m-anonyme"):
        assert mc.refusal_reason(model_id, catalog) is None, model_id
        assert mc.is_servable(model_id, catalog), model_id
    # Les deux jambes se recouvrent : l'ordre de préférence ne change pas le sort.
    deux = _cat(**_LEGS)
    deux[mc.FREE_PROVIDER]["models"]["m-paye"] = {}
    assert mc.refusal_reason("m-paye", deux) is None


def test_exception_interne_est_bien_la_paire_jev():
    """``jev-1.13`` / ``jev-1.13-free`` sont servis sur l'infra opencode
    (``/zen/v1/systemone``) mais absents du catalogue public : ni morts, ni
    retirés, ni refusés. L'exception est **nommée** — un élargissement doit être
    une décision explicite, pas un ajout silencieux.
    """
    assert mc.INTERNAL_IDS == ("jev-1.13", "jev-1.13-free")
    vide = _cat(**{p: _leg(()) for p in mc.OUR_PROVIDERS})
    for model_id in mc.INTERNAL_IDS:
        assert mc.refusal_reason(model_id, vide) is None, model_id
        assert mc.is_servable(model_id, vide), model_id
        assert model_id in cs.MODELS, f"{model_id} a quitté models:"


def test_parti_servi_ou_refus_jamais_les_deux():
    """Invariant de partition, sur un catalogue mélangé.

    `refusal_reason()` délègue à `is_servable()` au lieu de réimplémenter la
    règle : si les deux divergeaient un jour, un id sortirait servi **et**
    refusé, ou ni l'un ni l'autre.
    """
    catalog = _cat(**_LEGS)
    catalog["tiers-a"] = _tiers("@ai-sdk/anthropic", "m-tier", "m-paye")
    catalog["tiers-b"] = _tiers("@ai-sdk/google", "m-tier")

    candidats = {
        "m-paye",
        "m-anonyme",
        "m-tier",
        "m-inconnu-de-partout",
        *mc.INTERNAL_IDS,
    }
    for model_id in candidats:
        assert (mc.refusal_reason(model_id, catalog) is None) == mc.is_servable(model_id, catalog), model_id
    servis = {m for m in candidats if mc.is_servable(m, catalog)}
    assert servis == {"m-paye", "m-anonyme", *mc.INTERNAL_IDS}


# ─────────── Le message de refus ────────────────────────────────────────────


def test_refus_nomme_lid_les_hotes_et_le_npm_de_chacun():
    """Les formes du message, en un seul catalogue.

    Le ``npm`` de chaque hôte est dans le message parce que c'est lui qui aurait
    dû choisir l'endpoint — y compris, et surtout, quand il est hors table.

    [CORRECTION 2026-10-20] Le témoin « hors table » était `@ai-sdk/google`, qui
    est au catalogue. Remplacé par un package réellement inconnu de la table, et
    complété par un hôte google, désormais reconnu comme famille ``gemini``.

    Trois hôtes exactement : `MAX_NAMED_HOSTS` borne la liste à trois, et le
    quatrième serait tronqué — la borne a son propre test.
    """
    catalog = _cat(
        **_LEGS,
        connu=_tiers("@ai-sdk/anthropic", "m-tier"),
        gemini=_tiers("@ai-sdk/google", "m-tier"),
        hors_table=_tiers("@aihubmix/ai-sdk-provider", "m-tier"),
    )
    reason = mc.refusal_reason("m-tier", catalog)
    assert reason is not None
    assert reason.startswith(
        'model "m-tier" is not provided by this proxy: no opencode provider serves it, it is hosted upstream by '
    )
    assert "connu (npm @ai-sdk/anthropic, api messages)" in reason
    assert "gemini (npm @ai-sdk/google, api gemini)" in reason
    assert ("hors_table (npm @aihubmix/ai-sdk-provider, outside the known table: no endpoint can be derived)") in reason


def test_refus_nomme_un_hote_sans_npm():
    """Un hôte qui ne déclare aucun ``npm`` le dit, au lieu de rendre une chaîne vide."""
    catalog = _cat(**_LEGS, muet=_tiers(None, "m-tier"))
    reason = mc.refusal_reason("m-tier", catalog)
    assert reason is not None
    assert "muet (no npm declared)" in reason


def test_liste_dhotes_bornee_et_taille_annoncee():
    """Au-delà de `MAX_NAMED_HOSTS` hôtes, la liste s'arrête et dit combien
    manquent. Sans la borne, un id hébergé par six providers noie le ``npm``
    hors table — la seule information utile — sous du bruit.
    """
    total = mc.MAX_NAMED_HOSTS + 2
    catalog = _cat(**{p: _leg(()) for p in mc.OUR_PROVIDERS})
    for i in range(total):
        catalog[f"p{i}"] = _tiers("@ai-sdk/anthropic", "m-many")
    reason = mc.refusal_reason("m-many", catalog)
    assert reason is not None
    assert reason.count("api messages") == mc.MAX_NAMED_HOSTS
    assert f"and {total - mc.MAX_NAMED_HOSTS} more" in reason
    nommes = sorted(f"p{i}" for i in range(mc.MAX_NAMED_HOSTS))
    for provider in nommes:  # le tri, pas l'ordre d'insertion
        assert f"{provider} (npm" in reason
    for i in range(mc.MAX_NAMED_HOSTS, total):
        assert f"p{i} (npm" not in reason


def test_id_inconnu_de_tout_le_catalogue():
    """Aucun hôte : le message le dit, plutôt que de pointer une liste vide."""
    catalog = _cat(**_LEGS)
    reason = mc.refusal_reason("pas-un-modele-xyz", catalog)
    assert reason == ('model "pas-un-modele-xyz" is not provided by this proxy: unknown to the opencode catalog.')


def test_hotes_excluent_nos_deux_jambes():
    """Nos jambes ne sont jamais listées comme hôtes **tiers** d'un refus.

    Un refus qui nommerait `opencode` parmi les hôtes serait auto-contredit :
    si opencode héberge l'id, il n'y a pas de refus.
    """
    catalog = _cat(**{p: _leg(("m-anonyme",)) for p in mc.OUR_PROVIDERS})
    catalog["tiers"] = _tiers("@ai-sdk/anthropic", "m-anonyme")
    assert mc.hosts_of("m-anonyme", catalog) == ("tiers",)
    assert mc.refusal_reason("m-anonyme", catalog) is None


def test_hotes_tries_independamment_de_lordre_du_json():
    """Deux relevés du même catalogue rendent la même liste.

    L'ordre des clés du JSON amont n'est pas un contrat : sans tri, un message de
    refus (et un test) dépendrait de l'ordre dans lequel l'éditeur a sérialisé.
    """
    modèles_ordonnés = ("m",)
    ordre_a = _cat(
        zulu=_tiers("@ai-sdk/anthropic", *modèles_ordonnés),
        alpha=_tiers(None, *modèles_ordonnés),
        mike=_tiers("@ai-sdk/google", *modèles_ordonnés),
    )
    ordre_b = _cat(mike=ordre_a["mike"], zulu=ordre_a["zulu"], alpha=ordre_a["alpha"])
    assert mc.hosts_of("m", ordre_a) == ("alpha", "mike", "zulu")
    assert mc.hosts_of("m", ordre_b) == ("alpha", "mike", "zulu")


def test_hotes_tolèrent_une_entree_non_dict():
    """Défensif : une entrée de provider qui n'est pas un dict ne fait pas lever.

    Le catalogue live est bien formé aujourd'hui ; la garde évite qu'une seule
    entrée abîmée casse tout le relevé (et le refus avec).
    """
    catalog = _cat(**_LEGS)
    catalog["cassé"] = None
    catalog["texte"] = "pas un provider"
    catalog["liste"] = []
    catalog["ok"] = _tiers("@ai-sdk/anthropic", "m-tier")
    assert mc.hosts_of("m-tier", catalog) == ("ok",)
    assert mc.refusal_reason("m-tier", catalog) is not None
    assert mc.hosts_of("m-inconnu", catalog) == ()


# ─────────── Le porteur d'erreur ────────────────────────────────────────────


def test_model_out_of_scope_porte_lid_et_la_raison():
    """Le point de couture pour le routeur (N4) : l'exception transporte la
    phrase complète, `str(exc)` la rend telle quelle, et elle se dégrade sans
    raison fournie au lieu de lever une seconde fois.
    """
    catalog = _cat(**_LEGS, tiers=_tiers("@ai-sdk/google", "m-tier"))
    reason = mc.refusal_reason("m-tier", catalog)
    exc = ModelOutOfScope("m-tier", reason)
    assert exc.model_id == "m-tier"
    assert exc.reason == reason
    assert str(exc) == reason
    assert "m-tier" in str(exc)

    sans_raison = ModelOutOfScope("m-tier", "")
    assert sans_raison.model_id == "m-tier"
    assert sans_raison.reason and "m-tier" in sans_raison.reason
    assert str(sans_raison) == sans_raison.reason

    vide = ModelOutOfScope(None, None)
    assert vide.model_id == ""
    assert vide.reason  # jamais une exception sans message
    assert str(vide) == vide.reason


def test_exception_exportee_par_core_errors():
    """`core.errors` est le point d'entrée unique des erreurs du proxy : un type
    qui n'y est pas exporté serait introuvable pour l'hôte `opencode.py`."""
    assert issubclass(ModelOutOfScope, Exception)
    assert "ModelOutOfScope" in ce.__all__
