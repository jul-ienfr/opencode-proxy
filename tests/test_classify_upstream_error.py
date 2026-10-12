"""[PLAN_AUDIT_CLIENT_OFFICIEL G1] Classement des erreurs amont par le CORPS.

**Le défaut.** Le proxy ne regardait que le statut HTTP pour décider du type
d'erreur Anthropic rendu au client : tout échec amont partait en
``api_error``, le type fourre-tout. Or le client officiel, lui, lit le CORPS
(``packages/opencode/src/provider/error.ts:102-154``) : un overflow de
contexte remonte en ``invalid_request_error`` et le client déclenche alors
**sa** compaction. Chez nous il recevait ``api_error`` — type qu'il ne sait
pas interpréter — et il ne compactait pas : il réenvoyait tel quel, en
boucle, jusqu'à l'échec. Inversement, une surcharge transitoire
(``server_is_overloaded``) qui arrive sur un statut ambigu (400/422) n'était
jamais rejouée.

**La réparation.** Une seule table de motifs (``core/errors.py``), partagée
par les trois besoins qui jusqu'ici dupliquaient chacun leur teste en dur :

* ``classify_upstream_error(status, body)`` → le TYPE de l'enveloppe client
  (le statut n'est JAMAIS réécrit ici : la fenêtre d'auth 401/403 → 503 et la
  parité 429/529 restent la propriété de ``opencode._safe_client_status``) ;
* ``upstream_retry_signal`` / ``_motif_retryable`` → la décision de rejeu,
  limitée aux statuts ambigus pour ne jamais marcher sur le failover de clé ;
* ``is_credit_exhaustion`` → la pause longue de clé, qui remplace les huit
  ``any(x in resp.text for x in ("Insufficient balance", "Monthly usage
  limit"))`` dispersés (les motifs officiels ``insufficient_quota`` /
  ``usage_not_included`` entrent dans la même famille).

Ces tests couvrent la table, ses frontières de mot, son immunité aux entrées
réseau dégradées, et les gardes anti-régression des sites d'appel.
"""

import json

import pytest

import opencode as oc
from core.errors import (
    classify_upstream_error,
    free_refusal_response,
    is_credit_exhaustion,
    upstream_retry_signal,
)

# ─────────── La table : chaque motif amont → son type d'erreur ───────────


@pytest.mark.parametrize(
    "motif,etype",
    [
        ("context_length_exceeded", "invalid_request_error"),
        ("insufficient_quota", "permission_error"),
        ("usage_not_included", "permission_error"),
        ("insufficient balance", "permission_error"),
        ("monthly usage limit", "permission_error"),
        ("invalid_prompt", "invalid_request_error"),
        ("server_is_overloaded", "overloaded_error"),
        ("server_error", "api_error"),
    ],
)
def test_chaque_motif_donne_son_type(motif, etype):
    """Le cœur de G1 : le type rendu au client suit le CORPS amont, pas seul
    le statut. Le statut, lui, est conservé tel quel (les règles de fenêtre
    d'auth ne vivent pas ici)."""
    assert classify_upstream_error(400, f'{{"error":{{"message":"{motif}"}}}}') == (400, etype)


def test_overflow_remonte_en_invalid_request_error():
    """LA raison d'être de G1. ``invalid_request_error`` est le seul type que
    le client traduit en « ma requête est trop grosse → je compacte ». Un
    ``api_error`` sur ce même corps le laissait réessayer en boucle."""
    # Forme Anthropic (champ `type` du corps)…
    assert classify_upstream_error(
        400, '{"type":"error","error":{"type":"invalid_request_error","message":"context_length_exceeded"}}'
    ) == (400, "invalid_request_error")
    # …forme OpenAI-compatible (champ `code`)…
    assert classify_upstream_error(400, '{"error":{"code":"context_length_exceeded"}}') == (
        400,
        "invalid_request_error",
    )
    # …et message libre, sans JSON.
    assert classify_upstream_error(400, "prompt is too long: context_length_exceeded") == (
        400,
        "invalid_request_error",
    )


def test_surcharge_transitoire_est_marquee_rejouable():
    """Parité officielle : ``server_is_overloaded`` est transitoire, donc
    rejouable — y compris quand l'amont le rend sur un statut qu'on ne
    rejouerait pas autrement. ``server_error`` générique reste hors table
    (trop large : il matcherait « not_server_error »-style sans frontières
    sûres) → pas de signal."""
    assert upstream_retry_signal(422, "server_is_overloaded") == (True, False)
    assert upstream_retry_signal(422, "server_error") == (False, False)


