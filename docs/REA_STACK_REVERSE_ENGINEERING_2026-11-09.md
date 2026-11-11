# Stack REA — inventaire « studio d'ingénierie inverse »

**Date** : 2026-11-09
**Source** : liste transmise par l'opérateur (post « le créateur de REA vient de
lâcher 20 dépôts GitHub »), non recoupée avec les dépôts eux-mêmes.
**Statut** : inventaire de veille — rien n'est adopté, rien n'est installé.
**Portée** : cartographie des outils, évaluation, conditions d'usage dans ce
projet.
**Comparatif chiffré et alternatives** :
[`docs/REA_STACK_COMPARATIF_2026-11-09.md`](REA_STACK_COMPARATIF_2026-11-09.md)

> **Réserve de vérification.** La page `github.com/morluto/rea` n'a renvoyé
> qu'une coquille de navigation ; le README a ensuite été récupéré via
> `raw.githubusercontent.com` (MIT, TypeScript, npm `rea-agents`, moteurs
> Hopper/Ghidra/IDA/Kuna). Les capacités annoncées restent **des déclarations
> du projet**, non reproduites ici. Les licences indiquées sont indicatives et
> doivent être relues dans chaque `LICENSE` avant adoption.

---

## 1. Le pipeline annoncé

```text
binaire ──▶ REA ──▶ SPEC.md ──▶ reconstruction ──▶ oracle ──▶ retry
             │                        │                │
        (MCP, agent)            (code réécrit)    (tests qui prouvent
                                                  l'équivalence)
```

REA est présenté comme **un seul serveur MCP** qui expose l'outillage RE à un
agent, et comme le point d'entrée d'une boucle *clean-room* : l'agent produit
une spécification (`SPEC.md`), reconstruit le comportement, puis itère jusqu'à
ce que les oracles passent. Les autres dépôts sont les briques que REA pilote.

C'est la partie intéressante du lot : **l'assemblage**, pas les briques.

---

## 2. Inventaire par catégorie

### 01 — Noyau d'analyse

| Outil | Dépôt | Rôle | Licence / coût |
|---|---|---|---|
| REA | `morluto/rea` | Serveur MCP unique, orchestrateur de la boucle | à vérifier |
| Ghidra | `NationalSecurityAgency/ghidra` | Décompilateur multi-archi, GUI + `analyzeHeadless` | Apache-2.0 |
| GhidraMCP | `LaurieWired/GhidraMCP` | Plugin + pont MCP pour piloter Ghidra | à vérifier |
| ida-pro-mcp | `mrexodia/ida-pro-mcp` | Pont MCP vers IDA Pro | MIT (le pont) — **IDA Pro est payant** |

### 02 — Mobile & applications

| Outil | Dépôt | Rôle | Licence / coût |
|---|---|---|---|
| jadx | `skylot/jadx` | DEX/APK → Java, CLI + GUI | Apache-2.0 |
| Apktool | `iBotPeaches/Apktool` | Désassemble/remonte ressources + smali | Apache-2.0 |
| Frida | `frida/frida` | Instrumentation dynamique (hooks JS) | wxWindows Library Licence |
| ILSpy | `icsharpcode/ILSpy` | Décompilateur .NET | MIT |

### 03 — Binaire & bas niveau

| Outil | Dépôt | Rôle | Licence / coût |
|---|---|---|---|
| radare2 | `radareorg/radare2` | Framework RE en CLI, scriptable | LGPL-3.0 |
| x64dbg | `x64dbg/x64dbg` | Débogueur Windows user-mode | GPL-3.0 |
| ImHex | `WerWolv/ImHex` | Éditeur hexa + langage de motifs | GPL-2.0 |
| angr | `angr/angr` | Exécution symbolique (Python) | BSD-2-Clause |

### 04 — Réseau & runtime

