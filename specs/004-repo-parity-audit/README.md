# 004 — Audit des dépôts tiers et parité free

Paquet Spec Kit de l'audit du 2026-11-06. Il documente les neuf dépôts fournis,
les correctifs appliqués, l'état runtime et les divergences restantes.

## Lire dans l'ordre

1. [spec.md](./spec.md) — objectifs, exigences, critères d'acceptation.
2. [research.md](./research.md) — fiche par dépôt, faits de code, contradictions.
3. [plan.md](./plan.md) — architecture, journal des changements, décisions,
   risques, rollback.
4. [measurements.md](./measurements.md) — tests live et état runtime.
5. [tasks.md](./tasks.md) — fait / ouvert, avec la prochaine action.
6. [data-model.md](./data-model.md) — entités, invariants, transitions.
7. [quickstart.md](./quickstart.md) — vérifier sans secret ni action destructive.
8. [contracts/](./contracts/) — wire, `<env>`, cooldown, alias d'outils.

Audit consolidé (lecture rapide) :
[`docs/AUDIT_REPOS_2026-11-06.md`](../../docs/AUDIT_REPOS_2026-11-06.md).

## Résultat en une phrase

Le gate est compris et le proxy rend 200 sur une IP fraîche ; le blocage
restant est la **capacité** (une IP directe + une IP WARP), la **session**
(stable vs par appel, non tranchée) et l'**identité hybride** (corps 2.2.5,
UA 1.19.5).

## Correctifs de cette session

- `properties` non vide sur les 13 placeholders.
- Garde `<env>` avec sémantique du premier couple.
- Retrait de `tool_choice: "none"`.
- Plafond de cooldown 20 min appliqué à la **station** (pas seulement au ledger).

## État vérifié

- Proxy PID 16200, port 4000, mode `socks5`, **trois stations** :
  WARP IPv4 `104.28.230.202`, WARP IPv6 `2a09:bac5:32d4:be::/64`, pont direct
  `37.69.230.71`.
- Rotation observée `IPv6 → direct → IPv4`, **6 × 200** ; stream 200.
- Implémentation : `docs/IPV6_WARP_STATION_2026-11-06.md`.
- Suite ciblée → exit 0.
- Aucun commit : voir `T-405`.