def test_credit_epuise_ne_se_rejoue_pas_mais_pause_la_cle():
    """Un quota épuisé ne revient pas tout seul : rejouer consume des tentatives
    pour rien, en revanche il faut sortir la clé du pool (pause longue)."""
    for body in ("insufficient_quota", "usage_not_included", "Insufficient balance", "Monthly usage limit"):
        assert upstream_retry_signal(400, body) == (False, True), body
        assert is_credit_exhaustion(body)


def test_overflow_et_prompt_invalide_ne_pausent_jamais_la_cle():
    """Déviation volontaire et documentée : ``invalid_prompt`` et
    ``context_length_exceeded`` ne déclenchent PAS de pause de clé. Punir
    toutes les clés parce qu'une conversation est trop longue ou qu'un prompt
    est refusé provoquerait une panne auto-infligée — ce que l'instruction
    « sans rien casser » interdit."""
    for body in ("context_length_exceeded", "invalid_prompt"):
        assert upstream_retry_signal(400, body) == (False, False), body
        assert not is_credit_exhaustion(body)


def test_corps_sans_motif_reste_en_api_error():
    """Le comportement d'avant, intact : un corps que la table ne connaît pas
    continue de sortir en ``api_error``. G1 n'invente pas de type nouveau."""
    for body in ("", "boom", "rate limited", "internal server error", "service unavailable"):
        assert classify_upstream_error(503, body) == (503, "api_error"), body


def test_premier_motif_trouve_gagne():
    """La table est ordonnée du plus spécifique au plus général : un corps qui
    porterait les deux motifs rend le crédit (épuisé → pas de rejeu), pas la
    surcharge (transitoire → rejeu). Sans cet ordre, une clé sans crédit serait
    rejouée jusqu'à épuisement du budget de tentatives."""
    both = "insufficient_quota while retrying server_error"
    assert classify_upstream_error(400, both) == (400, "permission_error")
    assert upstream_retry_signal(400, both) == (False, True)
    # Et la surcharge seule, devant `server_error`, garde son type dédié.
    assert classify_upstream_error(502, "server_is_overloaded server_error") == (
        502,
        "overloaded_error",
    )


# ─────────── Frontières de mot : pas de faux positifs ───────────


@pytest.mark.parametrize(
    "body",
    [
        "server_errors_total=3",  # nom de métrique
        "not_server_error",  # négation
        "xserver_errorx",  # pas de séparateur
        "my_server_error_2",  # underscore collé → pas de frontière
        "internal server error",  # forme espacée, hors table (volontaire)
        "ServerError",  # camelCase sans underscore → hors table
    ],
)
def test_motifs_delimites_pas_de_faux_positif(body):
    """Les motifs sont compilés ``\\b…\\b`` : ``server_error`` ne doit pas
    matcher ``server_errors_total`` ni ``not_server_error``. Sans les
    frontières, une réponse porteuse d'une métrique Prometheus se ferait
    passer pour une surcharge et serait rejouée pour rien."""
    assert classify_upstream_error(500, body) == (500, "api_error"), body
    assert upstream_retry_signal(500, body) == (False, False), body
    assert not is_credit_exhaustion(body), body


def test_motifs_insensibles_a_la_casse():
    """L'amont n'a pas de convention de casse stable : la table doit reconnaître
    les deux formes (« INSUFFICIENT_QUOTA », « Insufficient Balance »)."""
    assert is_credit_exhaustion("INSUFFICIENT_QUOTA")
    assert is_credit_exhaustion("Insufficient Balance")
    # Les deux familles coexistent dans la table : le motif officiel OpenAI
    # (`insufficient_quota`, underscore) et le texte historique du proxy
    # (« Insufficient balance », espace). Chacun sous sa forme exacte.
    assert is_credit_exhaustion("insufficient_quota")
    assert is_credit_exhaustion("Monthly Usage Limit")
    assert classify_upstream_error(400, "SERVER_IS_OVERLOADED") == (400, "overloaded_error")


def test_motif_dans_du_json_reel():
    """Forme réellement renvoyée par un gateway OpenAI-compatible."""
    body = json.dumps(
        {"error": {"message": "You exceeded your current quota", "type": "insufficient_quota", "code": None}}
    )
    assert classify_upstream_error(429, body) == (429, "permission_error")
    assert is_credit_exhaustion(body)


# ─────────── Entrées dégradées : jamais de raise ───────────


