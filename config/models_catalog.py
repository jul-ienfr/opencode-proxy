"""config/models_catalog.py — hygiène du catalogue modèles (PLAN H1, lot V6).

**Le défaut.** Rien ne compare la table statique ``models:`` de ``config.yaml``
au catalogue **live** de l'éditeur. Une entrée peut donc survivre des mois après
que son modèle a disparu du catalogue (``mimo-v2-pro``, ``hy3-preview``,
``omen-alpha``, ``deepseek-flash``…), ou viser une jambe qui ne l'héberge plus
(``glm-5.1`` présent côté ``opencode`` anonyme, routé côté ``opencode-go``
payant). Rien ne le signale : le modèle meurt en amont, au premier appel.

**La réparation.** Un module **pur** qui classe chaque id configuré, et un job
en lecture seule (`scripts/check_models_catalog.py`) qui produit un rapport
horodaté. Le module ne devine aucune règle : il délègue la résolution
d'endpoint à `config.settings._resolve_model_endpoint` — **source unique** — via
un accesseur paresseux, parce qu'importer ``config.settings`` au chargement
déclenche le thread de découverte (`config/discovery.py:start_background_fetch`)
et qu'un module de diagnostic ne doit rien démarrer.

**Le contrat.** AUCUNE écriture : ni ``config.yaml``, ni la base. Le job se
désactive par config (`models_catalog.enabled: false``, le défaut) ; tant qu'il
n'est pas activé à la main, rien ne part sur le réseau.
"""

from __future__ import annotations

import json
from functools import lru_cache
from typing import Any
from urllib.request import Request, urlopen

CATALOG_URL = "https://models.opencode.ai/api.json"
CATALOG_TIMEOUT = 30.0

# Nos deux providers, dans le catalogue live.
PAID_PROVIDER = "opencode-go"    # https://opencode.ai/zen/go/v1  (jambe payée)
FREE_PROVIDER = "opencode"       # https://opencode.ai/inference/v1 (jambe anonyme)
OUR_PROVIDERS = (PAID_PROVIDER, FREE_PROVIDER)

# Exception de périmètre : servis sur l'infra opencode (`/inference/v1/systemone`)
# mais absents du catalogue public — ne jamais les déclarer morts ni les
# retirer. Tout le reste du catalogue (google, mistral, azure, bedrock…) est
# chez un provider TIERS : le proxy ne le fournit pas.
INTERNAL_IDS = ("jev-1.13", "jev-1.13-free")

# `npm` → famille d'API. Le `npm` est une clé de **niveau provider** dans le
# catalogue live (`provider.npm`), et ABSENTE au niveau modèle : aucune entrée
# de `models:` n'en porte. Elle désigne le package ai-sdk que l'éditeur emploie
# pour parler au endpoint du provider — d'où la famille d'API qui en découle.
# Règle officielle : packages/core/src/provider/model.ts:141-162.
NPM_TO_API = {
    "@ai-sdk/anthropic": "messages",
    "@ai-sdk/openai": "responses",
    "@ai-sdk/openai-compatible": "chat",
}

# Un id tiers peut être hébergé par une demi-douzaine de providers (7298 ids du
# catalogue live sont hors de notre périmètre). Le message de refus n'en nomme
# que quelques-uns : au-delà, la liste noie l'information utile — le `npm` hors
# table — sous du bruit.
MAX_NAMED_HOSTS = 3

# Verdicts, du plus grave au plus bénin (l'ordre est aussi celui du rapport).
MORT = "mort"
HORS_JAMBE = "hors-jambe"
DEPRECATED = "deprecated"
INTERNE = "non-catalogue-interne"
OK = "ok"
VERDICTS = (MORT, HORS_JAMBE, DEPRECATED, INTERNE, OK)

# Statut du catalogue qui signale un modèle en fin de vie.
DEPRECATED_STATUS = "deprecated"


# ─────────── Accès paresseux à la config (aucun effet de bord à l'import) ───


@lru_cache(maxsize=1)
def _settings():
    """``config.settings`` importé au PREMIER appel, jamais au chargement.

    ``import config`` → ``config/__init__.py`` → ``.settings`` → lance le thread
    de découverte amont. Un module de diagnostic doit pouvoir être importé (tests
    inclus) sans rien démarrer.
    """
    from config import settings

    return settings


