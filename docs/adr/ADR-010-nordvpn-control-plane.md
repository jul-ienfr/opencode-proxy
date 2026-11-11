# ADR-010 — Control plane NordVPN et démantèlement des monolithes

**Date** : 2026-11-09
**Statut** : Accepted
**Feature** : `014-nordvpn-exhaustive-rotation`
**Constitution** : I–XVI

## Contexte

La jambe NordVPN est répartie entre `opencode.py` (31 347 lignes),
`vpn/manager.py` (11 385), `free/pool.py` (8 190), `dashboard/api.py` (6 845),
`scripts/fresh_chain.py` (2 535) et `fresh_chain_supervisor.py` (1 934). Le
catalogue amont contient 7 070 serveurs, mais le catalogue d’egress ne voyait
que six stations, la file de claims était ordonnée par une priorité statique,
les pays étaient mis en pause en bloc, et le scanner s’arrêtait au premier 200.

## Décisions

1. **`registry.db` est l’autorité unique** pour la file de qualification, les
   tentatives, les époques, les stations, les incidents, les réparations et la
   découverte. Aucun JSON d’état n’est une seconde vérité.
2. **La qualification est une file persistante par hostname**, ordonnée par
   score de potentiel, quota, RTT opérateur, charge et hostname. Le pays et la
   source ne sont pas des facteurs.
3. **Le balayage est une époque reprenable** : chaque hostname éligible reçoit
   une tentative, le curseur survit au crash, et un 200 ne termine pas l’époque.
4. **Le cycle multi-transports est explicite** : WireGuard → OpenVPN UDP →
   OpenVPN TCP → WireGuard, avec curseur, round et santé par transport.
5. **Les stations sont indépendantes et parallèles** : un worker et un état par
   station, aucune pause globale par défaut, rotations concurrentes bornées.
6. **La rotation est make-before-break** : standby, cutover prepare/switch/probe,
   rollback si la nouvelle IP ne prouve pas son 200.
7. **Le superviseur reste le seul réparateur** ; le watchdog appelle
   `StationSupervisor.restart` de la seule station concernée.
8. **Aucun pays n’est une porte dure** ; le `country_backoff` est supprimé.
9. **Les monolithes sont démantelés par slices** (strangler, parité, shadow,
   canary, rollback), avec un ratchet de taille.

## Conséquences

- Positives : couverture exhaustive mesurable, rotation sans quota pays,
  disponibilité par station, incidents locaux, reprise après crash, dette
  maîtrisée.
- Coûts : nouvelles tables, nouveaux modules, migration des claims, tests de
  caractérisation, adaptation des dashboards.
- Risques : régression comportementale pendant les extractions, couplage caché,
  performance des cutovers. Mitigations : tests de caractérisation, shadow,
  canary, rollback, benchmarks, ratchet.

## Suivi

- `specs/014-nordvpn-exhaustive-rotation/spec.md`
- `specs/014-nordvpn-exhaustive-rotation/plan.md`
- `specs/014-nordvpn-exhaustive-rotation/runbook.md`
- `specs/014-nordvpn-exhaustive-rotation/module-baseline.json`
- `debt.md` : D-02 (monolithes), D-20 (dashboard lourd)
