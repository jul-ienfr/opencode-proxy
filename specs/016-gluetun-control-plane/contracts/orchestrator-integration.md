# Contrat — Intégration orchestrateur Python ↔ plan de contrôle Gluetun

**Feature** : `016-gluetun-control-plane`
**Côté projet** : `opencode-proxy-one` (Python 3.12)
**Version** : `1.0.0`

---

## 1. Objectif

Faire passer le chemin chaud de bascule de `docker exec` + `PUT
/v1/vpn/settings` + parsing texte à un client HTTP typé, idempotent et
observable, **sans** déplacer l'autorité IP/quota/SLO hors de `registry.db`.
Le chemin legacy reste disponible comme secours froid pendant toute la migration.

## 2. Frontière

| Décision | Propriétaire |
|---|---|
| Quel candidat, dans quel ordre, pour quelle station | Orchestrateur (`free/rotation.py`, superviseur). |
| Claim d'IP, lease, quota, `reset_at`, score composite | `registry.db`. |
| Verdict `client_usable`, SLO, warm pool, standby inter-stations | Feature 015. |
| Tunnel, settings, DNS ladder, preuve d'egress | Fork 016. |

Le client Python **n'envoie jamais** de score ni de quota au fork ; il envoie une
liste ordonnée, des exclusions et un budget.

## 3. Configuration

```yaml
control_plane:
  mode: legacy            # legacy | shadow | active
  base_url: "http://vpn-gluetun-{station}:8000"
  api_key_env: "GLUETUN_CONTROL_API_KEY"
  timeout_s: 10
  switch_timeout_s: 60
  rotate_timeout_s: 180
  verify_timeout_s: 10
  idempotency_window_h: 24
  retry:
    max_attempts: 3
    backoff_s: 0.5
    max_backoff_s: 4
  shadow:
    sample_rate: 1.0
    compare_window_s: 300
  events:
    ingest: true
    batch_size: 100
    poll_interval_s: 5
```

- `mode=legacy` : aucun appel au nouveau client ; comportement actuel.
- `mode=shadow` : appels en observation (dry_run quand possible), décision
  legacy conservée, comparaison enregistrée.
- `mode=active` : le nouveau client décide ; legacy utilisé seulement sur
  `5xx`/timeout après épuisement des retries.
- `base_url` peut être résolu par station (`{station}` → `vpn-gluetun-3`).
- La clé API n'est jamais loggée ; elle est lue depuis l'environnement.

## 4. Client Python

```python
class GluetunControlClient:
    def capabilities(self) -> Capabilities: ...
    def state(self) -> TunnelState: ...
    def switch(self, target: TunnelTarget, constraints: Constraints,
               *, idempotency_key: str, wait: bool = True) -> SwitchJob: ...
    def rotate(self, candidates: list[ServerCandidate], constraints: Constraints,
               *, idempotency_key: str, wait: bool = False) -> SwitchJob: ...
    def rollback(self, *, wait: bool = True) -> SwitchJob: ...
    def job(self, job_id: str) -> SwitchJob: ...
    def cancel(self, job_id: str) -> SwitchJob: ...
    def egress(self, *, fresh: bool = False) -> EgressVerification: ...
    def dns_state(self) -> DNSState: ...
    def transport_state(self) -> TransportState: ...
    def transport_reset(self, transport: str | None = None) -> TransportState: ...
    def events(self, since: int | None = None, limit: int = 100) -> list[ControlEvent]: ...
```

Règles :

- Aucune méthode n'appelle `GET /v1/vpn/settings` (secrets).
- `switch` et `rotate` envoient toujours un `Idempotency-Key` :
  `f"{action}-{station}-{cycle_id}"` ; le `cycle_id` vient du registre, pas d'un
  timestamp local.
- `wait=True` utilise un timeout HTTP > `switch_timeout_s` + marge ; un timeout
  côté client ne signifie pas échec : le job peut continuer, donc le client
  relit `GET /v1/vpn/jobs/{id}` avant de conclure.
- Retries uniquement sur `429`, `502`, `503`, `504` et erreurs de connexion ;
  jamais sur `4xx` métier (422, 409, 403).
- Aucun `docker exec`, `docker cp`, `docker restart` dans le chemin chaud.

### Erreurs typées

```python
class ControlPlaneError(Exception): ...
class UnknownTarget(ControlPlaneError): ...
class ActiveJob(ControlPlaneError): active_job_id: str
class VerificationFailed(ControlPlaneError): verification: EgressVerification
class Exhausted(ControlPlaneError): attempts: list[Attempt]
class LegacyFallback(ControlPlaneError): reason: str
```

