# 005 — Continuité du modèle free, egress GUI, isolation d'identité et profils vérifiés

Paquet Spec Kit de **conceptualisation**, avec le socle P0.5 hors ligne déjà
implémenté (`free/profile_registry.py`, `free/profile_verifier.py`).

## Les quatre sujets

1. **Modèle free** : ne plus dépendre d'un id hardcodé. Utiliser le système
   existant (`FREE_MODELS`, `free_models_probed.json`,
   `ensure_free_models_on_boot`) et ajouter uniquement une politique qui
   choisit/persiste un target `alive` quand le configuré disparaît.
2. **Egress GUI** : choisir `VPN (NordVPN)` ou `WARP`, définir le nombre de
   stations, voir le plan, appliquer ; le système crée/vérifie/draine les
   stations dans le mode choisi.
3. **Isolation d'identité (P0)** : chaque station doit avoir ses propres
   `project`, `ses_`, `msg_`, trace et `<env>` ; une conversation reste sur une
   station ; le prompt ne doit contenir aucun chemin réel. **Bloquant avant
   d'activer plus d'une station.**
4. **Profils vérifiés (P0.5)** : le générateur existant ne doit émettre que des
   profils ayant réellement répondu 200 avec contenu sur des blocs distincts ;
   registre, TTL, rejet des tuples mixtes, assignation par station.

## Lire dans l'ordre

1. [spec.md](./spec.md)
2. [research.md](./research.md)
3. [plan.md](./plan.md)
4. [data-model.md](./data-model.md)
5. [contracts/](./contracts/)
   - [model-continuity.md](./contracts/model-continuity.md)
   - [egress-provisioning.md](./contracts/egress-provisioning.md)
   - [gui-egress-api.md](./contracts/gui-egress-api.md)
   - [identity-isolation.md](./contracts/identity-isolation.md)
   - [verified-profiles.md](./contracts/verified-profiles.md)
   - [warp-rotation.md](./contracts/warp-rotation.md)
6. [tasks.md](./tasks.md)
7. [measurements.md](./measurements.md)
8. [checklists/requirements.md](./checklists/requirements.md)
9. [open-questions.md](./open-questions.md)
10. [quickstart.md](./quickstart.md)

## Décisions proposées

- Pas de second fetcher : le probe existant est la source de vérité.
- `free_discovery.auto_default_target: true`, `pinned_target` pour figer.
- Nouveau bloc `free_egress` (mode, station_count, providers), migration
  depuis `proxy_mode`/`station_count`.
- `caomingjun/warp` par défaut ; `warp-minimal` en option.
- `StationIdentity` par label + HMAC d'un secret local.
- Affinité de conversation obligatoire.
- Prompt templaté minimal (`/workspace/<label>`).
- Audit anti-corrélation bloquant.
- Générateur de profils vérifiés (P0.5) : registre avec preuves live, TTL,
  rejet des tuples mixtes, assignation déterministe par station.
- Le TLS/JA3 et l'UA restent partagés tant qu'un seul profil est vérifié ;
  l'audit expose ce résidu dans `shared_vectors` et ne prétend pas à une
  unlinkability totale.

## Socle P0.5 livré (hors ligne)

- `free/profile_registry.py` — `ProfileRegistry`, `VerifiedProfile`,
  `ProfileEvidence`, TTL, assignation HMAC, audit de cohérence.
- `free/profile_verifier.py` — `ProfileVerifier`, sender injectable,
  `200×2` sur blocs distincts, `429=quota`, `403×3=reject`.
- `free/profile_generator.py` — `VerifiedProfileGenerator`, enveloppe
  `identity_generator` et filtre `verified`.
- `free/profile_live.py` — requête de vérification P0/P0.5 (headers station,
  prompt templaté), transport injectable.
- `free/profile_transport.py` — adaptateur curl_cffi (proxy, JA3, sigalgs,
  ordre des en-têtes, IPv6), testé avec un faux Session.
