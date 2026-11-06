# Mesures — P0/P0.5, profils actifs 2.2.5/1.19.5 et archives

**Date** : 2026-11-06  
**Périmètre** : trois profils, identités de station P0, prompt templaté,
transport curl_cffi réel. Aucun paiement, aucun second compte.

## 1. Profils vérifiés

| Profil | Version / client | TLS | Corps | Outils | Statut |
|---|---|---|---|---:|---|
| `oc-2.2.5-cli-13` | `2.2.5` / `cli` | Bun 1.4.4 | `free/captures/2.2.5.json` | 13 | `verified` |
| `oc-1.19.5-desktop-11` | `1.19.5` / `desktop` | Bun 1.4.4 | `free/captures/1.19.5.json` | 11 | `verified` |
| `oc-1.19.3-desktop-10` | `1.19.3` / `desktop` | Bun 1.3.14 | `free/captures/1.19.3.json` | 10 | archive |
| `oc-1.18.31-desktop-10` | `1.18.31` / `desktop` | Bun 1.3.14 | `free/captures/1.18.31.json` | 10 | archive |

Registre : `free/profiles/verified_profiles.json`.  
Deux familles TLS sont désormais couvertes : `bun-1.4.4` (JA3
`1523504b…`) et `bun-1.3.14` (JA3 `d871d02c…`).

## 2. Incident prompt — trouvé puis corrigé

Les **premiers** 200 live ont été obtenus avec un prompt trop permissif :

1. le bloc `Instructions from: <chemin réel>\AGENTS.md` était conservé dans
   le prompt système ;
2. le corps 1.19.5 contenait aussi un chemin réel dans la description de
   l'outil `shell` (`/tools/0/function/description`).

Ces preuves ont été **retirées du registre**. Correction :

- suppression complète du bloc `Instructions from:` (chemin + contenu
  `AGENTS.md`) ;
- sanitation récursive de **toutes** les chaînes du corps (messages, outils,
  descriptions) ;
- remplacement de tout chemin `C:\Users\...` par `/workspace/<label>` ;
- remplacement du bloc `<env>` par sa version minimale ;
- date UTC courante.

Contrôle après correction sur les corps 2.2.5, 1.19.5 et 1.19.3 :

```
C:\Users            : absent
julie               : absent
Downloads           : absent
Instructions from:  : absent
/workspace/<label>  : présent
```

La mention générique « écrire dans AGENTS.md » peut rester dans un prompt
1.19.x : ce n'est ni un chemin ni un contenu local.

## 3. Preuves 2.2.5 `cli`

| Bloc `/64` | Egress | Modèle free | Statut | Latence | `response_sha256` | Project |
|---|---|---:|---:|---:|---|---|
| `2a09:bac1:27c0:2060::/64` | `2a09:bac1:27c0:2060::13:285` | `mimo-v2.6-flash-free` | **200** | 6734 ms | `07b683da2a02b5d0a0cead1c8bb88e368b3770b13c8242f2ce7d5f0ed4f9a2ec` | `a396083b973cf2c4631e97f806e5c3215560e842` |
| `2a09:bac5:32d3:150f::/64` | `2a09:bac5:32d3:150f::219:10a` | `glm-5.3-flash-free` | **200** | 3156 ms | `79f5f57e4ad60cd42b8ff37b922769922aec1ecb74eb147203a82a4ea3c5a268` | `1956351d33e11cd4d5a2160d187603424e6106a8` |

## 4. Preuves 1.19.5 `desktop`

| Bloc `/64` | Egress | Modèle free | Statut | Latence | `response_sha256` | Project |
|---|---|---:|---:|---:|---|---|
| `2a09:bac1:2780:2060::/64` | `2a09:bac1:2780:2060::216:b5` | `mimo-v2.6-flash-free` | **200** | 5906 ms | `255008c039a8da2834991aabf25d55b759514e407a0854d5d557edf4e9ba4adc` | `943771667c8172e0390ae5243035d9af3e774cb1` |
| `2a09:bac5:32d7:26aa::/64` | `2a09:bac5:32d7:26aa::3da:3` | `glm-5.3-flash-free` | **200** | 750 ms | `0f34a4492d017daf535925adff72f3cd690d5e5861659ebcf144443221205e29` | `8613147b4e025feaf3a3725eede88a4275c53b01` |

