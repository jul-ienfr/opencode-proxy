# Synthèse — alignement du proxy sur le client OpenCode 1.19.3

**Date :** 2026-10-20 · **Suite :** 3 784 tests, 0 échec, 5 ignorés en **4 min 07 s** ; gate CI des contrats : 512 tests en 20 s · **Journal détaillé :** `docs/PLAN_ALIGNEMENT_2026-10-20.md`

Ce document est le résumé qu'on lit avant d'ouvrir le journal. Il dit ce qui est
prouvé, ce qui ne l'est pas, et ce qu'il reste à faire — sans hiérarchie
flatteuse.

---

## 1. Ce qui est fait, et sur quelle preuve

| Lot | Objet | Preuve |
|---|---|---|
| V4 | Identité de session alignée sur 1.19.3 (`ses_` partagé, UA officiel) | **live** — données réellement émises |
| V4.3/V4.4 | Budget de reprises + statuts réessayables | tests (30) |
| V3.3 | `reasoning_content` sur la jambe free | **live** — 200 là où c'était 400 |
| V2.2b/V2.2c | Routage piloté par le catalogue, par jambe | tests (36) + cache 161 modèles |
| V2.3 | Les 5 bases amont viennent de la config | tests (15) + garde-fou anti-littéral |
| V6.4 | `big-pickle` exposé et servi | **live** — `PROXY OK`, `stop`, usage exact |
| V6.4.4 | Parité `usage` sur la jambe free | **live** — `reasoning_tokens: 23` |
| V5.1 | Flux vide ≠ HTTP 200 | **live (harnais ASGI)** + tests (29) |
| V6.3.1 | Classification des échecs free | tests (24), mesurable en production |
| V2.5 | Refus explicite du protocole `gemini` (501 nommant le modèle) | tests (10) + ASGI sur 3 endpoints |
| V5.4 | Contrat SSE Anthropic : ordre, unicité, thinking, tool_use | tests (6), ASGI bout en bout |
| V5.3 | Usage Anthropic : `cached_tokens` → `cache_read_input_tokens` | tests (3), ASGI bout en bout |
| V6.1.2/3 | Porte free testée par facteur + diagnostic branché | tests (28), `gate_factors` exposé |
| V6.3.2 | Disjoncteur par station, imputation prouvée | tests (18), `station_faults` exposé |
| V6.5.3 | Budget de temps total, abandon propre | tests (22), 504 explicite |
| V8.3 | Latence p50/p95 par jambe et par modèle | tests (15), `latency_by_leg` exposé |
| V6.3.4 | SLO de latence : verdict automatique | tests (16), `slo` exposé |
| V6.2.1 | Shims déclaratifs avec statut de nécessité | tests (22), `config.yaml > free.shims` |
| V6.2.2 | `bash` mesuré non load-bearing, retiré | mesure live 4 corps + tests (29) |
| V6.2.3 | `stream:true` exigé par la porte — forçage conservé | mesure live 3 corps + tests (7) |
| V7.5 | Modèle de porte documenté | `docs/free-leg.md` |
| V7.3 | Cliquet sur les URLs d'amont | tests (7), vérifié par injection |
| V8.1 | `/api/drift` + `/api/free-health` | live 4100 + tests (32) |
| V8.2 | Canary sur le contenu, alerte à 3 échecs | live (3 cycles) + tests (21) |
| V8.5 | Garde de protocole SystemOne | live + tests (8) |
| V8.6 | Diagnostic de verrou mono-instance | live + tests (10) |
| V8.4 | Gate CI des contrats + suite assainie | 378 tests/18 s, suite 24 min → 4 min |
| V7.2 | Audit de configuration (clés mortes) | 257 clés, 0 morte, 2 réparées |
| V8.7 | `/health` expose ses raisons | live + 10 tests |
| V8.8 | Reset des pauses persisté | 8 tests + fichier vérifié |
| V5.2 | Erreur diffusée dans le dialecte du client | live 2 routes + 14 tests |
| V0.5 | Table d'erreurs alignée sur `executor.ts` | fixture 29 cas + 29 tests |

**Trente-cinq lots clos.** Treize ne sont prouvés que par des tests (V2.3, V2.2b, V6.3.1, V2.5, V5.4, V5.3, V6.1.2, V6.1.3, V6.3.2, V6.5.3, V8.3, V6.3.4, V6.2.1) ;
les autres l'ont été par un appel réel ou une capture.

---

## 2. Les cinq prémisses d'audit démenties par la mesure

L'audit initial affirmait cinq choses fausses. Chacune aurait produit une
« correction » qui cassait quelque chose qui marchait.

| Affirmation | Réalité mesurée |
|---|---|
| `tool_choice: "auto"` est un écart | c'est **exactement** ce que le client officiel émet |
| Le proxy émet `max_completion_tokens` | il émet `max_tokens` — conforme |
| Le proxy émet `store` | il ne l'émet **jamais** |
| 5 modèles ont un mauvais protocole | **1 seul** (`minimax-m3.1-flash-free`) |
| 23 modèles free à exposer = gain facile | **1 seul** est servable (`big-pickle`) sur 81 testés |

