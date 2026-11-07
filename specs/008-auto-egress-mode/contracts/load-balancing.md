# Contract — Fallback vs Répartition

## Deux modes

```text
strategy = fallback  → ordre seul, pas de poids
strategy = balance   → poids 0–100, somme 100 %
```

Le mode est un choix explicite du GUI, persisté dans
`ip_rotation.egress_strategy`.

## Configuration

```yaml
ip_rotation:
  egress_providers: [nordvpn, warp, proxies]
  egress_strategy: fallback        # fallback | balance
  egress_weights:
    nordvpn: 50
    warp: 30
    proxies: 20
```

Règles :

- `egress_providers` garde l'ordre de priorité (↑/↓).
- `direct` peut figurer avec les autres providers : il est traité comme un
  egress à part entière (IP de la machine), avec un avertissement GUI.
- En `fallback`, `egress_weights` est ignoré (100/0/0 implicite).
- En `balance`, les poids des providers cochés sont normalisés à 100.
- Poids 0 = secours, pas désactivé.
- Un provider décoché est retiré du calcul ; son poids est conservé pour
  lorsqu'il sera recoché.
- Un provider down/quota est retiré du tirage ; les poids restants sont
  renormalisés à 100 pour cette décision.

## GUI

### Segmented control

```text
[ Fallback / Priorité ]   [ Répartition / Charge ]
```

### Fallback

- Liste ordonnée avec numéro (1er, 2e, 3e, 4e).
- Flèches ↑/↓ pour changer l'ordre.
- Aucun slider.
- Le premier provider sert ; les suivants sont des secours.

### Répartition

- Slider 0–100 par provider coché.
- Total affiché en temps réel : `100 %`.
- Barre empilée colorée (aperçu).
- Boutons `Tout équilibrer` et `Priorité au premier`.
- Flèches ↑/↓ visibles pour le failover/tie-break.
- Un provider à 0 affiche le badge `Secours`.

## Runtime

- Fallback : sélection dans l'ordre, sans tirage.
- Répartition : smooth weighted round-robin déterministe.
- Un provider à poids 0 n'est choisi que si tous les poids > 0 sont
  indisponibles.
- Le choix est journalisé (`provider`, `weight`, `reason`).

## Tests

- Fallback : 100 requêtes → 100 % sur le premier provider sain ; 0 sur les
  suivants.
- Répartition 2 providers 50/50 → ±10 points.
- Répartition 3 providers 33/33/33 → ±10 points.
- Répartition 4 providers 25/25/25/25 → ±10 points.
- Poids 0 → 0 requête tant qu'un provider > 0 est sain.
- Provider down → poids renormalisés, pas de division par zéro.
- Changement de mode → drain, pas d'interruption.
- GUI : total 100 %, presets, barre empilée, persistance.
