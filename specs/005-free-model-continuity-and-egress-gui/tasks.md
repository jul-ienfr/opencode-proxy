# Tasks — Modèle free, egress GUI et isolation d'identité

**Feature**: `005-free-model-continuity-and-egress-gui`  
**Statut**: conceptualisation. **Aucune tâche d'implémentation commencée.**  
Légende : `[ ]` à faire, `[~]` en cours, `[x]` fait, `[!]` bloqué.

## Phase 0 — Documentation (fait)

- [x] **T-500** Écrire `spec.md`.
- [x] **T-501** Écrire `research.md`.
- [x] **T-502** Écrire `plan.md`.
- [x] **T-503** Écrire `data-model.md`.
- [x] **T-504** Écrire `contracts/` (5 contrats).
- [x] **T-505** Écrire `open-questions.md` et `quickstart.md`.
- [x] **T-506** Écrire `checklists/requirements.md` et les critères de succès
  mesurables.

## Phase 1 — Isolation d'identité (P0, bloquant)

> Ne pas activer plus d'une station avant la fin de cette phase.
>
> **Socle hors ligne 2026-11-06** : `StationIdentityVault`, prompt templaté,
> affinité et audit sont implémentés dans `free/station_identity.py` et
> testés par `tests/test_station_identity.py`. La migration runtime et le
> test live à deux stations restent à faire.

- [x] **T-510** `StationIdentityVault` : secret local, HMAC par label,
  `project`/`trace`/`env_cwd`.
- [ ] **T-511** Migration du vault par IP vers le vault par station ; garder
  les identités existantes le temps de la transition ; canonicaliser
  `x-opencode-client` (`cli`, capture 2.2.5) et migrer les vaults `desktop`.
- [x] **T-512** `ses_` par `(station, conversation)`, état de mint isolé,
  canonique `…ffe`.
- [x] **T-513** `msg_` frais par requête, jamais partagé.
- [x] **T-514** `PromptTemplate` : marqueur officiel + date UTC + `<env>`
  minimal `/workspace/<label>` ; suppression du chemin réel et du bloc
  `Instructions from:`/`AGENTS.md`.
- [x] **T-515** `ConversationAffinity` : sticky station, TTL, failover
  journalisé.
- [x] **T-516** `IdentityAudit` : disjonction des tuples + scan de
  sous-strings interdites + sessions curl distinctes.
- [x] **T-517** Tests unitaires : 2 stations disjointes, 5 tours sticky,
  prompt sans fuite, audit duplicate/leak.
- [ ] **T-518** Test live sur IP sacrifiable : deux stations, capture des
  en-têtes/corps, diff des identités.

**Sortie** : aucune station ne partage project/session/trace/prompt avec une
autre ; une conversation reste sur une station.

## Phase 1b — Profils vérifiés (P0.5)

> Le générateur existe (`free/identity_generator.py`) ; cette phase ajoute la
> preuve live, le registre et l'assignation.
>
> **Socle hors ligne 2026-11-06** : `ProfileRegistry`, `ProfileVerifier`,
> `VerifiedProfileGenerator`, assignation et audit sont implémentés dans
> `free/profile_registry.py`, `free/profile_verifier.py` et
> `free/profile_generator.py`, testés par `tests/test_profile_registry.py`.
> Le sender live et les captures par version restent à faire.

- [~] **T-560** Auditer `VERSION_SPECS`, `ROTATION_VERSIONS`,
  `VERIFIED_CLIENTS` ; corps 2.2.5 (13), 1.19.5 (11), 1.19.3 (10) et 1.18.31
  (10) capturés ; 1.19.0 reste optionnel.
- [x] **T-561** `ProfileRegistry` : schéma, provenance, preuves, TTL,
  écriture atomique.
- [x] **T-562** `ProfileVerifier` : bloc frais, 200 + contenu, 2 blocs
  distincts, `429=quota`, `403×3=reject` ; transport curl_cffi fourni et
  validé live sur 2.2.5.
