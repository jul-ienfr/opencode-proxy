"""Gate T308 / INV-8 — Aucun artefact requis n'est absent du dépôt.

Ce gate vérifie de manière réaliste (via git ls-files --error-unmatch) que
chaque artefact déclaré dans la configuration existe sur disque et est
versionné par git. Le fichier `free/official_payload.json.bak` est explicitement
hors périmètre (c'est un duplicata optionnel non versionné).

L'audit 2026-11-20 a repéré les manquants suivants, maintenant corrigés :
- `free/captures/1.18.31.json` et `free/captures/1.19.3.json` supprimés
  du registre alors que `free/profiles/verified_profiles.json` y référait
  (corrigé en pointer vers les captures existantes 1.19.5 / 2.2.5 / 2.3.0).
"""

from __future__ import annotations

import json
import subprocess
from pathlib import Path
from typing import Iterable, List

# ---------------------------------------------------------------------------
# Root & paths
# ---------------------------------------------------------------------------

REPO_ROOT = Path(__file__).resolve().parents[1]
"""Répertoire racine du dépôt (parent de `tests/`)."""

PROFILES_PATH = REPO_ROOT / "free" / "profiles" / "verified_profiles.json"
"""Registre officiel des profils vérifiés (source de vérité)."""

OFFICIAL_PAYLOAD_PATH = REPO_ROOT / "free" / "official_payload.json"
"""Payload officiel gratuit (version canonique 1.19.5)."""

COMPOSE_DIR = REPO_ROOT / "deploy" / "compose"
"""Répertoire des fichiers compose du plan de contrôle."""

# Les 6 fichiers compose obligatoires (ex: compose-control-plane-1.yml .. 6.yml)
COMPOSE_ARTIFACTS: tuple[Path, ...] = tuple(
    COMPOSE_DIR / f"compose-control-plane-{index}.yml" for index in range(1, 7)
)

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _git_repo_root(root: Path) -> bool:
    """Retourne True si le répertoire `root` apparaît comme dépôt git."""
    return (root / ".git").exists()


def _git_tracked(root: Path, relative: Path) -> bool:
    """Retourne True si le fichier `relative` (par rapport à root) est suivi par git."""
    result = subprocess.run(
        ["git", "-C", str(root), "ls-files", "--error-unmatch", "--", relative.as_posix()],
        capture_output=True,
        text=True,
        check=False,
    )
    return result.returncode == 0


def _relative(raw: str) -> Path:
    """Normalise un chemin d'artefact déclaré et refuse toute sortie du dépôt.

    Lève ValueError si le chemin est absolu, vide, ou contient ``..`` (sortie de racine).
    """
    text = str(raw).strip()
    if not text:
        raise ValueError("chemin d'artefact vide")
    candidate = Path(text)
    # Absolute paths are forbidden – they could escape the repo.
    if candidate.is_absolute():
        raise ValueError(f"chemin absolu interdit: {raw}")
    # ``..`` segments are forbidden – they could escape the repo.
    if ".." in candidate.parts:
        raise ValueError(f"chemin hors dépôt interdit: {raw}")
    return candidate


def _audit(root: Path, declared: Iterable[str], *, require_tracked: bool | None = None) -> List[str]:
    """Retourne la liste des problèmes trouvés sur les artefacts déclarés.

    * `root` – racine du dépôt.
    * `declared` – itérable de chemins (relatifs) déclarés.
    * `require_tracked` – si True, chaque fichier doit aussi passer `git ls-files`.
      Si None, on ne vérifie le suivi que si un dépôt git est détecté à la racine.
    Retourne une liste de messages d'erreur (vide ⇒ tout est bon).
    """
    problems: List[str] = []
    repo = _git_repo_root(root)
    if require_tracked is None:
        require_tracked = repo

    for raw in declared:
        try:
            rel = _relative(raw)
        except ValueError as exc:
            problems.append(str(exc))
            continue

        target = root / rel
        # 1. Le fichier doit exister sur disque.
        if not target.is_file():
            problems.append(f"absent: {raw}")
            continue

        # 2. Le fichier doit être suivi par git (si requis).
        if require_tracked and not _git_tracked(root, rel):
            problems.append(f"non versionné: {raw}")

    return problems


def _load_profiles() -> dict:
    """Charge et valide le document JSON des profils vérifiés."""
    text = PROFILES_PATH.read_text(encoding="utf-8")
    document = json.loads(text)
    assert isinstance(document, dict), "document JSON attendu"
    return document


