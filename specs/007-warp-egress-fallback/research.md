# Research — WARP primaire + replis d'egress free

**Date**: 2026-11-07
**Source principale**: `PLAN_ALTERNATIVE_WARP_2026-11-06.md`
**But**: figer les décisions techniques avant l'implémentation.

## D1 — WARP reste la source principale

**Decision**: WARP reste le provider par défaut et doit porter > 80 % du trafic
free. On ne le remplace pas au premier `429` : on parke le bloc ou on tourne
vers un autre `/64` WARP.

**Rationale**: WARP est gratuit, sans cap de données, déjà en production, et
fournit 4–5 blocs `/64` utiles. Les alternatives gratuites ajoutent au mieux
1–2 blocs ou de la continuité.

**Alternatives considered**:
- Remplacer WARP par un VPS `/48` : capacité supérieure mais payant, hors
  priorité free.
- Empiler les conteneurs WARP : au-delà de ~4–6, Cloudflare throttle les
  enregistrements ; la rotation devient aléatoire.
- 5G/box seule : 1 `/64` = 1 bucket, pas un pool.

## D2 — Le seul multiplicateur gratuit est le bloc d'egress distinct

**Decision**: compter et dédupliquer les **blocs** (`/64` IPv6, `/32` IPv4),
pas les conteneurs, sessions, device IDs ou comptes.

**Rationale**: le ledger et l'audit montrent que `server_ip_key()` tronque
l'IPv6 aux 4 premiers groupes ; deux adresses du même `/64` partagent le
bucket. Les 12 dépôts analysés ne créent pas de quota illimité ; ils déplacent
un curseur (bloc, identité, compte/clé, provider).

**Alternatives considered**:
- Rotation d'interface ID dans le même `/64` : aucun gain.
- Spoof device/session : nettoie un banner, pas un bucket.
- Pool de comptes/clés : CGU, coût, suspension ; non retenu par défaut.

## D3 — Ordre des replis : gratuit d'abord

**Decision**:

```text
WARP
  └─ MicroWARP / usque (continuité, même pool)
      └─ IPv6 box/ISP (si /56 ou /48 disponible)
          └─ Oracle Always Free (2–4 /64)
              └─ Proton Free (1 station, data illimitée)
                  └─ Windscribe/Privado (10 Go, urgence)
                      └─ VPS /48 (payant, opt-in)
                          └─ direct (diagnostic explicite uniquement)
```

**Rationale**: préserve WARP, maximize le gratuit, et n'active le payant que
sur décision explicite. Maximum 1–2 stations en fallback pour ne pas vider un
forfait.

**Alternatives considered**:
- Proton en premier : data illimitée mais un seul appareil ; ne peut pas
  remplacer WARP pour six stations.
- VPS en premier : capacité mais payant, contraire à la priorité free.
- Direct en fallback : fuite silencieuse, interdit par la constitution X.

## D4 — MicroWARP/usque = continuité, pas capacité

**Decision**: supporter un client WARP alternatif comme repli de continuité
(même pool Cloudflare, mêmes blocs), pas comme multiplicateur de quota.

**Rationale**: ces clients changent le transport/la robustesse (MASQUE,
mémoire, vitesse de rotation) mais pas le pool d'IP. Ils sont utiles si un
conteneur casse ou si la rotation Docker est bloquée.

**Alternatives considered**:
- `warp-minimal` avec pool de 8 exits : à benchmarker ; peut simplifier
  l'exploitation mais ne crée pas de blocs.
- `warp-plus`/Psiphon : orienté censure, pas quota.

## D5 — Box/ISP : vérifier avant de compter

**Decision**: la Phase 0 vérifie la délégation IPv6 réelle **avec la box
actuelle**. Chez Red by SFR, la box fournie ne délègue qu'un `/64`
(1–2 buckets) ; le `/56` (256 buckets) exigerait un routeur compatible, ce qui
est **hors périmètre** (achat/remplacement matériel). On utilise donc le `/64`
existant s'il est disponible, sans achat.

**Rationale**: les retours SFR/RED et le guide OPNsense convergent : le réseau
attribue un `/56`, mais la box ne le transmet pas. Sans remplacement, le gain
est marginal.

**Alternatives considered**:
- Passthrough/next-hop : probablement 1 `/64`.
- DMZv6 : 1–2 `/64` selon la box.
- 5G Free : 1 `/64` par session, 350 Go/mois, pas un pool.

## D6 — Proton Free = un secours de capacité pour une station

**Decision**: Proton Free via gluetun est le premier secours de capacité
gratuit, limité à **une station** (un appareil), **sous réserve d'un compte
gratuit disponible ou créable sans achat**. Sinon, ce repli reste documenté et
désactivé.

**Rationale**: data illimitée, bon débit, mais un seul appareil simultané.
Windscribe/Privado (10 Go) sont réservés aux coupures courtes.

**Alternatives considered**:
- Mullvad (5 appareils, payant) : bon fallback payant mais hors priorité free.
- Proxies résidentiels : coût au Go, CGU, pas pour ce volume.
- VPN Gate/Tor/listes publiques : instables, MITM, à éviter.

## D7 — VPS `/48` : dernier recours payant, **différé hors périmètre**