@pytest.mark.parametrize(
    "bad",
    [None, "", 0, b"insufficient_quota", {"insufficient_quota": True}, ["insufficient_quota"], object(), 3.5, True],
)
def test_corps_degrade_ne_leve_jamais(bad):
    """Corps lu sur le réseau : n'importe quoi peut arriver (bytes, dict,
    None). Aucun helper ne doit lever — sinon la pompe d'erreur elle-même
    deviendrait une 500."""
    for fn in (
        classify_upstream_error,
        upstream_retry_signal,
        is_credit_exhaustion,
    ):
        if fn is is_credit_exhaustion:
            assert fn(bad) is False, fn.__name__
        else:
            assert isinstance(fn(400, bad), tuple), fn.__name__


@pytest.mark.parametrize(
    "bad_status,expected_status",
    [(None, 502), ("400", 400), (400, 400), (400.0, 400), ("abc", 502), (True, 1), (0, 0)],
)
def test_statut_degrade_est_normalise_pas_leve(bad_status, expected_status):
    """``classify_upstream_error`` ne réécrit jamais le statut, mais un statut
    illisible retombe sur 502 (Passerelle) au lieu de faire exploser le
    handler. ``True`` est un ``int`` en Python : ``int(True)`` vaut 1,conservé
    tel quel — le helper ne devine pas, il normalise."""
    st, etype = classify_upstream_error(bad_status, "insufficient_quota")
    assert st == expected_status
    assert etype == "permission_error"


def test_statut_illisible_desactive_le_signal_de_rejeu():
    """Sans statut exploitable, pas de rejeu : mieux vaut relayer l'erreur que
    boucler sur une réponse qu'on ne comprend pas."""
    assert upstream_retry_signal(None, "server_is_overloaded") == (False, False)
    assert upstream_retry_signal("x", "server_is_overloaded") == (False, False)


# ─────────── Le helper hôte : quels statuts peuvent devenir rejouables ───────────


@pytest.mark.parametrize("status", [400, 404, 408, 409, 422, 425])
def test_statut_ambigu_minuscule(status):
    """Les 6 statuts ambigus : hors ``_RETRYABLE_STATUSES`` mais dont le corps
    peut déclarer une surcharge (certains gateways rendent un 400/422 sur une
    charge). 200 n'y figure pas — un succès n'a pas d'erreur à rejouer."""
    assert oc._motif_retryable(status, "server_is_overloaded") is True, status


@pytest.mark.parametrize("status", [401, 403, 429, 499, 500, 502, 503, 504, 529])
def test_statuts_a_traitement_propre_jamais_rejoues_par_motif(status):
    """EXCLUSIONS — le cœur de la sécurité de G1. 429 (quota) et 499
    (déconnexion) ont leur propre traitement, 401/403 ouvrent la fenêtre
    d'auth qui déclenche le failover de clé. Si le motif pouvait rejouer ces
    statuts, il doublerait ces mécanismes et les contrecarrerait. Les 5xx sont
    déjà dans ``_RETRYABLE_STATUSES`` (chemin historique, inchangé)."""
    assert oc._motif_retryable(status, "server_is_overloaded") is False, status


def test_motif_retryable_exige_un_motif_rejouable():
    """Un statut ambigu SANS motif de surcharge ne se rejoue pas : sinon on
    rejouerait toutes les 400, y compris les vraies requêtes invalides."""
    assert oc._motif_retryable(400, "bad request") is False
    assert oc._motif_retryable(400, "context_length_exceeded") is False
    assert oc._motif_retryable(422, "insufficient_quota") is False


def test_motif_retryable_ne_leve_jamais():
    for bad in (None, "x", object()):
        assert oc._motif_retryable(bad, "server_is_overloaded") is False
    assert oc._motif_retryable(400, None) is False


def test_liste_ambigu_exclut_bien_les_statuts_proteges():
    """Garde explicite sur la constante : si quelqu'un ajoute 401/403/429/499
    à ``_MOTIF_RETRY_STATUSES``, ce test casse — la liste ne doit pas dériver."""
    proteges = {401, 403, 429, 499}
    assert not (proteges & oc._MOTIF_RETRY_STATUSES)
    assert oc._MOTIF_RETRY_STATUSES == {400, 404, 408, 409, 422, 425}

# ─────────── Intégration : le refus libre est classé ───────────


class _Redact:
    def __call__(self, text, limit):
        return str(text or "")[:limit]


