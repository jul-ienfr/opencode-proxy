# ADR-011 — Rester Python, découper d'abord ; Go seulement pour un module mesuré

**Date** : 2026-11-21
**Statut** : Accepted
**Feature** : `024-audit-optimisation-globale`
**Source** : [`specs/024-audit-optimisation-globale/architecture-review.md`](../../specs/024-audit-optimisation-globale/architecture-review.md)
**Constitution** : IX, XIV, XVII

## Contexte

La question « faut-il porter le proxy en Go ou en Rust ? » a été instruite sur
des mesures (audit du 2026-11-20), pas sur une intuition. Trois faits dominent :

1. **Le plancher Python est 14 ms** sur une médiane bout en bout de
   **1 203 ms**, soit **1,2 %**. Le temps restant est la génération du modèle
   distant et les attentes de retry (11,5 s / 19,7 s sur 429/503).
2. **Le projet est déjà polyglotte** : le fork Gluetun est en Go
   (**110 580 lignes**), soit plus que tout le Python de production hors
   `opencode.py`. Go tient déjà le composant le plus critique (le tunnel).
3. **Le coût d'un portage complet est ~250 000 lignes** (143 000 de production
   + 107 377 de tests) avec un bloqueur non négociable : l'empreinte TLS
   exacte (`curl_cffi` / JA3 `1523504b38f0fae0d881d4b6554aac1b`, 281 usages).
   Une erreur d'empreinte **brûle une IP** (principe XVIII).

Les cinq P0 de l'audit (fonction définie après `__main__`, alias
`sys.modules`, `elif` dupliqué, deux schémas `ip_state`, overlays gitignorés)
sont des **défauts de logique**. Un portage les reproduirait à l'identique.

## Décision

1. **Ne pas porter l'ensemble du proxy.** Python reste le langage par défaut
   de l'app, du superviseur, du pool et du dashboard.
2. **Découper d'abord** les monolithes par strangler (Phase 2 : `opencode.py`
   L1–L17, `vpn/manager.py` 15 modules, `free/pool` 8 modules,
   `app/protocol/mapping.py`, `dashboard/api.py`, `config/settings.py`,
   `scripts/fresh_chain*`). Le gate de taille (T216, INV-10) empêche la
   régression.
3. **Porter un module en Go uniquement sur preuve** : après découpage, un
   module ne devient candidat que s'il est (a) appelé à chaque requête,
   (b) logiquement pur (tri/score/sélection), et (c) mesure un coût CPU
   significatif au profilage. Le candidat le plus probable est le chemin
   chaud de sélection de `free/pool` une fois découpé.
4. **Si portage, choisir Go, pas Rust** : Go est déjà le langage du fork
   (110 580 l.), son écosystème TLS (`utls`) est mature, et un troisième
   langage n'apporterait pas de gain mesuré.
5. **Aucun portage sans test de parité** entre le module Python et sa
   réécriture, sur les mêmes entrées, avec les mêmes sorties.

## Conséquences

- Les tâches de découpage (T210–T215) deviennent le prérequis de tout portage ;
  elles ne sont pas annulées par cette ADR.
- Le gate `scripts/module_size_audit.py --ratchet` (T216) est la borne
  objective : aucun module > 1 500 lignes ne peut grossir.
- Toute proposition de portage devra citer un profil CPU et un test de parité,
  pas une préférence de langage.
- La question « Go ou Rust ? » est tranchée : Go si un portage est prouvé,
  jamais Rust en troisième langage.

## Alternatives rejetées

| Alternative | Raison du rejet |
|---|---|
| Portage big-bang Python → Go | ~250 000 lignes pour 1,2 % de la médiane ; bloqueur JA3 ; reproduit les P0 |
| Portage Python → Rust | troisième langage, écosystème d'empreinte TLS moins mature, aucun gain mesuré sur Go |
| Micro-service Go pour tout le proxy | duplique l'app et le dashboard sans preuve de besoin |
| Statu quo sans découpage | laisse les monolithes grossir (interdit par XIV) et rend tout portage futur impossible |

## Suivi

- La revue d'architecture complète (graphe, mesures, architecture cible) reste
  [`architecture-review.md`](../../specs/024-audit-optimisation-globale/architecture-review.md).
- Toute révision de cette ADR exige une mesure qui contredit l'une des trois
  conditions de portage (débit > 1 000 req/s, module > 50 % CPU, latence
  médiane < 100 ms).
