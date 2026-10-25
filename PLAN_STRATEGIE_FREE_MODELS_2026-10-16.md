# Plan de stratégie — Mise à jour automatique de la liste des modèles gratuits

Date : 2026-10-16 | État : **Lot A implémenté le 2026-10-17** (sonde de servabilité — 35 tests dédiés verts, suite intégrale 3242 tests : 0 échec). Lots B–G restent non implémentés.

---

## Constat : l'auto-update existe déjà (et c'est add-only)

La question « est-ce possible ? » est en grande partie tranchée par l'existant. Le proxy a déjà une chaîne complète de découverte automatique :

| Élément | Où | Comportement |
|---------|-----|--------------|
| Cascade de détection | `config/discovery.py:205` (`is_free_model`), `:236` (`detect_free_ids`) | `pricing == 0` → flags `is_free`/`free` → `capabilities.free` → filet suffixe `-free` |
| Sources | `config/discovery.py:187` (`free_discovery_urls`) | `/models` des deux bases (inference + openai), surcharge via `upstream.free_models_url` |
| Filet HTML | `config/discovery.py:352-374` | Scrapping de `https://opencode.ai/docs/fr/zen/` si la cascade trouve 0 id |
| Boucle périodique | `opencode.py:2775` (`_free_discovery_loop`) | Toutes les 3600 s ± 10 % jitter, backoff ×2 après 3 échecs (plafond 7200 s), single-flight |
| Boot | `config/settings.py:1091` (`ensure_free_models_on_boot`) | 1er fetch en tâche de fond post-lifespan (aucun I/O à l'import) |
| Persistance | `config/discovery.py:541` (`persist_free_mappings`) | Merge **add-only** dans `config.yaml`, écriture atomique sous verrou |
| Gardes | `config/settings.py:915` (`GO_ONLY_IDS`), `:924` (`PURGED_IDS`) | ids réservés Go exclus du pool anonyme ; tombeaux manuels anti-résurrection |
| Observabilité | `opencode.py:14653` (`GET /api/free-models`), état `settings.py:936`, SSE `free_models_updated` (`opencode.py:2826`) | `detected`, `removed`, `source`, `next_refresh`, `consecutive_failures` |

**Fait notable** : `PURGED_IDS` existe *parce que* l'auto-discovery a déjà ressuscité des ids morts (commentaire `settings.py:920-923`). Toute stratégie de retrait automatique doit respecter cette leçon : jamais un seul signal.

---

## Les 5 lacunes (pourquoi ce n'est pas encore une « stratégie »)

1. **Jamais de retrait** — `config/discovery.py:469-475` log « upstream removed X — keeping local, manual cleanup needed ». Un modèle devenu payant ou supprimé reste dans le pool jusqu'à une purge manuelle (`purged_ids`).
2. **Aucune vérification de servabilité** — la discovery *croit* le listing `/models` ; un id annoncé mais mort (403/404) entre dans `FREE_MODELS` sans être testé.
3. **Config minimale pour les nouveaux** — le persist n'écrit que `{"protocol": openai}` (`discovery.py:577-583`) ; pas d'`output_limit`, pas de cas particuliers (endpoints `muse/spark/jev` gérés seulement par heuristique de nom).
4. **Mappings et cible statiques** — quand `mimo-v2.5-free` est mort, la migration `free_model_map` → `mimo-v2.6-flash-free` a été manuelle. Le code ne fait qu'un warning + fallback sur `new_pool[0]` (`discovery.py:515-521`).
5. **Pas de ranking** — `free_model_usage` (`observability/db.py:289`) journalise déjà `status` + `duration_ms` par free_model, mais rien ne s'en sert pour choisir/moduler le `default_target`.

---

## Stratégie proposée — pipeline 5 étapes

```
DÉCOUVERTE (existant) → VÉRIFICATION (probe) → CANDIDATURE (diff 2 phases) → APPLICATION (anti-flapping) → SURVEILLANCE (rollback)
```

### Lot A — Probe actif de servabilité (P0)

- Après chaque fetch : requête minimale (`max_tokens=1`) par id **nouveau ou annoncé disparu**, via la jambe free (toujours en `asyncio.to_thread`, jamais dans le chemin requête client).
- Lecture du résultat : `200` → vivant ; `403/404/410` → mort ; `429` → vivant mais rate-limited (promu, marqué).
- Bornage strict : concurrence limitée, timeout court, **probe sauté** si `free_model_usage` contient déjà un succès récent pour cet id (le trafic réel est un meilleur signal qu'un probe).
- Point d'ancrage : nouveau helper dans `config/discovery.py` (ou `free/probe.py`), hook dans `_ensure_free_models_sync` (`settings.py:1026`) entre fetch et apply.

#### ✅ Réalisé (2026-10-17) — Lot A et déviations documentées

Fichiers : `config/discovery.py` (mécanique pure : `probe_cfg`, `free_probe_shaping`, `free_probe_verdict`, `probe_recheck_plan`), `config/settings.py` (slot DI `_FREE_PROBE_GATE` + hook dans `_ensure_free_models_sync`), `opencode.py` (gate runtime `_free_probe_gate`, dispatch httpx/curl, anti-burn via `_probe_ms_iso`, exposition `"probe"` dans `GET /api/free-models`), `config.yaml` (`free_discovery.probe`, `enabled: true`), `tests/test_free_probe.py` (35 tests).

Déviations par rapport à la version initiale du plan :

- **Verdicts en 5 niveaux, pas 3** : `alive | limited | dead | not_free | unknown | not_probeable`. En particulier **403 n'est JAMAIS mort** (variable selon l'IP de sortie) ; `401/402` = `not_free` (le modèle n'est pas gratuit) ; `200/429` = vivant ; `5xx`/erreur transport = `unknown`, re-sondé plus tard.
- **Budgets** : `max_ids=10` par cycle, `dead_ttl_s=86400`, `recheck_interval_s=1800`, skip si usage DB récent (`skip_recent_ok_s`) — le trafic réel reste meilleur signal qu'un probe (re-prenant du §A initial).
- **Egress** : la sonde utilise exactement le même chemin d'egress que le fetch `/models`, JAMAIS via le pool d'IP rotatives (ne pas brûler les quotas modèle/IP).
- **jev-\*** = `not_probeable` v1 (SystemOne non-chat) — en l'état des endpoints connus.
- **Hermeticité** : le hook de sonde est DISABLED par défaut côté module (les tests historiques restent indépendants) ; il n'est activé que par `config.yaml` (`free_discovery.probe.enabled: true`) en prod — pattern pytest-aware.
- **DI opencode → settings** : l'injection `_FREE_PROBE_GATE` garde la mécanique testable sans réseau ; fallback announce-only si la gate lève.

### Note d'intégration (custom mapping)

⚠️ Pendant l'intégration du Lot A, l'agent a — par erreur — restauré les 6 `custom_routes` de `config.yaml` vers les valeurs de HEAD pour faire passer des tests trip-wires, en interprétant le mapping déployé comme une dérive à corriger. **Verdict final : le custom mapping est souverain** (décision humaine). Les tests ont été réécrits en contrat « le runtime exécute exactement ce que le yaml déclare » (plus aucune cible épinglée) et les e2e du couple payant/free séquestrent le mapping déployé pour leur témoin, **sans jamais le muter**. Chronologie et règles : `docs/custom-mapping-gouvernance.md`.

### Lot B — Retrait automatisé à double consensus (P1)

- Retrait automatique **uniquement** si les deux conditions tiennent : id absent de **toutes** les sources pendant N=3 cycles consécutifs **ET** probe « mort ».
- Machine à états : `active → pending_retirement (visible dashboard + SSE, retrait manuel possible) → retired`.
- `retired` : id déplacé dans une section `free_discovery.retired_ids` (avec timestamp + raison), retiré de `models:` et comme cible de `free_model_map` — **historisé, rollback-able**. `purged_ids` reste pour les purges manuelles (défense en profondeur).
- Jamais de retrait d'un modèle avec trafic récent réussi (garde symétrique au probe).

### Lot C — Diff candidat + application contrôlée (P1)

- La discovery produit un **diff candidat** (ajouts / retraits proposés) au lieu de muter directement ; application sous plafond de churn (**≤ 20 % du pool modifié par cycle**, sinon report au cycle suivant).
- Snapshot de l'état avant application → **rollback automatique** si le taux d'erreur du pool explose (breaker sur les `free_status` déjà journalisés).

### Lot D — Migration des mappings + `default_target` (P2)

- Cible d'un `free_model_map` morte → re-pointage vers `default_target` (ou le mieux classé), avec écriture dans `mapping_history` (timestamp, ancienne cible, raison) → rollback possible.
- `default_target` mort → fallback enregistré + événement SSE ; **jamais de changement silencieux sans trace**.

### Lot E — Enrichissement metadata (P2)

- Récupérer dans le payload `/models` ce qu'il expose (context length, limits) pour les nouveaux ids ; sinon défaut conservateur.
- **Invariant à préserver** : un réglage manuel dans `models:` garde toujours la priorité sur l'auto (comportement actuel de `apply_discovered_free_models`, qui n'ajoute que les ids manquants).

### Lot F — Ranking dynamique (P3, optionnel)

- Agrégat `free_model_usage` (24 h glissantes) par free_model → score = taux de succès pondéré + latence.
- Exposé dans `GET /api/free-models` en mode **conseil** par défaut ; re-target automatique du `default_target` réservé à un mode `auto` explicite.

### Lot G — Tests + observabilité (P2)

- Miroir du style `tests/test_free_discovery.py` : retrait à double consensus, non-retrait sur source flapping, churn cap, rollback, priorité du overlay manuel.
- Dashboard : vue du diff (ajoutés / candidats-retrait / pending), événement SSE `free_models_updated` étendu avec le diff.

---

## Ce qui ne doit PAS être automatisé

- Les réglages curatés par modèle (`output_limit`, `geo extends`, `endpoint` systemone pour `jev-*`).
- `go_only_ids` (choix d'architecture, pas un fait d'inventaire).
- La suppression de `purged_ids` (filet de sécurité manuel conservé).

---

## Ordre d'implémentation

| Priorité | Lot | Impact | Effort |
|----------|-----|--------|--------|
| P0 | A (probe actif) | Fini les ids « fantômes » annoncés morts — base de confiance de tout le reste | ~3h |
| P1 | B (retrait double consensus) | La liste se purge toute seule, enfin — sans résurrection | ~4h |
| P1 | C (diff + rollback) | Aucune mutation non contrôlée du pool actif | ~3h |
| P2 | D (migration mappings) | Fini les cibles mortes pointées dans `free_model_map` | ~2h |
| P2 | E (metadata) | Config propre des nouveaux ids sans intervention | ~2h |
| P2 | G (tests + dashboard) | Non-régression + visibilité | ~3h |
| P3 | F (ranking) | Confort — améliore le choix du modèle par défaut | ~3h |

## Critères de succès

- Un modèle disparu/payant sort du pool automatiquement sous ≤ 3 cycles (~3 h) sans intervention humaine.
- **Zéro** retrait déclenché par un seul signal (test : une source qui flappe ne retire rien).
- Un rollback automatique se déclenche et se teste sur erreur qui explose post-changement.
- Zéro changement de `default_target` ou de mapping non journalisé (`mapping_history`/événement SSE).
- Aucun modèle avec trafic récent réussi n'est retiré (garde probe/traffic).

## Risques et garde-fous

| Risque | Garde-fou |
|--------|-----------|
| Résurrection d'ids morts (vu de fait : `PURGED_IDS`) | Double consensus absence + probe, historisation `retired_ids` |
| Probe = charge/abus sur l'upstream | Bornage, ids nouveaux/incertains uniquement, saut si trafic récent |
| Flapping du pool | Churn cap 20 %/cycle + hystérésis 3 cycles |
| Rollback qui restaure un état déjà incorrect | Snapshot versionné + re-probe avant restauration |

## Décision requise avant implémentation

**Mode de retrait** : human-in-the-loop d'abord (retrait *proposé* au dashboard, confirmation en un clic — **recommandé** pour démarrer), puis full auto réservé aux retraits à double consensus une fois la stabilité observée sur quelques semaines. Le plan est écrit pour les deux : le mode n'est qu'un flag (`free_discovery.retract_mode: confirm|auto`), le mécanisme est identique.