@pytest.mark.parametrize("proto", ["anthropic", "openai"])
def test_refus_libre_non_429_est_classe(proto):
    """Le refus libre non-429 portait déjà le corps amont borné dans son
    message : il passe maintenant par la table. Un 503 dû à un overflow doit
    sortir en ``invalid_request_error`` pour que le client compacte — pas en
    ``api_error``."""
    exc = oc.FreeRefusal(status=503, body="context_length_exceeded: too many tokens")
    r = free_refusal_response(
        exc, proto, anthropic_error_fn=oc._anthropic_error, openai_error_fn=oc._openai_error, redact_fn=_Redact()
    )
    payload = json.loads(r.body)
    err = payload.get("error", payload)
    assert err["type"] == "invalid_request_error", err
    assert r.status_code == 503


def test_refus_libre_429_reste_rate_limit():
    """Le 429 conserve son type dédié : G1 ne doit rien changer au chemin
    quota, qui est déjà correct et testé par Lot A."""
    exc = oc.FreeRefusal(status=429, body="insufficient_quota", retry_after="30")
    r = free_refusal_response(
        exc, "anthropic", anthropic_error_fn=oc._anthropic_error, openai_error_fn=oc._openai_error, redact_fn=_Redact()
    )
    payload = json.loads(r.body)
    assert payload["error"]["type"] == "rate_limit_error"
    assert r.status_code == 429
    assert r.headers.get("Retry-After") == "30"


def test_refus_libre_sans_motif_reste_api_error():
    """Le cas le plus courant (503 réseau) garde exactement le type d'avant."""
    exc = oc.FreeRefusal(status=503, body="Service Unavailable upstream")
    r = free_refusal_response(
        exc, "anthropic", anthropic_error_fn=oc._anthropic_error, openai_error_fn=oc._openai_error, redact_fn=_Redact()
    )
    assert json.loads(r.body)["error"]["type"] == "api_error"


# ─────────── Gardes anti-révert partiel (les sites d'appel) ───────────


def test_les_quatre_sites_derreur_anthropic_sont_classes():
    """Les 4 ``except UpstreamError`` des relais payant/compacté rendaient
    ``_anthropic_error(e.status_code, str(e))`` en ``api_error`` fourre-tout.
    Un seul site laissé nu suffirait à faire boucler le client sur un overflow
    dans cette jambe-là."""
    with open(oc.__file__, encoding="utf-8") as fh:
        body = fh.read()
    nus = body.count("return _anthropic_error(e.status_code, str(e))")
    classes = body.count("_classify_upstream_error(e.status_code, str(e))")
    assert nus == 0, f"{nus} site(s) d'erreur encore non classé(s)"
    assert classes >= 4, f"seulement {classes} site(s) d'erreur classé(s), 4 attendus"


def test_aucun_test_de_credit_en_dur_ne_survit():
    """Les huit ``any(x in resp.text for x in ("Insufficient balance",
    "Monthly usage limit"))`` sont remplacés par ``_is_credit_exhaustion``. S'il
    en restait un, la famille de motifs divergerait d'un site à l'autre (les
    deux motifs officiels actifs sur sept sites seulement)."""
    with open(oc.__file__, encoding="utf-8") as fh:
        body = fh.read()
    assert 'for x in ("Insufficient balance", "Monthly usage limit")' not in body
    assert body.count("_is_credit_exhaustion(") >= 8, "un site de pause crédit a perdu le helper G1"


def test_le_relais_general_json_est_classe():
    """L'enveloppe Anthropic forgée à la main sur le relais d'erreur OpenAI
    (``"type": "api_error"`` en dur, lignes du message ``HTTP {status}``)
    doit lire le type dans le corps amont."""
    with open(oc.__file__, encoding="utf-8") as fh:
        body = fh.read()
    assert '_classify_upstream_error(resp.status_code, resp.text[:4000])' in body


def test_le_relais_intact_de_compaction_nettoie_pas_le_type():
    """À l'inverse, la branche ``_is_compaction`` relaie le corps amont
    VERBATIME (contrat de la Phase 2). Ce test la fixe pour éviter qu'une
    « unification » future ne la reclasse par erreur."""
    with open(oc.__file__, encoding="utf-8") as fh:
        lines = fh.read().splitlines()
    idx = [i for i, ln in enumerate(lines) if "if _is_compaction:" in ln]
    assert idx, "branche _is_compaction introuvable"
    for i in idx:
        fenetre = "\n".join(lines[i : i + 40])
        assert "_classify_upstream_error" not in fenetre, (
            f"la branche _is_compaction (ligne {i + 1}) doit relaier intacte, sans reclassement"
        )
