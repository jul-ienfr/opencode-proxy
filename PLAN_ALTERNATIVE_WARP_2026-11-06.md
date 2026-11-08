# Plan — Garder WARP, ajouter un fallback (priorité gratuit)

**Date** : 2026-11-06  
**Périmètre** : `opencode-proxy-one`, mode `proxy_mode: socks5` (WARP), quota des
modèles free OpenCode.  
**Statut** : proposition, aucun changement de code ou d'infrastructure appliqué.  
**Hypothèse** : « WARP » = **Cloudflare WARP**, utilisé ici via les conteneurs
`caomingjun/warp` comme source d'egress IPv6. Si tu parlais du terminal Warp,
ce plan n'est pas applicable.  
**Dépôts analysés** : [`docs/REPOS_12_UNLIMITED_2026-11-06.md`](docs/REPOS_12_UNLIMITED_2026-11-06.md)
— 12 dépôts « OpenCode Unlimited », dont les leçons sont intégrées au §3.8.

---

## 0. Résumé exécutif

**Décision de principe : WARP reste la source principale.** La question n'est
pas de le remplacer, mais de définir ce qui prend le relais quand il est épuisé,
throttlé ou en panne.

Il faut distinguer deux types de repli :

- **Repli de continuité** : un autre client WARP (`MicroWARP`, `usque`,
  `warp-minimal`) ou un autre conteneur prend le relais. Même pool Cloudflare,
  donc **pas plus de quota**, mais le service continue.
- **Repli de capacité** : une autre source d'IP (VPN gratuit, IPv6 de la box,
  Oracle free, VPS). C'est le seul type qui ajoute des buckets.

Échelle recommandée, **gratuite d'abord** :

| Niveau | Repli | Coût | Capacité | Rôle |
|---:|---|---:|---|---|
| 0 | **WARP** (primaire) | 0 € | **4–5 blocs utiles** | trafic normal |
| 1 | **Client WARP alternatif** (`MicroWARP`, `usque`, `warp-minimal`) | 0 € | même pool | continuité si un conteneur casse ou si la rotation est bloquée |
| 2 | **IPv6 box/ISP** (`/56` ou `/48`) | 0 € | 256 à 65 536 | le meilleur repli gratuit **si disponible** |
| 3 | **Oracle Always Free** | 0 € | 2–4 `/64` | secours gratuit de capacité, mais setup |
| 4 | **Proton Free** via gluetun | 0 € | data illimitée, **1 appareil** | secours gratuit sérieux, une station à la fois |
| 5 | **Windscribe/Privado Free** | 0 € | 10 Go/mois | urgence de quelques heures/jours |
| 6 | BuyVM `/48` | 24 $/an | 65 536 | **dernier recours payant**, si les gratuits ne suffisent pas |

> Chez **Red by SFR**, le `/56` existe côté réseau, mais la box fournie ne le
> délègue pas proprement : sans remplacer la box, compte **1–2 buckets**, pas
> 256. Détail et chiffres au §3bis.
>
> En **5G Free**, une session = un `/64` = **1 bucket** ; 350 Go/mois, pas un
> pool de blocs. Détail au §3ter.
>
> **Aucun des 12 dépôts « OpenCode Unlimited » analysés ne crée de quota
> illimité.** Ils déplacent tous un curseur (bloc, identité, compte/clé ou
> fournisseur). Le seul multiplicateur gratuit propre reste le nombre de
> **blocs d'egress distincts**. Synthèse au §3.8 ; détail dans
> [`docs/REPOS_12_UNLIMITED_2026-11-06.md`](docs/REPOS_12_UNLIMITED_2026-11-06.md).

**Recommandation pour ton cas** : garder WARP, ajouter `MicroWARP`/`usque` en
continuité, puis **vérifier la box/ISP**. Chez Red by SFR, sans remplacer la
box, tu n'auras que **1–2 buckets** ; le `/56` = 256 buckets seulement si tu
remplaces la box par un routeur compatible. Oracle free (2–4) ou Proton Free
(1 station) sont alors de meilleurs replis gratuits. BuyVM seulement si les
options gratuites sont épuisées ou indisponibles.

---

## 0bis. Politique de fallback (WARP primaire)

### Déclencheurs

On ne bascule **pas** au premier 429 : le pool commence par la rotation WARP.

1. `FreeUsageLimitError` après épuisement des rotations WARP (plus de `/64`
   frais dans le ledger) ;
2. échec de création/enregistrement WARP ou throttle persistant > 10 min ;
3. conteneur WARP down > 2 min, ou erreurs de connexion répétées ;
4. bloc WARP qui revient épuisé plusieurs fois de suite ;
5. override manuel depuis le dashboard.

**Ne déclenche pas** : un `403 FreeTierError` de forme, geo, auth ou version. Un
403 se corrige dans le wire ou le pays, pas en tournant l'IP. Voir §3.8.

### Ordre de bascule

```text
WARP (primaire)
  └─ 1. re-register / recreate WARP (rotator existant)
      └─ 2. MicroWARP ou usque (même pool, continuité)
          └─ 3. IPv6 box/ISP (si /56 ou /48 disponible)
              └─ 4. Oracle Always Free (2–4 /64)
                  └─ 5. Proton Free via gluetun (1 appareil, data illimitée)
                      └─ 6. Windscribe/Privado Free (10 Go, urgence)
                          └─ 7. VPS /48 (payant, si activé)
                              └─ 8. direct (diagnostic seulement)
```

**Règle** : WARP doit rester le chemin majoritaire. On ne met en fallback que
**1 à 2 stations** à la fois, jamais les six, sinon on vide le forfait gratuit
en quelques heures.

### Retour à WARP

- sonde WARP toutes les 5 min (un modèle réel, pas la page d'accueil) ;
- retour après **2 succès consécutifs** ;
- cooldown de 15 min pour éviter le flapping ;
- si WARP re-échoue, garder le fallback et marquer le bloc WARP épuisé.

### Ledger et cohérence

- chaque endpoint fallback a un `label` et un `provider` distincts
  (`warp`, `proton`, `windscribe`, `oracle`, `vps-ipv6`) ;
- le bloc fallback est enregistré comme les autres, avec reset 00:00 UTC ;
- ne jamais réutiliser un bloc fallback pour une station WARP, et inversement ;
- le dashboard doit afficher : provider actif, bloc, dernier 429, état de santé.

---

## 1. Ce que WARP fait aujourd'hui, et pourquoi il plafonne

### 1.1 Architecture actuelle

- 6 endpoints WARP IPv6 actifs dans `config.yaml` (`warp-ipv6`, `warp-ipv6-2`,
  `warp-ipv6-3`, `warp-ipv6-4`, `warp-restore1`, `warp-restore3`), plus une
  entrée IPv4 désactivée.
- Chaque endpoint est un conteneur `caomingjun/warp` exposant un SOCKS5 local
  (`127.0.0.1:11080` à `11105`).
- `free/warp_rotation.py` recrée le conteneur pour obtenir un nouveau `/64`,
  évite les blocs déjà épuisés dans le ledger, et retente jusqu'à 4 fois.
- `free/pool.py` déclenche la rotation sur 429/quota, bloc dupliqué, modèle
  bloqué ou station `trial_dead`.
- Le ledger `free/quota_ledger.py` normalise IPv6 au `/64` et IPv4 au `/32`.

### 1.2 Limites mesurées

| Limite | Preuve / conséquence |
|---|---|
| Pool Cloudflare fini et réutilisé | les rotations rendent souvent le même `/64` ; le code doit écarter les blocs épuisés |
| Throttle des enregistrements | ~4 conteneurs fonctionnent, au-delà les créations échouent ou sont lentes |
| Rotation lente | restart + healthcheck + probe + recréation : minutes, pas secondes |
| Ressources | ~50 Mo de RAM par conteneur, 6 conteneurs = ~300 Mo |
| IPv4 partagée | WARP IPv4 est une NAT ; rotation inutile pour le quota |
| Réputation | IP Cloudflare très partagées, parfois déjà flagged |

### 1.2bis Combien d'IP WARP ?

- **1 conteneur = 1 tunnel = 1 adresse IPv6** (plus une IPv4 NAT partagée),
  pas un `/64` qui t'appartient.
- Le bucket est le **`/64` qui contient cette adresse** ; tu ne peux pas utiliser
  les autres adresses du `/64`, et plusieurs conteneurs peuvent tomber dans le
  même `/64`.
- État documenté : **5 egresses actives** = 4 `/64` IPv6 distincts + 1 IPv4 NAT ;
  6 endpoints sont configurés, mais le 6e n'est pas garanti.
- Limite pratique : **~4–6 conteneurs par hôte** ; au-delà, les enregistrements
  échouent ou sont throttlés. Le pool Cloudflare est fini et réutilisé.
- La rotation WARP coûte une **nouvelle registration** (`registration
  delete/new`), pas un simple `disconnect/connect` ; l'API est rate-limitée
  (~90 s entre deux rotations, quelques rotations par passe). `ebouther` mesure
  que six conteneurs tombent souvent sur la **même adresse** : il faut mesurer
  l'egress réel et dédupliquer, pas compter les conteneurs.
- **WARP+ et Zero Trust n'ajoutent pas d'IP** ; ils changent le routage/les
  fonctionnalités, pas le nombre d'egresses.
- Conséquence : WARP donne **4–5 buckets**, pas 256. À ~300 requêtes/bloc/jour,
  cela fait ~1 500 requêtes/jour, sous la demande de 3 000–10 000.

**Stabilité de l'IP** : un conteneur garde son IP tant qu'il n'est pas recréé.
`docker restart` réutilise le même tunnel (même IP/`/64` en général) ;
`docker rm -f` + `docker run` crée une nouvelle registration, avec une IP
**choisie par Cloudflare**, pas par toi. Le rotateur tente d'abord un restart,
puis recrée seulement si le bloc est identique ou déjà épuisé.

**IPv4 + IPv6** : les deux familles ont des quotas séparés, donc un conteneur
peut valoir **2 buckets** (1 IPv4 + 1 IPv6) si son IPv4 est distincte. Mais
WARP IPv4 est une **NAT partagée** : en pratique plusieurs conteneurs sortent
par la même IPv4, donc le gain réel est souvent **+1 bucket IPv4 global**, pas
+1 par conteneur. À vérifier avec `curl -4` et `curl -6` sur chaque port :
seules les IPv4 `/32` et IPv6 `/64` réellement distinctes comptent.

### 1.3 Le quota est par bloc, pas par session

Les documents internes convergent :

- `docs/AUDIT_REPOS_2026-11-06.md` : quota keyé par **bloc d'egress** (`/64`
  IPv6, `/32` IPv4), reset **00:00 UTC** ;
- `docs/quota-identity.md` : `server_ip_key()` tronque IPv6 aux 4 premiers
  groupes, donc au `/64` ; changer l'interface ID dans le même `/64` ne change
  pas le bucket ;
