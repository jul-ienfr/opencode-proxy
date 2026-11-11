# Preuves — veille outillage d'ingénierie inverse

Fichiers **générés** par `scripts/re_stack_evidence.py`, jamais édités à la
main. Ils portent les chiffres cités dans
[`docs/REA_STACK_COMPARATIF_2026-11-09.md`](../../REA_STACK_COMPARATIF_2026-11-09.md)
et la décision [`ADR-009`](../../adr/ADR-009-veille-stack-reverse-engineering.md).

## Fichiers

- `repos.csv` / `repos.json` — 81 dépôts : étoiles, forks, issues ouvertes,
  date de création, dernier push, archivage, licence SPDX, langage.
  Source : `api.github.com/repos/{owner}/{repo}`.
- `morluto-repos.json` — dépôts du compte `morluto` (date de création, étoiles)
  et agrégats : nombre de dépôts créés le même jour, dépôts à moins de
  10 étoiles. Source : `api.github.com/users/morluto/repos`.
- `decbench.json` — instantané public DecBench (`snapshots/index.json`) et
  extrait des `combos` `unoptimized|1`, `optimized|0`, `sample-set|0` :
  fonctions parfaites / mesurées, taux d'erreur d'analyse, taux de
  recompilation. Source : `decbench.com/data/aggregates.json`.
- `cache/` — réponses brutes, permet de relancer sans reconsommer le quota.

## Régénérer

```bash
python scripts/re_stack_evidence.py --wait
```

`--wait` attend le reset du quota GitHub (60 requêtes/heure en anonyme) au lieu
d'échouer. Sans `--wait`, une exécution après une autre ne consomme que les
dépôts absents du cache ; `--refresh` ignore le cache.

## Limites

- Les métadonnées GitHub sont un instantané : étoiles et dates bougent.
- DecBench est publié par Noelo Lab (University of Georgia), qui développe aussi
  Kuna. Le classement est open source mais **non indépendant** ; il inclut
  toutefois ses concurrents (Hex-Rays, Ghidra, Binary Ninja, angr, r2dec).
- L'échantillon LLM (250 fonctions) n'est pas comparable au jeu complet
  (16 000+ fonctions) : le document le signale.
