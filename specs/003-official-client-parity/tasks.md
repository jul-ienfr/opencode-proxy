# Tasks — Parité wire officielle de la jambe free

**Feature**: `003-official-client-parity`  
**Plan**: [plan.md](./plan.md)  
**Spec**: [spec.md](./spec.md)  
**Audit**: [../../docs/AUDIT_IMPERSONATION_FORMAT_2026-11-05.md](../../docs/AUDIT_IMPERSONATION_FORMAT_2026-11-05.md)

Légende : `[ ]` à faire, `[~]` en cours, `[x]` fait, `[!]` bloqué.

---

## Phase -1 — Triage incident et rétablissement (priorité absolue)

- [ ] **T-I00** Vérifier `capacity.usable_stations` : si `0/6`, `AUTH_FAILED` ou SOCKS rejected → **Plan N** (credentials/egress) avant tout test de wire.
- [x] **T-I00b** Vérifier le token NordVPN : `python scripts/refresh_nord_credentials.py --check` ; **token accepté le 2026-11-06**, credentials service régénérés via `--apply`.
- [~] **T-I00c** Mesurer la limite de sessions : six stations + test = `AUTH_FAILED` ; six arrêtées + un test = auth OK. Ajouter les stations une par une pour trouver la limite exacte.
- [ ] **T-I01** Capturer la requête cliente exacte et son 429 : modèle demandé, corps, `Retry-After`, IP d'egress, heure.
- [ ] **T-I02** Classer le 429 : `FreeUsageLimitError` long / rate-limit court / **cooldown local** / `FreeTierError` / 403 / 502.
- [ ] **T-I03** Vérifier le trafic client réel dans `requests.db` (hors smoke test `sysone_*`) ; si absent, corriger d'abord le routage/port/modèle.
- [ ] **T-I04** Arrêter ou ralentir le smoke test `jev-1.13-free` s'il martèle le modèle (rate-limit court).
- [ ] **T-I05** Vérifier `model_global_backoff` / `model_global_until` : si 429 local, purger (Plan M) avant toute rotation.
- [ ] **T-I06** Replay exact 2.2.5 `cli` sur une IP connue : 16 en-têtes dans l'ordre, 13 outils complets, pas de `tool_choice`, TLS 1.19.5, date du jour, project `global`.
- [ ] **T-I07** Si 200 + contenu : canary 10 requêtes, puis 2 stations, puis 6.
- [ ] **T-I08** Si 429 court : attendre le RA, relancer la même requête ; ne pas rotationner.
- [ ] **T-I09** Si 429 long : rotation IP, retester le même wire.
- [ ] **T-I10** Si 403/`FreeTierError` : A/B corps complet vs placeholders, puis prompt (date/cwd), puis ordre.
- [ ] **T-I11** Si 502 : réparer tunnel/egress, ne pas toucher au wire.
- [ ] **T-I12** Consigner chaque essai dans `measurements.md` (date, IP, variante, statut, RA, contenu).
- [ ] **T-I13** Décider du plan A–M retenu et documenter le rollback.
- [x] **T-I14** Ajouter `clear_model_global(model)` (le ledger `clear_quota_buckets` ne vide pas `_model_global_until`) + test d'expiration. *(implémenté + test `test_clear_model_global_purges_target_or_all`)*
- [~] **T-I15** Matrice CLI vs desktop : `--dry-run` OK (16 en-têtes, mêmes 13 outils, UA/client seuls différents) ; live bloqué par l'egress.
- [ ] **T-I16** Matrice 1.19.5 (rollback) : 13 en-têtes, 11 outils, `max_tokens`, `tool_choice: auto` ; live sur IP fraîche.
- [ ] **T-I17** Consigner les résultats CLI/desktop/1.19.5 dans `measurements.md` et décider du profil canonique.

**Sortie** : 200 + contenu sur une IP, cause du 429 identifiée, plan de généralisation choisi.

---

## Phase 0 — Mesure A/B (bloquant avant tout changement de défaut)

