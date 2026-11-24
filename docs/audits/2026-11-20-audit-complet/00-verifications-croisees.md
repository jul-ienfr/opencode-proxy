# Vérifications croisées — confirmations indépendantes des rapports

L'auditeur principal a revérifié personnellement les findings les plus lourds
des rapports d'audit, pour ne pas dépendre du seul jugement d'un sous-agent.
Chaque entrée indique la commande de vérification et son résultat brut.

---

## V-1 — P0-1 du rapport WARP : le probe post-rotation est cassé ✅ CONFIRMÉ

**Affirmation** : `opencode.py:24380` lit `capture["body"]` et `capture["url"]`,
mais les captures actives portent `body_raw` et `endpoint`.

**Vérification** :

```
free/captures/2.2.5.json: body=False body_raw=True url=False endpoint=True
free/captures/2.3.0.json: body=False body_raw=True url=False endpoint=True
free/official_payload.json: body=True body_raw=False url=False endpoint=False
```

Code fautif (`opencode.py:24378-24386`) :

```python
with open(capture_path, encoding="utf-8") as fh:
    capture = json.load(fh)
body = copy.deepcopy(capture["body"])          # KeyError sur 2.2.5 / 2.3.0
url = str(capture.get("url") or "")            # "" sur 2.2.5 / 2.3.0
except Exception as exc:
    _log(f"  [post-rotation-probe] capture illisible: {exc}")
    return False
if not url:
    return False                              # sortie systématique
```

**Deux causes d'échec indépendantes** : la clé `body` lève `KeyError` (capturé
par le `except` → `return False`), et même sans l'exception, `url` serait vide
→ `return False` à la ligne suivante.

**Impact confirmé** : le probe post-rotation retourne `False` **en
permanence**, ce qui explique les 599 `quota_exhausted` mesurés par
l'auditeur WARP — chaque IP fraîche est jugée épuisée après 4 recréations de
conteneur.

---

## V-2 — P0-3 du rapport WARP : `ip_state` n'a aucune ligne WARP ✅ CONFIRMÉ

**Affirmation** : `probe.py` code `provider="nordvpn"` en dur, donc
`ip_state` n'est jamais alimenté pour WARP.

**Vérification SQL** :

| Table | `nordvpn` | `warp` |
|---|---|---|
| `ip_state` | **283** | **0** |
| `ip_state_events` | 573 | 116 |

**Vérification du code** (`free/ip_registry/adapters/probe.py`) : la chaîne
`"nordvpn"` apparaît en dur aux lignes **115, 132, 134, 141, 185, 207, 212,
220** — soit 8 sites dans un adaptateur de sonde qui devrait être
générique.

**Conséquence** : `opencode.py:16031` fait `_store.get("warp", ip)` → `None`
→ `ip_proven = False` **structurellement**, pour toutes les IP WARP, quelle
que soit leur santé réelle.

**Détail aggravant** relevé par l'auditeur : `ip_state_events` enregistre
`ip = 'socks5:WARP 3 IPv6'` — un **label logique**, pas une adresse IP. Cela
viole le principe V (« une IP d'egress n'appartient qu'à une station ») dans
le journal lui-même, puisque deux stations différentes partagent le même
`wire_fp` `c333745d…` sur ces entrées.

---

## V-3 — `free/wire_admission.py` mort ✅ CONFIRMÉ (deux auditeurs indépendants)

**Affirmation** : le module d'admission n'est importé par aucun chemin runtime.

**Vérification** :

```
grep -rn "wire_admission" --include="*.py" .
→ ./tests/unit/test_wire_admission.py:2:from free import wire_admission as wa
```

**Un seul mentionneur dans tout le dépôt, et c'est son test.** Le rapport 04
(WARP) est arrivé indépendamment à la même conclusion que mon audit direct
(§18 de `00-live-evidence.md`).

**Importance** : c'est le garde-fou du principe XVIII, que la constitution
qualifie de « bug de sévérité maximale » s'il est enfreint. Corrélation
mesurée : `ip_state` ne contient **que** `unproven` (167) et `proven` (116) —
**jamais** `spent` ni `burned` — alors que 7 570 buckets sur 8 519 sont en
`last_status=429`.

---

## V-4 — P1-5 du rapport WARP : `rotation_jobs` ne sont jamais purgés ✅ CONFIRMÉ

**Affirmation** : `RotationBudgetStore.allow()` lit en filtrant mais ne
supprime jamais ; la docstring promet une purge qui n'existe pas.

**Vérification SQL** (`logs/registry.db`) :

| `result` | Nombre |
|---|---|
| `failed` | 1 377 |
| `ok` | 1 254 |
| `abandoned` | 1 042 |
| **`started` (sans `finished_at`)** | **135** |

- `finished_at IS NULL` : **135**
- dont > 1 h : **134**
- dont > 24 h : **133**

Plus ancienne ligne : **2026-11-07** (13 jours). La table croît d'environ
293 lignes/jour.

**Corrélation avec l'audit 019** : il signalait 1 042 `started` orphelins. La
réconciliation a bien eu lieu (1 042 lignes sont passées en `abandoned`),
**mais la fuite continue** : 135 nouvelles lignes sans état terminal. Le
correctif a traité le stock, pas le flux.

---

## V-5 — P1-6 du rapport WARP : le runtime réécrit la config de l'opérateur ✅ CONFIRMÉ

**Affirmation** : `reconcile_warp_ipv4` ré-active des endpoints IPv4 que
l'opérateur a désactivés, et réécrit `config.yaml` toutes les 300 s.

**Vérification** : `config.yaml` porte 12 lignes WARP IPv4 à
`enabled: false` — l'intention de l'opérateur est explicite. Le code
(`free/pool/__init__.py:4968-4995`) force `endpoint.enabled = want` puis
`yaml_set(...)`, ce qui **écrase** cette valeur.

**Impact** : conflit d'autorité config ↔ runtime, où le runtime gagne et
persiste. C'est une violation du principe XII (« le GUI affiche la valeur
effective ») appliquée à la config : l'opérateur ne peut pas désactiver
durablement une option.

---

## V-6 — Rapport 08 : `web_search`/`web_fetch` inertes ❌ FAUX POSITIF

**Affirmation de l'auditeur 08** : « ~14 contrôles GUI inertes », aucun
consommateur de `yaml_get("web_search", …)` hors du routeur d'affichage.

**Vérification** :

```
opencode.py:19721:    if not yaml_get("web_search", "enabled", True):
opencode.py:19731:    mode = yaml_get("web_search", "mode", "duckduckgo")
opencode.py:19732:    target_model = yaml_get("web_search", "target_model", None)
opencode.py:19733:    max_results = yaml_get("web_search", "max_results", 5)
opencode.py:19734:    timeout = yaml_get("web_search", "timeout", 10)
opencode.py:19735:    via_vpn = bool(yaml_get("web_search", "via_vpn", False))
opencode.py:19868:    if not yaml_get("web_fetch", "enabled", True):
opencode.py:19904-19908: mode, target_model, timeout, max_bytes, via_vpn
```

**Les consommateurs existent**, dans `_handle_web_search` et le handler
`web_fetch`, tous deux dans `opencode.py`. Le grep de l'auditeur ne couvrait
pas ce fichier.

**Seul `on_error` reste sans consommateur runtime identifié** — soit 2
contrôles sur 14, pas 14.

**Leçon méthodologique** (à appliquer aux rapports suivants) : un grep
d'absence de consommateur **doit** inclure `opencode.py`, où vivent la
majorité des consommateurs runtime (33 618 lignes). Un finding de ce type
doit citer la commande **et** la liste des fichiers couverts.

**Correction appliquée** dans `08-dashboard-observability-ops.md` §4.3.

**Corroboration indépendante** : le rapport 06 (monolithe), écrit par un
autre auditeur sans connaissance de cette correction, classe
`web_search.*` / `web_fetch.*` parmi les clés **avec** consommateur :

> | `web_search.*` / `web_fetch.*` | `server/websearch.py` | OK |

Vérification : `server/websearch.py` (19 166 o) existe et expose
`execute_web_fetch` (`:302`), `_execute_web_fetch` (`:445`), et documente en
docstring `:17` que `_handle_web_search` / `_handle_web_fetch` sont
« NON déplacés » (restés dans `opencode.py`). **Deux auditeurs indépendants
concluent donc que ces options sont réelles**, contre un faux positif du
rapport 08.

---

## V-7 — Rapport 10 : `gate.ps1` désélectionne 46 tests ✅ CONFIRMÉ

**Affirmation** : `scripts/gate.ps1:44` utilise `-k "not docker"` au lieu de
`-m "not docker"`, écartant des tests par correspondance de nom.

**Vérification** : `scripts/gate.ps1:44` →
`Step "pytest" { python -m pytest -k "not docker" --cov=. --cov-fail-under=60 }`

Le filtre `-k` sélectionne par **nom de test**, donc tout test dont le nom
contient « docker » est écarté — y compris `test_no_docker_exec_hot_path.py`
(3 tests qui vérifient justement qu'aucun `docker exec` n'est fait dans le
chemin chaud) et `test_docker_events.py` (7 tests). La CI, elle, utilise
correctement `-m` (`ci.yml:76`).

**Impact** : la gate locale est **plus faible** que la CI sur ce point, donc
un développeur qui valide en local peut livrer une régression que la CI
attrapera — ou l'inverse selon le filtre. L'incohérence est en soi le bug.

---

## V-8 — Rapport 10 : `platform_audit --strict` n'est câblé nulle part ✅ CONFIRMÉ

**Vérification** :

```
grep -rn "platform_audit" .github/workflows/ci.yml scripts/gate.ps1 \
        scripts/quality_gate.py scripts/ci_contracts.py
→ (aucun résultat)
```

