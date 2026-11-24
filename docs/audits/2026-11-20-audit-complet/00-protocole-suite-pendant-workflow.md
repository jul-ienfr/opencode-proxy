# Protocole : ne jamais lancer la suite pendant qu'un workflow écrit

**Date** : 2026-11-20
**Origine** : trois suites consécutives ont produit des échecs **faux**, tous
causés par la même erreur de méthode.

---

## 1. Le constat

| Suite | Lancée pendant | Échecs | Échecs réels |
|---|---|---|---|
| `full35` | écriture du workflow round 2 | **20** | 20 (régression T137 — **réels**) |
| `full36` | écriture du workflow round 2 | **5** | 0 |
| `full37` | écriture du workflow round 2 | **2** | 0 |

`full35` avait **raison** : la régression T137 était réelle et stable. Mais
`full36` et `full37` ont produit 7 échecs qui ont chacun coûté une
investigation — et qui étaient **tous** des artefacts.

## 2. Les deux mécanismes, identifiés

### 2.1 Budget de temps sous charge

```
tests\test_boot_phase3b_lazy.py:154: in test_opencode_import_budget
    assert ms < 1600, f"import opencode = {ms:.0f} ms (budget 1600 ms, passes={vals})"
E   AssertionError: import opencode = 2437 ms (budget 1600 ms, passes=[2849.0, 2437.0])
```

Le test mesure `import opencode` dans un **sous-processus** et compare à un
budget de 1 600 ms. Le commentaire du test dit lui-même : « *sous suite
complète + 12 conteneurs WARP + worker public + antivirus, les mesures
observées vont de 1 286 à 1 398 ms* ». Ici : **2 437 ms**, parce que **quatre
agents** compilaient et écrivaient en parallèle.

Isolé : **passe**. C'est un **faux échec de charge**, pas une régression.

### 2.2 Lecture d'un fichier pendant sa réécriture

```
tests\test_thinking_e2e.py:601: in test_T4_streaming_400_no_retry_after_started
    assert "if not is_stream:" in src
E   assert 'if not is_stream:' in 'def _clamp_native_output_limit(body: dict, model_id: str) -> dict:...'
```

Ce test fait `inspect.getsource(opencode.messages)` — il lit le source **par
numéro de ligne**. `opencode.py` a été écrit à **23:26:02** ; la vérification a
eu lieu à **23:26:21**. Le fichier faisant 1,6 Mo et ~33 600 lignes, une
écriture en cours décale les numéros et `inspect` renvoie le source d'une
**autre fonction** (`_clamp_native_output_limit` au lieu de `messages`).

Isolé, une fois l'écriture finie : **passe**.

**C'est le mécanisme le plus trompeur** : le message d'erreur accuse le code
d'être faux (« `if not is_stream:` absent ») alors que c'est la **lecture** qui
est incohérente. Sans regarder le fichier réellement renvoyé, on conclut à une
régression inexistante.

## 3. Le protocole

**Règle** : aucune suite complète n'est lancée tant qu'un agent de workflow
écrit dans l'arborescence.

Avant de lancer une suite :

```bash
# 1. Les agents sont-ils tous inactifs ?
python - <<'PY'
import os, time
d = r"...\subagents\workflows\<wf_id>"
now = time.time()
idle = [now-os.path.getmtime(os.path.join(d,f))
        for f in os.listdir(d) if f.endswith(".jsonl")]
print(f"plus récent inactif depuis {min(idle):.0f}s")
print("TERMINE" if min(idle) > 300 else "ACTIF — NE PAS LANCER")
PY

# 2. Aucun fichier source n'a bougé depuis 60 s ?
find . -name "*.py" -newermt "-60 seconds" -not -path "./.artifacts/*" | head
```

Puis, pour **tout** échec : d'abord le rejouer **seul**, sur un arbre au repos.
Un test qui passe seul après un échec en suite n'est **pas** innocent pour
autant — il faut distinguer :

| Symptôme | Diagnostic |
|---|---|
| Passe seul, échoue en suite, **mesure de temps** | charge machine |
| Passe seul, échoue en suite, `inspect.getsource` / lecture de fichier | écriture concurrente |
| Passe seul, échoue en suite, **état partagé** (DB, cache, module global) | pollution inter-tests — **vrai défaut** |
| Échoue partout | régression — **vrai défaut** |

## 4. La vérification a été faite — verdict

**`full38`, lancée sur un arbre totalement au repos** (tous les workflows
inactifs depuis > 300 s, aucun `.py` modifié depuis 120 s) :

```
python -m pytest tests/ -q --no-header -p no:cacheprovider
  -> 100 %, 6 407 tests, 0 FAILED, 0 ERROR, exit = 0
```

**Les 2 échecs de `full37` ont disparu** : c'étaient bien des artefacts de
charge et de lecture concurrente, pas une pollution inter-tests. La conclusion
« suite verte » est acquise — et le protocole est validé : il distingue
correctement un artefact d'une régression réelle.

**Bilan des 4 suites de ce round** : 20 échecs réels (`full35`, régression
T137 corrigée) + 7 artefacts (`full36`/`full37`) + 0 (`full38`).

---

## 5. Corollaire : ne pas modifier l'arbre sous un vérificateur

Le protocole §3 dit « ne pas lancer la suite pendant qu'un agent écrit ». Le
même raisonnement s'applique dans l'autre sens : **ne pas écrire pendant qu'un
agent vérifie**.

Cas observé le 2026-11-21 : mes correctifs (§14 du rapport round 2 —
sur-échantillonnage de la fenêtre, filtre de schéma à l'entrée) ont été écrits
**pendant** que quatre sceptiques vérifiaient les correctifs du round 2. Le
sceptique A1 l'a détecté et s'est ré-adapté :

> « The tree changed mid-session (the author's oversampling fix landed at
> 00:25). Let me read the full current selector and tests, then **re-run
> everything against the current state**. »

Il a bien réagi — il a re-mesuré. Mais **ce n'est pas garanti** : un sceptique
qui ne relit pas l'arbre rend un verdict sur un état périmé, et ce verdict
devient un faux témoignage.

**Règle** : pendant une phase de vérification adversariale, l'auteur ne touche
pas au code. S'il doit corriger quelque chose trouvé par un sceptique, il
attend que **tous** les sceptiques du lot aient rendu leur verdict.

**Coût de l'erreur** : mes correctifs du §14 sont arrivés après le début de la
vérification. Leurs verdicts portent donc sur un mélange de deux états. Le
verdict A1 (périmètre T137) reste valide — il l'a explicitement re-mesuré
contre l'arbre courant — mais les trois autres devront être relus avec cette
réserve, et le lot §14 devra avoir **sa propre** vérification.
