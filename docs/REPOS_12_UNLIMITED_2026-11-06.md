# Analyse des 12 dépôts « OpenCode Unlimited » — 2026-11-06

**Objet** : extraire ce que ces dépôts prouvent réellement sur le quota free
OpenCode, ce qu'ils ne prouvent pas, et ce qui mérite d'entrer dans
[`PLAN_ALTERNATIVE_WARP_2026-11-06.md`](../PLAN_ALTERNATIVE_WARP_2026-11-06.md).

**Méthode** : les 12 dépôts ont été téléchargés dans `_research/repos/`, puis
lus (README, code de rotation, ledger, gateway, scripts). Aucun test live WARP,
aucun proxy public, aucun credential n'a été utilisé pendant cette analyse.

**Niveaux de preuve** :

- **[M]** mesuré par l'auteur du dépôt (probe ou test documenté) ;
- **[R]** reproduit localement dans ce projet (audit ou test existant) ;
- **[D]** documenté seulement, pas reproduit ;
- **[I]** inféré du code, pas mesuré.

> **Conclusion en une phrase** : aucun de ces dépôts ne crée de quota
> « illimité ». Tous déplacent un curseur : bloc d'egress, identité de requête,
> compte/clé, ou fournisseur. Le seul vrai multiplicateur gratuit est le nombre
> de **blocs d'egress distincts** (`/64` IPv6, `/32` IPv4) ; le reste est de
> l'optimisation, de la continuité, ou du contournement à risque.

---

## 1. Tableau de synthèse

