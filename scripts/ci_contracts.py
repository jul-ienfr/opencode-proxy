#!/usr/bin/env python3
"""Le gate CI des contrats d'alignement — rapide, déterministe, bloquant.

**Pourquoi un script plutôt qu'un `pytest` global.** Le suite complet prend des
minutes et dépend du réseau et d'un daemon Docker : en CI il échoue pour des
raisons qui n'ont rien à voir avec le code, et personne ne le regarde plus. Ce
gate ne contient que les tests qui encodent des **contrats mesurés** sur le
client officiel et la jambe free : si l'un casse, c'est une régression réelle.

**Mesuré** : 330 tests, ~5 s. Assez rapide pour être *required check*.

Usage :
    python scripts/ci_contracts.py            # tout le gate
    python scripts/ci_contracts.py --list     # les fichiers couverts
    python scripts/ci_contracts.py --rest     # le RESTE de la suite (non bloquant)

Le second mode sert au job « suite complète » : il exécute tout sauf le gate,
pour ne pas payer deux fois les mêmes tests.
"""

from __future__ import annotations

import argparse
import subprocess
import sys
import time
import xml.etree.ElementTree as ET
from pathlib import Path

RACINE = Path(__file__).resolve().parent.parent
TESTS = RACINE / "tests"

# Chaque entrée est un contrat, pas un simple fichier de tests.
CONTRATS: dict[str, list[str]] = {
    "contrat de phase 0 (structure)": ["test_phase0_contracts.py"],
    "contrat SSE (streaming)": ["test_sse_contract.py", "test_responses_stream_contract.py"],
    "diffusion des erreurs (forme selon le CLIENT)": [
        "test_error_frame_parity.py",
        "test_classify_upstream_error.py",
        "test_error_class_table.py",
    ],
    "contrat de gate free (stream/read/tools)": [
        "test_gate_stream_contract.py",
        "test_free_gate_factors.py",
    ],
    "parité jambe free ↔ client officiel": ["test_free_leg_protocol_parity.py"],
    "routage du catalogue": ["test_catalog_model_routing.py", "test_catalog_routing_effective.py"],
    "dérive catalogue (orphelins)": ["test_catalog_drift.py"],
    "parité client (corps, outils, usage, retry)": [
        "test_client_parity_align.py",
        "test_official_client_parity.py",
        "test_parity_v2_pure.py",
        "test_opencode_client_parity_helpers.py",
        "test_retry_parity.py",
        "test_tool_order_parity.py",
        "test_usage_parity_anthropic.py",
    ],
    "santé de la jambe free (verdicts)": [
        "test_leg_status.py",
        "test_free_refusal_metric.py",
        "test_canary.py",
        "test_health_status_reasons.py",
    ],
    "état persistant (pauses de clé)": ["test_key_pause_reset.py"],
    "garde de protocole SystemOne": ["test_systemone_protocol_guard.py"],
    "diagnostic de verrou mono-instance": ["test_instance_lock_diagnosis.py"],
    "config : aucune clé inerte": ["test_config_audit.py"],
    "le gate CI ne se vide pas": ["test_ci_contracts_gate.py"],
    "pas d'URL en dur": ["test_no_hardcoded_urls.py"],
}


def _fichiers(noms: list[str]) -> list[Path]:
    return [TESTS / nom for nom in noms if (TESTS / nom).exists()]


def _tous_les_contrats() -> list[Path]:
    vus: list[Path] = []
    for noms in CONTRATS.values():
        for chemin in _fichiers(noms):
            if chemin not in vus:
                vus.append(chemin)
    return sorted(vus)


def _tous_les_tests() -> list[Path]:
    return sorted(TESTS.glob("test_*.py"))


def _lancer(chemins: list[Path], etiquette: str) -> int:
    if not chemins:
        print(f"[{etiquette}] aucun fichier à exécuter", file=sys.stderr)
        return 1
    xml = RACINE / "_research" / f"junit_{etiquette}.xml"
    xml.parent.mkdir(exist_ok=True)
    cmd = [
        sys.executable,
        "-m",
        "pytest",
        *[str(p) for p in chemins],
        "-p",
        "no:cacheprovider",
        "-p",
        "no:warnings",
        "--tb=short",
        "-q",
        f"--junit-xml={xml}",
    ]
    debut = time.perf_counter()
    code = subprocess.call(cmd, cwd=str(RACINE))
    duree = time.perf_counter() - debut
    total = echecs = 0
    try:
        racine = ET.parse(xml).getroot()
        suite = racine if racine.tag == "testsuite" else racine.find("testsuite")
        if suite is not None:
            total = int(suite.get("tests") or 0)
            echecs = int(suite.get("failures") or 0) + int(suite.get("errors") or 0)
            print(f"\n[{etiquette}] {total} tests, {echecs} échec(s), {duree:.1f} s sur {len(chemins)} fichiers")
    except Exception:
        print(f"\n[{etiquette}] code de sortie {code} ({duree:.1f} s)", file=sys.stderr)
    if code != 0:
        print(f"[{etiquette}] ÉCHEC — voir _research/junit_{etiquette}.xml", file=sys.stderr)
    return code


def main(argv: list[str] | None = None) -> int:
    parseur = argparse.ArgumentParser(description="Gate CI des contrats d'alignement")
    parseur.add_argument("--list", action="store_true", help="liste les contrats couverts")
    parseur.add_argument(
        "--rest",
        action="store_true",
        help="exécute le RESTE de la suite (tout sauf les contrats) — job non bloquant",
    )
    args = parseur.parse_args(argv)

    contrats = _tous_les_contrats()
    if args.list:
        for nom, noms in CONTRATS.items():
            manquants = [n for n in noms if not (TESTS / n).exists()]
            marque = f"  ⚠️ manquant : {manquants}" if manquants else ""
            print(f"- {nom}: {', '.join(noms)}{marque}")
        print(f"\n{len(contrats)} fichiers, {sum(len(v) for v in CONTRATS.values())} déclarations")
        return 0

    if args.rest:
        reste = [p for p in _tous_les_tests() if p not in set(contrats)]
        return _lancer(reste, "suite")
    return _lancer(contrats, "contrats")


if __name__ == "__main__":
    raise SystemExit(main())
