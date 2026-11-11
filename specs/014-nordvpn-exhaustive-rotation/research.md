# Research — qualification exhaustive NordVPN

**Date** : 2026-11-09
**Feature** : `014-nordvpn-exhaustive-rotation`

Ce document consigne les décisions techniques et les alternatives rejetées.
Les mesures brutes sont dans `audit.md`.

---

## R1. Unité de qualification : hostname + transport, pas « IP »

**Constat** : avant un bind, on ne connaît pas l'IP d'egress ; un hostname peut
rendre une IP différente selon le transport et le rebind. Le quota est attaché à
l'IP observée, pas au hostname.

**Décision** : la file stocke le **hostname** (avec ses transports) ; l'IP
observée est enregistrée dans `qualification_attempts.egress_ip` puis fusionnée
dans `egress_options` (clé canonique par IP). Une même IP rendue par deux
hostnames est une seule option de rotation ; deux stations ne la partagent
jamais.

**Rejeté** : une file par IP (impossible avant le bind) ; une file par serveur
sans transport (un hostname WireGuard-only échouerait sur OpenVPN).

Le catalogue legacy de 9 788 hostnames n'est **pas** la source de la file :
2 008 hostnames observés dans `scan_attempts` ne sont pas dans l'API courante.
Ils sont `skipped`/`stale` et ne consomment aucune session ; seuls les
hostnames de l'API ou du manifeste runtime sont qualifiés.

---

## R2. Score de potentiel avant mesure

**Objectif** : ordonner 7 070 hostnames dont 99,9 % n'ont aucune observation
récente.

**Décision** : score 0–100, calculé avant la première mesure :

| Facteur | Poids | Source | Justification |
|---|---:|---|---|
| Fiabilité | 30 % | taux de 200 Laplace sur `qualification_attempts` | « meilleur rating » |
| RTT opérateur | 30 % | `PreflightProber` (DNS + connect + handshake), médiane de 3 | « meilleur ping par rapport à l'opérateur » |
| Quota restant | 20 % | `egress_ips`/`quota_buckets` | « la moins utilisée » |
| Charge API | 15 % | `load` du catalogue | « moins utilisée » côté serveur |
| Fraîcheur | 5 % | âge de la dernière observation/catalogue | évite les données périmées |