## 5. Archive — preuves 1.19.3 `desktop`

| Bloc `/64` | Egress | Modèle free | Statut | Latence | `response_sha256` | Project |
|---|---|---:|---:|---:|---|---|
| `2a09:bac5:32d4:28a0::/64` | `2a09:bac5:32d4:28a0::40c:c` | `mimo-v2.6-flash-free` | **200** | 2827 ms | `6f5632126aef196760a09fd4d92a23149df511087bedcbad244ded3b24e83e60` | `b3efd19b4d4a2551fc248a4b359d548c01a1d05e` |
| `2a09:bac5:32d1:14e6::/64` | `2a09:bac5:32d1:14e6::215:b9` | `glm-5.3-flash-free` | **200** | 8797 ms | `22a7c765b691f93ff4d19b1fcb0c34ddca9bc077ce200bfaf24ca1eac964a9c0` | `2db3b234cd191468903e3e5bf39ce797abdb01dc` |

Le wire 1.19.3 utilise son jeu d'en-têtes réel :

```
Authorization, Content-Type, User-Agent,
x-opencode-session-id, x-session-affinity, x-session-id,
Connection, Accept, Host, Accept-Encoding, Content-Length
```

Pas de quartet `x-opencode-client/project/request/session` : c'est le client
1.19.3 natif, avec le TLS Bun 1.3.14 (`d871d02c…`).

## 6. Archive — preuves 1.18.31 `desktop`

| Bloc `/64` | Egress | Modèle free | Statut | Latence | `response_sha256` | Project |
|---|---|---:|---:|---:|---|---|
| `2a09:bac5:32d5:26aa::/64` | `2a09:bac5:32d5:26aa::3da:a` | `mimo-v2.6-flash-free` | **200** | 2812 ms | `0a2e7c4ff017c3c45b53be7af556d1c1f799f5ca7c14077744c50351a4249aa7` | `1d62755ba2ceb51489b59c81ae8bd930495ed03d` |
| `2a09:bac5:32d3:28b4::/64` | `2a09:bac5:32d3:28b4::40e:3a` | `glm-5.3-flash-free` | **200** | 4359 ms | `7b2afed345303164859a226a5742b62a9b8caa75008f8f13d471026922518590` | `44282ff737fac575d36d51cae80133af7370591a` |

Le wire 1.18.31 n'envoie que :

```
Authorization, Content-Type, User-Agent,
x-session-affinity, x-session-id,
Connection, Accept, Host, Accept-Encoding, Content-Length
```

Validation du style `legacy18` : aucun `x-opencode-session-id`, aucun quartet.

## 7. Quota vs rejet

| Station | Statut | Verdict | Note |
|---|---:|---|---|
| `warp-test` (IPv4) | 429 | `quota` | pas un rejet de profil |
| `warp-test4` | 429 | `quota` | pas un rejet de profil |
| `warp-test2` / `warp-test3` | timeout | `transient` | connectivité, pas un rejet |
| `clean1` / `clean3` avec `deepseek` | 429 | `quota` | 200 avec un autre modèle free |
| `clean4` / `clean5` | timeout | `transient` | pas un rejet |
| `1195a` / `1195b` avec `deepseek` | 429 | `quota` | 200 avec un autre modèle free |

Aucun de ces échecs n'a été compté comme un rejet de profil.

## 8. Infrastructure de test

- Conteneurs `warp-verify`, `warp-clean1` à `warp-clean6`,
  `warp-1195a`/`warp-1195b`, `warp-1193a`/`warp-1193b` créés uniquement pour
  les essais, puis supprimés.
