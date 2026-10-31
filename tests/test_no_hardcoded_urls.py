"""V7.3 — cliquet sur les URLs d'amont : on n'en ajoute plus, on n'en retire pas par accident.

**Pourquoi un cliquet et pas une interdiction.** L'audit annonçait « 4 URLs en dur à
supprimer » ; la mesure en a trouvé **deux réelles** dans `opencode.py`, plus des
défauts légitimes dans le résolveur et les constructeurs du catalogue. Une
interdiction sèche casserait la porte : `Origin: https://opencode.ai` fait partie
du facteur *identity* — le remplacer par un calcul serait un changement de
comportement déguisé en nettoyage.

**Pourquoi compter par fichier et non par ligne.** La première version figeait des
numéros de ligne. Mes propres modifications ont décalé le fichier de dix-neuf
lignes et le cliquet a crié « nouvelle URL » sur une URL qui existait déjà : un
garde-fou qui casse à chaque refactoring est un garde-fou qu'on désactive. Un
**comptage par fichier** attrape la même chose (un littéral de plus) sans dépendre
de la mise en page, et le message d'échec cite le code fautif pour la revue.

**Ce qu'il n'est pas.** Un test de style. Chaque entrée porte la raison de son
existence : une valeur qui appartient au protocole de la porte n'est pas une dette
technique, c'est une constante de fait.
"""

from __future__ import annotations

import pathlib
import re

ROOT = pathlib.Path(__file__).resolve().parent.parent

#: Dossiers scannés — le code produit, pas les tests ni la recherche.
SCANNED_DIRS = ("", "config", "upstream", "streaming", "observability")

#: Fichiers ignorés même dans ces dossiers.
SKIPPED_FILES = {"test_free_quota.py", "conftest.py"}

#: Un littéral *entre guillemets* : une URL dans une phrase de docstring n'est
#: pas un appel réseau. C'est la correction d'une erreur de la première version.
PATTERN = re.compile(r"""["']https?://opencode\.ai""")

#: Nombre maximal d'occurrences par fichier, avec la raison. Le comptage est
#: volontairement par fichier : robuste au décalage des lignes, strict sur le
#: nombre. Une occurrence de plus = un test rouge.
ALLOWED: dict[str, tuple[int, str]] = {
    "opencode.py": (
        2,
        "endpoint `systemone` (pas de variante dans le résolveur) + "
        "`Origin`/`Referer`, partie du facteur identity mesuré",
    ),
    "discovery.py": (1, "construction d'URL du catalogue + lien de documentation"),
    "settings.py": (5, "défauts du résolveur (`yaml_get` prioritaire)"),
}


def _iter_python_files():
    for sub in SCANNED_DIRS:
        base = ROOT / sub if sub else ROOT
        if not base.is_dir():
            continue
        for path in sorted(base.glob("*.py")):
            if path.name.startswith("test_") or path.name in SKIPPED_FILES:
                continue
            yield path


def _occurrences() -> dict[str, list[tuple[int, str]]]:
    """``{fichier: [(ligne, extrait), …]}`` — littéraux hors commentaires."""
    found: dict[str, list[tuple[int, str]]] = {}
    for path in _iter_python_files():
        try:
            text = path.read_text(encoding="utf-8", errors="replace")
        except Exception:
            continue
        for lineno, line in enumerate(text.splitlines(), start=1):
            if line.strip().startswith("#"):
                continue
            if PATTERN.search(line):
                found.setdefault(path.name, []).append((lineno, line.strip()[:110]))
    return found


class TestRatchet:
    def test_no_file_gains_a_new_literal(self):
        """Le cliquet : une occurrence de plus casse ce test, et c'est voulu."""
        trouves = _occurrences()
        exces = {}
        for name, entries in trouves.items():
            limite = ALLOWED.get(name, (0, ""))[0]
            if len(entries) > limite:
                exces[name] = entries[limite:]
        assert not exces, (
            f"URL d'amont en dur en plus du quota : passer par le résolveur (`config/settings.py`) — {exces}"
        )

    def test_no_allowlist_entry_is_stale(self):
        """Un quota trop large rendrait le cliquet menteur : on le resserre."""
        trouves = _occurrences()
        obsoletes = {
            name: (limite, len(trouves.get(name, [])))
            for name, (limite, _raison) in ALLOWED.items()
            if len(trouves.get(name, [])) < limite
        }
        assert not obsoletes, f"quotas à resserrer (moins d'occurrences que déclaré) : {obsoletes}"

    def test_every_quota_carries_a_reason(self):
        for name, (limite, raison) in ALLOWED.items():
            assert limite > 0, name
            assert raison.strip(), f"{name} sans raison documentée"

    def test_the_identity_header_is_still_present(self):
        """Garde-fou ciblé : `Origin` fait partie du facteur identity (mesuré).

        Si ce test casse, la valeur exacte a été remplacée. Re-mesurer le facteur
        identity avant de la changer — ne pas la calculer « proprement ».
        """
        source = (ROOT / "opencode.py").read_text(encoding="utf-8", errors="replace")
        assert '"https://opencode.ai"' in source, (
            "la valeur exacte de l'en-tête d'origine a disparu — re-mesurer le facteur identity avant de la remplacer"
        )


class TestScope:
    def test_the_scanner_really_scans(self):
        """Un scanner qui ne lit rien passerait tous les tests ci-dessus."""
        files = list(_iter_python_files())
        assert len(files) > 10, f"scanner vide ou mal cadré : {len(files)} fichiers"
        assert any(p.name == "opencode.py" for p in files)

    def test_the_scanner_finds_the_known_literals(self):
        """S'il ne trouve rien, les quotas seraient tous « obsolètes » sans bruit."""
        trouves = _occurrences()
        assert trouves.get("opencode.py"), "aucun littéral vu dans opencode.py"
        assert trouves.get("settings.py"), "aucun littéral vu dans settings.py"

    def test_tests_and_research_are_out_of_scope(self):
        names = {p.name for p in _iter_python_files()}
        assert not any(n.startswith("test_") for n in names)
