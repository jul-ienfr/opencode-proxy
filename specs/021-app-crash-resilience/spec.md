# Feature Specification: Résilience de l'app aux crashes et zombies

**Feature Branch**: `021-app-crash-resilience`

**Created**: 2026-11-15

**Status**: Draft

**Input**: Audit `docs/audits/2026-11-15-app-crash-audit.md` — « comment l'application a-t-elle pu mourir ? »

## Objectif

L'API `127.0.0.1:4000` ne doit plus jamais rester injoignable alors que le processus est vivant. Un socket d'écoute invalidé (`WinError 64`, `Accept failed on a socket`) doit être détecté et corrigé en moins de 60 secondes, sans intervention de l'opérateur, et chaque crash doit laisser une trace exploitable.

L'objectif opérationnel est : **aucune fenêtre d'indisponibilité supérieure à 60 s**, y compris après un changement réseau provoqué par une rotation VPN/WARP.

## État constaté

- `logs/app-stderr.log` contient `OSError: [WinError 64]` et `ERROR:asyncio:Accept failed on a socket`.
- Le processus reste vivant mais le port 4000 refuse les connexions : c'est un zombie.
- Aucun superviseur d'app n'était actif après le retour à la spec 18 : le zombie du 15/11 09:23 n'a été relancé qu'à 09:25, manuellement.
- `[warp-rotate] on_request` était exécuté à chaque requête en mode `vpn`, ajoutant du churn réseau inutile.
- `rotation_storm_cooldown_s=5` est hors bornes `[30, 7200]` et clampé à 30 à chaque chargement.
- Aucun crashguard/faulthandler n'est actif : pas de pile de crash natif.
- Aucun démarrage automatique au boot : un reboot laisse l'app non supervisée.

## User Scenarios & Testing

### User Story 1 — Un zombie est détecté et relancé (Priority: P1)

En tant qu'opérateur, je veux que l'app soit relancée automatiquement dès que `/health` ne répond plus, afin que le proxy ne reste jamais muet.

**Why this priority**: c'est la cause directe de l'indisponibilité prolongée.

**Independent Test**: tuer le socket d'écoute (ou le processus enfant) et vérifier que `/health` répond 200 en moins de 60 s, sans action manuelle.

**Acceptance Scenarios**:

1. **Given** l'app tourne, **When** `/health` échoue pendant 45 s, **Then** le superviseur tue le processus et en démarre un nouveau.
2. **Given** le processus a été tué, **When** le superviseur le relance, **Then** `/health` répond 200 en moins de 30 s.
3. **Given** l'app est saine, **When** le superviseur vérifie `/health`, **Then** aucun redémarrage n'est déclenché.

---

### User Story 2 — Le socket d'écoute est réparé sans attendre un crash (Priority: P2)

En tant qu'opérateur, je veux que `WinError 64` sur `accept()` recrée le socket d'écoute, afin d'éviter un redémarrage complet du processus.

**Why this priority**: réduit la fenêtre d'indisponibilité et évite de perdre l'état en mémoire.

**Independent Test**: provoquer une erreur `accept` et vérifier que le serveur ré-accepte une connexion dans les 10 s.

**Acceptance Scenarios**:

1. **Given** le socket d'écoute est invalidé, **When** `accept()` lève `WinError 64`, **Then** un nouveau socket est créé sur le même port.
2. **Given** la recréation échoue, **When** le superviseur détecte `/health` absent, **Then** il relance le processus.

---

### User Story 3 — Plus de rotation WARP en mode VPN (Priority: P3)

En tant qu'opérateur, je veux que `rotate_exhausted_managed()` ne s'exécute pas quand les WARP ne sont pas l'egress, afin de supprimer le churn réseau qui invalide le socket.

**Why this priority**: facteur aggravant identifié dans l'audit.

**Independent Test**: en `proxy_mode: vpn`, envoyer une requête et vérifier qu'aucun `[warp-rotate] on_request` n'est loggé.

**Acceptance Scenarios**:

1. **Given** `proxy_mode: vpn`, **When** une requête free passe, **Then** aucun scan/rotation WARP n'est déclenché.
2. **Given** `proxy_mode: socks5` ou `auto_egress`, **When** une requête passe, **Then** la rotation WARP reste disponible.

---

