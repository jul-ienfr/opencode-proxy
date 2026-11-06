# Feature Specification: Disponibilité permanente des stations free

**Feature Branch**: `001-free-station-availability`

**Created**: 2026-11-04

**Status**: Draft

**Input**: « Les six stations doivent être client-utilisables en permanence,
sans intervention. La liste NordVPN doit être la plus complète possible, chaque
IP présélectionnée et notée, et le quota/reset géré automatiquement. »

## Contexte et problème

La jambe free sort par six stations Docker (une session NordVPN chacune). Elle
est déjà protégée côté *porte* (TLS Bun, en-têtes, corps officiel) — hors
périmètre ici. Ce qui casse est la *sortie* : une IP peut être grillée (429
amont), un tunnel peut tomber (AUTH, egress mort), et les six stations peuvent
partager le même /24, donc mourir ensemble au reset fournisseur.

L'état actuel mélange trois cycles de vie (IP, station, session) et trois
vocabulaires d'états. L'allocateur est désactivé en config (`mode: false`), la
règle de diversité est réglée à `max_per_24: 6` (donc inopérante), et un seul
modèle sert de sonde. Résultat : des épisodes 6/6 connectées mais 0/6
utilisables, et aucune mesure de disponibilité par station.

## User Scenarios & Testing *(mandatory)*

### User Story 1 - Aucun trou de service (Priority: P1)

Un client appelle la jambe free. Le système choisit une station
client-utilisable sans intervention, et si toutes les IP actives sont grillées,
il promeut une IP validée du pool chaud avant que la dernière station ne tombe.

**Why this priority**: c'est la promesse visible : le trafic free ne doit
jamais voir 0/6.

**Independent Test**: couper une IP active (429 forcé) et vérifier que le
trafic repart sur une remplaçante validée en moins de 60 s, sans commande
manuelle.

**Acceptance Scenarios**:

1. **Given** 6 stations client-utilisables, **When** une IP reçoit un 429
   quota, **Then** elle passe en `COOLING` avec son `reset_at`, une `WARM` est
   promue, et le nombre de stations utilisables ne descend pas sous 5.
2. **Given** le pool chaud est vide, **When** une IP tombe, **Then** une chaîne
   de réparation démarre automatiquement dans la limite du budget de rebind et
   l'état dégradé est exposé.
3. **Given** le superviseur est arrêté, **When** le proxy démarre, **Then** le
   superviseur démarre avec lui et un watchdog le relance s'il meurt.

---

### User Story 2 - Présélection notée (Priority: P2)

À la découverte, chaque serveur candidat est testé (bind, egress, probe
officielle) et reçoit une note. La file de validation et l'ordre de promotion
suivent cette note, de sorte que les meilleures IP soient utilisées d'abord.

**Why this priority**: sans note, le pool se remplit dans l'ordre du catalogue
et les mauvaises IP consomment le budget de rebind.

**Independent Test**: injecter 20 candidats de qualité connue et vérifier que
la file de validation sort les meilleurs en premier, et qu'une IP notée 90 est
promue avant une IP notée 40.

**Acceptance Scenarios**:

1. **Given** un nouveau hostname, **When** le probe retourne 200 avec contenu,
   **Then** un enregistrement `ip_state` est créé avec `score`, composantes,
   `last_ok_at` et `latency_ms`.
2. **Given** deux IP validées, **When** une station a besoin d'une IP,
   **Then** la mieux notée est choisie, sauf contrainte de diversité.
3. **Given** une note calculée il y a 72 h, **When** une IP fraîche est
   disponible, **Then** la fraîcheur fait perdre la priorité à l'ancienne.

---

### User Story 3 - Quota et reset respectés (Priority: P2)

Chaque usage et chaque échec sont horodatés. Le `reset_at` fournisseur est
enregistré et fait barrière : aucune IP n'est re-testée avant.

**Why this priority**: re-tester une IP bloquée brûle du quota de rebind et
fabrique de faux négatifs.

**Independent Test**: forcer un 429 avec `Retry-After: 3600`, vérifier que
l'IP n'est plus proposée pendant 1 h puis re-testée avec jitter après
l'échéance.

**Acceptance Scenarios**:

1. **Given** une 429 quota, **When** le `reset_at` est enregistré, **Then**
   l'IP est exclue de toute sélection et de tout probe avant cette date.
