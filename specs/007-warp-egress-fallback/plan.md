# Implementation Plan: WARP primaire + replis d'egress free

**Branch**: `007-warp-egress-fallback` | **Date**: 2026-11-07 | **Spec**: [spec.md](./spec.md)

**Input**: Feature specification from `specs/007-warp-egress-fallback/spec.md`
et plan source `PLAN_ALTERNATIVE_WARP_2026-11-06.md`.

**Note**: Ce plan est généré par le workflow Spec Kit ; il décrit l'architecture
et les phases. Les tâches exécutables sont dans `tasks.md`.

## Summary

Garder **WARP comme source primaire** (4–5 blocs `/64` utiles, data
illimitée), corriger la politique de quota (park vs rotate, reset fournisseur),
puis ajouter des **replis gratuits ordonnés** : client WARP alternatif
(MicroWARP/usque), IPv6 box/ISP, Oracle Always Free, Proton Free. Un gateway
IPv6 VPS `/48` reste une option payante, désactivée par défaut.

Le plan formalise aussi la **Phase 0** : mesurer si le quota est par adresse,
par `/64`, par ASN/compte ou par modèle, avant d'investir dans une source de
capacité. Un `403` ne doit jamais être traité comme un `429`.

**Décision opérateur 2026-11-07 — free only** : cette itération n'implémente
que le gratuit et le déjà disponible. Le VPS payant, les VPN payants, la 5G
payante, OpenCode Go, l'empilement de comptes et le remplacement de box sont
**hors périmètre**. Ils restent documentés comme options futures, désactivés
par défaut. Le MVP gratuit est : WARP fiabilisé + park/reset + taxonomie
d'erreurs + MicroWARP/usque + IPv6 box actuelle + Oracle/Proton Free si un
compte gratuit existe déjà.

## Technical Context

**Language/Version**: Python 3.12+, PowerShell 5.1/7, TypeScript/Svelte 5
(dashboard)

**Primary Dependencies**: FastAPI/uvicorn, httpx, curl_cffi, SQLite WAL
(`registry.db`), Docker, pytest. Aucune nouvelle dépendance lourde sans
justification ; le gateway VPS reste en stdlib/asyncio si possible.

**Storage**: `registry.db` est l'autorité (endpoints, blocs, quotas, leases,
observations, provider health). `config.yaml` porte les seuils et l'ordre des
fallbacks. Les preuves Phase 0 vont dans `docs/evidence/`.

**Testing**: `pytest -k "not docker"` pour l'unité/intégration offline ;
tests live contrôlés pour WARP, fallback et gateway. Aucun test ne doit
consommer le quota de production sans marquage explicite.

**Target Platform**: Windows pour le proxy et les conteneurs WARP ; Linux VPS
optionnel pour le gateway IPv6. Le dashboard tourne sur la même machine que le
proxy.

**Project Type**: Backend Python + dashboard Svelte + scripts/PowerShell +
gateway Linux optionnel.

**Performance Goals**: disponibilité client > 99 % sur 24 h ; `429` < 5 % ;
rotation VPS < 10 s ; rotation WARP inchangée (restart + health + probe) ;
bascule fallback sans interrompre les requêtes en vol.

**Constraints**: free-only par défaut ; aucun fallback payant ou direct
silencieux ; `reset_at` fournisseur est une barrière dure ; aucun bloc partagé
entre stations ; aucun proxy public/Tor pour des credentials ; seuils dans
`config.yaml` ; une option GUI = un consommateur runtime.

**Scale/Scope**: 6 stations WARP, 4–5 blocs utiles, 1–2 stations fallback,
3 000–10 000 requêtes free/jour. Le gateway VPS cible 6–12 stations et un
`/48` (65 536 `/64`).

## Constitution Check

*GATE: Must pass before Phase 0 research. Re-check after Phase 1 design.*

| Principe | Verdict | Justification |
|---|---|---|
| I. Disponibilité mesurée | PASS | Les probes et le ledger fournissent les preuves ; le fallback expose `degraded` avec raison. |
| II. Trois ressources, trois cycles | PASS | Station, session et IP restent séparées ; le cooldown appartient au bloc/IP. |
| III. Une source de vérité | PASS | `registry.db` reste l'autorité ; `config.yaml` ne porte que les seuils. |
| IV. Reset fournisseur = barrière dure | PASS | Le plan passe en **park par défaut** jusqu'à `reset_at` ; le probe anticipé devient une option explicite, désactivée par défaut pour les nouveaux providers. |
| V. Une IP n'appartient qu'à une station | PASS | Dédup mesurée par provider et par bloc ; claim exclusif maintenu. |
| VI. Indépendance des stations | PASS | Aucun quota pays ; la diversité reste optionnelle. |
| VII. Aucune preuve, aucun succès | PASS | Un `200` de probe ne compte que si le contenu est valide ; `403` ne compte pas. |
| VIII. Autonomie, un seul réparateur | PASS | Le fallback est une politique du pool, pas un second réparateur ; le superviseur reste propriétaire. |
| IX. Score ordonne, portes dures décident | PASS | Le score ne contourne ni `reset_at`, ni l'unicité, ni la preuve. |
| X. Dégradation explicite, free only | PASS | Replis gratuits d'abord ; VPS payant opt-in, désactivé par défaut ; aucun direct silencieux. |
| XI. Fallbacks multiples | PASS | Le plan ajoute une chaîne ordonnée et configurable, avec retour à WARP. |
| XII. Toute option GUI a un effet | PASS | Chaque option aura la matrice `option → clé → consommateur → effet → test`. |
| XIII. Données traçables/migrables | PASS | Nouvelles tables/colonnes avec migration versionnée et backup ; aucune destruction. |
| XIV. Architecture modulaire | PASS | Modules `egress_provider`, `errors`, `fallback`, `ipv6_provider` < 1 500 lignes ; strangler, pas de nouveau monolithe. |

