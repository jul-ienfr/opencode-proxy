# Quickstart — lire et valider la conception

**Feature**: `005-free-model-continuity-and-egress-gui`  
**Statut**: conceptualisation. **Aucune commande d'implémentation n'existe
encore.**

## 1. Ordre de lecture

1. [spec.md](./spec.md) — objectifs, exigences, critères.
2. [research.md](./research.md) — état réel du code, fuites, options.
3. [plan.md](./plan.md) — architecture, flux, phases.
4. [data-model.md](./data-model.md) — entités et invariants.
5. [contracts/](./contracts/) — six contrats.
6. [measurements.md](./measurements.md) — preuves live 2.2.5.
7. [checklists/requirements.md](./checklists/requirements.md) — revue de
   qualité des exigences.
8. [open-questions.md](./open-questions.md) — décisions à trancher.

## 2. Ce qu'il faut valider avant d'implémenter

- [ ] Le resolver de modèle s'appuie bien sur `FREE_MODELS` +
  `free_models_probed.json`, sans nouveau fetcher.
- [ ] Le target n'est jamais remplacé par un modèle payant.
- [ ] Les mappings explicites `FREE_MODEL_MAP` ne sont pas réécrits.
- [ ] Le mode GUI principal est `VPN` / `WARP` ; `socks5`/`direct` en avancé.
- [ ] Le provisioning est idempotent, avec plan et rollback.
- [ ] **P0** : l'isolation d'identité est bloquante avant plus d'une station.
- [ ] **P0.5** : seuls des profils vérifiés sur deux blocs distincts sont
  émis ; un `429` n'est pas un rejet ; aucun tuple mixte.
- [ ] Le prompt `<env>` est minimal et synthétique.
- [ ] Une conversation reste sur une station.
- [ ] L'audit identité est bloquant pour une station dupliquée/fuyante.
- [ ] Les profils partagés et la limite TLS sont visibles dans l'audit.

## 3. Matrice de validation conceptuelle

| Scénario | Résultat attendu |
|---|---|
| target `alive` | conservé, aucune écriture |
| target absent | remplaçant `alive`, persisté |
| target `dead` | remplaçant `alive` |
| aucun `alive` | `no_free_model`, pas de payant |
| apply `warp 0→3` | 3 créations, 3 vérifications |
| apply `warp 3→3` | aucune recréation |
| apply `warp 5→3` | 2 retraits |
| échec 3ᵉ station | rollback, anciennes conservées |
| apply concurrent | `409 busy` |
| 2 stations | project/session/trace disjoints |
| 5 tours même conversation | même station, même `ses_` |
| prompt scanné | aucun chemin réel/username/hostname |
| station dupliquée | audit `duplicate_identity`, exclue |
| `direct` désactivé | aucun `socks5:direct` |
| profil 200 sur 2 blocs | `verified`, assignable |
| profil 200 puis 429 | reste `candidate`, pas `rejected` |
| profil 403 sur 3 blocs | `rejected`, jamais assigné |
| UA 2.2.5 + JA3 1.3.14 | tuple mixte rejeté |
| 4 stations, 4 profils | 4 profils distincts |
| 6 stations, 2 profils | 6 servies, `shared_profile` visible |

## 4. Commandes futures (quand implémenté)

> Ne pas lancer maintenant : les routes n'existent pas.

```bash
# État désiré/réel
curl -s http://127.0.0.1:4000/api/free-egress | jq

# Plan dry-run WARP ×5
curl -s -X POST http://127.0.0.1:4000/api/free-egress/plan \
  -H 'Content-Type: application/json' \
  -d '{"mode":"warp","station_count":5}' | jq

# Appliquer
curl -s -X PUT http://127.0.0.1:4000/api/free-egress \
  -H 'Content-Type: application/json' \
  -d '{"mode":"warp","station_count":5}' | jq

# Modèle actif
curl -s http://127.0.0.1:4000/api/free-models | jq '.active_default, .default_source'

# Audit identité
curl -s http://127.0.0.1:4000/api/free-egress/identity-audit | jq

# Registre des profils vérifiés
curl -s http://127.0.0.1:4000/api/free-egress/profiles | jq

# Vérifier un candidat (dry-run + blocs frais)
curl -s -X POST http://127.0.0.1:4000/api/free-egress/profiles/verify \
  -H 'Content-Type: application/json' \
  -d '{"profile_id":"oc-2.2.5-cli-13"}' | jq

# Forcer la rotation des WARP managés épuisés
curl -s -X POST http://127.0.0.1:4000/api/vpn/socks5/rotate-egress | jq
```