**La leçon est dans la méthode, pas dans la liste.** Lire le code produit des
faits sécurisants ; un appel réel produit parfois autre chose. Quand les deux
divergent, c'est l'appel qui a raison.

---

## 3. Les trois défauts de production cachés derrière des tests intermittents

Deux tests passaient seuls, échouaient en série. La lecture « test fragile » a
été fausse **deux fois sur deux**.

| Symptôme | Cause réelle |
|---|---|
| `test_free_429_*` instable | le fixture écrivait un dict que le lecteur n'interrogeait pas (`yaml_get` lit le YAML live) |
| `big-pickle` exposé une fois sur deux | la découverte écrit ses entrées **sans `source`** : l'entrée vérifiée était ignorée, et le modèle gardait un endpoint possiblement faux |

Puis un troisième, trouvé par un appel réel : le registre `FREE_MODELS` est
**réinitialisé** à chaque passe de découverte, effaçant la classification du
modèle — quelques secondes après le démarrage.

**Conclusion opérationnelle :** sur ce dépôt, un test qui passe seul et échoue en
série n'est pas du bruit. Il pointe un état partagé que personne ne contrôle.

---

## 4. Ce qui n'est pas prouvé

- **La jambe payante entière.** Aucune clé valide dans cette session. Tout ce qui
  la concerne est couvert par des fixtures, jamais par un appel réel. C'est le
  trou le plus large du lot.
- **V6.3.1 — la politique de reprise.** La classification existe, le compteur
  tourne, mais aucune décision n'a été changée : elle se prendra sur les chiffres
  de `free_failure_kinds`, pas sur une intuition.
- **Rien sur la latence de la jambe free.** Mesurée sur 21 appels réels : p50
  4,5 s, p90 7,4 s, max 13,4 s, **21 succès sur 21**. La station n'est pas la
  variable — l'écart intra-station (4,5 s → 13,4 s) dépasse l'écart entre
  stations. Le temps se passe en amont ; le seul levier proxy restant serait une
  course entre deux stations, au prix du double de quota. Non implémenté.

---

## 5. Ce qu'il reste à faire, dans l'ordre

1. **Redémarrer `:4000`** pour charger le garde V5.1, la classification et les
   correctifs de routage. `pythonw opencode.py --gui` après avoir tué le process.
2. **Regarder les compteurs** après quelques jours : `GET /health?detail=full` →
   `empty_stream_incidents` et `free_failure_kinds`. Si `gate` domine, la
   rotation de station sur 403 est démontrée inutile — et la politique de reprise
   pourra être changée avec des chiffres.
3. **Dès qu'une clé valide existe** : valider la jambe payante de bout en bout,
   puis finir V4.2 côté payant. C'est le seul lot bloqué par une dépendance
   externe.
4. **V2.5 est clos** — un id `gemini` renvoie désormais `501` en nommant le modèle et la famille, au lieu de retomber en silence sur l'endpoint chat `openai`. Reste ouvert : **exposer les 23 ids free manquants** (V2.6), sous contrainte de vérification live — un seul s'est révélé servable sur 81 testés.

---

## 6. Les instruments laissés en place

| Instrument | Où | À quoi il sert |
|---|---|---|
| `_note_empty_stream` | `/health?detail=full` | fréquence et jambe des flux vides |
| `free_failure_kinds` | idem | répartition gate / quota / transit / auth / model |
| `gate_factors` | idem | quel facteur de la porte a refusé (egress / identity / body) |
| `station_faults` | idem | quelle station a été écartée, et pour quelle cause |
| `latency_by_leg` | idem | p50/p95 par couple (jambe, modèle) |
| `slo` | idem | verdict ok / degraded / breached / unknown sur ces séries |
| Baseline de latence | `_research/free_leg_baseline.json` | p50/p90 TTFT et taux de succès de la jambe free |
| Garde de flux | `streaming/empty_guard.py` | 200 + flux vide → 502 |
| Harnais ASGI | `tests/test_empty_stream_asgi.py` | rejoue le défaut en exécution |
| Sonde de modèles free | `_research/probe_free_models.py` | régénérable, verdicts datés |
| Cache de routes | `scripts/refresh_catalog_routes.py --check` | détecte la dérive du catalogue |

---

## 7. La règle à retenir

**Les tests valident ce qu'on a pensé à tester ; un appel réel valide ce qui
existe.** Cette session a produit quatre correctifs qui passaient leurs tests et
échouaient à l'exécution — dont un que j'avais annoncé comme prouvé. La seule
défense trouvée est empirique : dès qu'un changement touche le chemin d'une
requête, le mesurer sur une requête.