Aucune violation à justifier. Le point sensible est **IV** : le plan remplace
le probe anticipé par défaut par un park strict ; l'ancien `max_block_s` reste
disponible comme exception opérateur documentée.

## Project Structure

### Documentation (this feature)

```text
specs/007-warp-egress-fallback/
├── spec.md
├── plan.md
├── research.md
├── data-model.md
├── quickstart.md
├── contracts/
│   ├── egress-provider.md
│   ├── fallback-policy.md
│   ├── error-taxonomy.md
│   └── gateway-api.md
└── tasks.md
```

### Source Code (repository root)

```text
free/
├── pool.py                    # dispatch rotation, sélection, dédup, sticky
├── warp_rotation.py           # WarpDockerProvider (existant, à abstraire)
├── quota_ledger.py            # blocs, reset_at, park, observations
├── egress_provider.py         # NOUVEAU : protocole + EgressRotationResult
├── errors.py                  # NOUVEAU : GATE_403/QUOTA_429/RATE_LIMIT_429/...
├── fallback.py                # NOUVEAU : politique, triggers, retour WARP
├── gluetun_provider.py        # NOUVEAU : Proton/Windscribe/Privado
└── ipv6_provider.py           # NOUVEAU : client du gateway VPS

scripts/
└── ipv6_egress_gateway.py     # NOUVEAU : gateway SOCKS5 + allocateur /64

dashboard/
└── api.py                     # statut provider/bloc/gateway, test/rotation

dashboard-ui/src/pages/vpn/
├── VpnStatus.svelte           # provider, bloc, dernier 429, fallback
└── VpnAdvanced.svelte         # options fallback, gateway, probes

config.yaml                    # egress_providers, fallback, seuils
opencode.py                    # probes, dispatch, statut free

tests/
├── test_egress_provider.py
├── test_error_taxonomy.py
├── test_fallback_policy.py
├── test_ipv6_gateway.py
├── test_warp_rotation.py      # étendu
└── test_quota_ledger.py       # étendu
```

**Structure Decision**: conserver le layout existant (backend Python à la
racine, dashboard séparé, scripts). Les nouveaux modules sont des ports/
adapters autour du pool et du rotateur WARP, pas un second moteur.

## Phase 0 — Validation (go/no-go)

But : déterminer la granularité du quota **avant** d'ajouter une source de
capacité.

1. Vérifier le baseline : gates 403 (UA, session, `read`, `stream`, `<env>`).
2. Tester adresse `A` épuisée vs adresse `B` du même `/64`.
3. Tester un `/64` distinct.
4. Rejouer avec un contrôle IPv4 `/32`.
5. Rejouer par modèle (`deepseek-v4.1-flash-free`, `mimo-v2.6-flash-free`).
6. Vérifier la box/ISP (`/56` ou `/48`), Oracle et Proton.
7. Produire `docs/QUOTA_GRANULARITY_2026-11-07.md` avec preuves brutes,
   `Retry-After`, heure UTC et go/no-go.

**Critère de sortie** : granularité connue pour au moins deux modèles, `403`
écarté, au moins un repli gratuit identifié ou un VPS chiffré.

## Phase 1 — Abstraction provider et taxonomie

1. Ajouter `provider`, `block_key`, `quota_axis`, `reset_at` à
   `Socks5Endpoint` et au schéma de config.
2. Extraire `EgressProvider` (protocole) et `EgressRotationResult`.
3. `WarpDockerProvider` = wrapper de `WarpRotator` (comportement actuel).
4. Dispatch `_request_egress_rotation` + `free/errors.py`.
5. Dédup d'egress mesuré après rotation (ne pas croire le rapport du
   rotateur) ; rejeter une famille IPv4 sur un endpoint `ipv6`.
6. Sticky session/egress : conserver le même bloc tant qu'un probe 200 passe.
7. Tests de non-régression WARP, 403/429, doublons.

**Critère** : aucune régression, config existante lisible sans migration, un
403 ne pénalise aucun bloc, deux adresses du même `/64` = un seul bloc.

## Phase 1bis — Fallback gratuit

1. Benchmark `MicroWARP` et `usque` (continuité, même pool).
2. Vérifier box/ISP et Oracle ; noter préfixe et nombre de `/64`.
3. Proton Free via gluetun : une station, data illimitée, test 24 h.
4. Windscribe/Privado : urgence 10 Go, pas de production.
5. Implémenter `free/fallback.py` : déclencheurs, ordre, max 2 stations,
   cooldown, retour WARP après 2 succès.