- Aucune modification de `config.yaml`, du pool ou du proxy en cours.
- Les conteneurs WARP existants n'ont pas été modifiés.

## 9. Contrôle format vs quota

Même bloc frais `2a09:bac5:32d4:28a0::/64` (`warp-wiretest`), même identité
P0, même profil `oc-2.2.5-cli-13` :

| Variante | Corps | Statut | Latence |
|---|---|---|---:|
| capture officielle 2.2.5 | 13 outils, `max_completion_tokens=32000`, pas de `tool_choice` | **200** | 6327 ms |
| wire runtime 2.2.5 | 13 outils, `tool_choice=auto`, `max_completion_tokens=32000` | **200** | 2500 ms |
| wire runtime 1.19.5 | 11 outils, `max_tokens=32000`, `tool_choice=auto`, quartet | **200** | 1469 ms |

Conclusion : **le format et l'identité runtime sont valides** pour les deux
dernières versions. Les 429 du proxy viennent du quota, pas d'un corps mal
formé.

Politique runtime : les deux dernières versions (`2.2.5`, `1.19.5`) et les
suivantes sont éligibles ; 1.19.3/1.18.31 sont retirés du runtime (mais
conservés au registre comme preuves historiques).

## 10. Cause racine du 429 — contrat strict et cooldown

Le proxy répondait 429 alors que les mêmes stations, testées directement avec
le wire runtime, répondaient 200. Diagnostic :

1. `FreeIPPool._station_usable()` appliquait le **contrat santé des stations
   Docker** aux `Socks5Endpoint` statiques. Sans auth/tunnel Docker, le
   contrat renvoyait `False` pour tous : `/api/free-egress/diag` affichait
   `usable:false` avec `bad_until:null`, sans cooldown ni quota.
2. `_free_cooldown_key()` keyait les SOCKS5 sur `socks5:<label>`. Après une
   rotation WARP, le label restait identique : l'ancien cooldown continuait
   de bloquer le nouveau bloc.

Corrections :

- le contrat strict est **ignoré pour les endpoints SOCKS5 statiques** ;
- la clé de cooldown utilise l'**egress réel normalisé `/64`**, avec repli
  `socks5:<label>` avant la première probe ;
- un reset opérationnel existe : `POST /api/free-egress/reset-cooldowns` ;
- diagnostic lecture seule : `GET /api/free-egress/diag`.

Après correction, `diag` retourne `usable:true` pour les six endpoints et
**6 requêtes proxy consécutives rendent 200**. Le proxy n'était donc ni en
panne de format ni en quota global : il ne sélectionnait plus les WARP.

## 11. Runtime proxy — flag actif

Le proxy a été redémarré avec :

```yaml
free:
  station_identity: true
```

Deux blocs WARP frais ont été ajoutés à `socks5_proxies` :

| Label | Bloc `/64` | Egress |
|---|---|---|
| `warp-restore1` | `2a09:bac1:27a0:2060::/64` | `2a09:bac1:27a0:2060::214:7c` |
| `warp-restore3` | `2a09:bac1:2780:2060::/64` | `2a09:bac1:2780:2060::216:7f` |

Requête réelle via le proxy :

```bash
POST http://127.0.0.1:4000/v1/chat/completions
{"model":"mimo-v2.6-flash-free","messages":[{"role":"user","content":"ping"}],
 "stream":false,"max_tokens":8}
```

Résultat : **200**, contenu `pong`. `logs/requests.db` enregistre :

- `station=-6`, `free_model_ip=socks5:warp-restore1`, `success=1` ;
- des requêtes concurrentes sur `station=-7`, `free_model_ip=socks5:warp-restore3`,
  `success=1`.

Le flag actif, un label de station et un profil vérifié sont donc bien
utilisés dans le chemin free réel. Le champ `capacity` de `/api/free-health`
reste à 0/1 car il suit les stations Docker, pas les entrées `socks5`.

### Affinité de conversation

Trois requêtes réelles :