| # | Dépôt | Type | Egress / quota | Idée unique | Verdict | Licence |
|---:|---|---|---|---|---|---|
| 1 | [`Tazmyan/OpenCode-Unlimited`](https://github.com/Tazmyan/OpenCode-Unlimited) | Launcher Windows | OS-level ; ledger `/64`/`/32` ; 00:00 UTC | « block » + quarantaine proxy + probe avant application | **Référence ledger/UX** | MIT |
| 2 | [`ebouther/opencode-unlimited-proxy`](https://github.com/ebouther/opencode-unlimited-proxy) | Gateway Docker | Moteur `opencode2api` ; WARP IPv6 ; `source_ipv6` routé | 4 gates mesurés + dédup d'egress réel + retrait 429/403 | **Référence principale** | MIT (moteur GPL-3.0) |
| 3 | [`Parsa013Ah/OpenCode-Unlimited-ProxyPool`](https://github.com/Parsa013Ah/OpenCode-Unlimited-ProxyPool) | Gateway Node | Proxies publics/perso + V2Ray/Xray | Dashboard/tray + test des proxies perso en premier | **Repli proxy, pas capacité** | MIT |
| 4 | [`coder-nguoi-tay/opencode-unlimited`](https://github.com/coder-nguoi-tay/opencode-unlimited) | Launcher Python | API de rotation 4G/proxy | `used_ips.json` + cooldown du modem + auto-resume | **Repli 4G, dédup utile** | Aucune |
| 5 | [`Netherstar44/OpenCode_Unlimited`](https://github.com/Netherstar44/OpenCode_Unlimited) | Launcher Windows | Reset réseau/VPN + device ID | Purge du contexte, reset des sockets | **À éviter** (identité, destructif) | MIT |
| 6 | [`d1alect-tech/opencode-zen-unlimited`](https://github.com/d1alect-tech/opencode-zen-unlimited) | Gateway Bun/TS | Relais + sing-box/Hysteria2 | Sticky pin + bench 429/401/403 + pas de direct silencieux | **Référence erreurs/sticky** | MIT |
| 7 | [`CarbonWalls/opencode-unlimited`](https://github.com/CarbonWalls/opencode-unlimited) | Launcher Bash | Tor/Privoxy, liste de proxies, WARP | Probe puis `--continue` sur la même session | **Repli simple** | MIT |
| 8 | [`muhamad-idris/opencode_unlimited`](https://github.com/muhamad-idris/opencode_unlimited) | Gateway FastAPI | Proxies aléatoires + blacklist | Fallback de modèle | **Prototype, à rejeter** | Aucune |
| 9 | [`mulqizamzam/opencode-unlimited-proxy`](https://github.com/mulqizamzam/opencode-unlimited-proxy) | Fork Docker | Copie de `ebouther` | Aucune | **Doublon** ; utiliser l'amont | MIT |
| 10 | [`Nirusaki-Malaal/OpenCode-Unlimited`](https://github.com/Nirusaki-Malaal/OpenCode-Unlimited) | Launcher Node | OS-level ; ledger `/64`/`/32` | `usage`/`block` + re-id de session + pool | **Référence cross-platform** | MIT |
| 11 | [`ngynlaam/zen-unlimited`](https://github.com/ngynlaam/zen-unlimited) | Daemon Bash/Python | WARP local + plugin OpenCode | Park quota, dedupe temporel, storm guards, preuves négatives | **Référence reset/preuve** | MIT |
| 12 | [`hansputera/opencode-multi-agents`](https://github.com/hansputera/opencode-multi-agents) | Gateway Go | ProtonVPN WireGuard + pool de clés | Axe compte/clé explicite + sticky + métriques | **Référence architecture** | Aucune |

**Lecture immédiate** : les dépôts 1, 2, 10 et 11 sont les plus utiles pour ton
cas. Les dépôts 3, 4, 6, 7 et 12 apportent des briques périphériques. Les
dépôts 5 et 8 sont des contre-exemples. Le 9 est une copie.

---

## 2. Fiches détaillées

### 2.1 `Tazmyan/OpenCode-Unlimited` — le ledger avant la rotation

**Ce que c'est** : un launcher Windows (Node.js 22.5+) qui pilote le vrai client
OpenCode, pas un gateway. Il mesure l'egress, tient un ledger de blocs, teste un
proxy avant de l'appliquer, puis relance OpenCode.

**Ce qu'il apporte** :

- ledger par **bloc** : IPv6 tronqué aux 4 premiers groupes (`/64`), IPv4
  normalisée en `/32` ;
- `resetAt` fixé au **prochain minuit UTC** ;
- refus explicite d'un proxy qui retombe dans le **même bloc** ;
- quarantaine progressive : 2 échecs → 5 min, doublement jusqu'à 6 h ;
- un proxy plus lent que 3 500 ms est considéré mauvais même s'il répond ;
- probe avant application, puis un seul redémarrage du service ;
- rotation d'identité **pour nettoyer un banner**, pas pour le quota.

**Ce qu'il prouve** :

- [D] le quota free est par **bloc d'egress**, pas par session, projet, modèle,
  device ou machine ;
- [D] changer l'interface ID dans le même `/64` ne change pas le bucket ;
- [D] un ledger `/64` évite les faux succès « nouvelle IP, même bloc ».

**Ce qu'il ne prouve pas** :

- que le quota est **toujours** purement par bloc (il ne teste pas les modèles
  à quota compte/clé) ;
- que 6 conteneurs WARP donnent 6 blocs distincts ;
- que la rotation d'identité aide le quota (l'auteur dit explicitement le
  contraire).

**À retenir** : c'est la meilleure référence pour le **ledger**, la
**quarantaine** et le **probe avant application**. Le code utile est
`lib/egress.js` (`blockKey`, `sameBlock`, `nextMidnightUtc`) et le README
§« The limit is per IP ».

**À ne pas copier** : le reset de device ID comme stratégie de quota ; c'est un
nettoyage d'état local, pas un multiplicateur.

---

### 2.2 `ebouther/opencode-unlimited-proxy` — la référence gates + WARP IPv6

**Ce que c'est** : un stack Docker autour du moteur `opencode2api`
(GPL-3.0). Le moteur sert une API OpenAI, fabrique l'identité OpenCode, gère un
pool WARP userspace et retire les exits sur `429`/`403`.

**Ce qu'il apporte** :

- **4 gates de requête** mesurés : UA `opencode/<version>` ≥ 1.18.0, session
  `ses_` canonique, outil nommé exactement `read`, `stream: true` ;
- distinction nette entre **403 de forme/géo** et **429 de quota** ;
- quota par **bloc** : `/64` IPv6, `/32` IPv4, reset 00:00 UTC ;
- WARP IPv4 = NAT partagée : la rotation IPv4 n'ajoute rien ;
- mode `warp.stack: ipv6` + `WARP_SOURCE_IPV6` routé : chaque exit lie sa
  source et obtient son propre `/64` — c'est le seul multiplicateur propre ;
- `ofu-dedupe` : mesure l'egress réel de chaque nœud, car le rotateur peut
  mentir ou se tromper ; 6 conteneurs tombent souvent sur la même adresse ;
- rotation WARP = **nouvelle registration** (`registration delete/new`), pas
  un simple `disconnect/connect` ;
- limite l'API d'enregistrement : ~90 s entre rotations, 2 rotations par passe,
  4 essais max ;
- retire un exit sur `429`/`403` et le remplace, avec `retry.max_attempts: 6`.

**Ce qu'il prouve** :

- [M] le gate et le quota sont **deux axes séparés** ; un 403 ne se règle pas
  en tournant l'IP ;
- [M] le quota est par bloc, et le nombre de **blocs distincts** est ce qui
  compte, pas le nombre de conteneurs ;
- [M] WARP IPv4 partagée est un mauvais levier ; WARP IPv6 avec source routée
  est le bon ;
- [I] un pool WARP dynamique peut fonctionner de 1 à 32 exits, mais le débit
  réel dépend du throttle Cloudflare.

**Ce qu'il ne prouve pas** :

- que le quota est **uniquement** par bloc pour tous les modèles ;
- que 32 exits donnent 32 blocs distincts (le pool Cloudflare est fini et
  réutilisé) ;
- que le stack tient des milliers de requêtes/jour : les résultats publiés sont
  un smoke test (57 requêtes en 1 h), pas un soak multi-jours ;
- que le mode `source_ipv6` fonctionne sans vrai préfixe routé.

**À retenir** : c'est la **référence principale** pour les gates, la dédup
d'egress, le retrait 429/403 et WARP IPv6. Le moteur peut être un **sidecar
optionnel** sur un hôte Linux avec `/64` routé, mais ce n'est pas un drop-in
Windows.

**Risques** : image Docker tierce (GPL-3.0), web UI avec mot de passe par défaut
à changer, pas de support Windows/macOS pour le moteur, et le mode `source_ipv6`
exige un vrai préfixe routé.

---

### 2.3 `Parsa013Ah/OpenCode-Unlimited-ProxyPool` — le pool de proxies, pas le quota

**Ce que c'est** : un gateway Node/Express OpenAI + Anthropic, avec un pool de
proxies publics et personnels, du support V2Ray/Xray (`vmess`, `vless`,
`trojan`, `ss`, abonnements), un dashboard et une icône tray.

**Ce qu'il apporte** :

- les proxies **personnels** sont testés en premier ; les listes publiques sont
  un dernier recours ;
- support de nombreux formats de proxies et de souscriptions ;
- cache disque des proxies qui marchent ;
- dashboard de requêtes/tokens et menu terminal.

**Ce qu'il prouve** :

- [D] les proxies publics gratuits sont rarement utilisables contre OpenCode ;
- [I] un pool de proxies heterogènes est utile pour la **continuité**, pas pour
  garantir des blocs frais ;
- [I] `openAuth: true` accepte n'importe quelle clé côté client : ce n'est pas
  une authentification.

**Ce qu'il ne prouve pas** :

- qu'un proxy public donné ne voit pas tes prompts ou tes tokens ;
- qu'il existe un ledger `/64` ou une dédup d'egress ;
- que le pool scale sans quotas ni blacklist.

**À retenir** : intéressant pour la **collecte** et la **santé** des proxies
(V2Ray/Xray, test avant usage, cache), pas pour la capacité. Le mode
`bind: network` et `openAuth` sont des risques si le service n'est pas en
localhost.

**À ne pas copier** : l'ouverture réseau par défaut, l'absence d'auth réelle,
et l'usage de proxies publics pour des credentials.

---

### 2.4 `coder-nguoi-tay/opencode-unlimited` — la rotation 4G et le cooldown

**Ce que c'est** : un launcher Python Windows/Linux qui parle à une **API de
rotation de proxy 4G** (ex. `proxyxoay.shop`), expose un bridge local, surveille
OpenCode et relance la tâche après une rotation.

**Ce qu'il apporte** :

- `used_ips.json` : mémoire des IP déjà vues dans la journée, refus des doublons ;
- parsing du **cooldown** du fournisseur (`Con 30s mới có thể đổi proxy`) et
  attente du bon nombre de secondes ;
- probe rapide (5–15 s) au lieu d'un délai fixe de 25–45 s ;
- health monitor du proxy toutes les 20 s ;
- auto-resume de la session après rotation ;
- `spoofer.py` pour le device ID (mais voir la contradiction ci-dessous).

**Ce qu'il prouve** :

- [I] un pool 4G réattribue souvent les mêmes IP ; la dédup est indispensable ;
- [I] le cooldown du fournisseur doit être respecté, sinon la rotation échoue ;
- [D] l'auteur affirme qu'en cas de « quota global communautaire », une IP
  fraîche ne suffit pas et il faut attendre 00:00 UTC.

**Ce qu'il ne prouve pas** :

- l'hypothèse du **quota global communautaire** : elle est affirmée, pas
  mesurée avec un contrôle ; d'autres dépôts mesurent un quota par bloc ;
- que le device ID change le quota ; les tests locaux [R] montrent que la
  rotation de session ne débloque pas une IP 429 ;
- que l'API 4G est gratuite ou stable.

**À retenir** : la **dédup** et le **cooldown parsing** sont bons ; le reste est
spécifique à un fournisseur 4G. La mention « global daily quota » est à traiter
comme une **hypothèse à tester**, pas comme un fait.

**Risque** : aucune licence ; dépendance à un service payant ; le device ID
spoof n'est pas un levier de quota.

---

### 2.5 `Netherstar44/OpenCode_Unlimited` — le contre-exemple identité/contexte

**Ce que c'est** : un gros `.bat` Windows/PowerShell qui tue les daemons
OpenCode, réinitialise le réseau/les VPN, change le device ID, purge le contexte
et propose un bridge OpenRouter.

**Ce qu'il apporte** :

- observation juste : un **contexte gonflé** (100 k+ tokens rejoués à chaque
  commande) brûle le quota beaucoup plus vite qu'une session courte ;
- reset des sockets/daemons et des erreurs stockées ;
- fallback OpenRouter pour les modèles payants.

**Ce qu'il ne prouve pas** :

- que le device ID / `.updaterId` soit un axe de quota ; les mesures [R] et les
  dépôts 1 et 10 disent l'inverse : le quota est par bloc, le device ID ne sert
  qu'à nettoyer un état local ;
- que « token budget » et « message limit » soient la même unité ;
- que réinitialiser le réseau ne casse pas d'autres applications.

**À retenir** : l'**hygiène de contexte** est réelle et utile ; le reste est
risqué ou faux. À classer comme **launcher de dépannage**, pas comme stratégie
de capacité.

**À ne pas copier** : le device ID spoof, le kill de processus, le reset réseau
automatique, et l'idée qu'un « reset » multiplie le quota.

---

### 2.6 `d1alect-tech/opencode-zen-unlimited` — la taxonomie d'erreurs et le sticky

**Ce que c'est** : un gateway Bun/TypeScript devant OpenCode Zen, avec un relais
SOCKS5 et des egress sing-box/Hysteria2 (6 pays dans l'exemple). Il exige un
egress configuré : pas de fallback direct silencieux.

**Ce qu'il apporte** :

- machine d'état par tentative :
  - `2xx` → réponse telle quelle ;
  - `429` → bench de l'egress (60 s par défaut, `Retry-After` plafonné à
    300 s, jitter), rotation, retry ;
  - `401/403` → bench + rotation ;
  - `5xx` → rotation **sans** bench (transitoire) ;
  - timeout/stall → bench + rotation ;
  - autre `4xx` → retour 1:1, pas de retry ;
- `MAX_ATTEMPTS = 5` ;
- sticky pin par session (`createPinnedPicker`) ;
- distinction entre 429 **provider** et 429 **gateway** ;
- `--no-egress-direct` : le boot échoue si aucun egress n'est configuré ;
- doctor/status/tests.

**Ce qu'il prouve** :

- [I] une taxonomie d'erreurs claire évite de tourner sur un 403 de forme ou
  sur un 5xx transitoire ;
- [I] le sticky par session est important pour le cache et la stabilité ;
- [I] un `Retry-After` de quelques minutes ne doit pas être confondu avec un
  reset quotidien.

**Ce qu'il ne prouve pas** :

- que le service est gratuit : les egress Hysteria2 viennent d'un abonnement
  payant ;
- que 6 pays = 6 blocs frais ; les IP peuvent être partagées/épuisées ;
- que le bench de 60–300 s est adapté à un quota journalier (il est trop court
  pour un vrai `FreeUsageLimitError`).

**À retenir** : excellente **référence de conception** pour la taxonomie, le
sticky et le fail-closed. Pour ton cas, remplacer le bench court par un
**park jusqu'au reset** quand le 429 est un quota journalier.

---

### 2.7 `CarbonWalls/opencode-unlimited` — le repli Tor simple

**Ce que c'est** : un launcher Bash qui fait tourner le vrai CLI OpenCode à
travers Tor/Privoxy, une liste de proxies ou un hook WARP ; il surveille le log,
tourne, probe, puis rouvre la même session avec `--continue`.

**Ce qu'il apporte** :

- modes `tor`, `proxy`, `warp`, `none` ;
- probe avec un modèle pas cher avant de rouvrir ;
- `fresh` (IP différente de la dernière) vs `continue` (même IP) ;
- plafond de rotation (`OC_ROTATE_TRIES_MAX=4`) et cooldown ;
- doctor et bridge OpenRouter optionnel.

**Ce qu'il prouve** :

- [D] le cycle « détecter → tourner → probe → `--continue` » fonctionne pour
  une utilisation interactive ;
- [D] Tor est souvent refusé ou lent ; le probe est nécessaire.

**Ce qu'il ne prouve pas** :

- qu'un exit Tor soit un bloc frais ;
- qu'il y ait un ledger `/64` (ce n'est pas le cas) ;
- que le cycle tienne en charge parallèle.

**À retenir** : bon **modèle UX** pour la reprise de session et le probe ; pas
une source de capacité. Tor est à éviter pour du volume.

---

### 2.8 `muhamad-idris/opencode_unlimited` — le prototype non sécurisé

**Ce que c'est** : un gateway FastAPI minimal, avec une liste de proxies
aléatoires, une blacklist sur `FreeTierError`, et un fallback de modèle.

**Ce qu'il apporte** :

- fallback de modèle ;
- blacklist locale hashée des proxies qui échouent ;
- endpoints `/health`, `/v1/models`, `/v1/chat/completions`.

**Ce qu'il ne prouve pas** :

- qu'il passe les gates OpenCode (le README ne documente pas les 4 conditions) ;
- qu'il gère correctement le streaming et les erreurs ;
- qu'il soit sûr : pas d'auth client, CORS `*`, `proxies.txt` d'exemple dans le
  dépôt.

**À retenir** : **à rejeter** comme base de production. Le fallback de modèle et
la blacklist sont des idées, pas une architecture.

---

### 2.9 `mulqizamzam/opencode-unlimited-proxy` — le fork

**Ce que c'est** : une copie de `ebouther` (même README, même stack, mêmes
scripts), avec une version plus ancienne du setup WARP.

**Ce qu'il apporte** : rien de plus que l'amont.

**À retenir** : **doublon**. Utiliser `ebouther` pour les correctifs et la
documentation à jour ; ne pas compter ce dépôt comme une preuve indépendante.

---

### 2.10 `Nirusaki-Malaal/OpenCode-Unlimited` — le launcher cross-platform

**Ce que c'est** : un launcher Node.js 22.5+ (Linux/macOS/Windows) qui affiche
l'usage, nettoie les erreurs de quota stockées, re-id les sessions, tourne le
device ID et change de proxy.

**Ce qu'apporte** :

- commandes `usage`, `block`, `reset`, `proxy pool`, `doctor` ;
- ledger `/64`/`/32` avec reset 00:00 UTC ;
- re-id des sessions **sans perdre les messages** (transaction par session) ;
- pool de proxies avec health et quarantaine ;
- support cross-platform.

**Ce qu'il prouve** :

- [D] le quota est par bloc, pas par device ; le device ID et le re-id servent à
  nettoyer un banner ou un état local, pas à refill ;
- [D] une session lourde peut consommer plus de requêtes qu'une session courte
  (le contexte est rejoué) ;
- [I] le nettoyage de la base et le re-id sont utiles pour l'UX, pas pour le
  quota.

**Ce qu'il ne prouve pas** :

- que le re-id de session débloque une IP 429 ; les tests [R] montrent que non ;
- que le pool de proxies publics soit fiable.

**À retenir** : bonne **référence cross-platform** pour `usage`/`block`/`doctor`
et la gestion de l'état local. Pour le quota, garder le ledger et le pool, pas
le device ID.

---

### 2.11 `ngynlaam/zen-unlimited` — la preuve négative et le park

**Ce que c'est** : un daemon Bash/Python `warp-auto` + un plugin OpenCode
`zen-quota-retry.js`. Il observe les 429, coupe/relance WARP, park les blocs,
déduplique les rotations et protège contre les storms.

**Ce qu'il apporte** :

- distinction **proxy mode** vs **tunnel mode** :
  - proxy : les rotations changent l'IPv6 mais le 429 persiste (pool sale) ;
  - tunnel : ≤ 2 rotations peuvent suffire, même sur un 429 libellé « quota » ;
- dédup par `(modèle, route, préfixe, minute)` ; préfixe `/48`, pas `/128`
  (l'IPv6 WARP change ~toutes les 5 min) ;
- `quota_until` + `QUOTA_PARK` (10 min par défaut, 90 s en tunnel) ;
- `stuck_until` : si l'edge IPv6 ne change pas, park au lieu de recouper ;
- `MAX_STUCK`, storm cap, backoff ;
- ledger de résultats et `zen report` ;
- tests et documentation des erreurs.

**Ce qu'il prouve** :

- [M] une rotation WARP peut changer l'IPv6 **et le 429 revenir 3 s plus tard** ;
- [M] le 429 n'est donc pas toujours levé par un simple changement d'edge ;
- [D] l'auteur conclut que le 429 peut être account/clé/global, mais ne le
  prouve pas ; il recommande un test avec contrôle (direct vs WARP).
- [M] tourner à chaque 429 sans dédup/storm guard crée des coupures inutiles.

**Ce qu'il ne prouve pas** :

- que WARP ne sert à rien : dans certains modes, 1–2 rotations suffisent ;
- que le quota est account/global : c'est une hypothèse ouverte ;
- que le park de 10 min soit le bon pour un quota journalier (il est trop court
  si le reset est à 00:00).

**À retenir** : c'est la **référence la plus honnête** sur les limites de la
rotation WARP. À intégrer :
- ne pas promettre qu'une rotation règle un 429 ;
- mesurer le taux de 429 **après** rotation, avec un contrôle ;
- parker plutôt que recouper en boucle ;
- dédupliquer par préfixe large, pas par `/128`.

**À ne pas copier** : le park court pour un quota journalier ; il faut un park
jusqu'au reset (ou un probe capé) quand le 429 est un `FreeUsageLimitError`.

---

### 2.12 `hansputera/opencode-multi-agents` — l'axe compte/clé

**Ce que c'est** : un gateway Go OpenAI-compatible, avec des conteneurs
ProtonVPN WireGuard, un pool de clés upstream, des sessions sticky, des métriques
et un système de clés PoW. Malgré le nom, ce n'est pas un orchestrateur
multi-agents : c'est un gateway.

**Ce qu'il apporte** :

- rotation ProtonVPN par région/serveur, plusieurs conteneurs ;
- `UPSTREAM_API_KEYS` : une clé aléatoire par requête pour répartir le quota
  entre plusieurs comptes ;
- `isPublicTier` : si l'upstream est le tier public, le code considère que le
  429 est **account-limited** et **ne tourne pas** les proxies ;
- sticky sessions ;
- `IP_BAN_DURATION`, `RATE_LIMIT_FRESH_IP_WAIT`, `Retry-After` ;
- dashboard/métriques, PoW pour l'auth client.

**Ce qu'il prouve** :

- [I] il existe un **axe compte/clé** distinct de l'axe IP ; un pool de clés
  peut répartir le quota ;
- [I] si le tier est account-limited, tourner l'IP ne sert à rien ;
- [I] ProtonVPN gratuit peut fournir plusieurs egress, mais l'usage de
  plusieurs conteneurs sur un compte gratuit est une zone CGU grise.

**Ce qu'il ne prouve pas** :

- que le tier public OpenCode est réellement account-limited **pour tous les
  modèles** ; le code l'affirme, mais les mesures [R] montrent un quota par
  bloc pour le pool par défaut ;
- que multiplier les comptes soit gratuit, stable ou conforme ;
- que ProtonVPN free tolère un pool de conteneurs.

**À retenir** : excellent **contre-point** : le jour où un 429 résiste à un
bloc frais, il faut regarder l'axe compte/clé, pas ajouter des proxies. Mais
le pool de comptes n'est pas une recommandation par défaut : CGU, coût,
complexité, et risque de suspension.

**Risque** : aucune licence ; ProtonVPN/Comptes multiples ; secrets ; le nom
« multi-agents » est trompeur.

---

## 2bis. Chemins de code utiles

| Dépôt | Fichiers à lire en premier |
|---|---|
| Tazmyan | `lib/egress.js` (`blockKey`, `nextMidnightUtc`), `bin/opencode-unlimited.ps1` |
| ebouther | `docs/free-tier-gates.md`, `scripts/gate-probe.py`, `stack/warp/rotator.py`, `stack/dedupe/dedupe.py`, `stack/config.json` |
| Parsa | `src/proxy/pool.mjs`, `src/server.mjs`, `src/v2ray/` |
| coder-nguoi-tay | `rotator.py`, `bridge.py`, `watchdog.py`, `spoofer.py` |
| Netherstar44 | `OpenCodeUnlimited.bat`, `Watch-NetworkChanges.ps1` |
| d1alect | `src/gateway/rotation.ts`, `src/gateway/egress-gate.ts`, `src/relay/helpers.ts`, `src/singbox/` |
| CarbonWalls | `opencode-unlimited`, `docs/COMPARISON.md` |
| muhamad-idris | `ai_gateway.py` |
| mulqizamzam | `stack/` (copie de l'amont) |
| Nirusaki | `lib/egress.js`, `lib/session-reid.js`, `lib/proxy-bridge.js`, `lib/db-tools.js` |
| ngynlaam | `lib/auto.sh`, `lib/rotate.sh`, `lib/state.sh`, `WARP-AUTO.md`, `plugins/zen-quota-retry.js` |
| hansputera | `internal/handler/handler.go` (`isPublicTier`), `internal/proxy/pool.go`, `docs/protonvpn-integration.md` |

---

## 3. Ce que les 12 dépôts disent ensemble

### 3.1 Le quota est surtout par bloc — mais pas seulement

Les dépôts 1, 2, 10 et l'audit local [R] convergent :

- unité : `/64` IPv6, `/32` IPv4 ;
- reset : **00:00 UTC** ;
- plusieurs adresses dans le même `/64` = **un** bucket ;
- plusieurs conteneurs dans le même bloc = **un** bucket.

Mais trois voix dissonantes existent :

- **dépôt 4** : affirme un « quota global communautaire » les jours de pointe ;
- **dépôt 11** : observe un 429 qui revient 3 s après un changement d'IPv6 ;
- **dépôt 12** : code `isPublicTier` qui traite le tier public comme
  account-limited.

**Interprétation raisonnable** : il peut y avoir **plusieurs couches** :

1. quota par bloc d'egress (prouvé pour le pool par défaut) ;
2. quota par modèle ou par tier (certains modèles `contributor-free`) ;
3. quota compte/clé (si une clé est utilisée) ;
4. budget global/sponsorisé (hypothèse non prouvée).

**Conséquence pour le plan** : ne pas écrire « quota = IP » comme une loi
universelle. Écrire « quota par bloc pour le pool par défaut ; tester par modèle
et surveiller l'axe compte/clé ». C'est exactement ce que la Phase 0 doit
trancher.

**Arbre de décision pratique** :

```text
429
 ├─ corps = 403 FreeTierError (forme/geo/auth/version)
 │    → corriger le wire ou changer de pays ; NE PAS tourner l'IP
 ├─ corps = RateLimitError (fenêtre courte)
 │    → backoff court, retenter ; ne pas parker jusqu'à minuit
 └─ corps = FreeUsageLimitError (quota journalier)
      ├─ bloc /64 neuf + même clé → 200
      │    → quota par bloc : ajouter des blocs
      ├─ bloc /64 neuf + même clé → 429
      │    ├─ autre modèle → 200  → axe modèle
      │    ├─ autre clé légitime → 200 → axe compte/clé
      │    └─ sinon → axe global/ASN : attendre le reset ou changer de provider
      └─ même /64, autre adresse → 429 → confirme le bucket /64
```

### 3.2 Le gate 403 n'est pas le quota 429

Le dépôt 2 est le plus clair :

- `403 FreeTierError` peut être : forme de requête, geo, auth, version ;
- `429 FreeUsageLimitError` est le quota ;
- `426` est une version de client trop ancienne.

**Règle** : un 403 ne doit **jamais** déclencher une rotation d'IP ou marquer un
bloc comme épuisé. Il faut d'abord corriger le wire (UA, session canonique,
outil `read`, `stream: true`, `<env>`), ou changer de pays pour un geo-gate.

### 3.3 La rotation WARP est nécessaire mais pas suffisante

Les dépôts 2 et 11 montrent les deux faces :

- WARP IPv6 avec source routée peut donner plusieurs `/64` distincts ;
- mais une rotation peut retomber sur le même bloc, ou sur un bloc déjà épuisé,
  et un 429 peut revenir malgré un changement d'IPv6.

**Règle** : mesurer l'egress réel après chaque rotation, dédupliquer, parker les
blocs épuisés, et **ne pas compter une rotation comme un succès** tant qu'un
probe réel n'a pas répondu 200.

### 3.4 Le sticky et le contexte comptent

Les dépôts 2, 6, 10 et 12 utilisent des sessions sticky ou préservent les vrais
IDs pour le cache. Les dépôts 1, 5 et 10 observent qu'une session lourde rejoue
le contexte et consomme beaucoup plus de requêtes.

**Règle** :
- garder le même egress pour une session (sauf 429) ;
- garder le même ID de session pour le cache ;
- découper les tâches longues en sessions courtes ;
- éviter les sorties d'outils énormes et le raisonnement `max` inutile.

Ce n'est pas un multiplicateur de quota ; c'est une **réduction du gaspillage**.

### 3.5 Le device ID / la session ne sont pas des leviers de quota

Les dépôts 1 et 10 le disent explicitement ; l'audit [R] a testé trois sessions
fraîches sur une IP 429 : **3 × 429**. Le dépôt 5 affirme le contraire, mais ne
fournit pas de contrôle.

**Règle** : ne pas investir dans le spoof de device ID, le re-id de session ou
le changement de projet pour le quota. Ces opérations peuvent nettoyer un
banner ou un état local, pas refill un bucket.

### 3.6 Les proxies publics sont un piège

Les dépôts 3, 7 et 8 utilisent des listes publiques ou Tor. Les trois admettent
que ça marche mal ; le dépôt 3 dit explicitement que les proxies personnels
doivent être préférés.

**Règles** :
- jamais de credentials, cookies ou prompts sensibles via un proxy public ;
- un proxy public peut être un honeypot ou un MITM ;
- pour du volume, WARP, un VPS ou un VPN personnel sont plus sûrs ;
- ne pas exposer un gateway avec `openAuth`, CORS `*` ou bind `0.0.0.0`.

### 3.7 Les licences et la confiance

- MIT : dépôts 1, 3, 6, 7, 9, 10, 11 ;
- MIT + moteur GPL-3.0 : dépôt 2 ;
- aucune licence : dépôts 4, 8, 12 ;
- dépôt 5 : MIT mais code destructif.

**Règle** : ne pas copier de code sans licence ; pour le moteur GPL-3.0, une
auto-hébergement est acceptable, mais toute redistribution/modification doit
respecter la GPL. Vérifier les images Docker et les secrets avant déploiement.

---

## 4. Ce qu'il faut intégrer au plan

### 4.1 À adopter

| Priorité | Élément | Source | Pourquoi |
|---:|---|---|---|
| P0 | Probe 403 vs 429 + gates | dépôt 2 | éviter de tourner sur un problème de forme |
| P0 | Test par modèle et par bloc | dépôts 2, 11 | le quota peut avoir plusieurs axes |
| P1 | Ledger `/64`/`/32` + reset 00:00 | dépôts 1, 10 | base de toute décision |
| P1 | Dédup d'egress réel | dépôt 2 | 6 conteneurs ≠ 6 blocs |
| P1 | Park jusqu'au reset + probe capé | dépôt 11 + plan §1.3bis | ne pas recréer pour rien |
| P1 | Sticky session/egress | dépôts 2, 6, 12 | cache, stabilité, moins de tokens |
| P1 | Retrait 429/403, pas 5xx | dépôt 6 | ne pas pénaliser un bloc pour un transitoire |
| P1 | Quarantaine/backoff | dépôt 1 | éviter les proxies morts |
| P2 | WARP IPv6 source routée | dépôt 2 | seul multiplicateur propre |
| P2 | Sidecar `opencode2api` (Linux) | dépôt 2 | gates + pool WARP + dédup, optionnel |
| P3 | Axe compte/clé (si clés légitimes) | dépôt 12 | diagnostic, pas contournement par défaut |
| P3 | Hygiène de contexte | dépôts 1, 5, 10 | réduire le gaspillage |

### 4.2 À rejeter

- rotation d'IP sur un **403** ;
- device ID / re-id de session comme levier de quota ;
- proxies publics pour des credentials ;
- gateway sans auth / CORS `*` / bind public ;
- « unlimited » sans mesure : aucun dépôt ne le prouve ;
- pool de comptes multiples comme stratégie gratuite (CGU, coût, suspension) ;
- reset réseau/kill de processus automatique (dépôt 5).

### 4.3 Décision de conception

Le plan doit rester :

```text
WARP (4–5 blocs utiles)
  + ledger / dédup / park reset
  + sticky session
  + fallback gratuit (Proton, box/ISP, Oracle)
  + option VPS /48 (payant, dernier recours)
  + diagnostic compte/clé si un bloc frais reste 429
```

Le nombre de **blocs distincts** est la métrique principale. Le nombre de
conteneurs, de sessions, de comptes ou de devices n'est qu'un proxy imparfait
de cette métrique.

---

## 5. Questions ouvertes à trancher en Phase 0

1. **Par modèle** : le quota de `deepseek-v4-flash-free`, `beam-free`,
   `muse-spark` et les autres modèles est-il le même axe ?
2. **Par bloc** : un `/64` neuf donne-t-il un 200 après épuisement d'un autre
   `/64` ? (E4)
3. **Par compte/clé** : un même bloc frais avec une clé `zent-` et avec
   `public` donne-t-il le même résultat ?
4. **Global** : un 429 persiste-t-il après un bloc neuf **et** un changement
   d'heure, ou revient-il seulement à 00:00 UTC ?
5. **WARP** : une rotation qui change l'IPv6 change-t-elle réellement le
   bucket, ou seulement l'edge ?
6. **Rotation vs park** : combien de temps un bloc reste-t-il 429 après le
   reset (jitter) ?

Ces questions sont plus importantes que le choix d'un dépôt à copier.

---

## 6. Références

- [`Tazmyan/OpenCode-Unlimited`](https://github.com/Tazmyan/OpenCode-Unlimited)
- [`ebouther/opencode-unlimited-proxy`](https://github.com/ebouther/opencode-unlimited-proxy)
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

**Documents internes liés** :

- [`PLAN_ALTERNATIVE_WARP_2026-11-06.md`](../PLAN_ALTERNATIVE_WARP_2026-11-06.md)
- [`docs/AUDIT_REPOS_2026-11-06.md`](AUDIT_REPOS_2026-11-06.md)
- [`docs/quota-identity.md`](quota-identity.md)
- [`docs/IPV6_WARP_STATION_2026-11-06.md`](IPV6_WARP_STATION_2026-11-06.md)
