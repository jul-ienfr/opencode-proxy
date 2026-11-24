# Audit complet — 2026-11-20

**Périmètre** : la jambe gratuite (free, NordVPN, WARP, proxys publics), le
proxy, et l'ensemble du projet.
**Méthode** : 10 auditeurs indépendants en **lecture seule**, puis
**vérification croisée** des findings les plus lourds par l'auditeur principal
(commandes rejouées, mesures SQL indépendantes).
**Volume** : **12 479 lignes**, **34 P0 · 102 P1 · 98 P2 · ~120 P3**.

**Plan d'exécution** : [`specs/024-audit-optimisation-globale/`](../../../specs/024-audit-optimisation-globale/)

---

## Par où commencer

| Vous voulez… | Lisez |
|---|---|
| **le diagnostic en 5 minutes** | [00-live-evidence.md](./00-live-evidence.md) §1–§3 |
| **savoir ce qui est cassé et pourquoi** | [plan.md](../../../specs/024-audit-optimisation-globale/plan.md) §1 |
| **comprendre les causes racines** | [research.md](../../../specs/024-audit-optimisation-globale/research.md) §1 |
| **la décision Go/Rust** | [architecture-review.md](../../../specs/024-audit-optimisation-globale/architecture-review.md) |
| **exécuter** | [tasks.md](../../../specs/024-audit-optimisation-globale/tasks.md) |
| **les tests à écrire** | [contracts/invariants.md](../../../specs/024-audit-optimisation-globale/contracts/invariants.md) |

---

## Les quatre documents de l'auditeur principal

| Document | Contenu | Pourquoi il existe |
|---|---|---|
| [00-live-evidence.md](./00-live-evidence.md) | Mesures directes : état de la flotte, bases, latences, langage | Les chiffres de référence, tous datés |
| [00-verifications-croisees.md](./00-verifications-croisees.md) | **66 vérifications indépendantes** des findings des auditeurs | Ne pas dépendre du seul jugement d'un sous-agent |
| [00-couverture-audit.md](./00-couverture-audit.md) | **Ce qui n'a PAS été audité** (43 fichiers, 9,7 %) | Un audit qui prétend 100 % de couverture mentirait |
| [00-specs-state.md](./00-specs-state.md) | État des 23 specs SpecKit (≈ 477 tâches ouvertes) | Le projet a un passif de tâches non closes |
| [00-stash-inventaire.md](./00-stash-inventaire.md) | Les 35 fichiers jamais commités du `git stash` | Un audit de 755 lignes dormait depuis 6 jours |

## Les dix rapports d'auditeurs

| # | Périmètre | P0 | P1 |
|---|---|---|---|
| [01](./01-free-core.md) | Cœur de la jambe free (`free/`, 45 246 l.) | 4 | 15 |
| [02](./02-free-registry-control-plane.md) | Registre, control plane, qualification | **8** | **20** |
| [03](./03-nordvpn-egress.md) | NordVPN, stations, `vpn/manager.py` (11 471 l.) | 2 | 8 |
| [04](./04-warp-egress.md) | WARP, profils wire, catalogue egress | 3 | 9 |
| [05](./05-public-proxies.md) | Proxys publics, sources, validation | 4 | 12 |
| [06](./06-opencode-monolith.md) | Le monolithe `opencode.py` (33 618 l.) | 3 | 7 |
| [07](./07-protocol-app.md) | Protocole, conversion, compaction, streaming | 4 | 13 |
| [08](./08-dashboard-observability-ops.md) | Dashboard, GUI, observabilité, ops | 2 | 8 |
| [09](./09-config-scripts-dette.md) | Configuration, scripts, dette, hygiène | 3 | 7 |
| [10](./10-tests-ci.md) | Suite de tests (6 385) et CI | 3 | 8 |

---

## Les cinq découvertes les plus graves

1. **La flotte survit par accident maintenu.** `docker-compose.yml` (versionné)
   déclare l'image *upstream* pour 5 des 6 stations ; seuls 6 overlays
   **gitignorés** dans `logs/` portent l'image du fork. Le mécanisme de
   rattrapage côté code n'a **aucun appelant de production**. Une purge de
   `logs/` — que deux rapports recommandent — **détruirait la configuration du
   control plane**.

2. **Trois causes convergentes du blocage** (1/6 stations utilisables) : le
   probe post-rotation WARP lit `capture["body"]` au lieu de `body_raw`
   (**3 jours sans rotation réussie**), `repair_owner: supervisor` délègue
   **sans vérifier que le superviseur vit**, et `_draining_stations` retire une
   station **définitivement** à chaque 429.

3. **Contournement de la porte géo.** Un `elif _geo_tunnel` **dupliqué**
   (`opencode.py:31595`) rend la route tunnel inatteignable, alors que
   `config.yaml` exige `require_vpn: true`. Le trafic sort **en direct**.
   *Précision (V-60)* : le motif apparaît **7 fois** dans le fichier et
   **6 sites sur 7 sont corrects** — c'est une **régression isolée** sur la
   porte `responses`, et le correctif est une **copie** du patron des autres
   sites.

