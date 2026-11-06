# Contract — Mode selector UI

**Feature**: `006-three-egress-modes`

## Modèle de mode

| Valeur API/config | Libellé FR | Libellé EN | Backend |
|---|---|---|---|
| `vpn` | NordVPN | NordVPN | Docker/gluetun |
| `socks5` | WARP | WARP | endpoints SOCKS5/WARP |
| `direct` | Direct | Direct | aucun tunnel |

- Les valeurs `vpn`, `socks5`, `direct` sont **stables**.
- Aucune valeur `warp` ou `nordvpn` n'est introduite dans `proxy_mode`.
- Le libellé est résolu par `free_models.mode_<id>` dans `locales.ts`.

## Sélecteur

- Exactement trois boutons, un seul `aria-checked="true"`.
- Ordre : NordVPN, WARP, Direct.
- Clic → `POST /api/vpn/proxy-mode` avec la valeur interne.
- Après succès : relecture de `/api/vpn-config` et `/api/vpn-status`.
- En cas d'échec : toast d'erreur, l'ancien mode reste affiché.
- Le changement ne redémarre pas le processus proxy.

## Sections conditionnelles

| Section | NordVPN | WARP | Direct |
|---|---|---|---|
| Stack/serveur/station_count | visible | masquée | masquée |
| Identifiants/serveurs/mode Docker | visible | masquée | masquée |
| Liste WARP + test/rotation | masquée | visible | masquée |
| HTTPS uplink NordVPN | visible | masquée | masquée |
| Routage free commun | visible | visible | visible + mention « non applicable » |
| Qualité/latence communes | visible | visible | visible + mention |
| Probe post-rotation WARP | masquée | visible | masquée |

Règles :

- Une section masquée n'envoie aucun PATCH automatique.
- Un réglage spécifique de l'ancien mode ne doit pas être appliqué après le
  changement.
- Les valeurs restent persistées même si la section est masquée.

## Tests d'acceptation

1. Les trois libellés sont exacts en FR et EN.
2. Le mode actif survit à un rechargement.
3. NordVPN ne montre pas la liste WARP ; WARP ne montre pas les identifiants
   NordVPN.
4. Direct affiche la mention « non applicable » sur le routage commun.
5. Aucun appel d'inférence n'est déclenché par un clic de mode.
