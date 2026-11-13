# Runbook — plan de contrôle 016

Objectif : amener les six stations de `legacy` à `active` sans jamais perdre
`client_usable`, puis revenir en arrière en une commande. Le plan de contrôle
choisit le tunnel, le transport, le DNS et l'egress ; l'orchestrateur garde le
registre (`registry.db`), les claims et les scores.

## Pré-requis

- Image Gluetun 016 déployée sur la station ciblée, `CONTROL_PLANE_ENABLED=true`.
- `GLUETUN_CONTROL_API_KEY` présent dans l'environnement du proxy (jamais dans
  `config.yaml`).
- `control_plane.mode=shadow` depuis au moins 24 h sur la station : zéro
  divergence IP, zéro job réel, zéro incident.
- Les routes `/v1/vpn/state`, `/v1/egress`, `/v1/events` répondent.

## 1. Vérifier le plan (aucun contact)

```bash
python scripts/rollout_control_plane.py --dry-run
python scripts/rollout_control_plane.py --dry-run --stations 6,5,4,3,2,1
```

Sortie attendue : ordre 6 → 1, un maximum de six stations, `dry run: no station
contacted, no config written`.

## 2. Activer une station (6 → 1)

1. Passer **cette seule station** en `control_plane.mode=active`
   (`config.yaml` ou l'environnement du proxy), sans toucher aux cinq autres.
2. Redémarrer le chemin froid legacy de la station si nécessaire.
3. Vérifier avant de continuer :

```bash
python scripts/rollout_control_plane.py --stations 6 --forbid-ip "$(python -c 'from free.ip_registry.adapters.egress import host_public_ip; print(host_public_ip())')"
```

`PASS` exige : `vpn_status=running`, `egress.verified=true`, IP valide et
différente de l'IP publique de l'hôte. Un `FAIL` arrête le rollout : les
stations restantes ne sont pas modifiées.

4. Répéter pour 5, 4, 3, 2, 1, **une station à la fois**, avec la même
   vérification avant de passer à la suivante.

## 3. Vérifications pendant le rollout

- `client_usable` reste vrai pour chaque station active.
- Aucune septième station/session n'apparaît : le rollout ne traite que les
  stations listées (maximum six).
- `GET /v1/vpn/state` répond en p95 < 100 ms.
- Les événements SSE arrivent < 1 s après un changement.
- Le chemin chaud n'appelle ni `docker exec` ni `docker cp`.

## 4. Rollback

1. Repasser la station (ou toutes) en `control_plane.mode=legacy`.
2. Repiner le digest précédent de l'image Gluetun.
3. `CONTROL_STATE_MODE=env` si l'état doit être relu depuis l'environnement.
4. Vérifier `client_usable` et le SLO ; le registre n'a jamais été modifié par
   le plan de contrôle (les claims restent dans `registry.db`).

## 5. Incidents

| Symptôme | Réaction |
|---|---|
| 5xx/timeout après retries | `LegacyFallback` : le binder froid legacy reprend, incident warning. |
| 409 `active_job` | Suivre `GET /v1/vpn/jobs/{id}` ; ne pas créer de second job. |
| 422 `unknown_target` | Rejet déterministe, candidat suivant, **pas** de legacy. |
| 403 | Erreur de configuration : arrêt du rollout et alerte. |
| `exhausted` | Repair request ; la station reste conservée, aucun « aucune IP disponible ». |
| `failed_degraded` | Incident critique ; réparation par 015, pas de boucle locale. |
| `capabilities` absentes | Le fork n'est pas 016 : forcer `mode=legacy` et alerter. |

Aucun fallback ne propose le payant, un second compte ou une attente.

## Limites connues

- T049 (DoH/TCP 853 réellement bloqués) demande un réseau filtré absent du banc
  local.
- T095/T099 : le ladder de transports est piloté par l'exécuteur de jobs ; son
  branchement dans `internal/vpn/loop.go` et les tests `TR-AT-*` complets
  restent ouverts.
- T067 : les métriques de jobs sont collectées ; leur export HTTP doit être
  branché sur le serveur Prometheus global.