def known_bases() -> frozenset[str]:
    """Les endpoints que le proxy sait résoudre seul (config.yaml `upstream.*`)."""
    s = _settings()
    return frozenset(
        {
            s.API_BASE_OPENAI,
            s.API_BASE_ANTHROPIC,
            s.API_BASE_FREE,
            s._RESPONSES_ENDPOINT,
            s._RESPONSES_FREE_ENDPOINT,
        }
    )


def free_bases() -> frozenset[str]:
    """Les endpoints de la jambe anonyme (les deux variantes chat/responses)."""
    s = _settings()
    return frozenset({s.API_BASE_FREE, s._RESPONSES_FREE_ENDPOINT})


def paid_bases() -> frozenset[str]:
    """Les endpoints de la jambe payée (les trois variantes)."""
    s = _settings()
    return frozenset(
        {s.API_BASE_OPENAI, s.API_BASE_ANTHROPIC, s._RESPONSES_ENDPOINT}
    ) - free_bases()


def resolved_endpoint(model_id: str, cfg: dict | None = None) -> str:
    """Endpoint réellement visé, via la SOURCE UNIQUE du proxy.

    Passe ``{}`` pour un id absent de ``models:`` — exactement ce que
    ``get_model_config()`` reçoit, donc exactement le repli implicite.
    """
    s = _settings()
    cfg = {} if cfg is None else cfg
    protocol = cfg.get("protocol") or "openai"
    return s._resolve_model_endpoint(model_id, cfg, protocol)


def endpoint_leg(endpoint: str) -> str | None:
    """Jambe (``opencode-go`` / ``opencode``) d'une URL d'endpoint, ou ``None``
    si l'URL est interne au proxy (hors des bases connues)."""
    if endpoint in free_bases():
        return FREE_PROVIDER
    if endpoint in paid_bases():
        return PAID_PROVIDER
    return None


# ─────────── Catalogue live ────────────────────────────────────────────────


def fetch_catalog(url: str = CATALOG_URL, timeout: float = CATALOG_TIMEOUT) -> dict:
    """Télécharge le catalogue (lecture seule). Lève en cas d'échec réseau :
    un diagnostic doit échouer bruyamment plutôt que rendre un verdict muet."""
    req = Request(url, headers={"User-Agent": "opencode-proxy-plan/1.0"})
    with urlopen(req, timeout=timeout) as resp:
        payload = json.loads(resp.read().decode("utf-8"))
    if not isinstance(payload, dict):
        raise ValueError(f"catalogue inattendu ({type(payload).__name__})")
    return payload


def legs(catalog: dict) -> dict[str, dict]:
    """`{provider: {model_id: entrée}}` restreint à NOS deux providers."""
    return {
        provider: ((catalog.get(provider) or {}).get("models") or {})
        for provider in OUR_PROVIDERS
    }


# ─────────── Matrice npm → API, et périmètre de fourniture ──────────────────


def serving_provider(model_id: str, catalog: dict) -> str | None:
    """Le provider opencode qui sert l'id, ou ``None``.

    L'ordre compte : un id présent sur les deux jambes (24 d'entre eux) est
    rendu par sa jambe **anonyme**, qui est celle que vise le suffixe ``-free``.
    """
    both = legs(catalog)
    if model_id in both[FREE_PROVIDER]:
        return FREE_PROVIDER
    if model_id in both[PAID_PROVIDER]:
        return PAID_PROVIDER
    return None


def provider_npm(catalog: dict, provider: str | None) -> str | None:
    """Le ``npm`` déclaré par un provider — clé de niveau provider, pas modèle.

    Un provider sans ``npm`` (ou sans ``api``) ne permet aucune déduction : on
    rend ``None`` plutôt qu'une chaîne vide qui passerait pour une valeur.
    """
    if not provider:
        return None
    return str((catalog.get(provider) or {}).get("npm") or "") or None


def api_family_for_npm(npm: str | None) -> str | None:
    """Famille d'API imposée par un ``npm`` **connu** ; ``None`` s'il est inconnu.

    ``None`` signifie « on ne sait pas », et c'est une réponse utile : l'appelant
    doit refuser en nommant l'id et son ``npm``, jamais deviner un endpoint.
    Inventer `/chat/completions` pour un `npm` inconnu est exactement le défaut
    que cette matrice existe pour éviter.
    """
    return NPM_TO_API.get(str(npm or "").strip()) if npm else None


def is_servable(model_id: str, catalog: dict) -> bool:
    """Règle de périmètre : servi par un provider opencode, ou exception interne."""
    return (
        serving_provider(model_id, catalog) is not None
        or model_id in INTERNAL_IDS
    )


