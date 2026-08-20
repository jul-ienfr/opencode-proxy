# Cause racine — boucle de recréation compose 19/08 (09:34Z+)

## Symptôme
Flotte VPN en boucle : chaque station est `compose up --force-recreate` toutes les
30-60 s, boote `VPN_TYPE=openvpn`, l'auth OpenVPN échoue (stations NordVPN
défaillantes), le watchdog escalade → recompose → boucle sans fin.

## Preuves
- `printenv VPN_TYPE` conteneurs (recréés 09:36-09:39Z) → `openvpn`
- `.env` racine = `wireguard` depuis 09:33Z (sed d'exploitation, backup
  `logs/env_vpn_type_backup_20260819.txt`)
- `docker compose -f docker-compose.yml config` (sans env) → `VPN_TYPE: wireguard`
- `VPN_TYPE_STATION1=openvpn docker compose config` → `VPN_TYPE: openvpn`
  → l'ENV du parent priorise sur le fichier .env
- labels conteneurs : config_files = C:\...\opencode-proxy-main\docker-compose.yml
  (le BON compose)

## Mécanisme exact
`config/settings.py:97-98` — charge le .env dans `os.environ` au boot SI ABSENT.
Le process `pythonw opencode.py --gui` (PID 9304, boot 08:50Z) a donc chargé
`VPN_TYPE_STATION{1..6}=openvpn` dans son env. Chaque invocation compose (enfant)
HÉRITE de cet env → `docker compose` priorise l'env parent sur le fichier `.env`.
Mon sed a corrigé le FICHIER mais pas l'env du process vivant.

## Correctif appliqué
Redémarrage du process proxy (`taskkill /T` + relance `pythonw opencode.py --gui`
depuis la racine) → au boot, settings.py charge le .env wireguard → recréations WG.
