# Comparatif — stack REA et outillage d'ingénierie inverse

**Date** : 2026-11-09
**Méthode** : métadonnées GitHub live (API, 2026-11-09), README des projets,
classement public DecBench (snapshot 2026-11-04), lecture des dépôts de l'auteur.
**Complément de** : [`docs/REA_STACK_REVERSE_ENGINEERING_2026-11-09.md`](REA_STACK_REVERSE_ENGINEERING_2026-11-09.md)
**But** : dire, pour chaque brique fournie, si c'est le meilleur choix, ce qui
fait mieux, et ce qui manque.
**Décision** : [`docs/adr/ADR-009-veille-stack-reverse-engineering.md`](adr/ADR-009-veille-stack-reverse-engineering.md)

## 0. Preuves

Chaque chiffre de ce document vient d'un fichier généré, jamais saisi à la
main (règle de [`docs/evidence/README.md`](evidence/README.md)).

| Fichier | Contenu |
|---|---|
| [`evidence/rea-2026-11-09/repos.csv`](evidence/rea-2026-11-09/repos.csv) | 81 dépôts : étoiles, forks, issues, création, dernier push, licence, archivage |
| [`evidence/rea-2026-11-09/morluto-repos.json`](evidence/rea-2026-11-09/morluto-repos.json) | activité du compte `morluto` (dépôts par jour de création, distribution des étoiles) |
| [`evidence/rea-2026-11-09/decbench.json`](evidence/rea-2026-11-09/decbench.json) | instantané DecBench + taux par décompilateur, erreurs et recompilation |

Régénération (quota GitHub 60 req/h, cache local) :

```bash
python scripts/re_stack_evidence.py --wait
```

---

## 1. Résumé exécutif

1. **La liste est bonne mais pas optimale.** Les briques sont les standards du
   domaine ; trois choix sont discutables (GhidraMCP, angr en généraliste,
   raddebugger en remplacement), et il manque la couche qui compte vraiment :
   **triage statique + vérification/oracles**.
2. **Le meilleur décompilateur libre n'est plus Ghidra sur les métriques
   publiées** : Kuna (Rust, Apache-2.0) sort premier sur DecBench, devant
   Hex-Rays. Mais Kuna est jeune, et DecBench est écrit par le même laboratoire
   que Kuna — classement à lire avec cette réserve.
3. **Ce qui fait la performance réelle, ce n'est pas le nombre d'outils**, c'est
   le tri amont (capa/LIEF/YARA) et l'oracle aval (BinDiff/Diaphora +
   Hypothesis/AFL++). Ces deux couches sont absentes de la liste et coûtent
   moins cher que le reste.
4. **La couche web/API manque aussi** : un seul outil du lot la traite,
   `kalil0321/reverse-api-engineer` (RAE, distinct de `morluto/rea`), et c'est
   le plus directement utile à un projet de proxy — avec la réserve des secrets
   contenus dans le HAR.
5. **La source est un écosystème marketing vertical** : REA, Kuna, DecBench,
   les listes « awesome » et le site de notation `agentskillshub.top` se
   citent mutuellement. Ça n'invalide pas les outils, mais chaque
   recommandation doit être vérifiée indépendamment — ce que fait ce document.

---

## 2. Le point dur : quel décompilateur est réellement le meilleur ?

DecBench (96 103 fonctions, 770 binaires, 16 décompilateurs, snapshot du
2026-11-04) mesure le pourcentage de fonctions où le décompilateur atteint un
score parfait sur au moins une des trois métriques (structure, types,
recompilation).

| Décompilateur | Non optimisé (n=16 289) | Optimisé (n=35 173) | Sortie qui recompile |
|---|---|---|---|
| Kuna 1.515 | **48,3 %** | **37,3 %** | 93,0 % |
| Hex-Rays / IDA 9.2 | 45,7 % | 37,0 % | **96,2 %** |
| angr 9.3.4 | 44,0 % | 36,9 % | 91,5 % |
| Binary Ninja 5.3 | 40,8 % | 30,7 % | 81,7 % |
| Ghidra 12.1 | 35,6 % | 31,5 % | 90,0 % |
| r2dec 6.2 | 29,3 % | 19,2 % | 48,2 % |
| Codex (échantillon 250 f.) | 59,6 % | — | 100 % |
| Claude Code (échantillon 250 f.) | 56,8 % | — | 88 % |

