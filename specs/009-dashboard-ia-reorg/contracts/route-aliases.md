# Contract — Route Aliases

## Table

| Ancien hash | Nouveau | Défaut |
|---|---|---|
| `#config` | `#config/server` | oui |
| `#free-models` | `#free-models/overview` | oui |
| `#vpn` | `#free-models/egress` | oui |
| `#config/models` | `#config/models` | direct |
| `#config/keys` | `#config/keys` | direct |
| `#free-models/egress` | inchangé | direct |

## Règles

1. Un alias est résolu avant le rendu ; aucun flash de page incorrecte.
2. Un hash inconnu tombe sur le défaut du groupe concerné.
3. Un hash d'un groupe inconnu tombe sur le dashboard par défaut.
4. Les alias sont documentés et testés un par un.
5. Les liens profonds sont partageables et stables.
6. Aucune redirection serveur : tout se passe côté client.

## Tests

- ouvrir chaque ancien hash → bon sous-onglet ;
- ouvrir un hash inconnu → défaut, pas d'erreur ;
- copier un lien de sous-onglet → même état au reload ;
- navigation arrière/avant du navigateur → sous-onglet correct.