| Outil | Dépôt | Rôle | Licence / coût |
|---|---|---|---|
| mitmproxy | `mitmproxy/mitmproxy` | Interception HTTP(S), scriptable | MIT |
| dnSpy / dnSpyEx | `dnSpyEx/dnSpy` | Débogueur + éditeur .NET (fork maintenu de dnSpy) | GPL-3.0 |
| rizin | `rizinorg/rizin` | Fork de radare2, CLI/API plus propre | LGPL-3.0 |
| raddebugger | `EpicGames/raddebugger` | Débogueur natif multi-plateforme | à vérifier |

### 05 — Reconstruction & vérification

| Élément | Dépôt | Rôle |
|---|---|---|
| awesome-ai-reverse-engineering | `zhuyansen/awesome-ai-reverse-engineering` | 122 dépôts, chacun noté |
| awesome-reverse-engineering | `morluto/awesome-reverse-engineering` | Liste maintenue par l'auteur de REA |
| workflows REA | livrés avec REA | Prompts d'investigation prêts à l'emploi |
| boucle clean-room | — | `SPEC.md` + oracles + checks jusqu'à équivalence |

### 06 — Web & API (ajout du 2026-11-09)

| Outil | Dépôt | Rôle | Licence / coût |
|---|---|---|---|
| Reverse API Engineer (RAE) | `kalil0321/reverse-api-engineer` | Site web → client API typé : navigation (manuelle ou agent), capture HAR, génération du client | MIT, Python 3.11+ |
| Playwright MCP / Vercel `agent-browser` | `microsoft/playwright-mcp`, `vercel-labs/agent-browser` | Navigateurs pilotés par l'agent utilisés par RAE | Apache-2.0 |

**Attention à l'homonymie** : `kalil0321/reverse-api-engineer` (RAE) n'a rien à
voir avec `morluto/rea` (REA). Le premier reconstruit des **API HTTP** à partir
du trafic d'un navigateur ; le second orchestre de l'**analyse de binaires**.
Seul le sigle se ressemble.

Fonctionnement de RAE :

