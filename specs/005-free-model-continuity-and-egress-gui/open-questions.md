# Questions ouvertes — à trancher avant implémentation

**Feature**: `005-free-model-continuity-and-egress-gui`  
Chaque question a une **proposition** ; l'opérateur tranche.

## Q-01 — Persister automatiquement le default target ?

**Contexte** : `apply_discovered_free_models` ne fait qu'un warning et un
fallback local.

**Proposition** : `free_discovery.auto_default_target: true` par défaut ;
persister seulement si le target est absent/`dead` et qu'un `alive` existe.

**Impact** : sinon le target reste stale et les scripts/GUI divergent.

## Q-02 — Sémantique du pin

**Proposition** : `pinned_target` non vide = aucune substitution automatique ;
la GUI affiche `pinned` et un bouton « revenir en auto ».

**Alternative** : pin = préférence, remplacé si `dead`.

## Q-03 — Politique de remplacement

**Proposition** : `alive` uniquement, priorité même famille, puis
`free_discovery.preference`, puis probe le plus récent.

**Alternative** : accepter `limited` en dernier recours (risque de 429).

## Q-04 — Provider WARP par défaut

**Proposition** : `caomingjun/warp` (4 `/64` prouvés) ; `warp-minimal` en
option.

**Alternative** : `warp-minimal` (MASQUE, plus léger, registration throttlée
aujourd'hui).

## Q-05 — Plafond de stations

**Proposition** : WARP 1–10 (4 prouvées, au-delà à valider) ; VPN 1–6 avec
limite de sessions mesurée ; refus explicite au-delà, pas de clamp.

## Q-06 — Bascule bleu/vert ou stop/start ?

**Proposition** : bleu/vert — créer/vérifier les nouvelles, puis drainer les
anciennes. Les anciennes restent actives pendant la vérification.

**Alternative** : stop/start simple, plus rapide mais coupe le service.

## Q-07 — Emplacement GUI

**Proposition** : nouvelle carte `FreeEgress` dans la page `FreeModels`
(ou page dédiée), pour ne pas mélanger avec le mode VPN existant.

**Alternative** : étendre `VpnStatus` avec un 4ᵉ mode `warp`.

## Q-08 — Contenu synthétique du `<env>`

**Proposition** : minimal
`<env>\n  Working directory: /workspace/<label>\n</env>`, aucun autre champ.
Le gate ne requiert que `Working directory:` ; les autres champs sont des
fuites.

**Alternative** : garder `Platform`/`Workspace root` mais synthétiques par
station.

## Q-09 — UA/TLS par station

**Proposition** : garder le profil vérifié unique (1.19.5) ; documenter que
JA3/UA sont partagés par conception. Ne pas inventer des versions non
vérifiées.

**Alternative** : introduire plusieurs profils vérifiés et les répartir par
station (plus de travail de capture).

## Q-10 — Failover d'une conversation

**Proposition** : la conversation reste sur sa station ; failover seulement si
la station est morte/429 persistante. Nouvelle identité, événement journalisé.
Ne jamais envoyer le même historique en parallèle à deux stations.

**Alternative** : re-pin immédiat sur une nouvelle station (plus disponible,
mais corrélation possible).

## Q-11 — WARP IPv4 partagé

**Proposition** : le garder comme un bloc séparé, mais le placer en dernier
dans la rotation (headroom partagé). Les quatre `/64` sont prioritaires.

**Alternative** : le retirer pour ne garder que des blocs dédiés.

## Q-12 — Qui exécute le provisioning ?

**Proposition** : le processus proxy (contrôleur) via un `DockerRunner`
injectable, comme `_apply_station_count` aujourd'hui.

**Alternative** : un helper séparé (`scripts/provision_free_egress.py`) appelé
par l'API ; plus facile à tester, moins intégré.

## Q-13 — Proxies `socks5` manuels

**Proposition** : bloc `managed: true` séparé ; les entrées manuelles restent
intactes et visibles en « avancé ».

## Q-14 — Modes `socks5`/`direct` dans la GUI

**Proposition** : les garder en « avancé », mais l'UX principale ne propose
que `VPN` et `WARP`.

## Q-15 — Audit identité : bloquant ou avertissement ?

**Proposition** : bloquant pour une station dupliquée/fuyante (exclue du pool) ;
avertissement visible dans la GUI. Ne pas démarrer en multi-station si l'audit
global échoue.

## Q-16 — Où stocker le secret d'identité ?

**Proposition** : `logs/identity_secret` (32 octets, 0600, gitignoré), jamais
envoyé, jamais loggé. Sauvegarde nécessaire pour la stabilité des projects.

## Q-17 — Combien de profils vérifiés et quel TTL ?

**Contexte** : il n'existe aujourd'hui que deux familles TLS vérifiées
(Bun 1.3.14 `d871…`, Bun 1.4.4 `1523…`). Vérifier 4–5 profils donne de la
diversité UA/client/corps, pas une diversité TLS supplémentaire.

**Proposition** : ne garder que les **deux dernières versions** (2.2.5 `cli`
et 1.19.5 `desktop`) et les futures `>= 1.19.5` ; 1.19.3/1.18.31 sont retirés
du runtime. TTL 7 jours ou nouvelle release client, re-vérification avant
assignation.

## Q-18 — Que faire si les stations dépassent les profils vérifiés ?

**Proposition** : assigner distinct d'abord ; si `M < N`, réutiliser un profil
et marquer `shared_profile` dans l'audit. Préférer une flotte complète avec un
résidu visible à une flotte incomplète. Ne jamais bloquer une station free
faute de profil.

## Récapitulatif des défauts proposés

Tant que l'opérateur n'a pas confirmé, ces défauts servent uniquement à garder
la conception cohérente. **Aucune implémentation n'est lancée** et chaque
choix reste réversible avant P0.

| Q | Défaut proposé | Réversible | Phase concernée |
|---|---|---|---|
| Q-01 | `auto_default_target: true`, écriture seulement si target absent/`dead` | oui (config) | P1 |
| Q-02 | `pinned_target` souverain ; `dead` → `pinned_unavailable` | oui (config) | P1 |
| Q-03 | `alive` uniquement ; `limited`/`unproven` jamais auto | oui (politique) | P1 |
| Q-04 | `caomingjun/warp` par défaut ; `warp-minimal` optionnel | oui (provider) | P3 |
| Q-05 | WARP 1–10, VPN 1–6 ; refus explicite, pas de clamp | oui (limites) | P3 |
| Q-06 | bleu/vert : vérifier les nouvelles avant de drainer | oui (stratégie) | P3 |
| Q-07 | nouvelle carte `FreeEgress` ; `VpnStatus` reste pour l'avancé | oui (UI) | P4 |
| Q-08 | `<env>` minimal : `Working directory: /workspace/<label>` | oui (template) | P0 |
| Q-09 | conserver le profil UA/TLS vérifié unique, documenter le partage | oui (profil) | P0 |
| Q-10 | conversation sticky ; failover seulement si station morte | oui (politique) | P0 |
| Q-11 | WARP IPv4 conservé comme bloc séparé, dernier dans la rotation | oui (ordre) | P3 |
| Q-12 | contrôleur dans le proxy + `DockerRunner` injectable | oui (architecture) | P3 |
| Q-13 | bloc `managed` séparé ; proxies manuels intacts | oui (config) | P3 |
| Q-14 | `socks5`/`direct` en section avancée | oui (UI) | P4 |
| Q-15 | audit bloquant pour station dupliquée/fuyante | difficile (sécurité) | P0 |
| Q-16 | `logs/identity_secret` 32 octets, 0600, gitignoré, sauvegardé | oui (emplacement) | P0 |
| Q-17 | deux dernières versions (2.2.5, 1.19.5) + futures ; anciennes retirées ; TTL 7 j | oui (registre) | P0.5 |
| Q-18 | distinct d'abord, réutilisation marquée `shared_profile` si `M < N` | oui (politique) | P0.5 |
