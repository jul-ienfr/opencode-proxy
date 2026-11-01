# Spike — binding d’un serveur NordVPN précis

Date : 2026-11-01  
Station test : 1 (`opencode-vpn`)  
But : prouver qu’une station peut être reconnectée au **même serveur** et
retrouver le **même egress**, condition indispensable au classement des IP.

## Méthode

1. Serveur cible : `al110.nordvpn.com` (Albanie).
2. Override Compose temporaire :

```yaml
services:
  vpn-gluetun:
    environment:
      - SERVER_HOSTNAMES=al110.nordvpn.com
```

3. Recréation de la station :

```bash
docker compose -f docker-compose.yml -f docker-compose.spike.yml \
  up -d --force-recreate --no-deps vpn-gluetun
```

4. Mesure de l’egress via le SOCKS5 de la station
   (`socks5h://127.0.0.1:1080`, puis `socks5://`).
5. Répétition de la recréation avec le même override.

## Résultats

| Essai | Serveur | Egress observé | SOCKS5 |
|---|---|---|---|
| 1 | `al110.nordvpn.com` | `186.247.44.61` | 200 |
| 2 | `al110.nordvpn.com` | `186.247.44.61` | 200 |

**L’egress est identique sur deux recréations complètes.** Le binding par
`SERVER_HOSTNAMES` + recréation est donc **reproductible**.

### Test complémentaire — control API

- `PUT /v1/vpn/settings` avec
  `{"provider":{"server_selection":{"countries":["Albania"],
  "hostnames":["al110.nordvpn.com"]}}}` fonctionne : première connexion sur
  `186.247.44.61`.
- Un second `PUT` avec les **mêmes** réglages retourne
  `settings left unchanged` : pas de reconnexion.
- `PUT /v1/vpn/status` `{"status":"stopped"}` retourne `rc=8` et laisse la
  station bloquée en `stopping` ; la station a dû être recréée.

## Décision

1. **Binder principal : Compose hostname.**
   - `SERVER_HOSTNAMES_STATIONn=<hostname>`
   - recréation du conteneur de la station
   - vérification de l’egress après connexion.
2. **Control API : fast-path uniquement quand le hostname change.**
   - jamais utilisé pour « reconnecter le même serveur » ;
   - fallback si le hostname est déjà le bon : recréation.
3. **Custom WireGuard** : option de secours si un fournisseur ne permet pas
   `SERVER_HOSTNAMES`.

## Conséquences pour le plan

- Le classement des IP peut être **persistant** : on stocke `hostname` et
  `egress_ip`, et on retrouve le même egress après recréation.
- L’allocateur peut donc réassigner une IP connue à une station.
- Le binder doit toujours vérifier l’egress réel après connexion : une IP NAT
  peut différer du `station` du catalogue.
- Le mode `control_api` reste utile pour un changement rapide de serveur, mais
  pas pour une reconnexion à l’identique.

## Artefacts

- Override temporaire supprimé après le spike.
- Station 1 restaurée avec la configuration Compose normale (Albanie).
- Commit : à compléter avec ce rapport.