1. une URL et un objectif (« récupérer toutes les offres d'emploi Apple ») ;
2. un navigateur visite le site — piloté à la main (`manual`) ou par un agent
   (`agent`, fournisseur `chrome-mcp` par défaut, Playwright MCP `auto`, ou le
   CLI `agent-browser`) ;
3. le trafic est capturé en **HAR** ;
4. un modèle écrit un client typé dans la langue choisie (Python, JS, TS, Go,
   Java, C#, PHP, Ruby, C, PowerShell).

Modes : `manual`, `agent`, `engineer` (régénère depuis un HAR existant),
`collector` (extraction de données structurées JSON/CSV). SDK au choix :
`claude`, `opencode`, `cursor`, `copilot` ; CLI scriptable
(`agent --json`, `run <run_id> --file`). Sorties dans
`~/.reverse-api/runs/scripts/<run_id>/` et `./scripts/<nom>/`.

Pourquoi ça compte ici : c'est la **seule brique du lot qui traite la couche
réseau/API** — celle qui intéresse un proxy — là où les 18 autres traitent du
binaire ou du mobile. Pour ce projet, l'équivalent utile est l'analyse de
l'API amont d'`opencode` (déjà couverte par mesure), pas d'un site tiers.

Réserves : (a) le HAR contient cookies, jetons et en-têtes d'authentification,
et ils partent chez le fournisseur du modèle — fuite de secrets si la session
est authentifiée ; (b) le code généré s'exécute localement et doit être relu ;
(c) un site avec détection de bots peut bloquer la capture (limite admise par
le projet).

#### Voisins de RAE — la chaîne complète

| Outil | Dépôt | ★ | Licence | Rôle dans la chaîne |
|---|---|---|---|---|
| mitmproxy2swagger | `alufers/mitmproxy2swagger` | 9,7k | **aucune licence déclarée** | Capture mitmproxy **ou HAR** → spec OpenAPI 3.0 (le `SPEC.md` de l'API) |
| curlconverter | `curlconverter/curlconverter` | 8,2k | MIT | Commande `curl` → code dans 30+ langages (dernier push 2026-03) |
| openapi-generator | `OpenAPITools/openapi-generator` | 26,9k | Apache-2.0 | Spec OpenAPI → client typé, 50+ générateurs |
| Schemathesis | `schemathesis/schemathesis` | 3,8k | MIT | Tests basés sur les propriétés + fuzzing d'une API depuis sa spec (l'oracle) |
| chrome-devtools-mcp | `ChromeDevTools/chrome-devtools-mcp` | 55,0k | Apache-2.0 | MCP officiel Chrome DevTools — fournisseur `chrome-mcp` par défaut de RAE |
| browser-use | `browser-use/browser-use` | 120,4k | MIT | Agent navigateur autonome (alternative au mode `agent` de RAE) |
| Stagehand | `browserbase/stagehand` | 26,0k | MIT | Automatisation navigateur pilotée par LLM (alternative) |
| Skyvern | `Skyvern-AI/skyvern` | 23,4k | **AGPL-3.0** | Automatisation de workflows web (hors livrable si AGPL) |
| Firecrawl | `mendableai/firecrawl` | 200,1k | **AGPL-3.0** | Site → markdown/JSON pour agents (pas de génération de client) |
| Crawl4AI | `unclecode/crawl4ai` | 87,1k | Apache-2.0 | Idem, alternative Apache |

Chaîne recommandée : **capture** (mitmproxy ou RAE ou `chrome-devtools-mcp`) →
**spec** (`mitmproxy2swagger`) → **client** (`openapi-generator` ou RAE) →
**oracle** (`Schemathesis`). C'est le pendant exact de la boucle
`SPEC.md`/oracle du monde binaire, et c'est reproductible.

Deux réserves : `mitmproxy2swagger` n'a **aucune licence déclarée** (usage
local toléré en pratique, redistribution à éviter) ; `Firecrawl` et `Skyvern`
sont AGPL-3.0, donc hors du livrable.

---

## 3. Évaluation

### 3.1 Ce qui est solide

- **Les briques sont les standards du domaine**, pas des nouveautés : Ghidra,
  r2/rizin, Frida, jadx, mitmproxy couvrent 90 % des besoins réels. La liste
  est un bon index de départ, à jour sur les forks (dnSpyEx, rizin,
  raddebugger).
- **Le cadrage par la spécification est le bon réflexe.** `SPEC.md` +
  oracle + retry est la seule manière de rendre une reconstruction vérifiable.
  Un décompilateur seul produit du code plausible, pas du code prouvé.
- **Les listes « awesome » notées** sont utiles pour trier le bruit du
  domaine, qui est considérable.

### 3.2 Ce qui est surfait

- **« Pointez-le sur n'importe quelle app et regardez-la s'expliquer toute
  seule »** est du marketing. Un binaire obfusqué, strippé ou généré (Go, Rust,
  Bun) ne « s'explique » pas ; la sortie d'un LLM sur du pseudo-code C est une
  hypothèse, pas un fait. Le coût réel est dans les oracles, qui sont
  spécifiques au domaine et ne se génériquent pas.
- **« Un seul MCP qui se branche sur tout » est un anti-pattern à l'échelle.**
  Chaque outil exposé ajoute des tokens de schéma, des collisions de noms et
  des erreurs de sélection. En pratique un agent pilote mal une GUI
  (Ghidra, x64dbg) ; le mode headless/CLI (`analyzeHeadless`, `r2 -A -qc`,
  scripts angr/Frida) reste plus fiable et plus reproductible qu'un MCP qui
  clique.
- **20 dépôts ≠ 20 outils matures** : plusieurs sont mono-mainteneur, certains
  archivés (dnSpy), et la valeur ajoutée de l'ensemble tient à la glue, qui est
  la partie la moins vérifiable sans lire le code.

### 3.3 Risques concrets

| Risque | Détail | Parade |
|---|---|---|
| Chaîne d'approvisionnement | un serveur MCP **exécute du code arbitraire** et parle à un LLM ; un plugin Ghidra/IDA a les mêmes droits que l'IDE | épingler des commits, vendoriser, lire le pont avant de l'installer, exécuter en VM |
| Licence | IDA Pro est commercial ; dnSpy/dnSpyEx, x64dbg, ImHex sont GPL → contamination si redistribution d'un dérivé | ne pas embarquer de code GPL dans le proxy ; garder les outils hors du produit livré |
| Juridique / ToS | le RE d'un binaire tiers peut violer un contrat (EULA, DMCA §1201, droit d'auteur) indépendamment de la légalité de l'outil | limiter aux binaires dont l'analyse est autorisée ; documenter la base légale |
| Exécution | analyse = exécution de code non fiable (émulateurs, échantillons, APK) | VM jetable, réseau isolé derrière mitmproxy, snapshots, pas d'accès aux secrets du proxy |

