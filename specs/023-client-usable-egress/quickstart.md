# Quickstart — 023-client-usable-egress

## 0. Prérequis

> Les scripts cités (client_smoke.py, wire_ab.py, egress_state.py,
> egress_toggle.py, soak_zero_429.py, capacity_probe.py) sont livrés par
> les tâches T014, T026, T067 et T073 ; avant leur livraison, utiliser les
> commandes de test unitaire correspondantes.

- Branche de feature : `023-client-usable-egress` (actuellement le dépôt est sur
  `019-audit-optimisation-flotte-free` ; le changement de branche est une action
  opérateur).
- `.specify/feature.json` pointe sur `specs/023-client-usable-egress`.
- Python 3.12+ ; dépendances installées.
- Un bucket prouvé disponible, ou une IP neuve pour un test A/B.

## 1. Gate locale (avant toute mesure live)

```powershell
python -m pytest -k "not docker" -q --no-header -p no:cacheprovider
python scripts/config_audit.py --strict
python scripts/platform_audit.py --strict
python -m pytest tests/test_option_map.py -q
python -m ruff check .
```

Attendu : 0 échec, 0 option morte, 0 violation, ruff propre.

## 2. Vérifier la nomenclature et les invariants

```powershell
python -m pytest tests/unit/test_ip_state.py tests/unit/test_classify_429.py -q
python -m pytest tests/unit/test_wire_profile.py tests/unit/test_wire_admission.py -q
```

Attendu : `burned` ⇒ zéro sélection ; `compat` ⇒ zéro egress prouvé ;
`spent` ⇒ aucune sélection avant `reset_at`.

## 3. Tester un client (format natif)

Claude Code, forme réelle (stream, outils, `system` en liste) :

```powershell
python scripts/client_smoke.py --client claude-code --stream --fresh-ip
```

Attendu : 3/3 `200` avec contenu, `tool_use` re-mappé, `message_stop` présent.

Pour les autres clients :

```powershell
python scripts/client_smoke.py --client codex --stream --fresh-ip
python scripts/client_smoke.py --client kilo-code --stream --fresh-ip
python scripts/client_smoke.py --client hermes-agent --stream --fresh-ip
python scripts/client_smoke.py --client deepseek --fresh-ip
python scripts/client_smoke.py --client aider --fresh-ip
```

Un client inconnu doit tomber dans `custom` et renvoyer un 422 explicite si le
corps n'est pas convertible — jamais un egress.

## 4. Protocole A/B de wire (obligatoire)

Règle : **une IP neuve par variante** (constitution XVIII).

```powershell
python scripts/wire_ab.py --variants official,convert --samples 20 --fresh-ip-per-variant
```

Critères :

- rapport horodaté dans `docs/evidence/wire-ab-*.json` ;
- aucune variante ne régresse par rapport au baseline ;
- une IP utilisée n'est jamais réutilisée pour l'autre variante.

## 5. Providers

```powershell
python scripts/egress_toggle.py --provider nordvpn --disable
python scripts/egress_state.py --provider nordvpn --wait 60
```

Attendu : plus aucune sélection du provider en < 60 s, état `disabled` visible,
aucune sonde d'entretien après `drain_s`.

Répéter pour `warp`, `proxies`, `direct`. Vérifier qu'aucune désactivation ne
change le `wire_fp` observé.

## 6. Rotation sûre

```powershell
docker inspect --format '{{.Name}}|{{.State.StartedAt}}' (docker ps -aq --filter name=opencode-vpn)
python scripts/egress_state.py --provider nordvpn --expect-api-rotation
```

Attendu : IP changée, `StartedAt` inchangé, une seule chaîne par station,
≤ 6 rotations/h, `evidence_ref` présent pour chaque libération.

## 7. Soak 24 h

```powershell
python scripts/soak_zero_429.py --hours 24 --require-reserve 1
```

Critères de sortie : SC-001 (0 429 client si réserve ≥ 1), SC-002 (0 libération
sans preuve), SC-003 (rotation bornée), SC-004 (0 429 non classé).

## 8. Archiver les preuves

- `docs/evidence/` : rapports A/B, états, incidents, soak.
- `CHANGELOG.md` : entrée datée avec les mesures.
- `specs/023-client-usable-egress/tasks.md` : cocher avec la preuve.
