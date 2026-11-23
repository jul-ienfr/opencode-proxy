# Quickstart — 024-audit-optimisation-globale

Comment vérifier que le plan a bien produit ce qu'il promet, phase par phase.

**Principe** : chaque commande ci-dessous **échoue avant** et **passe après**
la phase correspondante. C'est le test de non-régression de l'audit lui-même.

---

## 0. Prérequis

- Le proxy tourne localement (`pythonw opencode.py --gui`, cf. CLAUDE.md).
- Python 3.12+ ; dépendances installées.
- `sqlite3` **n'est pas** en CLI sur cette machine — utiliser Python
  (`import sqlite3`, `uri=True`, `mode=ro`).
- **Ne jamais** lire `logs/requests.db` ou `logs/debug.log` par `Read` :
  utiliser `tail` ou SQL.

---

## 1. Vérifier l'état AVANT toute correction (la référence)

Ces commandes établissent l'état décrit par l'audit. Elles doivent toutes
donner les valeurs « avant ».

```bash
# 1.1 — L'API se contredit (20 stations annoncées pour 6)
curl -s http://127.0.0.1:4000/api/vpn-status | python -c "
import json,sys; d=json.load(sys.stdin)
print('stations_total', d['stations_total'], '| usable', d['usable_stations'],
      '| configured', d['configured_stations'], '| healthy', d['healthy'])"
# AVANT : stations_total 6 | usable 20 | configured 20 | healthy 1

# 1.2 — Les violations de clés étrangères
python -c "
import sqlite3
c=sqlite3.connect('file:logs/registry.db?mode=ro',uri=True)
print(len(c.execute('PRAGMA foreign_key_check').fetchall()), 'violations')"
# AVANT : 15179 violations

# 1.3 — Le cache perdu sur la forme Responses
python -I -c "
import sys; sys.path.insert(0,'.')
from app.protocol.mapping import _extract_cache_tokens as f
print('Chat', f({'prompt_tokens':1000,'prompt_tokens_details':{'cached_tokens':900}}),
      '| Responses', f({'input_tokens':1000,'input_tokens_details':{'cached_tokens':900}}))"
# AVANT : Chat 900 | Responses 0

# 1.4 — Les rotations WARP (0 depuis le 18/11)
python -c "
import sqlite3
c=sqlite3.connect('file:logs/registry.db?mode=ro',uri=True)
for r in c.execute(\"select date(started_at,'unixepoch') d, result, count(*) n
                    from rotation_jobs where started_at > 1794700000
                    group by d,result order by d\"): print(' ', r)"
# AVANT : 2026-11-20 | failed | 128   (aucun 'ok')

# 1.5 — Les stations fantômes
python -c "
import json,urllib.request
d=json.load(urllib.request.urlopen('http://127.0.0.1:4000/api/vpn-status'))
sh=d['station_health']
neg=[k for k in sh if int(k)<0]
print(len(sh),'entrees |',len(neg),'negatives |',
      sum(1 for v in sh.values() if v.get('usable')),'usable')"
# AVANT : 64 entrees | 58 negatives | 46 usable

# 1.6 — Les données non bornées
python -c "
import sqlite3
for db,t in [('logs/registry.db','egress_observations'),('logs/registry.db','qualification_epochs')]:
    c=sqlite3.connect(f'file:{db}?mode=ro',uri=True)
    print(f'  {t}:', c.execute(f'select count(*) from {t}').fetchone()[0])"
du -sh logs/ ; ls -lh logs/registry.db logs/requests.db | awk '{print " ",$5,$9}'
# AVANT : egress_observations 326812 | qualification_epochs 21230
#         logs/ 15G | registry.db 160M | requests.db 160M

# 1.7 — Les artefacts non versionnés
ls logs/compose-control-plane-*.yml free/official_payload.json.bak 2>/dev/null
git check-ignore -v logs/compose-control-plane-1.yml free/official_payload.json.bak
# AVANT : les deux existent sur disque ET sont ignores par git
```

---

## 2. Gate locale (après chaque lot)