### 3.4 Pertinence pour ce projet

Le projet est un **proxy Python** ; il n'a pas besoin d'un MCP d'ingénierie
inverse pour tourner. L'usage pertinent est ailleurs :

1. **Analyser le client officiel** (`opencode`, binaire Bun) pour confirmer les
   constantes de gate/identité que nous obtenons aujourd'hui par mesure
   (`docs/REPO_INTEL_2026-11-06.md`, `docs/AUDIT_REPOS_2026-11-06.md`). Là,
   Ghidra headless + r2/rizin suffisent ; REA n'apporte que l'orchestration.
2. **Vérifier nos hypothèses de format** (SSE, `<env>`, session canonique) par
   lecture du binaire plutôt que par essai-erreur — uniquement si un doute
   subsiste, car la mesure live reste la preuve la plus forte.
3. **Analyser un APK/un client mobile** si un client non officiel devient
   pertinent (jadx + Frida + mitmproxy).

Le reste de la liste (x64dbg, ImHex, angr, raddebugger, ILSpy/dnSpy) est hors
périmètre pour ce dépôt : à garder en veille, pas à installer.

### 3.5 Ce que je garderais si on devait trancher

Quatre outils, pas vingt : **Ghidra headless**, **r2 ou rizin**, **Frida**,
**mitmproxy**. C'est le socle qui produit des preuves reproductibles ; le reste
est du confort ou du bruit.

---

## 4. Conditions d'usage

- **Hors production.** Jamais sur la machine qui sert les stations ; VM
  dédiée, réseau isolé, aucun secret du proxy monté.
- **Épinglé.** Commit SHA dans un fichier de lock, pas de `latest`.
- **Hors du livrable.** Aucun outil GPL n'entre dans le code distribué du
  proxy ; ce sont des outils d'analyse, pas des dépendances.
- **Preuve avant croyance.** Toute conclusion issue d'une décompilation doit
  être confirmée par une mesure live (probe, capture mitmproxy, test) avant
  d'entrer dans une spec.

---

## 5. Prochaines étapes (si adoption)

- [x] Lire `morluto/rea` (README récupéré le 2026-11-09 : MIT, TypeScript,
      npm `rea-agents`, moteurs Hopper/Ghidra/IDA/Kuna, parseurs intégrés
      Go/Rust/IL2CPP/PE/ELF/APK/.NET) — reste à auditer le code des ponts MCP.
- [ ] Épingler les commits des 4 outils retenus dans un `docs/` dédié.
- [ ] Monter une VM d'analyse isolée (pas d'accès au réseau des stations).
- [ ] Premier cas d'usage : confirmer une constante de gate du client officiel
      par Ghidra headless, et consigner le résultat dans
      `docs/REPO_INTEL_2026-11-06.md`.
- [ ] Décider ensuite si REA apporte quelque chose par rapport à des scripts
      headless directs — sinon s'en tenir aux scripts.
