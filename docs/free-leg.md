# La jambe gratuite — modèle de porte et mode d'emploi

> Ce document remplace l'archéologie. Tout ce qu'il affirme a été **mesuré** contre
> la passerelle réelle ; les cas où la preuve n'est qu'un test sont signalés.
> Dernière vérification live : 2026-10-22.

## 1. Ce qu'est la jambe gratuite

Le proxy sert deux amonts distincts :

| Jambe | Auth | Quota | Usage |
|---|---|---|---|
| `opencode-go` (payante) | clé API | facturé | modèles premium |
| `opencode` (gratuite) | **aucune** | 500 req/IP | modèles `*-free` |

La jambe gratuite sort par une **station** : un tunnel SOCKS5 (ports 1080/1081/1085,
six stations en rotation). Chaque station = une IP publique = un quota neuf. C'est
pourquoi la rotation d'IP est le mécanisme central, et pourquoi ce qui casse n'est
presque jamais « le modèle » mais **la porte** ou **la sortie**.

## 2. La porte : trois facteurs

La passerelle refuse un `403 FreeTierError` — *« OpenCode's free tier can only be
used from within OpenCode »* — sans jamais dire lequel des trois facteurs a échoué.
Le modèle de la porte est donc une conjonction :

```
passage = egress ∧ identity ∧ body
```

| Facteur | Ce qu'il vérifie | Preuve |
|---|---|---|
| **egress** | l'IP de sortie n'est pas grillée (quota, réputation) | rotation d'IP → nouveau quota ; `403` en rafale sur une station = IP morte |
| **identity** | la face réseau est celle du client officiel (TLS + en-têtes) | `curl_cffi impersonate=chrome131`, UA Bun, en-têtes `x-opencode-*` |
| **body** | le corps a la forme attendue (voir §3) | bisection octet par octet : quatre variantes mesurées |

Le diagnostic vit dans `config/free_gate.py` (`diagnose`), il est **branché** sur le
chemin réel depuis V6.1.2 : sur un `403` de classe `gate`, le proxy interroge les
en-têtes et le corps **réellement partis** et publie les facteurs en échec.

```bash
curl -s "http://127.0.0.1:4000/health?detail=full" | python -c "import json,sys; d=json.load(sys.stdin); print(d['gate_factors'])"
# {"egress": 2, "body": 1}  -> deux refus de sortie, un refus de forme
```

## 3. Le contrat de corps — mesuré, pas supposé

| Règle | Mesure | Conséquence |
|---|---|---|
| `stream: true` **obligatoire** | `true` → 200 ; `false` ou absent → **403** | un client non-stream est converti, sa réponse reconstituée |
| outil `read` réaliste **obligatoire** | `read` seul → 200 ; aucun outil → **403** | shim injecté automatiquement |
| `bash` **inutile** | `bash` seul → 403 ; `read`+`bash` → **200 = même verdict que `read` seul** | shim retiré (V6.2.2), réintroduisible par configuration |
| description courte **rejetée** | `properties: {}` ou description placeholder → 403 **même avec le bon nom** | validation au chargement, min. 40 caractères |
| `tool_choice: "auto"` | émis dès qu'il y a des outils, comme le client officiel | ne pas « corriger » |
| outils triés | collation ICU, majuscule avant minuscule | shim trié **avec** les outils client |

Les définitions vivent dans `config.yaml > free.shims`, avec un champ `necessity`
(`read: required`). Une entrée invalide est **écartée au chargement avec sa raison** :
une description trop courte ferait échouer chaque requête en 403 muet.

## 4. Le fil

- **TLS** : `curl_cffi`, `impersonate="chrome131"` (le client officiel utilise une
  empreinte navigateur ; un `requests` nu est refusé par le facteur *identity*).
- **En-têtes** : `_official_free_headers()` — UA Bun, `x-opencode-*`, jamais la clé
  payante ni les cookies du client (invariant A.0, testé).