## 5. Séquence de bascule

```text
rotation.py / superviseur
  │ 1. choisit une liste ordonnée de candidats (score, quota, claims)
  │ 2. POST /v1/vpn/rotate (idempotency_key = cycle_id)
  ▼
fork
  │ 3. exécute, prouve, commit/rollback/exhausted
  │ 4. émet job.* et egress.verified (SSE)
  ▼
client Python
  │ 5. poll GET /v1/vpn/jobs/{id} (ou SSE)
  │ 6. si committed : met à jour registry.db (observation + lease)
  │ 7. si rolled_back : libère le candidat, marque l'échec, planifie le suivant
  │ 8. si exhausted : enqueue repair, ne désactive jamais la station
  ▼
registry.db (autorité)
```

**Point clé** : entre 3 et 6, le registre ne considère pas l'IP comme claimée
avant la preuve. Le client écrit l'observation seulement après `committed` et
re-vérifie l'IP avec `GET /v1/egress` si le job a plus de 10 s.

## 6. Mapping avec le code existant

| Existant | Remplacé par | Mode |
|---|---|---|
| `_control_exec("wget ... /v1/vpn/status")` | `client.state()` | active |
| `PUT /v1/vpn/settings` via `docker cp`/`exec` | `client.switch()` / `client.rotate()` | active |
| Parsing du corps `running` | `SwitchJob.state` | active |
| Blacklist hostnames côté Python | `exclude_hostnames` dans la requête | active |
| Recréation Compose en dernier recours | `client.rollback()` puis legacy froid | actif → legacy |
| `free/ip_registry/adapters/egress.py` (5 couches) | `client.egress()` + couches existantes en secours | active |
| `free/transport_ladder.py` (WG/UDP/TCP Python) | `client.transport_state()` en shadow, puis ladder interne du fork | shadow → active |
| `docker restart` après échec | `job.rolled_back` / `failed_degraded` | active |

Le module legacy `free/ip_registry/adapters/gluetun_control.py` reste importable
et utilisé si `mode=legacy` ou si `LegacyFallback` est levée. Il est marqué
« chemin froid » et ne doit plus être appelé dans la boucle de rotation normale.

## 7. Shadow

En mode `shadow` :

1. Le client appelle `POST /v1/vpn/switch` avec `dry_run=true` (aucune coupure)
   pour chaque décision legacy.
2. Il enregistre : cible, candidats, plan, `would_cut_tunnel`, warnings.
3. La décision legacy s'exécute normalement.
4. Après coupure, il appelle `GET /v1/egress` et `GET /v1/vpn/state` pour
   comparer l'IP et le serveur effectifs.
5. Il écrit une ligne `control_plane_shadow` dans `telemetry.db` :
   `{ts, station, legacy_target, shadow_target, legacy_ip, shadow_ip, match,
   divergence_reason}`.
6. Divergence tolérée : 0 sur l'IP finale et les gates ; les warnings de plan
   sont informatifs.

Le shadow ne doit jamais provoquer de coupure, de double job ou de claim.

## 7bis. Migration du ladder de transports Python (015)

Le ladder Python (`free/transport_ladder.py`) et la cascade de `vpn/manager.py`
sont conservés **en shadow** pendant la migration, puis retirés du chemin chaud.

Séquence :

1. **Observation** : en `mode=shadow`, le Python lit
   `GET /v1/vpn/transport` après chaque décision et enregistre
   `{ts, station, legacy_transport, fork_transport, match, legacy_reason,
   fork_reason}` dans `telemetry.db`.
2. **Non-pilotage** : le Python n'appelle jamais `POST /v1/vpn/switch` avec un
   `transport_policy` en shadow et ne touche pas `PUT /v1/vpn/settings` pour
   changer de transport.
3. **Comparaison** : divergence attendue 0 sur le transport final et le résultat
   d'egress ; une divergence durable est un incident.
4. **Activation** : `mode=active` ; le fork possède le ladder, le Python ne fixed
   plus `VPN_TYPE`/`OPENVPN_PROTOCOL` dans le chemin chaud.
5. **Décommissionnement** : après ≥ 7 jours sans régression, la cascade Python
   n'est plus appelée que si `capabilities.transport_ladder == false` (fork
   ancien) ou sur `LegacyFallback` explicite.
6. **Garde-fou** : deux autorités ne doivent jamais piloter le transport en même
   temps ; un verrou de configuration (`control_plane.transport_owner=fork`)
   l'impose.

## 8. Ingestion des événements