Le script existe (`scripts/platform_audit.py`, 242 l.) et passe (0 violation
sur 279 fichiers runtime, exécuté par l'auditeur 08), mais **la gate nommée
par la constitution n'est invoquée par aucun pipeline**. Elle n'est couverte
qu'indirectement par `tests/test_platform_audit.py`.

---

## V-9 — Rapport 10 : `ci.yml` n'a pas de matrice d'OS ✅ CONFIRMÉ

**Vérification** : `grep -n "runs-on\|matrix\|strategy" .github/workflows/ci.yml`
→ `10:    runs-on: ubuntu-latest` (aucune autre occurrence).

Le principe XV exige que « la gate non-Docker tourne sur **Windows et
Linux** (matrice CI) ». Elle ne tourne que sur Linux. Quatre tests
Windows-only sont de ce fait skippés en CI sans contrepartie POSIX.

---

## V-10 — Rapport 10 : assertions tautologiques ✅ CONFIRMÉ

**Vérification** : 5 occurrences de `assert ... or True` dans les tests :

| Fichier:ligne | Contenu |
|---|---|
| `tests/test_cache_anthropic_conformance.py:247` | `assert "cache_control" in out["messages"][-1] or True` |
| `tests/test_chaos_lifecycle.py:75` | `assert mgr.calls.get("compose_up", 0) == 0 or True, "aucun compose post-cancel"` |
| `tests/test_coverage_boost.py:11` | `assert hasattr(docker_events, "DockerEventWatcher") or True` |
| `tests/test_lot7_ops.py:118` | `assert isinstance(text, str) and "vpn_station_connected" in text or True` |
| `tests/test_tool_schema_normalization.py:623` | `assert "additionalProperties" not in json.dumps(...) or True` |

Ces assertions **ne peuvent pas échouer**. `tests/test_socks5_quality_rotation.py:64`
est un faux positif (lambda légitime).

---

## V-11 — `test_tool_compat.py` ne contient aucun test ✅ CONFIRMÉ

**Vérification** :

```
grep -c "^def test_" tests/test_tool_compat.py  → 0
wc -l tests/test_tool_compat.py                 → 403
```

403 lignes, **zéro fonction de test**, zéro assertion. C'est un script live
qui, lancé par pytest, enverrait de vraies requêtes — risque de brûlure
d'IP (principe XVIII).

---

## V-12 — P0-1 du rapport 08 : fenêtre de perte de données au boot ✅ CONFIRMÉ

**Affirmation** : `init_requests_schema_fast` ne crée que 4 des 25 colonnes
migrées, et le writer démarre avant la migration de fond.

**Vérification du code** (`observability/db.py:536-542`) :

```python
# [P0.6] 3 colonnes d'observabilité ajoutées au boot (ALTER = métadonnée,
# ~0 ms) pour éviter la course « insert avant migration de fond ».
for _col, _ddl in (
    ("ttfb_ms", "INTEGER"),
    ("stop_reason", "TEXT"),
    ("aborted", "INTEGER DEFAULT 0"),
    ("server_host", "TEXT"),
):
```

Le commentaire **nomme la course** (« insert avant migration de fond ») mais
la parade ne couvre que **4 colonnes sur 25** (`_REQUEST_COLUMN_MIGRATIONS`
en déclare 25). Les 21 autres (`client_ip`, `account_alias`, `tools`,
`free_status`, `station`, `attempt_no`, `final_status`, …) ne sont créées
que par `migrate_and_canary`.

**Vérification de l'ordonnancement** (`opencode.py`) :

```
4284:    _db_writer_task = asyncio.create_task(_db_writer_loop())
4289:    app.state._db_migrate_task = asyncio.create_task(_db_migrate_bg())
```

Le writer (4284) démarre **5 lignes avant** la migration (4289).

**Vérification de l'avalement** : `observability/db.py:818` attrape
l'exception par item et la jette avec un simple `debug_fn`.

**Nuance importante** : sur la base **actuelle** (déjà migrée), le bug est
invisible. Il se manifeste sur base neuve (`rm logs/requests.db`) ou après
restauration — précisément les scénarios de reprise où l'observabilité
compte le plus.

---

## V-13 — P0-05 de l'audit 019 : régression confirmée ✅

Voir §13 de `00-live-evidence.md`. `free/pool/__init__.py:5389` conserve le
`logger.info("[warp-rotate] on_request …")` dans le chemin chaud, protégé
seulement par une condition `proxy_mode in ("socks5", "auto_egress")` — et
`auto_egress` est le mode actif. Mesure : **23 598** lignes dans le
`debug.log` courant.

---

## Synthèse des vérifications

| # | Finding | Source | Verdict |
|---|---|---|---|
| V-1 | Probe post-rotation cassé (`capture["body"]`) | rapport 04 | ✅ confirmé |
| V-2 | `ip_state` sans WARP (provider en dur) | rapport 04 | ✅ confirmé |
| V-3 | `wire_admission` mort | rapports 04 + audit direct | ✅ confirmé ×2 |
| V-4 | `rotation_jobs` jamais purgés | rapport 04 | ✅ confirmé |
| V-5 | Runtime réécrit la config opérateur | rapport 04 | ✅ confirmé |
| V-6 | `web_search`/`web_fetch` inertes | rapport 08 | ❌ **faux positif** |
| V-7 | `gate.ps1` `-k` au lieu de `-m` | rapport 10 | ✅ confirmé |
| V-8 | `platform_audit --strict` non câblé | rapport 10 | ✅ confirmé |
| V-9 | CI mono-OS (principe XV) | rapport 10 | ✅ confirmé |
| V-10 | 5 assertions tautologiques | rapport 10 | ✅ confirmé |
| V-11 | `test_tool_compat.py` sans test | rapport 10 | ✅ confirmé |
| V-12 | Fenêtre de perte de données au boot | rapport 08 | ✅ confirmé |
| V-13 | Régression du spam chemin chaud | audit direct | ✅ confirmé |
| V-14 | Capacité WARP IPv4 comptée à double | rapport 04 | ✅ confirmé |
| V-15 | Profil `1.19.5` → capture supprimée | rapport 04 | ✅ confirmé |

**14 confirmations, 1 faux positif corrigé.** Les rapports sont fiables ;
le seul écart venait d'un grep incomplet, désormais documenté comme piège
méthodologique.

---

## V-14 — P1-1 du rapport WARP : la capacité WARP IPv4 est comptée à double ✅ CONFIRMÉ

**Affirmation** : `provider_capabilities.capacity()` déclare 0 pour WARP
IPv4, mais aucun code ne l'appelle — les `/32` entrent donc dans
`unique_buckets`.

**Vérification du code** (`free/provider_capabilities.py:88-96`) :

```python
def capacity(provider: str, ip: str) -> int:
    """Capacité d'un bucket : 0 pour WARP IPv4, 1 sinon."""
    name = str(provider or "").lower()
    if name == WARP:
        try:
            return 0 if ipaddress.ip_address(str(ip).strip()).version == 4 else 1
        except ValueError:
            return 0
    return 1
```

La fonction est **correcte** et encode exactement la règle du principe XVIII
(« l'IPv4 WARP est une NAT partagée et ne crée aucune capacité »).

**Vérification des consommateurs** :

```
grep -rn "\.capacity(" --include="*.py" .
→ (aucun résultat)
```

**La fonction n'est appelée nulle part.** `free/pool/accounting.py:31-41`
construit ses blocs sans la consulter. Mesure de l'auditeur : 17 buckets
comptés pour une capacité réelle de 12 (5 `/32` IPv4 surévaluent la flotte
de **41 %**).

**Corrélation** : `egress_weights.warp = 24` (`config.yaml:156`) s'applique à
cette capacité gonflée, ce qui donne au WARP plus de trafic qu'il n'en
mérite — cohérent avec le score moyen WARP mesuré à 41,2 contre 73,7 pour
les proxys publics (§8 de `00-live-evidence.md`).

---

## V-15 — P1-7 du rapport WARP : un profil vérifié pointe vers une capture supprimée ✅ CONFIRMÉ

**Affirmation** : `free/profiles/verified_profiles.json` déclare un profil
`1.19.5` dont le fichier de corps est absent du disque.

**Vérification** :

```
$ ls free/captures/*.json
free/captures/2.2.5.json
free/captures/2.3.0.json

$ git status --short free/captures/
 D free/captures/1.18.31.json
 D free/captures/1.19.3.json
 D free/captures/1.19.5.json      ← supprimé du disque
 M free/captures/2.2.5.json
?? free/captures/2.3.0.json
```

`verified_profiles.json` déclare toujours
`"profile_id": "oc-1.19.5-desktop-11"`, `"version": "1.19.5"`,
`"body_capture": "free/captures/1.19.5.json"` — un fichier **supprimé**
(3 captures ont été retirées : `1.18.31`, `1.19.3`, `1.19.5`).

**Le repli est silencieux** : `free/runtime_identity.py:116-128`
(`_capture_body`) fait `path.read_text` dans un `except Exception: return
None`, et `build_body` retombe sur `template_official_body` **sans logger**.

**Impact** : d'après l'auditeur, la distribution HMAC envoie **6 des 12
labels WARP** sur le profil `1.19.5` — ces 6 stations servent donc un corps
**reconstruit** tout en annonçant l'UA et les en-têtes du profil `1.19.5`.
Le principe XVIII exige que « la sonde de qualification et la sonde live
envoient le corps officiel exact » : un template n'est pas la capture.

**Note de cohérence** : ce constat recoupe le P0-1 (V-1) — la même famille
de bugs « le code lit un artefact qui n'existe plus dans la forme attendue ».
Les deux se corrigent par un test qui **ouvre le fichier réellement
configuré** au lieu de tester sur une fixture.

---

## V-16 — P0 du rapport 06 : double exécution du corps de module ✅ CONFIRMÉ

**Affirmation** : `opencode.py` lancé en `__main__` est ensuite réimporté
sous le nom `opencode`, ce qui exécute **deux fois** le corps du module dans
le même processus.

**Vérification — les réimporteurs** :

```
gui/tray.py:253:            import opencode as _oc
dashboard/api.py:1995:        import opencode as oc
```

**Vérification — la trace d'exécution** (`logs/debug.log`) :

```
83340: [2026-11-16 15:50:43Z]   [db] SQLite connection established: …\logs\requests.db
83417: [2026-11-16 15:50:47Z]   [db] SQLite connection established: …\logs\requests.db
```

**Deux initialisations de la connexion SQLite, à 4 secondes d'intervalle.**
Le module s'exécute bien deux fois.

**Conséquences mesurables** (relevées par l'auditeur) :

| Ressource | Effet |
|---|---|
| `sqlite3.Connection` sur `requests.db` | **2 objets** ouverts |
| `_token_usage`, `_free_model_cooldowns` | **2 dicts** distincts |
| `_free_ip_pool` | **2 objets** ; celui du second namespace reste `None` à vie |
| `_geo_block_total` | **0** dans le second namespace |
| Lecteurs dashboard (`vpn_status_routes.py:327,339`, `geo_routes.py:136`) | lisent le **second** namespace → télémétrie géo **structurellement fausse** |

**C'est la cause racine probable de plusieurs anomalies mesurées
indépendamment** : les 58 stations fantômes et les compteurs
`usable_stations=20` pour 6 stations (§1 de `00-live-evidence.md`) sont
exactement le genre de symptôme que produit la lecture d'un namespace
parallèle dont l'état est vide ou partiel.

---

## V-17 — P0 du rapport 06 : `_resp_json_hors_boucle` jamais défini ✅ CONFIRMÉ

**Affirmation** : la fonction est définie **après** le bloc `__main__`, donc
jamais exécutée dans ce namespace ; ses appelants lèvent `NameError`, avalé
par un `except Exception`.

**Vérification — les trois sites** :

```
12044:                data = await _resp_json_hors_boucle(resp)      ← appel
31664:            data = await _resp_json_hors_boucle(resp)          ← appel
33605:async def _resp_json_hors_boucle(resp):                        ← définition
33459:if __name__ == "__main__":                                     ← bloc main
```

La définition (33605) est **146 lignes après** le début du bloc `__main__`
(33459), et le bloc se termine par un appel **bloquant** :

```python
        try:
            run_terminal_loop(ROUTES, _token_usage, _token_lock)
        except KeyboardInterrupt:
            …
        finally:
            _debug("  [main] cleaning up after terminal loop exit...")
            …
# [A11] Aide placee en fin de module : l'inserer juste avant `async def responses` la placerait
# ENTRE le decorateur @app.post("/v1/responses") et sa fonction, qui perdrait alors sa route
# (mesure : FastAPI renvoyait 422 au lieu de 200). La resolution se faisant a l'appel, l'ordre
# du module est sans effet.
async def _resp_json_hors_boucle(resp):
```

**Le commentaire est factuellement faux.** « La résolution se faisant à
l'appel » est vrai pour la **liaison de nom**, mais faux pour l'**exécution
de la définition** : en Python, `async def` est une instruction exécutée
séquentiellement. Tant que le bloc `__main__` bloque (via
`run_terminal_loop` ou `run_gui`), la ligne 33605 n'est **jamais atteinte**,
donc le nom n'est **jamais lié** dans le namespace `__main__`.

**Impact** : les deux appels lèvent `NameError`, capturé par un
`except Exception` — la jambe Anthropic non-stream de `/v1/responses` répond
**502 « Upstream returned non-JSON response »** alors que la réponse amont
est valide. Le bug est **silencieux depuis le 2026-09-15** (commit
`f73cce56` qui a déplacé la fonction).

**Note** : ce P0 illustre le P1 n°4 du même rapport — **367 `except …: pass`
nus sur 1 225 gestionnaires (30 %)**. C'est ce taux qui rend le bug
indétectable depuis deux mois.

**Le correctif** est trivial (déplacer la définition avant le bloc
`__main__`, en préservant l'ordre décorateur/fonction) ; le **test de
non-régression** doit appeler `/v1/responses` en non-stream et vérifier un
200 — pas seulement importer le module, ce qui ne reproduit pas le bug.

### V-17.1 — Le coût du P0-3 mesuré en base ✅ (incertitude levée)

L'auditeur du rapport 06 a déclaré dans ses incertitudes :

> « Les compteurs runtime (nombre de 502 sur `/v1/responses` non-stream)
> n'ont pas pu être mesurés ; `sqlite3` est indisponible sur ce poste. »

**Mesure effectuée** par l'auditeur principal, via Python + `sqlite3`
(disponible en bibliothèque standard) :

| `protocol` | `is_stream` | n | dont `HTTP 502` |
|---|---|---|---|
| `openai` | 1 (stream) | 34 727 | **0** |
| `openai` | 0 (non-stream) | 8 074 | **502** |

**Les 502 erreurs `HTTP 502` sont toutes non-stream, et il y en a
exactement 502.** Le taux d'échec du chemin non-stream est de
**502 / 8 074 = 6,2 %**.

Échantillon des dernières occurrences : toutes sur `jev-1.13-free`, entre
19:28 et 19:41 le 2026-11-16.

**Le mécanisme est confirmé par le code** (`opencode.py:31664-31669`) :

```python
try:
    data = await _resp_json_hors_boucle(resp)      # NameError en __main__
except Exception:
    _debug(f"  ✗ non-JSON response from {endpoint}")
    _log(f"  UPSTREAM DECODE ERROR: non-JSON response from {endpoint}")
    return _openai_error(502, "Upstream returned non-JSON response")
```

Le `NameError` levé par l'appel à une fonction jamais définie est capturé
par ce `except Exception` **générique**, qui produit précisément le message
`502 "Upstream returned non-JSON response"`.

**Conclusion** : le P0-3 n'est pas théorique. Il a fait échouer **502
requêtes réelles**, et son taux (6,2 % du chemin non-stream) est masqué par
le fait que 81 % du trafic est en streaming (où le bug ne se déclenche pas,
le chemin de code étant différent).

**C'est la démonstration que le P1 n°4 du même rapport (30 % de
`except: pass`) n'est pas une question de style** : c'est ce qui a rendu un
`NameError` franc invisible pendant deux mois et 502 requêtes.

---

## V-18 — P0-3 du rapport 06 : `_actual_model` non lié ✅ CONFIRMÉ

L'auditeur du rapport 06 signalait ce P0 comme « **à confirmer en
exécution** » (le chemin exige une route géo active). Vérification statique
par l'auditeur principal (`opencode.py:31578-31590`) :

```python
try:
    free_result = await _try_free_model_first(
        anthro_body, a_headers, "anthropic", model_id,
        forced_pool=getattr(request.state, "_geo_forced_pool", None),
        req_id=req_id,
    )
    if free_result is not None:
        resp, _, _actual_model, _actual_ip = free_result   # ← seule assignation
        model_id = _actual_model
    elif _geo_tunnel:
        model_id = _actual_model                            # ← lecture sans assignation
    elif _paused_sans_cle:
        return _anthropic_error(503, "All API keys paused and no free model available.")
```

**La lecture (31589) est dans une branche `elif`, donc atteinte précisément
quand l'assignation (31587) ne l'a pas été.** Quand `free_result is None` et
`_geo_tunnel` est vrai, Python lève `UnboundLocalError`.

**Pourquoi c'est la même classe que le bug A24** — documenté dans le code
lui-même (`opencode.py:29147-29157`) :

> « Sans cette déclaration, Python en fait une variable LOCALE : la première
> lecture levait `UnboundLocalError`, avalée par le `except Exception` du
> handler → le client recevait un `text/event-stream` de 0 octet »

Le projet a donc **déjà rencontré et documenté** ce motif exact, et il s'est
reproduit ailleurs dans le même fichier.

**Verdict** : l'analyse statique est **sans ambiguïté** — le `UnboundLocalError`
est certain dès que la branche est atteinte. Ce qui reste incertain est la
**fréquence** (dépend de la configuration géo de l'opérateur), pas
l'existence du bug.

**Conséquence pour le plan** : le lot L1 (« corriger les P0 ») porte bien sur
**3 correctifs** dans `opencode.py`, tous de faible effort :
1. `sys.modules.setdefault("opencode", sys.modules["__main__"])` (double
   exécution) ;
2. déplacer `_resp_json_hors_boucle` avant le bloc `__main__` (`NameError` →
   502 requêtes perdues, mesuré) ;
3. lier `_actual_model` par défaut dans la branche `elif _geo_tunnel:`
   (`UnboundLocalError`).

### V-18.1 — Le même site cache un contournement de la porte géo ⚠️

En vérifiant le P0-3, l'auditeur principal a trouvé que la chaîne `elif`
contient **deux tests identiques** de `_geo_tunnel` :

```python
31588:            elif _geo_tunnel:
31589:                model_id = _actual_model          # UnboundLocalError
31590:            elif _paused_sans_cle:
31591:                return _anthropic_error(503, ...)
31595:            elif _geo_tunnel:                     # ← JAMAIS atteint
31596:                # Axe A: geo-restricted paid → must route through tunnel station
31597:                async with _open_via_pool(
31598:                    endpoint, anthro_body, a_headers,
31599:                    is_stream=False,
31600:                    forced_pool=getattr(request.state, "_geo_forced_pool", None),
31601:                ) as resp:
31602:                    a_headers = dict(resp.headers)
31606:            else:
31607:                resp, a_headers = await _do_request_with_retry(endpoint, anthro_body, a_headers, "anthropic")
```

**Deux conséquences indépendantes :**

1. **Le second `elif _geo_tunnel` (31595) est inatteignable** — un `elif`
   après un `elif` de même condition est toujours faux. Le bloc
   `_open_via_pool` (31597-31602), qui est la **route tunnel payante
   obligatoire pour une politique géo `require_vpn`**, est donc **du code
   mort**.

2. **Si l'on corrige naïvement le `UnboundLocalError`** en liant
   `_actual_model = model_id` dans la branche 31588-31589, alors la branche
   `elif` de 31595 reste inatteignable et la requête tombe dans le `else`
   (31606) → `_do_request_with_retry` → **part en direct, sans tunnel**.

**C'est un contournement de la porte géo.** Un opérateur qui configure
`geo.enabled: true` avec une politique `require_vpn` croit que le trafic
passe par le tunnel ; en réalité, sur ce chemin (free épuisé + géo
restreinte), il sort **en direct** avec son IP réelle.

**Sévérité** : à traiter comme un P0 de sécurité, pas seulement comme un bug
de liaison de variable. Le correctif doit **fusionner les deux branches**
(supprimer le `elif` dupliqué et router effectivement via `_open_via_pool`),
et le test de non-régression doit vérifier que la requête **passe par le
tunnel** quand `_geo_tunnel` est vrai — pas seulement qu'elle ne lève pas
d'exception.

**Reachabilité prouvée** — la configuration active contient exactement la
politique qui exige le tunnel :

```yaml
# config.yaml:740-757
geo:
  enabled: true
  version: 1
  allow_direct_when_compatible: true
  pin_inline_timeout_s: 2
  policies:
    meta-muse-spark:
      allowed_countries: [United States, Canada, United Kingdom, Australia, Japan, Singapore]
      last_checked_at: '2026-08-20T00:00:00Z'
      mode: strict
      require_vpn: true          # ← exige le tunnel
```

`geo.enabled: true` et une politique à `mode: strict` + `require_vpn: true` :
le chemin est **actif en production**, pas hypothétique. La porte géo
configurée pour exiger un tunnel ne l'exige pas sur ce chemin de code.

---

## V-19 — P0-1 du rapport WARP : la flotte WARP ne tourne plus du tout ✅ CONFIRMÉ ET AGGRAVÉ

L'auditeur WARP a fourni une corrélation indicative (« 183 → 49 → 12 → 1 »).
**Mesure exacte en base** (`rotation_jobs`, groupé par jour) :

| Jour | total | `ok` | **taux de succès** |
|---|---|---|---|
| 2026-11-14 | 146 | 91 | 62,3 % |
| 2026-11-15 | 425 | **187** | 44,0 % |
| 2026-11-16 | 331 | 173 | 52,3 % |
| 2026-11-17 | 217 | 33 | 15,2 % |
| 2026-11-18 | 159 | **0** | **0,0 %** |
| 2026-11-19 | 95 | **0** | **0,0 %** |
| 2026-11-20 | 129 | **0** | **0,0 %** |

**Trois jours consécutifs sans une seule rotation WARP réussie** (18, 19,
20 novembre), alors que le rotator continue de tourner et d'échouer 128 fois
le 20. La bascule de schéma de capture date du **2026-11-17 17:30**
(réécriture par `scripts/opencode_client_sync.py:515`) — et le taux de
succès s'effondre précisément ce jour-là (52,3 % → 15,2 %), puis tombe à
zéro.

**La chronologie est concluante** : le changement de schéma a cassé le
probe, ce qui rend toute rotation impossible. Le rotator consomme donc
~128 tentatives/jour × 4 recréations × ~90 s de health ≈ **plusieurs heures
de churn Docker quotidien pour zéro résultat**.

**C'est le P0 le plus coûteux du système en ressources**, et il est
invisible : le watchdog tourne, les conteneurs sont `healthy`, seules les
rotations échouent silencieusement.

---

## V-20 — Cause du gonflement de `registry.db` (160 Mo) ✅ CONFIRMÉ

L'auditeur WARP attribue le gonflement à `_expire_recreated` qui écrit
~474 observations par cycle. **Mesure en base** :

| Table | Lignes |
|---|---|
| `egress_observations` | **326 812** |

Répartition par `status` :

| `status` | Lignes | Part |
|---|---|---|
| **0** (aucun statut réel) | **305 232** | **93,4 %** |
| 429 | 11 278 | 3,5 % |
| 200 | 10 302 | 3,2 % |

Répartition par provider (jointure `option_id` → `egress_options`) :

| Provider | Lignes |
|---|---|
| **warp** | **182 913 (56,0 %)** |
| public | 140 321 |
| nordvpn | 3 578 |

**Confirmé** : la table d'observations représente 56 % de la base, dominée
par WARP, et **93,4 % de ses lignes ne portent aucun statut réel** (`status=0`).
Ce ne sont pas des observations utiles mais des écritures de remplissage.

**Aggravant** : la purge existe mais ne tourne jamais —
`free/egress_runtime.py:173` attend **24 h** avant la première exécution, et
23 707 lignes dépassent déjà la rétention de 7 jours.

**Le lien avec V-19 est direct** : plus les rotations échouent, plus
`_expire_recreated` écrit d'observations `status=0`, plus la base grossit.
Le bug de probe (V-19) est donc aussi la cause de la croissance de
`registry.db` — les deux constats sont un seul problème.

Rétention : **24 327 lignes** dépassent la fenêtre de 7 jours (7,4 % de la
table), la plus ancienne à 7,7 j. La purge existe mais ne tourne pas.

---

## V-21 — P0-01 du rapport 01 : `repair_owner: supervisor` désarme le pool ✅ CONFIRMÉ

**Affirmation** : le pool délègue la rotation au superviseur sans vérifier
que celui-ci est vivant.

**Vérification** :

```
config.yaml:289:  repair_owner: supervisor
free/pool/__init__.py:770:   self._repair_owner = str(
free/pool/__init__.py:771:       _rel_cfg.get("repair_owner", "manager") or "manager"
free/pool/__init__.py:6827:  if getattr(self, "_repair_owner", "manager") == "supervisor":
```

Le code (`free/pool/__init__.py:6827-6833`) :

```python
if getattr(self, "_repair_owner", "manager") == "supervisor":
    self._request_repair(station, "rotation-delegated")
    logger.debug(
        "[free-ip] station %s rotation delegated to supervisor",
        getattr(station, "_station", "?"),
    )
    return
```

**Le pool ne rotationne plus lui-même** — il écrit une demande dans
`repair_requests` et sort. Or `scripts/fresh_chain_supervisor.py` est un
**processus séparé**, et le pool sait déjà détecter sa mort
(`_fresh_chain_supervisor_alive()`, `free/pool/__init__.py:119-152`) mais ne
l'interroge **pas** ici.

**Impact** : superviseur arrêté (crash, reboot, runbook oublié) → toute la
flotte reste sur ses IP épuisées, et `strict_free: true` (`config.yaml:701`)
refuse la jambe payante → **refus client permanent**. C'est le scénario
« 0/6 » de l'audit 019, avec sa cause racine dans le code.

**Effet secondaire documenté** : ce `return` sans enfilement est aussi l'une
des causes du P0-03 (fuite de `_draining_stations`) — les deux P0 sont liés.

---

## V-22 — P0-02 du rapport 01 : `time.monotonic()` persisté ✅ CONFIRMÉ, preuve décisive

**Affirmation** : des valeurs `time.monotonic()` sont écrites dans l'état
persisté puis restaurées telles quelles au boot suivant, alors que
`monotonic()` repart de zéro après un reboot.

**Vérification** — mesure de l'horloge courante puis lecture de
`logs/free_pool.json` (1 948 129 octets) :

```
time.monotonic() actuel = 269 939,6

=== les 6 vraies stations ===
st 1: state=usable  last_ok_at=762626.406  parked_until=0.0  next_retry=0.0
st 2: state=usable  last_ok_at=762626.406  parked_until=0.0  next_retry=0.0
st 3: state=usable  last_ok_at=762626.406  parked_until=0.0  next_retry=0.0
st 4: state=usable  last_ok_at=762626.406  parked_until=0.0  next_retry=0.0
st 5: state=usable  last_ok_at=762626.406  parked_until=0.0  next_retry=0.0
st 6: state=usable  last_ok_at=762626.406  parked_until=0.0  next_retry=0.0
```

**Les valeurs persistées (`762 626 s`) valent 2,8× l'horloge courante
(`269 939 s`).** Elles proviennent d'un boot où la machine avait ~8,8 jours
d'uptime, contre ~3,1 jours maintenant. Le fichier contient donc des
**epochs d'un boot antérieur**, exactement comme l'auditeur l'a décrit.

**Deux observations supplémentaires, non relevées par l'auditeur :**

1. **Les 6 stations portent la valeur identique `762626.406`** — au
   millième de seconde près. Six stations saines ne peuvent pas avoir vu
   leur dernier succès au même instant : la valeur est **écrasée** par un
   chemin d'écriture commun, pas mesurée par station.
2. **Les 6 stations sont `state=usable`** dans l'état persisté, alors que
   l'API les rapporte 5/6 en `repairing` et `vpn=error` (§3 de
   `00-live-evidence.md`). L'état persisté contredit l'état live.

**Impact combiné avec le §6.2 du rapport 01** : `free_pool.json` contient
aussi 58 stations fantômes (`-86`…`-1`) avec `last_ok_at=0.0`, et pèse
**1,9 Mo**. Le fichier d'état est donc à la fois **périmé** (epochs d'un
autre boot), **écrasé** (valeurs identiques), **contradictoire** (usable vs
repairing) et **pollué** (58 stations inexistantes).

**C'est la violation la plus directe du principe I** (« la disponibilité se
mesure, elle ne se déclare pas ») : l'état persisté **déclare** six stations
saines, avec un horodatage qui n'appartient pas au boot courant.

---

## V-23 — P0-03 du rapport 01 : `_draining_stations` fuit ✅ CONFIRMÉ

**Affirmation** : une station est ajoutée à `_draining_stations` avant
l'appel à `_launch_rotation`, mais n'est retirée que si une rotation a
**réellement** été exécutée ; sinon elle reste exclue de la sélection
définitivement.

**Vérification — les sites d'ajout et de retrait** :

| Ligne | Opération | Contexte |
|---|---|---|
| 1852 | `add(sid)` | `_plan_duplicate_rotation` — **avant** `_launch_rotation` |
| 1957 | `add(int(decision.station))` | `_plan_proactive_rotation` — **avant** |
| 5631 | `add(int(station._station))` | dual-clutch « approaching quota » — **avant** |
| 7027 | `discard(int(sid))` | dans un `finally` de `_rotate_station` |
| 7102 | `discard(int(sid))` | dans une fonction de retrait de station |
| 7200 | `discard(int(sid))` | idem |
| 7502 | **`clear()`** | sur `update_config` — **seul nettoyage global** |

**Vérification des contextes de `discard`** — les trois sont bien dans des
`finally` de rotation réelle ou des fonctions de retrait :

```python
# ligne 7027
        finally:
            self._rotation_tasks.pop(sid, None)
            try:
                self._draining_stations.discard(int(sid))
                self._spare_promoted = False
            except Exception:
```

```python
# ligne 7102 (et 7200, identique)
            self._per.pop(sid, None)
            self._pending.discard(sid)
            self._rotation_tasks.pop(sid, None)
            try:
                self._station_health.remove(sid)
                self._quota.prune_station(sid)
                self._draining_stations.discard(int(sid))
```

**Aucun de ces chemins ne couvre le cas « `_launch_rotation` sort sans
enfiler »** — or c'est précisément ce que fait la délégation superviseur
(V-21, ligne 6827-6833, `return` sans enfilement), ainsi que les branches
`_blocked` (`FROZEN`, `UNKNOWN`, `PENDING`/`RUNNING`).

**Vérification de l'exclusion** : la sélection filtre bien sur cet ensemble
(`free/pool/__init__.py:1829` et `:1990`) :

```python
st for st in stations if self._station_id(st) not in getattr(self, "_draining_stations", set())
...
if sid in getattr(self, "_draining_stations", set()):
```

**Impact confirmé** : avec `repair_owner: supervisor` (la config livrée),
**chaque 429 retire une station de la sélection jusqu'au prochain
`update_config`**. Sur une flotte de 6, quelques 429 suffisent à vider la
sélection alors que les tunnels sont sains. Et `get_status()` n'expose pas
`_draining_stations` : **le dashboard ne peut pas le voir**.

**C'est la troisième explication convergente du blocage observé**, avec
V-19 (probe WARP cassé) et V-21 (délégation sans vérification) : trois
mécanismes indépendants retirent des stations de la sélection, et aucun
n'est visible dans l'interface.

---

## V-24 — P1 du rapport 01 : la purge ne couvre pas les grosses tables ✅ CONFIRMÉ

**Affirmation** : `rotation/infrastructure/db/retention.py` purge 5 tables
mais laisse les plus volumineuses croître sans borne.

**Vérification** (`retention.py:25-33`) — la boucle de purge itère sur
**exactement 5 tables** :

```python
for table, key, days_key in (
    ("observations", "tested_at", "observations_days"),
    ("fallback_events", "started_at", "fallback_days"),
    ("incidents", "started_at", "incidents_days"),
    ("repair_jobs", "started_at", "repair_jobs_days"),
    ("config_snapshots", "created_at", "config_days"),
):
    cutoff = ts - max(0.0, float(cfg[days_key])) * 86400.0
    cur = conn.execute(f"DELETE FROM {table} WHERE {key} < ?", (cutoff,))
```

**Comparaison avec les tables réellement volumineuses** (mesures §11.2 de
`00-live-evidence.md`) :

| Table | Lignes | Purgée ? |
|---|---|---|
| `egress_observations` | **326 812** | ❌ **non** |
| `egress_threat_intel` | 174 163 | ❌ non |
| `scan_attempts` | 61 027 | ❌ non |
| `qualification_attempts` | 40 824 | ❌ non |
| `incidents` | 35 249 | ✅ oui |
| `egress_options` | 31 873 | ❌ non |
| `server_transports` | 20 945 | ❌ non |
| `catalog` / `claims` | 9 788 | ❌ non |
| `quota_buckets` | 8 519 | ❌ **non** |
| `rotation_jobs` | 3 808 | ❌ non |
| `observations` | 8 657 | ✅ oui |
| `repair_jobs` | 0 | ✅ oui |
| `fallback_events` | 0 | ✅ oui |
| `config_snapshots` | 0 | ✅ oui |

**La purge ne couvre que 3 tables non vides, dont une seule est
significative (`incidents`).** Les 7 tables les plus grosses — 95 % des
lignes de la base — ne sont **jamais** purgées. C'est la cause directe des
160 Mo de `registry.db`.

**Détail aggravant** : `retention.py` fournit aussi `checkpoint()`,
`integrity_ok()` et `backup()` — la machinerie est complète et de bonne
qualité. Elle n'est simplement **jamais planifiée** (§P2-7 du rapport 08 :
`run_once` n'est appelé que par `scripts/registry_maintenance.py`, manuel).

---

## V-25 — P1 du rapport 01 : `get_status()` sur le chemin du dashboard ✅ CONFIRMÉ

**Affirmation** : `get_status()` a un coût O(stations² × modèles) et lit le
vault d'identités par station ; deux endpoints l'appellent.

**Vérification des appelants** — il y en a **six**, pas deux :

```
dashboard/routers/nordvpn_routes.py:16:            status = manager.get_status()
dashboard/routers/pool_routes.py:86:        return pool.get_status()
dashboard/routers/station_routes.py:44:            status = manager.get_status()
dashboard/routers/station_routes.py:140:            status = manager.get_status()
dashboard/routers/vpn_status_routes.py:96:   data = shared_state.free_ip_pool.get_status()
dashboard/routers/vpn_status_routes.py:100:  manager.get_status() for manager in managers
```

`vpn_status_routes.py:100` l'appelle même **en compréhension sur tous les
managers** — donc N appels par requête dashboard.

**Vérification du corps** (`free/pool/__init__.py:7763-7780`) : la fonction
commence par lire `ip_registry.allocator.probe_models` puis itère sur les
stations, en construisant 20+ champs par station.

**Impact** : c'est l'endpoint que le dashboard interroge en boucle (le front
poll toutes les 3 s sur certaines pages, §P3-3 du rapport 08). Le coût est
donc **récurrent**, pas ponctuel.

---

## V-26 — Validation de la méthode « code mort » du rapport 01 ✅ FIABLE

Le rapport 01 liste **37 symboles morts**. Validation par échantillonnage
indépendant de 5 d'entre eux :

| Symbole | Occurrences hors `Lib/`/`_research/`/audits | Verdict |
|---|---|---|
| `_TRANSIENT_HINTS` (`free/classify_429.py:36`) | **1** (sa définition) | ✅ mort |
| `Classification.is_format` (`free/classify_429.py:70`) | **1** (sa définition) | ✅ mort |
| `FreeIPPool.pick_two` (`free/pool/__init__.py:3850`) | 2 (dont un commentaire « Back-compat alias ») | ✅ mort |
| `_station_health_snapshot` (`free/pool/__init__.py:1154`) | **1** (sa définition) | ✅ mort |
| `QualityRegistry.slow_station_count` (`free/quality.py:577`) | **1** (sa définition) | ✅ mort |

**5 sur 5 confirmés.** Chaque symbole n'apparaît qu'à sa propre définition
(le cas `pick_two` est explicite : un commentaire `# Back-compat alias for
plan's pick_two` au-dessus d'une méthode que personne n'appelle).

**La méthode de l'auditeur est fiable** : il cite la commande grep et
distingue « mort en production » (appelé seulement par des tests) de « mort
partout ». L'inventaire de 37 entrées est utilisable tel quel pour le plan
de nettoyage.

**Point notable** : `free/wire_admission.py` figure dans **deux** rapports
indépendants (01 et 04) plus ma vérification directe — trois sources
concordantes sur le même constat.

---

## V-27 — Config morte du rapport 01 ✅ CONFIRMÉ

**Affirmation** : `ip_registry.selection.*` est déclaré mais sans
consommateur, et `logs/ip_registry.db` est un fichier vide configuré comme
source de vérité.

**Vérification — les clés** (`config.yaml:1273-1284`) :

```yaml
  selection:
    unique_ip: true
    allow_countries: []
    deny_countries: []
    warm_pool_size: 9
```

**Vérification — les consommateurs** :

```
grep -rn "warm_pool_size\|allow_countries\|deny_countries" \
     --include="*.py" opencode.py dashboard/ free/
→ (aucun résultat)
```

**Confirmé** : quatre clés déclarées, **zéro consommateur** dans le code de
production. C'est une violation directe du principe XII (« Zéro option morte
… aucune option inerte n'est acceptée ») et de la gate XVII
(`config_audit --strict` à 0 dead) — ces clés sont vraisemblablement dans la
liste de tolérance du baseline, donc **le gate ne les voit pas**.

**Vérification — la base fantôme** :

```
$ ls -la logs/ip_registry.db
-rw-r--r-- 1 msi 197121 0 Nov  9 21:23 logs/ip_registry.db
```

**0 octet**, daté du 2026-11-09. La config `ip_registry.db_path`
(`config.yaml:1254`) pointe vers un fichier vide, et l'auditeur confirme
qu'aucun process de production n'instancie `SqliteIpRegistry`.

**Aggravant relevé par le rapport 01** : `free/ip_state.py:48` et
`free/ip_registry/sqlite.py:58` définissent **tous deux** une table
`ip_state` avec des **schémas incompatibles**, dans le **même**
`registry.db`. Deux définitions concurrentes de la même table : selon
l'ordre d'initialisation, l'une écrase ou casse l'autre.

> **⚠️ CORRECTION (rapport 09, 2026-11-20)** — Ma conclusion « zéro
> consommateur » était **hâtive**. Le rapport 09 a conduit une vérification
> **AST** des deux formes d'appel (`yaml_get("section", "clé")` et
> `yaml_get("section.clé")`) et établit que les clés
> `ip_registry.selection.*` **ont** des consommateurs :
> `rotation/infrastructure/config/schema.py` et `scripts/fresh_chain*.py`.
>
> **Le vrai défaut n'est donc pas « clé sans consommateur » mais
> « consommateur non atteignable en production »** — ces modules ne sont pas
> branchés. C'est le même défaut de famille C (ressource hors du chemin
> vivant), pas un défaut de configuration.
>
> La mesure reste exacte : ces clés **ne sont pas lues** par `opencode.py`,
> `dashboard/` ni `free/`. Mais l'énoncé correct est « le consommateur existe
> et n'est pas branché », ce qui change la recommandation : il faut
> **brancher ou retirer le module**, pas supprimer la clé.