4. **Le correctif anti-autocompact est annulé sur `/v1/responses`.**
   Vérifié par exécution : `_extract_cache_tokens` retourne **0** au lieu de
   900 sur la forme Responses. C'est exactement le bug documenté dans le
   `CLAUDE.md` du projet.

5. **502 requêtes perdues** par un `NameError` avalé : `_resp_json_hors_boucle`
   est défini **après** le bloc `__main__` bloquant. Mesuré en base :
   502 `HTTP 502`, tous non-stream (6,2 % du chemin).

---

## Ce que l'audit a corrigé dans l'audit

Un audit qui ne se contredit pas lui-même n'a pas été vérifié.

| Cas | Verdict |
|---|---|
| `web_search` / `web_fetch` « inertes » | ❌ **faux positif** — `opencode.py:19721-19908` les consomme ; corroboré par 2 autres auditeurs |
| `ip_registry.selection.*` « sans consommateur » | ⚠️ **conclusion hâtive** (dont la mienne) — les consommateurs **existent mais ne sont pas branchés** |
| 78 tests de `tests/unit` « non collectés » | ⚠️ **inexact** — ils **sont** collectés ; ils ne sont pas **versionnés** |
| `vpn/manager.py` « viole XVI » | ⚠️ **nuancé** — il **pose bien** `CREATE_NO_WINDOW` ; c'est la *duplication* de politique qui est en cause |

**Bilan : 66 vérifications, 60 findings confirmés, 1 faux positif, 2
conclusions corrigées, 1 nuance.** Les rapports sont fiables ; les écarts
venaient de greps incomplets ou de conclusions tirées trop vite.

**Tous les 34 P0 ont été vérifiés personnellement** par l'auditeur principal
— commande rejouée, mesure SQL indépendante, ou exécution Python.

---

## Les rapports de workflow (exécution du plan)

L'audit a produit un plan de 124 tâches ; leur exécution est vérifiée par des
workflows d'agents, chacun suivi d'un **réfuteur adversarial** indépendant.
Les rapports qui en sortent sont aussi importants que l'audit : ils disent ce
qui a été corrigé **et ce qui ne l'a pas été**.

| Rapport | Objet | Bilan |
|---|---|---|
| [00-rapport-workflow.md](./00-rapport-workflow.md) | Premier lot (P0) | 3 confirmés, 4 réfutés |
| [00-rapport-workflow-p1.md](./00-rapport-workflow-p1.md) | Lot P1 (7 groupes) | 3 confirmés, **1 réfuté** (régression de 2 608 octets), 3 échecs d'infra |
| [00-rapport-workflow-p1-r2.md](./00-rapport-workflow-p1-r2.md) | Lot P1 round 2 | **20 tests cassés** par un correctif qui **ne fermait pas** le défaut qu'il citait ; puis **46 → 64 slots** récupérés |
| [00-protocole-suite-pendant-workflow.md](./00-protocole-suite-pendant-workflow.md) | Méthode | 3 suites ont produit **7 échecs faux** ; **validé** : la 4ᵉ, sur arbre au repos, rend **6 407 tests / 0 échec** |

Le round 2 vaut d'être lu pour une raison de méthode : le correctif T137
(« proxys `http://` en clair promus `usable` ») a été appliqué au **point
d'entrée** (la découverte) en laissant le **point de sortie** (le service),
alors que l'audit portait explicitement sur les options **déjà en base**.
C'est la **huitième** occurrence du motif central — *un correctif qui semble
bon et ne traite pas le défaut qu'il nomme*.

**Trois mesures qui valent d'être retenues** (2026-11-21) :

| Mesure | Valeur | Conséquence |
|---|---|---|
| Fenêtre du pool non sur-échantillonnée | `select_many(64)` → **46 servis** ; `(128)` → **64** | le pool tournait à **72 %** de sa capacité ; corrigé → **+39 %**, coût nul |
| Doublons d'URL à la sonde | 1 964 options → **847 URL** (1 117 doublons) | **57 % du budget de sonde** gaspillé ; une URL répétée **66 fois** |
| T150 (réfuté) | **21 outils sur 60** au boot à froid | 39 outils absents des menus ; le commentaire « 5000 suffisent » est faux |

**Un agent est mort en 503 après avoir corrigé son propre bug** et sans
pouvoir le signaler — c'est le seul groupe du lot sans réfuteur. Ses 3 tâches
ont été vérifiées par rejeu (§12 du rapport).

