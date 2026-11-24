# Feature Specification: Audit global et optimisation — jambe free, NordVPN, WARP, proxys, proxy

**Feature Branch**: `024-audit-optimisation-globale`
**Created**: 2026-11-20
**Status**: Draft
**Input**: « Je veux que tu me fasses un audit complet et exhaustif du projet,
puis, avec SpecKit, un plan de tout ce qui ne fonctionne pas, du code mort, du
code dépassé, tout ce qu'il faut optimiser, fiabiliser, rendre plus
qualitatif, plus performant, plus modulable, plus optimal et fonctionnel.
Concentre-toi principalement sur la branche gratuite, sur NordVPN, sur WARP,
le proxy et les proxys possibles, puis sur le reste du projet de façon
générale. […] Je voudrais qu'on revoie également dans ce plan que tu vas me
faire intégralement l'architecture. Voire même, je pensais peut-être passer en
Go ou en Rust, dis-moi ce que tu en penses à ce sujet. »

**Audit source** : `docs/audits/2026-11-20-audit-complet/` (10 rapports
d'auditeurs indépendants + 4 documents de preuves vérifiées par l'auditeur
principal).

---

## 1. Pourquoi cette feature existe

Un audit SRE complet a été conduit le 2026-11-14 et a produit un diagnostic
exact (5 causes racines P0). Ce travail **n'a jamais été commité** : il dort
dans `git stash@{0}` depuis 6 jours, et la branche qui porte son nom
(`019-audit-optimisation-flotte-free`) n'a même pas son dossier `specs/`.

Résultat mesuré au 2026-11-20 : **0 des 5 P0 fermés, 0 des 10 P1 fermés, 1
P0 régressé**. La flotte est à **1/6 `client_usable`**, 4 stations sur 6 sont
en `vpn=error` depuis ~48 h, et le nombre de rotations WARP réussies est
tombé à **zéro depuis 3 jours**.

Cette feature ne recommence pas l'audit : elle **consomme** l'audit du
2026-11-20 et le transforme en plan exécutable, versionné, avec des gates.

## 2. Constat de départ (mesuré, non négociable)

### 2.1 État de la flotte

| Mesure | Valeur au 2026-11-20 |
|---|---|
| Stations `client_usable` | **1 / 6** |
| Stations `vpn=error` | **4 / 6** (depuis ~48 h) |
| Rotations WARP réussies (18–20/11) | **0** (187 le 15/11) |
| `claims ok` | **4** (17 le 14/11) |
| `bind_error` | **81,9 %** (59,1 % le 14/11) |
| `warm_pool` vivantes | **2** (15 le 14/11) |
| Incidents cumulés | **35 219** (17 516 le 14/11) |

### 2.2 État des données

| Fichier | Taille | Problème |
|---|---|---|
| `logs/` | **15 Go** | dont 13 Go de backups jamais purgés |
| `logs/registry.db` | 160 Mo | 95 % des lignes dans des tables jamais purgées |
| `logs/requests.db` | 159 Mo | **99,6 Mo de pages libres** jamais récupérées |
| `free_pool.json` | 1,9 Mo | horodatages d'un autre boot, 58 stations fantômes |

### 2.3 État du code

| Mesure | Valeur |
|---|---|
| Lignes Python (hors tests) | ≈ 143 000 |
| Tests | 6 385 (6 379 passent) |
| Modules > 1 500 lignes (principe XIV) | **9**, dont 3 créés **après** la constitution |
| `except Exception: pass` dans `opencode.py` | **367 / 1 225 (30 %)** |
| Symboles morts identifiés | **37** dans `free/` + 25 dans `opencode.py` |
| Tâches SpecKit ouvertes | **≈ 477** sur 13 specs |

### 2.4 Les cinq défauts les plus graves (tous vérifiés)

1. **La flotte survit par accident maintenu** — `docker-compose.yml`
   (versionné) déclare l'image upstream pour 5 des 6 stations ; seuls 6
   overlays **gitignorés** dans `logs/` portent l'image du fork ; le
   mécanisme de rattrapage côté code (`extra_compose_files`) n'a **aucun
   appelant de production**. Une purge de `logs/` ou un `compose up` détruit
   la flotte.

2. **Contournement de la porte géo** — `opencode.py:31595` : un
   `elif _geo_tunnel` **dupliqué** rend la route tunnel inatteignable, alors
   que `config.yaml` exige `require_vpn: true` pour `meta-muse-spark`. Le
   trafic sort en direct.

3. **Deux schémas `ip_state` incompatibles** dans le même `registry.db`
   (`free/ip_state.py:49` vs `free/ip_registry/sqlite.py:58`), aucun
   `CREATE TABLE` ne signalant le conflit.

4. **Trois causes convergentes du blocage** — probe post-rotation WARP cassé
   (`capture["body"]` vs `body_raw`, 3 jours sans rotation), délégation
   superviseur sans vérification de vie, et fuite de `_draining_stations` qui
   retire les stations **définitivement** de la sélection.