- `free/warp_rotation.py` — rotation automatique des WARP managés : restart,
  recréation, health, egress, bloc distinct, persistance, sonde
  post-rotation.
- `tests/test_warp_rotation.py` — 12 tests, aucun docker réel.
- `tests/test_profile_registry.py` — 15 tests, aucun réseau.
- `tests/test_profile_live.py` — 5 tests, aucun réseau.
- `tests/test_profile_transport.py` — 5 tests, aucun réseau.
- `free/station_identity.py` — `StationIdentityVault`, `StationIdentity`,
  prompt templaté, `ConversationAffinity`, audit P0.
- `free/runtime_identity.py` — résolution station → identité/profil, en-têtes
  et corps templatés.
- `tests/test_station_identity.py` — 23 tests, aucun réseau.
- `tests/test_runtime_identity.py` — 11 tests, aucun réseau.
- `tests/test_socks5_affinity.py` — 18 tests, aucun réseau : affinité,
  TTL glissant, failover sticky, round-robin piloté par la GUI, modes
  strict/load-balance/hedge, épinglage du gagnant de hedge et admission
  d'une flotte hard-cooled par `latency_rotation`.
- `tests/test_socks5_quality_rotation.py` — 2 tests : `quality_rotation`
  tourne un WARP managé, ignore un proxy statique non managé.
- `free/profiles/verified_profiles.json` — registre **actif** : 2.2.5 `cli`
  et 1.19.5 `desktop`, deux blocs `/64` distincts chacun. Les preuves
  1.19.3/1.18.31 restent dans `measurements.md` comme archive historique,
  mais ont été retirées du registre actif.

Le sender live est branché (`free/profile_transport.py`) et l'intégration
runtime est derrière `free.station_identity` (activé sur cette instance). Le
prompt est templaté avant tout envoi.

## État

- **Spec**: Draft, conceptualisation complète.
- **Paquet**: spec, research, plan, data-model, 6 contrats, tasks, checklist,
  questions ouvertes, quickstart.
- **Code**: socles P0 et P0.5 hors ligne ajoutés (identité par station,
  prompt templaté, affinité, audit, registre, vérificateur, assignation,
  pont de requête, transport curl_cffi, tests) ; deux profils actifs
  2.2.5/1.19.5, deux archives 1.19.3/1.18.31, runtime deux dernières versions,
  rotation des six WARP managés ; six blocs distincts, config persistée.
- **Cause racine du 429** : le contrat santé Docker était appliqué aux
  SOCKS5 statiques (`usable:false`), et le cooldown était keyé sur le label
  au lieu de l'IP. Corrigé : 6 requêtes proxy consécutives → **200**.
- **Affinité de conversation** : deux tours d'une même conversation sont
  restés sur la station -2 ; une nouvelle conversation est allée sur -7.
- **Pilotage GUI** : `routing=failover` → 3 conversations sur la même
  station ; `round-robin`/`strict` → 3 stations distinctes ;
  `socks5_auto_rotate=false` ou `free_parallel.enabled=false` → sticky.
  En `hedge`, le premier tour course 2 stations puis la gagnante est épinglée.
- **Suite complète** : `pytest -k "not docker"` → **4808 tests, exit 0**.
  Le test fast-pin qui bloquait à 37 % (92 s) utilise désormais `_sleep_fn`
  et passe en 0,92 s ; `tests/test_gluetun_control_api.py` : 59/59.
- **Compatibilité runtime**: deux dernières versions uniquement —
  `2.2.5` (13 outils) et `1.19.5` (11 outils) — plus les futures `>= 1.19.5`.
  Les profils 1.19.3/1.18.31 sont retirés du runtime et conservés comme
  preuves historiques.
- **Revue**: 122 critères de qualité, dont 16 critères ouverts correspondant
  aux 18 questions d'opérateur dans
  [checklists/requirements.md](./checklists/requirements.md) et
  [open-questions.md](./open-questions.md).
- **Prochaine étape**: surveiller la stabilité 6/6 sur plusieurs cycles de
  rotation et terminer la suite complète hors tests live/Docker lents.