def hosts_of(model_id: str, catalog: dict) -> tuple[str, ...]:
    """Les providers **tiers** qui hébergent l'id (nos deux jambes exclues).

    Trié : l'ordre d'insertion du JSON amont n'est pas un contrat, et deux
    relevés du même catalogue doivent rendre la même liste.
    """
    return tuple(
        sorted(
            provider
            for provider, entry in catalog.items()
            if provider not in OUR_PROVIDERS
            and isinstance(entry, dict)
            and model_id in (entry.get("models") or {})
        )
    )


def refusal_reason(model_id: str, catalog: dict) -> str | None:
    """Raison d'un refus de périmètre ; ``None`` si l'id doit être servi.

    C'est l'inverse du repli implicite : celui-ci répondait ``404`` amont après
    avoir visé ``zen/go/v1`` — la jambe payée, donc la mauvaise — sans jamais
    dire pourquoi. Ici le refus **nomme** l'id, les providers qui l'hébergent,
    et le ``npm`` de chacun, parce que c'est ce ``npm`` qui aurait dû choisir
    l'endpoint. Un ``npm`` hors table est dit tel quel, jamais masqué sous un
    ``/chat/completions`` deviné.

    Ne refuse JAMAIS un id qu'un provider opencode sert, ni les deux internes.
    """
    if is_servable(model_id, catalog):
        return None
    hosts = sorted(hosts_of(model_id, catalog))
    if not hosts:
        return (
            f'model "{model_id}" is not provided by this proxy: '
            "unknown to the opencode catalog."
        )
    details = []
    for provider in hosts[:MAX_NAMED_HOSTS]:
        npm = provider_npm(catalog, provider)
        family = api_family_for_npm(npm)
        if not npm:
            details.append(f"{provider} (no npm declared)")
        elif family is None:
            details.append(
                f"{provider} (npm {npm}, outside the known table: "
                "no endpoint can be derived)"
            )
        else:
            details.append(f"{provider} (npm {npm}, api {family})")
    where = ", ".join(details)
    hidden = len(hosts) - len(details)
    if hidden > 0:
        where = f"{where} and {hidden} more"
    return (
        f'model "{model_id}" is not provided by this proxy: no opencode '
        f"provider serves it, it is hosted upstream by {where}."
    )


# ─────────── Classification ─────────────────────────────────────────────────


def is_internal(model_id: str, cfg: dict | None = None) -> bool:
    """Vrai si l'id porte un endpoint interne explicite (ex. ``systemone``).

    Un tel id n'a **aucune** raison d'être dans le catalogue public : il ne doit
    jamais être déclaré mort.
    """
    endpoint = str((cfg or {}).get("endpoint") or "").strip()
    if not endpoint:
        return False
    return resolved_endpoint(model_id, cfg or {}) not in known_bases()


def classify(model_id: str, cfg: dict | None, catalog: dict) -> str:
    """Verdict d'un id configuré, contre les DEUX jambes du catalogue.

    `hors-jambe` compare l'endpoint réellement configuré (donc la jambe que le
    proxy vise) à la jambe qui héberge l'id : une comparaison par seul suffixe
    ``-free`` est aveugle — trois ids de la jambe payée finissent par ``-free``
    (``longcat-2.5-preview-free``, ``space-bunny-free``, ``step-5-preview-free``).
    """
    both = legs(catalog)
    in_paid = model_id in both[PAID_PROVIDER]
    in_free = model_id in both[FREE_PROVIDER]

    if not in_paid and not in_free:
        return INTERNE if is_internal(model_id, cfg) else MORT

    cfg = cfg or {}
    leg = endpoint_leg(resolved_endpoint(model_id, cfg))
    if leg is None:
        return INTERNE  # endpoint interne explicite sur un id aussi au catalogue

    if leg == PAID_PROVIDER and not in_paid:
        return HORS_JAMBE  # routé payé, hébergé anonyme
    if leg == FREE_PROVIDER and not in_free:
        return HORS_JAMBE  # routé anonyme, hébergé payé

    if (both[leg].get(model_id) or {}).get("status") == DEPRECATED_STATUS:
        return DEPRECATED
    return OK


# ─────────── Rapport ───────────────────────────────────────────────────────