- [x] **T000** Créer `specs/003-official-client-parity/measurements.md` avec la matrice A–F de `plan.md` §6. *(stub créé le 2026-11-05)*
- [ ] **T001** Écrire `scripts/free_wire_ab.py` : envoie une variante normalisée sur une IP donnée, avec témoin intercalé, et journalise statut, `Retry-After`, TTFT, contenu.
- [ ] **T002** Variante A : proxy actuel (hybride) — baseline.
- [ ] **T003** Variante B : 2.2.5 `cli` exact (ordre + corps + TLS 1.19.5).
- [ ] **T004** Variante C : 2.2.5 `desktop` exact.
- [ ] **T005** Variante D : 1.19.5 exact (rollback).
- [ ] **T006** Variante E : placeholders vs 13 définitions complètes.
- [ ] **T007** Variante F : date figée vs courante ; chemin réel vs synthétique ; project `global` vs réel.
- [ ] **T008** 20 échantillons minimum par variante, même IP / même fenêtre ; consigner les 200/403/429.
- [ ] **T009** Décider : D1/D2/D3/D6 sont-ils fonctionnels ou seulement fidélité ? Mettre à jour `plan.md` §6.

**Sortie** : `measurements.md` rempli + décision de priorité.

---

## Phase 1 — Profil unique, cible 2.2.5 `cli`

- [ ] **T100** Créer `free/wire_profile.py` : `FreeWireProfile`, `resolve_profile`, chargement/mémoïsation.
- [ ] **T101** Créer le manifeste `free/impersonation.json` schema v2 (profils `2.2.5-cli`, `2.2.5-desktop`, `1.19.5-rollback`).
- [x] **T102** Ajouter la constante d'ordre 2.2.5 complète dans `opencode.py` (`_OPENCODE_HEADER_ORDER_225`) + la choisir quand le wire porte b3/traceparent. *(fait ; e2e exact)*
- [x] **T103** Brancher `_free_fp_override` / `_get_free_fp_kwargs` sur le profil (TLS 1.19.5 pour 2.2.5). *(fait : 1.19.5 = Bun 1.4.4 = 2.2.5)*
- [~] **T104** Brancher `_official_free_headers` sur `resolve_profile` + `apply_profile` ; supprimer les choix locaux d'UA/client/ordre. *(ordre + project + trace faits ; profil complet à faire)*
- [ ] **T105** Brancher `_free_wire_body` sur le profil (clés, outils, `max_tokens_field`, `tool_choice`).
- [x] **T106** Geler `ROTATION_VERSIONS` sur `("1.19.5",)` (TLS identique au 2.2.5) ; garder 1.19.5 en rollback explicite. *(fait)*
- [~] **T107** Ajouter le test de cohérence `(version, ua, client, tls, header_order, body_keys, tools)`. *(e2e + ordre/project/trace faits ; tuple complet à faire)*
- [x] **T108** Ajouter le test e2e `tests/test_official_wire_e2e.py` : curl_cffi → serveur local, diff contre `2.2.5-1.json`. *(fait : 16 en-têtes exacts, 13 outils)*
- [~] **T109** Vérifier le diff normalisé : noms, ordre, clés, outils, schémas. *(2.2.5 fait ; 1.19.5 à faire)*

**Sortie** : wire 2.2.5 `cli` conforme ; tests verts.

---

## Phase 2 — Privacy et sémantique

- [x] **T200** Supprimer l'override project `global` dans `apply_spec` / `apply_profile`. *(fait + test)*
- [ ] **T201** Choisir `global` ou `sha256("global|ip")[:40]` ; tester les deux formats.
- [ ] **T202** Extraire le prompt système en template (`free/captures/2.2.5-system.txt` + variables).
- [ ] **T203** Rendre `Today's date` en UTC courant.
- [ ] **T204** Rendre `Working directory` / `Workspace root folder` synthétiques par identité.
- [ ] **T205** Rendre `Platform` / `Is directory a git repo` cohérents avec le profil.
- [ ] **T206** Test unitaire : aucune requête sortante ne contient le chemin réel ni la date figée.
- [x] **T207** Stocker `trace_id` dans l'identité ; span par requête ; test sur 2 requêtes. *(fait via `_TRACE_IDS` + test)*
- [ ] **T208** Passer la session en TTL long / par conversation ; test > 30 min.
- [ ] **T209** Aligner `x-opencode-directory` avec le cwd synthétique, ou le retirer.
- [ ] **T210** Test : deux IP ne partagent ni project, ni session, ni trace, ni cwd.

**Sortie** : privacy vérifiée par grep sortant + tests.

---

## Phase 3 — Outillage et tests