def _profile_capture_paths(document: dict) -> List[str]:
    """Extrait la liste des chemins body_capture déclarés dans le registre."""
    profiles = document.get("profiles")
    assert isinstance(profiles, list) and profiles, (
        "registre de profils vide ou invalide (attendu une liste non vide)"
    )
    captures: List[str] = []
    for idx, profile in enumerate(profiles):
        assert isinstance(profile, dict), f"profil #{idx} invalide (attendu un dictionnaire)"
        raw = str(profile.get("body_capture") or "").strip()
        assert raw, f"profil #{idx} ({profile.get('profile_id')}) sans body_capture"
        captures.append(raw)
    return captures


def _required_artifacts() -> List[str]:
    """Retourne la liste complète des artefacts requis (hors .bak optionnel)."""
    document = _load_profiles()
    captures = _profile_capture_paths(document)
    artifacts: List[str] = []

    # 1. Le registre lui-même
    artifacts.append("free/profiles/verified_profiles.json")

    # 2. Chaque body_capture référencé par le registre
    artifacts.extend(captures)

    # 3. Les 6 fichiers compose du plan de contrôle
    for path in COMPOSE_ARTIFACTS:
        artifacts.append(str(path.relative_to(REPO_ROOT)))

    # 4. Le payload officiel (exclut le .bak)
    artifacts.append("free/official_payload.json")

    return artifacts


# ---------------------------------------------------------------------------
# Tests unitaires
# ---------------------------------------------------------------------------


def test_gate_refuses_paths_outside_repository():
    """Le helper _relative doit rejeter les chemins sortants du dépôt."""
    problems = _audit(REPO_ROOT, ["../outside.json"], require_tracked=False)
    assert any("hors dépôt" in p for p in problems)


def test_gate_refuses_absolute_paths():
    """Le helper _relative doit rejeter les chemins absolus (indépendant de l'OS)."""
    absolute = str(REPO_ROOT / "outside.json")
    problems = _audit(REPO_ROOT, [absolute], require_tracked=False)
    assert any("absolu" in p for p in problems)


def test_profile_captures_are_existing_and_tracked():
    """Chaque body_capture du registre existe sur disque et est versionné."""
    document = _load_profiles()
    captures = _profile_capture_paths(document)
    problems = _audit(REPO_ROOT, captures)
    assert not problems, (
        "artefacts de profils invalides ou non versionnés :\n  - "
        + "\n  - ".join(problems)
    )


def test_compose_artifacts_are_existing_and_tracked():
    """Chaque fichier compose du plan de contrôle existe et est versionné."""
    declared = [path.relative_to(REPO_ROOT).as_posix() for path in COMPOSE_ARTIFACTS]
    problems = _audit(REPO_ROOT, declared)
    assert not problems, (
        "fichiers compose invalides ou non versionnés :\n  - "
        + "\n  - ".join(problems)
    )


def test_official_payload_is_existing_and_tracked():
    """Le fichier official_payload.json existe et est versionné."""
    problems = _audit(REPO_ROOT, ["free/official_payload.json"])
    assert not problems, (
        "payload officiel invalide ou non versionné :\n  - "
        + "\n  - ".join(problems)
    )


def test_optional_bak_is_not_required():
    """Le fichier .bak est explicitement hors périmètre du gate."""
    # Le gate ne doit jamais inclure .bak dans les artefacts requis.
    required = _required_artifacts()
    # Il est acceptable que .bak n'apparaisse pas dans la liste, ou qu'on l'exclue.
    assert "free/official_payload.json.bak" not in required, (
        ".bak ne doit pas être un artefact requis"
    )


def test_gate_self_check_against_synthetic_missing():
    """Le gate détecte un corps de capture manquant dans un document synthétique."""
    # Aucun fichier absent n'existe ni n'est suivi ; le gate doit le signaler.
    problems = _audit(REPO_ROOT, ["free/captures/absent.json"], require_tracked=True)
    assert any("absent" in p for p in problems), (
        f"Le gate n'a pas détecté le fichier manquant : {problems}"
    )


def test_required_artifacts_list_is_complete():
    """Vérifie que la liste d'artefacts requis est complète et cohérente."""
    required = _required_artifacts()
    # Aucun élément ne doit être vide ni None.
    for artifact in required:
        assert artifact, "élément d'artefact requis vide"
        # Doit être un chemin relatif simple (pas de ``..``).
        assert ".." not in artifact, f"chemin sortant détecté dans la liste requise : {artifact}"
    # Le .bak ne doit jamais apparaître.
    assert "free/official_payload.json.bak" not in required