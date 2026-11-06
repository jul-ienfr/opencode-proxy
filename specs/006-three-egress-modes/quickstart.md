# Quickstart — Trois modes d'egress

**Feature**: `006-three-egress-modes` | **Statut**: planification

Ce guide décrit la validation attendue après implémentation. Il ne modifie
rien aujourd'hui.

## Prérequis

- Proxy démarré sur `http://127.0.0.1:4000`.
- Dashboard accessible.
- Au moins un endpoint WARP activé pour le scénario WARP.
- Docker/gluetun disponible pour le scénario NordVPN.

## 1. Vérifier le mapping des modes

```bash
curl -s http://127.0.0.1:4000/api/vpn-config | jq '.proxy_mode'
# vpn | socks5 | direct
```

GUI :

1. ouvrir Free Models ;
2. vérifier les trois boutons **NordVPN**, **WARP**, **Direct** ;
3. vérifier qu'aucun libellé « VPN (OpenVPN) » ou « SOCKS5 » ne sert de nom de
   mode.

## 2. Vérifier la persistance et l'isolation

1. sélectionner **NordVPN** ;
2. vérifier la présence de la stack, de la sélection serveur, des identifiants
   et du nombre de stations ;
3. sélectionner **WARP** ;
4. vérifier la disparition des sections NordVPN-only et l'apparition de la
   liste WARP ;
5. recharger la page : WARP doit rester actif ;
6. vérifier qu'aucun PATCH automatique n'a été envoyé pour les réglages
   NordVPN.

## 3. Vérifier les paramètres communs

En mode **WARP**, modifier :

- `free_parallel.mode` → `strict` ;
- `on_429_action` → `both` ;
- `station_bad_ttl_s` → `30` ;
- `free_attempts` → `3` ;
- `quality_rotation.enabled` → `true`.

Puis :

```bash
curl -s http://127.0.0.1:4000/api/vpn-config | jq '{
  proxy_mode,
  free_parallel,
  on_429_action,
  station_bad_ttl_s,
  max_free_attempts,
  quality_rotation
}'
```

Attendu : les valeurs sont persistées dans `ip_rotation`, pas dupliquées dans
un bloc WARP.

Basculer en **NordVPN** : les mêmes valeurs doivent être affichées et lues par
le backend Docker. Revenir en WARP : elles doivent être inchangées.

## 4. Vérifier la capacité par mode

```bash
curl -s http://127.0.0.1:4000/health | jq '.status, .free_capacity'
```

Attendu :

- WARP 7/7 → `configured_stations=7`, `usable_stations=7`,
  `capacity_degraded=false` ;
- WARP dégradé → `capacity_reason="x/7 endpoints usable"` ;
- NordVPN → capacité des stations Docker ;
- Direct → aucune capacité de station, pas de faux `0/1`.

## 5. Vérifier le test WARP

Dans la section WARP :

1. vérifier que chaque ligne affiche label, host:port, egress, bloc ;
2. cliquer **Test** sur `warp-ipv6-2` ;
3. attendre `✓ <IPv6> — N ms` ;
4. vérifier qu'aucune erreur console n'apparaît.

## 6. Non-régression

```bash
python -m pytest -k "not docker" -q
npm --prefix dashboard-ui run build
```

Attendu : suite verte, build Svelte sans erreur, aucune fenêtre de console
Windows pendant les rotations.

## Critères de sortie

- trois modes exacts et persistants ;
- sections spécifiques isolées ;
- paramètres communs effectifs dans les deux backends ;
- capacité par mode honnête ;
- tests backend + build frontend verts.