- [ ] **T300** Réécrire `scripts/opencode_client_sync.py` : desktop + npm, `--standalone` détecté, multi-requêtes.
- [ ] **T301** Persister `header_order`, `client`, `project`, `tls`, `body_keys`, `tool_names`, `body_sha256`.
- [ ] **T302** `--check` : code 2 si manifeste incomplet ou diff non vide.
- [ ] **T303** Créer `scripts/opencode_wire_diff.py` : normalise IDs/trace/Content-Length, imprime un diff lisible.
- [ ] **T304** Mettre à jour `tests/test_official_client_parity.py` (supprimer `OFFICIAL_ORDER` hybride, tester le chemin per-IP).
- [ ] **T305** Mettre à jour `tests/test_identity_generator.py` (pas de 1.19.5 + corps 2.2.5 ; tuple cohérent).
- [ ] **T306** Mettre à jour `tests/test_free_wire_complet.py` (ordre 2.2.5 réel).
- [ ] **T307** CI : `pytest` + `opencode_client_sync.py --check` (fixtures, hors réseau).
- [ ] **T308** Documenter la procédure de capture dans `free/captures/README.md`.

**Sortie** : un changement d'UA/ordre/outil fait échouer un test ; le sync détecte une release.

---

## Phase 4 — Mode `compat` propre

- [ ] **T400** Séparer `official` et `compat` dans le profil.
- [ ] **T401** En `compat`, utiliser les 13 définitions complètes + outils client.
- [ ] **T402** Mesurer la taille du corps et le budget `free.max_wire_bytes`.
- [ ] **T403** Supprimer les placeholders par défaut ; flag `OPENCODE_FREE_PLACEHOLDERS=1` si nécessaire.
- [ ] **T404** Log structuré : mode, profil, outils, taille, hash système.
- [ ] **T405** Mettre à jour `docs/FREE_PROBE_FORMAT.md` et `docs/identity-generator.md`.

**Sortie** : mode visible, définitions réalistes, A/B confirmé.

---

## Phase 5 — Diversité versionnée (optionnelle)

- [ ] **T500** Capturer les profils complets 1.18.31 / 1.19.0 / 1.19.3 / 1.19.5 (headers, ordre, corps, titre, TLS).
- [ ] **T501** Ajouter `profile` au vault ; `version_for(ip)` retourne un profil complet.
- [ ] **T502** Test de cohérence par tuple pour chaque version.
- [ ] **T503** Réactiver la rotation de versions seulement si chaque tuple est prouvé.
- [ ] **T504** A/B de non-régression sur 4 profils.

**Sortie** : 4 profils complets, 4 tests, A/B sans régression.

---

## Phase 6 — Campagne de tests complète

- [ ] **T600** Unitaires : ordre 16 en-têtes, tuple cohérent, project `global`, prompt sans chemin, trace stable, corps 13 outils.
- [~] **T601** Contrat : diff normalisé proxy ↔ `2.2.5-1.json` et ↔ `1.19.5-1.json`. *(e2e 2.2.5 fait ; 1.19.5 à faire)*
- [x] **T602** E2E local : wire réel per-IP via curl_cffi, ordre compris. *(fait)*
- [ ] **T603** TLS : ClientHello local, JA3/MD5 1.18.31 / 1.19.5 / 2.2.5.
- [ ] **T604** Intégration : SSE→JSON, non-stream, titre, conversation > 30 min, deux IP isolées.
- [ ] **T605** Canary live : 1 IP, 1 puis 10 requêtes, 200 + contenu ; puis 2 puis 6 stations.
- [ ] **T606** Charge : 6 stations, 8 requêtes/station, pas de hedge storm, RA court respecté.
- [ ] **T607** Privacy : aucun chemin/project/AGENTS.md/ID partagé dans les requêtes sortantes.
- [ ] **T608** Rollback : 1.19.5, legacy, header_order off ; 200 sans redéploiement.
- [ ] **T609** Consigner les résultats et les preuves dans `measurements.md`.

**Sortie** : tous les niveaux verts, 10/10 canary, 6/6 stations.

---

## Suivi

| Phase | Propriétaire | Statut | Preuve |
|---|---|---|---|
| -1 — Triage incident | | `[~]` | CLI/desktop testés jusqu'au quota ; egress bloqué (token 401) |
| 0 — Mesure | | `[~]` | `measurements.md` campagne 1 |
| 1 — Profil | | `[ ]` | diff wire vide |
| 2 — Privacy | | `[ ]` | grep sortant vide |
| 3 — Outillage | | `[ ]` | `--check` + CI |
| 4 — Compat | | `[ ]` | A/B |
| 5 — Diversité | | `[ ]` | 4 tuples |
| 6 — Tests | | `[ ]` | campagne complète |