```bash
python -m pytest -k "not docker" -q --no-header -p no:cacheprovider
python scripts/config_audit.py --strict
python scripts/platform_audit.py --strict
python -m ruff check .
python -m mypy opencode.py
```

**Attendu** : 0 échec, 0 option morte, 0 violation, ruff et mypy propres.

**Note connue** : `tests/test_boot_phase3b_lazy.py::test_opencode_import_budget`
échoue **avant** la tâche T111 (mesuré 1 723 ms contre un budget de 1 600 ms).
C'est **le seul test en échec** de la suite ; il doit passer après T111.

---

## 3. Vérifier la Phase 0 — « arrêter de perdre et de mentir »

```bash
# 3.1 — Plus de 502 sur /v1/responses non-stream
curl -s -X POST http://127.0.0.1:4000/v1/responses \
  -H 'content-type: application/json' \
  -d '{"model":"haiku","input":"dis bonjour","stream":false}' | head -c 200
# AVANT : {"error":"Upstream returned non-JSON response"} (502)
# APRES : une reponse valide

# 3.2 — Une seule execution du corps de module
grep -c "SQLite connection established" logs/debug.log
# AVANT : 2   APRES : 1

# 3.3 — Le contournement geo
grep -c 'elif _geo_tunnel' opencode.py
# AVANT : 2 (dont un inatteignable)   APRES : 1

# 3.4 — Le probe post-rotation reussit
python -c "
import json
d=json.load(open('free/captures/2.2.5.json'))
print('body_raw present:', 'body_raw' in d, '| body present:', 'body' in d)"
# AVANT : body_raw present: True | body present: False  (le code lit 'body')
# APRES : le code lit body_raw -> plus de KeyError

# 3.5 — Les 34 contrats d'invariant (P0)
python -m pytest tests/unit/ tests/contract/ -q
```

---

## 4. Vérifier la Phase 1 — « restaurer la capacité »

```bash
# 4.1 — Les surfaces d'etat concordent (INV-7)
python -c "
import json,sqlite3,urllib.request
api=json.load(urllib.request.urlopen('http://127.0.0.1:4000/api/vpn-status'))
c=sqlite3.connect('file:logs/registry.db?mode=ro',uri=True)
st={r[0]:r[1] for r in c.execute('select station,client_usable from station_state')}
div=[s['station'] for s in api['stations']
     if bool(s['client_usable'])!=bool(st.get(s['station']))]
print('divergences:', div or 'aucune')"
# AVANT : au moins la station 2 diverge   APRES : aucune

# 4.2 — Aucune valeur monotonic persistee (INV-3)
python -c "
import json,time
d=json.load(open('logs/free_pool.json'))
m=time.monotonic(); h=d['health']
bad=[k for k,v in h.items() if isinstance(v,dict)
     and isinstance(v.get('last_ok_at'),(int,float)) and v['last_ok_at']>m]
print('monotonic() =',round(m),'| valeurs futures:',len(bad))"
# AVANT : valeurs futures: 6   APRES : 0

# 4.3 — Le garde d'unicite d'IP (INV-14)
python -m pytest tests/ -k "egress_ip_in_use or duplicate" -q

# 4.4 — Les compteurs de qualification (INV-4)
python -c "
import sqlite3
c=sqlite3.connect('file:logs/registry.db?mode=ro',uri=True)
s=c.execute('select sum(tested) from qualification_epochs').fetchone()[0]
a=c.execute('select count(*) from qualification_attempts').fetchone()[0]
print(f'SUM(tested)={s} tentatives={a} ratio={s/a:.1f}x')"
# AVANT : ratio 20.6x   APRES : ratio <= 2x

# 4.5 — La rotation WARP refonctionne
python -c "
import sqlite3
c=sqlite3.connect('file:logs/registry.db?mode=ro',uri=True)
for r in c.execute(\"select result,count(*) from rotation_jobs
                    where started_at > strftime('%s','now')-86400
                    group by result\"): print(' ',r)"
# AVANT : failed 128   APRES : ok > 50% du total

# 4.6 — Aucun chemin de production conditionne par pytest (INV-9)
grep -rn "PYTEST_CURRENT_TEST" --include="*.py" opencode.py free/ vpn/ | wc -l
# AVANT : 10   APRES : 0 (remplaces par un flag de config)
```