---

## V-28 — Deux schémas `ip_state` incompatibles dans le même `registry.db` ✅ CONFIRMÉ (violation III majeure)

**Vérification** — deux `CREATE TABLE IF NOT EXISTS ip_state` :

```
free/ip_state.py:49:          CREATE TABLE IF NOT EXISTS ip_state (
free/ip_registry/sqlite.py:58: CREATE TABLE IF NOT EXISTS ip_state (
```

**Comparaison des colonnes** :

| `free/ip_state.py:49` (spec 023) | `free/ip_registry/sqlite.py:58` (legacy) |
|---|---|
| `provider TEXT NOT NULL` | `egress_ip TEXT PRIMARY KEY` |
| `ip TEXT NOT NULL` | `hostname TEXT NOT NULL` |
| `bucket_key TEXT NOT NULL DEFAULT ''` | `country TEXT NOT NULL` |
| `state TEXT NOT NULL` | `state TEXT NOT NULL` |
| `wire_fp TEXT NOT NULL DEFAULT ''` | `score REAL NOT NULL DEFAULT 0` |
| `proven_at REAL` | `last_status INTEGER` |
| `serving_at REAL` | `last_tested_at REAL` |
| `reset_at REAL` | `last_ok_at REAL` |
| `burned_at REAL` | `reset_at REAL` |
| `reason TEXT NOT NULL DEFAULT ''` | `error_until REAL` |
| `evidence_ref TEXT NOT NULL DEFAULT ''` | `latency_ms REAL`, `tests INTEGER` |

**Aucune colonne commune à part `state` et `reset_at`.** Les deux schémas
sont structurellement incompatibles : l'un est indexé par `(provider, ip)`,
l'autre par `egress_ip` seule ; l'un porte `wire_fp`/`burned_at` (le modèle
de brûlage du principe XVIII), l'autre porte `score`/`latency_ms`.

**Le `IF NOT EXISTS` rend le conflit silencieux** : la **première**
définition exécutée crée la table, la seconde est un **no-op**. Aucune erreur,
aucun avertissement. Le comportement du système dépend donc de **l'ordre
d'initialisation des modules** — et l'un des deux chemins échouera à
l'exécution avec `no such column`.

**C'est la violation la plus grave du principe III** (« une seule source de
vérité par cycle de vie ») trouvée dans cet audit : deux définitions
concurrentes de la même table, dans la même base, par deux modules qui
s'ignorent.

**Corrélation avec les mesures** : la table `ip_state` en production contient
**283 lignes, toutes `provider='nordvpn'`** (§4.2 de `00-live-evidence.md`) —
colonnes du schéma 023 (`provider` existe). C'est donc le schéma **023** qui
a gagné, et le chemin legacy (`free/ip_registry/sqlite.py`) qui est cassé.

---

## V-29 — P0 du rapport 03 : `docker-compose.yml` détruirait la flotte ✅ CONFIRMÉ

**Affirmation** : le compose déclare l'image **upstream** pour 9 stations sur
10, alors que les 6 conteneurs vivants tournent le **fork** `gluetun-016:local`.
Tout `compose up` ramènerait les stations sur l'upstream, dépourvu de l'API de
contrôle v2.

**Vérification — les images déclarées** (`docker-compose.yml`) :

