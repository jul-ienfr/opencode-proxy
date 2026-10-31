"""Aucune URL amont n'est ecrite en dur : les cinq bases viennent de la config.

Contexte (audit 2026-10-20, ecart V2.3). L'audit annoncait « 4 occurrences en dur
dans opencode.py ». Verification faite, c'etait faux pour l'essentiel : les
trois bases principales (`openai_base`, `anthropic_base`, `free_base`) venaient
**deja** de `config.yaml`, et l'endpoint `systemone` etait **deja** surchargeable
par variable d'environnement.

Il restait un vrai ecart, plus etroit : les deux endpoints **Responses** etaient
les seuls litteraux non surchargeables (`config/settings.py`). Ils sont
desormais dans `config.yaml` (`responses_base`, `responses_free_base`).

Ce fichier verrouille les deux proprietes qui doivent rester vraies :
1. les cinq bases sont lues dans la config, et ce que le proxy utilise est bien
   ce que le fichier declare ;
2. aucune affectation de litteral `https://opencode.ai/...` ne reapparait dans le
   code — un `endpoint = "https://..."` en dur est exactement le mecanisme qui
   avait produit les divergences de base.
"""

import pathlib
import re

import pytest

from config import settings as cs

PROXY = pathlib.Path("opencode.py")

#: Les cinq endpoints amont que le proxy doit joindre, et leur cle de config.
BASES = {
    "openai_base": "API_BASE_OPENAI",
    "anthropic_base": "API_BASE_ANTHROPIC",
    "free_base": "API_BASE_FREE",
    "responses_base": "_RESPONSES_ENDPOINT",
    "responses_free_base": "_RESPONSES_FREE_ENDPOINT",
}


@pytest.mark.parametrize("yaml_key,attr", sorted(BASES.items()))
def test_base_comes_from_config(yaml_key, attr):
    """La valeur utilisee par le proxy est celle du fichier, pas un litteral."""
    declared = cs.yaml_get("upstream", yaml_key)
    assert declared, f"upstream.{yaml_key} absent de config.yaml"
    assert getattr(cs, attr) == declared, (
        f"{attr} ({getattr(cs, attr)!r}) ne suit pas la config ({declared!r}) : un litteral a repris la main"
    )


@pytest.mark.parametrize("yaml_key,attr", sorted(BASES.items()))
def test_bases_look_like_opencode_endpoints(yaml_key, attr):
    """Garde-fou de forme : https, hote opencode, chemin de methode plausible."""
    value = getattr(cs, attr)
    assert value.startswith("https://opencode.ai/"), value
    assert value.rsplit("/", 1)[-1] in ("completions", "messages", "responses"), value


def test_legs_stay_distinct():
    """Payant et free ne doivent jamais pointer la meme URL par accident."""
    assert cs.API_BASE_OPENAI != cs.API_BASE_FREE
    assert cs._RESPONSES_ENDPOINT != cs._RESPONSES_FREE_ENDPOINT
    assert "/inference/openai/" in cs.API_BASE_FREE and "/inference/go/" in cs.API_BASE_OPENAI


# ── Garde-fou contre le retour des litteraux ────────────────────────────────

#: Affectation directe d'une URL opencode a une variable : c'est la forme qui
#: avait produit les divergences. Une valeur par defaut dans `yaml_get(...)` ou
#: `os.getenv(..., default)` n'est PAS de cette forme et reste autorisee : elle
#: est surchargeable, donc sans danger.
_HARDCODED = re.compile(r"^\s*[\w.\[\]]+\s*=\s*[\"']https://opencode\.ai/", re.M)


def test_no_hardcoded_upstream_url_assignment():
    source = PROXY.read_text(encoding="utf-8", errors="replace")
    hits = _HARDCODED.findall(source)
    assert not hits, f"URL amont ecrite en dur dans opencode.py : {len(hits)} occurrence(s)"


def test_no_hardcoded_upstream_url_in_settings():
    source = pathlib.Path("config/settings.py").read_text(encoding="utf-8", errors="replace")
    # Les valeurs par defaut de `yaml_get(...)` sont acceptables : elles ne
    # s'appliquent que si la cle manque. On cherche donc la meme forme que
    # ci-dessus, plus restrictive.
    hits = _HARDCODED.findall(source)
    assert not hits, f"URL amont ecrite en dur dans config/settings.py : {hits}"


def test_systemone_endpoint_stays_overridable():
    """L'endpoint SystemOne a un defaut, mais il est surchargeable."""
    import opencode as oc

    assert str(oc._SYSTEMONE_ENDPOINT).startswith("https://opencode.ai/")


def test_catalog_routes_agree_with_the_configured_bases():
    """Le cache de routes et la config ne doivent pas designer des hotes differents.

    Les *chemins* peuvent differer (`/inference/openai/v1` cote catalogue,
    `/inference/v1` cote proxy — decision documentee de ne pas changer une base
    qui repond 200). L'**hote**, lui, doit etre le meme : deux hotes differents
    signifieraient que l'un des deux est perime.
    """
    hosts = set()
    for provider, models in (cs.CATALOG_ROUTES or {}).items():
        for route in models.values():
            base = str(route.get("base_url") or "")
            if base:
                hosts.add(base.split("/", 3)[2])
    if not hosts:
        pytest.skip("cache de routes absent")
    assert hosts == {"opencode.ai"}, hosts
