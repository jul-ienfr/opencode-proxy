# Feature Specification: Audit et optimisation de la flotte free — autorité unique, quota réel, 6/6 prouvé

**Feature Branch**: `019-audit-optimisation-flotte-free`
**Created**: 2026-11-14
**Status**: Draft
**Input**: Audit SRE du 2026-11-14 (`specs/019-audit-optimisation-flotte-free/audit.md`)

## Objectif

Faire passer la flotte de **0/6 `client_usable` mesuré à 11:12:12Z** à
**6/6 sur un même cycle de probe de 120 s, tenu 30 minutes**, en free, sans
payant, sans second compte, sans « attendre » comme réponse unique, avec six
stations au maximum.

Le résultat attendu n'est pas « une station a vu un 200 ». C'est : six stations
`client_usable=true` dans la même fenêtre, un `warm_pool` vivant ≥ 6, zéro
`code reload` sur 15 min, une app qui ne peut pas mourir sans relance, et un
`rotate` qui commit plus d'une fois sur deux.

Cinq causes racines ont été identifiées (audit §4) :

1. **P0-01** — le quota par IP est modélisé comme un compteur local de 200,
   alors que le bucket amont est partagé : des IP à `used=1/200` sont dites
   épuisées.
2. **P0-02** — trois composants publient trois vérités différentes sur
   `client_usable` (API 0/6 vs `station_state` station 3 `USABLE`).
3. **P0-03** — l'app `:4000` n'a aucun superviseur de processus.
4. **P0-04** — la chaîne d'approvisionnement rend 5,6 % de 200 et 78 % de
   `no_egress`, alors que 15 IP vivent dans le warm pool sans être consommées.
5. **P0-05** — le chemin de requête déclenche et journalise une rotation
   (`free/pool/__init__.py:5134`).

## Périmètre

1. **Propriété de l'état** : une seule autorité par objet (IP, pin, lease,
   décision de rotation, `client_usable`, warm pool).
2. **Modèle de quota par IP** : séparer notre consommation du verdict amont,
   plafonner le blocage, respecter `reset_at`.
3. **Approvisionnement** : consommer le warm pool avant de qualifier,
   préflight avant bind, rendement mesuré.
4. **Fiabilité de l'app** : lanceur supervisé, capture stderr, heartbeat.
5. **Performance de rotation** : aligner les timeouts, rendre la file visible.
6. **Données** : purge et compaction de `registry.db` et `requests.db`.
7. **Observabilité** : `/metrics`, SLO « 6/6 même cycle », alertes.
8. **Qualité** : tests des fonctions superviseur, CI bloquante.
9. **Sécurité** : token dashboard, auth du proxy, écoute loopback par défaut.
10. **Exploitation** : six stations garanties, images homogènes, WARP borné.

**Hors périmètre** : jambe payante, second compte, attente comme stratégie,
septième station ou session, réécriture big-bang, modification de
`app/protocol/tool_aliases.py` ou des réglages WARP de `config.yaml`.

## Contraintes dures

- Free uniquement ; le quota est **par IP**, jamais « compte-wide ».
- Six stations, jamais sept ; `station_count=6`.
- Une station saine n'est jamais recréée pour une mise à jour.
- Le superviseur n'est jamais gelé ; il répare seul.
- Aucune rotation avant le vrai `reset_at` / fin de lease.
- Aucune IP venue d'un 429 n'est re-testée avant son reset.
- Toute affirmation est adossée à une preuve (fichier:ligne, commande, SQL).
- Aucune modification de code pendant l'audit ; les correctifs sont des tâches.

## Scénarios utilisateurs

### US1 — Prouver 6/6 sur un même cycle (P1)

Un opérateur ouvre le dashboard et voit six stations `client_usable=true`
simultanément, avec `last_200_at` dans la même fenêtre de 120 s, pendant
30 minutes.

**Test indépendant** : exécuter la requête SQL « 6/6 même cycle » toutes les
2 min pendant 30 min ; elle doit renvoyer 6 à chaque fois.

**Critères d'acceptation mesurables** :

1. `COUNT(client_usable=1 AND last_200_at > now-120) = 6` pendant 30 min.
2. `warm_pool_live ≥ 6` sur la même fenêtre.
3. Zéro intervention humaine (aucun `docker restart`, aucun rotate manuel).
4. Aucune IP re-testée avant `reset_at` (requête de contrôle = 0).

### US2 — Une seule vérité d'état (P1)

L'API `/api/vpn-status`, `registry.db/station_state` et le fork décrivent le
même état pour les six stations, à chaque cycle.

**Test indépendant** : comparer les trois surfaces toutes les 30 s pendant
24 h ; zéro divergence sur `(station, client_usable, current_ip)`.

**Critères d'acceptation** :

