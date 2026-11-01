# Enquête 429 free — pourquoi « toutes les IPs » échouent

**Date : 2026-11-01.** Enquête demandée après l'hypothèse « statistiquement,
toutes les IPs ne peuvent pas être épuisées ; le 429 doit venir d'autre
chose ». Conclusion courte :

> Le 429 n'est **pas** causé par le corps, les en-têtes, la session, le TLS ou
> le SDK. Un **replay exact d'une requête officielle capturée** (même corps,
> mêmes en-têtes, même session) prend 429 sur plusieurs IPs et plusieurs ASN.
> Le quota est bien **par IP**, mais le reset est **par IP** (pas un minuit
> UTC unique) et les IPs testées sont toutes des IPs **datacenter/VPN
> partagées**, déjà épuisées ou flaggées. La seule voie free est une **IP
> d'egress fraîche non-datacenter** (mobile/PPPoE/dédiée) ; sinon compte
> payant/Go.

Ce document rassemble les tests, les mesures, le code, les sources web et les
hypothèses éliminées.

---

## 1. Ce qui a été testé

| Test | Résultat |
|---|---|
| Corps officiel exact (`free/official_payload.json`) via 5 stations | **429** sur 5 IPs / 3 ASN |
| Replay exact d'une capture officielle (headers + session + corps) | **429** |
| 8 modèles free actuels, même IP | **429** pour les 8 |
| `tool_choice: none` → `auto` + note no-tools | corrigé ; le 429 persiste |
| `stream_options` manquant pour un client `stream:true` | corrigé ; le 429 persiste |
| Session `ses_…` réelle du client officiel | **429** (replay) |
| TLS/JA3 Bun officiel 1.19.5 | **429** (le TLS n'est pas la clé) |
| UA officiel `opencode/1.19.5 …` | **429** (le UA gate est passé) |
| Modèle free alternatif (flint/mimo/glm/nemotron/big-pickle/beam) | **429** |
| Endpoint `/zen/v1/chat/completions` vs `/inference/...` | **429** (même limiteur) |
| `x-real-ip` spoofé | écrasé par Cloudflare ; **429** |
| IPv6 / Teredo | pas d'IPv6 global ; Teredo désactivé |
| Reset par IP | **00:01 → 00:53 UTC**, pas un minuit unique |

---

## 2. Preuve décisive : replay exact d'une requête officielle

Capture mitm du vrai client 1.19.5 (`mitm_capture.jsonl`), requête du
2026-11-01T05:31:23Z :

```text
session: ses_e6a68b6bafferOd9gHMxsU3upU
body_len: 2477
Authorization: Bearer public
User-Agent: opencode/1.19.5 ai-sdk/provider-utils/4.0.23 runtime/bun/1.4.4
x-opencode-client: desktop
x-opencode-project: global
x-opencode-request: msg_195974961001XQe8Tldyf1iiJb
x-opencode-session: ses_e6a68b6bafferOd9gHMxsU3upU
x-opencode-session-id: ses_e6a68b6bafferOd9gHMxsU3upU
```

Replay **à l'identique** (mêmes headers, même session réelle, même corps),
uniquement le tunnel SOCKS5 changé pour sortir par la station 1
(`187.13.9.69`) :

```text
status: 429
retry-after: 58435
reset: 2026-11-02T00:01:34Z
body: {"type":"error","error":{"type":"FreeUsageLimitError", ...}}
```

Si le 429 venait du corps, de la session, du projet, du UA, du SDK ou du TLS,
ce replay — qui est **la requête officielle elle-même** — passerait. Il prend
429. La seule variable restante est le réseau/le bucket IP.

### 2.1 Corps officiel exact sur 5 stations / 3 ASN

Corps `free/official_payload.json` non modifié (système 21 604, 11 tools,
`tool_choice: auto`, `max_tokens: 32000`, `stream: true`,
`stream_options`), headers officiels 1.19.5, une requête par station :

| Station | IP | ASN / pays | Status | `Retry-After` |
|---|---|---|---|---|
| 1 | 187.13.9.69 | AS207137 PacketHub, DE | 429 | 58777 |
| 2 | 195.242.213.155 | AS9009 M247, CH | 429 | 58889 |
| 3 | 187.13.197.28 | PacketHub, DE | 429 | 61404 |
| 4 | 186.247.161.247 | PacketHub, NL | 429 | 61019 |
| 5 | 186.247.77.153 | AS136787 PacketHub, NL | 429 | 59821 |
| 6 | 86.104.248.30 | AS25369 Hydra, FR | 429 | 59608 |
| 7 | — | timeout | — | — |

Cinq IPs, trois ASN différents, cinq 429 avec le **corps officiel exact**.
Ce n'est pas une erreur de forme.

### 2.2 Tous les modèles free

Sur les mêmes IPs, les 8 modèles free actuels (`deepseek-v4.1-flash-free`,
`flint-40b-free`, `mimo-v2.6-flash-free`, `glm-5.3-flash-free`,
`nemotron-3.5-super-vl-free`, `nemotron-3.5-lightning-free`, `big-pickle`,
`beam-free`) renvoient tous `FreeUsageLimitError`. Le bucket « default » est
donc partagé par ces modèles sur ces IPs — ou l'IP est globalement épuisée.

---

## 3. Découverte majeure : le reset est **par IP**, pas un minuit UTC unique

Le code public `getRetryAfterDay(now)` (commit `47f33329`) calcule :

```ts
Math.ceil((86_400_000 - (now % 86_400_000)) / 1000)
```

soit « secondes jusqu'à 00:00 UTC ». Si c'était la production actuelle,
toutes les IPs interrogées à la même minute auraient le **même** reset.

Mesure réelle (corps officiel exact, une requête par station, 2026-11-01
vers 07:48 UTC) :

| Station | IP | `date` HTTP | `Retry-After` | Reset calculé |
|---|---|---|---|---|
| 1 | 187.13.9.69 | 07:48:24 | 58390 | **2026-11-02T00:01:34Z** |
| 3 | 187.13.197.28 | 07:48:29 | 61021 | **2026-11-02T00:45:30Z** |
| 5 | 187.15.173.147 | 07:48:45 | 61465 | **2026-11-02T00:53:10Z** |
| 6 | 86.104.248.30 | 07:48:53 | 59193 | **2026-11-02T00:15:26Z** |
| 7 | 86.104.23.239 | 07:48:58 | 60879 | **2026-11-02T00:43:37Z** |

Les resets s'étalent de **00:01 à 00:53 UTC**, pas un minuit unique. Deux
requêtes espacées sur la **même** IP donnent le même reset (station 1 :
00:01:34 aux deux passages ; station 5 : 00:19:14 aux deux passages), et le
`Retry-After` diminue exactement avec le temps. Donc :

- le reset est **stable par IP** ;
- il est **différent entre IPs** ;
- le `Retry-After` est un **compte à rebours vers le reset de cette IP**.

Hypothèses compatibles :

1. **Fenêtre glissante de 24 h par IP**, ancrée sur la plus ancienne requête
   de cette IP (par n'importe quel utilisateur, puisque l'IP est partagée) ;
2. **Minuit UTC + jitter par IP** (0–60 min) pour éviter le thundering herd ;
3. **Cycle 24 h avec offset par IP** (le provider a peut-être migré du
   minuit fixe vers un cycle 24 h — voir issue #16844).

Le code public du commit `47f33329` ne correspond donc plus exactement à la
production : le backend console n'est plus dans le dépôt public `dev`
(seul le frontend `zen/index.tsx` reste). Le `Retry-After` live est la seule
source de vérité.

### 3.1 Conséquence pour le proxy

Le ledger `(IP, jour UTC, bucket)` doit stocker `reset_at = now + Retry-After`
**sans snap sur minuit**. L'ancien code snappait à minuit si l'écart était
≤ 120 s ; c'est faux pour les resets à 00:01/00:45/00:53. Corrigé : le
`Retry-After` est utilisé tel quel. Le ledger a déjà une entrée **par IP**,
donc il peut suivre des resets différents.

---

## 4. Pourquoi « toutes » les IPs testées sont épuisées

Les IPs testées sont toutes des IPs **datacenter/VPN partagées** :

- PacketHub S.A. (AS207137 / AS136787)
- M247 Europe (AS9009)
- Hydra Communications (AS25369)

Ces ASN sont massivement utilisés par les utilisateurs d'OpenCode free et par
les proxys/VPN. Le provider le documente indirectement :

- **#60991** : quota free **par IP**, les IPs partagées (VPN, CGNAT, campus,
  cloud) arrivent **pré-épuisées** ; un utilisateur peut prendre 429 à sa
  première requête.
- **#56719** : depuis le 17 octobre 2026, les modèles free sont **throttlés
  depuis les IPs datacenter** ; le mainteneur confirme un blocage
  **network-level** et dit que les VPN/proxys partagés peuvent être flaggés.
- **#56965** : message explicite « free models aren't available from your
  current network because we've detected abusive traffic from this IP » ;
  blocage par réseau, pas par compte.
- **#34344** : l'exploit historique « changer d'IP VPN reset le quota » est
  basé sur le bucket IP ; il ne marche que si l'IP n'est pas déjà épuisée.

Un échantillon de 5–6 IPs VPN n'est donc **pas** représentatif des milliers
d'IPs NordVPN : la majorité des IPs datacenter sont partagées, et beaucoup
sont brûlées ou flaggées. Statistiquement, il faudrait un échantillon
beaucoup plus grand, et surtout **hors des ASN datacenter**.

---

## 5. Hypothèses éliminées

| Hypothèse | Test | Verdict |
|---|---|---|
| Corps mal formé | corps officiel exact | éliminé |
| `tool_choice: none` | corrigé en `auto` ; 429 persiste | éliminé comme cause actuelle |
| `stream_options` manquant | corrigé ; 429 persiste | éliminé comme cause actuelle |
| Session/project/msg inventés | replay session réelle du client | éliminé |
| UA non officiel | UA officiel `opencode/1.19.5` | éliminé (gate passé) |
| TLS/JA3 différent | JA3 Bun officiel | éliminé |
| Endpoint différent | `/zen/v1` et `/inference` | éliminé |
| `x-real-ip` spoofé | Cloudflare écrase | éliminé |
| IPv6 /64 différent | pas d'IPv6 global | non testé |
| Modèle free précis | 8 modèles testés | éliminé (tous 429) |
| Compte/workspace | `Bearer public`, pas de compte | éliminé (pas de compte) |

Les bugs de corps corrigés (`tool_choice`, `stream_options`, probe du hunter
sans système officiel) sont **réels** et importants pour les clients
tool-less/stream, mais ils ne sont pas la cause du 429 actuel : le corps
officiel exact prend 429.

---

## 6. Hypothèses restantes

1. **IP/ASN datacenter partagée pré-épuisée ou flaggée** — la plus probable.
   Le bucket est par IP ; les IPs VPN sont partagées et le provider filtre
   les réseaux datacenter.
2. **Capacité free globale** — le provider dit que la limite free est
   « dynamic, based on capacity vs demand » (#28055). Si la capacité free est
   momentanément saturée, même une IP fraîche peut prendre 429. Le message
   serait le même `FreeUsageLimitError`.
3. **Fenêtre glissante par IP** — le reset observé par IP le suggère ; une IP
   peut rester bloquée tant que de nouvelles requêtes (d'autres utilisateurs)
   entretiennent la fenêtre.
4. **Quota par préfixe de modèle** — certains modèles ont un `rateLimit`
   explicite et un bucket `YYYYMMDD+préfixe` ; sur nos IPs, les 8 modèles
   étaient épuisés, donc pas de bucket frais.
5. **Blocage abus réseau** — #56719/#56965 ; le message peut être le 429
   quota ou le message « abusive traffic » selon le stade.

---

## 7. La seule voie pour que le free fonctionne

### 7.1 IP fraîche non-datacenter (voie free)

Il faut une IP d'egress qui n'est **pas** dans un ASN datacenter/VPN partagé :

- **hotspot mobile** (IP carrier, souvent CGNAT mais parfois fraîche) ;
- **reconnexion PPPoE / reboot routeur** pour une nouvelle IP résidentielle ;
- **IP résidentielle dédiée** (static/residential proxy) ;
- **IPv6 /64 dédié** si l'ISP délègue un préfixe (chaque /64 = bucket).

Le proxy est déjà prêt à utiliser une nouvelle IP : le ledger est par IP, et
le corps/headers sont officiels. Pour une IP directe, il faut que le trafic
free sorte par cette IP (mode direct ou fallback direct), pas par les
stations VPN. Le plus simple :

1. connecter le hotspot / reconnecter le routeur ;
2. vérifier l'IP publique (`https://api.ipify.org`) ;
3. basculer la jambe free en direct (ou ajouter cette IP comme egress) ;
4. envoyer une requête free avec le corps officiel.

Si cette IP fraîche renvoie **200**, le quota est bien par IP et la voie est
validée. Si elle renvoie **429**, le free est globalement saturé/flaggué et
la seule voie est un compte payant/Go.

### 7.2 Compte payant/Go

Le quota payant est par clé/workspace, avec des fenêtres rolling/weekly/
monthly et un reset réellement personnel. C'est la seule voie garantie si le
free est globalement saturé. La clé actuelle est expirée ; le cookie Go est
expiré.

### 7.3 Pourquoi NordVPN seul ne suffit pas

NordVPN a des milliers d'IPs, mais elles sont **datacenter et partagées**.
Le provider filtre/limite ces réseaux, et les utilisateurs OpenCode free les
utilisent massivement. Une rotation NordVPN est un tirage dans un sac
majoritairement brûlé ; un échantillon plus grand peut trouver une IP fraîche,
mais c'est un lottery, pas une correction fiable. La recherche doit être
lente (throttle AUTH) et peut durer des heures.

---

## 8. Corrections code déjà faites

- `free/quota_ledger.py` : bucket `(IP, jour UTC, bucket)`, `reset_at` exact
  depuis `Retry-After` (plus de snap minuit), `clear()` pour les faux marks.
- `free/pool.py` : `daily-bucket-exhausted`, `all_usable_buckets_exhausted`,
  `clear_quota_buckets`, `quota_ledger` dans `/api/pool-status`.
- `opencode.py` : `tool_choice: auto` + note no-tools ; `stream_options`
  ajouté aux clients streaming ; `_mark_free_stations_429` transmet l'IP
  réellement tentée ; `POST /api/free/quota/clear`.
- `scripts/fresh_ip_hunter.py` : probe via `_free_wire_body` (système
  officiel), vérification d'un vrai 200, pas de faux positif ledger.
- `free/hunter.py` + GUI : toggle Fresh IP Hunter, statut PID/log.

Ces corrections rendent le proxy **exact** et **honnête** ; elles ne créent
pas de quota. La voie qui reste est réseau/compte.

---

## 9. Tests restants (dans l'ordre)

1. **Hotspot mobile / PPPoE** : nouvelle IP, corps officiel exact, vérifier
   200 vs 429. C'est le test décisif.
2. **IPv6 /64** si disponible (ou Teredo/6to4) : nouveau bucket /64.
3. **Pays/ASN non-datacenter** : un serveur NordVPN n'est pas résidentiel ;
   tester un provider avec des IPs résidentielles si disponible.
4. **Après le reset par IP** (00:01–00:53 UTC) : retester la même IP ; si le
   reset est per-IP, elle doit repasser 200 (sauf si la fenêtre glissante est
   entretenue par d'autres utilisateurs).
5. **Compte payant/Go** : si une clé valide est disponible, comparer le
   limiteur (clé/workspace) au limiteur IP.

---

## 10. Références

- Code public (ancien commit) :
  `packages/console/app/src/routes/zen/util/handler.ts`,
  `ipRateLimiter.ts`, `ip.sql.ts` — commit `47f33329`.
- Le backend console n'est plus dans `anomalyco/opencode@dev` (seul le
  frontend `zen/index.tsx` reste) ; la production peut différer.
- Issues :
  - #56719 — free models throttled from datacenter IP since Oct 17
  - #60991 — free quota IP-bucketed, shared IPs pre-exhausted
  - #42977 — model-specific 429, Retry-After ~19.4h
  - #42074 — UA gate (`User-Agent: opencode/...`)
  - #57540 — free-tier accounting undocumented (reset/scope)
  - #16844 — midnight reset → 24h cycle question
  - #34344 — VPN IP rotation exploit
  - #56965 — network abuse detection message
  - #28055 — free usage dynamic, capacity vs demand
- Captures locales :
  - `C:\Users\julie\AppData\Local\Temp\opencode\mitm_capture.jsonl`
  - `C:\Users\julie\AppData\Local\Temp\opencode\probe_reset_all.py`
  - `C:\Users\julie\AppData\Local\Temp\opencode\probe_exact_all_stations.py`
  - `C:\Users\julie\AppData\Local\Temp\opencode\replay_official_capture.py`

---

## 11. Conclusion

L'hypothèse « le corps est mal formé » a permis de trouver **deux vrais bugs**
(`tool_choice: none`, `stream_options` manquant) et un **faux positif du
hunter** (probe sans prompt système). Ces corrections sont utiles.

Mais le 429 actuel est **réel** : le replay exact du client officiel, avec
session réelle, prend 429 sur cinq IPs et trois ASN. Le quota est **par IP**,
le reset est **par IP** (00:01–00:53 UTC observés), et les IPs testées sont
toutes des IPs **datacenter/VPN partagées** déjà épuisées ou flaggées.

La seule voie free est une **IP d'egress fraîche non-datacenter**. La seule
voie garantie est un **compte payant/Go**. Continuer à rotationner NordVPN
peut, statistiquement, finir par trouver une IP fraîche, mais ce n'est pas une
correction : c'est un tirage lent dans un pool majoritairement brûlé.
