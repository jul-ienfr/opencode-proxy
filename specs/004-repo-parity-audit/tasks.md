# Tasks — Audit des dépôts et parité free

**Feature**: `004-repo-parity-audit`  
**Spec**: [spec.md](./spec.md)  
**Plan**: [plan.md](./plan.md)  
**Research**: [research.md](./research.md)  
**Mesures**: [measurements.md](./measurements.md)

Légende : `[ ]` à faire, `[~]` en cours, `[x]` fait, `[!]` bloqué.

---

## Phase 0 — Documentation de l'audit

- [x] **T-000** Écrire `spec.md` : objectifs, exigences, critères d'acceptation.
- [x] **T-001** Écrire `research.md` : fiche par dépôt, faits de code, qualité
  de preuve, contradictions.
- [x] **T-002** Écrire `plan.md` : architecture, journal des changements,
  décisions, risques, rollback.
- [x] **T-003** Écrire `measurements.md` : tests live, état runtime, suite.
- [x] **T-004** Écrire `data-model.md` : entités, invariants, transitions.
- [x] **T-005** Écrire `contracts/` : wire, `<env>`, cooldown, alias d'outils.
- [x] **T-006** Écrire `docs/AUDIT_REPOS_2026-11-06.md` : audit consolidé et
  matrice de divergences.
- [x] **T-007** Écrire `quickstart.md` : vérification sans secret et sans
  action destructive.

---

## Phase 1 — Corps et gate

- [x] **T-100** Placeholders : `properties` non vide (`path`) sur les 13
  outils officiels. Test `test_official_placeholders_are_minimal_but_keep_names`.
- [x] **T-101** Garde `<env>` dans `_free_wire_body`, y compris sur le chemin
  « signature officielle ». Tests `test_env_guard_*`.
- [x] **T-102** Sémantique du **premier couple** `<env>` : `<env></env>` vide
  ou `<env x="1">` devant un couple valide → invalide. Test
  `test_env_guard_rejects_empty_first_pair_before_valid_one`.
- [x] **T-103** Retirer `tool_choice: "none"` de toute copie free. Test
  `test_tool_choice_none_is_dropped_from_free_wire`.
- [x] **T-104** Adapter les fixtures officielles pour qu'elles contiennent un
  `<env>` valide (`V2_SYSTEM`, `test_official_signature_is_never_trimmed`).

**Sortie** : un corps free ne peut plus partir sans `<env>` valide, avec un
premier couple invalide, ou avec `tool_choice: none`.

---

## Phase 2 — Cooldown et quota

- [x] **T-200** Ajouter `_FREE_COOLDOWN_CAP_DEFAULT` et
  `_free_cooldown_cap_seconds()` (`free.cooldown_cap_s`, défaut 1200 s,
  0 = désactivé).
- [x] **T-201** Plafonner `_free_429_cooldown_seconds` (header et corps).
- [x] **T-202** Appliquer le plafond **après** le `max` avec
  `trial_dead_cooldown_s` dans l'appelant.
- [x] **T-203** Ajouter `QuotaLedger.max_block_s` et plafonner `reset_at`
  quand `ra > 0`.
- [x] **T-204** Passer `ip_quota.ledger_max_block_s` depuis `free/pool.py`.
- [x] **T-205** Config : `free.cooldown_cap_s: 1200` et
  `ip_quota.ledger_max_block_s: 1200`.
- [x] **T-206** Tests : cap actif, cap désactivé, ledger, mise à jour des
  attentes historiques.

**Sortie** : une station 429 n'est plus parkée 6–24 h ; elle est re-testable
après 20 min, et le ledger reste cohérent.

---

## Phase 3 — Vérification live

- [x] **T-300** Redémarrer le proxy et confirmer l'écoute sur `:4000`
  (PID 19116).
