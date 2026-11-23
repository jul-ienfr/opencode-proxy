# Revue d'architecture et décision de langage

**Feature** : `024-audit-optimisation-globale`
**Date** : 2026-11-20
**Question posée** : « Je voudrais qu'on revoie également dans ce plan que tu
vas me faire intégralement l'architecture. Voire même, je pensais peut-être
passer en Go ou en Rust, dis-moi ce que tu en penses à ce sujet. »

---

## 1. L'architecture actuelle, telle qu'elle est

### 1.1 Le graphe réel des composants

```
┌─────────────────────────────────────────────────────────────────────┐
│  Client (Claude Code, Codex, autres)                                │
└──────────────────────────┬──────────────────────────────────────────┘
                           │ Anthropic Messages / OpenAI Chat / Responses
                           ▼
┌─────────────────────────────────────────────────────────────────────┐
│  opencode.py :4000                          [Python, 33 618 lignes] │
│  ├─ 34 routes (proxy + API opérateur)                               │
│  ├─ conversion de protocole  ──► app/protocol/  [6 329 l. mapping]  │
│  ├─ compaction               ──► app/compaction/ [3 000 l.]         │
│  ├─ streaming SSE            ──► streaming/      [2 136 l.]         │
│  ├─ télémétrie               ──► observability/  [3 389 l.]         │
│  └─ jambe free               ──► free/           [45 246 l.]        │
│       └─ free/pool/__init__.py [8 362 l.]  ← sélection + quota      │
└──────────┬────────────────────────────────────┬─────────────────────┘
           │ docker compose (8 sites, 0 profil)  │ HTTP /v1/vpn/*
           ▼                                     ▼
┌──────────────────────────────┐   ┌──────────────────────────────────┐
│  Superviseur                 │   │  Fork Gluetun 016                │
│  fresh_chain_supervisor.py   │──►│  [Go, 110 580 lignes]            │
│  [Python, 3 294 l.]          │   │  :8001..8006 par station         │
│  + vpn/manager.py [11 471 l.]│   │  transports, DNS, egress, jobs   │
└──────────┬───────────────────┘   └──────────────────────────────────┘
           │ SQLite WAL
           ▼
┌─────────────────────────────────────────────────────────────────────┐
│  registry.db (52 tables, 159 Mo)   requests.db (159 Mo)             │
│  ⚠ 6 déclarants de schéma · 15 179 violations FK · 7 tables non     │
│    purgées · 5 tables multi-déclarants                              │
└─────────────────────────────────────────────────────────────────────┘

  Egress réels :  6 stations NordVPN (Gluetun fork)  ·  12 conteneurs WARP
                  ·  ~30 000 proxys publics         ·  direct
```

### 1.2 Ce que cette architecture fait bien

Il faut le dire avant de proposer des changements :

| Force | Preuve mesurée |
|---|---|
| **Le fork Go est le bon choix** | 110 580 lignes de Go, opérationnelles, pour le composant le plus critique (le tunnel). Go est déjà le langage du système là où ça compte. |
| **La séparation des responsabilités est correcte** | app (preuve client) / superviseur (décisions) / fork (tunnel) — le principe VIII est **respecté** (vérifié par 2 auditeurs). |
| **Le domaine pur est réellement pur** | `free/egress_catalog.py` : stdlib seule, aucune I/O SQLite. |
| **La suite de tests est saine** | 6 379/6 385, 0 `random` non seedé, 0 port fixe, 201 fichiers sous mock. |
| **Aucun trou de sécurité classique** | 0 injection SQL, 0 XSS, 0 secret en clair (vérifié par grep exhaustif). |
| **Le budget de latence est tenu** | plancher de 14-15 ms, contre une cible de 30 ms. |

### 1.3 Ce qui est cassé dans l'architecture

Cinq défauts **structurels**, pas cosmétiques :