- `docs/OPENCODE_FREE_SOLUTIONS_2026-11-04.md` : IPv4 et IPv6 ont des quotas
  séparés ; le multiplicateur propre est l'IPv6.

**Conséquence** : un VPS avec un seul `/64` ne vaut qu'**un** bucket. Il faut
un `/56` (256 `/64`) ou un `/48` (65 536 `/64`), pas juste « une IP IPv6 ».

**Nuance importante** : c'est le modèle dominant pour le pool free par défaut,
pas une loi universelle. Certains modèles/tiers peuvent ajouter un quota
**compte/clé**, voire un budget global ; les 12 dépôts sont divisés sur ce
point. La Phase 0 doit donc tester **par modèle** et **avec un contrôle**
(direct vs WARP, même clé vs clé différente) avant de conclure. Voir §3.8.

**Ne pas confondre** : un `403 FreeTierError` (forme, geo, auth, version) n'est
pas un `429 FreeUsageLimitError` (quota). Une rotation d'IP ne corrige pas un
403 ; il faut corriger le wire ou changer de pays.

> ⚠️ Le test E4 « même `/64` vs `/64` neuf » est encore marqué **à faire** dans
> `docs/quota-identity.md`. C'est la première chose à valider avant d'acheter.

### 1.3bis Reset temporel : parquer plutôt que recréer

Le bucket est journalier, reset **00:00 UTC**. Un conteneur épuisé à 14 h n'est
pas « mort » : à 00:00, **la même IP/le même bloc redevient utilisable**. Il
faut donc distinguer deux mécanismes :

- **cooldown** : le bucket est épuisé, on **parke** le bloc jusqu'à `reset_at`
  (ou jusqu'à un probe plus tôt) ;
- **rotation** : on recrée le conteneur pour obtenir **un autre bloc**, seulement
  si on a besoin de capacité **avant** le reset et qu'aucun autre bloc n'est
  disponible.

Le ledger sait déjà stocker `retry_after`, `reset_at`, `earliest_reset`,
`snapshot` et `max_block_s` (`ledger_max_block_s: 1200` dans `config.yaml`).
Mais `free/pool.py` déclenche encore une rotation sur **chaque 429**
(`free/pool.py:1455`), donc il recrée avant d'exploiter l'heure de reset.

**Politique proposée :**

1. sur 429, enregistrer le bloc, `exhausted_at`, `retry_after` et `reset_at` ;
2. si le reset est proche (< 30 min) et qu'un autre bloc est disponible :
   **parquer**, ne pas recréer ;
3. si le reset est loin et qu'aucun bloc n'est disponible : rotate/fallback ;
4. au reset (ou au probe cap), tester **une** requête :
   - `200` → réutiliser le même conteneur/bloc ;
   - `429` → re-planifier avec backoff ;
5. ne recréer que si le bloc reste épuisé après le reset, ou si la capacité
   immédiate est indispensable.

⚠️ `Retry-After` peut annoncer ~19 h alors que la recovery réelle arrive plus
tôt (jitter observé 00:01–00:53 UTC) : d'où le cap `max_block_s` et des probes,
pas un sommeil aveugle jusqu'à 00:00.

### 1.3ter Ce qui ne multiplie pas le quota (et ce qui le gaspille)

Les 12 dépôts convergent sur ce point : il faut distinguer les **vrais
multiplicateurs** des **faux leviers**.

| Levier | Effet sur le quota | Preuve |
|---|---|---|
| Nouveau **bloc** `/64` ou `/32` | **multiplicateur réel** | [M] `ebouther`, [R] audit |
| Plusieurs adresses dans le même `/64` | **aucun** (même bucket) | [D] `Tazmyan`, [R] audit |
| Plusieurs conteneurs sur le même bloc | **aucun** | [M] `ebouther` (dédup) |
| Device ID / session / projet / machine ID | **aucun** ; nettoie seulement un banner | [D] `Tazmyan`, `Nirusaki` ; [R] audit |
| Même clé sur une IP fraîche | dépend de l'axe compte/clé ; à tester | [I] `hansputera` |
| Rotation WARP qui change l'IPv6 | **pas garanti** de changer le bucket ; mesurer | [M] `ngynlaam` |
| Contexte long / raisonnement `max` / grosses sorties d'outils | **gaspille** le quota (rejeu de tokens) | [D] `Tazmyan`, `Netherstar` |
| Egress sticky par session | **économise** le cache et les rejeux | [I] `d1alect`, `hansputera` |

**Règles pratiques** :

1. compter les **blocs distincts**, pas les conteneurs, sessions ou comptes ;
2. garder le même egress et le même ID de session tant qu'il n'y a pas de 429
   (sticky) ;
3. découper les tâches longues en sessions courtes et éviter les sorties
   d'outils énormes ;
4. ne pas investir dans le spoof d'identité pour le quota ;
5. si un bloc frais reste 429, tester l'axe **modèle / compte / clé** avant
   d'ajouter des proxies.

### 1.4 Volume réel à absorber

Mesures locales dans `logs/requests.db` (à titre indicatif, les tokens ne sont
pas des octets) :

| Date | Requêtes free | IP distinctes | Tokens entrée | Tokens sortie |
|---|---:|---:|---:|---:|
| 2026-11-06 | 6 698 | 325 | 507,8 M | 2,0 M |
| 2026-11-04 | 2 611 | 189 | 151,9 M | 1,4 M |
| 2026-11-03 | 4 929 | 133 | 280,7 M | 1,8 M |
| 2026-10-28 | 10 487 | 64 | 801,9 M | 6,2 M |

Ordre de grandeur : **0,6 à 3 Go/jour** si on compte ~4 octets/token, soit
**18 à 90 Go/mois**. Un forfait VPN gratuit à 10 Go/mois tient quelques jours,
pas un mois. Il faut donc une source **sans cap de données** (WARP, VPS
unmetered, Oracle 10 To/mois) ou un forfait payant adapté.

Surtout, le pool actuel est **structurellement sous-dimensionné** : en prenant
~300 requêtes/bloc/jour comme ordre de grandeur, **4–5 blocs ≈ 1 200–1 500
requêtes/jour**, alors que les logs montrent **3 000 à 10 000 requêtes/jour**.
Les 429 ne sont pas un accident de rotation : il manque des blocs. Un `/56`
(256 blocs) ou un `/48` (65 536 blocs) change l'échelle.

Sur les 3 derniers jours, la base contient **11 028 réponses 200** contre
**3 655 réponses 429**, soit ~25 % d'échecs de quota. C'est cohérent avec un
pool de 4–5 blocs pour 3 000–10 000 requêtes/jour.

---

## 2. Phase 0 — valider la granularité du quota (go/no-go)

C'est la décision la plus importante : si le quota n'est ni par `/64` ni par
adresse, **tout le plan de repli de capacité** tombe (VPS, Oracle, box/ISP) et
WARP reste la seule source utilisable.

### 2.1 Question

Le bucket est-il keyé par :

1. **adresse IPv6 complète** → un seul `/64` suffit (2^64 adresses) ;
2. **préfixe `/64`** → il faut plusieurs `/64` (box/ISP `/56`, Oracle, BuyVM
   `/48`, Linode `/56`) ;
3. **ASN / préfixe agrégé / compte / budget global** → aucun VPS ne suffit, il
   faut des résidentiels/mobiles, des comptes, ou attendre le reset ;
4. **modèle** → certains modèles `contributor-free` peuvent avoir un axe
   différent du pool par défaut ; tester chaque modèle utilisé.

### 2.2 Protocole de test

Sur une machine Linux avec un `/64` routé (VPS de test ou machine locale) :

0. **Gate d'abord** : vérifier qu'un baseline passe (UA, session canonique,
   `read`, `stream: true`, `<env>`). Un `403` n'est pas un `429` ; tant que le
   baseline est 403, le test de quota ne mesure rien.
1. Envoyer des requêtes free depuis l'adresse `A` du `/64` jusqu'au premier
   `429` (`FreeUsageLimitError`).
2. Envoyer immédiatement depuis `B`, **même `/64`**, adresse différente.
   - `429` → le bucket est le `/64` ;
   - `200` → le bucket est l'adresse.
3. Envoyer depuis `C`, **`/64` différent**.
   - `200` → nouveau bucket par `/64` (ou par adresse) ;
   - `429` → bucket agrégé (ASN/compte/global), le VPS ne suffit pas.
4. Rejouer avec deux IPv4 `/32` comme contrôle.
5. Rejouer le point 3 **par modèle** (`deepseek-v4-flash-free`, `beam-free`,
   `muse-spark`, etc.) ; un modèle peut être account-limited alors qu'un autre
   est per-block.
6. Si tu as plusieurs clés légitimes, comparer `public` vs clé `zent-` sur le
   **même** bloc frais ; sinon ne pas conclure sur l'axe compte/clé.

Utiliser le wire exact du proxy (`scripts/test_free_egress.py` ou une requête
`/inference` avec le profil 2.2.5), pas un simple `curl` vers la page d'accueil.
Noter le `Retry-After`, l'heure UTC, et le temps réel de recovery.

⚠️ Distinguer un **429 de quota** (`FreeUsageLimitError`, bucket journalier)
d'un **429 de rate-limit court** (`RateLimitError`, fenêtre de quelques
secondes). Attendre la fin de la fenêtre courte ou lire le corps de la réponse
avant de conclure ; sinon le test est faux.

⚠️ Un **403** (`FreeTierError`) peut être une forme de requête, un geo-gate, une
version de client ou un problème d'auth. Il ne doit **jamais** être compté comme
un bloc épuisé ni déclencher une rotation.

### 2.3 Go/no-go

| Résultat | Décision |
|---|---|
| bucket par adresse | un seul `/64` suffit ; box/ISP ou Oracle en priorité |
| bucket par `/64` | box/ISP `/56` > Oracle > BuyVM `/48` en dernier recours |
| bucket par ASN/compte/global | WARP seul ; pivot résidentiel/mobile, compte payant, ou reset |
| bucket différent selon le modèle | router les modèles account-limited vers un autre provider ou les parker |

### 2.4 Vérifications à faire en même temps

- **Box/ISP** : vérifier si la box délègue un `/56` ou `/48` (`ip -6 addr`,
  page de la box, `test-ipv6.com`). Si oui, c'est la meilleure source gratuite :
  256 à 65 536 blocs, IP résidentielle, pas de VPS à louer.
- **Oracle Always Free** : vérifier la disponibilité des 2 VM et des `/64` ;
  prévoir la carte bancaire et le setup réseau.
- **Proton Free** : tester une connexion gluetun, data illimitée mais **un seul
  appareil** ; c'est le secours gratuit le plus simple.
- **BuyVM** (seulement si nécessaire) : confirmer dans Stallion que le plan 512
  reçoit bien un **`/48` routé**.