**Decision**: un gateway IPv6 sur VPS `/48` (BuyVM 24 $/an ou équivalent) est
documenté mais **non implémenté dans cette itération**. Il reste désactivé par
défaut et ne pourra être activé que sur décision explicite ultérieure.

**Rationale**: 65 536 `/64` donnent une capacité réelle, rotation en secondes,
pas de throttle Cloudflare. Mais c'est payant et ne doit pas devenir la source
principale.

**Alternatives considered**:
- Linode `/56` : plus fiable mais ~5 $/mois et sur justification.
- Oracle free : gratuit mais 2–4 blocs et setup plus long.
- Hetzner/Vultr/Contabo : un seul `/64` par VM.

## D8 — Phase 0 : granularité du quota inconnue

**Decision**: ne pas acheter/activer de capacité avant d'avoir mesuré si le
quota est par adresse, `/64`, ASN/compte/global, ou par modèle.

**Rationale**: si le bucket est agrégé (ASN/compte), un VPS `/48` ne suffit
pas ; il faut un pivot résidentiel/mobile ou attendre le reset. Le test doit
être fait avec le wire exact, pas un `curl` de page d'accueil.

**Alternatives considered**:
- Supposer `/64` et acheter BuyVM : risque de dépense inutile.
- Tester seulement un modèle : un modèle `contributor-free` peut avoir un axe
  différent.

## D9 — 403 ≠ 429

**Decision**: classer explicitement `GATE_403`, `QUOTA_429`,
`RATE_LIMIT_429`, `TRANSIENT_5XX`, `ACCOUNT_OR_GLOBAL`, `AUTH`.

**Rationale**: un `403 FreeTierError` vient de la forme, du geo, de l'auth ou
de la version ; tourner l'IP ne le corrige pas et gaspille des blocs. Un `429`
transitoire court ne doit pas être parké comme un quota journalier.

**Alternatives considered**:
- Traiter tout non-200 comme rotation : churn, faux blocs épuisés.
- Traiter tout 429 comme quota : bloque inutilement sur un rate-limit court.

## D10 — Park reset plutôt que rotation systématique

**Decision**: sur un quota journalier, parker le bloc jusqu'à `reset_at` si le
reset est proche et qu'un autre bloc est disponible ; tourner seulement si le
reset est lointain et que la capacité manque.

**Rationale**: le bucket est journalier ; recréer un conteneur pour un bloc qui
reset dans 20 min gaspille une registration et peut retomber sur le même
`/64`. La constitution IV interdit d'utiliser une IP avant son `reset_at`.

**Alternatives considered**:
- Rotation sur chaque 429 (comportement actuel) : churn, throttle.
- Attendre le reset sans probe : peut retarder la recovery réelle (jitter).
- Probe anticipé par défaut : contraire à IV ; reste une exception explicite.

## D11 — Dédup et sticky

**Decision**: mesurer l'egress réel après chaque rotation (pas le rapport du
rotateur), rejeter les doublons de bloc, et garder le même egress/session tant
qu'un probe `200` passe.

**Rationale**: WARP peut rendre le même `/64` ; deux conteneurs peuvent sortir
par le même bloc. Le sticky préserve le cache et réduit le rejeu de contexte.

**Alternatives considered**:
- Faire confiance au label/conteneur : doublons invisibles.
- Changer d'IP à chaque requête : casse le ledger et le cache.

## D12 — Pas d'empilement de comptes/clés par défaut

**Decision**: ne pas multiplier les comptes/clés comme stratégie free. Si un
bloc frais reste `429`, tester l'axe modèle/compte/clé avant d'ajouter des
proxies.

**Rationale**: CGU, coût, suspension ; les 12 dépôts sont divisés sur l'axe
compte. Le seul levier gratuit propre reste le bloc.

**Alternatives considered**:
- Pool de clés `zent-` publiques : interdit, risque de suspension.
- Comptes multiples : contraire aux CGU, non retenu.

## D13 — Gateway VPS : source stable par `/64`

**Decision**: un listener SOCKS5 par station, chaque listener sort avec une
adresse source stable dans son `/64` ; la rotation change le `/64`, pas
l'adresse à chaque requête.

**Rationale**: un proxy qui change d'adresse à chaque requête casse le ledger
et le sticky. Un `/64` réservé par label garde la comptabilité exacte.

**Alternatives considered**:
- `zu1k/http-proxy-ipv6-pool` : rotation par requête, incompatible ledger.
- 3proxy + script : plus simple mais sans API de rotation/drain.

## D14 — Observabilité et GUI

**Decision**: exposer provider, bloc, dernier `429`, `reset_at`, santé,
fallback en cours, et métriques par bloc. Chaque option GUI a un contrat
`option → clé → consommateur → effet → test`.

**Rationale**: sans visibilité, une bascule ou un park est indiscernable d'une
panne ; la constitution XII interdit les options inertes.

**Alternatives considered**:
- Logs seulement : insuffisant pour l'opérateur.
- Statut agrégé seulement : masque les blocs et les doublons.

## D15 — Risque CGU

**Decision**: documenter le risque CGU et ne pas revendre/partager le service.
Le plan est technique, pas juridique.

**Rationale**: le contournement de quota et l'usage WARP comme pool de proxies
peuvent violer les CGU. L'opérateur décide en connaissance de cause.

**Alternatives considered**:
- Ignorer le risque : non honnête.
- Passer au payant par défaut : hors priorité free et hors décision.
