# Research — Fiabilité des requêtes free (2026-11-08)

## Sources

- `logs/requests.db` : 7 767 tentatives, 00:00–22:05 UTC.
- `logs/registry.db` : `rotation_jobs`, `station_state`, `incidents`.
- `/api/stats`, `/api/vpn-status`, `/api/free-health`.
- `logs/debug.log` : `AUTH_FAILED`, `model slot timeout`, rotations.
- Audit détaillé : `docs/AUDIT_REQUETES_2026-11-08.md`.

## Mesures

| Mesure | Valeur |
|---|---:|
| Tentatives | 7 767 |
| Succès | 3 618 (46,6 %) |
| `503` locaux | 2 647 |
| `429` finaux | 1 496 |
| Tentatives station | 13 642 |
| Succès station | 3 774 (27,7 %) |
| 429 station | 9 868 (72,3 %) |
| IP sans succès (≥10 essais) | 217 (4 591 essais) |
| Latence succès p50/p95 | 13,8 s / 50,4 s |
| `AUTH_FAILED` | 6 052 lignes |
| Rotations OK / KO | 616 / 154 |
| `model slot timeout` | 6 359 |

## Décisions

### D1 — Accepter `auto_egress` dans tous les chemins free

**Constat** : le chemin streaming utilise déjà `auto_egress` ; les chemins
non-stream et SystemOne testent seulement `vpn`/`socks5`, donc fail-close en
`503` alors que WARP est disponible.

**Décision** : un tuple `_FREE_POOL_MODES = ("vpn", "socks5", "auto_egress")`
partagé par les cinq points de garde. Le `503` reste uniquement si le pool est
vide.

**Alternatives rejetées** :
- Convertir `auto_egress` en `socks5` au démarrage : masque l'intention et
  casse le mode si le pool change.
- Faire un direct résidentiel : interdit par `enforce_vpn_only`.

### D2 — Un seul canari IPv4 WARP

**Constat** : `104.28.230.202` 622 essais/0 succès, `.200` 88/0, `.204` 67/0,
`.198.202` 55/0. Ces `/32` sont un pool NAT partagé, déjà à quota nul.

**Décision** : `WARP 1` IPv4 reste actif comme canari ; `WARP 2..6` IPv4 sont
désactivés. Les six `/64` IPv6 restent actifs. Aucune rotation IPv4.

**Alternative rejetée** : rotationner les IPv4 pour chercher un `/32` frais —
le pool est partagé et la rotation détruit le `/64` de la station.

### D3 — Parkage NordVPN avant réparation

**Constat** : 6 052 `AUTH_FAILED`, six stations `error`, credentials du
2026-11-07. Le token NordVPN répond 200 et les credentials sont dérivables
(`refresh_nord_credentials.py --check`).

**Décision** : régénérer les credentials de service (`--apply`), parquer les six
stations, valider une station avec un egress public, puis réactiver 1 → 6.

**Alternative rejetée** : laisser les six stations tenter et échouer — 6 000
lignes de bruit et des rotations inutiles.

### D4 — Distinguer quota journalier et 429 transitoire

**Constat** : les 1 496 `429` finaux portent tous `FreeUsageLimitError`. Le gel
anti-churn de 180 s empêche la rotation qui trouverait un `/64` frais.

**Décision** : bloquer `(modèle, IP)` jusqu'au reset pour un quota journalier ;
conserver le gel pour les 429 transitoires ; autoriser une rotation contrôlée
quand toute la flotte est épuisée.

### D5 — Réduire l'attente de slot

**Constat** : 6 359 timeouts de 3 s, fail-open. La protection ne protège pas et
ajoute de la latence.

**Décision** : `model_slot_wait_s` → 0,25 s. Une vraie file globale pourra
remplacer le fail-open en Phase 2.

## Contraintes free

- Pas de second compte, pas de payant, pas de direct résidentiel.
- Le reset fournisseur est une barrière dure.
- Le canari IPv4 ne doit pas être compté comme capacité.
- Le parkage NordVPN est réversible et ne supprime aucun conteneur.
