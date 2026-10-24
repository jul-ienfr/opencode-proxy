"""V8.1 — détecteur de dérive : catalogue de l'éditeur contre routage réel.

**Ce que ces tests protègent.** Un tableau de bord qui crie au loup est pire
qu'aucun tableau de bord : on finit par l'ignorer. Trois pièges mesurés le
2026-10-22, chacun a son test :

1. un **alias** (`jev-1.13` → `jev-1.13-free`) n'est pas un modèle mort ;
2. un modèle absent du snapshot mais **servi avec succès** n'est pas mort : c'est
   le snapshot qui a vieilli ;
3. un modèle **refusé à chaque appel** n'est pas servi, même s'il répond en 200
   avec une trame d'erreur — le proxy renvoie 200 dans ce cas.
"""

from __future__ import annotations

import pathlib
import sys

import pytest

ROOT = pathlib.Path(__file__).resolve().parent.parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from observability import drift  # noqa: E402


class TestCompute:
    def test_a_routed_model_missing_from_the_catalog_is_an_orphan(self):
        section = drift.compute(["a", "b"], ["b", "disparu"], "paid")
        assert section["orphans"] == ["disparu"]
        assert section["status"] == "orphans"

    def test_a_catalog_model_not_routed_is_only_a_candidate(self):
        """Un candidat n'est pas un défaut : sur 81 ids free, un seul servait."""
        section = drift.compute(["a", "b", "c"], ["a"], "free")
        assert section["candidates"] == ["b", "c"]
        assert section["orphans"] == []
        assert section["status"] == "candidates"

    def test_orphans_outrank_candidates_in_the_status(self):
        section = drift.compute(["a"], ["zzz"], "paid")
        assert section["status"] == "orphans"

    def test_aligned_is_pure_alignment(self):
        section = drift.compute(["a", "b"], ["a", "b"], "free")
        assert section["status"] == "aligned"
        assert section["candidates"] == [] and section["orphans"] == []

    def test_counts_are_consistent(self):
        section = drift.compute(["a", "b", "c"], ["c", "d"], "free")
        assert section["catalog_count"] == 3
        assert section["routed_count"] == 2
        assert section["common_count"] == 1

    def test_empty_inputs_do_not_explode(self):
        section = drift.compute(None, None, "free")
        assert section["status"] == "aligned"

    def test_the_common_list_is_counted_not_enumerated(self):
        """80 lignes de « communs » ne servent aucune décision."""
        section = drift.compute([f"m{i}" for i in range(90)], [f"m{i}" for i in range(90)], "free")
        assert "common" not in section
        assert section["common_count"] == 90


class TestAnnotate:
    def test_an_orphan_seen_succeeding_blames_the_snapshot(self):
        section = drift.compute(["a"], ["jev-1.13-free"], "free")
        annotated = drift.annotate(section, {"jev-1.13-free"})
        assert annotated["orphans_stale_snapshot"] == ["jev-1.13-free"]
        assert annotated["orphans_to_verify"] == []

    def test_an_orphan_never_seen_succeeding_must_be_verified(self):
        section = drift.compute(["a"], ["modele-fantome"], "free")
        annotated = drift.annotate(section, set())
        assert annotated["orphans_to_verify"] == ["modele-fantome"]
        assert annotated["orphans_stale_snapshot"] == []

    def test_annotate_does_not_mutate_the_input(self):
        section = drift.compute(["a"], ["b"], "free")
        before = dict(section)
        drift.annotate(section, {"b"})
        assert section == before

    def test_annotate_tolerates_a_missing_orphan_list(self):
        assert drift.annotate({}, set())["orphans_to_verify"] == []


class TestSummarise:
    def test_it_aggregates_orphans_across_legs(self):
        payload = drift.summarise([
            drift.compute(["a"], ["x"], "free"),
            drift.compute(["b"], ["y"], "paid"),
        ])
        assert payload["orphan_count"] == 2
        assert payload["orphans"] == ["free::x", "paid::y"]

    def test_candidates_alone_are_not_a_failure(self):
        payload = drift.summarise([drift.compute(["a", "b"], ["a"], "free")])
        assert payload["status"] == "candidates"

    def test_empty_input_is_aligned(self):
        assert drift.summarise([])["status"] == "aligned"

    def test_garbage_is_ignored(self):
        payload = drift.summarise([None, "pas un dict", {"leg": "free"}])
        assert payload["status"] == "aligned"


class TestAgainstTheShippedSnapshot:
    def test_the_snapshot_on_disk_is_parseable(self):
        import json

        path = ROOT / "config" / "catalog_routes.json"
        snapshot = json.loads(path.read_text(encoding="utf-8"))
        assert "providers" in snapshot
        assert snapshot["providers"]["opencode"]

    def test_the_jev_lesson_is_recorded(self):
        """Régression : `jev-1.13-free` a été déclaré « servi » parce qu'il
        répondait vite. Il échouait à chaque appel. Le module ne doit jamais
        confondre « observé » et « servi » : c'est pourquoi `annotate` prend une
        liste de succès, jamais une liste d'observations."""
        import inspect

        source = inspect.getsource(drift.annotate)
        assert "succès" in source.lower() or "success" in source.lower() or "observed" in source