2. **Given** une 429 transitoire sans `Retry-After`, **When** elle est classée
   `transient`, **Then** l'IP n'est pas blacklistée jusqu'au lendemain.
3. **Given** une 429 sans aucun succès antérieur, **When** elle est classée
   `bad_ip`, **Then** la quarantaine longue s'applique, distincte du quota.

---

### User Story 4 - Liste la plus complète possible (Priority: P3)

Le catalogue de serveurs est maintenu avec provenance et fraîcheur, et chaque
serveur actif est soit vérifié (egress + note), soit en file avec une raison.

**Why this priority**: une IP sans hostname n'est pas disponible ; l'inventaire
historique seul donne une fausse impression de stock.

**Independent Test**: vérifier qu'aucune IP de l'inventaire ne reste sans
hostname plus de 7 jours sans raison tracée, et que le catalogue fusionne les
manifestes Gluetun disponibles.

**Acceptance Scenarios**:

1. **Given** un catalogue de N serveurs actifs, **When** le statut est demandé,
   **Then** il expose vérifiés / en file / sans adaptateur, avec la raison.
2. **Given** un hostname absent de l'union des manifestes runtime, **When**
   une chaîne prépare un bind, **Then** il n'est jamais proposé.

---

### User Story 5 - Montée en charge (Priority: P3)

Passer de 6 à 10 stations, ou revenir à 1, ne demande qu'un changement de
configuration.

**Why this priority**: la contrainte de session est globale ; le nombre de
stations est un paramètre, pas une hypothèse de code.

**Independent Test**: passer `station_count` de 6 à 8 et vérifier que les
conteneurs, ports et overrides sont créés, et que les stations au-delà du
budget de sessions sont marquées `standby`, pas `broken`.

**Acceptance Scenarios**:

1. **Given** `station_count: N`, **When** N dépasse la limite du compte,
   **Then** les stations excédentaires sont `standby` et l'état est exposé.
2. **Given** une station ajoutée, **When** le superviseur tourne, **Then** elle
   entre dans la boucle sans modification de code.

---

### Edge Cases

- Toutes les IP du pool sont en `COOLING` avant leur reset : le système expose
  `capacity_degraded`, alerte, et ne teste rien avant échéance.
- Le reset fournisseur tombe pendant une rotation : la nouvelle IP est
  conservée, l'ancienne reste `COOLING` jusqu'à son heure.
- Deux stations obtiennent le même egress : la seconde est libérée et
  re-claim, jamais assignée.
- Un probe renvoie 200 sans contenu : compté comme échec, jamais comme succès.
- Le compte NordVPN refuse une 7e session : la 7e station est `standby`, pas
  `broken`.
- Le superviseur meurt en cours d'assignation : au redémarrage, l'état est
  réconcilié depuis SQLite, pas depuis la mémoire.
- Un /24 entier tombe : la diversité doit avoir laissé au moins une station
  hors de ce /24, sinon l'incident est déclaré corrélé.

## Requirements *(mandatory)*

### Functional Requirements

**Cycle de vie et automatisation**

- **FR-001**: Le superviseur MUST démarrer avec le proxy et être relancé par un
  watchdog s'il s'arrête.