def audit(catalog: dict, models: dict[str, Any] | None = None) -> dict:
    """Rapport complet, déterministe (triés verdict puis id).

    ``generated_at``/``source`` sont ajoutés par l'appelant : le module reste
    pur et testable sans horloge.
    """
    if models is None:
        models = _settings().MODELS
    both = legs(catalog)

    configured = []
    for model_id in sorted(models):
        cfg = models[model_id] if isinstance(models[model_id], dict) else {}
        # Les deux statuts, pas un seul : un id peut être `deprecated` côté payé
        # et actif côté anonyme (space-bunny-free). Le verdict porte sur la jambe
        # routée, le rapport montre les deux.
        paid_entry = (both[PAID_PROVIDER].get(model_id) or {}).get("status")
        free_entry = (both[FREE_PROVIDER].get(model_id) or {}).get("status")
        leg = endpoint_leg(resolved_endpoint(model_id, cfg))
        configured.append(
            {
                "id": model_id,
                "verdict": classify(model_id, cfg, catalog),
                "jambe_configuree": leg,
                "present_cote_payant": model_id in both[PAID_PROVIDER],
                "present_cote_anonyme": model_id in both[FREE_PROVIDER],
                "statut_cote_payant": paid_entry,
                "statut_cote_anonyme": free_entry,
                "statut_catalogue": paid_entry if leg == PAID_PROVIDER else free_entry,
            }
        )
    configured.sort(key=lambda row: (VERDICTS.index(row["verdict"]), row["id"]))

    # Ids du catalogue absents de config.yaml : ce que produirait le repli
    # implicite de get_model_config(). Les deux passent par la MÊME source
    # (_resolve_model_endpoint), donc `repli_endpoint` est littéralement ce que
    # rendrait le routeur — cf. tests/test_model_catalog.py § [PLAN N4]. Le repli
    # ne lit que le nom (suffixe `-free`, famille `muse`/`spark`) : pour un id
    # muet il visera le payant, et seule la découverte auto corrigera la jambe.
    missing = []
    for provider in OUR_PROVIDERS:
        for model_id in sorted(both[provider]):
            if model_id in models:
                continue
            missing.append(
                {
                    "id": model_id,
                    "jambe_catalogue": provider,
                    "repli_endpoint": resolved_endpoint(model_id, {}),
                    "repli_jambe": endpoint_leg(resolved_endpoint(model_id, {})),
                    "statut_catalogue": (both[provider].get(model_id) or {}).get("status"),
                }
            )
    missing.sort(key=lambda row: (row["jambe_catalogue"], row["id"]))

    counts: dict[str, int] = dict.fromkeys(VERDICTS, 0)
    for row in configured:
        counts[row["verdict"]] += 1
    counts["configured"] = len(configured)
    counts["missing_from_config"] = len(missing)

    return {
        "catalog": {
            PAID_PROVIDER: len(both[PAID_PROVIDER]),
            FREE_PROVIDER: len(both[FREE_PROVIDER]),
        },
        "counts": counts,
        "configured": configured,
        "missing_from_config": missing,
    }


def format_report(report: dict) -> str:
    """Rapport texte (français), lisible dans un terminal ou un fichier."""
    out: list[str] = []
    counts = report.get("counts", {})
    out.append(
        "Catalogue models — %d entrées configurées (%d côté payant, %d côté anonyme au catalogue)"
        % (
            counts.get("configured", 0),
            report.get("catalog", {}).get(PAID_PROVIDER, 0),
            report.get("catalog", {}).get(FREE_PROVIDER, 0),
        )
    )
    for verdict in VERDICTS:
        rows = [r for r in report.get("configured", []) if r["verdict"] == verdict]
        if not rows:
            continue
        out.append("")
        out.append("%s (%d) —" % (verdict.upper(), len(rows)))
        for row in rows:
            out.append(
                "  %-34s jambe=%s  payé=%s(%s) anonyme=%s(%s)"
                % (
                    row["id"],
                    row["jambe_configuree"] or "interne",
                    "oui" if row["present_cote_payant"] else "non",
                    row["statut_cote_payant"] or "-",
                    "oui" if row["present_cote_anonyme"] else "non",
                    row["statut_cote_anonyme"] or "-",
                )
            )
    missing = report.get("missing_from_config") or []
    if missing:
        out.append("")
        out.append(
            "ABSENTS DE CONFIG (%d) — le repli implicite les routerait côté payant :"
            % len(missing)
        )
        for row in missing:
            out.append(
                "  %-34s catalogue=%s  repli=%s"
                % (row["id"], row["jambe_catalogue"], row["repli_jambe"] or "interne")
            )
    return "\n".join(out)