| Requête | Conversation | Station | Bloc | Statut |
|---|---|---|---:|---:|
| `ping` | `ping` | -2 | `2a09:bac5:32d7:1c82::/64` | 200 |
| `ping` + `encore` | `ping` | -2 | `2a09:bac5:32d7:1c82::/64` | 200 |
| `bonjour` | `bonjour` | -7 | `2a09:bac5:32d2:1519::/64` | 200 |

La même conversation (même premier user `ping`) est restée sur la station
-2 ; une nouvelle conversation est allée sur -7. L'affinité runtime est
validée.

Le TTL est **glissant** : chaque tour réarme l'expiration. Si le TTL est
dépassé mais que la station est toujours saine, elle est réutilisée ; le
failover ne se déclenche que si elle est devenue inutilisable. Revalidation
live après redémarrage : deux tours sur la station -2, nouvelle conversation
sur la station -3, trois `200`.

### Pilotage par la GUI

Le round-robin n'est plus appliqué par défaut : il dépend des paramètres
réels de la GUI. Preuves live (`/api/vpn-config` puis requêtes proxy) :

| Paramètres GUI | Conversations neuves | Stations |
|---|---|---:|
| `enabled=true`, `routing=failover` | 3 | -3, -3, -3 |
| `enabled=true`, `routing=round-robin`, `mode=load-balance` | 3 | -2, -4, -6 |
| `enabled=true`, `routing=round-robin`, `mode=strict` | 3 | -6, -7, -1 |
| `socks5_auto_rotate=false` | 2 | -6, -6 |
| `free_parallel.enabled=false` | 2 | -7, -7 |

Le diagnostic expose l'état effectif :

```json
{"socks5_auto_rotate": true, "free_parallel_enabled": true,
 "free_parallel_routing": "failover", "free_parallel_mode": "load-balance",
 "hedge_delay_ms": 1500, "hedge_max_attempts": 1,
 "hedge_ready": false, "spread": false}
```

En mode `hedge`, avec `hedge_delay_ms=0`, un premier tour a incrémenté deux
stations (+1 chacune) : la course a bien eu lieu. Le tour suivant de la même
conversation n'a incrémenté que la gagnante : le hedge est épinglé après le
premier tour, jamais deux copies du même historique en parallèle.

Les paramètres qualité/latence sont aussi actifs en SOCKS5 :
`quality_preselection` ajuste le seuil de rotation via la qualité par IP,
`quality_rotation` déclenche le rotateur WARP sur un endpoint managé (un
non-managé est ignoré), et un parc entier hard-cooled par `latency_rotation`
reste sélectionnable via le troisième passage d'admission — jamais zéro
candidat.

`post_rotation_probe` est branché : sonde synchrone avec l'identité/profil de
la station après rotation WARP, `max_tokens` et `max_attempts` respectés.
Preuve live sur `warp-ipv6-2` : la sonde renvoie `True` sur un `429` (egress
joignable, quota modèle), sans bad-mark. Les échecs réseau/403 sont retentés
puis bad-markés 120 s.

Le watchdog tourne aussi un endpoint managé dont un couple `(modèle, IP)` est
bloqué après `429`, même si le ledger journalier n'est pas épuisé. Cela évite
d'attendre que tous les blocs soient marqués avant de rendre une IP fraîche.

Sous charge, une vague de timeouts pouvait bad-mark toute la flotte SOCKS5 et
laisser 1-2 candidats. Un journal dédié compte les couples `(station, IP)`
distincts sur 60 s : au 3e, la cause est un événement amont/réseau et aucun
bad-mark supplémentaire n'est posé. Le journal 429/rotation reste séparé, donc
la rotation quota n'est pas gelée par cette garde.

Le state pool persistait aussi `bad_remaining` : au redémarrage, les 7
endpoints héritaient des timeouts de la session précédente (4/7 utilisables
pendant ~60 s). Le loader restaure désormais les compteurs et `ip_stats` mais
**ignore les bad-marks transitoires**. Preuve live : après restart, `diag`
retourne immédiatement **7/7 usable**, `bad_remaining_s=0` partout, et le ping
proxy rend `200`.