Lecture :

- **Ghidra est solide mais pas le meilleur** : ~32-36 %, avec un taux d'échec
  d'analyse de 10,5 % sur le jeu optimisé (contre 3,7 % pour Kuna, 5,9 % pour
  IDA). C'est le prix de sa couverture d'architectures, pas de sa qualité.
- **Kuna premier, IDA deuxième, angr troisième** — l'écart Kuna/IDA est
  faible (2-3 points) et dépend du compilateur et du niveau d'optimisation.
- **Les agents LLM battent les décompilateurs classiques sur le petit
  échantillon** (59 % contre 48 %), mais au prix d'un appel agentique par
  fonction : c'est lent et cher. C'est exactement le pari de REA — et sa
  limite : ça ne passe pas à l'échelle d'un binaire entier.
- **Réserves** : DecBench est publié par **Noelo Lab (University of Georgia)**,
  qui développe **Kuna**. Le classement n'est donc pas indépendant, même s'il
  est open source et inclut ses concurrents (ce que peu de vendors font).
  Les 250 fonctions de l'échantillon LLM ne sont pas comparables aux 16 000
  du jeu complet.

**Conclusion** : pour un usage général, Ghidra reste le meilleur compromis
couverture/coût (0 €) ; Kuna est le meilleur choix si la qualité du pseudo-code
prime et que le projet accepte une dépendance jeune ; IDA/Binary Ninja restent
les références payantes.

---

## 3. Verdict brique par brique

Légende : **Garder** = bon choix · **Remplacer** = il existe mieux pour le même
usage · **Compléter** = utile mais insuffisant seul · **Écarter** = ne pas
adopter.

| Brique | Verdict | Pourquoi | Meilleur choix / alternative |
|---|---|---|---|
| REA | Compléter | Vrai produit (parseurs Go/Rust/IL2CPP/PE/ELF/APK/.NET, MIT), mais dépend de moteurs GUI, ~20+ outils MCP, et le discours « tout seul » est faux | Scripts headless + MCP ciblé (ReVa, radare2-mcp) |
| Ghidra | **Garder** | Meilleure couverture, Apache-2.0, `analyzeHeadless` | — |
| GhidraMCP | Remplacer | Plugin **GUI** : Ghidra doit rester ouvert ; grosse liste d'outils = contexte gonflé | **ReVa** (954★, Apache, MCP Ghidra) ou **GhidrAssistMCP** (808★, MIT) ou `pyghidra` headless |
| ida-pro-mcp | Garder si IDA | Le meilleur décompilateur payant ; pont MIT actif | Sinon Binary Ninja + `binary_ninja_mcp` (GPL, 456★) |
| jadx | **Garder** | Référence libre DEX→Java, 51,7k★, actif | JEB (payant) pour les apps durcies |
| Apktool | Garder | Indispensable pour ressources/smali ; ne décompile pas en Java | — |
| Frida | **Garder** | Référence instrumentation dynamique, 22,7k★ | + `r2frida` (1,4k★), `objection` (9,5k★), LSPosed (25,4k★) pour le système |
| ILSpy | Garder | Meilleur lecteur .NET, MIT | + dnSpyEx pour le debug |
| radare2 | Garder | CLI/JSON parfait pour un agent, écosystème de plugins | **rizin** si on veut une API propre — mais r2 gagne sur les plugins (`r2ghidra`, `r2frida`) |
| x64dbg | Garder (Windows) | Référence libre user-mode | WinDbg pour le kernel ; HyperDbg (4,2k★) pour l'hyperviseur |
| ImHex | Compléter | Excellent éditeur + langage de motifs, mais GPL-2.0 et manuel | LIEF/py pour l'automatisation ; 010 Editor (payant) pour les templates |
| angr | Compléter | Puissant mais lent et fragile sur binaires modernes | **Triton** (4,4k★, rapide, taint) ou **miasm** pour du ciblé ; KLEE/SymCC pour du C |
| mitmproxy | **Garder** | Le seul MITM vraiment scriptable, 46,8k★ | ZAP (16,1k★) pour le scan ; Burp/Charles pour le manuel |
| dnSpyEx | Garder | Seul debugger .NET éditable ; dnSpy d'origine est archivé (2023) | + ILSpy pour la lecture, AsmResolver pour le patch programmatique |
| rizin | Compléter | Fork plus propre, mais écosystème plus petit | Choisir r2 **ou** rizin, pas les deux |
| raddebugger | Surveiller | MIT, Epic, 8,8k★, très prometteur | Pas encore au niveau de x64dbg/WinDbg (kernel, scripting stable) |
| awesome-ai-reverse-engineering | Écarter comme preuve | 93★, 117 dépôts (pas 122), notation par `agentskillshub.top` — site affilié | `awesome-reverse-engineering` (2k★) comme simple index |
| awesome-reverse-engineering | Garder comme index | Correct, maintenu | — |
| workflows REA / boucle clean-room | **Garder le principe** | `SPEC.md` + oracle + retry est la bonne méthode | À implémenter avec des scripts, pas avec un MCP |
| Reverse API Engineer (RAE) — `kalil0321` | **Garder** | 3,0k★, MIT, Python 3.11+, actif : HAR → client typé dans 11 langages, modes manuel/agent/engineer/collector, CLI `--json`, skill pour OpenCode | Seul outil du lot qui traite la couche **web/API** ; à tester encadré (cf. §4.6) |