## 5. Activation runtime

Le flag est désactivé par défaut :

```yaml
free:
  station_identity: false
```

Pour activer l'identité par station :

1. vérifier `free/profiles/verified_profiles.json` (`status: ok`) ;
2. vérifier que `logs/identity_secret` existe ou sera créé au premier appel ;
3. passer `free.station_identity: true` (ou `FREE_STATION_IDENTITY=1`) ;
4. redémarrer le proxy ;
5. tester une requête free, puis vérifier que deux stations ont des
   `project`/`ses_`/`msg_`/trace distincts.

Sans profil vérifié ou sans secret lisible, le chemin échoue **fermé** : il ne
retombe pas silencieusement sur l'identité par IP ni sur `direct`.

## 6. Sécurité / ordre des travaux
**Ne pas activer plusieurs stations avant P0.** Le round-robin actuel envoie
le même historique sous plusieurs identités ; c'est exactement ce que la
couche d'isolation doit empêcher.

Ordre recommandé :

```
P0 identité → P0.5 profils vérifiés → P1 modèle → P2 persistance
→ P3 plan egress → P4 VPN → P5 WARP → P6 GUI → P7 boot/runbook
```

## 7. Références code

- `config/discovery.py` — découverte/probe, default target.
- `config/settings.py` — `FREE_MODELS`, `FREE_MODEL_MAP`,
  `FREE_DISCOVERY_DEFAULT_TARGET`, `ensure_free_models_on_boot`.
- `opencode.py` — `_resolve_free_model`, `_apply_station_count`, fallback
  `jev-1.13-free`.
- `free/identities.py` — vault par IP, mint `ses_`/`msg_`, `VERSION_SPECS`.
- `free/identity_generator.py` — générateur de profils existant.
- `free/profile_registry.py`, `free/profile_verifier.py`,
  `free/profile_generator.py`, `free/profile_live.py`,
  `free/profile_transport.py`, `free/warp_rotation.py` — socle P0.5,
  vérification et rotation WARP.
- `free/station_identity.py`, `free/runtime_identity.py` — identité par
  station, prompt templaté, affinité, audit P0 et résolution runtime.
- `tests/test_profile_registry.py`, `tests/test_profile_live.py`,
  `tests/test_profile_transport.py`, `tests/test_station_identity.py`,
  `tests/test_runtime_identity.py`, `tests/test_warp_rotation.py`,
  `tests/test_socks5_affinity.py` — tests sans réseau.
- `free/pool.py` — pool `socks5`, labels, round-robin et affinité de
  conversation : `_socks5_pick` épingle la station par clé de conversation
  avec un **TTL glissant**, réutilise la station saine même après expiration,
  bascule seulement si elle devient inutilisable. Le round-robin n'est actif
  que si la GUI le demande (`socks5_auto_rotate`, `free_parallel.enabled`,
  `routing=round-robin`) ; `failover` reste sticky, `strict` utilise le
  compteur, `load-balance` la station la moins chargée et `hedge` course deux
  stations au premier tour avant d'épingler la gagnante.
- `scripts/mint_capture.py`, `scripts/test_free_egress.py` — vérification live.
- `dashboard/api.py` — modes, station_count, config.
- `dashboard-ui/src/pages/FreeModels.svelte`, `vpn/VpnStatus.svelte`,
  `vpn/VpnRouting.svelte`.
- `free/official_payload.json`, `free/captures/2.2.5.json` — prompt réel à
  templater.
