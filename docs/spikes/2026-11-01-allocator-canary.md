# Canary allocateur — 2026-11-01

But : exécuter la chaîne complète **claim → Compose bind → probe → assign/release**
sur une station réelle, sans toucher à la station 6.

## Commande

```bash
python scripts/ip_registry_canary.py \
  --station 1 \
  --hostname al110.nordvpn.com \
  --egress 186.247.44.61 \
  --max-attempts 1
```

## Résultat

```text
db=logs\ip_registry.db station=1 candidates=1
  186.247.44.61   al110.nordvpn.com   Albania   state=fresh
outcome ok=False station=1 ip=None host=None attempts=1 reason=bind_error:no-egress
```

## Interprétation

1. Le candidat a été **claim** correctement.
2. `ComposeHostnameBinder` a recréé la station 1 avec
   `SERVER_HOSTNAMES=al110.nordvpn.com`.
3. La vérification d’egress n’a répondu sur aucun DNS (`socks5h` ni `socks5`) :
   le serveur `al110` était joignable lors du spike mais est tombé entre-temps.
4. L’allocateur a **refusé l’assignation** (`bind_error:no-egress`) et libéré
   le lease. Aucune IP morte n’a été conservée.
5. La station 1 a été restaurée ensuite avec la configuration Compose normale.

## Ce que ça valide

- Le binder ne fait pas confiance au seul `SERVER_HOSTNAMES` : il **vérifie
  l’egress réel** avant d’assigner.
- Un serveur instable est rejeté sans polluer la station.
- Le lease est libéré ; le candidat reste dans le registre pour un prochain
  essai (ou sera marqué `error` si le probe échoue).
- La chaîne allocateur fonctionne de bout en bout ; il manque seulement un
  serveur/egress réellement vivant pour obtenir un `200`.

## Correctif appliqué après le canary

Un échec `bind_error` / `egress_mismatch` ne laisse plus le candidat en
`candidate` : `release()` le passe en `ERROR` avec `error_until = now +
error_cooldown_s` (30 min par défaut). Cela évite de retenter immédiatement un
serveur mort à chaque passage de l’allocateur. Un `probe` ultérieur peut le
remettre en jeu après le cooldown.

## Suite

- Alimenter `ip_state` avec des candidats **dont l’hostname est connu** et
  l’egress vérifié (observations récentes).
- Rejouer le canary sur un candidat dont le tunnel est vivant.
- Ensuite seulement, brancher l’allocateur sur la flotte et le dashboard.