La fiabilité utilise un lissage de Laplace : `(200 + 1) / (200 + 429 + 2)`.
Les `no_egress`/`bind_error` sont exclus du dénominateur (ce sont des échecs
d'infrastructure, pas de hostname). Si aucune donnée : 0,5 neutre.

L'API NordVPN n'expose pas de note de serveur : le « rating » est donc le taux
de 200 mesuré, pas un attribut fournisseur. La « meilleure connexion » est
représentée par le RTT préflight et la disponibilité du transport ; le
transport est un filtre/tiebreak, pas un facteur de score (constitution IX).

Le quota restant est neutralisé à 0,5 quand l'IP est inconnue ; quand elle est
connue : `clamp(remaining / quota_estimate, 0, 1)`, avec `remaining = 0` si
`reset_at` est futur.

**Rejeté** : utiliser le pays comme facteur (constitution IX/VI) ; utiliser la
source (constitution IX) ; utiliser uniquement la charge API (ne dit rien du
quota ni du ping) ; utiliser le score mesuré pour un host jamais testé (50,0
artificiel dans `egress_catalog.score_option`).

---

## R3. Score mesuré après qualification

**Décision** : conserver la formule de la constitution IX, déjà implémentée
dans `free/egress_catalog.score_option` :

- 40 % taux de 200 ;
- 25 % latence (budget 8 000 ms) ;
- 20 % débit (throughput observé) ;
- 10 % stabilité (1 − fail/tests) ;
- 5 % fraîcheur.

La latence through-tunnel remplace la latence préflight dans ce score ; la
valeur préflight reste stockée séparément. Le score mesuré n'ordonne que les
options `usable` ; il ne réordonne pas la file de qualification.

**Rejeté** : un seul score pour les deux phases ; cela mélangerait un a priori
et une mesure, et permettrait à un host inconnu de battre un host prouvé.

---

## R4. Ordre de la file

**Décision** :

```sql
ORDER BY potential_score DESC,
         quota_remaining DESC,
         rtt_ms ASC,
         load ASC,
         hostname ASC
```

Portes dures avant : `next_eligible_at <= now`, `reset_at <= now`, statut
`pending` ou `quota` reseté (un `usable` n'est jamais re-testé), transport
supporté, non denylisté, non claimé, non `auth`/`dead`/`skipped`.

Un hostname sans RTT connu reçoit un RTT neutre (budget, pas 0) pour ne pas
fausser l'ordre ; il est préflighté avant le bind.

**Rejeté** : `ORDER BY fail_count, catalog.priority, hostname` (ordre actuel,
statique et non mesuré) ; `ORDER BY hostname` (alphabétique, famine) ;
`ORDER BY country` (quota pays déguisée).

---

## R5. Balayage par époque, reprenable

**Décision** :

- Une **époque** est un passage sur tous les hostnames éligibles.
- `qualification_epochs` : `epoch_id`, `started_at`, `finished_at`, `status`,
  `total`, `tested`, `usable`, `quota`, `infra`, `dead`, `skipped`, `cursor`.
- `sweep_cursor` : `(epoch_id, hostname, status, next_eligible_at, updated_at)`.
- `max_attempts_per_host_per_epoch` (défaut 3) ; au-delà, le hostname est
  `skipped` (`max-attempts`) pour l'époque, sans être `dead`.
- Reprise : au démarrage, les leases `claimed` expirés repassent `pending` ;
  le curseur reprend au prochain hostname non terminal.
- Le balayage **continue après un 200** ; il ne s'arrête que si l'époque est
  complète ou si le budget `sweep_time_budget_s` est atteint.

**Rejeté** : un run unique non repris (l'existant : 107/285 runs interrompus) ;
un curseur en JSON (constitution III) ; un arrêt au premier 200 (l'existant :
`recovered`).

---

## R6. Quota et rotation sur 429

**Décision** :

- Le `reset_at` vient exclusivement de la 429 fournisseur (`Retry-After` ou
  corps) ; aucune estimation locale (constitution IV).
- Une IP `usable` qui reçoit 429 passe `quota` avec `reset_at` ; elle n'est plus
  sélectionnée avant.
- La station libère l'option et claim le meilleur candidat suivant en p95
  < 60 s ; elle n'attend pas le reset de l'IP perdue.
- Le ledger `quota_buckets`/`egress_ips` fournit `used`, `successes`,
  `failures`, `reset_at` ; la sélection vérifie `reset_at <= now`.
- Un 429 sans 200 antérieur est une mauvaise IP, pas un quota consommé
  (constitution IV) ; backoff court, respect du reset.
- Jitter de 0–60 s après un reset pour éviter la ruée.

**Rejeté** : attendre le reset avant de rotation (immobilise la station) ;
réessayer la même IP avant le reset (viole IV) ; compter le 429 comme `dead`
(confond quota et mort) ; utiliser un cooldown hostname de 600 s comme seule
vérité (ne dit rien du quota IP).

---

## R7. Classification des échecs

**Décision** :

| Résultat | État | Effet |
|---|---|---|
| `200` + contenu | `usable` | promotion, score mesuré, service |
| `429` | `quota` | `reset_at`, rotation |
| `no_egress` | `infra` | retry autre transport/station, backoff borné |
| `bind_error` | `infra` | idem |
| `timeout` | `infra` | idem, backoff plus long |
| `AUTH_FAILED` | `auth` (station) | quarantaine station, host `pending` |
| `NXDOMAIN`/invalide | `dead` | terminal, raison |
| retiré du catalogue | `skipped` | `removed-from-catalog`, pas de bind |
| dédié/denylist/transport absent | `skipped` | raison, pas de bind |
| `max-attempts` | `skipped` | pour l'époque seulement |

**Rejeté** : marquer `no_egress` comme `dead` (85 % des tentatives) ; marquer
`AUTH_FAILED` comme host mort (c'est la station) ; marquer un host retiré
comme `no_egress` (ce n'est pas un échec de bind).

---

## R8. Ping opérateur

**Décision** : utiliser `free/ip_registry/preflight.py` :

- résolution DNS ;
- connect TCP (OpenVPN TCP) ou probe UDP (WireGuard/OVPN UDP) ;
- handshake si disponible ;
- 3 échantillons, médiane, timeout configurable.

Le point de mesure est l'hôte de l'opérateur ; aucune géolocalisation externe
n'est nécessaire. Le RTT préflight alimente le score de potentiel ; la latence
through-tunnel alimente le score mesuré.

**Rejeté** : ping ICMP seul (souvent bloqué) ; géolocalisation par pays (ne
mesure pas la route réelle) ; charge API comme proxy de latence (agrégat, pas
une mesure).

---

## R9. Suppression du `country_backoff`

**Constat** : 5 407 hostnames sont en `country_backoff` ; le pays est mis en
pause après 3 × 429 ou 5 × bind failures. Cela viole la constitution VI et
empêche de découvrir les IP fraîches d'un pays.

**Décision** : supprimer la branche `country_backoff` de
`free/catalog_claims.py:316-320` et les déclencheurs de
`scripts/fresh_chain.py:1817-1843`/`:2053-2080`. Le pays reste une métadonnée
pour l'affichage. Le backoff est par hostname/IP :

- `infra_backoff_s` 300, `infra_backoff_max_s` 3600, exponentiel avec jitter ;
- `429` : `reset_at` de l'IP, pas un cooldown pays ;
- 200 : remise à zéro.

**Rejeté** : garder un backoff pays « court » (10 min) ; le problème n'est pas
la durée, c'est la porte. Un pays de 2 485 serveurs ne peut pas être jugé sur
trois IP.

---

## R10. Stockage et intégration

**Décision** :

- `registry.db` : `qualification_candidates`, `qualification_attempts`,
  `qualification_epochs`, `sweep_cursor`, `server_transports`.
- `egress_options` : options observées, fusion par IP.
- `free/qualification/` : domaine pur, store, source, preflight, sweep,
  runtime.
- `free/egress_runtime.py` : construit le service et le planifie ; pas de
  nouveau daemon.
- `scripts/fresh_chain.py` : mode `--sweep` (continue après 200) et mode
  `--repair` (arrêt au premier 200) ; `--claims` lit la file.
- `scripts/qualification_sweep.py` : CLI d'exploitation.
- `dashboard/api.py` : endpoints de couverture.

**Rejeté** : un service séparé (complexité de déploiement, deux propriétaires) ;
un état JSON (constitution III) ; écrire dans `logs/ip_registry.db` (0 octet,
mauvais chemin).

---

## R11. Priorité d'exécution

**Décision** : service > repair > sweep.

- Une station qui sert du trafic n'est jamais préemptée par le balayage.
- Le balayage utilise les stations idle ou les créneaux entre requêtes.
- `max_active_stations` reste 6 ; le balayage ne crée pas de session
  supplémentaire.
- Si toutes les stations servent, le balayage attend ; il ne dégrade pas le
  service.

**Rejeté** : préempter une station qui sert (baisse de capacité) ; ouvrir une
7e session (limite compte) ; utiliser le payant (interdit).

---

## R12. Observabilité

**Décision** :

- API : `summary`, `queue`, `next`, `epochs`, `attempts`, `quota`.
- Dashboard : couverture par pays/transport, époque, prochains candidats,
  lancer/pause, export.
- Métriques : `tested`, `200`, `429`, `infra`, `skipped`, `queue_depth`,
  `epoch_duration_s`, `time_to_first_200_s`.
- Preuve : chaque compteur est dérivable de `qualification_attempts` +
  `observations` ; aucun chiffre déclaré (constitution I).

**Rejeté** : un dashboard qui ne montre que le nombre d'options `usable` (ne
dit pas pourquoi les autres ne le sont pas) ; un compteur sans échantillons.

---

## R13. Alternatives globales rejetées

| Alternative | Raison du rejet |
|---|---|
| Ajouter des stations | La ressource rare est la session ; le problème est la qualification. |
| acheter des IP / second compte | Interdit par la jambe free et AGENTS.md. |
| Attendre le reset global | Le quota est par IP ; aucune attente globale n'est justifiée. |
| Utiliser `catalog` legacy comme source | 9 788 hostnames, dont des fantômes ; l'API est l'autorité. |
| Garder `country_backoff` | Viole VI et parque 5 407 hostnames. |
| Un seul score | Mélange a priori et mesure ; faux 50,0 pour l'inconnu. |
| Un run unique sans reprise | 107/285 runs interrompus ; pas de couverture. |
| Un service séparé | Deux propriétaires ; complexité de déploiement. |

---

## R14. Parallélisme et continuité des stations

**Constat** : `free/rotation_planner.py:1-11` interdit deux rotations
simultanées ; `config.yaml:1259-1263` définit des pauses globales ;
`catalog_claims` partage le `country_backoff` ; `fresh_chain` rebinde avant de
prouver la nouvelle IP. Sur les 2 871 échantillons SLO des dernières 24 h,
49,8 % sont `6/6 healthy` et 38,4 % `0/6`.

**Décision** :

- Un worker et un état par station ; aucun lock global.
- Rotations concurrentes autorisées sur des stations différentes, avec un rate
  limit global de rebind.
- Standby par station et rotation proactive au seuil de headroom.
- Make-before-break : l'ancienne IP sert jusqu'au 200 de la nouvelle ;
  rollback si échec.
- Cutover mesuré : 0 si second tunnel/espace de noms, sinon p95 < 2 s,
  max < 5 s.
- Disponibilité cible ≥ 99 % `client_usable` par station quand une IP 200
  existe.
- Protection de la dernière station utilisable ; jamais 0/6 par rotation
  planifiée.

**Rejeté** : une rotation à la fois (immobilise les autres) ; pauses globales
(une IP affecte six stations) ; rebind sans standby (coupure non mesurée) ;
septième session permanente (limite compte).

---

## R15. Stratégie multi-transports cyclique

**Constat** : `fast_discovery: true` réduit la liste à un transport
(`scripts/fresh_chain.py:1903-1917`) ; un 429 passe au hostname suivant
(`:2018-2028`) ; `server_transports` est vide. WireGuard porte 73 200,
OpenVPN UDP/TCP cumulent 16 045 tentatives et 0 200.

**Décision** :

- En `auto`, parcourir tous les transports supportés :
  `wireguard` → `openvpn-udp` → `openvpn-tcp` → `wireguard`.
- Un échec infra passe au transport suivant ; un 429 n'empêche pas les autres.
- `transport_retry_rounds` (défaut 2) et `transport_retry_backoff_s` (défaut
  60) bornent le cycle.
- Curseur/round persistés ; reprise au bon transport.
- Santé par transport ; `degraded` après `transport_health_min_attempts` sans
  200 ; diagnostic OpenVPN (bind/AUTH/probe/DNS).
- Aucun hostname `dead` après un seul transport.

**Rejeté** : un transport fixe en `auto` ; sauter les autres transports sur
429 (hypothèse non mesurée) ; condamner le host sur un `no_egress` WireGuard ;
retenter OpenVPN à l'aveugle sans diagnostic.

---

## R16. Watchdog, gestion et découverte de bout en bout

**Constat** : `ops/supervisor.py` annonce un watchdog non branché ;
`free/station_health.py` n'est pas persisté (`station_state` vide) ;
`free/repair_queue.py` vit dans une base séparée ; `repair_jobs` et
`repair_requests` sont vides ; les 162 incidents sont tous `scope='fleet'` (157
`client_usable_low`, 3 `sweep_stalled`, 2 `warm_pool_low`) ;
la découverte est un script ciblé (`scripts/ip_registry_discover.py`), pas un
scheduler ; `catalog_sources.server_count=0` et `server_transports` est vide.

**Décision** :

- Watchdog par station : heartbeat, sondes tunnel/DNS/egress/AUTH/quota/
  `client_usable`, état dans `station_state`, incidents `scope='station'`.
- Réparation locale uniquement ; superviseur seul propriétaire ; file
  `repair_jobs` dans `registry.db` ; backoff, `max_repair_attempts`, escalade.
- Le watchdog ne redémarre jamais une station saine qui sert ; heartbeat stale
  → redémarrage du watchdog seul.
- Découverte schedulée : API 6 h → cache → embedded → manifeste ;
  `discovery_runs`, `stale`, jamais un catalogue vide.
- Réutilisation de `ops/lifecycle.py`, `ops/reconciler.py`,
  `ops/supervisor.py`, `free/station_health.py` et `vpn/manager.py` par
  strangler.

**Rejeté** : un second watchdog parallèle ; une seconde file de réparation ;
un fork de `vpn/manager.py` ; une découverte one-shot ; des incidents `fleet`
pour des pannes locales.

---

## R17. Démantèlement des monolithes

**Constat** : `opencode.py` 31 347 lignes, `vpn/manager.py` 11 385,
`free/pool.py` 8 190, `dashboard/api.py` 6 845, `scripts/fresh_chain.py` 2 535,
`fresh_chain_supervisor.py` 1 934. La couverture totale est de 38 % (dernier
`.coverage`). Les dettes D-02 (P1 monolithes) et D-20 (P2 dashboard) sont
ouvertes.

**Décision** :

- Strangler par seams, pas de big-bang.
- Tests de caractérisation avant chaque extraction ; shadow, canary, rollback.
- Cibles : `free/station/` (manager), `free/pool/` (pool),
  `free/qualification/sweep.py` (fresh_chain), `dashboard/routers/`
  (dashboard), `free/request_pipeline/` (opencode).
- Chaque module < 1 500 lignes ou entrée fine ; parité p95 ≤ +5 % ; 0 cycle ;
  domaine pur.
- D-02/D-20 closes à la fin ; aucun nouveau module monolithique.

**Rejeté** : big-bang (risque de régression), fork du manager, extraction sans
tests de caractérisation, flag permanent qui masque un échec, réécriture qui
change le comportement fonctionnel.

---

## R18. Questions ouvertes

1. **Budget de quota** : faut-il plafonner le nombre de tests par IP et par
   jour pour ne pas gaspiller le quota qui pourrait servir ? La spec propose
   `max_attempts_per_host_per_epoch=3` et une priorité service, mais un budget
   par IP reste à valider en canary.
2. **Fréquence d'époque** : une époque par 24 h est l'objectif ; la fréquence
   réelle dépend du débit mesuré sur six stations.
3. **Dédiés** : certains serveurs dédiés refusent la jambe free ; la liste
   exacte doit être mesurée, pas supposée.
4. **Throughput** : le débit through-tunnel n'est pas encore mesuré par le
   probe actuel ; prévoir un petit transfert ou une mesure de temps de premier
   octet.
5. **Opérateur multi-site** : si l'opérateur change de réseau, les RTT changent ;
   prévoir un `measurement_point_id` et un recalcul.