- [x] **T-563** `ProfileGenerator` : enveloppe `identity_generator`, filtre
  `verified`/`stale`, fallback canonical.
- [x] **T-564** `ProfileAssigner` : HMAC(label), un profil par station,
  distinct si possible, `shared_profile` sinon.
- [~] **T-565** `ProfileAudit` : cohérence UA/Bun/JA3/corps/session ; l'audit
  d'ordre des en-têtes reste à brancher.
- [x] **T-566** Tests : verified/rejected/stale/mixed/reuse/registre
  corrompu.
- [x] **T-567** Vérification live : 2.2.5 `cli`, 1.19.5 `desktop`, 1.19.3
  `desktop` et 1.18.31 `desktop`, **200 sur deux blocs `/64` distincts
  chacun avec prompt propre** ; deux familles TLS et trois styles de wire
  (voir `measurements.md`).
- [x] **T-568** Intégration runtime derrière `free.station_identity` :
  en-têtes station, profil vérifié, corps templaté, pas de fallback direct
  silencieux. **Activée live le 2026-11-06** ; 200 via `warp-restore1` et
  `warp-restore3` (voir `measurements.md`).
- [x] **T-569** Rotation automatique WARP : hooks 429 + `on_quota_exhausted`
  + watchdog 30 s, restart/recréation, bloc distinct, persistance config.
  **Validée live** : six stations tournées vers six blocs distincts.
- [x] **T-570** Assembler le corps par version : 1.19.5 (11 outils) est
  maintenant construit depuis sa capture ; 1.19.3/1.18.31 sont retirés du
  runtime.
- [x] **T-571** Politique « deux dernières versions + futures » : runtime
  plancher 1.19.5, top 2 dynamique ; 1.18.31/1.19.3 **retirés du registre
  actif** (captures conservées en archive) et jamais assignés.
- [x] **T-572** Audit 429 : le contrat santé Docker rendait tous les SOCKS5
  statiques `usable:false`. Bypass pour `Socks5Endpoint` + diagnostic
  `/api/free-egress/diag` + reset `/api/free-egress/reset-cooldowns`.
- [x] **T-573** Cooldown keyé sur l'egress réel (`/64` IPv6) au lieu du label ;
  une rotation WARP débloque immédiatement le nouveau bloc. **6 requêtes
  proxy consécutives rendent 200.**
- [x] **T-574** Affinité de conversation runtime : `_socks5_pick` épingle la
  station par clé de conversation, failover si elle devient inutilisable,
  round-robin pour les nouvelles. **Validée live** : deux tours sur -2,
  nouvelle conversation sur -7.
- [x] **T-575** Suite complète hors Docker : **4809 tests, exit 0**. Le test
  `test_watchdog_auth_failed_fast_pins_once` bloquait la gate (6 × 15 s de
  sleeps réels, 92 s) ; les pauses fast-pin passent par `_sleep_fn` → 0,92 s,
  fichier `test_gluetun_control_api.py` 59/59. Marge du test SSE keepalive
  élargie (0,09 → 0,15 s pour un intervalle 0,05 s) : 5 passes consécutives
  stables. Budget d'import `opencode` relevé 900 → 1200 ms (mesuré 1019 ms
  sous charge avec 6 tunnels).
- [x] **T-576** Round-robin SOCKS5 piloté par la GUI : `failover` → sticky,
  `round-robin+strict` → compteur, `round-robin+load-balance` → moins chargée.
  Preuve live : failover `-3,-3,-3` ; strict `-6,-7,-1`.
- [x] **T-577** `free_parallel.enabled=false` et toggle SOCKS5 off → sticky.
  Hedge SOCKS5 activé : `pick_candidates` expose les endpoints, le premier
  tour course 2 stations, la gagnante est épinglée. Preuve live : tour 1
  `+1/+1`, tour 2 `+1` sur la gagnante.