- `GET /v1/events?since=<seq>` toutes les 5 s, ou SSE en continu.
- Les événements sont idempotents par `(station_id, seq)`.
- Mapping :

| Événement fork | Table `registry.db` |
|---|---|
| `job.committed` | `observations` (type `switch_committed`, job_id, ip, hostname) |
| `job.rolled_back` | `observations` + `repair_requests` (si échec répété) |
| `job.failed_degraded` | `incidents` (severity critical) |
| `job.exhausted` | `repair_requests` (priority), jamais `stations.disabled` |
| `egress.verified` | `observations` (ip, quorum, sources, latency) |
| `dns.tier_down` / `ladder_exhausted` | `incidents` (severity warning/critical) |
| `transport.connected` / `transport.failed` | `observations` (type `transport_attempt`, transport, reason, latency) |
| `transport.cycle_exhausted` / `tunnel.repair_needed` | `repair_requests` (priority), jamais `stations.disabled` |
| `state.drift` / `state.invalid` | `incidents` + log opérateur |

- Le curseur `seq` est persisté dans `registry.db` (`control_cursor`).
- Aucun événement ne crée un claim ou un lease : le registre reste seul à
  décider.

## 9. Fallback et dégradation

| Situation | Comportement |
|---|---|
| Fork 5xx/timeout après retries | `LegacyFallback` ; chemin froid legacy ; incident warning. |
| Fork 409 active_job | Attendre le job actif (`GET jobs/{id}`) ; ne pas créer de second job. |
| Fork 422 unknown_target | Rejet déterministe ; candidat suivant ; pas de legacy. |
| Fork 403 | Erreur de configuration ; arrêt de la rotation, alerte. |
| Fork `exhausted` | Enqueue repair, station conservée, candidats suivants au prochain cycle. |
| Fork `failed_degraded` | Incident critique, station réparable par 015, aucune boucle locale. |
| `capabilities` absentes | Le fork n'est pas 016 ; mode legacy forcé, alerte. |

Aucun fallback ne propose le payant, un second compte ou l'attente.

## 10. Tests

| Test | Contenu |
|---|---|
| `test_gluetun_control_v2.py` | Client, sérialisation, erreurs typées, retries, idempotence. |
| `test_control_plane_shadow.py` | 100 décisions, zéro divergence IP/gates, aucun job réel en shadow. |
| `test_control_plane_events.py` | Ingestion idempotente, curseur, mapping incidents. |
| `test_control_plane_fallback.py` | 5xx → legacy, 409 → attente, 422 → candidat suivant. |
| `test_rotation_contract.py` | Liste ordonnée non re-scorée, `exhausted` local, 6 stations. |
| `test_transport_ladder_shadow.py` | Parité transport legacy/fork, aucun pilotage Python, divergence tracée. |
| `test_no_docker_exec_hot_path.py` | Le chemin actif n'appelle ni `docker exec` ni `docker cp`. |

## 11. Rollout

1. Déployer l'image 016 sur la station 6, `mode=shadow`, 24 h.
2. Vérifier : 0 divergence IP, 0 job réel, 0 incident.
3. Passer `mode=active` sur la station 6 ; vérifier `client_usable` et le SLO.
4. Répéter 5 → 1, une station à la fois.
5. Après 7 jours sans régression : le chemin legacy n'est plus utilisé que sur
   `LegacyFallback` ; le décommissionnement complet est une décision séparée.
6. Rollback : `mode=legacy`, repin du digest précédent, `CONTROL_STATE_MODE=env`.

## 12. Critères d'acceptation

| ID | Critère |
|---|---|
| ORCH-AT-01 | ≥ 95 % des bascules passent par l'API en mode `active`. |
| ORCH-AT-02 | Aucun `docker exec`/`docker cp` dans le chemin chaud. |
| ORCH-AT-03 | Un timeout client ne crée pas de second job (idempotence). |
| ORCH-AT-04 | Un `exhausted` n'écrit jamais « aucune IP disponible » et ne désactive pas la station. |
| ORCH-AT-05 | Les six stations restent `client_usable` pendant le rollout 1→6. |
| ORCH-AT-06 | Le shadow ne coupe jamais le tunnel et ne crée aucun claim. |
| ORCH-AT-07 | Les événements sont ingérés sans doublon (`station_id`,`seq`). |
| ORCH-AT-08 | Le fallback legacy fonctionne sur 5xx/timeout et est tracé. |
| ORCH-AT-09 | Le ladder Python n'est plus qu'un observateur en shadow, puis n'est plus appelé en `active` (sauf `LegacyFallback`). |
