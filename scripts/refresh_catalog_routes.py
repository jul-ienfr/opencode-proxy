"""Genere `config/catalog_routes.json` depuis le catalogue public de l'editeur.

    python scripts/refresh_catalog_routes.py            # ecrit le cache
    python scripts/refresh_catalog_routes.py --check    # ne compare que, code retour 1 si derive

**Pourquoi un cache et pas un appel runtime.** Le routage doit repondre en
microsecondes, au demarrage, y compris sans reseau. Le catalogue public est la
source de verite, mais il n'a pas sa place sur le chemin critique : on le lit
hors ligne, on ecrit un fichier plat, le proxy le charge une fois.

**Ce que le fichier contient.** Pour chaque modele de nos deux jambes :
``{family, protocol, base_url}``. Rien de plus — pas de tarifs, pas de
capacites, pas de contexte : ces champs-la viennent deja de la config et n'ont
pas besoin d'etre doubles.

**Derive.** `--check` compare le catalogue live au cache commite et sort en 1
s'il a bouge. C'est le signal qu'il faut relire le diff, pas qu'il faut
l'appliquer : un modele peut changer de famille sans que le proxy doive le
suivre le jour meme.
"""

from __future__ import annotations

import argparse
import json
import pathlib
import sys
from urllib.request import Request, urlopen

CATALOG_URL = "https://models.opencode.ai/api.json"
CACHE = pathlib.Path("config/catalog_routes.json")
PROVIDERS = ("opencode", "opencode-go")

sys.path.insert(0, ".")
from config.models_catalog import FAMILY_TO_PROTOCOL, api_family_for_model, model_api  # noqa: E402


def build(catalog: dict) -> dict:
    """`{provider: {model_id: {family, protocol, base_url}}}`."""
    out: dict = {}
    for provider in PROVIDERS:
        leg = catalog.get(provider) or {}
        models = leg.get("models") or {}
        routes = {}
        for model_id in models:
            family = api_family_for_model(catalog, provider, model_id)
            base = model_api(catalog, provider, model_id)
            if not family or not base:
                continue
            routes[model_id] = {
                "family": family,
                "protocol": FAMILY_TO_PROTOCOL.get(family),
                "base_url": base,
            }
        out[provider] = dict(sorted(routes.items()))
    return {"generated_from": CATALOG_URL, "providers": out}


def fetch() -> dict:
    req = Request(CATALOG_URL, headers={"User-Agent": "opencode-proxy/refresh-catalog"})
    with urlopen(req, timeout=30) as resp:  # noqa: S310 — URL constante, https
        return json.loads(resp.read().decode("utf-8"))


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--check", action="store_true", help="ne rien ecrire, comparer")
    ap.add_argument("--from", dest="source", help="lire un instantane local au lieu du reseau")
    args = ap.parse_args()

    if args.source:
        catalog = json.loads(pathlib.Path(args.source).read_text(encoding="utf-8"))
    else:
        catalog = fetch()

    fresh = build(catalog)
    if args.check:
        if not CACHE.exists():
            print(f"cache absent : {CACHE}")
            return 1
        current = json.loads(CACHE.read_text(encoding="utf-8"))
        if current.get("providers") == fresh["providers"]:
            print("cache a jour")
            return 0
        old = current.get("providers") or {}
        for provider in PROVIDERS:
            a = old.get(provider) or {}
            b = fresh["providers"].get(provider) or {}
            ajoutes = sorted(set(b) - set(a))
            retires = sorted(set(a) - set(b))
            changes = sorted(m for m in set(a) & set(b) if a[m] != b[m])
            for label, ids in (("ajoutes", ajoutes), ("retires", retires), ("changes", changes)):
                if ids:
                    print(f"{provider}: {len(ids)} {label} -> {ids[:5]}")
        return 1

    CACHE.parent.mkdir(parents=True, exist_ok=True)
    CACHE.write_text(json.dumps(fresh, indent=1, ensure_ascii=False) + "\n", encoding="utf-8")
    total = sum(len(v) for v in fresh["providers"].values())
    print(f"{CACHE} ecrit : {total} modeles")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