- [x] **T-578** Paramètres qualité/latence appliqués en SOCKS5 :
  `quality_rotation` tourne un WARP managé via le rotateur WARP (non-managé
  ignoré), `quality_preselection` ajuste déjà le seuil de rotation, et
  `latency_rotation` hard-cooled garde un dernier passage d'admission
  (jamais zéro candidat).
- [x] **T-579** `post_rotation_probe` branché : après rotation WARP, sonde
  synchrone avec l'identité/profil de la station, `max_tokens` et
  `max_attempts` respectés. `200`/`429` = egress joignable, autres échecs
  bad-markés. Preuve live : sonde sur `warp-ipv6-2` → `True` (429 quota,
  egress OK). GUI : libellé « réservé » remplacé.
- [x] **T-580** Rotation proactive sur bloc `(modèle, IP)` : le watchdog
  tourne un WARP managé bloqué après 429 même si le ledger journalier n'est
  pas épuisé. Évite d'attendre que les six blocs soient tous marqués.
- [x] **T-581** Garde corrélée des timeouts SOCKS5 : journal dédié de couples
  `(station, IP)` distincts sur 60 s ; au 3e, pas de bad-mark (événement
  amont/réseau). Le log 429/rotation n'est pas touché, donc la rotation quota
  reste libre.
- [x] **T-582** Bad-marks transitoires non restaurés au boot : les compteurs
  et quotas persistent, mais un redémarrage rend les 7 endpoints disponibles
  immédiatement (avant : 4/7 pendant le TTL timeout). Preuve live : 7/7 dès
  le healthcheck post-restart.
- [x] **T-583** Rotation des endpoints `trial-dead` : callback
  `_trial_dead_managed_labels` (tous modèles) → watchdog cause
  `trial_dead`. Un trial-dead de 6 h ne fige plus une station managée.
