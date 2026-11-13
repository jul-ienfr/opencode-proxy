# Checklist — Récupération autonome des six stations

**Feature** : `018-autonomous-six-station-recovery`
**Date** : 2026-11-14

## Primitive

- [x] T201 — un seul `/v1/vpn/rotate` par chaîne.
- [x] T202 — `committed`/`exhausted`/`rolled_back` sans disable.
- [x] T203 — le fork épuise candidats et transports.

## Candidats

- [x] T204 — preuves < 6 h, warm pool frais.
- [x] T205 — qualification live tunnel + DNS.
- [x] T206 — budgets et timeouts bornés.
- [x] T207 — ordre du registre, aucun re-score fork.

## Auth et conteneur

- [x] T208 — `AUTH_FAILED` : refresh + cooldown + WG, pas de boucle.
- [x] T209 — identifiants/image périmés ⇒ recreate.
- [x] T210 — conteneur crashé/API down ⇒ recreate autonome.
- [ ] T211 — DNS transitoire ne tue pas un tunnel sain.

## Preuves

- [x] T212 — 0 recreate d'une station saine sur 24 h.
- [ ] T213 — réparation autonome live, sans commande manuelle.
- [ ] T214 — SLO détection/réparation/switch mesurés.
- [ ] T215 — benchmark p95 ≤ 45 s.
- [ ] T216 — 6/6 `client_usable`, IP distinctes, proxies, soak 24 h.
- [x] T217 — aucune station comptée sans preuve 200 fraîche.

## Definition of Done

1. Une station cassée revient seule `healthy` + `egress.verified=true`.
2. Une station saine n'est jamais recréée sur 24 h.
3. 6/6 `client_usable` en free, IP distinctes, proxies OK.
4. Trois transports visibles et conservés.
5. Aucune commande manuelle dans les logs de réparation.