- **FR-002**: Le système MUST détecter le besoin (station inutilisable, modèle
  inutilisable, absence d'IP, tunnel non connecté) et réparer sans commande
  manuelle.
- **FR-003**: Une station MUST NOT rester vacante si une IP validée est
  disponible ; la promotion doit être effective en moins de 60 s (p95).
- **FR-004**: Les réparations MUST être bornées par le budget de sessions et le
  budget de rebind, tous deux en configuration.
- **FR-005**: Un interrupteur unique MUST arrêter l'automatisation en conservant
  l'état et les assignations.
- **FR-006**: Chaque décision automatique MUST être journalisée avec sa cause,
  son horodatage et la preuve associée.

**Présélection et score**

- **FR-010**: À la découverte, chaque candidat MUST être testé (bind, egress,
  probe officielle) avant d'entrer dans le pool sélectionnable.
- **FR-011**: Chaque IP MUST porter une note dans [0, 100] et ses composantes :
  disponibilité historique, taux de 200 valides, latence, débit, fraîcheur,
  stabilité, headroom de quota, pénalités de diversité et de charge.
- **FR-012**: Le calcul MUST être déterministe (mêmes entrées → même note) et
  couvert par des tests unitaires.
- **FR-013**: La note MUST décroître avec l'âge de l'observation (TTL de
  fraîcheur) et être recalculée à chaque nouvelle observation.
- **FR-014**: La note MUST ordonner la file de validation et la promotion
  `WARM → ACTIVE`, sans jamais contourner les portes dures.
- **FR-015**: Les poids MUST être en configuration, avec une somme normalisée à
  1, et un plancher `min_score` en dessous duquel une IP n'est pas promue.

**Quota, reset et échecs**

- **FR-020**: Chaque requête MUST enregistrer l'IP d'egress, la station,
  l'horodatage de début et de fin, et le résultat.
- **FR-021**: Aucun probe ni usage MUST avoir lieu avant `reset_at` ; le
  timestamp vient du fournisseur, jamais d'une estimation locale.
- **FR-022**: Après `reset_at`, le re-test MUST appliquer un jitter pour éviter
  la ruée.
- **FR-023**: Les 429 MUST être taxonomisées (`daily`, `rate_limit`,
  `transient`, `unknown`, `invalid_session`) et traitées différemment.
- **FR-024**: Une 429 sans succès antérieur MUST être classée `bad_ip` avec
  quarantaine longue, distincte du quota consommé.
- **FR-025**: Le quota MUST être indexé par IP d'egress (et par famille de
  modèle), jamais par station ni par hostname.
- **FR-026**: Une station MUST entrer en `DRAINING` quand son headroom passe
  sous le seuil de rotation, et une remplaçante MUST être préparée avant
  l'épuisement.
- **FR-027**: Chaque requête free MUST porter un `ses_` dont le préfixe 12-hex
  est minté par `descending()` (`Date.now()*0x1000+counter`, complément 48
  bits) ; un préfixe aléatoire MUST être traité comme un défaut de format et
  re-minté, jamais comme un quota épuisé. Les requêtes de test MUST utiliser
  un `ses_` neuf par requête (ou réutiliser explicitement celui de la
  capture), jamais un préfixe aléatoire.

**Inventaire et catalogue**

- **FR-030**: Le catalogue MUST conserver la provenance et la fraîcheur de
  chaque serveur (API, embarqué, cache, manifeste Gluetun).
- **FR-031**: Le système MUST maintenir une table hostname → egress observé,
  avec date d'observation et nombre de confirmations.
- **FR-032**: Une IP sans hostname connu MUST NOT être sélectionnable ; elle est
  en file de mapping avec une raison.
- **FR-033**: La découverte MUST être bornée (candidats par cycle, rebinds par
  minute) et priorisée par la note.
- **FR-034**: Le statut MUST exposer, pour chaque serveur actif : vérifié, en
  file, ou sans adaptateur, avec la raison.

**Diversité**

- **FR-040**: Au plus 2 stations par /24, au plus 2 par pays, au moins 3 /24
  distincts dès 3 stations.
- **FR-041**: Si la contrainte ne peut pas être tenue, l'état
  `diversity_degraded` MUST être exposé avec la contrainte bloquante.
- **FR-042**: Une station MUST NOT être assignée à un egress dont le /24 est
  déjà représenté au-delà du plafond, même si sa note est la meilleure.
- **FR-043**: Aucun pays ni hostname MUST NOT être codé en dur comme source
  prioritaire ; une liste de secours ne peut servir qu'après épuisement du
  catalogue et doit expirer.
- **FR-044**: Un pays ou un pool qui produit N échecs consécutifs MUST entrer
  en cooldown temporaire, sans jamais bloquer les autres pays.
- **FR-045**: La sélection MUST couvrir l'ensemble des pays du catalogue, la
  géographie étant un facteur de score et d'anti-affinité, pas un filtre
  d'exclusion, sauf politique explicite par modèle.
- **FR-046**: À chaque requête, la sélection MUST utiliser l'observation la
  plus fraîche disponible ; une mesure plus vieille que `max_state_age_s`
  n'est pas utilisée pour choisir.
- **FR-047**: Une part d'exploration (≥ 5 %) MUST être réservée aux IP et pays
  non représentés, afin d'empêcher un optimum local de se verrouiller.
