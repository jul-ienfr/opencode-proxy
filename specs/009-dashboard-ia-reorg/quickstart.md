# Quickstart — Réorganisation dashboard

**But**: vérifier que la réorganisation ne perd aucun réglage et améliore la
navigation.

## Prérequis

- Dashboard lancé sur `http://127.0.0.1:4000`.
- `npm --prefix dashboard-ui run dev` ou build servi.
- Navigateur desktop + mode responsive 375 px.

## 1. Modèles Gratuits

```text
#free-models/overview
#free-models/egress
#free-models/routing
#free-models/quality
#free-models/advanced
```

Vérifier :

- chaque sous-onglet s'ouvre ;
- l'URL change ;
- reload → même sous-onglet ;
- aucune section perdue ;
- aucun doublon de réglage.

## 2. Configuration

```text
#config/server
#config/models
#config/keys
#config/web
#config/maintenance
```

Vérifier :

- chaque carte est dans un seul sous-onglet ;
- les sauvegardes fonctionnent ;
- les valeurs persistent après changement de sous-onglet.

## 3. Anciens liens

Ouvrir :

```text
#config
#free-models
#vpn
#inconnu
```

Attendu : redirection vers le sous-onglet par défaut, aucune erreur.

## 4. Recherche et dirty guard

1. Rechercher « web search » → « Outils web » s'ouvre, champ mis en évidence.
2. Modifier un champ, changer de sous-onglet → confirmation.
3. Annuler → on reste ; sauvegarder → on navigue.

## 5. Accessibilité

- Tab/Shift+Tab : tous les sous-onglets atteignables.
- Flèches gauche/droite : changement de sous-onglet.
- Focus visible.
- `aria-selected` correct.

## 6. Responsive

- 375 px : sous-onglets scrollables, pas de scroll horizontal de page.
- 768 px : barre horizontale lisible.

## 7. Tests

```powershell
npm --prefix dashboard-ui run check
npm --prefix dashboard-ui run build
```

Attendu : 0 erreur, 0 warning bloquant.

## 8. Preuve

Consigner les captures/notes dans `docs/evidence/009-dashboard-ia.md` :

- avant/après (scroll, nombre de clics) ;
- anciens liens ;
- dirty guard ;
- responsive ;
- captures desktop/mobile.
