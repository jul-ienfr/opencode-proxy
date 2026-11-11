# ADR-009 — Veille outillage d'ingénierie inverse (lot REA)

- Statut : proposé (veille, aucune adoption)
- Date : 2026-11-09
- Portée : outillage d'analyse de binaires et de clients ; hors runtime du proxy

## Contexte

L'opérateur a transmis un lot de 20 dépôts présenté comme un « studio
d'ingénierie inverse » articulé autour de REA (serveur MCP). La question posée
est triple : ces outils sont-ils les meilleurs, existe-t-il mieux, et lesquels
sont compatibles entre eux.

Le projet n'a pas besoin d'un outil RE pour servir les stations. Le seul usage
pertinent est l'analyse du **client officiel** (binaire Bun) pour confirmer les
constantes de gate et d'identité obtenues par mesure, usage déjà couvert par
`docs/REPO_INTEL_2026-11-06.md` et `docs/AUDIT_REPOS_2026-11-06.md`.

## Décision

1. **Aucune adoption immédiate.** Le lot reste en veille documentée. Rien n'est
   installé, aucune dépendance n'entre dans le projet.
2. **Ne pas retenir REA comme socle.** REA est un agrégateur MCP (MIT,
   TypeScript, npm) qui dépend de moteurs GUI (Hopper/Ghidra/IDA/Kuna) et
   expose plusieurs dizaines d'outils : coût de contexte et non-reproductibilité
   pour un gain non démontré sur nos cibles. Il reste candidat à une évaluation
   isolée, en dernier.
3. **Retenir une stack minimale de huit outils, hors produit :**
   triage (`capa`, `LIEF`, `Detect-It-Easy`), statique (Ghidra headless ou Kuna,
   plus `radare2`/`rizin`), dynamique (`Frida`, `mitmproxy`), vérification
   (`BinDiff`, `Hypothesis`, `AFL++` si binaire natif).
4. **Remplacer les choix faibles du lot :** `GhidraMCP` par ReVa ou
   GhidrAssistMCP (ou `pyghidra` headless), `angr` généraliste par Triton/miasm
   pour le ciblé, `raddebugger` en surveillance seulement.
5. **Ne pas traiter les listes « awesome » ni le classement DecBench comme des
   preuves indépendantes** : `morluto` a créé 88 dépôts le même jour
   (2026-10-09) et 96 de ses 100 dépôts ont moins de 10 étoiles ; DecBench est
   publié par le laboratoire qui développe Kuna.
6. **Retenir `kalil0321/reverse-api-engineer` (RAE) comme seul candidat de la
   couche web/API** : MIT, Python 3.11+, capture HAR → client typé dans
   11 langages, CLI `--json` utilisable en CI. Ce n'est pas `morluto/rea`
   malgré l'homonymie. Évaluation encadrée uniquement, jamais sur une session
   authentifiée réelle : le HAR contient cookies et jetons, et part chez le
   fournisseur du modèle.

## Invariants

- Aucun outil d'analyse n'est exécuté sur une machine qui sert les stations.
- Aucun outil GPL/AGPL n'entre dans le code distribué du proxy.
- Toute conclusion issue d'une décompilation est confirmée par une mesure live
  avant d'entrer dans une spec.
- Tout chiffre cité dans un rapport a un fichier de preuve et une commande de
  régénération (`docs/evidence/README.md`).

## Conséquences

- Positives : décision tracée, coût nul, pas de dette de dépendance, cadre
  clair si l'analyse du client devient nécessaire.
- Négatives : aucune capacité d'analyse n'est disponible immédiatement ; il
  faudra une VM isolée le jour où le besoin arrive.
- Suivi : réévaluer si (a) une constante de gate ne peut pas être obtenue par
  mesure, (b) un client mobile non officiel devient pertinent, ou (c) Kuna
  dépasse Ghidra sur nos propres cibles.

## Références

- [`docs/REA_STACK_REVERSE_ENGINEERING_2026-11-09.md`](../REA_STACK_REVERSE_ENGINEERING_2026-11-09.md) — inventaire et conditions d'usage.
- [`docs/REA_STACK_COMPARATIF_2026-11-09.md`](../REA_STACK_COMPARATIF_2026-11-09.md) — comparatif chiffré, alternatives, compléments.
- [`docs/evidence/rea-2026-11-09/`](../evidence/rea-2026-11-09/) — métadonnées GitHub, activité `morluto`, classement DecBench.
- `scripts/re_stack_evidence.py` — régénération des preuves.