Enfin, un endpoint managé dont l'IP est `trial-dead` (cooldown 6 h) est tourné
par le watchdog avec la cause `trial_dead`, via le callback
`_trial_dead_managed_labels` qui balaie tous les modèles. Sans cela, une
station pouvait rester hors sélection 6 h alors qu'une rotation rendait une IP
fraîche.

Un contrôle a révélé que `warp-restore1` et `warp-restore3` partageaient le
même `/64` : `_apply` s'exécutait hors du verrou global, donc deux rotations
concurrentes pouvaient choisir le même bloc avant que la première ne soit
visible. `_apply` est désormais sous `_ROTATE_LOCK`, et un doublon résiduel
est détecté/tourné (`duplicate_block`). Après correction live :
**6/6 blocs distincts**.

| Label | Bloc |
|---|---|
| `warp-ipv6` | `2a09:bac5:32d2:1519::/64` |
| `warp-ipv6-2` | `2a09:bac5:32d3:14e6::/64` |
| `warp-ipv6-3` | `2a09:bac1:27a0:2060::/64` |
| `warp-ipv6-4` | `2a09:bac1:2780:2060::/64` |
| `warp-restore1` | `2a09:bac5:32d1:14dc::/64` |
| `warp-restore3` | `2a09:bac5:32d5:28b4::/64` |

Les fenêtres de console intempestives venaient aussi de `free/warp_rotation.py` :
ses appels `docker` (restart/inspect/recreate) et le `curl` de secours
n'utilisaient pas `CREATE_NO_WINDOW`, donc chaque rotation ouvrait une fenêtre
MS-DOS sous `pythonw.exe`. Les deux appels passent désormais
`creationflags=CREATE_NO_WINDOW` (0 hors Windows) ; un test vérifie le flag.

La GUI SOCKS5 affiche maintenant le `label`, `host:port`, l'egress, le bloc et
le badge WARP. Vérifié dans le navigateur intégré : 8 lignes, **6 WARP avec
6 blocs distincts**, aucune erreur console. Le bouton Test envoie le label
exact et force IPv6, ce qui distingue `warp-ipv4`/`warp-ipv6` sur le même
`127.0.0.1:11080`. Test live du bouton sur `warp-ipv6-2` :
`✓ 2a09:bac1:27a0:2060::3da:52 — 750 ms`.

Un plancher de redondance empêche aussi un timeout de bad-mark au point de
laisser moins de `min(3, n-1)` endpoints sains. La garde corrélée couvre les
rafales dans la fenêtre 60 s ; le plancher couvre les vagues plus longues.