- **Trafic** : mesurer les octets réels par requête sur 7 jours avant de choisir
  un forfait VPN à cap.

---

## 3. Panorama des alternatives

### 3.1 Remplacer le client WARP (pas la source d'IP)

Ces projets peuvent améliorer WARP, mais **ne changent pas le pool Cloudflare**.
Ils ne résolvent donc pas le plafond de blocs.

| Projet | Langage | Ce qu'il apporte | Limite |
|---|---|---|---|
| [`Deury08/warp-minimal`](https://github.com/Deury08/warp-minimal) | Go | proxy HTTP/HTTPS MASQUE, pool d'exits, rotation auto sur 429, pensé pour OpenCode free | HTTP seulement, pool WARP fini, projet très récent (2 ★) |
| [`Diniboy1123/usque`](https://github.com/Diniboy1123/usque) | Go | client MASQUE, SOCKS5/HTTP, 1,5 k ★ | un processus par tunnel ; même pool |
| [`ccbkkb/MicroWARP`](https://github.com/ccbkkb/MicroWARP) | Shell/Docker | SOCKS5 WARP en ~800 Ko de RAM, dual-stack, remplace `caomingjun/warp` | même pool, même throttle |
| [`bepass-org/warp-plus`](https://github.com/bepass-org/warp-plus) | Go | WARP + Psiphon | même pool, orienté censure |
| `warp-go`, `sing-box`, `wireproxy` | divers | autres clients WARP | même pool |

**Verdict** : à garder comme **optimisation** (RAM, vitesse de rotation,
robustesse MASQUE), pas comme alternative de capacité. `warp-minimal` mérite un
benchmark de 1 jour : s'il tient 8 exits et rotate en quelques secondes, il
peut remplacer les 6 conteneurs et simplifier l'exploitation.

**Benchmark concret** : lancer `warp-minimal` avec `pool_size: 8` (proxy HTTP),
puis comparer le nombre de `/64` distincts, le temps de rotation et le taux de
429 avec les 6 conteneurs actuels. `MicroWARP` peut souvent remplacer l'image
`caomingjun/warp` à schéma `host:port` proche, mais vérifie les variables
d'environnement avant de le déployer.

Le pool possède déjà `HttpsProxyEndpoint` (sous-classe de `Socks5Endpoint`,
utilisée pour NordVPN `:89`) : le transport HTTP de `warp-minimal` est donc
intégrable. Le point dur n'est pas le transport, c'est l'attribution de l'IP de
sortie quand un même proxy sert plusieurs exits.

### 3.2 VPN gratuits (IPv4)

Gluetun supporte nativement ProtonVPN, Windscribe, Privado, Mullvad, IVPN,
AirVPN, PIA et Surfshark ([wiki](https://github.com/qdm12/gluetun-wiki/tree/main/setup/providers)).
Mais les offres gratuites ont des limites dirimantes :

| Fournisseur | Data | Pays | Appareils | Verdict |
|---|---:|---:|---|---|
| **ProtonVPN Free** | illimité | 10 | **1 à la fois** | bon débit, mais une seule station parallèle |
| **Windscribe Free** | 10 Go/mois (2 Go sans email) | 10 | illimités | cap trop petit pour le volume actuel |
| **PrivadoVPN Free** | 10 Go/mois | 8–10 | illimités | cap trop petit |
| hide.me Free | ~10 Go/mois (à vérifier) | 8 | limité | pas de support gluetun natif (hide.me ≠ HideMyAss) |
| VPN Gate | illimité | nombreux | — | relais publics, **usage académique**, non fiable et non sûr |

**Verdict** : dans une stratégie **WARP primaire**, Proton Free est le meilleur
secours gratuit de capacité pour une station (data illimitée) ;
Windscribe/Privado sont des urgences à 10 Go. Aucun ne doit devenir la source
principale.

⚠️ La plupart de ces offres gratuites exposent surtout des sorties **IPv4**
(`/32`), pas des `/64` : elles ajoutent **un bucket**, pas une famille de blocs.
Pour plusieurs `/64`, il faut de l'IPv6 (box/ISP, Oracle, VPS).

### 3.3 VPS IPv6 — le repli de capacité (payant)

| Fournisseur | Préfixe | Prix | Blocs `/64` | Remarque |
|---|---|---:|---:|---|
| **BuyVM/Frantech 512** | `/48` routé | **24 $/an** | **65 536** | 512 Mo, 10 Go SSD, bande passante non mesurée |
| **Linode/Akamai** | `/128` par défaut ; `/64`, `/116` ou `/56` sur justification | ~5 $/mois | 1 à 256 | support réactif ; `/56` non garanti |
| **Oracle Cloud Always Free** | VCN `/56`, sous-réseaux `/64` | 0 $ | 2–4 | 2 VM AMD, 10 To/mois, mais peu de VNIC |
| Hetzner Cloud | `/64` par serveur | ~3,79 €/mois | 1 | fiable, mais un seul bloc par VM |
| Vultr | `/64` par instance | ~3,50 $/mois | 1 | idem |
| Contabo | `/64` par VPS | ~5 €/mois | 1 | idem |
| Hurricane Electric | `/64` par tunnel | 0 $ | 1 par tunnel | 6in4, IPv4 publique + proto 41 |
| BGPTunnel/Route48 | transit BGP gratuit | 0 $ | dépend de ton ASN/préfixe | il faut déjà un ASN/préfixe |
| **Box/ISP** | `/56` ou `/48` si délégation ; SFR = `/56` mais box limitée à `/64` | 0 $ | 1–65 536 | la meilleure réputation ; voir §3bis pour SFR |

**Verdict (priorité gratuit)** :
1. **Box/ISP** : si ta box délègue un `/56` ou `/48`, c'est gratuit et sans
   VPS ; chez **Red by SFR**, la box fournie ne donne que 1–2 `/64` — il faut
   la remplacer pour obtenir le `/56` (voir §3bis).
2. **Oracle Always Free** : gratuit, 2–4 blocs, utile comme secours.
3. **BuyVM `/48`** : meilleur rapport capacité/prix **payant**, mais seulement
   en dernier recours.
4. **Linode `/56`** : plus fiable, mais payant et sur justification ; moins
   prioritaire dans une stratégie gratuite.

> **Préfixe routé vs on-link** : un `/48` routé (BuyVM) se configure avec
> `ip_nonlocal_bind` + route locale, sans NDP. Un préfixe on-link (souvent la
> box) nécessite `ndppd` ou un routeur qui fait proxy NDP pour répondre aux
> sollicitations de voisinage. C'est une complication en plus à prévoir.

### 3bis. Cas concret : Red by SFR fibre (ton forfait)

C'est le point le plus important pour ton cas : **SFR/RED a bien un `/56`, mais
la box fournie ne le délègue pas.**

Ce que disent les retours SFR/RED et le guide OPNsense :

- le réseau SFR/RED attribue un **`/56`** ; le guide OPNsense pour SFR/RED FTTH
  demande explicitement une **taille de délégation de préfixe `56`** ;
- mais la **box fournie** (NB6VAC, SFR 7/8, etc.) ne sait pas transmettre ce
  `/56` proprement : derrière elle, un routeur ne reçoit qu'un **`/64`** ;
- un utilisateur SFR le résume : la box « ne fait que passer un `/64` pour le
  réseau LAN normal, et un autre `/64` pour la DMZv6 » ;
- le forum RED indique aussi que la SFR 7 délivre un **next-hop en `/64`**.

| Configuration | Préfixe obtenu | Blocs `/64` | Buckets quota* |
|---|---|---:|---:|
| **Box Red/SFR conservée** | `/64` LAN (+ `/64` DMZv6) | 1–2 | **1–2** |
| Box + passthrough / next-hop | `/64` | 1 | **1** |
| **Box remplacée** par OPNsense/OpenWrt + DHCPv6-PD SFR | `/56` | **256** | **256** |
| Autre FAI avec `/48` | `/48` | 65 536 | 65 536 |

\* si le quota est bien par `/64`, ce que la Phase 0 doit confirmer.

> Ces chiffres viennent des retours 2022–2024 et du guide OPNsense ; selon le
> modèle/firmware de box, le comportement peut varier. Vérifie sur ta box :
> page IPv6, préfixe affiché, DMZv6/Next Hop, et `ip -6 addr` derrière.

**Combien d'« IP » ?**

- un `/64` contient **2^64 adresses** (≈ 1,84 × 10^19), mais ne vaut **qu'un
  bucket** si le quota est par `/64` ;
- un `/56` contient **256 `/64`** et **2^72 adresses** (≈ 4,72 × 10^21) ; pour
  le quota, c'est **256 buckets**, pas 2^72 ;
- un `/60` (si une box le donnait) = 16 `/64` = 16 buckets.

**Donc, avec ton forfait Red by SFR fibre à 21 €/mois :**

- **sans toucher à la box** : 1 bucket, éventuellement 2 avec la DMZv6 — pas de
  quoi remplacer 6 stations WARP ;
- **en remplaçant la box** par un routeur compatible (OPNsense/OpenWrt) et en
  faisant le DHCPv6-PD avec la vendor class SFR : **256 buckets** gratuits ;
- **en passthrough/next-hop** : probablement 1 `/64`, donc 1 bucket.

**Points de vigilance si tu remplaces la box** :

- téléphonie/TV à reconfigurer ; le guide OPNsense couvre le téléphone, pas la
  TV ;
- le préfixe `/56` peut changer après reboot/reconnexion : le gateway doit le
  relire et réallouer ;
- RED peut être en **CGNAT** selon la zone : l'IPv4 publique n'est pas garantie,
  l'IPv6 est la seule piste ;
- ne pas exposer le gateway sur Internet : loopback + SSH/WireGuard uniquement.

**Verdict** : garder la box = **1–2 buckets**, pas 256. Le `/56` gratuit existe,
mais il faut **remplacer la box** pour l'exploiter. Si tu ne veux pas remplacer
la box, Oracle free (2–4 `/64`) ou Proton Free (1 station) sont de meilleurs
replis gratuits que l'IPv6 de la box.

### 3ter. Forfaits 5G (exemple Free Mobile)

Un forfait 5G n'est **pas** un pool de blocs : c'est **une seule connexion**,
donc **un seul `/64`** en pratique (standard mobile), soit **1 bucket** si le
quota est par `/64`.

| Offre | Prix | Data | IPv6 | Utilité ici |
|---|---:|---|---|---|
| **Free 5G+** (hors Freebox) | 19,99 €/mois | 350 Go/mois en France, puis débit réduit ; 35 Go/mois à l'étranger | IPv6 public, IPv4 en CGNAT | **1 station de secours** ; data suffisante pour 18–90 Go/mois |
| **Free 5G+** (abonné Freebox) | 9,99 €/mois | illimité en France | idem | intéressant seulement si tu as déjà une Freebox |
| **Free Box 5G** | 29,99 €/mois la 1re année, puis 39,99 €/mois | 5G/4G illimitée | IPv6, IPv4 CGNAT | box fixe de secours, mais **payante** |

**Ce que ça donne concrètement :**

- **IPv6** : Free Mobile active IPv6 par défaut ; un appareil obtient une
  adresse publique. En pratique, une session mobile = **un `/64`**, pas un
  `/56` ; donc **1 bucket**, pas 256.
- **IPv4** : CGNAT, pas d'IPv4 publique dédiée ; l'IPv4 ne sert à rien pour le
  quota.
- **Rotation** : couper/relancer la connexion peut donner un autre `/64`, donc
  un autre bucket ; mais ce n'est **pas garanti** (le réseau peut réattribuer
  le même préfixe), et tu n'as **qu'un bloc à la fois**.
- **Data** : 350 Go/mois à plein débit, puis débit réduit. C'est suffisant pour
  le volume actuel (18–90 Go/mois), mais partagé par toutes les stations qui
  sortent par ce lien.
- **Parallélisme** : un seul `/64` signifie un seul bucket ; 6 stations sur le
  même hotspot ne gagnent rien. Pour 6 buckets simultanés, il faut 6 sources
  (WARP, VPS, SIMs), pas 6 clients derrière la même 5G.
- **Coût** : ce n'est **pas gratuit** ; 19,99 €/mois pour Free 5G+, ou 0 €
  marginal si tu as déjà un forfait 5G que tu peux partager.
- **CGU** : l'offre est prévue pour un usage mobile ; l'usage en routeur 5G
  fixe ou en pool de proxies peut être contraire aux conditions. À vérifier.

**Verdict** : la 5G Free est un bon **fallback de continuité** (une station,
data confortable, ASN différent de Cloudflare), pas un multiplicateur de blocs.
Elle ne remplace ni un `/56`/`/48`, ni Oracle pour la capacité.

### 3.4 VPN payants (beaucoup de `/32`)

| Fournisseur | Prix | Appareils | IPv6 | Intérêt |
|---|---:|---:|---|---|
| **Mullvad** | 5 €/mois | 5 | oui | beaucoup de serveurs, fiable ; rotation de serveurs = rotation de points de sortie |
| IVPN | ~6 €/mois | 2 | partiel | serveurs variés |
| AirVPN | ~7 €/mois | 5 | partiel | serveurs variés |
| PIA | ~3,33 $/mois | 10 | partiel | beaucoup de serveurs |
| Surfshark | ~2,49 $/mois | illimité | partiel | beaucoup de serveurs, IP très partagées |

**Verdict** : bon **fallback payant** si les IP du VPS sont bloquées. Mais les
IP de VPN commerciaux sont souvent déjà flagged (les IP NordVPN testées dans le
projet sortent en 429) et les limites d'appareils réduisent le parallélisme.
Pour 6 stations, Mullvad (5 appareils) est juste ; Surfshark est plus souple
mais moins prévisible.

### 3.5 Proxies résidentiels / mobile

- Bright Data, Oxylabs, Smartproxy, IPRoyal, Webshare, SOAX…
- Beaucoup d'IP, rotation par session, bonne réputation.
- **Coût au Go** élevé pour 18–90 Go/mois ; conditions d'utilisation souvent
  incompatibles avec le contournement de quota.
- Webshare a un petit free tier (10 proxys, 1 Go) : utile pour tester, pas pour
  la production.

**Verdict** : dernier recours, si le VPS et les VPN payants sont bloqués.

### 3.6 Listes gratuites, Tor, VPN Gate

- Listes GitHub : [`iplocate/free-proxy-list`](https://github.com/iplocate/free-proxy-list),
  [`monosans/proxy-list`](https://github.com/monosans/proxy-list),
  [`TheSpeedX/PROXY-List`](https://github.com/TheSpeedX/PROXY-List).
- Tor : ~1 000 exits, souvent bloqués, latence élevée.
- VPN Gate : relais publics, **usage académique**, sécurité inconnue.

**Verdict** : **à éviter** pour du trafic API. IP instables, MITM possible,
déjà flagged, aucun SLA. Si tu les utilises quand même : jamais de cookie ou de
clé, validation stricte, quota séparé.

### 3.7 Tableau comparatif

Notes de 1 (mauvais) à 5 (excellent) ; « risque » élevé = plus sûr. Ce tableau
compare la **capacité technique** ; pour la contrainte « gratuit d'abord », le
classement des sections 0bis et 4.1 s'applique.

| Option | Blocs | Coût | Fiabilité | Automatisation | Risque CGU | Effort | Total pondéré* |
|---|---:|---:|---:|---:|---:|---:|---:|
| **BuyVM `/48`** | 5 | 5 | 4 | 4 | 4 | 3 | **4,50** |
| Linode `/56` | 4 | 4 | 5 | 4 | 4 | 3 | 4,25 |
| Box/ISP `/56` | 4 | 5 | 4 | 4 | 4 | 4 | 4,20 |
| Mullvad | 3 | 3 | 5 | 4 | 4 | 3 | 3,75 |
| Oracle free | 3 | 5 | 3 | 3 | 4 | 3 | 3,50 |
| Proton free | 2 | 5 | 4 | 3 | 4 | 2 | 3,45 |
| WARP + warp-minimal | 2 | 5 | 3 | 4 | 2 | 2 | 3,15 |
| Windscribe free | 2 | 4 | 3 | 4 | 3 | 2 | 3,05 |
| VPN Gate | 2 | 5 | 1 | 1 | 1 | 2 | 2,10 |
| Listes gratuites | 2 | 5 | 1 | 1 | 1 | 2 | 2,10 |

\* Pondération : blocs 30 %, fiabilité 25 %, coût 20 %, automatisation 15 %,
risque 10 %.

### 3.8 Les 12 dépôts « OpenCode Unlimited » : ce qu'ils prouvent

Analyse détaillée, fiches par dépôt et chemins de code :
[`docs/REPOS_12_UNLIMITED_2026-11-06.md`](docs/REPOS_12_UNLIMITED_2026-11-06.md).

| Dépôt | Apport principal | Verdict |
|---|---|---|
| `Tazmyan/OpenCode-Unlimited` | Ledger `/64`/`/32`, quarantaine proxy, probe avant application | **Référence ledger/UX** |
| `ebouther/opencode-unlimited-proxy` | 4 gates mesurés, WARP IPv6 source routée, dédup d'egress réel | **Référence principale** |
| `Parsa013Ah/OpenCode-Unlimited-ProxyPool` | Proxies publics/perso + V2Ray/Xray, dashboard | Repli proxy, pas capacité |
| `coder-nguoi-tay/opencode-unlimited` | Dédup d'IP 4G, parsing du cooldown, auto-resume | Repli 4G, dédup utile |
| `Netherstar44/OpenCode_Unlimited` | Reset réseau/device ID, purge du contexte | **À éviter** (identité, destructif) |
| `d1alect-tech/opencode-zen-unlimited` | Taxonomie 429/401/403/5xx, sticky, fail-closed | **Référence erreurs/sticky** |
| `CarbonWalls/opencode-unlimited` | Tor/proxy/WARP, probe puis `--continue` | Repli simple |
| `muhamad-idris/opencode_unlimited` | Proxy aléatoire + blacklist + fallback modèle | Prototype, à rejeter |
| `mulqizamzam/opencode-unlimited-proxy` | Copie de `ebouther` | Doublon ; utiliser l'amont |
| `Nirusaki-Malaal/OpenCode-Unlimited` | `usage`/`block`, re-id de session, pool cross-platform | **Référence cross-platform** |
| `ngynlaam/zen-unlimited` | Park quota, dédup temporel, preuve négative sur la rotation | **Référence reset/preuve** |
| `hansputera/opencode-multi-agents` | Axe compte/clé explicite, sticky, ProtonVPN, métriques | **Référence architecture** |

**Les six conclusions qui changent le plan** :

1. **Aucun dépôt ne rend le quota illimité.** Tous tournent autour du même
   levier : un bloc d'egress neuf, une identité de requête, un compte/clé ou un
   fournisseur. Le seul multiplicateur gratuit propre est le **nombre de blocs
   distincts** ; le reste est de la continuité ou de l'optimisation.
2. **Le gate 403 n'est pas le quota 429.** `ebouther` mesure quatre conditions
   de forme (UA ≥ 1.18.0, session canonique, outil `read`, `stream: true`), plus
   le bloc `<env>`. Un 403 ne se règle pas en tournant l'IP ; il faut corriger
   le wire ou changer de pays.
3. **Une rotation WARP n'est pas une preuve de quota neuf.** `ngynlaam` mesure
   un 429 qui revient 3 s après un changement d'IPv6. Il faut mesurer l'egress
   réel, dédupliquer par `/64`, et faire un probe 200 avant de compter le bloc.
4. **Le device ID, la session et le projet ne sont pas des leviers de quota.**
   `Tazmyan` et `Nirusaki` le disent ; l'audit [R] a testé trois sessions
   fraîches sur une IP 429 : 3 × 429. Ces rotations nettoient un banner, pas un
   bucket.
5. **Le sticky et le contexte comptent.** Garder le même egress et le même ID
   de session préserve le cache ; une session longue rejoue son contexte et
   consomme beaucoup plus de requêtes. C'est une réduction du gaspillage, pas
   un multiplicateur.
6. **Il existe peut-être un axe compte/clé.** `hansputera` code un
   `isPublicTier` qui traite le tier public comme account-limited ; `coder`
   parle d'un budget global. Ces affirmations ne sont pas reproduites ici. Si
   un bloc frais reste 429, tester l'axe **modèle / compte / clé** avant
   d'ajouter des proxies ; ne pas empiler des comptes par défaut (CGU, coût,
   suspension).

**Ce que j'intègre** : ledger et dédup (§1.2bis, §1.3), park reset (§1.3bis),
sticky/contexte (§1.3ter), probe 403 vs 429 (Phase 0), taxonomie d'erreurs et
retrait 429/403 (Phase 1), dédup d'egress réel et WARP IPv6 source routée
(Phase 1bis/2), métriques par bloc (Phase 4).

**Ce que je rejette** : rotation sur 403, spoof de device ID, proxies publics
pour des credentials, gateway sans auth, pool de comptes multiples comme
stratégie gratuite, et le mot « unlimited » sans mesure.

---

## 4. Recommandation

### 4.1 Classement (WARP reste primaire)

1. **WARP** — gratuit, data illimitée, **4–5 blocs utiles** ; c'est la base, pas
   le fallback.
2. **Client WARP alternatif** (`MicroWARP`, `usque`, `warp-minimal`) — 0 €,
   même pool, mais continuité si le conteneur ou la rotation casse.
3. **IPv6 box/ISP** — 0 € ; chez **Red by SFR**, la box fournie ne donne que
   **1–2 buckets** ; le `/56` (256 buckets) exige de **remplacer la box** par
   OPNsense/OpenWrt. Ailleurs, `/56` ou `/48` peut donner 256 à 65 536 blocs.
4. **Oracle Always Free** — 0 €, 2–4 blocs, pour tenir quelques stations.
5. **Proton Free** — 0 €, data illimitée mais **un seul appareil** ; bon secours
   pour une station.
6. **Windscribe/Privado Free** — 0 €, 10 Go/mois ; urgence seulement.
7. **5G Free** — 19,99 €/mois, **1 `/64`** par session ; bon fallback mobile,
   mais pas un pool de blocs.
8. **BuyVM `/48`** — 24 $/an, 65 536 blocs ; **dernier recours payant** si les
   options gratuites sont absentes ou épuisées.
9. **VPN Gate / listes / Tor** — à éviter.

**Variante optionnelle (Linux)** : le moteur `opencode2api` du dépôt
`ebouther` peut servir de **sidecar** gratuit sur un hôte Linux avec un `/64`
routé. Il combine les quatre gates, un pool WARP IPv6 dynamique, la dédup
d'egress et le retrait 429/403. Ce n'est **pas** un drop-in Windows, le moteur
est GPL-3.0, et il faut changer le mot de passe de la web UI. À benchmarker
avant de l'utiliser comme repli ; ne pas l'exposer publiquement.

### 4.2 Pourquoi BuyVM reste le fallback payant

BuyVM n'est **pas** la cible : c'est l'assurance si les replis gratuits
(box/ISP, Oracle, Proton) ne suffisent pas.

- **Capacité** : 65 536 `/64`, soit 65 536 buckets potentiels. Même en
  n'utilisant que 6–12 blocs, on ne réutilise jamais un bloc épuisé.
- **Coût** : 24 $/an, soit ~0,00037 $ par bloc/an ; le meilleur ratio.
- **Bande passante** : « unmetered » sur le plan 512, ce qui absorbe les
  18–90 Go/mois sans surprise.
- **Contrôle** : gateway SOCKS5 maison, rotation en secondes, pas de Docker à
  recréer, pas de throttle Cloudflare.
- **Pérennité** : un VPS ne dépend pas d'un pool d'enregistrements gratuits.

**Risque principal** : BuyVM ne détaille pas le `/48` sur sa page produit, mais
la documentation communautaire (IRCNow, LowEndTalk) l'affirme et le panneau
Stallion expose « Routed Subnets ». Il faut le confirmer à la commande. Si
c'était seulement un `/64`, BuyVM resterait un bon VPS mais n'apporterait qu'un
bloc : dans ce cas, passer à Linode `/56` ou à l'ISP.

### 4.3 Pourquoi ne pas simplement empiler WARP

- Chaque nouveau conteneur consomme un enregistrement Cloudflare ; au-delà de
  ~4–6, le throttle rend l'ajout aléatoire.
- Les `/64` sont réutilisés ; le ledger doit écarter les blocs épuisés, ce qui
  réduit encore le pool utile.
- La rotation par recréation est lente et fragile ; un pool de 6 conteneurs
  coûte ~300 Mo de RAM.
- WARP reste la **base** ; empiler des conteneurs ne remplace pas un vrai repli
  de capacité quand le pool Cloudflare est épuisé.

### 4.4 La seule option vraiment conforme

La seule alternative qui ne contourne pas les limites du free tier est un
**abonnement payant OpenCode Go** (ou un modèle local Ollama/vLLM). Le reste de
ce plan est de l'ingénierie de contournement : elle peut violer les CGU
d'OpenCode, de Cloudflare WARP ou du fournisseur VPN, et exposer à une
suspension. À décider en connaissance de cause.

---

## 5. Architecture du fallback VPS (option payante)

> **WARP reste le chemin par défaut.** Ce gateway n'est activé que pour 1 à 2
> stations quand les replis gratuits (box/ISP, Oracle, Proton) sont absents ou
> insuffisants. Il ne doit pas absorber tout le trafic.

### 5.1 Schéma

```text
Windows (opencode-proxy)
  │
  ├─ SOCKS5 127.0.0.1:11090  ──┐
  ├─ SOCKS5 127.0.0.1:11091  ──┤
  ├─ SOCKS5 127.0.0.1:11092  ──┤   tunnel SSH / WireGuard
  └─ ...                       │
                               ▼
                 Linux VPS (BuyVM) — préfixe 2001:db8:abcd::/48
                 │
                 ├─ ipv6-egress-gateway (systemd)
                 │    ├─ allocateur /64 (SQLite)
                 │    ├─ listener 127.0.0.1:11090 → source 2001:db8:abcd:0001::2
                 │    ├─ listener 127.0.0.1:11091 → source 2001:db8:abcd:0002::2
                 │    ├─ listener 127.0.0.1:11092 → source 2001:db8:abcd:0003::2
                 │    └─ API POST /rotate, GET /health
                 │
                 └─ net.ipv6.ip_nonlocal_bind=1
                    ip -6 route add local 2001:db8:abcd::/48 dev lo
```

### 5.2 Composants

1. **Gateway IPv6** (`scripts/ipv6_egress_gateway.py`) :
   - un listener SOCKS5 par station, bindé sur `127.0.0.1:<port>` ;
   - chaque listener sort avec une adresse source fixe dans son `/64` ;
   - résolution DNS forcée en `AF_INET6` pour les endpoints `ipv6: true`
     (sinon le gateway peut sortir en IPv4) ;
   - API locale pour allouer, tourner, libérer, health ;
   - SQLite pour survivre aux redémarrages ;
   - drain des connexions avant rotation.
2. **Allocateur de blocs** :
   - réserve un `/64` par label ;
   - évite les doublons et les blocs épuisés du jour ;
   - garde 2 blocs chauds d'avance ;
   - ne réutilise jamais un bloc marqué épuisé avant le reset UTC ;
   - si la Phase 0 montre que le quota est par **adresse** et non par `/64`,
     faire tourner l'interface ID dans le même `/64` (le reste de l'architecture
     ne change pas).
3. **Adaptateur côté proxy** (`free/ipv6_provider.py`) :
   - appelle l'API du gateway ;
   - retourne un `WarpRotationResult` (même contrat que WARP) ;
   - met à jour `egress_ip`, `block`, `current_ip` ;
   - `_apply` existant persiste dans `config.yaml`.
4. **Pool** :
   - `Socks5Endpoint` gagne un champ `provider` (`warp`, `vps-ipv6`,
     `gluetun`, `static`) ;
   - `_request_warp_rotation` devient `_request_egress_rotation` et dispatche ;
   - les règles 429/quota/duplicate/trial_dead restent identiques.
5. **GUI/dashboard** :
   - affiche le provider, le bloc, l'état du gateway ;
   - boutons « tester » et « tourner » par endpoint ;
   - alerte si le gateway est down ou si deux endpoints partagent un bloc.

**Alternative à faible code** : `3proxy` peut porter les listeners SOCKS5
(`socks -p11090 -e<source>`) et un petit script peut régénérer/recharger la
config à chaque rotation. Le gateway custom n'apporte que l'API de rotation, le
drain propre et l'allocateur ; c'est utile si tu veux 6–12 stations sans
redémarrer de processus.

### 5.3 Flux de rotation

Ce flux est celui du **fallback VPS** : en amont, le pool a déjà tenté les
rotations WARP et les replis gratuits.

```text
429 / quota épuisé / bloc dupliqué
        │
        ▼
pool._request_egress_rotation(endpoint, cause)
        │
        ▼
Ipv6GatewayProvider.rotate(endpoint)
        │  POST /rotate {label}
        ▼
gateway: drain listener → libère l'ancien /64 → alloue un /64 frais
        → rebind le même port sur la nouvelle source → répond {ip, block}
        │
        ▼
endpoint.egress_ip = ip ; endpoint.block = block
pool._apply → ledger + config persist
        │
        ▼
probe post-rotation (dernier modèle réel) → 200/429
```

La rotation ne recrée **aucun conteneur** : quelques secondes, pas minutes.

### 5.4 Pourquoi un gateway « sticky par /64 »

Un proxy qui change d'adresse **à chaque requête** (comme
`zu1k/http-proxy-ipv6-pool` ou `NyxProxy`) casse le ledger : le pool ne sait
plus quelle IP a servi quelle requête, et le quota par bloc devient
incontrôlable. Ici, chaque station garde **une adresse source stable dans son
`/64`** ; c'est le `/64` qui change à la rotation, pas l'adresse à chaque
requête. Le ledger reste juste, et la session peut rester collante.

### 5.5 Intégration au pool existant

Changements minimaux :

| Fichier | Changement |
|---|---|
| `free/pool.py` | champs `provider`, `block_key`, `quota_axis`, `reset_at` sur `Socks5Endpoint` ; dispatch de rotation ; sticky session/egress ; `set_socks5_proxies` lit `provider` |
| `free/errors.py` | **nouveau** : taxonomie `GATE_403`, `QUOTA_429`, `RATE_LIMIT_429`, `TRANSIENT_5XX`, `ACCOUNT_OR_GLOBAL` |
| `free/warp_rotation.py` | extraire une interface `EgressProvider` ; `WarpDockerProvider` = implémentation actuelle ; dédup d'egress mesuré |
| `free/fallback.py` | **nouveau** : politique de bascule, seuils, cooldown, retour WARP, pas de direct silencieux |
| `free/gluetun_provider.py` | **nouveau** : Proton/Windscribe/Privado via gluetun |
| `free/ipv6_provider.py` | **nouveau** : client HTTP du gateway, rotation, health |
| `scripts/ipv6_egress_gateway.py` | **nouveau** : gateway SOCKS5 + allocateur |
| `config.yaml` | section `egress_providers`, `fallback.order`, entrées `provider` |
| `dashboard/api.py` | statut provider/bloc/gateway, endpoints de test/rotation |
| `tests/` | tests de dispatch, d'allocation, de rotation, de doublons, de fallback |

Le contrat `WarpRotationResult` (label, ok, old_ip, new_ip, block, attempts,
reason) est réutilisable tel quel ; on peut le renommer plus tard en
`EgressRotationResult` sans casser les tests.

### 5.6 Exemple de configuration

```yaml
ip_rotation:
  proxy_mode: socks5
  fallback:
    order: [warp, micro_warp, isp_ipv6, oracle, proton, windscribe, vps_ipv6]
    max_fallback_stations: 2
    probe_interval_s: 300
    recovery_successes: 2
    cooldown_s: 900
  egress_providers:
    warp:
      type: warp_docker
      enabled: true
    micro_warp:
      type: warp_http
      enabled: false          # continuité, même pool
    proton:
      type: gluetun
      enabled: false          # secours gratuit, 1 appareil
    vps_ipv6:
      type: ipv6_gateway
      enabled: false          # dernier recours payant
      gateway_url: http://127.0.0.1:8099
      prefix: 2001:db8:abcd::/48
      port_base: 11090
      token_env: IPV6_GATEWAY_TOKEN
  socks5_proxies:
    - host: 127.0.0.1
      port: 11090
      label: vps-1
      ipv6: true
      provider: vps-ipv6
      managed: true
      egress_ip: ''
      block: ''
```

Le token ne doit pas être en clair dans `config.yaml` : variable
d'environnement ou fichier `credentials.env`.

---

## 6. Plan d'implémentation

> **Lecture priorité gratuit** : pour un fallback 100 % gratuit, faire Phase 0,
> Phase 1, Phase 1bis et Phase 5. Les phases 2–4 ne concernent que le fallback
> VPS payant, à n'activer que si les options gratuites ne suffisent pas.

### Phase 0 — Validation (0,5–1 jour)

| # | Tâche | Livrable |
|---|---|---|
| P0.0 | Probe **403 vs 429** : gates (UA, session canonique, `read`, `stream`, `<env>`) et quota | note + `scripts/gate-probe` adapté |
| P0.1 | Vérifier la délégation IPv6 de la box/ISP (`/56` ? `/48` ?) | note |
| P0.2 | Test **même `/64` vs `/64` neuf vs direct vs WARP**, par modèle | `docs/QUOTA_GRANULARITY_2026-11-06.md` |
| P0.3 | Mesurer les octets/requête et le contexte rejoué sur 7 jours | tableau |
| P0.4 | Contrôle de rotation : 429 sur A → rotation B → probe 200/429 ; noter `Retry-After`, reset, heure UTC | note |
| P0.5 | Si les gratuits ne suffisent pas : confirmer le `/48` BuyVM | capture |
| P0.6 | Décider : repli gratuit, VPS payant, ou direct | go/no-go |

**Règles du test** :

- un **403** ne doit pas déclencher de rotation ni marquer un bloc épuisé ;
- un **429** doit être classé : `FreeUsageLimitError` (quota journalier),
  `RateLimitError` (fenêtre courte), ou compte/clé ;
- tester **par modèle** : le quota de `deepseek-v4-flash-free` n'est peut-être
  pas le même que celui de `beam-free` ou `muse-spark` ;
- garder un **contrôle** : si la session réelle marche pendant le test, le test
  mesure autre chose ;
- le probe de gates consomme du quota (environ 8–10 requêtes) : le faire une
  fois, pas en boucle.

**Critère de sortie** : le quota est par `/64` ou par adresse **pour le modèle
utilisé**, l'axe compte/clé est écarté ou identifié, et au moins un repli
**gratuit** est identifié (box/ISP, Oracle ou Proton) ; à défaut, un VPS payant
est chiffré.

### Phase 1 — Abstraction provider (1–2 jours)

| # | Tâche | Fichiers |
|---|---|---|
| P1.1 | Ajouter `provider`, `block_key`, `quota_axis`, `reset_at` à `Socks5Endpoint` + config | `free/pool.py`, `config.yaml` |
| P1.2 | Extraire `EgressProvider` (protocole) | `free/egress_provider.py` |
| P1.3 | `WarpDockerProvider` = wrapper de `WarpRotator` | `free/warp_rotation.py` |
| P1.4 | Dispatch `_request_egress_rotation` + taxonomie `GATE_403` / `QUOTA_429` / `RATE_LIMIT_429` / `TRANSIENT_5XX` | `free/pool.py`, `free/errors.py` |
| P1.5 | Sticky session/egress : ne pas tourner sur chaque requête ; conserver le même bloc tant qu'un probe 200 passe | `free/pool.py` |
| P1.6 | Dédup d'egress **mesuré** après rotation ; ne pas croire le rapport du rotateur | `free/warp_rotation.py` |
| P1.7 | Tests de non-régression WARP + 403/429 + doublons | `tests/test_warp_rotation.py`, `tests/test_quota_errors.py` |

**Critère** : aucune régression, tous les tests WARP verts, config existante
lisible sans migration ; un 403 ne pénalise aucun bloc ; un 429 quota parke ou
tourne selon la politique ; deux adresses du même `/64` sont un seul bloc.

### Phase 1bis — Fallback gratuit (1–2 jours)

| # | Tâche | Détail |
|---|---|---|
| P1b.1 | Benchmark `MicroWARP` et `usque` | même pool, mais continuité si un conteneur casse |
| P1b.2 | Vérifier box/ISP et Oracle | noter préfixe, nombre de `/64`, setup |
| P1b.3 | Proton Free via gluetun | 1 station, data illimitée, test 24 h |
| P1b.4 | Windscribe/Privado via gluetun | 10 Go/mois, réservé à l'urgence |
| P1b.5 | Implémenter la politique de bascule | déclencheurs, ordre, cooldown, retour WARP |
| P1b.6 | Benchmark `opencode2api` en sidecar Linux | gates + pool WARP IPv6 + dédup ; GPL-3.0 ; localhost seulement |
| P1b.7 | Dédup + park reset dans le pool | mesurer l'egress réel, ledger `/64`, probe après reset |

**Critère** : WARP reste primaire ; une station bascule sur un repli gratuit
(Proton ou MicroWARP), puis revient à WARP après 2 succès ; aucun bloc fallback
réutilisé par WARP ; **aucun fallback direct silencieux** (si le pool est vide,
le service échoue clairement ou parke, il ne sort pas en direct sans le dire).

### Phase 2 — Gateway IPv6 MVP (2–4 jours, optionnel)

| # | Tâche | Détail |
|---|---|---|
| P2.1 | Serveur SOCKS5 asyncio | handshake, CONNECT, bind source, relay |
| P2.2 | Allocateur `/64` + SQLite | label → block → port → source |
| P2.3 | API `/health`, `/allocate`, `/rotate`, `/release` | token, JSON |
| P2.4 | Drain + rebind même port | rotation sans changer le port |
| P2.5 | Setup VPS | `ip_nonlocal_bind`, route locale, systemd, nftables |
| P2.6 | Tunnel SSH/WireGuard | gateway accessible seulement en local |
| P2.7 | Tests unitaires + intégration | curl via proxy, famille IPv6, bloc |
| P2.8 | Dédup + retrait 429/403 | mesurer l'egress réel, retirer un bloc sur quota, pas sur 5xx |
| P2.9 | Fail-closed | pas de direct silencieux si le pool est vide |

**Critère** : 1 station, 3 rotations, 3 `/64` distincts, 3 × 200, rotation
< 10 s ; 0 doublon ; un 403 ne retire pas de bloc.

### Phase 3 — Intégration fallback (3–5 jours)

| # | Tâche | Détail |
|---|---|---|
| P3.1 | `Ipv6GatewayProvider.rotate` | appelle `/rotate`, retourne `new_ip`/`block` |
| P3.2 | `refresh_egress` vérifie la famille et le `/64` | rejette une IP hors bloc |
| P3.3 | Déclencheurs 429/quota/duplicate/trial_dead | mêmes règles que WARP |
| P3.4 | `rotate_exhausted_managed` inclut VPS | scan + choix |
| P3.5 | GUI statut + test/rotation | `dashboard/api.py` |
| P3.6 | Tests de doublons et de persistance | `tests/test_ipv6_provider.py` |

**Critère** : 1 station VPS + WARP primaire, 24 h de soak, 0 doublon,
rotation < 10 s, retour à WARP propre.

### Phase 4 — Scale et durcissement (2–3 jours, optionnel)

| # | Tâche |
|---|---|
| P4.1 | 1–2 stations VPS de secours + 2 blocs chauds |
| P4.2 | Allocateur : séquentiel/aléatoire, jamais de bloc épuisé |
| P4.3 | Métriques : **blocs distincts**, blocs épuisés, 429 par modèle/axe, 403 gate, rotations, doublons, temps de reset |
| P4.4 | Failover WARP/Direct + circuit breaker, sans fallback direct silencieux |
| P4.5 | Sécurité : token, firewall, SSH, fail2ban, updates |
| P4.6 | Runbook et rollback |
| P4.7 | Audit trimestriel des gates (UA, session, `read`, `stream`, `<env>`) |

**Critère** : 7 jours de soak, 99 % de disponibilité, < 5 % de 429, aucun proxy
ouvert, bande passante < 1 To/mois, WARP toujours majoritaire.

### Phase 5 — Providers optionnels (2–3 jours)

- `GluetunProvider` pour Proton Free, Windscribe, Privado et Mullvad.
- **Proton d'abord** : data illimitée, un appareil, donc une station de secours.
- Windscribe/Privado : réservés aux coupures de quelques heures/jours.
- Mullvad : seulement si les IP gratuites sont bloquées et que tu acceptes le
  coût ; ce n'est pas prioritaire.

### Tableau récapitulatif

| Phase | Durée | Risque | Valeur |
|---|---:|---|---|
| 0 — Validation | 0,5–1 j | faible | **go/no-go, à faire en premier** |
| 1 — Abstraction | 1–2 j | faible | prépare tout le reste |
| 1bis — Fallback gratuit | 1–2 j | faible | **WARP + Proton/MicroWARP, 0 €** |
| 2 — Gateway VPS | 2–4 j | moyen | optionnel, capacité de secours |
| 3 — Intégration fallback | 3–5 j | moyen | 1 station VPS + WARP |
| 4 — Scale VPS | 2–3 j | moyen | optionnel, 1–2 stations |
| 5 — Providers optionnels | 2–3 j | faible | Proton/Windscribe/Mullvad |

---

## 7. Tests et critères d'acceptation

### 7.1 Unitaires

- allocation d'un `/64` dans un `/48` : pas de chevauchement, pas de réseau
  `::`, pas de multicast ;
- normalisation ledger : deux adresses du même `/64` = même bucket ;
- rotation : nouveau bloc, ancien libéré, port conservé ;
- doublons : deux labels ne peuvent pas avoir le même `/64` ;
- config : `provider` absent → comportement WARP historique ;
- **403 vs 429** : un 403 ne marque pas le bloc épuisé et ne déclenche pas de
  rotation ; un 429 quota le marke et parke/rotate selon la politique ;
- **reset** : un bloc épuisé est parké jusqu'à `reset_at`, puis un seul probe ;
- **sticky** : deux requêtes d'une même session gardent le même egress ;
- **fallback** : pool vide → erreur explicite, pas de direct silencieux ;
- **sécurité** : aucun proxy public n'est utilisé pour un credential.

### 7.2 Intégration

- gateway : `curl` via SOCKS5, `api64.ipify.org` renvoie une adresse du bloc ;
- famille : un endpoint `ipv6: true` ne sort jamais en IPv4 ;
- rotation : 3 rotations, 3 blocs distincts, 3 × 200 ;
- panne : gateway down → endpoint bad-marké, WARP prend le relais ;
- redémarrage : le gateway recharge les allocations SQLite ;
- **gate probe** : le baseline passe, les contrôles 403 échouent comme prévu ;
  un 403 ne change pas le ledger ;
- **dédup** : deux rotations qui retombent sur le même `/64` ne créent pas deux
  buckets ;
- **compte/clé** : un 429 sur un bloc frais est classé `ACCOUNT_OR_GLOBAL`, pas
  traité comme un simple bloc épuisé.

### 7.3 Live

| Test | Critère |
|---|---|
| 1–2 blocs fallback × 10 requêtes | 10–20 × 200 |
| WARP primaire | > 80 % du trafic reste sur WARP |
| 24 h de trafic | disponibilité > 99 %, 429 < 5 % |
| 7 jours | pas de fuite de blocs, pas de doublon |
| rotation sous charge | < 10 s, pas de coupure > 30 s |
| contrôle 429 → rotation | noter si le probe suivant est 200 **ou** 429 ; ne pas conclure sur un seul essai |
| même `/64` vs `/64` neuf | le `/64` neuf doit donner 200 pour prouver le bucket par bloc |
| 403 gate vs 429 quota | un 403 ne doit jamais être compté comme un bloc épuisé |
| modèle par modèle | le résultat doit être répété sur au moins 2 modèles free |
| rollback | WARP seul redémarre en < 2 min |

### 7.4 Sécurité

- scan externe : seuls SSH (et éventuellement WireGuard) exposés ;
- gateway en `127.0.0.1` uniquement ;
- API token obligatoire ;
- aucun secret dans les logs ;
- pas de proxy ouvert (test `curl` depuis une autre machine).

---

## 8. Coûts

| Poste | Coût | Commentaire |
|---|---:|---|
| BuyVM 512 | **24 $/an** | 512 Mo, 10 Go SSD, unmetered, 1 IPv4 ; `/48` à confirmer |
| Linode 1 Go | ~5 $/mois | `/56` sur justification |
| Oracle Always Free | 0 $ | 2 VM, 10 To/mois, 2–4 `/64` |
| Hetzner CX22 | ~3,79 €/mois | 1 `/64` |
| Proton Free | 0 $ | illimité, 1 appareil |
| Windscribe Free | 0 $ | 10 Go/mois, 10 pays |
| Privado Free | 0 $ | 10 Go/mois |
| Mullvad | 5 €/mois | 5 appareils, IPv6 |
| Free 5G+ | 19,99 €/mois | 350 Go, 1 `/64` par session |
| Free Box 5G | 29,99–39,99 €/mois | data illimitée, 1 `/64` |
| WARP | 0 $ | source principale |

**Scénario recommandé (gratuit)** : WARP + `MicroWARP`/`usque` + Proton Free +
vérification box/ISP. Coût total : **0 €**. Oracle free si tu as besoin de 2–4
blocs supplémentaires. La 5G Free (19,99 €/mois) n'est intéressante que si tu
as déjà un forfait 5G ou si tu veux un secours mobile ; ce n'est pas gratuit.
BuyVM (24 $/an) seulement si les options gratuites sont absentes ou épuisées.

Prix et stocks vérifiés le 2026-11-06 ; à reconfirmer au moment de la commande
(BuyVM est souvent en rupture sur les petits plans).

---

## 9. Risques et mitigations

| Risque | Probabilité | Impact | Mitigation |
|---|---|---|---|
| Le quota n'est pas par `/64` | moyenne | élevé | Phase 0 avant achat ; si ASN, pivot résidentiel |
| Les replis gratuits ne suffisent pas | moyenne | moyen | WARP primaire ; BuyVM seulement en dernier recours |
| Proton Free limité à 1 appareil | certaine | moyen | une seule station fallback ; Windscribe/Privado en relais |
| 5G = 1 `/64`, pas un pool | certaine | moyen | fallback 1 station ; reconnecter ou multiplier les SIMs |
| 5G cap 350 Go/mois | moyenne | faible | surveiller ; le volume actuel est 18–90 Go/mois |
| BuyVM ne donne pas de `/48` | faible | élevé | confirmer à la commande ; fallback Linode `/56` |
| Les IP du VPS sont flagged | moyenne | élevé | tester 1 bloc avant scale ; changer de région/provider |
| Le VPS est en rupture de stock | moyenne | moyen | commander tôt ; fallback Oracle/Linode |
| Bande passante insuffisante | faible | moyen | BuyVM unmetered ; surveiller |
| CGU OpenCode/Cloudflare | élevée | élevé | lire les termes ; volume raisonnable ; ne pas revendre |
| Gateway mal sécurisé | faible | critique | localhost + SSH/WireGuard + token + firewall |
| Panne du gateway | moyenne | moyen | systemd, health, fallback WARP |
| Bloc réutilisé par erreur | moyenne | moyen | allocateur + ledger + test doublons |
| IP résidentielle dynamique | moyenne | faible | si ISP, gérer le changement de préfixe |
| **Rotation WARP qui ne lève pas le 429** | **élevée** | **élevé** | contrôle direct vs WARP ; park reset ; ne pas promettre « unlimited » |
| **Quota compte/clé ou global** | moyenne | élevé | tester par modèle ; si bloc frais = 429, ne pas empiler les proxies |
| **Device ID / session spoof inutile** | élevée | faible | ne pas l'implémenter comme levier quota |
| **Proxy public / MITM** | élevée | critique | interdire les credentials ; WARP/VPS only |
| **Moteur tiers GPL / image Docker** | moyenne | moyen | épingler le digest, auditer, localhost seulement |
| Coût caché (support, IP supplémentaires) | faible | faible | budget 0–24 $/an ; gratuit d'abord |

### Points de vigilance CGU

- **OpenCode free** : le contournement de quota par rotation d'IP peut être
  considéré comme un abus ; risque de blocage du compte/workspace.
- **Comptes/clés multiples** : empiler des comptes pour multiplier le quota est
  généralement contraire aux CGU et peut entraîner une suspension ; ne pas
  utiliser de clés `zent-` trouvées dans un dépôt public.
- **Cloudflare WARP** : l'usage comme pool de proxies n'est pas l'usage
  personnel typique ; les enregistrements peuvent être throttlés ou bloqués.
- **VPN gratuits** : caps, limitations d'appareils, journaux ; ne pas envoyer
  de données sensibles.
- **Listes de proxies/Tor/VPN Gate** : MITM possible ; jamais de credentials.

Ce plan est technique, pas juridique : vérifie les CGU applicables avant de
déployer.

---

## 10. Décision demandée

WARP reste la base. Choisir le **niveau de repli** à ajouter :

- **F0. WARP + `MicroWARP`/`usque`** (recommandé d'abord) — 0 €, continuité.
- **F1. WARP + Proton Free** — 0 €, une station de secours, data illimitée.
- **F2. WARP + Oracle Always Free** — 0 €, 2–4 blocs, setup plus long.
- **F3. WARP + box/ISP** — 0 € ; chez Red by SFR, 1–2 buckets avec la box,
  256 seulement si tu la remplaces.
- **F4. WARP + BuyVM `/48`** — 24 $/an, dernier recours de capacité.
- **F5. WARP + 5G Free** — 19,99 €/mois, 1 `/64` = 1 bucket, fallback mobile.
- **F6. WARP seul + monitoring** — 0 €, pas de repli de capacité.
- **F7. WARP + sidecar `opencode2api` (Linux)** — 0 € si tu as un hôte Linux
  avec `/64` routé ; gates + pool WARP + dédup ; GPL-3.0 ; optionnel.
- **F8. WARP + pool de clés légitimes** — 0 € marginal **si tu as déjà**
  plusieurs clés ; sinon CGU/coût/suspension ; non recommandé par défaut.
- **F9. Remplacer la box SFR pour obtenir un `/56`** — coût du routeur ;
  256 buckets potentiels ; à valider en Phase 0 avant achat.

**Prochaine action recommandée** : Phase 0 (probe **403 vs 429**, test de
granularité **par modèle**, contrôle de rotation WARP) **et** Phase 1bis
(MicroWARP/Proton + vérification box/ISP). Lire d'abord
[`docs/REPOS_12_UNLIMITED_2026-11-06.md`](docs/REPOS_12_UNLIMITED_2026-11-06.md) :
aucun dépôt ne donne un quota illimité, et certains « leviers » (device ID,
session) sont des faux leviers. Ne pas commander de VPS avant d'avoir vérifié
les options gratuites et validé que le quota change bien avec le `/64`.

---

## 11. Sources

### Dépôt et mesures locales

- [`docs/IPV6_WARP_STATION_2026-11-06.md`](docs/IPV6_WARP_STATION_2026-11-06.md)
  — 5 blocs WARP, limites, rotation.
- [`docs/AUDIT_REPOS_2026-11-06.md`](docs/AUDIT_REPOS_2026-11-06.md) — quota
  par bloc, WARP IPv4 NAT, roadmap capacité.
- [`docs/quota-identity.md`](docs/quota-identity.md) — normalisation `/64`,
  test E4 à faire.
- [`free/warp_rotation.py`](free/warp_rotation.py) — rotation WARP actuelle.
- [`free/pool.py`](free/pool.py) — `Socks5Endpoint`, ledger, rotation.
- [`config.yaml`](config.yaml) — 6 endpoints WARP.

### Dépôts « OpenCode Unlimited » analysés

Analyse détaillée : [`docs/REPOS_12_UNLIMITED_2026-11-06.md`](docs/REPOS_12_UNLIMITED_2026-11-06.md).

- [`Tazmyan/OpenCode-Unlimited`](https://github.com/Tazmyan/OpenCode-Unlimited)
- [`ebouther/opencode-unlimited-proxy`](https://github.com/ebouther/opencode-unlimited-proxy)
  — moteur [`jasonxu114514/opencode2api`](https://github.com/jasonxu114514/opencode2api)
- [`Parsa013Ah/OpenCode-Unlimited-ProxyPool`](https://github.com/Parsa013Ah/OpenCode-Unlimited-ProxyPool)
- [`coder-nguoi-tay/opencode-unlimited`](https://github.com/coder-nguoi-tay/opencode-unlimited)
- [`Netherstar44/OpenCode_Unlimited`](https://github.com/Netherstar44/OpenCode_Unlimited)
- [`d1alect-tech/opencode-zen-unlimited`](https://github.com/d1alect-tech/opencode-zen-unlimited)
- [`CarbonWalls/opencode-unlimited`](https://github.com/CarbonWalls/opencode-unlimited)
- [`muhamad-idris/opencode_unlimited`](https://github.com/muhamad-idris/opencode_unlimited)
- [`mulqizamzam/opencode-unlimited-proxy`](https://github.com/mulqizamzam/opencode-unlimited-proxy)
- [`Nirusaki-Malaal/OpenCode-Unlimited`](https://github.com/Nirusaki-Malaal/OpenCode-Unlimited)
- [`ngynlaam/zen-unlimited`](https://github.com/ngynlaam/zen-unlimited)
- [`hansputera/opencode-multi-agents`](https://github.com/hansputera/opencode-multi-agents)

### Fournisseurs et préfixes

- [BuyVM — Dedicated KVM Slices](https://buyvm.net/kvm-dedicated-server-slices/)
  — 512 Mo à 24 $/an, bande passante non mesurée.
- [BuyVM — fonctionnalités](https://buyvm.net/features/) — assignation IPv6.
- [IRCNow — BuyVM IPv6](https://docs.ircnow.org/buyvm/ipv6/) — `/48` par VPS,
  sous-réseau routé.
- [LowEndTalk — VPS avec IPv6 par défaut](https://lowendtalk.com/discussion/178453/vps-providers-that-have-ipv6-by-default)
  — « BuyVM offers a /48 subnet for each VPS by default ».
- [LowEndTalk — VPS avec /48 IPv6](https://lowendtalk.com/discussion/185643/looking-for-multiple-kvm-vps-with-48-ipv6)
  — retour d'expérience sur les `/48` BuyVM.
- [Linode — /64 blocks](https://www.linode.com/community/questions/266/ipv6-64-blocks-on-linode)
  — `/64`, `/116`, `/56` sur justification.
- [Linode — /56 vs /64](https://www.akamai.com/cloud/community/questions/19613/56-vs-64-ipv6-block-on-linode)
  — un `/56` = 256 `/64`.
- [Oracle — IPv6 Addresses](https://docs.oracle.com/en-us/iaas/Content/Network/Concepts/ipv6.htm)
  — VCN `/56`, sous-réseaux `/64`, `/112` par VNIC.
- [Oracle — Always Free](https://www.oracle.com/cloud/free/) — 2 VM, 10 To.
- [Hurricane Electric — Tunnel Broker](https://tunnelbroker.net/) — `/64` par
  tunnel.
- [BGPTunnel](https://bgptunnel.com/) — transit BGP gratuit pour réseaux
  personnels avec ASN/préfixe.

### SFR / Red by SFR

- [OPNsense — SFR/RED France FTTH](https://docs.opnsense.org/manual/how-tos/sfr_red_fr_ftth.html)
  — DHCPv6-PD avec **taille de préfixe 56** et vendor class SFR.
- [Communauté SFR — OPNsense derrière NB6VAC](https://la-communaute.sfr.fr/t5/installation-et-param%C3%A9trage/routeur-opnsense-plac%C3%A9-derri%C3%A8re-la-sfr-box-nb6vac-en-ftth/td-p/2340549)
  — SFR fournit un `/56`, mais la box ne passe qu'un `/64` au routeur.
- [Communauté RED — IPv6 avec routeur et SFR 7/8](https://communaute.red-by-sfr.fr/t5/Box-d%C3%A9codeur-TV/IPV6-avec-Routeur-et-SFR-7-Fibre-ou-SFR-8-en-zone-CGNAT/td-p/605454)
  — passthrough/DMZv6/Next Hop, next-hop en `/64`.
- [La Fibre — DHCPv6 sous OpenWrt pour SFR](https://lafibre.info/remplacer-sfr/dhcpv6-sous-openwrt-nouveaux-pre-requis-pour-avoir-la-connectivite-ipv6-sur-sfr/)
  — vendor class et options DHCPv6 nécessaires.

### Free Mobile / 5G

- [Free — Forfait Free 5G+](https://mobile.free.fr/fiche-forfait-free) —
  350 Go/mois hors Freebox, 9,99 €/mois avec Freebox, 35 Go à l'étranger.
- [Free — Box 5G](https://www.free.fr/freebox/box-5g) — box 5G/4G, data
  illimitée, 29,99 €/mois la 1re année puis 39,99 €.
- [Assistance Free — Activer l'IPv6](https://assistance.free.fr/articles/activer-lipv6-356)
  — IPv6 automatiquement activé sur Freebox et forfait mobile.
- [Freenews — IPv6 activé par défaut chez Free Mobile](https://www.freenews.fr/free-mobile/free-mobile-officialise-lactivation-de-lipv6-par-defaut)
  — IPv6 public par appareil, moins de NAT.
- [MikroTik — /64 mobile](https://forum.mikrotik.com/t/ipv6-with-t-mobile-usa-home-internet-gateway-and-single-64-address-no-prefix-how-to/173938)
  — la plupart des opérateurs mobiles donnent un `/64`, pas un `/56`.

### VPN et gluetun

- [Proton VPN — plans](https://protonvpn.com/support/proton-vpn-plans) —
  10 pays, 1 appareil, data illimitée.
- [Windscribe — free](https://windscribe.com/features/use-for-free) — 10 Go/mois,
  10 pays, appareils illimités.
- [PrivadoVPN — free](https://privadovpn.com/freevpn/) — 10 Go/mois, appareils
  illimités.
- [Mullvad — pricing](https://mullvad.net/en/pricing) — 5 €/mois, 5 appareils.
- [Mullvad — IPv6](https://mullvad.net/en/blog/ipv6-support) — tunnel IPv6.
- [Gluetun — providers](https://github.com/qdm12/gluetun-wiki/tree/main/setup/providers)
  — Proton, Windscribe, Privado, Mullvad, IVPN, AirVPN, PIA, Surfshark.
- [VPN Gate](https://www.vpngate.net/EN/) — relais publics, usage académique.

### Outils IPv6 et rotation

- [3proxy — rotating proxy issue](https://github.com/3proxy/3proxy/issues/992)
  — `extip <prefix>/64`, `ip_nonlocal_bind`.
- [3proxy — IPv6 subnet rotation](https://github.com/3proxy/3proxy/issues/832)
  — exemple `parent 1000 extip .../64`.
- [Dante — sockd.conf](https://www.inet.no/dante/doc/1.3.x/sockd.conf.5.html)
  — `external.rotation`.
- [zu1k/http-proxy-ipv6-pool](https://github.com/zu1k/http-proxy-ipv6-pool) —
  proxy IPv6 par requête, route locale + `ndppd`.
- [NyxProxy-OSS](https://github.com/phanes-io/nyxproxy-oss) — rotation IPv6
  automatique.
- [txthinking/socks5](https://github.com/txthinking/socks5) — bibliothèque
  SOCKS5 Go si le gateway est écrit en Go plutôt qu'en Python.

### Clients WARP

- [Deury08/warp-minimal](https://github.com/Deury08/warp-minimal) — MASQUE,
  pool, rotation 429, pensé OpenCode.
- [Diniboy1123/usque](https://github.com/Diniboy1123/usque) — MASQUE, SOCKS5.
- [ccbkkb/MicroWARP](https://github.com/ccbkkb/MicroWARP) — SOCKS5 WARP léger.
- [bepass-org/warp-plus](https://github.com/bepass-org/warp-plus) — WARP +
  Psiphon.
- [Cloudflare WARP — legal](https://developers.cloudflare.com/warp-client/legal/)
  — termes et liens.

### Listes de proxies (non recommandées)

- [iplocate/free-proxy-list](https://github.com/iplocate/free-proxy-list)
- [monosans/proxy-list](https://github.com/monosans/proxy-list)
- [TheSpeedX/PROXY-List](https://github.com/TheSpeedX/PROXY-List)

---

## Annexes

### A. Calcul des blocs

| Préfixe | Nombre de `/64` | Usage |
|---|---:|---|
| `/64` | 1 | un seul bucket |
| `/60` | 16 | 16 buckets |
| `/56` | 256 | 6–12 stations + réserve |
| `/48` | 65 536 | jamais à court |
| `/32` IPv4 | 1 | un bucket IPv4 |

> **Red by SFR** : le réseau a un `/56`, mais la box fournie ne donne qu'un
> `/64` (1–2 buckets). Le `/56` = 256 buckets nécessite de remplacer la box.
> Voir §3bis.

### B. API du gateway (proposition)

```http
GET  /health
→ {"ok": true, "listeners": [{"label":"vps-1","port":11090,"block":"...:1::/64","source":"...:1::2"}]}

POST /allocate
{"label":"vps-1"}
→ {"ok":true,"label":"vps-1","port":11090,"block":"...:1::/64","source":"...:1::2"}

POST /rotate
{"label":"vps-1"}
→ {"ok":true,"label":"vps-1","port":11090,"block":"...:2::/64","source":"...:2::2"}

POST /release
{"label":"vps-1"}
→ {"ok":true}
```

Authentification : `Authorization: Bearer <token>`.

### C. Commandes Phase 0 (exemple)

```bash
# Sur le VPS, avec un /64 routé
ip -6 addr add 2001:db8:abcd:1::2/64 dev eth0
# Préfixe routé : route locale sur lo. Si le /64 est on-link, utiliser eth0.
ip -6 route add local 2001:db8:abcd:1::/64 dev lo
sysctl -w net.ipv6.ip_nonlocal_bind=1

# Vérifier la source
curl --interface 2001:db8:abcd:1::2 https://api64.ipify.org
curl --interface 2001:db8:abcd:1::3 https://api64.ipify.org

# Proxy de test (3proxy ou gateway)
# Puis tester le wire free via scripts/test_free_egress.py
```

### D. Runbook VPS (optionnel, dernier recours)

0. Vérifier d'abord box/ISP, Oracle et Proton ; n'utiliser ce runbook que si
   les replis gratuits ne suffisent pas.
1. (Optionnel) Commander BuyVM 512 (Debian 12), clé SSH, IPv6 activé.
2. Vérifier le `/48` dans Stallion → `ip -6 addr`.
3. `sysctl net.ipv6.ip_nonlocal_bind=1` (fichier `/etc/sysctl.d/`).
4. `ip -6 route add local <prefix>/48 dev lo` (persistant via systemd).
5. Installer le gateway, `systemd` unit, token, SQLite.
6. nftables : SSH depuis l'admin, gateway seulement loopback/WireGuard.
7. Tunnel SSH : `ssh -N -L 8099:127.0.0.1:8099 -L 11090:127.0.0.1:11090 ...`.
8. Config proxy : endpoints `vps-ipv6`, redémarrer.
9. Test : `curl` via chaque SOCKS5, vérifier le `/64`.
10. Rollback : désactiver les endpoints VPS, garder WARP.

### E. Glossaire

- **Egress** : IP source vue par l'API free.
- **Bucket** : compteur de quota `(ip_norm, jour, bucket)`.
- **Bloc** : `/32` IPv4 ou `/64` IPv6 ; unité de quota.
- **Rotation** : changer le bloc d'une station sans changer son label.
- **Sticky** : une station garde la même adresse source entre deux rotations.
- **Gateway** : service qui expose des SOCKS5 locaux bindés sur des préfixes
  IPv6.