- **Sessions** : mutualisées par `(proxy, face)` via `_get_pooled_curl_session` ;
  la poignée de main TLS est donc payée une fois par station, pas par requête.

## 5. Stations : qui accuser, et quand

Un `403` free ne dit pas qui est fautif. La politique (`upstream/station_faults.py`)
distingue deux causes opposées :

| Signal | Imputé à la station ? | Mise à l'écart |
|---|---|---|
| facteur `egress` **seul** | oui — l'IP est en cause | 300 s |
| `egress` **+** `body`/`identity` | **non** — la requête l'est aussi | aucune |
| `body` / `identity` seuls | **non** — les cinq stations échoueraient | aucune |
| timeout de connexion | oui | 60 s |
| 5xx de transit | oui, brièvement | 45 s |

Écarter une station sur une faute de **forme** ne réparerait rien et consommerait du
quota de rotation : c'est la distinction que V6.3.2 a introduite.

## 6. Diagnostic : les cinq champs utiles

`GET /health?detail=full`

| Champ | Répond à |
|---|---|
| `gate_factors` | quel facteur refuse (`egress` / `identity` / `body`) |
| `free_failure_kinds` | classe des échecs (`gate`, `quota`, `transit`, `auth`, `model`, `empty`) |
| `station_faults` | quelle station a été écartée, et pour quelle cause |
| `latency_by_leg` | `p50`/`p95` par couple `(jambe, modèle)` |
| `slo` | verdict `ok` / `degraded` / `breached` sur ces séries (cible p50 < 4 s, p95 < 12 s) |
| `empty_stream_incidents` | flux vides interceptés par le garde (V5.1) |

## 7. Latence : ce qui est vrai

**Un 200 ne prouve rien.** Quand l'amont refuse un modèle, le proxy renvoie quand
même `200` avec une trame `event: error`. Un modèle cassé qui échoue en 4 s est donc
« rapide ». C'est exactement le piège dans lequel une mesure de cette session est
tombée : `jev-1.13-free` a été recommandé comme « 3× plus rapide » alors qu'il ne
générait rien — il était refusé à chaque appel (`api_usage_error / Invalid request`,
sur toutes les variantes : `max_tokens` 16/256/1024, avec ou sans outil client).

**Ce qui sert vraiment** — balayage complet des 36 modèles free routés, une requête
courte chacun, 2026-10-22 (`_research/free_models_live_status.json`) :

| Modèle | Latence | Verdict |
|---|---|---|
| `nemotron-3.5-lightning-free` | 1,6 s | ✅ |
| `big-pickle` | 2,6 s | ✅ |
| `deepseek-v4.1-flash` | 2,7 s | ✅ |
| `muse-spark-1.2-contributor` | 3,2 s | ✅ |
| `deepseek-v4.1-flash-free` | 4,1 s | ✅ |
| `kimi-k2.6` | 5,8 s | ✅ |
| `minimax-m2.5` | 7,7 s | ✅ |
| `nemotron-3-ultra-free` | 8,5 s | ✅ |
| `glm-5.3-flash-free` | 14,2 s | ✅ |
| `mimo-v2.6-flash-free` | 18,0 s | ✅ |
| 10 autres | 4–51 s | ✅ |
| `jev-1.13-free` | — | ⚠️ **autre protocole** (voir §7 bis) |
| 15 autres `*-free` | 4–54 s | ❌ refus amont |

## 7 bis. Le piège `jev` : mauvais protocole, pas modèle mort

`jev-1.13` et `jev-1.13-free` ne sont **pas** des modèles de chat : leur endpoint
est `https://opencode.ai/inference/v1/systemone`, un protocole à questions
(`questions` + `state` + `criteria`/`instructions`). Les appeler sur
`/v1/messages` ou `/v1/chat/completions` renvoie `api_usage_error / Invalid
request`.

**Une conclusion de cette session était donc fausse** : « `jev-1.13-free` est
refusé en amont » décrivait en réalité un **mauvais protocole côté appelant**.