- **FR-048**: Aucun hostname, pays ou plage d'IP ne MUST apparaître en dur dans
  le code de sélection ; un test de régression le vérifie.
- **FR-049**: Le changement d'IP d'une station MUST être planifié
  (`DRAINING` → promotion), jamais déclenché sur le chemin synchrone d'une
  requête client.

**Observabilité**

- **FR-050**: Chaque station MUST avoir une sonde synthétique périodique
  (≤ 30 s) avec validation du contenu, enregistrée.
- **FR-051**: La disponibilité MUST être calculée par station et par flotte sur
  1 h, 24 h, 7 j et 30 j.
- **FR-052**: Un journal d'incidents MUST enregistrer début, fin, cause et
  remédiation de chaque perte de disponibilité.
- **FR-053**: L'état MUST être exposé en JSON et dans le dashboard, avec
  `client_usable`, `warm_pool`, `auth30`, part du top /24, et la raison de
  chaque station non utilisable.
- **FR-054**: Les seuils d'alerte MUST être en configuration et déclenchés
  automatiquement (`client_usable < 5`, `warm_pool < 3`, `auth30 > 5`,
  top /24 > 50 %).

**Montée en charge**

- **FR-060**: `station_count` MUST être le seul paramètre nécessaire pour
  ajouter ou retirer une station (conteneur, port, override, entrée de boucle).
- **FR-061**: Les stations au-delà du budget de sessions MUST être `standby` et
  ne jamais être comptées comme en panne.
- **FR-062**: Le nombre maximal de stations MUST être
  `min(station_count, budget de sessions, limite de ports/conteneurs)` et
  exposé ; ajouter une station ne doit demander aucune modification de code.