Enfin, `capacity_snapshot()` comptait les stations Docker (vides) en mode
SOCKS5 : `/health` restait `degraded` avec « 0/1 stations usable » alors que
7/7 WARP répondaient. La branche SOCKS5 compte désormais les endpoints réels.
Après redémarrage : `status=ok`, `configured_stations=7`,
`usable_stations=7`, `unique_usable_ips=6` (l'IPv4 `warp-ipv4` est logique),
`capacity_degraded=false`.

## 12. Rotation automatique WARP

Déclenchement manuel contrôlé puis watchdog :

```bash
POST http://127.0.0.1:4000/api/vpn/socks5/rotate-egress
{"ok":true,"started":2}
```

Le watchdog a ensuite tourné les quatre autres stations épuisées.

Les six stations managées ont été tournées automatiquement et ont obtenu six
blocs distincts, persistés dans `config.yaml`. État au moment de la preuve
finale (2026-11-06 11:29Z) :

| Label | Egress | Bloc `/64` |
|---|---|---|
| `warp-ipv6` | `2a09:bac5:32d7:1c82::2d7:10f` | `2a09:bac5:32d7:1c82::/64` |
| `warp-ipv6-2` | `2a09:bac5:32d2:14e6::215:11a` | `2a09:bac5:32d2:14e6::/64` |
| `warp-ipv6-3` | `2a09:bac5:32d0:14f0::216:9e` | `2a09:bac5:32d0:14f0::/64` |
| `warp-ipv6-4` | `2a09:bac5:32d4:1519::21a:96` | `2a09:bac5:32d4:1519::/64` |
| `warp-restore1` | `2a09:bac5:32d3:150f::219:a9` | `2a09:bac5:32d3:150f::/64` |
| `warp-restore3` | `2a09:bac5:32d2:1519::21a:68` | `2a09:bac5:32d2:1519::/64` |

Les six blocs sont distincts deux à deux. La rotation change donc bien
l'identité d'egress de chaque station.

Contrôle anti-churn à 12:41Z : les six conteneurs sont `running`,
`RestartCount=0`, démarrés entre 11:22Z et 11:40Z. Aucune rotation en boucle
une heure après la convergence.

### Preuve 6/6 directe

Chaque station managée a reçu le corps officiel 2.2.5 (13 outils) via son
SOCKS5, avec une identité de station fraîche :

| Station | Port | Statut | Verdict | Latence |
|---|---:|---:|---|---:|
| `warp-ipv6` | 11080 | 200 | pass | 2093 ms |
| `warp-ipv6-2` | 11085 | 200 | pass | 2250 ms |
| `warp-ipv6-3` | 11086 | 200 | pass | 7358 ms |
| `warp-ipv6-4` | 11087 | 200 | pass | 3202 ms |
| `warp-restore1` | 11103 | 200 | pass | 4485 ms |
| `warp-restore3` | 11105 | 200 | pass | 1561 ms |

**6/6 stations client-utilisables.**

### Répartition des nouvelles conversations

Six conversations distinctes envoyées au proxy (clé d'affinité différente à
chaque fois) :

| Conversation | Statut | Station | Egress |
|---|---:|---:|---|
| `station-check-…-0` | 200 | -1 | `socks5:warp-ipv4` |
| `station-check-…-1` | 200 | -2 | `2a09:bac5:32d7:1c82::2d7:10f` |
| `station-check-…-2` | 200 | -6 | `2a09:bac5:32d3:150f::219:a9` |
| `station-check-…-3` | 200 | -1 | `socks5:warp-ipv4` |
| `station-check-…-4` | 200 | -2 | `2a09:bac5:32d7:1c82::2d7:10f` |
| `station-check-…-5` | 200 | -6 | `2a09:bac5:32d3:150f::219:a9` |

Le round-robin n'utilise que les stations sous leur seuil de quota ; les
autres sont réservées et redeviennent éligibles après rotation. Aucune
requête n'a rendu 429.

Le déclenchement est aussi branché sur :

- chaque `429` de quota (`_ledger_note_result`) ;
- `on_quota_exhausted` en mode socks5 ;
- `rotate_exhausted_managed()` avant sélection ;
- un watchdog lifespan toutes les 30 s, même sans requête.

Le 429 observé juste après une rotation venait du contrat strict appliqué aux
SOCKS5 (section 10), pas de la rotation. Après correction, les séries de six
requêtes proxy et la preuve directe 6/6 rendent 200.

## 13. Portée

- **Fait** : 2.2.5 `cli` et 1.19.5 `desktop` sont les profils actifs ;
  1.19.3/1.18.31 sont archivés, plus jamais assignés au runtime.
- **Fait** : deux styles de wire actifs (`2.2.5` 13 outils, `1.19.5` 11
  outils) et deux archives legacy.
- **Fait** : six stations WARP managées, six blocs distincts, rotation
  automatique, six requêtes proxy consécutives en 200 et preuve directe
  6/6 (200/pass).
- **Fait** : suite complète hors Docker **4808 tests, exit 0** ; le test
  fast-pin qui bloquait à 37 % (92 s de sleeps réels) passe par `_sleep_fn`
  et tombe à 0,92 s. `test_gluetun_control_api.py` : 59/59 en 13 s.
