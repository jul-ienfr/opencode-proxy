"""Le vault d'identités par IP est borné (mémoire et disque).

Constat 2026-10-25 : 548 fichiers dans `logs/identities/`, un par IP vue à
vie. `IdentityVault.prune()` existait mais n'était appelé nulle part, et les
fichiers des IP jamais revues n'étaient jamais chargés — donc jamais évincés.

Ces tests verrouillent les deux bornes : plafond à l'insertion (mémoire +
fichier) et balayage des fichiers périmés au boot.
"""

import os
import time

import pytest

from free.identities import IdentityVault


@pytest.fixture()
def vault(tmp_path, monkeypatch):
    d = tmp_path / "ids"
    monkeypatch.setenv("OPENCODE_IDENTITIES_DIR", str(d))
    v = IdentityVault()
    return v, d


def _files(d):
    return sorted(os.listdir(d)) if os.path.isdir(d) else []


def test_le_plafond_evince_les_plus_anciennes(vault):
    v, d = vault
    v._max_ids = 3
    for i in range(5):
        v.get(f"10.0.0.{i}")
    assert set(v._ids) == {"10.0.0.2", "10.0.0.3", "10.0.0.4"}
    # Les fichiers des évincées sont supprimés, pas seulement la mémoire.
    assert _files(d) == ["10.0.0.2.json", "10.0.0.3.json", "10.0.0.4.json"]


def test_prune_supprime_aussi_les_fichiers(vault):
    v, d = vault
    for i in range(4):
        v.get(f"10.0.0.{i}")
    removed = v.prune({"10.0.0.3"})
    assert removed == 3
    assert set(v._ids) == {"10.0.0.3"}
    assert _files(d) == ["10.0.0.3.json"]


def test_une_ip_revue_apres_eviction_repart_neuve(vault):
    v, d = vault
    v._max_ids = 1
    first = v.get("10.0.0.9")
    v.get("10.0.0.10")  # force l'éviction de .9
    second = v.get("10.0.0.9")
    assert second is not first


def test_cleanup_ne_touche_pas_les_fichiers_recents(vault):
    v, d = vault
    v.get("10.0.0.1")
    assert v.cleanup_stale_files(max_age_days=14) == 0
    assert len(_files(d)) == 1


def test_cleanup_supprime_les_fichiers_perimes(vault):
    v, d = vault
    v.get("10.0.0.1")
    path = d / "10.0.0.1.json"
    old = time.time() - 30 * 86400
    os.utime(path, (old, old))
    assert v.cleanup_stale_files(max_age_days=14) == 1
    assert _files(d) == []