- **FR-063**: Après une rotation, le système MUST re-tester le pays et le pool
  évincés avant de les considérer durablement mauvais (pas de condamnation
  définitive d'un pays).

### Key Entities

- **Station** : slot logique (id, conteneur, port SOCKS, transport, état
  `active/standby/broken`). Ne porte pas le quota.
- **Session** : connexion NordVPN (compte, limite, état auth). Ressource
  globale bornée.
- **Server** : serveur du catalogue (hostname, pays, ville, charge,
  technologies, source, fraîcheur).
- **EgressIp** : IP observée (ip, hostname, pays, état, note, composantes,
  `last_ok_at`, `tests`, `successes`, `reset_at`, `error_until`).
- **Assignment** : lien exclusif IP ↔ station (ouvert/fermé, horodatage).
- **Observation** : résultat d'un probe ou d'une requête réelle (statut, contenu
  valide, latence, débit, taxonomie 429).
- **Incident** : perte de disponibilité (station ou flotte), début, fin, cause,
  remédiation.

## Modèle de score de présélection

Note dans [0, 100], recalculée à chaque observation :

```text
score = 100 × (0.30·A + 0.20·G + 0.15·L + 0.10·T + 0.10·F + 0.10·S + 0.05·Q)
        − P_diversité − P_charge
```

| Composante | Définition | Défaut |
|---|---|---|
| `A` disponibilité | `(succès + 1) / (tests + 2)` (lissage de Laplace) | — |
| `G` porte | taux de 200 **avec contenu** sur les 3 premières sondes | — |
| `L` latence | `1 − min(1, ttfb_p50 / 8000 ms)` | budget 8 s |
| `T` débit | `min(1, tokens/s / 60)` | cible 60 t/s |
| `F` fraîcheur | `exp(−âge_h / 48)` | TTL 48 h |
| `S` stabilité | `1 − min(1, écart-type_latence / médiane)` | — |
| `Q` headroom | `headroom / quota`, 0.5 si inconnu | quota 500 |
| `P_diversité` | −15 si même /24 qu'une active, −10 si même pays | — |
| `P_charge` | `min(10, charge_serveur / 10)` | — |

Portes dures, prioritaires sur la note : `reset_at` non atteint, état
`retired/error`, IP déjà assignée, plafond de diversité atteint, absence de
preuve de contenu.

## Success Criteria *(mandatory)*

### Measurable Outcomes

- **SC-001**: Chaque station atteint **≥ 99,9 %** de disponibilité client sur
  30 jours glissants, avec **99,99 %** comme objectif mesuré et poursuivi ;
  l'écart résiduel est expliqué par le journal d'incidents.
- **SC-002**: La flotte ne tombe **jamais à 0/6** et reste à **≥ 5/6
  client-utilisables 99,99 %** du temps sur 30 jours.
- **SC-003**: **6/6 client-utilisables ≥ 99,9 %** du temps sur 30 jours.
- **SC-004**: Bascule sur remplaçante validée en **< 60 s p95** après détection.
- **SC-005**: Pool chaud ≥ 6 IP validées en permanence, jamais < 2.
- **SC-006**: **0** probe ou usage avant `reset_at` (violation = incident P0).
- **SC-007**: ≥ 95 % des serveurs actifs du catalogue ont soit un egress
  vérifié, soit une raison de file tracée ; aucune IP de l'inventaire ne reste
  sans hostname > 7 jours sans raison.
- **SC-008**: **0 intervention manuelle** sur 30 jours, hors test de
  l'interrupteur.
- **SC-009**: Le tri par note améliore le taux de 200 au premier probe d'au
  moins 30 % par rapport à l'ordre du catalogue (mesure sur 100 candidats).
- **SC-010**: Aucun /24 ne porte plus de 2 stations à aucun moment.
- **SC-011**: Zéro littéral de hostname, pays ou plage d'IP dans les modules de
  sélection (vérifié par test), et au moins 5 % des probes consacrés à
  l'exploration de pays non représentés.

### Écart arithmétique à trancher

99,99 % = 263 s/mois. Un rebind série coûte 30 à 60 s (`settle_s: 12` +
`egress_wait_s: 45` + probe). Conséquences :

| Fréquence de rotation | Disponibilité par station |
|---|---|
| 4 rotations/mois | ~99,99 % |
| 1 rotation/jour | ~99,93 % |
| 1 rotation/heure | ~97,2 % |

Et pour **6/6 en permanence à 99,99 %**, il faudrait moins de **8,6 s de
rotation par jour sur toute la flotte** — donc zéro rotation de routine ou
bascule sans coupure.

Trois options, décision opérateur requise (voir Clarifications) :

1. **SLO flotte** : ≥ 5/6 à 99,99 %, 6/6 visé à 99,9 %. Atteignable avec le
   matériel actuel.
2. **7e session** (second compte ou plan supérieur) : slot chaud validé avant
   de lâcher l'ancien ; seule voie vers 6/6 strict.
3. **IP stables** : la rotation devient un incident, pas une routine.

## Assumptions

- Le compte NordVPN autorise 6 sessions simultanées ; une 7e est refusée.
- Le trafic cible est ~20 requêtes/jour (référence `PLAN_FREE_6OF6`), avec un
  pic à absorber de l'ordre de 10×.
- Le reset quota est un bucket journalier annoncé par le fournisseur (~00:10
  UTC observé) ; `Retry-After` fait foi quand il est présent.
- L'impersonation de la porte (TLS, en-têtes, corps) est acquise et hors
  périmètre.
- L'inventaire de 3 435 IP est un **ensemble de candidats**, pas une vérité :
  chaque IP doit être re-validée.
- La clé payante peut rester expirée ; le repli payant n'est pas une
  dépendance du fonctionnement nominal.
- Windows + Docker Desktop + PowerShell ; le service démarre au boot.

## Clarifications

### Session 2026-11-04

- **C1 — Niveau du SLO (tranché le 2026-11-04)** : l'objectif est de se
  rapprocher le plus possible de 99,99 %, pas de l'atteindre strictement. Le
  SLO de référence devient flotte : ≥ 5/6 à 99,99 %, 6/6 visé à ≥ 99,9 %,
  jamais 0/6. `slo_target` reste en config.
- **C2 — Session supplémentaire** : un second compte ou un plan supérieur
  est-il disponible ? Sans lui, SC-001 par station reste conditionnel à des IP
  stables.
- **C3 — Définition de « client-utilisable »** : retenu ici = tunnel sain +
  probe 200 **avec contenu** + pas de 429 en fenêtre courante. À confirmer si
  le dashboard doit compter aussi les requêtes client réelles.
- **C4 — Couverture pays (tranché le 2026-11-04)** : tous les pays du catalogue
  doivent être éligibles ; aucune liste codée en dur ne doit favoriser un pays.
  La diversité reste bornée par session (≤ 2 par pays, ≤ 2 par /24).