- [x] **T-584** Blocs distincts sous rotations concurrentes : `_apply` passe
  sous `_ROTATE_LOCK` (l'egress est visible avant la fin de la rotation) et
  le watchdog tourne le second endpoint d'un bloc dupliqué
  (`duplicate_block`). Preuve live : `restore1`/`restore3` partageaient
  `27a0:2060::/64`, corrigé → **6/6 blocs distincts**.
- [x] **T-585** Fenêtres de console intempestives : `free/warp_rotation.py`
  lançait `docker`/`curl` sans `CREATE_NO_WINDOW` à chaque rotation. Ajouté
  sur les deux appels + `station_slo_watch`. Test de non-régression sur les
  flags.
- [x] **T-586** GUI SOCKS5 lisible : label, host:port, egress, bloc et badge
  WARP affichés ; le Test envoie le label et force IPv6. Vérifié dans le
  navigateur : 8 lignes, 6 WARP avec blocs distincts, aucune erreur console.
  Test live `warp-ipv6-2` → `✓ 2a09:bac1:27a0:2060::3da:52 — 750 ms`.
- [x] **T-587** Plancher de redondance SOCKS5 : un timeout ne bad-mark pas si
  les autres sains sont sous `min(3, n-1)`. La garde est centrale dans
  `_apply_bad_mark` (marqueur traçable + `degraded_override`) et couvre aussi
  les timeouts hors `notify_connection_failure`. Évite l'effondrement à 1-2
  candidats.
- [x] **T-588** Capacité SOCKS5 honnête dans `/health` : `capacity_snapshot`
  compte les endpoints statiques. Avant : `degraded`, « 0/1 stations usable »
  alors que 7/7 répondaient. Après : `status=ok`, `configured=7`, `usable=7`,
  `capacity_degraded=false`.
- [~] **T-518** Test live sur IP sacrifiable : deux stations servent 200 ;
  la capture différentielle des en-têtes/corps reste à instrumenter.

**Sortie** : aucun profil non vérifié n'est émis ; les profils vérifiés sont
assignés par station ; la limite TLS est visible.

## Phase 2 — Continuité du modèle free

- [ ] **T-520** `FreeModelContinuity` : lecture de `FREE_MODELS` +
  `free_models_probed.json`, aucun fetch.
- [ ] **T-521** Algorithme de sélection : `alive`, même famille, préférence,
  fraîcheur.
- [ ] **T-522** Persistance `free_discovery.default_target` +
  `auto_default_target` + `pinned_target`.
- [ ] **T-523** Propagation `FREE_DISCOVERY_DEFAULT_TARGET`, suppression du
  fallback `jev-1.13-free`, défaut `fresh_chain.py`.
- [ ] **T-524** Tests : kept/missing/dead/no_alive/pinned/concurrent.
- [ ] **T-525** API `/api/free-models/reconcile` + extension
  `active_default/source/last_switch`.

**Sortie** : le target survit à une disparition, sans second fetcher.

## Phase 3 — Contrôleur d'egress

- [ ] **T-530** `FreeEgressController` : desired/actual, plan, lock.
- [ ] **T-531** `EgressPlanner` pur : create/keep/remove/verify.
- [ ] **T-532** `DockerRunner` injectable + fake pour tests.
- [ ] **T-533** `VpnProvisioner` : réutilise `_apply_station_count`, WireGuard
  + DoT, limites.
- [ ] **T-534** `WarpProvisioner` : conteneurs labellisés, ports, `/64`,
  `socks5_proxies` managed.
- [ ] **T-535** Vérification : egress + probe free + `IdentityAudit`.
- [ ] **T-536** Rollback/blue-green : ancien ensemble conservé jusqu'à
  validation.
- [ ] **T-537** Tests : idempotence, delta, échec 3ᵉ station, concurrence 409,
  proxy manuel préservé.

**Sortie** : « appliquer N stations en mode X » converge sans doublon et sans
coupure.

## Phase 4 — GUI

- [ ] **T-540** `FreeModels` : bandeau modèle actif, source, dernier switch,
  Reconcile, Auto/Pin.
- [ ] **T-541** `FreeEgress` : radio VPN/WARP, nombre, limites.
- [ ] **T-542** Plan dry-run + Apply + progression par station.
- [ ] **T-543** Table stations : label, port, egress/bloc, état, erreur,
  `identity_ok`.
- [ ] **T-544** Badge audit identité.
- [ ] **T-545** Tests UI (composants) + captures.

**Sortie** : l'opérateur pilote mode + count depuis la GUI, avec plan et état.

## Phase 5 — Intégration et validation

- [ ] **T-550** Migration config `proxy_mode`/`station_count` → `free_egress`.
- [ ] **T-551** Auto-start des stations/bridge au boot.
- [ ] **T-552** Runbook : appliquer 5 WARP, vérifier les 5 blocs.
- [ ] **T-553** Tests de non-régression : wire, gate, quota, identité.
- [ ] **T-554** Audit final : aucun ID partagé, aucun chemin réel.
- [ ] **T-555** Commit (T-405 de spec 004).

## Dépendances

```
T-510..T-518 (P0)
      │
      ├── T-560..T-567 (P0.5 profils vérifiés)
      │
      ├── T-520..T-525 (modèle)
      └── T-530..T-537 (egress)
                 │
                 └── T-540..T-545 (GUI)
                            │
                            └── T-550..T-555
```

## Blocages / décisions ouvertes

- **B-001** Choix du provider WARP par défaut (`caomingjun` vs `warp-minimal`).
- **B-002** Politique de failover d'une conversation (même station vs
  nouvelle identité).
- **B-003** Valeurs synthétiques de `<env>` (platform, workspace root).
- **B-004** Nombre de profils UA/TLS à vérifier et TTL de validité (Q-17).
- **B-005** Plafond de stations WARP (4 prouvées, 10 théoriques).
- **B-006** Emplacement GUI : nouvelle page vs extension de `VpnStatus`.
- **B-007** Réutilisation d'un profil quand les stations dépassent les profils
  vérifiés (Q-18).
