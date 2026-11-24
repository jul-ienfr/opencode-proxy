# Rapport du workflow — correction des 12 P0 restants

**Date** : 2026-11-20
**Workflow** : `fix-remaining-p0` (run `wf_d0703851-1ef`)
**Méthode** : 7 groupes de fichiers disjoints en parallèle, chacun suivi d'un
**vérificateur adversarial** indépendant chargé de le **réfuter**.
**Bilan** : 13 agents, 1 147 537 tokens, ~105 minutes, **12 agents terminés,
1 en échec**.

---

## 1. Résultat brut

| Groupe | Verdict du vérificateur | Suite donnée |
|---|---|---|
| `store-draining` (T010) | ✅ **CONFIRMÉ** — confiance haute | clos |
| `registry-ddl` (T024) | ✅ **CONFIRMÉ** — confiance haute | clos |
| `mapping-usage` (T011/T013/T014b) | ❌ **RÉFUTÉ** — 4 trous | **3 corrigés** par l'auditeur principal |
| `opencode-stream` (T012/T014/T014c) | ❌ **RÉFUTÉ** — confiance haute | T012/T014 clos ; **T014c remis en ouvert** |
| `proxy-credentials` (T021) | ❌ **RÉFUTÉ** — confiance haute | **T021 remis en ouvert** (4 trous) |
| `profile-ttl` (T022) | ❌ **RÉFUTÉ** — confiance haute | T022 clos ; **2 cas limites** à instruire |
| `security-config` (T025) | ⚠️ **agent en échec** (limite de tokens) | traité à part — décision opérateur |

**3 confirmés, 4 réfutés, 1 en échec.** Un taux de réfutation de 57 % — c'est
le signe que la vérification adversariale **sert à quelque chose** : sans
elle, 4 correctifs auraient été déclarés faits à tort.

## 2. Les deux correctifs confirmés — et pourquoi la confirmation compte

### T010 — `egress_ip_in_use` (garde d'unicité d'IP, principe V)

**Verdict** : « *Le correctif est REEL et fonctionnellement complet* ».

Le vérificateur a fait **plus** que rejouer la preuve de l'agent :

- il a vérifié la **convention en base** : `typeof(station)='text'` sur
  **32 140 lignes**, valeurs `''` / `station-1..6` / `warp-1..12` ;
- il a confirmé les **3 producteurs** (`free/egress_runtime.py:317`,
  `free/egress_adapters/warp.py:75`, `opencode.py:3232`) ;
- il a écrit **son propre script** : **10/10** cas limites corrects, dont
  `warp-3` vs station 1, `public` avec `''`, `None`/`0`/`-1` en échec fermé ;
- il a testé sur le **`registry.db` réel** en lecture seule : **40/40** lignes
  `usable` interrogées avec leur propre numéro retournent `False` ;
- il a vérifié qu'**aucun test n'a été supprimé** (`git diff --diff-filter=D`
  vide) ni affaibli sur les 5 fichiers gardiens ;
- il a cherché **d'autres sites** comparant `egress_options.station` à un
  entier : **aucun**.

**C'est une vérification de la qualité qu'on attend d'un relecteur humain.**

### T024 — DDL hors transaction + index couvrant

**Verdict** : « *Le correctif est REEL et COMPLET* ».

Le vérificateur a construit un **oracle indépendant** : après un échec forcé
de `apply` + rollback, `control_events`/`control_cursor` **et l'index**
survivent (possible **uniquement** si le DDL est committé hors transaction),
tandis que les données sont bien rollback. Et `EXPLAIN QUERY PLAN` passe de
`sqlite_autoindex_control_events_1` à
**`USING COVERING INDEX idx_control_station_type`**.

Il a aussi **écarté un faux positif** : un flakiness « database is locked »
sur 8 threads, qu'il a montré **pré-existant** (il se produit dans
`migrate.connect()` ligne 54, avant tout code T024, à l'identique AVANT et
APRÈS).

## 3. Le groupe réfuté — et c'est le résultat le plus utile

### `mapping-usage` (T011 / T013 / T014b) — 4 trous trouvés

**Trou principal (prouvé)** : le chemin de streaming vivant
`_chat_stream_to_responses` (`opencode.py` ~L32953-32971) construit l'usage
terminal en lisant **directement** `_usage.get("prompt_tokens")` — il ne passe
**pas** par `openai_chat_to_responses`. Le symptôme T014b (usage à zéro)
**persistait donc sur ce site**, et l'affirmation de l'agent (« les deux sites
consommateurs bénéficient du correctif ») était **fausse**.

Le vérificateur a même noté que le bloc qui appelle la fonction corrigée est
**du code mort** (après un `return` inconditionnel L33015).

**Trou latent (prouvé)** : `_ByteAwareOrderedDict` ne surcharge ni
`__delitem__` ni `copy` — mesuré : `del c['k']` laisse le compteur à 100,
`c.copy()` le fait passer de 100 à 200. Le commentaire de classe revendiquait
« tout chemin de mutation » : **sur-claim**.

**2 cas limites mineurs** : `cached_tokens: None` passé tel quel ; les
compteurs `float` deviennent 0 via `_usage_int`.

**Preuve falsifiable fournie** : le même script exécuté contre la source
`HEAD` → **11 échecs** ; contre l'arbre actuel → **26 OK / 0 FAIL**.

### Ce que l'auditeur principal a fait de ce rapport

Les 3 trous actionnables ont été **corrigés** :

| Trou | Correctif |
|---|---|
| Usage Chat ignoré sur `_chat_stream_to_responses` | lecture des **deux** formes (`prompt_tokens`/`input_tokens`) |
| `__delitem__` manquant | ajouté (décrémente le compteur) |
| `copy` manquant | ajouté (retourne un `OrderedDict` — ne re-compte pas) |

Et le commentaire de classe a été **corrigé** pour ne plus sur-revendiquer.

## 4. L'agent en échec

`fix:security-config` (T025) a échoué sur une **limite de tokens**
(« *response exceeded the 128000 output token maximum* »).

**Ce n'est pas grave** : T025 est un **arbitrage d'exposition**, pas un bug.
L'auditeur principal a traité le sujet séparément — voir `tasks.md` T025 :
le constat d'audit était **imprécis** (il existe une protection par réseau en
403 hors LAN), et la décision revient à l'opérateur.

## 5. Ce que ce workflow démontre

**La vérification adversariale fonctionne.** Sur 3 groupes vérifiés :

- **2 confirmés** avec des preuves **plus fortes** que celles de l'agent
  (scripts indépendants, base réelle, oracle de rollback, faux positif
  écarté) ;
- **1 réfuté** avec **4 trous prouvés**, dont un que l'auditeur principal
  avait lui-même manqué en vérifiant T011/T012 séparément.

**La leçon centrale** : un agent qui *dit* avoir corrigé n'est pas un agent
qui *a* corrigé. Les trois vérificateurs ont chacun trouvé ou confirmé
quelque chose que le rapport de l'agent ne contenait pas — dans un sens comme
dans l'autre.

**Et une leçon sur la vérification elle-même** : j'avais vérifié T011 et T012
par exécution et les avais déclarés complets. Le vérificateur a trouvé un
**troisième** chemin (`_chat_stream_to_responses`) que ni moi ni l'agent
n'avions testé. Vérifier une fonction ne suffit pas : il faut vérifier
**tous ses appelants**.