5. **L'état persisté ment** — `free_pool.json` déclare 6 stations `usable`
   avec des `time.monotonic()` d'un **autre boot**, écrasés à l'identique sur
   les 6. Violation directe du principe I.

## 3. Périmètre

### 3.1 Dans le périmètre

1. **Jambe free** : quota par IP, ledger, sélection, warm pool, rotation.
2. **NordVPN** : provisionnement, sessions, transports, réparation.
3. **WARP** : probe, rotation, capacité, cycle de vie des conteneurs.
4. **Proxys publics** : catalogue, validation, promotion, rétention.
5. **Proxy** : conversion de protocole, streaming, compaction, télémétrie.
6. **Architecture** : découpage des monolithes, autorité d'état unique,
   suppression du code mort, unification de la configuration.
7. **Données** : rétention, purge, compaction, sauvegardes bornées.
8. **Qualité** : gates CI, tests manquants, options mortes, dette.
9. **Question du langage** : évaluation argumentée d'un portage Go/Rust.

### 3.2 Hors périmètre

- Jambe payante (aucune modification fonctionnelle).
- Réécriture big-bang.
- Second compte, 7ᵉ station, résidentiel.
- `app/protocol/tool_aliases.py` (gelé par la spec 003).

## 4. Contraintes dures

- **Free uniquement** ; le quota est **par IP**, jamais « compte-wide ».
- **Six stations**, jamais sept ; `station_count=6`.
- Aucune rotation avant le vrai `reset_at`.
- Toute affirmation est adossée à une preuve (fichier:ligne, commande, SQL).
- Aucune modification de code pendant l'audit ; les correctifs sont des tâches.
- **Le proxy tourne sur la machine de l'opérateur** : jamais de restart
  pendant l'audit (CLAUDE.md).
- Toute suppression de code est **précédée d'un test d'absence d'usage**.

## 5. User Stories

### US1 — Les P0 silencieux sont corrigés et prouvés (P1)

Un opérateur constate que les trois défauts qui perdent des requêtes sans
laisser de trace sont corrigés : le `NameError` sur `/v1/responses`
non-stream, la double exécution du module, et le contournement géo.

**Test indépendant** : appeler `/v1/responses` en non-stream sur une route
gé0-restreinte et vérifier un 200 ; vérifier `len(sys.modules)` n'a qu'un
seul `opencode` ; vérifier qu'une requête géo-restreinte passe par le tunnel.

**Critères d'acceptation** :