1. Divergence = 0 sur 24 h.
2. Un test d'intégration échoue si les surfaces divergent.
3. `client_usable` n'est calculé qu'à un seul endroit.

### US3 — L'app ne meurt plus sans relance (P1)

Si le processus `:4000` meurt, il revient en moins de 30 s, avec la trace de
la cause.

**Test indépendant** : `taskkill /F` sur le PID, mesurer le retour de
`/health` et vérifier la présence du traceback dans le fichier de capture.

**Critères d'acceptation** :

1. `/health` répond 200 en < 30 s après un kill.
2. `stdout` et `stderr` sont capturés et horodatés.
3. Une alerte est émise si l'app est absente > 30 s.

### US4 — Le warm pool est consommé (P1)

Une IP déjà prouvée est affectée à une station avant toute nouvelle
qualification.

**Test indépendant** : provoquer un `DRAINING` sur une station ; elle doit
recevoir une IP du warm pool dans le cycle suivant, sans nouvelle
qualification.

**Critères d'acceptation** :

1. `warm_pool_used / warm_pool_consumed ≥ 0,8` sur 2 h.
2. Taux de 200 > 30 % sur 1 h.
3. `bind_error` < 20 % des tentatives.

### US5 — Le bruit ne masque plus les incidents (P2)

Au repos, le log ne contient plus de rotation par requête, et les incidents
sont regroupés par cause.

**Critères d'acceptation** :

1. ≤ 1 ligne `warp-rotate` par minute au repos.
2. ≤ 5 incidents/heure hors panne réelle.
3. Un incident flotte pour N stations touchées dans la même minute.

### US6 — Les données restent bornées (P2)

`registry.db` < 50 Mio, `requests.db` < 500 Mio, zéro ligne au-delà de la
rétention.

**Critères d'acceptation** :

1. Purge planifiée et testée.
2. `warm_pool` ne contient que des lignes vivantes.
3. `PRAGMA integrity_check = ok` après purge.

### Cas limites

- Toutes les IP sont `cooling` : la flotte doit rester ≥ 1 station et le
  superviseur ne doit pas rotationner avant les resets.
- AUTH_FAILED global : pause unique, une tentative par 10 min, aucune chaîne
  parallèle.
- App absente : le superviseur continue de réparer, l'API est indisponible
  mais le SLO est calculé depuis le registre.
- Disque plein : les écritures SQLite échouent proprement, l'app reste en vie.

## Entités clés

| Entité | Autorité | Champs |
|---|---|---|
| `station_state` | app (probe) → registre | station, state, current_ip, client_usable, last_200_at, reason |
| `quota_buckets` | registre | ip, model, day, used, last_status, reset_at |
| `warm_pool` | superviseur | ip, hostname, transport, model, verified_at, expires_at, score_ip |
| `claims` | superviseur | hostname, station, status, retry_after, reset_at, lease_until |
| `incidents` | job SLO | scope, station, cause, started_at, ended_at |
| `rotation_jobs` | fork (via superviseur) | id, station, candidates, attempt, outcome, latency |

## Métriques de succès

| Métrique | Avant (mesuré) | Cible |
|---|---|---|
| `client_usable` même cycle | 0/6 | 6/6 pendant 30 min |
| `warm_pool` vivant | 15 (non consommé) | ≥ 6 et consommé |
| Taux de 200 | 5,6 % | > 30 % |
| `no_egress` dans claims | 78,2 % | < 30 % |
| `code reload` / 15 min | 0 (depuis 08:32Z) | 0 en permanence |
| Uptime app | crash 10:51Z, relance manuelle 11:08Z | 99,9 % sur 24 h |
| `rotate_committed` | non mesuré | > 50 % |
| Latence rotation p95 | jusqu'à 300 s | < 240 s |
| Resets respectés | non mesuré | 100 % |
| `requests.db` | 1,79 Gio | < 500 Mio |
| Incidents | 17 516 cumulés | ≤ 5/h |

## Dépendances

- Fork 016 (`016-control-plane`) : `/v1/vpn/rotate`, `/v1/vpn/state`,
  `/v1/vpn/jobs` (à exposer pour la rotation asynchrone).
- `registry.db` schéma v3 : migration douce pour les nouveaux champs
  (`upstream_verdict`, `source`).
- CI existante (`.github/workflows/ci.yml`).
- Specs 017 et 018 : la primitive `rotate` et la migration douce restent la
  base.

## Non-objectifs

- Ne pas changer de fournisseur VPN.
- Ne pas ajouter de mécanisme de compte ou de clé payante.
- Ne pas réécrire `fresh_chain.py` ni `fresh_chain_supervisor.py`.
- Ne pas modifier les réglages WARP de `config.yaml`.
- Ne pas créer de septième station, même temporairement.