6. Park reset : ne pas recréer si `reset_at` est proche et qu'un autre bloc
   est disponible ; probe après reset.
7. Aucun fallback direct silencieux : pool vide → `degraded` explicite.

**Critère** : WARP reste primaire, une station bascule sur un repli gratuit,
revient après 2 succès, aucun bloc fallback réutilisé par WARP.

## Phase 2 — Gateway IPv6 VPS (différé, payant, hors périmètre)

1. Gateway SOCKS5 asyncio : handshake, CONNECT, bind source, relay.
2. Allocateur `/64` + SQLite : label → bloc → port → source.
3. API `/health`, `/allocate`, `/rotate`, `/release` avec token.
4. Drain + rebind même port ; rotation < 10 s.
5. Setup VPS : `ip_nonlocal_bind`, route locale, systemd, nftables.
6. Tunnel SSH/WireGuard ; gateway en `127.0.0.1` uniquement.
7. Tests unitaires et intégration : 3 rotations, 3 blocs, 3 × 200.

**Critère** : 1 station VPS + WARP primaire, 24 h de soak, 0 doublon,
rotation < 10 s, retour WARP propre.

## Phase 3 — Intégration fallback VPS (différé, payant, hors périmètre)

1. `Ipv6GatewayProvider.rotate` : appelle `/rotate`, retourne `new_ip`/`block`.
2. `refresh_egress` vérifie la famille et le `/64`.
3. Déclencheurs 429/quota/duplicate/trial_dead identiques à WARP.
4. `rotate_exhausted_managed` inclut le provider VPS.
5. GUI : provider, bloc, état gateway, boutons test/rotation.
6. Tests de doublons et de persistance.

**Critère** : 1 station VPS + WARP primaire, 24 h de soak, 0 doublon,
rotation < 10 s.

## Phase 4 — Scale et durcissement

- 1–2 stations VPS de secours + 2 blocs chauds.
- Allocateur : jamais de bloc épuisé, pas de collision.
- Métriques : blocs distincts, blocs épuisés, 429 par modèle/axe, 403 gate,
  rotations, doublons, temps de reset.
- Failover WARP/fallback + circuit breaker, sans direct silencieux.
- Sécurité : token, firewall, SSH, fail2ban, updates.
- Runbook, rollback, audit trimestriel des gates.

**Critère** : 7 jours de soak, 99 % de disponibilité, < 5 % de 429, aucun
proxy ouvert, WARP toujours majoritaire.

## Phase 5 — Providers optionnels

- `GluetunProvider` pour Proton Free, Windscribe, Privado, Mullvad.
- Proton d'abord (data illimitée, un appareil).
- Windscribe/Privado en urgence seulement.
- Mullvad seulement si les IP gratuites sont bloquées et que le coût est
  accepté.

## Risks and Mitigations

| Risque | Probabilité | Impact | Mitigation |
|---|---|---|---|
| Quota non par `/64` | moyenne | élevé | Phase 0 avant achat ; si ASN, pivot résidentiel/mobile |
| Replis gratuits insuffisants | moyenne | moyen | WARP primaire + park/reset ; VPS seulement en dernier recours |
| Proton 1 appareil | certaine | moyen | une seule station fallback |
| Box SFR ne délègue que `/64` | certaine | moyen | 1–2 buckets ; Oracle/Proton en priorité |
| 5G = 1 `/64` | certaine | moyen | fallback 1 station, pas un pool |
| Rotation WARP ne lève pas le 429 | élevée | élevé | probe 200 avant de compter le bloc, dedup, park |
| 403 pris pour un 429 | moyenne | élevé | taxonomie + tests dédiés |
| Gateway mal sécurisé | faible | critique | localhost + token + SSH/WireGuard + firewall |
| Bloc réutilisé par erreur | moyenne | moyen | allocateur + ledger + test doublons |
| CGU OpenCode/Cloudflare | élevée | élevé | volume raisonnable, pas de revente, revue CGU |

## Re-check Constitution (post-design)

Tous les principes restent **PASS**. Les points à surveiller :

- **IV** : le park strict devient le défaut ; `ledger_max_block_s` reste une
  exception explicite, documentée et désactivée par défaut pour les nouveaux
  providers.
- **X** : le VPS est opt-in ; aucun fallback payant automatique.
- **XII** : chaque nouvel affichage provider/bloc doit avoir un test runtime.
- **XIV** : les nouveaux modules restent sous 1 500 lignes et sont testables
  avec des adapters factices.

## Next Step

Exécuter `/speckit.tasks` (ou lire `tasks.md`) puis démarrer par la **Phase 0**
et la **Phase 1** gratuitement. La Phase 1bis (MicroWARP/usque, IPv6 box
actuelle, Oracle/Proton Free si compte existant) suit. Les phases VPS 2/3 et
les providers payants restent **hors périmètre** jusqu'à décision explicite
ultérieure.