| Ligne | Service | Image déclarée |
|---|---|---|
| 26 | *(ancre YAML)* | `qmcgaw/gluetun@sha256:4af0c804…` |
| 185 | `vpn-gluetun` | `qmcgaw/gluetun@sha256:4af0c804…` |
| 296 | `vpn-gluetun-2` | `qmcgaw/gluetun@sha256:4af0c804…` |
| 384 | `vpn-gluetun-3` | *(hérite de l'ancre)* |
| **416** | **`vpn-gluetun-4`** | **`gluetun-016:local`** ✅ |
| 437 | `vpn-gluetun-5` | *(hérite de l'ancre)* |
| 460 | `vpn-gluetun-6` | *(hérite de l'ancre)* |
| 591 | `vpn-wg-test` | `qmcgaw/gluetun@sha256:4af0c804…` |

**Vérification — les images réellement utilisées** :

```
$ docker ps --format "{{.Names}}\t{{.Image}}" | grep -i vpn
opencode-vpn-4	gluetun-016:local
opencode-vpn-3	gluetun-016:local
opencode-vpn-6	gluetun-016:local
opencode-vpn-5	gluetun-016:local
opencode-vpn-2	gluetun-016:local
opencode-vpn	gluetun-016:local
```

**Les 6 stations tournent `gluetun-016:local`, mais le compose n'en déclare
qu'une seule (`vpn-gluetun-4`).**

**Impact** : `vpn/manager.py` appelle `docker compose` à **8 sites** avec un
seul `-f` et **zéro `--profile`** (vérifié : `grep -c -- "--profile"
vpn/manager.py` → 0). Les overlays qui portent le fork ne sont **pas
versionnés** (ils vivent dans `logs/`). Donc :

- un `compose up` recrée les stations sur `qmcgaw/gluetun` (upstream) ;
- l'API de contrôle v2 du fork (`/v1/vpn/*`) **n'existe pas** dans l'upstream
  → les rotations tombent en **404** ;
- `free/ip_registry/adapters/compose_binder.py:121` affirme explicitement que
  le fork est requis.

**C'est le P0 le plus dangereux de l'audit en termes de destruction** : une
seule commande d'exploitation anodine (`docker compose up -d`) casse les 6
stations simultanément, et le système n'a aucun garde-fou contre.

**Aggravant** : l'auto-update (`vpn/manager.py:9423,9572,10871`,
`ops/updater.py:26,47`) compare et *pull* le digest **upstream** pendant que
la flotte tourne le fork, et `_rollback_update` retague sur `qmcgaw/gluetun`.
L'auto-update peut donc **déclencher lui-même** la destruction qu'il croit
réparer.

---

## V-30 — P1 du rapport 03 : `get_public_ip()` valide sur une IP auto-déclarée ✅ CONFIRMÉ

**Affirmation** : l'autorité de validation d'une rotation retourne l'IP
**auto-déclarée par gluetun** 2 fois sur 3, sans preuve d'egress — violation
du principe VII (« aucune preuve, aucun succès »).

**Vérification** (`vpn/manager.py:3694-3712`) — le code fait tourner l'ordre
des sources, et son **propre commentaire** l'admet :

```python
try:
    _m = int(getattr(self, "_ip_method_idx", 0) or 0) % 3
except Exception:
    _m = 0
try:
    self._ip_method_idx = (_m + 1) % 3
except Exception:
    pass
# Ordres tournants : control d'abord 2 fois sur 3 (rapide, sans
# egress — il couvre le cas SOCKS loopback KO), SOCKS d'abord
# 1 fois sur 3 (preuve d'egress complète, contrat sticky testé).
if _m == 1:
    order = ("socks", "control", "http")
elif _m == 2:
    order = ("control", "http", "socks")
else:
    order = ("control", "socks", "http")
```

**Confirmé, et le commentaire le documente lui-même** : `control` est en
première position dans **2 des 3 ordres** (`_m == 0` et `_m == 2`), avec la
justification explicite « **rapide, sans egress** ». Seul `_m == 1` place
`socks` en tête — la seule source qui apporte une **preuve d'egress
complète**.

**Conséquence** : `_finalize_ip` → `_commit_ip` enregistre l'IP au registre
partagé, avance l'identité (`_advance_identity`) et crédite
`_breaker_charge_success()` — **sur la foi d'une valeur que le conteneur
déclare sur lui-même**, sans vérifier que le trafic sort réellement par là.

**C'est une violation frontale du principe VII** appliquée à l'objet le plus
sensible du système : l'IP d'egress. La constitution dit « un bind sans
egress réel n'est pas un succès » ; ici, deux rotations sur trois sont
validées exactement de cette façon.

**Note** : le choix est délibéré et commenté (« il couvre le cas SOCKS
loopback KO ») — ce n'est pas un oubli mais un compromis de performance
assumé. Il reste que le compromis est enregistré comme un **succès prouvé**
dans le registre, ce qui contamine les mesures de disponibilité (principe I).

---

## V-31 — P1 du rapport 03 : aucun profil compose, overlays non versionnés ✅ CONFIRMÉ

**Vérification** :

```
$ grep -c -- "--profile" vpn/manager.py
0
```

**Zéro occurrence.** Les invocations de compose dans `vpn/manager.py` (8
sites, dont `:1256`, `:2100`, `:2257`) passent un seul `-f` et aucun
`--profile`. Les overlays qui portent l'image du fork ne sont donc **jamais**
chargés par le code — ils vivent dans `logs/` (non versionnés).

**Combiné avec V-29**, cela signifie que le mécanisme qui maintient le fork
en place est **entièrement externe au dépôt** : ni le compose versionné, ni
le code ne l'appliquent. Le fork survit uniquement parce que personne n'a
lancé `compose up` depuis sa mise en place.

---

## V-32 — P1 du rapport 03 sur la constitution XVI : **nuancé** ⚠️

**Affirmation du rapport 03** : « `vpn/manager.py` n'utilise jamais le
wrapper commun, et les scripts service-spawned échappent au gate » — 16
sites hors `core.process`.

**Vérification — le fond (les fenêtres s'ouvrent-elles ?)** :

```
$ grep -c "creationflags" vpn/manager.py
8
$ grep -n "CREATE_NO_WINDOW" vpn/manager.py
218:  creationflags=CREATE_NO_WINDOW,
269:  creationflags=CREATE_NO_WINDOW,
282:  creationflags=CREATE_NO_WINDOW,
354:  creationflags=CREATE_NO_WINDOW if sys.platform == "win32" else 0,
381:  creationflags=CREATE_NO_WINDOW,
432:  creationflags=CREATE_NO_WINDOW,
```

**`vpn/manager.py` pose bien `CREATE_NO_WINDOW`** sur ses appels
`subprocess`. Le wrapper commun (`core/process.py:53` `run_hidden`, `:84`
`popen_hidden`) existe mais n'est pas utilisé.

**Vérification — la couverture du gate** (`tests/test_no_visible_spawn.py:16-23`) :

```python
RUNTIME_ROOTS = [
    ROOT / "opencode.py",
    ROOT / "scripts" / "fresh_chain.py",
    ROOT / "scripts" / "fresh_chain_supervisor.py",
    ROOT / "free",
    ROOT / "vpn",          # ← manager.py est couvert
    ROOT / "dashboard",
    ROOT / "observability",
]
```

**`vpn/` est dans `RUNTIME_ROOTS`** : le gate AST inspecte bien
`vpn/manager.py`, et l'auditeur du rapport 08 a mesuré
`platform_audit.py` → **0 violation sur 279 fichiers**.

**Verdict nuancé** :

| Aspect | Verdict |
|---|---|
| **Fond** — des fenêtres s'ouvrent-elles ? | ❌ **non, principe XVI respecté** |
| **Forme** — le wrapper commun est-il utilisé ? | ⚠️ non, 18 appels `subprocess.` directs avec flags manuels |
| **Le gate couvre-t-il le fichier ?** | ✅ oui, `vpn/` est dans `RUNTIME_ROOTS` |

**Ce qui reste un vrai problème** : la **duplication de la politique**. Le
principe XVI exige que le wrapper soit « la seule voie autorisée » car il
« centralise `creationflags`, `start_new_session`, `stdin`, `cwd` et le
scrub d'environnement ». Ici la politique est **réimplémentée 8 fois à la
main** — donc toute évolution (nouveau flag, scrub d'env, `cwd`) devra être
appliquée 8 fois, et un futur site oubliera le flag.

**Recommandation ajustée** : migrer les 18 sites vers `run_hidden` /
`popen_hidden` est un **refactor de dette (P2)**, pas un correctif de bug.
Ce n'est pas un P1 de sécurité.

**Point qui reste ouvert** : l'affirmation que `scripts/fresh_chain*.py`
(15 sites) **n'est pas scanné** par le gate — vérifié partiellement :
`fresh_chain.py` et `fresh_chain_supervisor.py` sont **dans** `RUNTIME_ROOTS`,
donc couverts. Les autres scripts de `scripts/` (hors ces deux) ne le sont
pas, mais ils sont des CLI opérateur (exemptés par la constitution).

---

## V-33 — Preuve décisive du V-29 : le fork ne survit que par des fichiers gitignorés ⚠️ CRITIQUE

**Vérification — les overlays existent** :

```
$ ls -la logs/compose-control-plane-*.yml
-rw-r--r-- 1 msi 197121  825 Nov 14 08:45 logs/compose-control-plane-1.yml
-rw-r--r-- 1 msi 197121  827 Nov 14 08:45 logs/compose-control-plane-2.yml
-rw-r--r-- 1 msi 197121  827 Nov 14 08:45 logs/compose-control-plane-3.yml
-rw-r--r-- 1 msi 197121  827 Nov 14 08:45 logs/compose-control-plane-4.yml
-rw-r--r-- 1 msi 197121  827 Nov 14 08:45 logs/compose-control-plane-5.yml
-rw-r--r-- 1 msi 197121 1131 Nov 14 08:45 logs/compose-control-plane-6.yml
```

**Contenu** (`logs/compose-control-plane-1.yml`) :

```yaml
services:
  vpn-gluetun:
    image: gluetun-016:local          # ← le fork
    pull_policy: never
    ports:
      - "127.0.0.1:8001:8000"
    environment:
      CONTROL_PLANE_ENABLED: "true"
      CONTROL_STATE_DIR: /gluetun/control-state
      CONTROL_STATE_MODE: env
      HEALTH_TARGET_ADDRESSES: 1.1.1.1:443,1.0.0.1:443
      CONTROL_EGRESS_SOURCES: http:cloudflare,http:ipify,http:ifconfig,http:icanhazip,publicip:internal
      DNS_LADDER: doh,plain,dot
      TRANSPORT_ATTEMPT_TIMEOUT_S: "100"
      …
```

**Ces 6 fichiers portent exactement ce qui fait fonctionner la flotte** :
l'image du fork, `pull_policy: never`, le port du control plane (`8001`), et
les 12+ variables qui activent l'API `/v1/vpn/*`.

**Vérification — sont-ils versionnés ?**

```
$ git check-ignore -v logs/compose-control-plane-1.yml
.gitignore:56:logs/	logs/compose-control-plane-1.yml
```

**Non. Ils sont ignorés par git** (`.gitignore:56` : `logs/`).

**Conclusion** : l'ensemble du dispositif qui maintient la flotte sur le fork
— image, port de contrôle, variables `CONTROL_*` — repose sur **6 fichiers
non versionnés, dans un dossier déclaré jetable**, que :

- aucun `git clone` ne reconstitue ;
- aucun `git clean -fd` ne protège ;
- aucune revue ne couvre ;
- le `docker-compose.yml` versionné **contredit** (V-29).

**C'est le risque de perte le plus élevé du projet.** Le `logs/` fait 15 Go et
sa purge est recommandée par plusieurs rapports (`purge_logs.py`, P2-6 du
rapport 08) : une purge de `logs/` **détruirait la configuration du control
plane** sans que rien ne le signale, puisque le code ne lit ces fichiers que
par un chemin (`extra_compose_files`) dont le rapport 03 a montré qu'il n'a
**aucun appelant de production**.

**Recommandation prioritaire** : déplacer ces overlays hors de `logs/` (par
exemple `deploy/compose/control-plane-N.yml`), les versionner, et faire du
`docker-compose.yml` la source unique qui les inclut — ou intégrer leur
contenu dans les services du compose principal.

---

## V-34 — P1 du rapport 03 : aucun compteur de sessions NordVPN ✅ CONFIRMÉ

**Affirmation** : la constitution (principe II) définit la **session** comme
« une connexion NordVPN, ressource rare et bornée par le compte », distincte
de la station et de l'IP. Aucun code ne compte les sessions ouvertes.

**Vérification** (`vpn/auth.py:30-34`) — tout ce qui existe est un
**throttle temporel** :

```python
_AUTH_LAST_CONNECT_AT = 0.0
_AUTH_IN_FLIGHT = 0
_AUTH_MIN_INTERVAL_GLOBAL_S = 12.0
_AUTH_MIN_INTERVAL_SAME_STATION_S = 30.0
_AUTH_LAST_BY_STATION: dict[str, float] = {}
```

`_AUTH_IN_FLIGHT` est un compteur de **connexions en cours** (transitoire),
pas de **sessions ouvertes** (durable). Le throttle limite le *débit* de
connexions (12 s entre deux globalement, 30 s par station), il ne borne pas
le *nombre* de sessions simultanées.

**Vérification** (`free/egress_adapters/nordvpn.py:34-40`) :

```python
    *,
    probe=None,
    max_active_stations: int = 6,
) -> None:
    self._stations = stations
    self._probe = probe
    self.max_active_stations = max(1, int(max_active_stations))
```

Le rapport 03 précise que ce module « ne ouvre jamais de session » (docstring
`l. 1-8`) et que `max_active_stations` ne **gate que des observations**.

**Conséquence** : 6 stations + 1 canari WireGuard = **7 sessions
potentielles sur un compte**, sans aucun compteur qui le sache. Le principe
II n'est pas implémenté pour l'objet qu'il nomme explicitement comme « rare
et borné ».

**Note de cohérence avec le passé du projet** : l'audit 019 (D-014-11)
documente un épisode `AUTH_FAILED` en cascade causé par « des binds simultanés
au-delà de la limite de sessions NordVPN », résolu par un throttle
(`_auth_pause_remaining`, `auth_fail_30min`). **Le symptôme a été traité, pas
la cause** : la limite de sessions reste inconnue du code, donc un futur
changement (7ᵉ station, canari supplémentaire, WARP) peut la redépasser sans
avertissement.

---

## V-35 — Deux preuves supplémentaires du V-29 (le code sait, mais rien ne protège)

**Preuve A — le code affirme explicitement que l'image legacy est
inutilisable.** `free/ip_registry/adapters/compose_binder.py:120-123` :

```python
if not tag.startswith("gluetun-016:"):
    # A legacy image cannot serve the control plane; migrate it on
    # the next bind, one station at a time.
    return True
```

Le commentaire est **sans ambiguïté** : une image qui n'est pas le fork « ne
peut pas servir le control plane ». Le projet connaît donc la contrainte.

**Preuve B — le mécanisme qui pourrait rattraper l'overlay n'a aucun appelant
de production.** Recherche de `extra_compose_files` :

```
free/ip_registry/adapters/compose_binder.py:36   (déclaration du paramètre)
free/ip_registry/adapters/compose_binder.py:50   (stockage)
free/ip_registry/adapters/compose_binder.py:345  (usage interne)
tests/test_ip_registry_compose_binder.py:442     (test)
tests/test_ip_registry_compose_binder.py:452     (test)
```

**Aucun site dans `vpn/manager.py`, `opencode.py`, `scripts/` ou `dashboard/`.**
Le paramètre existe, est implémenté, est testé — et n'est **jamais alimenté
en production**.

**Synthèse du risque** : trois pièces cohérentes entre elles —

| Pièce | État |
|---|---|
| `docker-compose.yml` (versionné) | déclare l'image **upstream** pour 5 des 6 stations |
| `logs/compose-control-plane-*.yml` (gitignoré) | porte l'image du **fork** — ce qui fait marcher la flotte |
| `extra_compose_files` (le rattrapage côté code) | implémenté, testé, **jamais appelé** |

Le système fonctionne aujourd'hui **par accident maintenu** : les conteneurs
tournent, donc personne ne relance `compose up`. Le jour où une rotation
recrée une station (`--force-recreate`), ou qu'un opérateur lance
`docker compose up -d`, la station revient sur l'upstream — et le code
**détectera** l'image legacy (`compose_binder.py:121`) sans pouvoir la
corriger, puisque son propre chemin de rattrapage n'est pas branché.

---

## V-36 — P0 du rapport 02 : 15 179 violations de clés étrangères ✅ CONFIRMÉ (chiffre exact)

**Affirmation** : 5 des 6 connexions qui écrivent dans `registry.db` omettent
`PRAGMA foreign_keys=ON`, produisant 15 179 violations.

**Vérification** :

```python
>>> c.execute("PRAGMA foreign_key_check").fetchall()
15179 violations
  table=server_transports parent=servers : 15179
```

**Le chiffre est exact au chiffre près : 15 179 violations**, toutes de la
même nature — `server_transports.hostname` référence des hostnames qui n'ont
pas de ligne dans `servers`.

**Ce que cela signifie** : la base **déclare** des clés étrangères (le schéma
les porte, `PRAGMA foreign_key_check` les connaît) mais **ne les applique
pas** à l'écriture. Le principe XIII exige « des clés primaires, clés
étrangères, contraintes, index » — les clés étrangères sont **déclaratives,
pas effectives**.

**Aggravant** : `foreign_keys` est un pragma **par connexion**, non
persistant. L'auditeur a mesuré que **5 des 6 connexions écrivantes** ne le
posent pas. Le contrôle dépend donc du chemin de code emprunté, ce qui est
exactement le genre d'incohérence que le principe III interdit.

---

## V-37 — P0 du rapport 02 : la boucle d'epochs gonfle les compteurs ×20 ✅ CONFIRMÉ et précisé

**Affirmation** : la passe de qualification re-claim le même hostname 40 fois
et écrit un epoch toutes les 60 s ; `SUM(tested)` vaut ×20 les tentatives
réelles.

**Vérification** :

| Mesure | Valeur |
|---|---|
| `qualification_epochs` total | **21 230** |
| `SUM(tested)` | **847 383** |
| `qualification_attempts` (réelles) | **41 166** |
| **ratio** | **×20,6** |
| epochs créés aujourd'hui (2026-11-20) | **1 215** |
| status | `partial` ×21 228, `running` ×2 |

**Précision apportée par l'auditeur principal** — la cause est plus nette
que « 21 182 epochs vides ». En groupant par `total` (la taille de l'ensemble
de candidats) :

| `total` | nombre d'epochs | `SUM(tested)` |
|---|---|---|
| **7 118** | **6 769** | 270 760 |
| **7 124** | **4 493** | 179 720 |
| **7 117** | **4 084** | 163 360 |
| 7 110 | 3 449 | 137 960 |
| 7 092 | 1 091 | 43 636 |
| 7 106 | 655 | 26 200 |

**6 769 epochs portent le même `total=7118`** — c'est le **même ensemble de
candidats** traité 6 769 fois. Les epochs récents ne sont pas vides
(`tested=40` chacun) : ils **re-comptent le même travail**, à ~60 s
d'intervalle.

**Conséquence** : `SUM(tested)` = 847 383 n'est pas une mesure de travail
accompli, c'est **une somme de re-comptages**. Toute métrique de progression
de la qualification est **fausse d'un facteur 20**. C'est une violation du
principe I appliquée aux compteurs internes du système.

**Lien avec les autres constats** : cette boucle écrit **1 215 lignes/jour**
dans `qualification_epochs`, contribuant au gonflement de `registry.db`
(V-20), et elle tourne alors que la flotte est à 1/6 — le système dépense son
activité à re-mesurer un ensemble qu'il ne parvient pas à exploiter.

---

## V-38 — P0 du rapport 05 : les credentials de proxy sont jetés ✅ CONFIRMÉ

**Affirmation** : un proxy authentifié (`http://user:pass@host:port`) perd ses
credentials à la construction de l'URL, donc tout proxy authentifié promu
`usable` renvoie 407 systématiquement.

**Vérification** (`free/egress_rotation.py:96-104`) :

```python
    for option in options:
        if not option.host or not option.port:
            continue
        scheme = option.kind if option.kind in ("http", "https", "socks5", "socks5h") else "https"
        host = f"[{option.host}]" if ":" in option.host else option.host
        rows.append(
            {
                "url": f"{scheme}://{host}:{int(option.port)}",     # ← pas de user:pass
                "egress_ip": option.egress_ip,
                "block": option.block,
                "provider": "public",
                "label": option.key,
```

**L'URL est reconstruite en `scheme://host:port`** — aucun champ `user` /
`password` n'existe dans le dictionnaire, et `free/egress_catalog.py` n'a
**pas** de champ d'authentification (vérifié : les seules occurrences de
`auth` concernent des **statuts HTTP** 401/407, pas des credentials).

**Conséquence** : un proxy `http://user:pa55@1.2.3.4:8080` devient
`http://1.2.3.4:8080`. Il peut être promu `usable` (si le gate répond 200 sans
exiger d'auth) puis échouer en **407** à chaque usage réel. L'information
d'authentification est perdue **dès la découverte** : elle n'entre jamais
dans le catalogue.

**C'est une perte de capacité silencieuse** : la source de proxys publics
contient des proxys authentifiés, ils sont comptés dans les « connus », et
aucun ne peut fonctionner.

---

## V-39 — P0 du rapport 05 : le gate public ne valide pas le contenu ✅ CONFIRMÉ (asymétrie avec le chemin client)

**Affirmation** : `free/egress_adapters/public.py:142` promeut `usable` sur
un simple `200`, sans vérifier que le flux a produit du contenu — alors que
le chemin client, lui, valide.

**Vérification du gate** (`free/egress_adapters/public.py:142-150`) :

```python
                free_status = int(getattr(response, "status_code", 0) or 0)
                headers = dict(getattr(response, "headers", None) or {})
                body = str(getattr(response, "text", "") or "")[:2_000]
                if free_status == 429:
                    from free.egress_validation import parse_retry_after
                    reset_at = parse_retry_after(headers, body, now=time.time())
```

Le code lit `free_status`, `headers` et `body` — mais la promotion se fait sur
`free_status == 200`. **Le corps n'est lu que dans la branche 429.**

**Vérification du chemin client** (`opencode.py:11290-11296`) — il fait
exactement ce que le gate ne fait pas :

```python
            if not _forced_data:
                # SSE vide (tunnel tronqué ?) → proxy suivant, comme une
                # erreur transport (pas de bad-mark : pas un connect-error).
                last_exc = UpstreamError("free: empty SSE collection", status_code=502)
                continue
```

Le commentaire est explicite : « **SSE vide (tunnel tronqué ?) → proxy
suivant** ».

**C'est une asymétrie de preuve** : le même système applique deux standards
opposés selon le chemin.

| Chemin | Vérifie le contenu ? |
|---|---|
| Client (`opencode.py:11292`) | ✅ oui — rejette un SSE vide en 502 |
| Gate catalogue (`public.py:142`) | ❌ non — promeut sur le seul code HTTP |

**Violation du principe VII** (« un `200` ne compte que si le flux a produit
du contenu et n'a pas émis de trame `event: error` ») **et du principe XVIII**
(une promotion non prouvée consomme une IP sur une preuve inexistante).

**Effet mesurable** : les options promues par ce gate alimentent le ledger via
`free/pool/__init__.py:4501` — donc des IP sont marquées « prouvées » sur la
foi d'un `200` dont personne n'a vérifié le contenu.

---

## V-40 — P0 du rapport 05 : 86,4 % des proxys publics sont morts sans avoir été testés ✅ CONFIRMÉ et aggravé

**Affirmation** : un candidat dont la sonde n'a pas abouti est marqué `dead`
pour 7 jours, créant une famine auto-entretenue.

**Vérification du code** (`free/egress_adapters/public.py:552-567`) :

```python
                    CandidateProbe(url=str(candidate["url"]), error=(outcome.error if outcome else "no probe")),
                    policy,
                    run_id,
                    phase=ObservationPhase.LIVENESS,
                )
                result.dead += 1
                continue
```

Le message d'erreur vaut `outcome.error if outcome else "no probe"` — et
**`result.dead += 1` s'applique dans les deux cas**. Un candidat jamais sondé
(`outcome is None`) est compté `dead` au même titre qu'un candidat réellement
mort.

**Mesure en base** (plus forte que le chiffre du rapport) :

| Mesure | Valeur |
|---|---|
| options `public` totales | 30 647 |
| options `public` `dead` **avec `egress_ip` vide** | **26 474 (86,4 %)** |
| observations phase `liveness` | **129 805** |

**86,4 % des proxys publics sont classés morts sans avoir d'IP d'egress
observée** — c'est-à-dire sans preuve qu'ils ont été testés avec succès, ni
qu'ils ont échoué pour une raison de transport.

**C'est une famine auto-entretenue** : le gate ne peut promouvoir que depuis
un ensemble qu'il vide lui-même à chaque cycle. Cela explique aussi pourquoi
le provider `public` affiche 1 413 options `usable` (§4.2 de
`00-live-evidence.md`) mais **172 `usable` seulement** dans
`egress_provider_state` — et pourquoi le taux de succès cumulé du provider
public est de **3,4 %** (§8).

**Violation du principe I** : « la disponibilité se mesure, elle ne se
déclare pas ». Ici l'**indisponibilité** est déclarée sans mesure.

---

## V-41 — P1 du rapport 05 : un fichier gitignoré est requis au démarrage ✅ CONFIRMÉ

**Affirmation** : `StationProbe.__init__` exige
`free/official_payload.json.bak`, qui est gitignoré ; un checkout neuf lève
`FileNotFoundError`.

**Vérification du code** (`free/ip_registry/adapters/probe.py:88-90`) :

```python
        self._body_v1, self._body_raw_v1 = self._load_payload(
            str(PAYLOAD_PATH) + ".bak"
        )
```

Le chemin `.bak` est **codé en dur**, dans le `__init__` — donc l'échec est
immédiat et non rattrapable.

**Vérification de la versionnabilité** :

```
$ ls -la free/official_payload.json*
-rw-r--r-- 1 msi 197121 96326 Nov 17 17:29 free/official_payload.json
-rw-r--r-- 1 msi 197121 96326 Nov 17 17:29 free/official_payload.json.bak

$ git check-ignore -v free/official_payload.json.bak
.gitignore:91:free/*.json.bak	free/official_payload.json.bak

$ git ls-files free/official_payload.json*
free/official_payload.json
```

**Seul le `.json` est versionné ; le `.bak` est ignoré.** Les deux fichiers
font 96 326 octets (donc actuellement identiques), mais seul l'un des deux
survit à un `git clone`.

**Le commentaire du code justifie la distinction** : le `.bak` porte la
capture **1.19.5** destinée aux profils 1.18.x/1.19.x, tandis que
`official_payload.json` porte la **2.2.5**. Le code explique que « *sending a
2.2.5 body with a 1.19.x UA is a wire mismatch and the gate answers 429
(disguised form rejection)* » — donc la distinction est **fonctionnellement
nécessaire**, pas cosmétique.

**Conséquence** : le fichier nécessaire au respect du principe XVIII (envoyer
le bon corps avec la bonne empreinte) est **absent de tout clone neuf**.

**C'est le troisième cas de dépendance à un fichier non versionné** trouvé
dans cet audit :

| Cas | Fichier requis | Versionné ? |
|---|---|---|
| V-33 | `logs/compose-control-plane-{1..6}.yml` | ❌ gitignoré |
| V-41 | `free/official_payload.json.bak` | ❌ gitignoré |
| V-15 | `free/captures/1.19.5.json` | ❌ supprimé du disque |

**Le fil rouge** : trois ressources dont dépend le fonctionnement de la jambe
free ne sont pas reconstructibles depuis le dépôt. Le critère d'acceptation
SC-008 de la spec 024 (« un `git clone` neuf produit une flotte
fonctionnelle ») est aujourd'hui **structurellement inatteignable**.

---

## V-42 — P0 du rapport 02 : quatre tables déclarées deux fois, DDL divergentes ✅ CONFIRMÉ

**Affirmation** : `server_transports`, `station_state`, `incidents` et
`repair_jobs` sont déclarées deux fois, avec des DDL qui divergent ; le
premier écrivain gagne silencieusement.

**Vérification — les déclarants** :

```
$ grep -rln "CREATE TABLE IF NOT EXISTS <table>"
server_transports  → free/qualification/store.py
                     rotation/infrastructure/db/schema.sql
station_state      → free/qualification/store.py
                     rotation/infrastructure/db/schema.sql
incidents          → free/qualification/store.py
                     rotation/infrastructure/db/schema.sql
                     scripts/station_slo_watch.py     ← un TROISIÈME
repair_jobs        → free/qualification/store.py
                     rotation/infrastructure/db/schema.sql
```

**`incidents` a trois déclarants.**

**Vérification de la divergence** — comparaison de `station_state` :

| Colonne | `free/qualification/store.py` | `rotation/infrastructure/db/schema.sql` |
|---|---|---|
| `container` | `TEXT NOT NULL DEFAULT ''` | `TEXT NOT NULL` (sans défaut) |
| `transport_effective` | `TEXT NOT NULL DEFAULT ''` | `TEXT` (nullable) |
| `state` | `TEXT NOT NULL DEFAULT ''` | `TEXT NOT NULL CHECK (state IN ('BOOT','CONNECTING','VERIFYING','USABLE','DRAINING','REPAIRING','STANDBY','BROKEN'))` |
| `country` | `TEXT NOT NULL DEFAULT ''` | `TEXT` (nullable) |
| `current_ip` | `TEXT NOT NULL DEFAULT ''` | `TEXT` (nullable) |
| `last_probe_at` | `REAL NOT NULL DEFAULT 0` | `REAL` (nullable) |
| `reason` | `TEXT NOT NULL DEFAULT ''` | `TEXT` (nullable) |

**La divergence est fonctionnelle, pas cosmétique** : la version
`schema.sql` porte une **contrainte `CHECK` sur `state`** limitant à 8
valeurs, et refuse `NULL` sur `container`. La version `store.py` **accepte
n'importe quelle chaîne** (défaut `''`) et tolère `NULL` sur cinq colonnes.

**Conséquence** : selon **l'ordre d'initialisation des modules**, la table est
créée avec ou sans contrainte `CHECK`. Le `IF NOT EXISTS` rend le conflit
**totalement silencieux** — aucune erreur, aucun avertissement, aucun log.

**C'est le même défaut que V-28** (deux schémas `ip_state`), mais **à quatre
tables de plus**. Total mesuré :

| Cas | Tables concernées |
|---|---|
| V-28 | `ip_state` (2 déclarants, colonnes incompatibles) |
| V-42 | `server_transports`, `station_state`, `incidents` (3 déclarants), `repair_jobs` |
| **Total** | **5 tables** avec déclarants multiples et DDL divergentes |

**Cause racine identifiée par le rapport 02** : `registry.db` a **52 tables**
mais `rotation/infrastructure/db/schema.sql` n'en déclare que **30**. Les
**22 autres** sont créées par **15 modules différents** à l'exécution. Il n'y a
donc **pas de propriétaire de schéma** — chaque module crée ce dont il a
besoin, quand il en a besoin.

**Violation directe du principe XIII** (« Le schéma a des clés primaires, clés
étrangères, contraintes, index et **migrations versionnées** ») : les
migrations existent (3 versions) mais ne couvrent pas les 22 tables hors
schéma.

---

## V-43 — P1 du rapport 02 : le chemin testé n'est pas le chemin livré ✅ CONFIRMÉ et étendu

**Affirmation** : trois gardes `PYTEST_CURRENT_TEST` dans `probe.py`
désactivent des chemins de production, donc « le chemin testé n'est pas le
chemin livré ».

**Vérification — le phénomène est plus étendu** : **10 sites** dans le code de
production, pas 3 :

```
free/ip_registry/adapters/probe.py:109, 179, 252
free/pool/__init__.py:7690
free/quota_ledger.py:335
free/rotation.py:568, 705
opencode.py:4575, 14709, 16020
```

**Analyse par nature** — tous ne sont pas problématiques. Deux catégories :

**Catégorie 1 — protections de test légitimes (2 sites)** :

| Site | Code | Effet |
|---|---|---|
| `free/quota_ledger.py:335` | `if os.getenv("PYTEST_CURRENT_TEST") and self._path == DEFAULT_PATH: self.persist = False` | empêche un test d'écrire dans la base réelle |
| `opencode.py:4575` | `if os.getenv("PYTEST_CURRENT_TEST"): return` | supprime un log de gate en test |

Ce sont de **bonnes pratiques** : elles évitent qu'un test corrompe l'état ou
pollue les logs.

**Catégorie 2 — chemins de production non testés (5+ sites)** :

| Site | Ce qui est sauté en test |
|---|---|
| `probe.py:109` | la **réutilisation de la preuve** (`ip_state`) |
| `probe.py:179` | la **persistance du 429** |
| `probe.py:252` | la transition `proven` → `free.ip_state` |
| `opencode.py:16020` | **`classify_429`** et **`ip_state`** sur le chemin client |
| `free/pool/__init__.py:7690` | *(à instruire)* |

**C'est le point grave** : les branches sautéest en test sont exactement
celles qui portent les **principes IV (reset_at), VII (preuve) et XVIII
(brûlage de format)**.

**Conséquence mesurable et cohérente avec le reste de l'audit** : la table
`ip_state` ne contient **que** `unproven`/`proven`, **jamais** `spent` ni
`burned` (V-3), et **0 ligne WARP** (V-2). Les transitions d'état qui
devraient produire ces valeurs sont précisément celles que
`PYTEST_CURRENT_TEST` désactive dans `probe.py:252`.

**Un test vert sur la persistance du 429 ne prouve rien du comportement
livré** — violation directe du principe XVII (« chaque exigence fonctionnelle
a un test automatisé »).

**Recommandation** : remplacer la lecture de la variable pytest par un **flag
de configuration explicite** (`probe.persist_evidence: true`) que le test
positionne comme il positionnerait n'importe quelle option. Le test exerce
alors le **même** code que la production.

---

## V-44 — P0-08 du rapport 02 : le garde d'unicité d'IP ne fonctionne pas ✅ CONFIRMÉ (bug de type)

**Affirmation** : `egress_ip_in_use` compare un entier à une colonne TEXT,
donc le garde d'unicité d'IP (principe V) est inopérant.

**Vérification du code** (`free/qualification/store.py:1122-1144`) :

```python
    def egress_ip_in_use(self, egress_ip: str, *, station: int = 0) -> bool:
        """True when a usable egress option is owned by another station.

        The sweep must never promote a duplicate IP.  The query is read-only
        and fails soft when the egress catalog tables are absent.
        """
        ip = str(egress_ip or "").strip()
        if not ip:
            return False
        with self._lock:
            try:
                row = self._conn.execute(
                    """
                    SELECT 1 FROM egress_options
                    WHERE egress_ip=? AND status='usable' AND station<>?
                    LIMIT 1
                    """,
                    (ip, str(int(station))),        # ← '1', '2', … (texte d'un entier)
                ).fetchone()
```

**Vérification du type réel de la colonne** :

```
egress_options.station : typeof = 'text'
valeurs observées       : 'warp-1', 'warp-2', '', 'station-2', 'station-3'…
```

**La colonne contient des identifiants textuels** (`'warp-1'`,
`'station-2'`), **jamais des entiers**. Le paramètre lié est `str(int(station))`
— donc `'1'`, `'2'`.

**En SQLite, `'warp-1' <> '1'` est vrai** (comparaison textuelle). Donc dès
qu'une option `usable` porte la même IP, la clause `station<>?` est **satisfaite
par toutes les lignes**, et la requête retourne `1`.

**Conséquence** : `egress_ip_in_use` retourne **`True` pour toute IP déjà
présente** dans `egress_options` avec `status='usable'` — y compris quand
c'est la **même** station qui la détient. Le commentaire du code dit
exactement l'inverse de ce qui se passe : « *The sweep must never promote a
duplicate IP* ».

**Corrélation avec les mesures** : cela explique le
`duplicate_ip_groups = 1` et `capacity_reason = "1 station(s) partagent une
IP"` observés dans `/api/vpn-status` (§1 de `00-live-evidence.md`), ainsi que
le parking d'options en `DUPLICATE`.

**C'est une violation du principe V** (« Claim exclusif par IP, index
d'unicité en base ») : le garde existe, est appelé, et **ne peut pas
fonctionner** à cause d'une comparaison de types.

**Note méthodologique** : c'est le deuxième bug de ce type trouvé dans
l'audit (après `time.monotonic()` persisté, V-22). SQLite étant **typé
dynamiquement**, une comparaison entre types incompatibles ne lève **aucune
erreur** — elle retourne simplement faux. Un `CHECK` de type ou une colonne
`INTEGER` aurait été détecté à l'écriture.

---

## V-45 — P1-06 du rapport 02 : le god-init coûte 63 % du budget d'import ✅ CONFIRMÉ

**Affirmation** : `from free.ip_registry.registry_db import reconcile` charge
60 modules `free.*` dont `control_plane`, `adapters/probe` et
`compose_binder`, pour un `reconcile()` qui n'en a pas besoin.

**Vérification** — mesure directe :

```python
import time, sys
t = time.perf_counter()
import free.ip_registry.registry_db as m
dt = time.perf_counter() - t
n = [x for x in sys.modules if x.startswith('free.')]
```

| Mesure | Valeur |
|---|---|
| durée d'import | **757 ms** |
| modules `free.*` chargés | **60** |
| dont `control_plane` | **8** |
| dont `adapters` | **8** |

**Le rapport annonçait 60 modules et 1,14 s ; la mesure confirme exactement
60 modules**, avec 757 ms sur cette machine.

**Lien avec la gate CI** : `.github/workflows/ci.yml:113-129` impose un
**budget d'import de 1 200 ms** pour `import opencode` :

```python
assert best < 1200, f"import opencode trop lent: {best:.0f} ms (budget 1200 ms)"
```

**Ce seul import consomme 757 ms, soit 63 % du budget.** Et
`tests/test_boot_phase3b_lazy.py:135` (`test_opencode_import_budget`) est
**l'unique test en échec** de la suite (mesuré à 1 723 ms contre un budget de
1 600 ms, §10).

**Cause structurelle** : `free/ip_registry/__init__.py` (67 l.) fait un
**re-export eager** de 6 adapters + allocator + sources + sqlite_registry +
status. Donc **tout** import d'un sous-module charge l'ensemble — y compris
`opencode.py:3589`, `free/ip_state.py:175`, `free/probe_budget.py:51` et
`free/control_plane/events.py:19`.

**Conséquence** : le budget d'import de la CI est **structurellement
inatteignable** tant que le re-export reste eager. Le test qui échoue n'est
pas un flake : c'est le symptôme d'un couplage d'import.

**Recommandation** : passer `free/ip_registry/__init__.py` en `__getattr__`
paresseux (PEP 562), ou importer directement les sous-modules depuis les
appelants. Le gain attendu est de l'ordre de plusieurs centaines de ms sur le
démarrage.

---

## V-46 — P1-03 du rapport 02 : `transport_health` recalculé à chaque tentative ✅ CONFIRMÉ

**Affirmation** : `refresh_transport_health` recalcule 2 agrégats O(n) à
**chaque** tentative (~61 ms), donnant une croissance O(n²).

**Vérification — les sites d'appel** (`free/qualification/store.py`) :

```
27:   from .transport_health import refresh_transport_health
1265: refresh_transport_health(self._conn, attempt, min_attempts=100, now=float(attempt.finished_at or self._clock()))
1332: refresh_transport_health(
```

**Deux appels dans le chemin d'écriture d'une tentative** — donc **par
tentative**, pas par cycle.

**Vérification de l'ampleur** — la table `transport_health` ne contient que
**30 lignes** (agrégats par `(transport, station)`), mais les valeurs
montrent le volume sous-jacent :

| transport | station | total | succès | échecs | latence moyenne |
|---|---|---|---|---|---|
| `wireguard` | 0 | **34 944** | 2 775 | 30 644 | 2 156 ms |
| `wireguard` | 5 | 1 213 | 414 | 571 | 2 109 ms |
| `wireguard` | 3 | 7 105 | 499 | 6 345 | 2 156 ms |

Les agrégats portent sur des dizaines de milliers de tentatives issues de
`scan_attempts` (**61 027 lignes**). Recalculer ces sommes **à chaque
nouvelle tentative** est un coût O(n) par tentative → **O(n²)** sur
l'ensemble.

**Corrélation avec le volume mesuré** : 61 027 `scan_attempts` et 40 761
`qualification_attempts` (§4.1 de `00-live-evidence.md`) — le système écrit
beaucoup de tentatives, et paie un recalcul complet à chacune.

**Recommandation** : passer `refresh_transport_health` en **agrégat
incrémental** (mise à jour des compteurs au lieu du recalcul), ou le
déclencher **par lot** (toutes les N tentatives ou à la fin d'un cycle)
plutôt qu'à chaque écriture.

---

## V-47 — Shims racine du rapport 06 : confirmés morts, avec précision ✅

**Affirmation** : les shims racine (`traffic_capture.py`, `protocol_mapping.py`,
`free_discovery.py`…) sont des façades sans consommateur de production.

**Vérification — le piège du grep par nom** : une recherche naïve donne des
résultats **trompeurs** :

| Fichier | Fichiers le mentionnant | Verdict naïf | Verdict réel |
|---|---|---|---|
| `ddgtest.py` | **0** | mort | ✅ mort |
| `wstest.py` | **0** | mort | ✅ mort |
| `free_discovery.py` | 14 | vivant | ⚠️ à instruire |
| `traffic_capture.py` | 9 | vivant | ✅ **mort** (shim) |
| `protocol_mapping.py` | 31 | vivant | ✅ **quasi mort** (1 usage) |

**Vérification par lecture des sites** — les mentions ne sont pas des imports :

`opencode.py:4893-4897` :

```python
# (traffic_capture.py = shim).
from observability.capture import capture as _traffic_capture  # noqa: E402
```

`dashboard/api.py:40-41` :

```python
# [Phase 2 refonte] domicile canonique : observability.capture (traffic_capture.py = shim).
from observability.capture import capture as _traffic_capture
```

**Le code importe `observability.capture`** et mentionne `traffic_capture.py`
**dans un commentaire** qui dit explicitement « = shim ». Le shim n'est
**jamais importé**.

`opencode.py:5309-5312` — le seul usage runtime réel de `protocol_mapping` :

```python
        # protocol_mapping, exposés ici pour un point de collecte unique).
            import protocol_mapping as _pm
```

Et `opencode.py:19964` :

```python
# ([Phase 4] déplacé depuis protocol_mapping.py — shim conservé). Imported
```

**Un seul usage** (`conversion_cache_stats` pour `/metrics`), le reste étant
des commentaires « *shim conservé* ».

**Leçon méthodologique (deuxième occurrence dans cet audit)** : un grep de
« qui mentionne X » ne distingue pas un **import** d'un **commentaire**. Le
rapport 06 avait raison sur le fond ; la vérification le confirme et précise
que `traffic_capture.py` a **0 import runtime** malgré 9 fichiers qui le
nomment.

**Recommandation** : migrer le seul usage de `protocol_mapping` vers
`app.protocol.mapping` (mettre à jour `tests/test_phase0_contracts.py` qui
bloque la suppression), puis supprimer les 4 shims morts
(`traffic_capture.py`, `free_discovery.py`, `ddgtest.py`, `wstest.py`).

---

## V-48 — P1-1 du rapport 09 : le gate « zéro option morte » est permissif sur 62 % des clés ✅ CONFIRMÉ

**Affirmation** : `config_audit.py:266` déclare une clé vivante dès que son
**nom de feuille** apparaît entre guillemets n'importe où dans le code, donc
**392 des 625 clés (62 %)** ne sont jamais réellement vérifiées.

**Vérification du code** (`scripts/config_audit.py:260-268`) :

```python
        feuille = cle.rsplit(".", 1)[-1]
        # « utilisée » = son nom apparaît quelque part dans le code, ou elle est
        # lue via yaml_get sous sa forme complète. Volontairement permissif :
        # on cherche le silence, pas la pureté.
        if cle in lues_exactement:
            continue
        if re.search(rf"[\"']{re.escape(feuille)}[\"']", source):
            continue                       # ← "vivante" par coïncidence de nom
        mortes.append(cle)
```

Le commentaire **assume** la permissivité (« on cherche le silence, pas la
pureté »). Le problème est que le principe XII exige l'inverse.

**Vérification de l'ampleur du faux positif** :

```
$ grep -rho '"enabled"' --include="*.py" . | wc -l
1110
```

**1 110 occurrences de la seule chaîne `"enabled"`.** Donc toute clé de
config nommée `enabled` — et il y en a des dizaines (`retry_until_success.enabled`,
`cache.enabled`, `web_search.enabled`, `web_fetch.enabled`, `free.enabled`…) —
est déclarée « vivante » par **simple coïncidence de nom**.

**Le seul garde-fou réel** :

```python
STRICT_SECTIONS = {"ip_registry"}
```

**Une section sur 45.** Pour `ip_registry` uniquement, le gate exige une
lecture explicite de la feuille.

**Conséquence** : un `config_audit --strict` vert — qui est une **gate CI
bloquante** (`ci.yml:58-64`) — ne prouve **rien sur 62 % des clés**. Les 6
clés legacy auto-déclarées à retirer depuis le 2026-09-08 sont dans une liste
« informative » du baseline, donc **ne feront jamais échouer la CI**.

**Corrélation avec le rapport 09** : le différentiel réel est plus propre
qu'annoncé — après vérification AST des deux formes d'appel, **0 clé morte
prouvable** (y compris les 20 clés `ip_registry.*` que d'autres rapports
déclaraient mortes : leurs consommateurs **existent**, dans
`scripts/fresh_chain*.py`). Le vrai défaut est donc **« consommateur non
atteignable en production »**, pas « clé sans consommateur ».

**Nuance importante** : le rapport 09 **corrige** une conclusion hâtive de
mon V-27 et du rapport 04. Les clés `ip_registry.selection.*` ont bien des
consommateurs (`rotation/infrastructure/config/schema.py`,
`scripts/fresh_chain*.py`) — mais **ces modules ne sont pas branchés en
production**. C'est le même défaut de famille C, pas un défaut de clé.

**Recommandation** : inverser la logique du gate — exiger une **lecture
explicite** (`yaml_get("section", "clé")` avec les deux littéraux) pour
**toutes** les sections, et traiter l'heuristique de nom comme un
avertissement, jamais comme une preuve.

---

## V-49 — P0-1 du rapport 09 : nuance sur la collecte des tests ✅ CORRIGÉ

**Affirmation du rapport 09** : « 13 fichiers de tests non suivis **ne sont
pas collectés** par la CI (`testpaths=["tests"]`) ».

**Vérification — la collecte fonctionne** :

```
$ python -m pytest tests/unit tests/contract --collect-only -q
tests/contract/test_client_families.py: 25
tests/contract/test_client_sse_contract.py: 6
tests/unit/test_classify_429.py: 9
tests/unit/test_client_registry.py: 8
tests/unit/test_egress_ip.py: (…)
tests/unit/test_ip_state.py: (…)
tests/unit/test_probe_budget.py: 4
tests/unit/test_rotation_policy.py: 7
tests/unit/test_tool_policy.py: 6
tests/unit/test_wire_admission.py: 6
tests/unit/test_wire_profile.py: 7
```

`pyproject.toml:53-56` déclare `testpaths = ["tests"]` et
`python_files = ["test_*.py"]` — pytest **descend récursivement** dans
`tests/`, donc `tests/unit/` et `tests/contract/` sont **collectés** (78
tests).

**La conclusion du rapport 09 est donc inexacte sur ce point** : les tests
**sont** exécutés.

**Le vrai problème reste entier, et il est réel** : ces fichiers sont
**non versionnés** (`git status` → `?? tests/unit/`, `?? tests/contract/`).
Un `git clean -fd` les **efface**, et avec eux la couverture des modules 023
(`ip_state`, `wire_profile`, `wire_admission`, `classify_429`,
`probe_budget`, `rotation_policy`).

**Énoncé corrigé** : la CI **collecte** ces tests sur la machine où ils
existent, mais ils **n'existent que sur cette machine**. Sur un clone neuf ou
après un nettoyage, **78 tests disparaissent silencieusement** — et la
couverture du socle 023 avec eux.

**C'est la même famille C** (ressource critique hors du dépôt) que V-33 et
V-41 : le système dépend de fichiers non versionnés, sans que rien ne le
signale.

---

## V-50 — Écarts doc ↔ réalité confirmés ✅ (bugs selon le principe XVII)

Le principe XVII pose : « Le changement met à jour dans le même lot : spec,
plan, `CHANGELOG`, `option-map`, config et documentation. **Un écart
doc/config est un bug.** »

**Vérification de deux écarts dans `CLAUDE.md`** (le fichier que lisent les
agents pour travailler sur ce dépôt) :

**Écart 1 — `WEB_PORT` n'existe plus mais est documenté.**

```
CLAUDE.md:89  - `__init__.py` - Exports: … `HOST`, `PORT`, `WEB_PORT`, `MODELS` …
config/settings.py:825  # [P6] WEB_PORT supprimé : rien n'a jamais écouté sur :8082
```

**Le code dit explicitement que la constante a été supprimée**, et la doc
d'architecture la liste encore comme export de `config/__init__.py`.

**Écart 2 — la taille de `requests.db` est fausse d'un facteur 20.**

```
CLAUDE.md:134  - Logs/DB : jamais `Read` sur `logs/requests.db` (3 Go) …
$ ls -lh logs/requests.db
-rw-r--r-- 1 msi 197121 160M
```

La doc annonce **3 Go**, la réalité est **160 Mo**. L'écart n'est pas
anodin : c'est la **justification** de la règle « ne jamais lire ce fichier »,
règle qui reste bonne mais dont le motif est faux.

**Autres écarts relevés par le rapport 09** (à vérifier avant correction) :

| Fichier | Écart |
|---|---|
| `README.md:341-342,349` | documente **3 endpoints inexistants** (`/api/config/tool-capabilities` GET+POST, `/api/tools`) — 0 occurrence dans le dépôt |
| `README.md:91` | chemin de verrou faux |
| `README.md:423` | annonce **~740 tests** (réel : **6 385**) |
| `README.md` (URLs) | 3 URLs upstream fausses (`zen/v1/*` vs `inference/*`) |
| `docs/runbook-docker.md:21-23` | décrit des boutons disparus du front Svelte |
| `config/settings.py:146` | cite `WEB_PORT` |

**Pourquoi c'est un bug et pas une coquille** : `CLAUDE.md` est **chargé
automatiquement** dans le contexte de chaque agent qui travaille sur ce
dépôt. Une constante inexistante et une taille fausse d'un facteur 20
produisent des décisions erronées — ici, la règle « ne pas lire
`requests.db` » aurait pu être levée par un agent constatant 160 Mo au lieu
des 3 Go annoncés.

**Recommandation** : mettre à jour `CLAUDE.md` et `README.md` **dans le même
lot** que les correctifs (principe XVII), et ajouter un test qui vérifie que
les endpoints cités par le README existent réellement.

---

## V-51 — Les 6 clés legacy et leur lien avec les fichiers d'état périmés ✅ CONFIRMÉ

**Vérification** — les 6 clés `legacy_a_retirer` du baseline existent
**toutes** dans `config.yaml` :

| Clé | Ligne | Valeur |
|---|---|---|
| `compose_service_2` | 118 | `vpn-gluetun-2` |
| `credentials_file` | 126 | `vpn_configs/credentials.txt` |
| `dual_station` | 135 | `false` |
| `socks5_proxy_port_2` | 660 | `1081` |
| **`state_file_2`** | **664** | **`logs/vpn_state2.json`** |
| `vpn_proxy_port_2` | 720 | `8889` |

**Le baseline les déclare hors gate** :

```json
"legacy_a_retirer": ["ip_rotation.dual_station", "ip_rotation.vpn_proxy_port_2",
                     "ip_rotation.socks5_proxy_port_2", "ip_rotation.state_file_2",
                     "ip_rotation.compose_service_2", "ip_rotation.credentials_file"],
"_legacy_comment": "Informative (non utilisée par le gate) : secondes sources
                    obsolètes lues pour rétro-compat …"
```

**« Informative (non utilisée par le gate) »** — ces clés ne peuvent donc
**jamais** faire échouer `config_audit --strict`, quelle que soit leur
inactivité.

**Découverte de l'auditeur principal — le lien avec V-21.2** :

`state_file_2: logs/vpn_state2.json` désigne **exactement** l'un des 6
fichiers d'état périmés mesurés en V-21.2 :

| Fichier | Dernière écriture |
|---|---|
| `vpn_state.json` | 2026-11-18 10:10 |
| **`vpn_state2.json`** | **2026-11-18 12:21** ← pointé par `state_file_2` |
| `vpn_state3.json` | 2026-11-18 00:54 |
| `vpn_state4.json` | 2026-11-18 11:19 |
| `vpn_state5.json` | 2026-11-20 09:04 |
| `vpn_state6.json` | 2026-11-18 19:01 |

**La clé legacy n'est pas seulement morte : elle désigne un artefact qui
existe encore et contient un état périmé.** Le « plan de retrait avec ADR »
annoncé par le commentaire n'a jamais eu lieu, et les deux moitiés du problème
(la clé et le fichier) subsistent.

**Recommandation** : supprimer les 6 clés **et** les 6 fichiers
`logs/vpn_state*.json` **dans le même lot**, en vérifiant d'abord qu'aucun
code ne lit `state_file_2` (la clé étant déclarée morte, ce devrait être le
cas — mais la vérification doit être faite, pas supposée).

---

## V-52 — P0-2 du rapport 07 : la forme « Responses » retourne 0 token caché ✅ CONFIRMÉ PAR EXÉCUTION

**Affirmation** : `_extract_cache_tokens` ne lit que la forme Chat, pas la
forme Responses, ce qui **annule le correctif anti-autocompact du
2026-10-26** sur ces portes.

**Vérification par exécution directe** :

```python
from app.protocol.mapping import _extract_cache_tokens as f
f({'prompt_tokens': 1000, 'prompt_tokens_details': {'cached_tokens': 900}})  # Chat
f({'input_tokens': 1000, 'input_tokens_details': {'cached_tokens': 900}})    # Responses
```

Résultat mesuré :

| Forme | Attendu | **Obtenu** |
|---|---|---|
| Chat (`prompt_tokens_details.cached_tokens`) | 900 | **900** ✅ |
| **Responses (`input_tokens_details.cached_tokens`)** | 900 | **0** ❌ |

**La forme Responses retourne 0.** L'information de cache est **perdue**.

**Pourquoi c'est grave** : le `CLAUDE.md` du projet documente que la cause
racine du « Autocompact thrashing » était exactement ce type d'erreur —
« *l'usage OpenAI (`prompt_tokens`, cache inclus) était relayé tel quel en
`input_tokens` **et** en `cache_read_input_tokens` : Claude Code additionne
les deux et voyait ~2× le contexte réel* ». Le correctif du 2026-10-26 a
traité le **chemin Chat**.

**Le rapport 07 établit que le correctif est complet sur Chat, mais que
quatre chemins refont l'erreur ou en perdent l'information** — dont
`/v1/responses` et la jambe free Responses, où `message_start` /
`message_delta` annoncent `cache_read_input_tokens: 0` **alors que le
contexte est massivement caché**.

**Conséquence pour l'opérateur** : sur ces portes, le client voit un contexte
**sous-évalué** (0 au lieu de la réalité). C'est l'erreur inverse de celle
corrigée en octobre, avec le même effet : une comptabilité de contexte fausse.

**Recommandation** : `_extract_cache_tokens` doit lire **les deux formes**
(`prompt_tokens_details` **et** `input_tokens_details`), et un test de contrat
doit couvrir les deux.

---

## V-53 — P0-3 du rapport 07 : `TypeError` non protégé → 500 ✅ CONFIRMÉ PAR EXÉCUTION

**Affirmation** : `_extract_cache_tokens` lève `TypeError` sur un `details`
non-dict, et deux sites d'appel sont hors de tout `try`.

**Vérification par exécution** :

```python
f({'prompt_tokens': 10, 'prompt_tokens_details': 5})
→ TypeError: argument of type 'int' is not iterable
```

**L'exception est levée**, confirmée.

**Vérification des sites d'appel non protégés** :

`opencode.py:27626-27630` (handler `chat_completions`) :

```python
            req_in = usage.get("prompt_tokens", 0)
            req_out = usage.get("completion_tokens", 0)
            cache = _extract_cache_tokens(usage)                          # ← hors try
            _debug(f"  usage: in={req_in} out={req_out} cache={cache}")
            _update_token_usage(model_id, req_in, req_out, cache, _extract_cache_creation_tokens(usage))
```

`opencode.py:31790-31793` (handler `responses`) :

```python
                    req_in = usage.get("prompt_tokens", 0)
                    req_out = usage.get("completion_tokens", 0)
                    cache = _extract_cache_tokens(usage)                  # ← hors try
                    _update_token_usage(_actual_model, req_in, req_out, cache, _extract_cache_creation_tokens(usage))
```

**Aucun `try` autour de ces appels.** Une réponse amont valide mais dont
`*_details` est un entier (au lieu d'un objet) provoque un **500** sur le
client.

**Sévérité** : c'est un **crash sur données amont valides**. Le principe XVII
exige qu'« un cycle, une rotation ou une migration échoué conserve l'état
précédent » — ici, une réponse par ailleurs correcte est perdue à cause d'un
champ de télémétrie.

**Recommandation** : `_extract_cache_tokens` doit être **défensif**
(`isinstance(details, dict)`), et les deux sites d'appel doivent être dans le
`try` du handler. Un test doit couvrir `details` non-dict sur les deux portes.

---

## V-54 — P0-1 du rapport 07 : le plafond du cache n'existe pas ✅ CONFIRMÉ

**Affirmation** : `_anthropic_cache_max_bytes = 64 MB` est déclaré et exposé
au dashboard, mais jamais incrémenté ni vérifié.

**Vérification — les deux seules occurrences** :

```
app/protocol/mapping.py:2047:  _anthropic_cache_max_bytes = 64 * 1024 * 1024
app/protocol/mapping.py:2066:      "max_bytes": _anthropic_cache_max_bytes,
```

**La ligne 2047 est la déclaration, la ligne 2066 est un dict d'exposition**
(probablement pour `/metrics` ou le dashboard). **Aucune troisième
occurrence** — donc jamais de comparaison, jamais d'éviction sur ce seuil.

**Conséquence** : le commentaire du code dit avoir corrigé le risque
d'explosion mémoire, mais **le garde-fou est décoratif**. Si le cache retient
512 corps de 10 Mo, cela représente **~5 Go de RSS**.

**Note** : le rapport mentionne « exactement le risque que le commentaire dit
avoir corrigé » — c'est le **troisième cas** dans cet audit d'un garde-fou
écrit mais non branché, après `wire_admission` (V-3) et
`provider_capabilities.capacity()` (V-14).

**Recommandation** : implémenter l'éviction sur le seuil (LRU par octets) et
exposer `current_bytes` / `max_bytes` dans `/metrics`, avec un test qui
remplit le cache au-delà du plafond et vérifie que la taille redescend.

---

## V-55 — P0-4 du rapport 07 : l'usage Responses est mis à zéro ✅ CONFIRMÉ

**Affirmation** : le mapping d'usage d'une réponse Responses lit les clés de
la forme Chat, donc retourne des zéros.

**Vérification du code** (`app/protocol/mapping.py:3453-3456`) :

```python
    # Usage mapping
    prompt_tokens = usage.get("prompt_tokens", 0)         # ← clé forme Chat
    completion_tokens = usage.get("completion_tokens", 0) # ← clé forme Chat
    cached = _extract_cache_tokens(usage)
    oai_usage = {
        "input_tokens": prompt_tokens,
        "output_tokens": completion_tokens,
        "total_tokens": prompt_tokens + completion_tokens,
```

**Le contexte est une fonction de conversion vers la forme Responses**, mais
elle lit `prompt_tokens` / `completion_tokens` — les noms de la forme
**Chat**. Une réponse Responses porte `input_tokens` / `output_tokens`.

**Résultat** : `usage.get("prompt_tokens", 0)` → **0**, et l'usage complet est
**mis à zéro**.

**Corrélation avec V-52** : c'est le **même défaut de forme** que
`_extract_cache_tokens` (Responses → 0), mais appliqué à l'usage entier, pas
seulement au cache.

**Le rapport 07 identifie deux sites consommateurs** (`opencode.py:32388-32458`
et `:32950-33032`) qui stockent l'usage brut puis l'injectent dans un dict de
forme Chat ; `_finalize_stream_tokens` retombe alors sur l'**estimation**
`len // 3`.

**Conséquence** : sur les portes Responses, la télémétrie de tokens est
**fausse** — soit zéro, soit une estimation grossière — alors que la valeur
exacte était disponible dans la réponse amont.

**C'est le quatrième chemin** qui « refait l'erreur ou en perd
l'information », comme l'annonce le rapport : la racine structurelle est la
**duplication de la normalisation d'usage en 4 endroits** avec des variantes
divergentes (`mapping.py:248`, `chat_sse_to_anthropic.py:710`,
`opencode.py:20307`, `:29550`). Couvert par la tâche T118.

---

## V-56 — P1 du rapport 07 : `/healthz` et `/readyz` sont rate-limités ✅ CONFIRMÉ

**Affirmation** : `/readyz` et `/metrics` peuvent recevoir un 429 à cause du
rate limiter.

**Vérification** (`server/throttle.py:97` et `:126`) :

```python
    _SKIP_PREFIXES = ("/api/", "/static/", "/health")
    ...
        if path == "/health" or any(path.startswith(p) for p in self._SKIP_PREFIXES):
```

**La garde teste `path == "/health"` en égalité stricte**, et les préfixes
exclus sont `/api/`, `/static/`, `/health`.

**Vérification des endpoints déclarés** (`opencode.py:4494`) :

```
# /healthz = liveness pure (jamais I/O ni DB) ; /readyz = readiness warmup ;
```

Le système déclare donc **trois** sondes de santé : `/health` (agrégée),
`/healthz` (liveness) et `/readyz` (readiness).

| Endpoint | Rate-limité ? |
|---|---|
| `/health` | ✅ exclu (égalité stricte) |
| **`/healthz`** | ❌ **limité** |
| **`/readyz`** | ❌ **limité** |
| `/startupz` | ❌ limité |
| **`/metrics`** | ❌ **limité** |

**Conséquence** : un orchestrateur (Kubernetes `livenessProbe`,
`readinessProbe`) ou un superviseur qui sonde `/healthz` ou `/readyz` peut
recevoir un **429** au lieu de l'état de santé. Un probe qui échoue sur 429
peut déclencher un **redémarrage de pod** ou un **retrait du load balancer**
sur un service parfaitement sain.

**Aggravant** : `/metrics` est aussi limité — or c'est précisément
l'endpoint qu'un scraper Prometheus interroge **périodiquement**, et le
système dispose d'un `app_supervisor` qui pourrait s'en servir.

**Le défaut est une asymétrie** : le code exclut `/health` mais pas les deux
autres sondes qu'il déclare lui-même deux lignes plus loin dans le même
fichier.

**Recommandation** : étendre `_SKIP_PREFIXES` à `("/api/", "/static/",
"/health", "/healthz", "/readyz", "/startupz", "/metrics")` — ou remplacer
l'égalité stricte par un test de famille (`path.startswith("/health")`).

---

## V-57 — P1 du rapport 07 : `outer["committed"]` jamais écrit ✅ CONFIRMÉ

**Affirmation** : la clé `committed` est initialisée mais **jamais écrite**,
rendant trois branches inatteignables — un client peut attendre 120 s sans
recevoir un octet.

**Vérification** :

```
$ grep -n '"committed"' streaming/retry_until_success.py
709:    outer = {"committed": False}      ← initialisation
```

Le rapport indique **5 lectures et 0 écriture**. Le mécanisme visé est clair :
`committed` doit passer à `True` dès que le premier octet est envoyé au
client, ce qui autorise le keepalive (`_sleep_with_keepalive:565`) et la
branche `outer_committed` de `_finish_with_error:650`.

**Conséquence** : si la valeur reste `False` à vie, le système croit
**toujours** qu'aucun octet n'est parti — donc les protections qui en
dépendent ne s'arment jamais.

**C'est le quatrième garde-fou écrit mais non branché** trouvé dans cet audit,
après `wire_admission` (V-3), `provider_capabilities.capacity()` (V-14) et
`_anthropic_cache_max_bytes` (V-54).

**Le motif est récurrent** : une variable ou une constante est déclarée, lue
par endroits, exposée parfois — mais **jamais alimentée**. Le code *semble*
protégé et ne l'est pas.

**Recommandation** : écrire `outer["committed"] = True` au premier yield réel,
et ajouter un test qui vérifie qu'un client qui reçoit un octet puis un
timeout reçoit bien le keepalive.

---

## V-58 — P0-07 du rapport 02 : les sauvegardes ne sont ni vérifiées ni purgées ✅ CONFIRMÉ

**Affirmation** : la sauvegarde `VACUUM INTO` n'est pas vérifiée par
`integrity_check`, et aucune rétention ne s'applique.

**Vérification du code** (`rotation/infrastructure/db/retention.py:51-57`) :

```python
def backup(conn: sqlite3.Connection, path: str | Path) -> Path:
    target = Path(path)
    target.parent.mkdir(parents=True, exist_ok=True)
    if target.exists():
        target.unlink()
    conn.execute("VACUUM INTO ?", (str(target),))
    return target
```

**La fonction se termine après `VACUUM INTO`.** Aucun `PRAGMA
integrity_check` sur `target`, aucun comptage de lignes, aucune comparaison
avec la source.

**Contraste avec le principe XIII**, qui exige « un backup `VACUUM INTO`
**vérifié** ». La vérification existe pourtant dans le même module
(`integrity_ok()`, `:46-48`) — mais elle est appliquée à la **source**, pas à
la copie produite.

**Vérification de l'ampleur** (rapport 02 + mes mesures §11.3) :

| Mesure | Valeur |
|---|---|
| `logs/backups/` | **13 Go**, **188 fichiers** |
| Plus gros | `requests-20261116T174327Z.db` = **998 Mo** |
| Cadence | ~140 Mo **2×/jour** depuis le 2026-11-05 |
| Rétention | **aucune** |

**Une sauvegarde non vérifiée est une croyance, pas une garantie** : le
fichier peut être tronqué, corrompu ou incomplet, et personne ne le saura
avant d'en avoir besoin — précisément au moment où la restauration compte.

**Recommandation** : `backup()` doit exécuter `PRAGMA integrity_check` sur la
**copie** et échouer si le résultat n'est pas `ok` ; ajouter une rétention
(7 copies ou 3 jours) et supprimer les 188 fichiers actuels après
vérification du plus récent.

---

## V-60 — Le contournement géo est une régression **isolée**, pas systémique ✅ (précision importante)

**Contexte** : en vérifiant la faisabilité du correctif T003, l'auditeur
principal a recherché **tous** les sites du motif `elif _geo_tunnel` — il y en
a **7**, pas 2.

**Vérification site par site** :

| Ligne | Motif | État |
|---|---|---|
| 20949 | `if free_result` → `elif _geo_tunnel` → `_open_via_pool` | ✅ **correct** |
| 22583 | idem | ✅ **correct** |
| 31425 | idem | ✅ **correct** |
| **31590 + 31596** | `elif _geo_tunnel` → `model_id = _actual_model` **puis** un second `elif _geo_tunnel` → `_open_via_pool` | ❌ **CASSÉ** |
| 31894 | `if free_result` → `elif _geo_tunnel` → `_open_via_pool` | ✅ **correct** |
| 32224 | idem (+ commentaire sur la fermeture du stream) | ✅ **correct** |

**Conclusion** : **6 des 7 sites appliquent correctement la porte géo.** Un
seul — `opencode.py:31590-31607` — a la branche `elif _geo_tunnel`
**dupliquée**, la première écrasant la seconde.

**Cela change trois choses dans l'analyse** :

1. **Ce n'est pas un défaut systémique** mais une **régression isolée**,
   probablement introduite en éditant ce bloc (le second `elif` a été laissé
   en place après l'ajout du premier).
2. **Le patron correct est disponible** : les 6 autres sites montrent
   exactement ce qu'il faut écrire — `elif _geo_tunnel:` → `async with
   _open_via_pool(...)`. Le correctif est donc une **copie**, pas une
   conception.
3. **Le risque du correctif est faible** : la branche fautive
   (`model_id = _actual_model`) est **inatteignable en pratique** puisqu'elle
   lève `UnboundLocalError` — la supprimer ne retire aucun comportement
   fonctionnel.

**Impact mesuré inchangé** : sur ce chemin (free épuisé + géo restreinte +
`require_vpn: true`), la requête **ne passe pas par le tunnel** — soit elle
échoue en 500, soit (si l'`UnboundLocalError` était corrigé naïvement en liant
`_actual_model`) elle partirait **en direct**. La sévérité reste un P0 de
sécurité ; c'est sa **portée** qui est plus étroite qu'estimé (1 porte sur 3,
pas toutes).

**Recommandation précisée** : aligner `opencode.py:31588-31607` sur le patron
des 6 autres sites, et ajouter un **test paramétré sur les 3 portes**
(`messages`, `chat_completions`, `responses`) qui vérifie que chacune route par
le tunnel quand `_geo_tunnel` est vrai. Le test doit échouer sur la porte
`responses` avant le correctif.

---

## V-61 — P0-03 du rapport 02 : le TTL des profils est neutralisé ✅ CONFIRMÉ (mécanisme exact)

**Affirmation** : `refresh_status` rétrograde `verified → stale` une fois, puis
re-promeut au 2ᵉ appel avec un nouveau `expires_at` **sans nouvelle preuve**.

**Vérification** (`free/profile_registry.py:242-266`) :

```python
    def refresh_status(self, now: float | None = None) -> str:
        ts = _now(now)
        if self.status == STATUS_REJECTED:
            return self.status
        if (
            self.status == STATUS_VERIFIED
            and self.expires_at is not None
            and ts >= float(self.expires_at)
        ):
            self.status = STATUS_STALE          # ← 1er appel : rétrogradation
            return self.status
        passes = len(self.pass_blocks())
        rejects = len(self.reject_blocks())
        if rejects >= MAX_REJECTS:
            self.status = STATUS_REJECTED
            return self.status
        if passes >= MIN_PASSES:
            if self.status != STATUS_VERIFIED:
                self.verified_at = ts
                self.expires_at = ts + float(self.ttl_s)   # ← RE-PROMOTION + TTL
                self.status = STATUS_VERIFIED
            return self.status
```

**Le mécanisme est en deux temps** :

1. **Appel 1** (TTL dépassé) : la garde `ts >= expires_at` déclenche →
   `status = STATUS_STALE` → `return`.
2. **Appel 2** : `status` vaut maintenant `STALE`, donc la **première garde ne
   s'applique plus** (elle exige `status == VERIFIED`). Le code descend, lit
   `pass_blocks()` — **les mêmes preuves, inchangées** — et comme
   `passes >= MIN_PASSES` était déjà vrai, il **re-promeut** avec
   `expires_at = ts + ttl_s`.

**Le TTL se contourne lui-même** : il faut deux appels pour l'épuiser, et le
second annule le premier. Un profil expiré depuis le 2026-11-13 est donc
`VERIFIED` à nouveau après un simple second appel.

**Corrélation mesurée** : `free/profiles/verified_profiles.json` contient
**2 profils, tous deux expirés depuis le 2026-11-13** — et le rapport 05
mesure un `RuntimeError` au 1ᵉʳ appel après démarrage (`opencode.py:11107`,
`call1 profile=None`, `call2 profile=oc-2.2.5-cli-13`), ce qui correspond
exactement à cette séquence en deux temps.

**Violation du principe VII** : une preuve qui n'est pas rafraîchie ne peut pas
fonder un statut `VERIFIED`.

**Recommandation** : la re-promotion doit exiger **une preuve postérieure à
`expires_at`** (comparer `max(evidence.tested_at) > expires_at`), pas
seulement `passes >= MIN_PASSES`.

---

## V-62 — P0-2 du rapport 03 : l'auto-update surveille l'image upstream ✅ CONFIRMÉ

**Affirmation** : `check_update` compare et pull `qmcgaw/gluetun` pendant que
la flotte tourne `gluetun-016:local`.

**Vérification** (`vpn/manager.py:9420-9428`) :

```python
        if not self._update_enabled:
            return False
        try:
            image = "qmcgaw/gluetun"                      # ← nom codé en dur
            old_digest = await self._docker_repo_digest(image)
            if not old_digest:
                return False  # image not installed yet — nothing to update
            old_id = await self._docker_image_id(image)
            compose_file = self._compose_file_path()
```

**L'image surveillée est `qmcgaw/gluetun`, littéralement**, alors que les 6
conteneurs vivants tournent `gluetun-016:local` (V-29).

**Vérification du chemin d'application** (`vpn/manager.py:9515-9529`) :

```python
                            "--pull",
                            "never",
                            self._compose_service,
                        ],
                        120,
                        env=self._compose_env(),
                    )
                ...
                started_at = await self._wait_healthy(timeout=120)
```

`apply_update` recrée avec **`--pull never`** : l'image utilisée est donc celle
que **compose résout** — c'est-à-dire l'upstream de la stanza pour 5 des 6
services (V-29).

**La chaîne complète du risque** :

1. `check_update` compare le digest de **`qmcgaw/gluetun`** (upstream) ;
2. il signale « mise à jour disponible » sur un changement **sans rapport**
   avec le fork ;
3. `apply_update` recrée **toutes** les stations avec `--pull never` ;
4. compose résout l'image **upstream** → l'API v2 disparaît → **404** sur les
   rotations ;
5. `_rollback_update` retague sur `qmcgaw/gluetun` — donc le rollback
   **aggrave** au lieu de corriger.

**Aggravant** : `_watchdog_escalate` (`manager.py:10817-10824`) appelle
`apply_update(check_opportune=True)` **pendant une panne**, et
`_update_opportune()` retourne `True` par construction dans ce cas
(`manager.py:9502`). **Le mécanisme de réparation peut donc déclencher la
destruction.**

**Recommandation** : dériver l'image depuis la définition compose réelle
(`docker compose config --format json` → `.services[<svc>].image`) et
**refuser** `check_update` quand l'image résolue n'est pas `qmcgaw/gluetun`.

---

## V-63 — P0-04 du rapport 05 : le worker de probe contourne l'isolation ✅ CONFIRMÉ

**Affirmation** : le pool sert en `isolation: container` mais les probes
partent de l'hôte.

**Vérification** (`config.yaml:262`) :

```yaml
    isolation: container
```

**Vérification du code** (`free/egress_adapters/public.py:67-70`) :

```python
    if not candidates:
        return {}
    from free.public_egress_worker import start_worker_process

    upstreams = {
        f"p{index}": str(candidate["url"])
        for index, candidate in enumerate(candidates)
    }
```

**Le code importe et lance `start_worker_process`** — un processus **sur
l'hôte**, pas un conteneur. Le rapport mesure ~450 connexions par cycle
depuis l'hôte, sans `cap-drop` ni `read-only`.

**L'incohérence** : la configuration **déclare** une isolation conteneur, et
le code fait l'inverse. Un opérateur qui lit `config.yaml` croit que les
probes sont confinées.

**Impact** : c'est à la fois un **écart config ↔ code** (principe XVII : « un
écart doc/config est un bug ») et un **risque de sécurité** — les proxys
publics sont des tiers non fiables, et leurs réponses sont traitées par un
processus qui a accès à l'hôte.

**Recommandation** : soit implémenter l'isolation déclarée, soit **changer la
valeur de config** pour refléter la réalité (`isolation: host`) et documenter
le risque. Ne pas laisser une config qui ment.

---

## V-64 — P0-01 du rapport 02 : la boucle d'epochs — mécanisme exact trouvé ✅ CONFIRMÉ

**Affirmation** : le mode shadow re-claim le même hostname 40 fois par epoch et
écrit un epoch toutes les 60 s.

**Vérification** (`free/qualification/sweep.py:159-168`) — **la ligne causale** :

```python
                    epoch_id=eid,
                )
                if candidate is None:
                    break
                result.claimed += 1
                if is_shadow:
                    self.store.release(candidate.hostname, status=CandidateStatus.PENDING, now=self._clock())
                    continue
```

**Le mécanisme est en trois lignes** :

1. `result.claimed += 1` — le compteur **avance** ;
2. `if is_shadow:` → `self.store.release(..., status=CandidateStatus.PENDING)`
   — le candidat est **relâché en `PENDING`**, donc **re-claimable** ;
3. `continue` — la boucle reprend **sans consommer** le candidat.

**Le candidat relâché en `PENDING` est immédiatement re-claimable par la même
passe** → le même hostname est traité en boucle, et chaque passage incrémente
`result.claimed` (donc `tested`).

**Cela explique les trois mesures** :

| Mesure | Explication |
|---|---|
| **6 769 epochs** sur le même `total=7118` | le même ensemble de candidats, re-mis en `PENDING` à chaque passe |
| `SUM(tested)` **×20,6** les tentatives réelles | `result.claimed += 1` s'exécute **à chaque tour de boucle**, pas à chaque tentative réelle |
| **1 215 epochs/jour** | un epoch est écrit toutes les ~60 s |

**Le mode `shadow` est donc une boucle d'écriture non bornée** qui gonfle
`qualification_epochs` et fausse tous les compteurs de progression.

**Recommandation** : en mode shadow, **ne pas relâcher en `PENDING`** — utiliser
un état terminal (`SKIPPED`) qui sort le candidat de la passe courante, et
n'incrémenter `claimed` qu'au moment d'une **tentative réelle**. Le
`continue` sans changement d'état est le défaut.

---

## V-65 — Validation des références `fichier:ligne` du plan ✅ (32 contrôlées)

**Pourquoi** : un plan qui cite de fausses lignes fait perdre du temps à celui
qui l'exécute. Les 32 références `fichier:ligne` de `tasks.md` ont été
vérifiées automatiquement : pour chacune, le symbole attendu doit apparaître
dans une fenêtre de ±3 lignes autour de la référence.

**Résultat : 29 exactes, 3 à préciser, 0 fausse.**

| Référence | Constat | Action |
|---|---|---|
| `free/egress_rotation.py:96` | la ligne 96 est `scheme = option.kind if …` ; **l'URL est ligne 100** | ✅ corrigée en T021 |
| `opencode.py:19127` | la ligne est `return final_in, final_out, final_cache, log_tag` | ✅ **confirme le P0** — `final_cache_write` n'est pas dans le tuple |
| `free/egress_adapters/public.py:558` | la ligne est `self._record(` ; le `dead` suit dans la fenêtre | ✅ référence valide |

**Deux confirmations au caractère près** obtenues pendant cette validation :

**1. Les credentials de proxy sont bien absents** (`free/egress_rotation.py:100`) :

```python
                "url": f"{scheme}://{host}:{int(option.port)}",
```

**Aucun champ d'authentification** dans la construction de l'URL — et
`free/egress_catalog.py` n'en a pas non plus (V-38).

**2. `final_cache_write` est bien absent du retour** (`opencode.py:19127`) :

```python
    return final_in, final_out, final_cache, log_tag
```

La variable `final_cache_write` est calculée plus haut dans la fonction et
**n'apparaît pas dans le tuple de retour** — le P0-4 du rapport 07 est exact.

**Valeur de cette validation** : les 124 tâches du plan citent des lignes
**vérifiées**. Un exécutant peut ouvrir chaque référence et trouver le code
annoncé.

---

## V-66 — `scripts/fresh_chain.py` utilise l'image upstream (usage **légitime**) ✅

**Découvert en exécutant T005** : le test `test_manifest_pinning.py` échouait
après le correctif du compose. Il vérifiait que `qmcgaw/gluetun@<digest>`
apparaissait **à la fois** dans `docker-compose.yml` **et** dans
`scripts/fresh_chain.py`.

**Vérification** — `fresh_chain.py:311` et `:363` :

```python
            [
                "docker",
                "run",
                "--rm",
                "-v",
                f"{volume}:/gluetun",
                "--entrypoint",
                "sh",
                "qmcgaw/gluetun@sha256:4af0c804…",
                "-c",
                "mkdir -p /gluetun/servers && cp /tmp/nordvpn.json …",
            ],
```

**Cet usage est légitime**, et il faut le distinguer du P0 :

| Critère | `docker-compose.yml` (P0, corrigé) | `fresh_chain.py` (légitime) |
|---|---|---|
| Nature | définition du **conteneur station** | `docker run --rm` **éphémère** |
| Rôle de l'image | **le tunnel** | **shell utilitaire** (`--entrypoint sh`) |
| Durée de vie | permanente | le temps d'un `cp`/`cat` |
| Besoin de l'API v2 | ✅ **oui** — sans elle, rotations en 404 | ❌ non — seul `sh` est utilisé |

**L'image sert de shell jetable** pour lire et écrire un fichier de manifeste
(`nordvpn.json`) dans un volume Docker. Elle ne porte aucun trafic.

**Le test protégeait donc deux choses différentes** avec le même critère :
une intention valide (« aucune image flottante ») et une contrainte devenue
fausse (« le compose doit épingler l'upstream »).

**Correctif appliqué** (`tests/test_manifest_pinning.py`) : le test
**conserve son intention** et la rend explicite —

1. **aucune image flottante** (`:latest`) nulle part ;
2. le **compose** déclare `gluetun-016:local` + `pull_policy: never` et
   **n'épingle plus l'upstream** ;
3. `fresh_chain.py` **garde** l'upstream épinglé par digest **pour son usage
   de shell éphémère**, avec un commentaire qui explique pourquoi.

**Leçon** : un test qui échoue après un correctif n'a pas toujours tort — il
faut lire **ce qu'il protège**. Ici, il protégeait une bonne intention avec
un critère obsolète ; le corriger valait mieux que le supprimer.

---

## V-67 — `official_payload.json.bak` n'est **pas** la capture 1.19.5 qu'il prétend être ✅ (prémisse du code invalidée)

**Découvert en préparant T006.** Le code
(`free/ip_registry/adapters/probe.py:82-90`) justifie l'existence du `.bak`
par un besoin d'empreinte :

```python
        self._body, self._body_raw = self._load_payload()
        # [P0 2026-11-14] The identity is chosen per egress IP among the
        # 1.18.x/1.19.x profiles, but the current payload is a 2.2.5 capture.
        # Sending a 2.2.5 body (13 tools, max_completion_tokens) with a 1.19.x
        # UA is a wire mismatch and the gate answers 429 (disguised form
        # rejection). Keep the matching 1.19.5 capture for those profiles.
        self._body_v1, self._body_raw_v1 = self._load_payload(
            str(PAYLOAD_PATH) + ".bak"
        )
```

**Le commentaire affirme que le `.bak` porte la capture 1.19.5.** Vérification
du contenu réel :

| Mesure | `official_payload.json` | `official_payload.json.bak` |
|---|---|---|
| Taille | 96 326 o | 96 326 o |
| `_source` | **`opencode 2.3.0 capture`** | **`opencode 2.3.0 capture`** |
| `_captured_at` | 2026-11-17T16:28:03Z | 2026-11-17T15:31:28Z |
| **`_body_sha256`** | **`117db5114912ffcc…`** | **`117db5114912ffcc…`** |
| `body.model` | `deepseek-v4.1-flash-free` | idem |
| `body.tools` | 13 | 13 |
| `body.max_completion_tokens` | 32 000 | 32 000 |
| **Clés divergentes** | — | **aucune** |

**Les deux fichiers sont fonctionnellement identiques** : même `_body_sha256`,
aucune clé divergente, même version déclarée (**2.3.0**), capturés à 57
minutes d'intervalle le même jour.

**Conséquence — la prémisse du code est fausse** : le `.bak` **n'est pas** la
capture 1.19.5. Il porte la **même** capture 2.3.0 que le fichier principal.

**Ce que cela implique** :

1. **Le risque décrit par le commentaire existe toujours** : une identité
   `1.19.x` accompagnée d'un corps **2.3.0** est précisément le « wire
   mismatch » qui, selon le commentaire, provoque un 429 de forme.
2. **Le correctif du 2026-11-14 n'a pas corrigé le problème** : il a ajouté un
   second chemin de chargement qui lit **le même contenu**.
3. **Le fichier `.bak` n'apporte rien** : il est identique au principal, donc
   `self._body_v1 == self._body`. La distinction `_body` / `_body_v1` est
   **sans effet**.
4. **T006 reste nécessaire**, mais pour une raison différente : il faut soit
   **fournir une vraie capture 1.19.5**, soit **retirer le profil 1.19.5** du
   registre (ce que la tâche T023 propose déjà).

**Lien avec V-15** : la vraie capture `1.19.5` a été **supprimée du disque**
(`git status` → `D free/captures/1.19.5.json`) et `verified_profiles.json:26`
la référence toujours. Le `.bak` a probablement été créé pour combler ce
manque — mais avec le mauvais contenu.

**Recommandation corrigée** :

- **ne pas** se contenter de versionner le `.bak` (il ne résout rien) ;
- **trancher** : restaurer une capture 1.19.5 authentique, **ou** retirer le
  profil 1.19.5 (`RUNTIME_FLOOR` à `(2,2,5)`), ce qui rend le `.bak` inutile
  et permet de le supprimer ;
- ajouter un test qui vérifie que `_body_sha256` **diffère** entre les deux
  chemins quand les profils diffèrent — sinon la distinction est décorative.

---

## V-59 — P0-05 du rapport 02 : du DDL dans une transaction d'ingestion ✅ CONFIRMÉ

**Affirmation** : `ingest_control_events` exécute du DDL **à l'intérieur** de
la transaction `BEGIN IMMEDIATE`.

**Vérification** (`free/ip_registry/registry_db.py:346-352`) :

```python
    try:
        conn.execute("BEGIN IMMEDIATE")
        _ensure_control_schema(conn)          # ← DDL dans la transaction
        row = conn.execute(
            "SELECT last_seq FROM control_cursor WHERE station=?",
            (int(station),),
        ).fetchone()
        last_seq = int(row["last_seq"]) if row is not None else 0
        for event in events:
```

**Le DDL (`_ensure_control_schema`) est appelé entre `BEGIN IMMEDIATE` et les
lectures/écritures d'événements.** C'est un anti-pattern : une transaction
d'écriture de données qui crée des tables.

**Impact** : le DDL prend un verrou de schéma (`SCHMA` lock) pendant toute la
durée de l'ingestion, ce qui bloque les autres écrivains plus longtemps que
nécessaire. Sur `registry.db` — qui a **6 déclarants de schéma** et des
écritures concurrentes depuis plusieurs threads — c'est un facteur de
contention.

**Statut particulier** : le rapport 02 note que le module n'est **pas branché**
(les tables `control_events`/`control_cursor` sont **absentes** de la base
live) — c'est donc un **P0 latent**, pas actif. Il se manifestera si le
module est branché.

**Recommandation** : déplacer `_ensure_control_schema(conn)` **avant** le
`BEGIN IMMEDIATE` (le schéma est idempotent), et ajouter un index
`(station, event_type)` sur `control_events` (absent aujourd'hui).
