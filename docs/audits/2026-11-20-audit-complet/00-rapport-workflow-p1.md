# Rapport du workflow P1 — lot de 7 groupes

**Date** : 2026-11-20
**Workflow** : `fix-p1-batch` (run `wf_2e8ccbc0-b49`)
**Méthode** : 7 groupes de fichiers disjoints en parallèle, chacun suivi d'un
**vérificateur adversarial** indépendant.
**Bilan** : 11 agents, 935 657 tokens, ~108 minutes, **8 agents terminés,
3 en échec**.

---

## 1. Résultat brut

| Groupe | Verdict | Suite donnée |
|---|---|---|
| `retry-committed` (T115/T116/T125) | ✅ **CONFIRMÉ** — confiance haute | clos |
| `quota-pool` (T128/T129/T131/T132) | ✅ **CONFIRMÉ** — confiance haute | clos |
| `dashboard-read` (T149/T150/T151) | ✅ **CONFIRMÉ** — confiance haute | clos |
| `empty-guard` (T117/T124) | ❌ **RÉFUTÉ** — **régression prouvée** | **corrigée** par l'auditeur principal |
| `core-keys-errors` (T120–T122, T127) | ⚠️ agent en échec (503) | à reprendre |
| `workers-throttle` (T140/T142/T148) | ⚠️ agent en échec (504) | à reprendre |
| `egress-public` (T137–T144) | ⚠️ agent en échec (limite tokens) | à reprendre |

**3 confirmés, 1 réfuté, 3 en échec.**

## 2. La réfutation — et pourquoi elle vaut plus que les confirmations

Le groupe `empty-guard` a corrigé un défaut réel (mémoire non bornée) en en
introduisant un **pire** : le plafonnement `(head + raw[:scan])[:8192]` coupe
**au milieu d'un événement SSE**.

**Mesure du vérificateur** — 300 pings (36 o) puis du contenu :

| | Ancien code | Correctif du workflow | Ma correction |
|---|---|---|---|
| Octets relayés | 10 874 | **8 266** | **10 626** |
| Perte | 0 | **2 608** | **0** |
| JSON valides | ✅ | ❌ `message_start` avalé | ✅ |

**La correction** : borner `head` sur une **frontière d'événement** (`\n\n`)
au lieu d'un `[:8192]` sec — l'excédent reste sous le curseur, donc rien
n'est perdu et la mémoire reste bornée.

**Ce que cela démontre** : un test qui vérifie « le buffer est borné » ne
voit pas « 2 608 octets ont disparu ». C'est le **vérificateur adversarial**,
pas le test de l'agent, qui l'a vu.

## 3. Les trois confirmations — et la qualité des preuves

Les trois vérificateurs ont produit des preuves **plus fortes** que celles
des agents. Extraits :

### `retry-committed` (T115/T116/T125)

- **T115** : simulation ASGI in-process → **1 seul appel** pour un 429 local
  (au lieu de 6 pour un 429 non-local) — la classification **est** ce qui a
  changé. Ordre ASGI vérifié (`Starlette 1.6.0` : `insert(0)` + `reversed`)
  → `Retry` est **outer** par rapport au délimiteur, donc le 429 du breaker
  le traverse forcément.
- **T116** : vérification **AST** → **zéro** référence de code à `committed`
  (seuls des commentaires restent). Confirme que la suppression était la
  bonne option.
- **T125** : corps chunked de 3 076 B avec cap 1 024 → l'app reçoit les
  **3 076 B en 1 appel**. `_append_capped` rend **exactement 65 536 B** sur
  un chunk de 10 Mo.

### `quota-pool` (T128/T129/T131/T132)

- **T128** : replay sur base temporaire → `egress_ips` ne contient plus que
  `['198.51.100.7']` (les `socks5:*` et l'IPv6 tronquée ont disparu),
  `quota_buckets` conserve la clé de parité. **Purge héritée** rejouée :
  lignes insérées manuellement → **0/0** après ouverture.
- **T129** : replay **subprocess** sans flush manuel → les 2 lignes sont en
  base **après la sortie de l'interpréteur**.
- **T131** : `evaluate(QUOTA)` → bascule vers `proton` ; `GATE_403` → `noop` ;
  `WARP_DOWN` sans provider → `degraded`.
- **T132** : park egress + `observe(ok=True)` → **reste PARKED**,
  `parked_until` inchangé.

### `dashboard-read` (T149/T150/T151)

- **T150** : mesure sur la base **réelle** en lecture seule — scan complet
  **31 909 lignes / 33–87 ms** → borné **5 000 lignes / 10–20 ms**. Sur une
  synthétique 300 k : **105,4 ms → 1,8 ms**.
- **T151** : la nouvelle requête couvre **93/100** IP du top affiché (contre
  **8/100** avant), **0 divergence** de valeur sur les IP communes.
- Le vérificateur a **explicitement écarté** les quatre défauts qu'il
  cherchait : « *code sans appelant, écriture suivie d'un return, fonction
  jumelle non corrigée, round-trip non testé* » — aucun ne s'applique.

## 4. Les trois agents en échec

| Groupe | Cause | État |
|---|---|---|
| `core-keys-errors` | `503 Chat admission capacity temporarily unavailable` | **T120–T122, T127 ouverts** |
| `workers-throttle` | `504 upstream request timed out` | **T140, T142, T148 ouverts** |
| `egress-public` | `response exceeded the 32000 output token maximum` | **T137–T144 ouverts** |

**Ce sont des échecs d'infrastructure, pas de conception.** Les fichiers
concernés n'ont **pas** été modifiés (vérifié : `git diff` vide sur
`core/keys.py`, `core/__init__.py`, `core/errors.py`,
`free/public_egress_worker.py`). Les tâches restent ouvertes et reprenables.

## 5. Ce que ce lot confirme du constat central

Le workflow a produit **3 confirmations solides** et **1 réfutation qui vaut
plus** — et la réfutation portait sur un correctif qui **semblait bon** :
mémoire bornée, tests verts, commentaire soigné.

**Le défaut n'était pas dans le code écrit, mais dans ce qu'il ne vérifiait
pas** : personne n'avait testé « et si les keepalives dépassent la borne ? ».

C'est la **septième** occurrence de ce motif dans cet audit :

| Cas | Ce qui était vérifié | Ce qui ne l'était pas |
|---|---|---|
| T014c | le marqueur est écrit | qu'il est **relu** |
| T021 | le modèle porte les credentials | qu'un **appelant** les passe |
| T011/T012 | une fonction corrigée | sa **jumelle** |
| T139 | la traversée est bloquée | que l'**absolu légitime** passe |
| **T117/T124** | la mémoire est **bornée** | que les **octets arrivent** |
| probe WARP | le correctif du 14/11 | qu'il **fonctionne** |
| `warp-rotate` | le log est conditionné | que la **condition** couvre le mode actif |

**Le projet n'a pas un déficit de code — il a un déficit de vérification de
ce qui compte.** Les 7 groupes de ce lot avaient tous des tests verts. Un
seul avait un défaut mesurable, et il fallait **simuler le cas extrême** pour
le voir.
