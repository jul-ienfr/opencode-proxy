"""[plan v10 §11.5 Lot D] Verrouille le contrat V1 via les golden fixtures.

Chaque fichier de docs/v1-response-golden/*.json = {fn, input, expected}.
Le test rejoue la fonction RÉELLE sur l'input et compare exactement à
`expected`, après normalisation des champs non-déterministes (ids msg_/
toolu_ générés par uuid4). Toute modification de conversion qui change une
sortie = échec → il faut régénérer/réviser la fixture EXPLICITEMENT
(scripts/gen_golden_fixtures.py) : c'est le rôle de verrou du contrat.
"""

import json
import re
from pathlib import Path

import pytest

import protocol_mapping as pm

GOLDEN_DIR = Path(__file__).resolve().parent.parent / "docs" / "v1-response-golden"

_NONDETERMINISTIC_IDS = [
    (re.compile(r"^msg_[0-9a-f]{24}$"), "<msg_id>"),
    (re.compile(r"^toolu_[0-9a-f]{8}$"), "<toolu_id>"),
    (re.compile(r"^chatcmpl-[0-9a-f]{24}$"), "<chatcmpl_id>"),
]


def _normalize(obj):
    if isinstance(obj, dict):
        out = {}
        for k, v in obj.items():
            if k == "created" and isinstance(v, int):
                out[k] = "<epoch>"  # timestamp généré à chaque réponse
            else:
                out[k] = _normalize(v)
        return out
    if isinstance(obj, list):
        return [_normalize(v) for v in obj]
    if isinstance(obj, str):
        for pat, repl in _NONDETERMINISTIC_IDS:
            if pat.match(obj):
                return repl
    return obj


def _call(fn_name: str, args: dict):
    """Dispatch miroir de scripts/gen_golden_fixtures.py::call_fn."""
    if fn_name == "anthropic_to_openai":
        return pm.anthropic_to_openai(args["body"], args["model"])
    if fn_name == "openai_to_anthropic":
        return pm.openai_to_anthropic(args["resp"], args["model"])
    if fn_name == "openai_to_anthropic_request":
        return pm.openai_to_anthropic_request(args["oai_body"])
    if fn_name == "anthropic_to_openai_response":
        return pm.anthropic_to_openai_response(args["anthro"], args["model"])
    if fn_name == "openai_responses_to_anthropic":
        return pm.openai_responses_to_anthropic(args["body"])
    if fn_name == "_responses_sse_to_chat_deltas_lines":
        return [pm._responses_sse_to_chat_deltas(line) for line in args["lines"]]
    raise KeyError(f"fixture: fonction inconnue '{fn_name}' — mettre à jour le dispatcher")


FIXTURES = sorted(GOLDEN_DIR.glob("*.json"))


def test_golden_dir_not_empty():
    assert FIXTURES, (
        "docs/v1-response-golden/ vide — générer les fixtures avant toute "
        "modification de conversion (§11.5)"
    )


@pytest.mark.parametrize("path", FIXTURES, ids=[p.stem for p in FIXTURES])
def test_golden_contract(path: Path):
    case = json.loads(path.read_text(encoding="utf-8"))
    for required in ("fn", "input", "expected"):
        assert required in case, f"{path.name}: champ '{required}' manquant"
    actual = _call(case["fn"], case["input"])
    assert _normalize(actual) == _normalize(case["expected"]), (
        f"{path.name}: la sortie a dérivé du contrat V1 figé. Si le changement "
        "est VOLONTAIRE (fix 14.x), régénérer via scripts/gen_golden_fixtures.py "
        "et relire le diff avant commit."
    )
