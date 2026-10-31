"""Garde du gate CI : un contrat déclaré doit exister, sinon la CI ment.

**Le risque réel.** Un gate qui liste des fichiers en dur pourrit en silence :
on renomme `test_free_gate_factors.py`, le gate ne le voit plus, il passe au
vert, et plus personne ne teste le contrat. Une CI verte qui ne teste rien est
pire qu'une CI rouge — elle donne la permission de livrer.

Ces tests vérifient que **chaque** fichier déclaré existe, et que les contrats
gagnés cette session y sont.
"""

from __future__ import annotations

import importlib.util
import pathlib
import sys

import pytest

ROOT = pathlib.Path(__file__).resolve().parent.parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

_SPEC = importlib.util.spec_from_file_location("ci_contracts", ROOT / "scripts" / "ci_contracts.py")
assert _SPEC and _SPEC.loader
ci = importlib.util.module_from_spec(_SPEC)
_SPEC.loader.exec_module(ci)


class TestEveryDeclaredContractExists:
    @pytest.mark.parametrize("nom", sorted({n for noms in ci.CONTRATS.values() for n in noms}))
    def test_declared_file_exists(self, nom):
        assert (ROOT / "tests" / nom).exists(), (
            f"{nom} est déclaré dans le gate CI mais n'existe pas : le gate passerait au vert sans l'exécuter"
        )

    def test_the_gate_is_not_empty(self):
        assert len(ci._tous_les_contrats()) >= 15

    def test_the_gate_contains_no_duplicates(self):
        chemins = ci._tous_les_contrats()
        assert len(chemins) == len(set(chemins))


class TestTheContractsOfThisSessionAreGated:
    """Les garanties chèrement gagnées doivent rester sous gate."""

    @pytest.mark.parametrize(
        "fichier, contrat",
        [
            ("test_gate_stream_contract.py", "stream:true requis par le gate free"),
            ("test_free_leg_protocol_parity.py", "protocole de la jambe free"),
            ("test_free_refusal_metric.py", "un 200 + trame d'erreur n'est pas un succès"),
            ("test_canary.py", "verdict du canary sur le contenu"),
            ("test_catalog_drift.py", "orphelins du catalogue"),
            ("test_systemone_protocol_guard.py", "mauvais protocole refusé clairement"),
            ("test_instance_lock_diagnosis.py", "verrou : zombie vs instance vivante"),
            ("test_no_hardcoded_urls.py", "pas d'URL en dur"),
        ],
    )
    def test_contract_is_under_gate(self, fichier, contrat):
        noms = {p.name for p in ci._tous_les_contrats()}
        assert fichier in noms, f"{contrat} n'est plus couvert par le gate CI"


class TestTheGateCommandLine:
    def test_list_mode_exits_zero(self, capsys):
        assert ci.main(["--list"]) == 0
        sortie = capsys.readouterr().out
        assert "gate" in sortie.lower() or "contrat" in sortie.lower()

    def test_list_mode_flags_a_missing_file(self, monkeypatch, capsys):
        monkeypatch.setitem(ci.CONTRATS, "contrat bidon", ["test_qui_n_existe_pas.py"])
        assert ci.main(["--list"]) == 0  # lister ne doit jamais échouer
        assert "manquant" in capsys.readouterr().out