### User Story 4 — Chaque crash laisse une trace (Priority: P2)

En tant qu'opérateur, je veux un `app-crash.log` horodaté avec la pile des crashes natifs et des exceptions non gérées, afin de ne plus dépendre des seuls logs asyncio.

**Independent Test**: lever une exception dans un thread et vérifier l'entrée dans `logs/app-crash.log`.

**Acceptance Scenarios**:

1. **Given** une exception non gérée, **When** elle survient, **Then** une entrée horodatée avec traceback est écrite.
2. **Given** un crash natif (segfault), **When** il survient, **Then** `faulthandler` écrit la pile.

---

### User Story 5 — Démarrage automatique au boot (Priority: P1)

En tant qu'opérateur, je veux que `app_supervisor.py` démarre au boot Windows, afin qu'un reboot ne laisse jamais l'app sans surveillance.

**Independent Test**: redémarrer la session (ou le service) et vérifier que `/health` répond sans lancement manuel.

**Acceptance Scenarios**:

1. **Given** un reboot, **When** la session utilisateur démarre, **Then** `app_supervisor.py` est actif et l'app répond.

---

### User Story 6 — La configuration est validée (Priority: P3)

En tant qu'opérateur, je veux que les valeurs hors bornes soient refusées ou corrigées avec un log explicite, afin d'éviter les clamps silencieux.

**Independent Test**: mettre `rotation_storm_cooldown_s: 5` et vérifier un log d'erreur de validation.

**Acceptance Scenarios**:

1. **Given** une valeur hors bornes, **When** la config est chargée, **Then** un log explicite indique la valeur et la borne.
2. **Given** une valeur valide, **When** la config est chargée, **Then** aucun log d'erreur.

## Exigences fonctionnelles

- **FR-001** : le superviseur vérifie `/health` au moins toutes les 0,5 s et déclenche un redémarrage après 45 s d'échec consécutif.
- **FR-002** : chaque cycle de surveillance est journalisé dans `logs/app-supervisor.log` (démarrage, sortie, code, raison).
- **FR-003** : après un redémarrage, `/health` répond 200 en moins de 30 s.
- **FR-004** : `rotate_exhausted_managed()` est interdit en `proxy_mode: vpn`.
- **FR-005** : un crashguard installe `faulthandler` et un `excepthook` qui écrivent dans `logs/app-crash.log`.
- **FR-006** : `app_supervisor.py` est démarré au boot (service ou tâche planifiée) et ne dépend pas d'une session interactive.
- **FR-007** : les valeurs de configuration sont validées dans leurs bornes ; un clamp est loggé avec la valeur d'origine.
- **FR-008** : une alerte est émise si `/health` est absent plus de 60 s.
- **FR-009** : aucun sous-processus runtime n'ouvre de fenêtre console (`CREATE_NO_WINDOW`).
- **FR-010** : chaque exigence a un test de non-régression.

## Critères d'acceptation mesurables

| Critère | Mesure |
|---|---|
| Détection du zombie | `/health` échoue → redémarrage ≤ 45 s |
| Retour au service | `/health` 200 ≤ 30 s après redémarrage |
| Socket réparé | nouvelle connexion acceptée ≤ 10 s après `WinError 64` |
| WARP en mode vpn | 0 log `[warp-rotate] on_request` sur 100 requêtes |
| Trace de crash | 100 % des exceptions non gérées dans `app-crash.log` |
| Boot | `/health` 200 sans lancement manuel après reboot |
| Config | 0 clamp silencieux ; chaque clamp loggé |
| Fenêtres | 0 fenêtre console sur 10 min de trafic |

## Hors périmètre

- Refonte du pool, du quota ou du superviseur de flotte.
- Modification des six stations ou de leurs images Docker.
- Changement du wire free ou de l'identité de station.
- Jambe payante, second compte, workspace.

## Contraintes dures

- Ne pas perdre l'état en mémoire lors d'un redémarrage (le registre reste dans `registry.db`).
- Le superviseur d'app ne doit jamais ouvrir de fenêtre console.
- Le health check ne doit pas dépendre d'une connexion réseau externe.
- Un redémarrage ne doit pas tuer le superviseur de flotte.
- Les correctifs doivent rester compatibles avec la spec 18 et le rollback 19.