- [x] **T-301** `ping` → 200 `pong`.
- [x] **T-302** `tool_choice: none` + `<env></env>` invalide + `read` → 200.
- [x] **T-303** `tool_choice: none` + `<env>` valide → 200.
- [x] **T-304** Vraie demande de code → 200.
- [x] **T-305** Stream SSE → 200, `[DONE]` présent, frame `cost` observée.
- [x] **T-306** Suite ciblée `free|official|wire|identity|quota|pool|trial_dead`
  → exit 0 (~140 tests).
- [x] **T-307** WARP : SOCKS5 `11080`, egress `104.28.230.202`, probe 2.2.5
  exacte → 200.

---

## Phase 4 — Divergences ouvertes

- [ ] **T-401** **UA 2.2.5** : créer un profil `2.2.5` dans
  `free/identities.py` (`UA=opencode/latest/2.2.5/cli`, client `cli`,
  TLS Bun 1.4.4) et supprimer le mélange UA 1.19.5 + corps 2.2.5. Nécessite
  une re-capture et un test de cohérence de tuple.
- [ ] **T-402** **Session** : tester la réutilisation d'une session sur des
  requêtes sans historique **sur une IP sacrifiable** (WARP ou VPN), jamais sur
  l'IP directe. Décider : stable par conversation (actuel) ou par appel
  (afree).
- [x] **T-403** **WARP station 2** : intégré via `proxy_mode: socks5` avec
  deux entrées — WARP `11080` et pont direct `11084`. Rotation observée
  `11080 → 11084 → 11080 → 11084`, 4 × 200 ; stream 200. Contrat :
  [contracts/egress.md](./contracts/egress.md).
- [x] **T-404** **Six stations — atteint via WARP**. Quatre conteneurs WARP
  (`11080`, `11085`, `11086`, `11087`) donnent quatre `/64` distincts ;
  avec WARP IPv4 et le direct, six egresses distinctes ont été démontrées
  (6 × 200). Sur demande opérateur, le direct est désactivé : **5 blocs WARP
  actifs** (5 × 200). NordVPN n'est plus nécessaire ; le superviseur reste
  arrêté.
- [ ] **T-405** **Commit** : committer l'état avec un message de référence
  (`spec 004`) ; sans commit, le `git diff` est la seule trace.
- [ ] **T-406** **Quota** : calibrer `quota_per_ip` (500 actuel vs ~200 req/5 h
  mesuré par afree) et vérifier le comportement du ledger sur une journée.
- [ ] **T-407** **Frame `cost`** : tester un client OpenAI strict (SDK Python,
  codex) sur le stream ; si rejet, couper la frame ou la déplacer après
  `[DONE]`.
- [ ] **T-408** **Keep-alive après 429** : mesurer si la session curl_cffi
  réutilisée après un 429 propage l'échec ; si oui, `evict` au lieu de
  `checkin`.
- [ ] **T-409** **Alias d'outils** : implémenter `fn_<camelCase>_ex` pour les
  noms snake_case **si** un modèle free les refuse ; réutiliser
  `sanitize_tool_names` / `restore_tool_name`.
- [ ] **T-410** **Endpoint par modèle** : vérifier `free_endpoint_for` sur
  `muse-*`, `spark-*`, `jev-*` avec une requête réelle ; le code route déjà,
  mais aucun test live récent.
- [ ] **T-411** **Trace** : vérifier si le trace ID stable par session (30 min)
  divergence du trace par processus (rianilham) ou par requête (xljiulang) ;
  cosmétique sauf si le gate le lit.
- [ ] **T-412** **Réconcilier spec 003** : marquer dans
  `specs/003-official-client-parity/tasks.md` ce qui est fait (ordre, corps,
  project, trace) et ce qui reste (profil 2.2.5 unique, matrice live).
- [x] **T-413** **Contradiction du gate — résolue** : `ebouther` mesure
  `read` seul (2026-10-16, une variable à la fois) ; le nombre et les autres
  noms sont sans effet. Notre grille à 13 outils reste un sur-ensemble sûr.