---

## 5. Vérifier la Phase 2 — « borner, découper, nettoyer »

```bash
# 5.1 — Les donnees sont bornees (INV-15)
python -c "
import sqlite3
c=sqlite3.connect('file:logs/registry.db?mode=ro',uri=True)
pc=c.execute('pragma page_count').fetchone()[0]
ps=c.execute('pragma page_size').fetchone()[0]
print(f'registry.db = {pc*ps/1048576:.0f} Mo')"
du -sh logs/ logs/backups/
# AVANT : registry.db 160 Mo | logs/ 15G | backups 13G
# APRES : registry.db < 50 Mo | logs/ < 2G | backups < 1G

# 5.2 — requests.db sans espace mort
python -c "
import sqlite3
c=sqlite3.connect('file:logs/requests.db?mode=ro',uri=True)
pc=c.execute('pragma page_count').fetchone()[0]
fl=c.execute('pragma freelist_count').fetchone()[0]
print(f'freelist = {100*fl/pc:.1f}% de page_count')"
# AVANT : 62.6%   APRES : < 5%

# 5.3 — Un seul declarant par table (INV-1)
python -m pytest tests/ -k "schema or declarant" -q

# 5.4 — Aucun module post-constitution > 1500 lignes (INV-10)
python -m pytest tests/ -k "module_size" -q
wc -l opencode.py vpn/manager.py free/pool/__init__.py app/protocol/mapping.py
# AVANT : 33618 / 11471 / 8362 / 6329
# APRES : facade <= 400 pour opencode.py ; chaque module <= 1500

# 5.5 — Aucune dependance non versionnee (INV-8)
python -m pytest tests/ -k "artifact" -q
git status --short | grep "^??" | wc -l
# AVANT : 29 fichiers non suivis (dont la spec 023 entiere)
# APRES : 0 (hors livrables d'audit)
```

---

## 6. Vérifier la Phase 3 — « gates et langage »

```bash
# 6.1 — Le gate option-map couvre toutes les pages (INV-11)
python -m pytest tests/test_option_map.py -q -v
# AVANT : 4 tests, 1 page inspectee sur 15
# APRES : couverture >= 90% des 184 controles

# 6.2 — La CI tourne sur Windows et Linux (INV-12)
grep -c "matrix" .github/workflows/ci.yml
grep -c "runs-on" .github/workflows/ci.yml
# AVANT : matrix 0   APRES : matrix >= 1

# 6.3 — platform_audit est cable
grep -rn "platform_audit" .github/workflows/ci.yml scripts/gate.ps1
# AVANT : (aucun resultat)   APRES : au moins une occurrence

# 6.4 — gate.ps1 utilise -m et non -k
grep -n 'pytest' scripts/gate.ps1
# AVANT : python -m pytest -k "not docker"   APRES : -m "not docker"

# 6.5 — Aucune assertion tautologique
grep -rn "or True" --include="test_*.py" tests/ | wc -l
# AVANT : 5   APRES : 0
```

---

## 7. Vérification finale — les 8 success criteria