1. `HTTP 502` sur `/v1/responses` non-stream : **0** sur 24 h (502 aujourd'hui).
2. Un seul `SQLite connection established` par boot (2 aujourd'hui).
3. Une requête `require_vpn` sort par le tunnel (vérifiable par l'IP de sortie).

### US2 — La flotte ne dépend plus de fichiers gitignorés (P1)

L'image du fork, le port du control plane et les variables `CONTROL_*` sont
déclarés dans un fichier **versionné**. Un `git clone` sur une machine neuve
reconstitue une flotte fonctionnelle.

**Test indépendant** : `git clone` dans un répertoire neuf, `docker compose
config`, vérifier que chaque service station porte `image: gluetun-016:local`
et ses variables de contrôle.

**Critères d'acceptation** :

1. `docker compose config` liste `gluetun-016:local` pour les 6 stations.
2. Aucun fichier nécessaire au runtime n'est dans `logs/`.
3. Un `compose up` sur un clone neuf ne casse aucune station.

### US3 — Une seule vérité d'état, mesurée (P1)

`/api/vpn-status`, `station_state`, `free_pool.json` et les pins décrivent le
même état. Un test échoue si deux surfaces divergent.

**Test indépendant** : comparer les surfaces toutes les 30 s pendant 24 h ;
zéro divergence sur `(station, client_usable, current_ip)`.

**Critères d'acceptation** :

1. `stations_total` = `configured_stations` = `usable_stations` ≤ 6.
2. Aucune valeur `time.monotonic()` persistée.
3. Zéro station négative dans `station_health` (58 aujourd'hui).

### US4 — La rotation WARP refonctionne (P1)

Le probe post-rotation lit le bon schéma de capture ; les rotations réussissent
à nouveau.

**Test indépendant** : provoquer 10 rotations, compter les succès.

**Critères d'acceptation** :

1. Taux de rotation WARP `ok` > 50 % sur 24 h (0 % aujourd'hui).
2. Zéro `KeyError` sur `capture["body"]` dans les logs.
3. Aucune IP refusée n'est appliquée au ledger.

### US5 — Les données sont bornées (P2)

`registry.db` et `requests.db` ont une rétention **exécutée**, pas seulement
codée.

**Test indépendant** : injecter des lignes datées, lancer la purge, compter.

**Critères d'acceptation** :

1. `logs/` < 2 Go (15 Go aujourd'hui).
2. `registry.db` < 50 Mo (160 Mo aujourd'hui).
3. `requests.db` : `freelist_count` < 5 % de `page_count` (62 % aujourd'hui).
4. Toutes les tables volumineuses sont couvertes par la purge.

### US6 — Le code redevient modulaire (P2)

Les monolithes sont découpés par strangler, dans l'ordre du plan.

**Test indépendant** : `wc -l` sur chaque module cible après chaque lot.

**Critères d'acceptation** :

1. Aucun module **créé après le 2026-11-04** ne dépasse 1 500 lignes.
2. `opencode.py` < 5 000 lignes après le lot L5.
3. Le code mort identifié (62 symboles) est supprimé ou justifié.

### US7 — Les gates couvrent ce qu'elles prétendent (P2)

Les gates de la constitution sont câblées et non circulaires.

**Test indépendant** : casser volontairement une option, vérifier que le gate
échoue.

**Critères d'acceptation** :

1. Le gate option-map couvre **15 pages / 15** (1 aujourd'hui).
2. `platform_audit --strict` est invoqué par la CI.
3. La CI tourne sur Windows **et** Linux (matrice).
4. Zéro assertion tautologique (`or True`) dans les tests.

### US8 — La décision de langage est tranchée sur des faits (P3)

Un document d'architecture compare Python / Go / Rust sur les mesures de
l'audit, avec une recommandation et un plan de migration par strangler.

**Critères d'acceptation** :

1. Le coût du portage est chiffré (lignes, modules, dépendances bloquantes).
2. Le gain est chiffré (latence, concurrence, démarrage).
3. La recommandation nomme ce qui doit être **fait d'abord** dans tous les cas.

### US9 — Le modèle free reste servi malgré les IP épuisées (P0)

Un opérateur teste `deepseek-v4.1-flash-free` : le modèle répond **200** dès
qu'une IP prouvée 200 existe, même quand toutes les IP NordVPN d'une station
sont `spent` ; sinon le client reçoit une **dégradation explicite**, jamais un
faux « modèle indisponible ».

**Preuve** : [docs/evidence/free-model-429-2026-11-23.md](../../docs/evidence/free-model-429-2026-11-23.md).

**Test indépendant** : `scripts/client_smoke.py` avec les IP NordVPN `spent` →
200 via WARP (ou 503 explicite) ; `scripts/test_free_egress.py` par IP.

**Critères d'acceptation** :

1. Le modèle free répond **200** tant qu'une IP prouvée 200 existe.
2. Les 429 d'IP sont classés `quota` avec `reset_at` réel ; 0 faux 429.
3. `client_usable` provient d'une preuve 200, jamais de `vpn=connected`.
4. Aucune conclusion « aucune IP disponible » à partir d'une série d'échecs.

## 6. Edge cases

- Que se passe-t-il si le superviseur est mort alors que `repair_owner:
  supervisor` ? → refus client permanent (à corriger, P0-01 du rapport 01).
- Que se passe-t-il après un reboot OS avec des `parked_until` monotonic
  persistés ? → station invisible plusieurs jours (P0-02).
- Que se passe-t-il si `logs/` est purgé ? → perte du control plane (V-33).
- Que se passe-t-il si `docker compose up` est lancé ? → 5 stations sur
  l'image upstream, API v2 absente (V-29).
- Que se passe-t-il si une IP `proven` n'a pas de `reset_at` ? → 102 cas
  aujourd'hui, aucune purge.

## 7. Key Entities

- **Station** : slot (conteneur, tunnel, port SOCKS).
- **Session** : connexion NordVPN, ressource de compte — **non comptée
  aujourd'hui** (V-34).
- **IP** : egress public, ressource périssable — 116 `proven`, 0 `spent`/`burned`.
- **Preuve** : `(wire_fp, 200, contenu)` — deux constructions incompatibles
  aujourd'hui (V-3).
- **Verdict de quota** : `upstream_verdict` vs `ours_used` — non séparés.

## 8. Success Criteria

### Mesurables

- **SC-001** : 6/6 `client_usable` sur un même cycle de 120 s, tenu 30 min.
- **SC-002** : 0 requête perdue par exception avalée sur 24 h.
- **SC-003** : `logs/` < 2 Go, `registry.db` < 50 Mo.
- **SC-004** : taux de rotation WARP > 50 %.
- **SC-005** : zéro divergence entre les 4 surfaces d'état sur 24 h.
- **SC-006** : aucun module post-constitution > 1 500 lignes.
- **SC-007** : les 8 gates de la constitution sont câblées et passent.
- **SC-008** : un `git clone` neuf produit une flotte fonctionnelle.
- **SC-009** : le modèle free répond 200 tant qu'une IP prouvée 200 existe ;
  0 faux « modèle indisponible » ; 0 station « connectée mais inutilisable ».

## 9. Assumptions

- L'opérateur peut redémarrer le proxy pour appliquer les correctifs qui le
  nécessitent (mais pas pendant l'audit).
- Le compte NordVPN reste limité à ses sessions actuelles.
- Les proxys publics restent une source d'appoint, pas la source principale.
- Le fork Gluetun (`gluetun-016`) reste la cible des stations.