- [x] **T-416** **Voie IPv6 `/64` — résolue côté WARP.** L'hôte Windows n'a
  aucune IPv6 globale, mais le tunnel WARP fournit `2a09:bac5:32d4:be::/64` ;
  il est exposé comme station (T-421). Reste pour plusieurs `/64` :
  `warp-minimal` (T-422).
- [ ] **T-417** **Probe de gate local** : adapter `gate-probe.py` d'`ebouther`
  à `/inference/openai/v1` + `<env>` + `max_completion_tokens`, puis le lancer
  sur une IP sacrifiable (WARP ou VPN). Vérifier les 4 gates + `<env>`.
- [x] **T-418** **Session canonique** : test `tests/test_session_canonical.py`
  (layout `ffe`, horloge implicite, forme 30 caractères) ; le mint
  `free/identities.py` est conforme.
- [ ] **T-419** **Geo-gate** : si `muse-spark-*` ou `beam-free` reviennent
  dans le catalogue free, tester une sortie US (WARP/Wasmer Edge) ; le
  geo-gate n'est pas corrigeable par les en-têtes.
- [x] **T-420** **Quota par bloc** : `test_socks5_ipv6_station.py` vérifie
  `server_ip_key` `/64` ; le reset 00:00 UTC est documenté dans
  `contracts/cooldown.md`.
- [x] **T-421** **Station IPv6 WARP** : entrée `warp-ipv6` (`ipv6: true`,
  label), `IPRESOLVE=6` par session, egress réel
  `2a09:bac5:32d4:be::13:23e` dans le ledger. **6 × 200** en rotation
  IPv6 → direct → IPv4. Doc : `docs/IPV6_WARP_STATION_2026-11-06.md`.
- [x] **T-422** **Multi-exits — atteint via conteneurs WARP**. Quatre
  conteneurs `caomingjun/warp` = quatre `/64`. `warp-minimal` (MASQUE) reste
  une alternative plus légère mais son enregistrement est throttlé ; non
  nécessaire pour l'objectif actuel.
- [ ] **T-423** **Swap du prompt de titre** (`openrot`) : vérifier si nos
  requêtes de titre prennent un 403 `FreeTierError` ; si oui, remplacer le
  prompt par l'équivalent reformulé.
- [ ] **T-424** **Rotation de projet sur 429** (`acruz6421`) : tester si un
  `x-opencode-project` neuf débloque une IP 429. La rotation de session est
  déjà prouvée inefficace (3 × 429).
- [ ] **T-425** **Reste du balayage** : lire `CarbonWalls/opencode-unlimited`
  (2026-11-06) et `weiyentan/opencode-gateway` (2026-11-05) ; classer ou
  archiver.
- [ ] **T-414** **Démarrage automatique du pont direct** : lancer
  `scripts/start_direct_socks5.ps1` au boot (tâche planifiée ou wrapper de
  démarrage du proxy) ; sans lui, le pool perd le round-robin et garde WARP.
- [ ] **T-415** **Failover WARP → direct** : simuler un 429 ou arrêter
  `warp-test`, vérifier que la requête suivante passe par `11084` sans 500 ;
  mesurer le temps de bad-mark.

---

## Blocages connus

- **B-001** Aucune IP VPN fraîche le 2026-11-06 : les 16 hôtes historiques
  testés répondent 429. WARP fournit une IP utilisable mais non rotative.
- **B-002** La limite de sessions NordVPN n'est pas alignée sur six stations.
- **B-003** Le test de shadow-ban de session est destructif pour l'IP directe ;
  il doit attendre une IP sacrifiable.

## Prochaine action recommandée

`T-404` (limite de sessions) et `T-403` (WARP station 2) pour retrouver de la
capacité, puis `T-401` (profil 2.2.5) pour supprimer la dernière incohérence
d'identité. `T-405` (commit) doit précéder toute nouvelle campagne.
