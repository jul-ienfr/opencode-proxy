# Research — Audit global 2026-11-20

**Feature** : `024-audit-optimisation-globale`
**Sources** : `docs/audits/2026-11-20-audit-complet/` (10 rapports d'auditeurs
+ 4 documents de preuves vérifiées)
**Méthode** : 10 auditeurs indépendants en lecture seule, puis vérification
croisée des findings les plus lourds par l'auditeur principal (commandes
rejouées, mesures SQL indépendantes).

---

## 1. Le fil rouge : quatre familles de défauts, pas soixante

L'audit produit des dizaines de findings, mais ils se regroupent en **quatre
familles causales**. Traiter les findings un par un sans traiter les familles
reproduira le cycle observé depuis le 2026-11-14.

### Famille A — Un artefact lu sous une forme qui n'existe plus

Quatre bugs indépendants, trouvés par trois auditeurs différents :

| Bug | Forme attendue | Forme réelle | Impact mesuré |
|---|---|---|---|
| Probe post-rotation WARP | `capture["body"]` | `body_raw` | **3 jours sans rotation réussie** |
| Profil identité `1.19.5` | `free/captures/1.19.5.json` | fichier supprimé | 6 des 12 labels WARP servent un corps templaté |
| `wire_fp` | une seule construction | **deux** incompatibles | `classify_429` aveugle |
| `_resp_json_hors_boucle` | défini avant usage | défini après `__main__` | **502 requêtes perdues** |

**Cause commune** : les tests valident sur des **fixtures**, pas sur les
artefacts que le runtime lit réellement. Un test qui ouvrirait le fichier
configuré aurait attrapé les quatre.

**Correctif de famille** : une catégorie de tests « artefact réel » qui ouvre
le fichier effectivement référencé par la config, et un gate qui vérifie que
chaque chemin d'artefact déclaré existe sur disque.

### Famille B — L'état est déclaré, pas mesuré (principe I)

| Constat | Mesure |
|---|---|
| `free_pool.json` déclare 6 stations `usable` | l'API en voit 5/6 en `repairing` |
| Horodatages d'un **autre boot** (`762 626 s` vs `269 939 s`) | écrasés à l'identique sur les 6 |
| 58 stations fantômes dans `station_health` | 46 se déclarent `usable` avec `good_probes=0` |
| 8 045 requêtes servies par des stations **négatives** | taux de 200 de 1,2 % |
| `/health` annonce **20 stations** pour une flotte de 6 | `usable_stations=20` |
| 21 230 epochs, `SUM(tested)` ×20,6 les tentatives réelles | 6 769 epochs sur le même `total=7118` |
| 26 474 proxys `dead` **sans IP observée** (86,4 %) | indisponibilité déclarée sans mesure |
| `get_public_ip` valide sur une IP auto-déclarée 2/3 du temps | principe VII |

**Cause commune** : plusieurs composants publient leur propre vérité, et
l'horodatage lui-même n'est pas fiable (`time.monotonic()` persisté).

### Famille C — Des ressources critiques hors du dépôt

Trois dépendances non reconstructibles depuis un `git clone` :

| Ressource | État | Ce qui casse |
|---|---|---|
| `logs/compose-control-plane-{1..6}.yml` | gitignoré | image du fork, port 8001, variables `CONTROL_*` |
| `free/official_payload.json.bak` | gitignoré | `StationProbe.__init__` → `FileNotFoundError` |
| `free/captures/1.19.5.json` | supprimé | repli silencieux sur un corps templaté |

**Aggravant** : deux rapports recommandent de purger `logs/` (15 Go) — ce qui
**détruirait la configuration du control plane**.

### Famille D — Les gates ne couvrent pas ce qu'elles prétendent

| Gate | Couverture réelle |
|---|---|
| option-map (principe XII) | **1 page / 15**, 17 marqueurs / 184 contrôles — contrôle **circulaire** |
| `platform_audit --strict` | **jamais invoqué** par la CI |
| Matrice CI Windows+Linux (principe XV) | `runs-on: ubuntu-latest`, **aucune matrice** |
| `gate.ps1` local | `-k "not docker"` au lieu de `-m` → **46 tests désélectionnés** |
| Taille des modules (XIV) | aucun gate ; 3 modules créés **après** la constitution dépassent 1 500 l. |
| Clés de config mortes (XII) | 6 clés legacy en liste « informative » → **ne peuvent pas échouer** |

**Cause commune** : les gates existent mais leur **périmètre** est dérivé de
ce qu'ils vérifient (cercle vicieux), ou ils ne sont pas câblés.

---

## 2. Les cinq P0 les plus coûteux, chiffrés

| # | Défaut | Coût mesuré |
|---|---|---|
| 1 | Probe post-rotation WARP cassé | **3 jours sans rotation**, ~128 tentatives/jour × 4 recréations × 90 s de health |
| 2 | `NameError` sur `/v1/responses` non-stream | **502 requêtes perdues**, 6,2 % du chemin non-stream |
| 3 | Contournement de la porte géo | trafic sortant **en direct** alors que `require_vpn: true` |
| 4 | Double exécution du module | 2 connexions SQLite, télémétrie géo **toujours à 0** |
| 5 | Flotte sur overlays gitignorés | un `compose up` casse **5 stations sur 6** |

---

## 3. Ce que l'audit a confirmé comme sain (à ne pas casser)

Il ne faut pas reconstruire ce qui fonctionne :