**1. Il n'y a pas d'autorité d'état.**
Cinq surfaces publient l'état d'une station (`station_state`, `pins`, `.env`,
`vpn_state*.json`, `free_pool.json`), et elles **divergent** : `free_pool.json`
déclare 6 stations `usable`, l'API en voit 1. `/api/vpn-status` ne lit jamais
`station_state`. L'audit 019 avait déjà identifié ce défaut (P0-02) ; il s'est
**aggravé** (16 → 20 stations annoncées).

**2. Il n'y a pas de propriétaire de schéma.**
`registry.db` a **52 tables**, dont **22 créées hors `schema.sql`** par **15
modules**. **5 tables** ont des déclarants multiples avec des DDL divergentes
(`station_state` a un `CHECK` dans un cas, pas dans l'autre). Le `IF NOT
EXISTS` rend chaque conflit silencieux.

**3. Les monolithes ne sont pas des monolithes hérités.**
`free/pool/__init__.py` a été **créé le 2026-11-11** — après la constitution —
et fait **8 362 lignes**, avec **+276 lignes en 8 commits** sans extraction.
Ce n'est pas de la dette historique : c'est la règle XIV qui n'est pas
appliquée au moment d'écrire.

**4. Les gates ne mesurent pas ce qu'elles prétendent.**
Le gate option-map inspecte **1 page sur 15** (contrôle circulaire : il filtre
sur le marqueur qu'il vérifie). `platform_audit --strict` n'est invoqué par
aucun pipeline. La CI tourne sur **un seul OS** alors que XV en exige deux.

**5. Des ressources critiques vivent hors du dépôt.**
L'image du fork, le port du control plane et les variables `CONTROL_*` sont
dans **6 overlays gitignorés**. Un `git clone` neuf ne reproduit pas la
flotte.

---

## 2. La question du langage — analyse

### 2.1 Le raisonnement naïf, et pourquoi il est faux

L'intuition derrière « passer en Go ou Rust » est que **Python est lent**. Il
faut la tester contre les mesures.

**Où passe le temps, réellement** (`requests.db`, 2 000 dernières requêtes) :

| Poste | Durée | Part de la médiane |
|---|---|---|
| **Médiane bout en bout** | **1 203 ms** | 100 % |
| Génération du modèle (`deepseek`) | **16 600 ms** | (sur les requêtes concernées) |
| Attente d'un `429` avant retry | **11 500 ms** | — |
| Attente d'un `503` | **19 700 ms** | — |
| **Plancher du proxy (Python)** | **14 ms** | **1,2 %** |

**Le proxy Python coûte 14 ms sur une requête médiane de 1 203 ms.**

Ces 14 ms incluent : réception, parsing JSON, conversion de protocole, appel
amont, reconstruction SSE, télémétrie. **C'est déjà sous la cible de 30 ms
fixée par la spec 023.**

### 2.2 Ce qu'un portage gagnerait

| Poste | Gain réel |
|---|---|
| Plancher proxy (14 ms → ~2 ms) | **1,2 %** de la médiane |
| Génération du modèle | **0 %** — c'est le serveur distant |
| Retries d'échec (11,5 s / 19,7 s) | **0 %** — c'est de l'attente réseau |
| Démarrage (budget 1 200 ms) | gain réel mais marginal pour un service long-running |
| **Concurrence (GIL → goroutines)** | **réel, non mesuré** — 100 flux visés, 42 787 requêtes servies |

**Un seul poste est un vrai argument : la concurrence.** 181 `async def`,
73 points de `asyncio.to_thread`, un GIL. C'est là — et seulement là — que Go
apporterait quelque chose de structurel.

### 2.3 Ce qu'un portage coûterait

| Poste | Volume |
|---|---|
| Python de production | ≈ **143 000 lignes** |
| Tests Python | **107 377 lignes** |
| **Total à porter** | ≈ **250 000 lignes** |
| Modules à découper **avant** de pouvoir porter | `opencode.py` 33 618 · `vpn/manager.py` 11 471 · `free/pool/__init__.py` 8 362 · `app/protocol/mapping.py` 6 329 · + 5 autres |

**Le bloqueur non négociable : `curl_cffi`.**

| Mesure | Valeur |
|---|---|
| Usages de `curl_cffi` / `impersonate` | **281** occurrences, 61 dans `opencode.py` |
| Empreinte à reproduire | `ja3_md5 = 1523504b38f0fae0d881d4b6554aac1b` |
| Conséquence d'une erreur | **une IP brûlée** (principe XVIII) |

Toute la légitimité de la jambe free repose sur l'émission d'un ClientHello
au JA3 **exact** de la capture officielle. En Go, `utls` sait le faire ; en
Rust, `rquest`/`boring` aussi — mais c'est **la** partie à valider en premier,
et une erreur se paie en IP brûlées.

### 2.4 Le fait qui change la perspective

**Go est déjà dans le projet : 110 580 lignes**, dans `gluetun/`, sur la
branche `016-control-plane`. C'est **plus que tout le Python de production
hors monolithe** :

| Composant | Langage | Lignes |
|---|---|---|
| **Fork Gluetun** | **Go** | **110 580** |
| `free/` | Python | 45 246 |
| `opencode.py` | Python | 33 618 |
| `scripts/` | Python | 28 042 |
| `app/` + `dashboard/` | Python | 24 195 |
| `vpn/manager.py` | Python | 11 471 |

**Le projet est déjà polyglotte, et Go y tient le composant le plus
critique.** La question n'est donc pas « Python ou Go ? » mais « **quel
composant doit rejoindre Go ?** ».

---

## 3. Recommandation

### 3.1 Ne pas porter l'ensemble

**Trois raisons mesurées :**

1. **Le gain est de 1,2 %** sur la métrique qui compte. Le temps est dans la
   génération du modèle et dans les retries d'échec — deux postes qu'un
   changement de langage ne touche pas.

2. **Le coût est de ~250 000 lignes** avec un bloqueur non négociable
   (l'empreinte JA3 exacte). Une erreur brûle des IP.

3. **Aucun des 5 P0 n'est un problème de langage.** Le probe qui lit la
   mauvaise clé, la fonction définie après `__main__`, l'`elif` dupliqué, les
   deux schémas `ip_state`, les overlays gitignorés — **porter ce code en Go
   reproduirait ces défauts à l'identique**, avec 250 000 lignes de
   réécriture en plus.

**Un portage maintenant, c'est payer 250 000 lignes pour 1,2 %.**

### 3.2 Ce qu'il faut faire à la place

**Étape 1 — Découper (déjà planifié, Phase 2).**
Les plans de découpage existent : **17 lots** pour `opencode.py`, **15
modules** pour `vpn/manager.py`, **8 modules** pour `free/pool`. Après
découpage, chaque module est **portable indépendamment**.

**Étape 2 — Mesurer (Phase 3).**
Après découpage, profiler les modules chauds. Si un module montre un coût CPU
significatif, il devient un **candidat de portage isolé** — pas une réécriture
big-bang.

**Étape 3 — Porter ce qui le mérite, si c'est prouvé.**
Le candidat le plus probable est le **chemin chaud de sélection**
(`free/pool`, 8 362 l. → 8 modules). C'est le seul endroit où :
- le code tourne **à chaque requête** ;
- la logique est **pure** (tri, score, sélection) donc portable sans I/O ;
- Go apporterait la concurrence **et** la vitesse.

**Étape 4 — Aller vers Go, pas Rust, si on y va.**
Go est **déjà** le langage du fork (110 580 l.). Ajouter du Rust créerait un
**troisième** langage pour un gain marginal sur Go, avec un écosystème
d'empreinte TLS moins mature (`rquest` vs `utls`).

### 3.3 Ce qui changerait la recommandation

| Si… | alors |
|---|---|
| le proxy devait servir **> 1 000 requêtes/s** | la concurrence (GIL) deviendrait le goulot → Go |
| un module montrait **> 50 % de CPU** en profilage | portage isolé de ce module, avec test de parité |
| la latence médiane tombait **sous 100 ms** (modèles locaux) | les 14 ms deviendraient significatifs (14 %) |
| `curl_cffi` était remplacé par un besoin d'empreinte plus simple | le bloqueur principal tombe |
| l'opérateur voulait **distribuer** le proxy | un binaire Go unique serait un vrai avantage de packaging |

**Aucune de ces conditions n'est vraie aujourd'hui.** Mais elles sont
mesurables, et le plan les rend vérifiables.

### 3.4 Une réponse en une phrase

> **Non, ne portez pas.** Le proxy coûte 14 ms sur 1 200 ms (1,2 %), Go est
> déjà là où il faut (110 580 lignes dans le fork), les cinq P0 sont des
> défauts de logique et non de langage, et le bloqueur (`curl_cffi`/JA3) peut
> brûler des IP. **Découpez d'abord** — après, si un module prouve son coût,
> portez **ce module** en Go, avec un test de parité.

---

## 4. L'architecture cible (après le plan)

```
┌─────────────────────────────────────────────────────────────────────┐
│  main.py  [≤ 150 l.]  ──►  server/lifespan.py  ──►  server/manager.py│
│  (sort le bloc __main__ du monolithe ⇒ P0-1 et P0-2 disparaissent    │
│   structurellement)                                                  │
└──────────────────────────┬──────────────────────────────────────────┘
                           ▼
┌─────────────────────────────────────────────────────────────────────┐
│  server/handlers/  messages.py · chat.py · responses.py · systemone  │
│  server/request_preamble.py   (prologue commun, extrait 3×)          │
│  upstream/transport.py · streaming/collectors.py                     │
│  ── chaque module ≤ 1 500 lignes ──                                  │
└──────────────────────────┬──────────────────────────────────────────┘
                           ▼
┌─────────────────────────────────────────────────────────────────────┐
│  free/  (8 modules de pool + wire + sessions + cooldown + preflight) │
│  free/providers/  nordvpn · warp · proxies · direct  (un adapter)    │
│  free/ip_state.py  ── machine à états UNIQUE (legacy fusionné)       │
│  free/wire_admission.py  ── BRANCHÉ (garde-fou XVIII)                │
└──────────────────────────┬──────────────────────────────────────────┘
                           ▼
┌─────────────────────────────────────────────────────────────────────┐
│  registry.db  ── SEULE autorité d'état                               │
│  · 1 déclarant de schéma (schema.sql + migrations versionnées)       │
│  · foreign_keys=ON partout, 0 violation                              │
│  · rétention exécutée sur TOUTES les tables > 10 000 lignes          │
│  · 0 valeur monotonic persistée                                      │
└─────────────────────────────────────────────────────────────────────┘

  observability/state_authority.py  ── une seule surface, testée
  deploy/compose/  ── overlays VERSIONNÉS (sortis de logs/)
  core/process.py  ── wrapper subprocess unique (18 sites migrés)
```

**Les quatre changements structurants :**

1. **Une autorité d'état** (`observability/state_authority.py`), avec un test
   de non-divergence permanent (INV-7).
2. **Un propriétaire de schéma** (`schema.sql` + migrations), avec un gate
   qui interdit tout `CREATE TABLE` ailleurs (INV-1).
3. **Des modules bornés** (≤ 1 500 l.), garantis par un gate (INV-10).
4. **Zéro ressource hors du dépôt**, garanti par un gate (INV-8).

Ces quatre propriétés sont ce qui manque aujourd'hui, et ce qui permet à
soixante findings de coexister : sans autorité, sans propriétaire, sans borne
et sans gate, chaque correctif local crée un nouveau défaut ailleurs.
