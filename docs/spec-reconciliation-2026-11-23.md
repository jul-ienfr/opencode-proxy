# Réconciliation des tâches SpecKit — 2026-11-23 (T228 / spec 024)

**Décision** : la spec `024-audit-optimisation-globale` est la liste de travail
courante. Les specs antérieures restent comme historique ; leurs cases `[ ]`
ne sont **pas** des travaux à reprendre telles quelles.

## Règle appliquée

1. Une tâche `[x]` d'une ancienne spec reste une preuve historique valide.
2. Une tâche `[ ]` d'une ancienne spec est **supersédée** dès qu'elle est
   couverte par une tâche de 024 (ou de 023 pour le trafic free). Le bandeau
   ajouté en tête de chaque `tasks.md` renvoie vers 024.
3. Aucune tâche n'est supprimée : la traçabilité `git log` reste intacte.

## Inventaire au moment de la réconciliation

| Spec | Cases ouvertes | Statut |
|---|---:|---|
| `001-free-station-availability` | 36 | supersédée par 024 (mesure/SLO) |
| `002-vpn-catalog-rotation` | 6 | supersédée par 024 |
| `003-official-client-parity` | 65 | supersédée par 023/024 |
| `004-repo-parity-audit` | 17 | supersédée par 024 |
| `005-free-model-continuity-and-egress-gui` | 28 | supersédée par 023/024 |
| `007-warp-egress-fallback` | 42 | supersédée par 024 |
| `008-auto-egress-mode` | 56 | supersédée par 024 |
| `009-dashboard-ia-reorg` | 3 | supersédée par 024 |
| `011-station-fleet-usability` | 79 | supersédée par 024 |
| `012-request-reliability` | 2 | supersédée par 024 |
| `014-nordvpn-exhaustive-rotation` | 31 | supersédée par 024 |
| `016-gluetun-control-plane` | 11 | supersédée par 024 |
| `023-client-usable-egress` | 4 | **reste la référence free** |

Total ≈ 380 cases encore `[ ]` dans les anciennes specs ; 024 porte la liste
active (voir son `tasks.md`).

## Ce qui n'est pas clôturé par cette réconciliation

- Les décisions opérateur D01–D06 de 024 restent ouvertes.
- Les fenêtres d'exploitation live (24 h, 30 min 6/6) restent à mesurer.
- `023` garde ses 4 cases : elles concernent le trafic free et restent la
  référence fonctionnelle jusqu'à clôture de 024.
