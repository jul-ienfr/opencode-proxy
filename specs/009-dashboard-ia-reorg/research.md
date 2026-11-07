# Research — Réorganisation dashboard

**Date**: 2026-11-07
**But**: choisir une structure d'information qui réduit le scroll et la
recherche, sans casser les réglages ni les liens existants.

## D1 — Sous-onglets plutôt qu'accordéon

**Decision**: utiliser des sous-onglets horizontaux par page, pas un accordéon
géant.

**Rationale**: un accordéon garde tous les blocs dans le DOM et le scroll ;
les sous-onglets isolent les domaines et permettent le lazy loading. Ils
correspondent aussi à la navigation déjà familière (onglets principaux).

**Alternatives**:
- Accordéon : simple, mais page toujours longue.
- Colonne latérale de sections : prend de la place, moins lisible sur mobile.
- Pages séparées : trop de navigation, perd le contexte.

## D2 — Découpage de « Modèles Gratuits »

**Decision**: 5 sous-onglets :

1. **Vue d'ensemble** : inventaire, counts, policy.
2. **Egress & providers** : mode/checkboxes, VpnStatus, allocator, rotation.
3. **Routage free** : VpnRouting, free attempts, strict/fallback.
4. **Qualité & probes** : quality rotation, latency, timeouts, post-rotation.
5. **Avancé** : reste de VpnAdvanced (WARP-only, HTTPS, diagnostics).

**Rationale**: sépare le « quoi » (modèles), le « par où » (egress), le
« comment » (routage), la « qualité » et les cas avancés.

**Alternatives**:
- 3 onglets : Egress trop chargé.
- 7 onglets : trop fragmenté.
- Garder tout : le problème actuel.

## D3 — Découpage de « Configuration »

**Decision**: 5 sous-onglets :

1. **Serveur** : port, bind, restart, debug.
2. **Modèles & routage** : mapping, routes custom, catalogue.
3. **Clés & accès** : API keys, token.
4. **Outils web** : web search, web fetch.
5. **Maintenance** : updates, logs, backups.

**Rationale**: regroupe par domaine fonctionnel et par fréquence d'usage ;
« Serveur » est le défaut, « Maintenance » est rare.

**Alternatives**:
- Un onglet par carte : trop nombreux.
- Garder la page unique : 44 Ko, scroll interminable.

## D4 — URL comme source de vérité du sous-onglet

**Decision**: `#free-models/<tab>` et `#config/<tab>` ; le sous-onglet actif
est lu depuis l'URL, avec fallback `localStorage` puis défaut.

**Rationale**: deep links partageables, reload stable, pas d'état caché. Le
routeur hash existant suffit.

**Alternatives**:
- localStorage seul : liens non partageables.
- État Svelte seul : perdu au reload.
- Routes serveur : inutile pour un dashboard statique.

## D5 — Aliases des anciens hashes

**Decision**: table d'alias `#config` → `#config/server`, `#free-models` →
`#free-models/overview`, `#vpn` → `#free-models/egress`, hash inconnu →
défaut.

**Rationale**: ne pas casser les favoris/liens existants ; migration douce.

**Alternatives**:
- Redirection 404 : mauvaise UX.
- Ignorer : liens cassés.

## D6 — Lazy loading des composants lourds

**Decision**: charger `VpnAdvanced`, `VpnServers`, `Config`-Models et
`Config`-Web à la demande (dynamic import).

**Rationale**: le premier rendu ne doit pas payer tous les composants ; le
budget est +20 % maximum.

**Alternatives**:
- Tout charger : premier rendu lourd.
- Tout découper : sur-ingénierie.

## D7 — Recherche de réglage

**Decision**: barre de recherche dans chaque page ; un résultat ouvre le
sous-onglet et met en évidence le champ (ancre + flash).

**Rationale**: la réorganisation doit réduire le temps de recherche, pas
seulement le scroll.

**Alternatives**:
- Recherche globale uniquement : moins contextuelle.
- Pas de recherche : l'opérateur continue à fouiller.

## D8 — Dirty guard

**Decision**: si un formulaire est modifié, quitter le sous-onglet demande
confirmation (sauvegarder / annuler / quitter).

**Rationale**: éviter de perdre une config à cause d'un clic.

**Alternatives**:
- Sauvegarde automatique : risquée pour des réglages critiques.
- Aucun guard : perte silencieuse.

## D9 — Un réglage, un seul emplacement

**Decision**: matrice `SettingOwnership` : chaque clé de config apparaît dans
un seul sous-onglet. Les doublons sont supprimés ou marqués « affichage seul ».

**Rationale**: la duplication est la cause principale du « bazar » et des
valeurs contradictoires.

**Alternatives**:
- Dupliquer pour convenience : source de vérité multiple.
- Tout mettre dans « Avancé » : cache le problème.

## D10 — Accessibilité et responsive

**Decision**: `role=tablist/tab/tabpanel`, `aria-selected`, focus visible,
flèches gauche/droite, scroll horizontal à 375 px, libellés FR/EN.

**Rationale**: la réorganisation ne doit pas créer de nouvelle dette ; la
constitution XII et les tests UI l'exigent.

**Alternatives**:
- Divs cliquables : inaccessible.
- Menu déroulant sur mobile : acceptable mais moins direct ; le scroll
  horizontal est préféré.

## D11 — Aucun changement backend

**Decision**: ne toucher ni aux API, ni aux clés de config, ni aux
consommateurs runtime.

**Rationale**: réduire le risque ; la feature est une réorganisation d'UI.

**Alternatives**:
- Renommer les clés : migration inutile et risquée.
- Changer les endpoints : casse les tests/clients.

## D12 — Tests et data-testid

**Decision**: `data-testid` stables par sous-onglet et par champ critique ;
tests de route, de sauvegarde, d'accessibilité et de responsive.

**Rationale**: la réorganisation déplace les éléments ; les tests doivent
suivre sans dépendre de la position visuelle.