```bash
# SC-001 : 6/6 client_usable sur un cycle de 120 s
curl -s http://127.0.0.1:4000/api/vpn-status | python -c "
import json,sys; d=json.load(sys.stdin)
print(sum(1 for s in d['stations'] if s['client_usable']),'/6')"

# SC-002 : 0 requete perdue par exception avalee sur 24 h
curl -s http://127.0.0.1:4000/metrics | grep -E "swallowed|http_502"

# SC-003 : donnees bornees
du -sh logs/ ; ls -lh logs/registry.db | awk '{print $5}'

# SC-004 : taux de rotation WARP
python -c "
import sqlite3
c=sqlite3.connect('file:logs/registry.db?mode=ro',uri=True)
r=c.execute(\"select sum(result='ok'), count(*) from rotation_jobs
             where started_at > strftime('%s','now')-86400\").fetchone()
print(f'{r[0]}/{r[1]} = {100*r[0]/r[1]:.0f}%')"

# SC-005 : 0 divergence entre surfaces d'etat sur 24 h
#   (le test INV-7 tourne en boucle ; cf. §4.1)

# SC-006 : aucun module post-constitution > 1500 lignes
python -m pytest tests/ -k "module_size" -q

# SC-007 : les 8 gates de la constitution
python scripts/ci_contracts.py && python scripts/config_audit.py --strict \
  && python scripts/platform_audit.py --strict && python -m pytest -k "not docker" -q

# SC-008 : un git clone neuf produit une flotte fonctionnelle
git clone . /tmp/opencode-neuf && cd /tmp/opencode-neuf \
  && docker compose config | grep -c "gluetun-016:local"
# AVANT : 1 (seul vpn-gluetun-4)   APRES : 6
```

---

## 8. Faisabilité des correctifs P0 les plus délicats (vérifiée)

Avant d'exécuter T001–T003, l'auditeur principal a vérifié que les correctifs
sont **techniquement applicables** — un plan qui propose un correctif
impossible serait pire qu'inutile.

**T001 — déplacer `_resp_json_hors_boucle`** : ✅ **sans dépendance.**
La fonction (`opencode.py:33605-33617`) ne référence que `asyncio` et son
paramètre `resp` :

```python
async def _resp_json_hors_boucle(resp):
    if not resp.headers.get("content-type", "").startswith("application/json"):
        return {}
    return await asyncio.to_thread(resp.json)
```

Aucun symbole défini plus bas n'est utilisé → le déplacement avant le bloc
`__main__` (par ex. après `ensure_min_tokens`, ~l. 20511) est **sûr**.

**T002 — `sys.modules.setdefault("opencode", sys.modules["__main__"])`** :
✅ **emplacement identifié.** La ligne se place immédiatement avant
`if __name__ == "__main__":` (`opencode.py:33459`), qui suit un import
(`ops.lock.acquire_instance_lock`, l. 33457). Aucun code ne lit
`sys.modules["opencode"]` avant ce point (vérifié : les seules occurrences
concernent `curl_cffi`, l. 1954-1980).

**T003 — fusionner les deux `elif _geo_tunnel`** : ✅ **fusion claire.**
Les deux branches testent la **même** condition :

```python
            if free_result is not None:
                resp, _, _actual_model, _actual_ip = free_result
                model_id = _actual_model
            elif _geo_tunnel:
                model_id = _actual_model          # ← UnboundLocalError
            elif _paused_sans_cle:
                return _anthropic_error(503, …)
            elif _geo_tunnel:                     # ← JAMAIS atteint
                async with _open_via_pool(…) as resp:
                    a_headers = dict(resp.headers)
            else:
                resp, a_headers = await _do_request_with_retry(…)
```

**Le correctif** : supprimer la première branche (l. 31588-31589) et laisser la
seconde s'exécuter — `_open_via_pool` route alors **effectivement** par le
tunnel, et `model_id` n'a pas besoin d'être réassigné (le pool choisit le
modèle).

---

## 9. En cas d'échec — diagnostic

| Symptôme | Cause probable | Où regarder |
|---|---|---|
| Un lot casse la flotte | un correctif P0 appliqué sur les 6 stations | revenir au commit précédent, appliquer sur 1 station |
| Un test de contrat INV échoue | le correctif est incomplet | la tâche citée dans `contracts/invariants.md` |
| Le proxy ne redémarre plus | import cassé par un déplacement de lot | `tail -n 50 logs/app-stderr.log` |
| `database is locked` | connexions SQLite non fermées (T135) | `logs/debug.log` via `tail` |
| La flotte tombe après `compose up` | T005 non fait | comparer `docker ps` et `docker compose config` |
| Le client voit un contexte ×2 | une porte refait l'erreur d'usage (V-52/V-55) | `_extract_cache_tokens` sur les 4 chemins |

**Règle** : ne jamais appliquer deux lots sans repasser la gate locale (§2) et
les contrats de la phase (§3–§6).