---

## 4. Ce qui manque à la liste (et qui compte le plus)

### 4.1 Triage statique — avant de décompiler

| Outil | ★ | Licence | Apport |
|---|---|---|---|
| [capa](https://github.com/mandiant/capa) | 6,3k | Apache-2.0 | Capacités et comportements d'un binaire, sans exécution |
| [FLOSS](https://github.com/mandiant/flare-floss) | 4,2k | Apache-2.0 | Chaînes obfusquées/empilées |
| [YARA](https://github.com/VirusTotal/yara) | 10,0k | BSD-3 | Règles de classification |
| [Detect-It-Easy](https://github.com/horsicq/Detect-It-Easy) | 11,9k | MIT | Packer/compilateur/protections |
| [LIEF](https://github.com/lief-project/LIEF) | 5,7k | Apache-2.0 | Parsing/édition ELF/PE/Mach-O en Python/C++ |
| [goblin](https://github.com/m4b/goblin) | 1,6k | MIT | Parsing binaire en Rust |

Ces cinq outils coûtent quelques secondes par binaire et évitent de lancer un
décompilateur (et un agent) sur 80 % des cibles triviales.

### 4.2 Vérification — après la reconstruction

| Outil | ★ | Licence | Apport |
|---|---|---|---|
| [BinDiff](https://github.com/google/bindiff) | 3,2k | Apache-2.0 | Mesure la similarité original/reconstruction |
| [Diaphora](https://github.com/joxeankoret/diaphora) | 4,5k | **AGPL-3.0** | Diffing avancé (attention à la licence) |
| [Hypothesis](https://github.com/HypothesisWorks/hypothesis) | 9,1k | MPL-2.0 | Tests basés sur les propriétés = vrais oracles |
| [AFL++](https://github.com/AFLplusplus/AFLplusplus) | 6,9k | **AGPL-3.0** | Fuzzing différentiel original vs réécriture |
| [atheris](https://github.com/google/atheris) | 1,7k | Apache-2.0 | Fuzzing Python (si la réécriture est en Python) |
| [rr](https://github.com/rr-debugger/rr) | 10,7k | MIT-like | Record/replay déterministe pour comparer deux exécutions |

C'est la couche qui rend la boucle « clean-room » crédible : sans elle,
`SPEC.md` n'est qu'une hypothèse.

### 4.3 Émulation et instrumentation

| Outil | ★ | Licence | Apport |
|---|---|---|---|
| [Unicorn](https://github.com/unicorn-engine/unicorn) | 9,6k | GPL-2.0 | Émulation CPU rapide, scriptable |
| [Qiling](https://github.com/qilingframework/qiling) | 6,2k | GPL-2.0 | Émulation OS complète (syscalls, FS) |
| [Speakeasy](https://github.com/mandiant/speakeasy) | 2,1k | MIT | Émulation de binaires Windows |
| [QBDI](https://github.com/quarkslab/QBDI) | 1,8k | Apache-2.0 | DBI précis, sans instrumentation source |
| [DynamoRIO](https://github.com/DynamoRIO/dynamorio) | 3,2k | BSD-like | DBI mature |

### 4.4 Mobile

| Outil | ★ | Licence | Apport |
|---|---|---|---|
| [MobSF](https://github.com/MobSF/Mobile-Security-Framework-MobSF) | 22,2k | GPL-3.0 | Orchestrateur APK/IPA (statique + dynamique) |
| [androguard](https://github.com/androguard/androguard) | 6,4k | Apache-2.0 | Parsing DEX/ARSC en Python |
| [quark-engine](https://github.com/quark-engine/quark-engine) | 1,7k | GPL-3.0 | Score de comportement malveillant |
| [r2frida](https://github.com/nowsecure/r2frida) | 1,5k | MIT | Pont r2 ↔ Frida (le combo le plus efficace) |

### 4.5 Débogage bas niveau

| Outil | ★ | Licence | Apport |
|---|---|---|---|
| [pwndbg](https://github.com/pwndbg/pwndbg) | 11,2k | MIT | GDB augmenté (Linux) — il manquait au lot |
| [gef](https://github.com/hugsy/gef) | 8,4k | MIT | Alternative à pwndbg |
| [HyperDbg](https://github.com/HyperDbg/hyperdbg) | 4,2k | GPL-3.0 | Débogueur hyperviseur (Windows, kernel/anti-debug) |
| [drgn](https://github.com/osandov/drgn) | 2,1k | LGPL-2.1 | Introspection kernel/processus en Python |

### 4.6 Web & API — la couche absente du lot

| Outil | ★ | Licence | Apport |
|---|---|---|---|
| [Reverse API Engineer](https://github.com/kalil0321/reverse-api-engineer) | 3,0k | MIT | Site → client API typé (HAR + LLM), 11 langages, modes `manual/agent/engineer/collector`, CLI `--json` |
| [mitmproxy2swagger](https://github.com/alufers/mitmproxy2swagger) | 9,7k | **aucune licence déclarée** | Capture mitmproxy **ou HAR** → spec OpenAPI 3.0 |
| [curlconverter](https://github.com/curlconverter/curlconverter) | 8,2k | MIT | `curl` → code dans 30+ langages (push 2026-03, à surveiller) |
| [openapi-generator](https://github.com/OpenAPITools/openapi-generator) | 26,9k | Apache-2.0 | Spec OpenAPI → client typé, 50+ générateurs |
| [Schemathesis](https://github.com/schemathesis/schemathesis) | 3,8k | MIT | Tests basés sur les propriétés + fuzzing depuis la spec = oracle |
| [Playwright MCP](https://github.com/microsoft/playwright-mcp) | 39,4k | Apache-2.0 | Navigateur piloté par l'agent (capture reproductible) |
| [chrome-devtools-mcp](https://github.com/ChromeDevTools/chrome-devtools-mcp) | 55,0k | Apache-2.0 | MCP officiel Chrome DevTools (fournisseur `chrome-mcp` de RAE) |
| [agent-browser](https://github.com/vercel-labs/agent-browser) | 48,3k | Apache-2.0 | CLI de navigation pour agents, utilisée par RAE |
| [browser-use](https://github.com/browser-use/browser-use) | 120,4k | MIT | Agent navigateur autonome (alternative au mode `agent`) |
| [Stagehand](https://github.com/browserbase/stagehand) | 26,0k | MIT | Automatisation navigateur pilotée par LLM |
| [Skyvern](https://github.com/Skyvern-AI/skyvern) | 23,4k | **AGPL-3.0** | Workflows web automatisés (hors livrable) |
| [Firecrawl](https://github.com/mendableai/firecrawl) | 200,1k | **AGPL-3.0** | Site → markdown/JSON (pas de client) |
| [Crawl4AI](https://github.com/unclecode/crawl4ai) | 87,1k | Apache-2.0 | Idem, alternative Apache |

`reverse-api-engineer` (sigle RAE) **n'est pas** `morluto/rea` (REA) : homonymie
seule. Le premier reconstruit des API HTTP, le second orchestre de l'analyse de
binaires.

Pourquoi c'est le meilleur candidat du lot pour un projet de proxy :
c'est le seul qui produise un **artefact vérifiable et jetable** (un client
typé) à partir d'une observation réseau, avec un mode non interactif
(`agent --json`, `run <run_id> --file`) utilisable en CI.

Trois précautions :

1. **Secrets.** Le HAR contient cookies, jetons et en-têtes d'authentification ;
   ils sont envoyés au fournisseur du modèle. Toute capture sur une session
   authentifiée doit être faite avec un compte jetable, ou après filtrage du
   HAR. C'est le risque principal, et il n'est pas documenté par le projet.
2. **Exécution.** Le client généré s'exécute localement : le relire, comme le
   recommande le projet.
3. **Détection de bots.** Les sites agressifs bloquent la capture (limite admise
   par le projet) ; `manual` reste le repli.

**La chaîne complète, et qui fait quoi :**

```text
capture   mitmproxy · RAE · chrome-devtools-mcp · Playwright MCP
   ↓
spec      mitmproxy2swagger        (HAR/mitmproxy -> OpenAPI 3)
   ↓
client    openapi-generator · RAE  (spec/HAR -> code typé)
   ↓
oracle    Schemathesis             (propriétés + fuzz sur la spec)
```

C'est le pendant exact de `SPEC.md` → reconstruction → oracle du monde binaire :
la spec est l'artefact intermédiaire, et l'oracle est ce qui prouve que le
client reconstruit est correct. RAE couvre capture + client en un outil ;
`mitmproxy2swagger` + `openapi-generator` + `Schemathesis` couvrent les mêmes
étapes en pièces détachées, plus vérifiables et scriptables.

Pour ce dépôt, l'usage pertinent n'est pas un site tiers mais la comparaison
d'un client reconstruit avec le comportement attendu de l'API amont — donc en
complément des mesures déjà consignées, jamais à leur place.

### 4.7 IA de décompilation (côté binaire)

| Outil | ★ | Licence | Apport |
|---|---|---|---|
| [LLM4Decompile](https://github.com/albertan017/LLM4Decompile) | 7,3k | MIT | Modèles spécialisés binaire → C, en post-traitement d'un décompilateur classique |

C'est la brique « LLM » du monde binaire, symétrique de RAE côté API : elle
améliore la lisibilité, elle ne prouve rien. À confronter à un oracle
(BinDiff, tests) avant toute conclusion. Dernier push 2026-02 : vérifier la
compatibilité avec les binaires récents avant adoption.

---

## 5. Le problème de l'orchestration MCP

La liste vend « un seul MCP qui se branche sur tout ». Les faits :

- **Chaque outil MCP ajoute son schéma au contexte.** REA en expose plusieurs
  dizaines ; GhidraMCP en expose ~20. Sur un modèle à contexte moyen, cela
  consomme une part significative de la fenêtre avant même la première analyse,
  et augmente les erreurs de sélection d'outil.
- **Les MCP de décompilateur sont liés à une GUI** (GhidraMCP, GhidrAssistMCP,
  ida-pro-mcp) : il faut garder l'IDE ouvert, ce qui casse l'automatisation en
  CI et la reproductibilité.
- **Les alternatives headless sont plus fiables** : `analyzeHeadless` +
  `pyghidra`, `radare2 -A -qc`, `r2ghidra`, scripts angr/Frida. Un agent qui
  appelle un script déterministe obtient le même résultat deux fois ; un agent
  qui pilote une GUI, non.
- **Le compromis raisonnable** : 1 MCP de décompilation (ReVa ou
  `radare2-mcp`), 1 MCP réseau (mitmproxy via `mitmdump`), et des scripts
  versionnés pour tout le reste.

---

## 6. Fiabilité de la source

Constats vérifiés le 2026-11-09 sur le compte `morluto` :

- **100 dépôts publics**, dont **88 créés le même jour (2026-10-09)** ;
  **96 sur 100 ont moins de 10 étoiles**, majoritairement des `awesome-*`,
  `*-skills`, `*-mcp-list`. C'est un motif de référencement, pas un studio de
  20 outils.
- **`rea` : 446 000★** et **`awesome-reverse-engineering` : 2 000★** ; le
  reste de l'écosystème (Kuna sur le compte de l'auteur : 6★ — le vrai dépôt,
  Noelo-Lab/kuna, en a 854) est de taille normale.
- **`awesome-ai-reverse-engineering`** (93★) annonce 117 dépôts « lus et notés
  par Agent Skills Hub », un site affilié qui met en avant REA. Ce n'est pas
  une évaluation indépendante.
- **Kuna** est publié par **Noelo Lab (University of Georgia)** ; **DecBench**
  aussi. Le « n°1 » de Kuna est donc auto-évalué — crédible sur la méthode
  (open source, concurrents inclus), mais pas neutre.

**Conséquence pratique** : garder les outils, ignorer le classement
« meilleur » autoproclamé, et vérifier chaque brique sur *notre* cas d'usage.

---

## 7. Recommandation

### 7.1 Stack minimale (8 outils + 1 optionnel, 0 €)

```text
Triage      : capa · LIEF · Detect-It-Easy
Statique    : Ghidra headless (ou Kuna si qualité prioritaire) · radare2/rizin
Dynamique   : Frida · mitmproxy
Vérification: BinDiff · Hypothesis (+ AFL++ si binaire natif)
Web/API     : reverse-api-engineer (optionnel, compte jetable pour la capture)
```

Ajouter **IDA Pro** seulement si un budget existe et que le pseudo-code est le
goulot ; ajouter **MobSF** seulement si le mobile devient un axe ; activer
**RAE** seulement si l'objectif est un client HTTP typé — et jamais sur une
session authentifiée réelle.

### 7.2 Ordre d'adoption

1. Triage (une journée) — gain immédiat, aucun risque.
2. `r2ghidra` + scripts headless (deux jours) — supprime la dépendance GUI.
3. Oracle : BinDiff + Hypothesis sur un premier cas réel (une semaine).
4. Frida/mitmproxy sur le client cible (une semaine).
5. `reverse-api-engineer` en `manual` puis `agent --json`, sur un site sans
   authentification, pour mesurer la qualité du client généré (deux jours).
6. REA/Kuna en évaluation isolée, en dernier, si les étapes 1-5 laissent un
   manque identifié.

### 7.3 Ce qu'il faut mesurer avant d'adopter

- Taux de fonctions correctement reconstruites sur **notre** binaire cible
  (pas sur le jeu DecBench).
- Temps CPU/agent par fonction, et coût en tokens.
- Taux d'échec d'analyse (Ghidra : 10 % sur le jeu optimisé — à vérifier sur
  nos cibles).
- Reproductibilité : deux runs identiques donnent-ils le même `SPEC.md` ?

Tant que ces quatre chiffres ne sont pas mesurés, « plus performant » reste une
opinion de vendor.