**Le protocole adversarial a corrigé quatre erreurs de l'auditeur lui-même**
(§22 du rapport) : un plafond de pool confondu (64 vs 3), un test qui
n'entrait jamais dans la branche qu'il gardait, une attribution fausse
(« pré-existant » alors qu'introduit par ce lot), et deux assertions
auto-référentielles. **Sur cinq erreurs, l'auteur n'en avait vu qu'une.**
C'est la démonstration la plus directe de la valeur du dispositif — et la
raison pour laquelle les 34 P0 de cet audit ont tous été rejoués.

---

## Les points sains (à ne pas casser)

Il ne faut pas reconstruire ce qui fonctionne :

- Suite de tests **6 379 / 6 385** en 8 min 48 ; 0 `random` non seedé,
  0 port fixe réel, 201 fichiers sous mock.
- **Aucune injection SQL, aucun XSS, aucun secret** en clair (grep exhaustif).
- Principe **XVI** : `platform_audit.py` → **0 violation / 279 fichiers**.
- Principe **VIII** : un seul réparateur, séparation cohérente.
- **`requests.db` conforme à XIII sur 6 critères sur 8.**
- **`/metrics` livré** (167 lignes, 9 métriques de flotte).
- Le **plancher de latence du proxy est de 14-15 ms**, contre une cible de
  30 ms.

---

## Comment vérifier une affirmation de cet audit

Toutes les commandes sont reproductibles :

```bash
# l'état de la flotte (le payload se contredit)
curl -s http://127.0.0.1:4000/api/vpn-status | python -c "import json,sys; d=json.load(sys.stdin); print('stations_total',d['stations_total'],'| usable',d['usable_stations'],'| configured',d['configured_stations'])"

# les violations de clés étrangères
python -c "import sqlite3; c=sqlite3.connect('file:logs/registry.db?mode=ro',uri=True); print(len(c.execute('PRAGMA foreign_key_check').fetchall()),'violations')"

# le cache perdu sur la forme Responses
python -I -c "import sys; sys.path.insert(0,'.'); from app.protocol.mapping import _extract_cache_tokens as f; print('Chat',f({'prompt_tokens':1000,'prompt_tokens_details':{'cached_tokens':900}}),'| Responses',f({'input_tokens':1000,'input_tokens_details':{'cached_tokens':900}}))"

# les rotations WARP par jour (0 depuis le 18/11)
python -c "import sqlite3; c=sqlite3.connect('file:logs/registry.db?mode=ro',uri=True); [print(r) for r in c.execute(\"select date(started_at,'unixepoch') d, result, count(*) from rotation_jobs group by d,result having d>='2026-11-16' order by d\")]"
```

---

## Règle à retenir

**Un livrable d'audit non commité n'existe pas.** L'audit SRE du 2026-11-14
(755 lignes, 5 P0 documentés) a dormi dans un `git stash` pendant 6 jours,
tandis que la branche qui porte son nom restait sans son dossier `specs/`.
Ses 5 P0 sont **tous encore ouverts** — et l'un a **régressé**.

---

## Correctifs déjà livrés (2026-11-20)

Quatre P0 de cet audit ont été **corrigés et vérifiés** le jour même :

| Tâche | Correctif | Preuve |
|---|---|---|
| **T001** | `_resp_json_hors_boucle` déplacée avant le bloc `__main__` | AST : définition index **818** < main **892** ; `test_a11` 2/2 |
| **T002** | Alias `sys.modules` (mono-namespace, fin de la double exécution) | Exécution : alias **identique** à `__main__`, import ultérieur = **même objet**, **0** module en plus |
| **T003** | Fusion des `elif _geo_tunnel` (fin du contournement géo) | AST : **7 blocs**, **0 lecture non assignée**, **7/7** appellent `_open_via_pool` |
| **T005** | Overlays compose versionnés + fork déclaré dans le compose | `grep "image: qmcgaw/gluetun"` → **0** ; `docker compose config` → **11/11** services sur le fork |
| **T007** | Probe post-rotation : lecture du schéma réel des captures | Exécution sur les 4 captures : **ANCIEN 0/4 → NOUVEAU 4/4** exploitables |
| **T154** | Audit des 2 modules de classification d'échec | **19/19** cas corrects — modules **sains**, aucun défaut |

**Validation** : suite complète **exit 0** (6 262 passed, 0 failed),
`ruff check opencode.py` **All checks passed**, `config_audit --strict`
0 dead / 0 missing, `platform_audit --strict` 0 violation / 279 fichiers.

**Reste à valider en fenêtre d'exploitation** : un seul
`SQLite connection established` au démarrage (T002), un appel réel à
`/v1/responses` non-stream → 200 (T001), une requête géo-restreinte par le
tunnel (T003), et un `docker compose up` sans casse (T005).

**Deux découvertes faites en exécutant** :

- **V-66** : `scripts/fresh_chain.py` garde l'image upstream **légitimement**
  (`docker run --rm --entrypoint sh` — un shell jetable, pas un tunnel). Le
  test `test_manifest_pinning.py` protégeait une bonne intention avec un
  critère obsolète ; il a été mis à jour sans perdre son intention.
- **V-67** : `official_payload.json.bak` **n'est pas** la capture 1.19.5 que
  le code prétend (même `_body_sha256` que le fichier principal). Le
  correctif du 2026-11-14 qui l'a créé **n'a pas résolu** le problème de
  format qu'il visait — la distinction `_body` / `_body_v1` est décorative.