| Élément | Verdict |
|---|---|
| Suite de tests | **6 379 / 6 385** passent, 8 min 48 |
| Hermétisme des tests | 0 `random` non seedé, 0 port fixe réel, 201 fichiers sous mock |
| `requests.db` vs principe XIII | **conforme sur 6 critères sur 8** |
| Principe XVI (fenêtres) | `platform_audit.py` → **0 violation / 279 fichiers** |
| Principe VIII (un seul réparateur) | séparation **cohérente** app_supervisor / ops.supervisor / reconciler |
| Sécurité injection | **aucune** injection SQL, **aucun** XSS, **aucun** secret en clair |
| Doublons d'endpoints | **aucun** (135 dashboard vs 34 monolithe, intersection vide) |
| `/metrics` | livré, 167 lignes, 9 métriques de flotte |
| `app/compaction/`, `app/protocol/` | modules propres, sous la limite |
| `free/egress_catalog.py`, `egress_sources.py`, `threat_intel.py` | domaine pur, sans I/O SQLite |

**Conséquence pour le plan** : les correctifs sont chirurgicaux, pas une
refonte. Le système a de bonnes fondations et des défauts localisés.

---

## 4. La question du langage — données mesurées

### 4.1 Ce que l'audit mesure

| Mesure | Valeur | Source |
|---|---|---|
| Plancher du proxy (hors génération) | **14-15 ms** | requêtes `jev-1.13-free` non-stream |
| Latence médiane bout en bout | **1 203 ms** | `requests.db`, 2 000 dernières |
| Temps de génération du modèle dominant | **16,6 s** (deepseek) | idem |
| Coût d'un `503` | **19,7 s** | idem |
| Coût d'un `429` | **11,5 s** | idem |
| Go déjà écrit dans le projet | **110 580 lignes** | `gluetun/` (fork `016-control-plane`) |
| Python de production | ≈ **143 000 lignes** | hors tests |
| Tests Python | **107 377 lignes** | à réécrire aussi |

### 4.2 Le calcul

Remplacer Python par Go/Rust gagne sur le **plancher** (14 ms → ~2 ms), soit
**1,2 %** d'une requête médiane. Le reste du temps est :

- la **génération du modèle** (16,6 s) — inchangée ;
- les **échecs et leurs retries** (11,5 s / 19,7 s) — c'est de l'attente
  réseau, pas du CPU.

**Un portage ne corrige aucun des cinq P0 ci-dessus.**

### 4.3 Le bloqueur technique

| Dépendance | Usages | Équivalent Go/Rust | Risque |
|---|---|---|---|
| **`curl_cffi`** (empreinte TLS JA3 exacte, `ja3_md5=1523504b…`) | **281** occurrences, 61 dans `opencode.py` | Go : `utls` · Rust : `rquest`/`boring` | **une erreur d'empreinte brûle une IP** (principe XVIII) |
| `tiktoken` | 25 | `tiktoken-go` / `tiktoken-rs` | portage direct |
| FastAPI (35 routes) | — | `chi`/`gin` · `axum` | portage direct |
| `pywebview`/`pystray` | `gui/` 484 l. | `systray` / `tray-icon` | à réécrire |

### 4.4 Le vrai gain : la concurrence

Le poste où Go apporte quelque chose n'est pas la latence, c'est la
**concurrence** : 42 787 requêtes avec un GIL, 181 `async def`, 73 points de
`asyncio.to_thread` / `run_in_executor`, et 100 flux concurrents visés.

### 4.5 Conclusion de recherche

**Ne pas porter.** Trois raisons mesurées :

1. **Le gain est de 1,2 %** sur la métrique qui compte (latence médiane).
2. **Le coût est de ~250 000 lignes** (143 k prod + 107 k tests) à réécrire,
   avec un bloqueur non négociable (`curl_cffi`/JA3).
3. **Aucun des cinq P0 n'est un problème de langage** — ce sont des défauts de
   logique, d'état et de gates.

**Ce qu'il faut faire à la place** :

- **Go/Rust là où c'est déjà le cas** : le fork Gluetun (110 580 l.) est en Go
  et fonctionne. Si un composant doit être extrait en Go, c'est **le chemin
  chaud de sélection** (`free/pool/__init__.py`, 8 362 l.) — pas tout le
  proxy.
- **Extraire, pas traduire** : le plan de découpage (17 lots pour
  `opencode.py`, 15 pour `vpn/manager.py`) rend chaque module
  **portable indépendamment** si le besoin se confirme plus tard.
- **Mesurer avant de décider** : après le découpage, si un module précis
  (sélection, budget de sondes) montre un coût CPU mesuré, il devient un
  candidat de portage **isolé** — avec un test de parité, pas une réécriture
  big-bang.

---

## 5. Ce qui a rendu le diagnostic possible (leçon de méthode)

| Erreur évitée | Comment |
|---|---|
| Croire les rapports sur parole | 17 vérifications croisées indépendantes → **1 faux positif corrigé** (`web_search`) |
| Conclure « tout est cassé » | 3 modules > 1 500 l. **hors périmètre** (`dashboard/api.py`, `config/settings.py`, `gen_golden_fixtures.py`) sont historiques |
| Confondre symptôme et cause | `AUTH_FAILED` traité par throttle, **pas** par compteur de sessions (V-34) |
| Oublier le travail perdu | l'audit du 14/11 dormait dans `git stash` — **restauré** |
| Prendre un compteur pour une mesure | `SUM(tested)` ×20,6 (V-37) |

**Règle à retenir** : un grep d'absence de consommateur doit couvrir
`opencode.py` (33 618 l.), où vivent la majorité des consommateurs runtime.
C'est l'unique faux positif de l'audit.