Sur son endpoint dédié, `/v1/systemone` accepte le corps et valide le format (le
proxy répond `400` détaillé sur un `questions[...]` invalide), mais renvoie
aujourd'hui `422 Endpoint is unavailable` en 0,5 s — sur toutes les variantes
testées (`state` chaîne ou objet, `noul`, `choice`, version payante incluse).
Cause non tranchée faute de capture SystemOne de référence : endpoint
indisponible, ou corps encore incomplet. **Non conclu, donc non corrigé.**

Bilan de la jambe free en chat : **20 servent, 15 sont refusés en amont, 1 est sur
un autre protocole**.

**Vérifier à tout moment**, sans re-balayer 36 modèles :

```bash
curl -s http://127.0.0.1:4000/api/free-health   # servis / non prouvés depuis le démarrage
curl -s http://127.0.0.1:4000/api/drift         # routé vs catalogue (orphelins / candidats)
curl -s "http://127.0.0.1:4000/health?detail=full" | python -c "import json,sys; print(json.load(sys.stdin)['latency_by_leg'])"
```

`/api/free-health` ne compte que les séries dont le **dernier** appel a réussi : un
modèle refusé à chaque appel n'y apparaît jamais comme servi.

## 7 bis. Le canary : ne pas confondre « ça répond » et « ça produit »

Deux sondes courtes toutes les 30 min (une par protocole client) vérifient que la
jambe répond **vraiment** : un flux sans `text_delta`/`thinking_delta`, ou portant
une trame `event: error`, est un échec — même avec un statut `200`.

```bash
curl -s  http://127.0.0.1:4000/health?detail=full | python -c "import json,sys; print(json.load(sys.stdin)['canary'])"
curl -sX POST http://127.0.0.1:4000/api/canary/run            # sonde immédiate
curl -sX POST "http://127.0.0.1:4000/api/canary/run?model=jev-1.13-free"   # prouver l'alerte
```

| Verdict | Signification |
|---|---|
| `ok` | le flux porte du contenu et aucune trame d'erreur |
| `degraded` | un ou deux échecs consécutifs (raté normal d'une jambe free) |
| `failing` | trois échecs consécutifs → `/health` passe `degraded` |

Un modèle refusé en amont fait donc basculer le canary en trois cycles, alors
qu'un canary de statut n'aurait jamais rien vu.

## 8. Reproduire les mesures

| Commande | Ce qu'elle mesure |
|---|---|
| `python _research/measure_shim_necessity.py` | quels shims sont load-bearing (4 corps) |
| `python _research/measure_gate_stream.py` | `stream:true` exigé ou non (3 corps) |
| `python _research/measure_free_leg.py` | latence de la jambe (12 appels) |
| `python _research/probe_free_models.py` | quels ids free du catalogue répondent vraiment |

Chaque script écrit son résultat brut en JSON dans `_research/` : une mesure non
conservée est une opinion avec de la sortie console.

## 9. Ce qui est prouvé, et comment

| Affirmation | Niveau de preuve |
|---|---|
| `stream:true` et `read` obligatoires, `bash` inutile | **mesuré live** (10 corps) |
| Trois facteurs de porte, diagnostic branché | **mesuré live** + tests |
| Écartement par station seulement si attributable | tests (18) |
| Budget de temps, garde de flux vide, SLO | tests (22 / 13 / 16) |
| Cible p50 < 4 s **atteinte** | **non** — la jambe free la viole (4,3 s sur `jev`) |

## 10. Les cinq prémisses qui sont tombées

À relire avant de « corriger » quoi que ce soit :

1. `tool_choice:"auto"` — **correct** tel quel, ne pas s'en abstenir.
2. `max_completion_tokens` — le proxy ne l'émet **jamais** (il utilise `max_tokens`).
3. `store` — jamais émis.
4. « 5 incohérences de protocole » — **une seule** réelle.
5. « 81 ids free exposables » — seul `big-pickle` est servable en plus.

Et une prémisse **confirmée** : la porte exige bien `stream:true` (V6.2.3).
