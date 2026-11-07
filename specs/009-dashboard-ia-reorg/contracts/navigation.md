# Contract — Navigation

## Structure

```text
#free-models/overview    → Vue d'ensemble
#free-models/egress      → Egress & providers
#free-models/routing     → Routage free
#free-models/quality     → Qualité & probes
#free-models/advanced    → Avancé

#config/server           → Serveur
#config/models           → Modèles & routage
#config/keys             → Clés & accès
#config/web              → Outils web
#config/maintenance      → Maintenance
```

## Comportement

1. Le sous-onglet actif est lu depuis l'URL (`hash`).
2. Si absent, `localStorage` (`dashboard.tab.<group>`) ; sinon défaut.
3. Un clic met à jour l'URL sans rechargement complet.
4. Un reload restaure le sous-onglet.
5. Un hash inconnu → défaut, sans erreur.
6. Les composants `lazy=true` sont chargés à la demande.
7. Le changement de sous-onglet ne perd aucun réglage non sauvegardé
   (dirty guard).

## Accessibilité

- `role="tablist"` sur la barre, `role="tab"` sur chaque onglet,
  `role="tabpanel"` sur le contenu.
- `aria-selected`, `aria-controls`, `aria-labelledby`.
- Flèches gauche/droite, Home/End, Entrée/Espace.
- Focus visible.
- `data-testid="tab-<id>"`.

## Responsive

- ≥ 768 px : barre horizontale.
- < 768 px : scroll horizontal avec indicateur, pas de wrap en plusieurs
  lignes.
- 375 px : tous les onglets atteignables.

## i18n

- Libellés FR/EN dans `locales.ts`.
- Aucun texte en dur dans les composants.
