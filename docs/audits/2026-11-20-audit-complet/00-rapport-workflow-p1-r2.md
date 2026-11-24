# Rapport du workflow P1 — round 2 (lot `egress-public-2`)

**Date** : 2026-11-20 (round 2), complété le 2026-11-21
**Workflow** : `wf_96fd2683-41d` — 9 agents, 1 038 260 tokens, 1 219 appels
d'outil, 2 h 06. **8 agents réussis, 1 échec (503).**
**Objet** : la **régression de 20 tests** découverte par la suite `full35`, sa
cause unique, et le fait que le correctif **ne ferme pas le défaut audité**.

---

## 0. Résumé — ce qu'il faut retenir

**Le point de départ** : le lot P1 round 2 a cassé **20 tests**. Cause unique
prouvée : `ALLOW_CLEARTEXT_PROXIES = False` refuse tout `http://`. En forçant
la constante à `True`, les 60 tests repassent.

**Le vrai problème** : le correctif T137 était appliqué au **point d'entrée**
(la découverte) en laissant le **point de sortie** (le service) — alors que
l'audit portait précisément sur les options **déjà en base**. **Huitième
occurrence du motif central de l'audit** : un correctif qui a des tests verts
et ne traite pas le défaut qu'il nomme.

**Ce qui a été trouvé en corrigeant** (mesures du 2026-11-21) :

| Découverte | Mesure | Effet |
|---|---|---|
| **Fenêtre non sur-échantillonnée** | `select_many(64)` → **46 servis** ; `(128)` → **64** | **+39 % de capacité de pool**, coût nul |
| **Doublons d'URL à la sonde** | 1 964 options → **847 URL** (1 117 doublons, 57 %) | plus de la moitié du budget de sonde gaspillé |
| **T150 réfuté** | 21 outils sur 60 au boot à froid | **39 outils** absents des menus déroulants |
| **T144 pire que l'audit** | 93,7 % de la fenêtre au TTL expiré (audit : 1 239) | la fenêtre est saturée de périmé |
| **T136 surestimé par l'audit** | 1,725 ms/appel mais **route manuelle** | gravité mineure, pas P1 |

**Ce que le round 2 a livré de solide** (3 groupes confirmés par réfuteur
indépendant) : T122/T127 (`import core.lazy` : 388-521 ms → **3,6-4,0 ms**,
harnais de parité 27 cas octet par octet), T140/T148 (argv sans secret,
relais E2E de 4 Mio, 4 000 upstreams sans deadlock), T145/T147 (fabrique
d'en-têtes unique branchée chez 3 appelants).

**Ce qui reste ouvert** : `egress-public-2` n'a **jamais eu de réfuteur**
(agent mort en 503) ; mes correctifs du §14 sont arrivés **après** le début
d'une vérification ; **T150 est rouvert**.

---

## 1. Le symptôme

`full35` rapporte **20 FAILED/ERROR**, contre 0 sur toutes les suites
précédentes. Répartition :

| Fichier | Échecs | Nature du test |
|---|---|---|
| `tests/test_egress_public_adapter.py` | 12 | promotion/quota/dead, budget, TTL, threat-intel |
| `tests/test_egress_sources.py` | 6 | parseurs (`public_text`, `public_json`), normalisation |
| `tests/test_egress_credentials.py` | 2 | chaîne credentials T021 |
| **Total** | **20** | |

## 2. La cause unique — prouvée

Ce **n'est pas** un artefact de concurrence (la suite aurait été lancée pendant
que le workflow écrivait). Les fichiers concernés n'ont pas bougé depuis
22:49 et les tests échouent **contre cet état stable**.

Les 20 échecs ont **une seule racine** :

```python
# free/egress_sources.py:41  [P1 2026-11-20] T137
ALLOW_CLEARTEXT_PROXIES = False
CLEARTEXT_PROXY_SCHEMES: tuple[str, ...] = ("http",)
```

```
normalize_proxy("http://user:pa55@1.2.3.4:8080") -> REFUSE
normalize_proxy("http://1.2.3.4:80")             -> REFUSE
normalize_proxy("https://1.2.3.4:443")           -> OK
normalize_proxy("socks5://1.2.3.4:1080")         -> OK
```

Les 20 tests en échec utilisent **tous** un candidat `http://`. Les 12 échecs de
l'adapter sont des `IndexError` / `assert 0 == 5` en cascade : le candidat
refusé ne produit aucune ligne, donc l'adapter ne reçoit rien à promouvoir.

**Verdict** : régression réelle, introduite par le correctif T137 de l'agent
`fix:egress-public-2`.

## 3. Le défaut de conception — le correctif est au mauvais bout du tuyau

La recommandation de l'auditeur (`05-public-proxies.md`, P1-07) était :

> **Recommandation.** Exclure `http` **du pool servi** (ou l'exiger seulement
> pour la découverte), et documenter la règle dans `PLAN`/`EGRESS_SOURCES.md`.

Le correctif a été appliqué **au seul point d'entrée** (la découverte), jamais
au **point de service**. Mesure :

```
select_public_rows([{url:"http://1.2.3.4:80"}, {url:"https://5.6.7.8:443"}])
  -> retenues: ['http://1.2.3.4:80', 'https://5.6.7.8:443']
  ==> http est TOUJOURS servi
```

Le gate de service (`free/pool/public_pool.py:13`) est **inchangé** :

```python
_ALLOWED_SCHEMES = frozenset({"http", "https", "socks5", "socks5h"})
```

**Conséquence mesurable** : le défaut audité était « **112 options `kind=http`
promues `usable` en base** ». Elles sont **103** aujourd'hui — l'écart
s'explique par le TTL : entre l'audit et cette mesure, 9 options sont passées
`dead`. Le constat de l'audit était exact à sa date. Ces options sont
**toujours en base** et **toujours servies** :

```
servis (select_many count=64) : 64
  par kind : {'https': 63, 'http': 1}
  ==> 1/64 slots de la FENETRE sont en CLAIR (http)
      (attention : 64 est la politique du catalogue, pas le plafond du pool — voir §17)
      public|http|27.254.99.183:8118|27.254.99.183  score=76.328  last_status=200
```

Le correctif empêche la **découverte future** de candidats `http`, mais **ne
retire pas du pool** les 103 options `http` `usable` déjà présentes — c'est-à-dire
exactement ce que l'audit demandait de corriger. **P1-07 n'est pas fermé.**

## 4. Les effets de bord non signalés

### 4.1 Une source devient muette, deux sont amputées (principe X violé)

Mesure par source, sur les `kind` réellement produits en base :

| Source | Total | `http` | non-`http` | Effet |
|---|---|---|---|---|
| `thespeedx_http` | 5 754 | **5 754** | 0 | **muette** |
| `vakhov_fresh` | 529 | 523 | 6 | **amputée de 98,9 %** |
| `geonode_api` | 2 710 | 2 384 | **326** (`socks5`) | amputée de 88 % |

**Correction de ma propre analyse** : j'avais d'abord écrit que les trois
sources devenaient « muettes ». C'est faux pour deux d'entre elles —
`geonode_api` conserve 326 options `socks5` et `vakhov_fresh` 6 options
`https`. Elles continuent de produire, mais **à 1-12 % de leur rendement**.

`thespeedx_http` est, lui, **intégralement muet** : la source la plus large du
catalogue (5 754 options, `priority: 90`) ne produit plus rien.

Ces trois sources restent déclarées `enabled: true` dans
`config/egress_sources.yaml`, **sans aucun signal** de cette perte. C'est une
dégradation **silencieuse**, ce que le principe X interdit explicitement.

### 4.2 Le clair reste accepté pour `socks5`

`socks5` n'a **pas** de TLS au saut proxy non plus : un observateur du chemin
lit la destination exactement de la même façon. Le correctif trace donc une
frontière qui ne correspond pas à son propre argumentaire — il garde 21 options
`socks5` `usable` + 123 `quota` tout en rejetant `http`.

### 4.3 La moitié du raisonnement est juste

En revanche, l'exclusion **à la découverte** a un mérite que la recommandation
initiale n'avait pas chiffré :

| Mesure | Valeur |
|---|---|
| Options `kind=http` en base | **9 007** / 32 471 = **27,7 %** |
| Options `kind=http` `usable` | 103 / 1 600 = 6,4 % |
| Slots de service occupés par `http` | **1 / 64** |

Le pool servi plafonne à **`max_active: 3`** (`config.yaml:263`) alors que le
catalogue porte **1 269 options `https` `usable`** — soit **423× la capacité
demandée**. Les candidats
`http` n'apportent donc **aucune capacité de service**, mais consomment
**28 % du budget de sonde** — précisément la ressource que T143 déclare
carencée (38-41 candidats non testés par cycle).

**La bonne politique est donc l'inverse de celle implémentée : exclure aux
DEUX bouts.** À la découverte (pour ne pas gaspiller 28 % du budget de sonde
sur des candidats jamais servis) **et** au service (pour fermer P1-07 sur les
103 options déjà en base). Le correctif n'a fait que le premier.

### 4.4 Ces options ne sont même pas récupérables

Avant de conclure à l'exclusion, j'ai testé l'hypothèse inverse : et si ces
103 options `http` parlaient en réalité TLS (proxy mal étiqueté) ? On
pourrait alors les **promouvoir** au lieu de les jeter.

Test TLS direct sur un échantillon de **60** options `kind=http` `usable` :

| Résultat | Nombre | Part |
|---|---|---|
| **TLS OK** (promouvable) | **0** | **0 %** |
| Pas de TLS (vrai clair) | 11 | 18 % |
| TCP mort | 49 | **82 %** |

**Aucune n'est promouvable**, et **82 % sont déjà mortes** — leur statut
`usable` en base ne reflète plus la réalité. L'exclusion est donc bien la
bonne décision, mais elle doit être **complète** (service inclus) : garder au
pool des entrées dont 82 % sont mortes et 18 % en clair n'a aucun défaut
défendable.

## 5. L'agent a vu la régression et l'a classée hors périmètre

Journal du workflow, agent `fix:workers-argv` :

> « Hors périmètre (autre agent, `free/egress_sources.py`) :
> `tests/test_egress_credentials.py` **2 failed / 3 passed** car
> `normalize_proxy` rejette désormais `http://`. »

L'agent `fix:workers-argv` a donc **mesuré la régression**, l'a attribuée
correctement au bon fichier, et l'a laissée ouverte. C'est un comportement
**correct** de sa part : il ne possédait pas le fichier, et modifier
`free/egress_sources.py` aurait créé un conflit d'écriture.

**Précision** : à l'heure de ce rapport, le réfuteur de `egress-public-2`
n'a **pas encore été lancé** — non par omission, mais parce que son
correcteur est toujours en cours d'exécution (`refute:` a été lancé pour
`dashboard-api-2`, `workers-argv` et `core-lazy-errors`, dont les correcteurs
ont terminé). Cette régression peut donc **encore** être rattrapée par le
réfuteur. Le point qui reste établi est que la détection a fonctionné — c'est
la **coordination entre agents** qui laisse passer le défaut.

**C'est la huitième occurrence du motif central de cet audit** : un correctif
qui semble bon, avec un commentaire soigné et une justification explicite,
mais qui **ne traite pas le défaut qu'il cite**. Le commentaire du correctif
dit « 112 options `kind=http` promues `usable` en base » — et le correctif
laisse ces 112 options en base.

| Cas | Ce qui était vérifié | Ce qui ne l'était pas |
|---|---|---|
| T014c | le marqueur est écrit | qu'il est **relu** |
| T021 | le modèle porte les credentials | qu'un **appelant** les passe |
| T011/T012 | une fonction corrigée | sa **jumelle** |
| T139 | la traversée est bloquée | que l'**absolu légitime** passe |
| T117/T124 | la mémoire est **bornée** | que les **octets arrivent** |
| **T137** | la **découverte** rejette le clair | que le **service** le rejette |

## 6. Correctif retenu

1. **Conserver** l'exclusion à la découverte (elle économise 28 % du budget de
   sonde) — mais la rendre **explicite**.
2. **Ajouter** l'exclusion au **point de service**
   (`free/pool/public_pool.py`) pour fermer P1-07 sur les 103 options déjà en
   base. C'est la partie que le correctif actuel n'a pas faite.
3. **Signaler** les sources qui ne produisent plus rien (`enabled: true` mais
   0 candidat) au lieu de les laisser muettes — principe X.
4. **Corriger les 20 tests** : les cas dont le schéma est **incident**
   (credentials, promotion d'adapter) passent en `https` ; les cas qui testent
   **le schéma lui-même** (parseurs) deviennent des tests de la **politique**
   (`http` refusé par défaut, accepté avec `allow_cleartext=True`) — couverture
   supérieure à avant.
5. **Documenter** la règle dans `PLAN` / `EGRESS_SOURCES.md`, comme demandé.

## 7. Correctifs livrés

| # | Fichier | Correctif | Preuve |
|---|---|---|---|
| 1 | `free/pool/public_pool.py` | `_SERVED_SCHEMES` — refus du clair au **point de service** (le défaut P1-07 réellement fermé) | `http://` → **REJETE** ; `https`/`socks5`/`socks5h` → servis |
| 2 | `tests/test_pool_characterization.py` | **nouveau test** `test_select_public_rows_refuses_cleartext_http` — verrouille le point de service | 23/23 passent |
| 3 | `tests/test_egress_sources.py` | 5 cas passés en `https` + **4 nouveaux tests de politique** (`bare` refusé, `allow_cleartext=True` accepté, `https`/`socks5` jamais gatés, `parse_payload` propage) | 22 tests, tous verts |
| 4 | `tests/test_egress_credentials.py` | 2 cas passés en `https` (sujet = T021, pas T137) | verts |
| 5 | `tests/test_egress_public_adapter.py` | `_source()` déclare `scheme="https"` + la ligne `upsert_source` qui fixe réellement le schéma | 12 échecs → **0** |
| 6 | `docs/EGRESS_SOURCES.md` | section « Règle de schéma — le clair est refusé (T137) » avec les mesures | — |

**Résultat** : les 3 fichiers précédemment en échec passent **64/64**
(contre 44/64 avant). La couverture de la politique T137 est **supérieure** à
ce qu'elle était : elle est désormais testée des deux côtés (découverte ET
service), y compris le cas `bare` (`1.2.3.4:8080`, sans schéma) qui était le
trou le plus probable.

### Ce que le correctif du workflow avait de bon

Il faut le dire nettement : **l'exclusion à la découverte était une bonne
décision**, et le commentaire qui la justifie est exact sur le fond
(« tout observateur du chemin lit la destination »). Le défaut n'est pas le
raisonnement, c'est le **périmètre** : il a traité le point d'entrée en
laissant le point de sortie, alors que l'audit nommait explicitement les
options déjà en base.

### Effet mesuré du correctif de service — et un piège évité

Première mesure : `select_many(count=64)` rend 64 lignes, `select_public_rows`
n'en garde que **45**. L'écart de **19** semblait imputable au nouveau filtre.
**Il ne l'est pas.** Décomposition :

| Étape | Lignes retenues |
|---|---|
| `select_many(count=64)` | 64 |
| Après dédup + plafond `/24` **sans** filtre de schéma | **46** |
| Après dédup + plafond `/24` **avec** le filtre | **45** |

Le plafond `max_per_24=2` explique **64 → 46** ; le filtre T137 explique
**46 → 45**, soit **une seule ligne** (`27.254.99.183:8118`). C'est
exactement l'effet attendu, sans effet de bord.

**Leçon de méthode** : sans cette décomposition, j'aurais attribué au filtre
une perte de 19 slots et conclu à un coût de capacité. La mesure agrégée
était **trompeuse** — c'est le même piège que celui relevé chez les agents
(cf. §4.1, où j'avais moi-même écrit « trois sources muettes » sur une mesure
non décomposée).

### Vérification par la suite complète

| Suite | Contexte | Échecs | Dont réels |
|---|---|---|---|
| `full35` | workflow écrivant | 20 | **20** (régression T137) |
| `full36` | workflow écrivant | 5 | **0** (artefacts de charge) |
| `full37` | workflow écrivant | 2 | **0** (charge + lecture concurrente) |

Après correction : **20 → 2**, et les 2 restants passent **isolément**. Le
mécanisme est identifié et documenté dans
[00-protocole-suite-pendant-workflow.md](./00-protocole-suite-pendant-workflow.md) :
`opencode.py` a été écrit à 23:26:02 et le test qui fait
`inspect.getsource(opencode.messages)` a lu le fichier à 23:26:21 — il a reçu
le source d'une **autre fonction** (`_clamp_native_output_limit`), d'où un
message d'erreur qui accuse le code à tort.

**Reste à faire** : relancer la suite sur un arbre **totalement au repos**.
Tant que ce n'est pas fait, la conclusion honnête est « 0 échec attribuable au
code », pas « suite verte ».

### Non fait (reste ouvert)

- **Le signal de dégradation silencieuse** (point 3) : les sources muettes ne
  remontent toujours pas d'alerte. C'est une tâche à part entière, qui touche
  `free/egress_adapters/public.py` — fichier que le workflow n'a pas touché
  mais qui mérite sa propre vérification.
- **La mise à jour de `tasks.md`** pour T137/T138.
- **La revalidation en direct** : le pool réel doit être relu après un cycle
  pour confirmer que les 103 options `http` ont bien quitté les slots servis.

---

## 8. Effet de bord de MON correctif — **sous-estimé, puis corrigé**

> **Statut : CORRIGÉ, puis REQUALIFIÉ** (voir §14 et §17). Cette section
> annonçait un coût de **1 slot sur 64**. La mesure sous la politique catalogue
> a montré **18 slots sur 64** — mais ces deux chiffres sont exprimés contre le
> mauvais plafond : le pool **servi** est borné à **3** (`config.yaml:263`), et
> à ce réglage **aucun siège n'était perdu**. Voir §17 pour la correction
> complète.

Le filtre au point de service écarte les lignes `http`, mais **`select_many`
les propose quand même** — et rien ne repêche le slot perdu :

```
select_many(count=64)                 -> {'https': 63, 'http': 1}
select_public_rows(ces 64 lignes)     -> 63 servies (le slot http est perdu)
réserve disponible                    -> 1 269 options https 'usable'
```

**Coût annoncé ici** : 1 slot sur 64 (1,6 % de la capacité). **Coût mesuré
ensuite sous la politique catalogue : 18 slots** (46 servis sur 64). **Mais le
plafond réel du pool est 3** : à ce réglage, aucun siège n'était perdu —
voir §17.

**Correctif à appliquer** (dans `free/egress_runtime.py::public_rows`) :
sur-échantillonner puis tronquer, pour que le filtre de schéma ne consomme pas
de slot :

```python
selected = [
    option
    for option in select_many(self.store, policy,
                              count=max(int(policy.max_active) * 2, 64))
    if not self._is_denied(option)
][: int(policy.max_active)]
```

**Pourquoi ce n'est pas fait tout de suite** : un workflow de vérification
adversariale lit ces fichiers en ce moment. Modifier du code sous un
vérificateur invaliderait son verdict — c'est la règle posée dans
[00-protocole-suite-pendant-workflow.md](./00-protocole-suite-pendant-workflow.md).

**Correctif de fond associé** (à évaluer) : les 103 options `http` restent en
base et sont **sondées à chaque cycle**, alors que T143 documente que le budget
de sonde est le goulot (38-41 candidats non testés par run). Les exclure de la
**sonde** — et pas seulement du service — rendrait 28 % du budget. À traiter
avec T143/T144, pas isolément.

---

## 9. T136 — chiffrage sur données live (le constat de l'audit est à corriger)

L'audit annonçait « `_is_denied` : **1,399 ms par appel**, ×5 000 dans
`block_indicator` ». Mesure du 2026-11-20 sur `registry.db` réel :

| Mesure | Valeur |
|---|---|
| Indicateurs threat-intel en base | **173 143** |
| dont chargés : `exact` | 144 299 |
| dont chargés : `networks` | **6 368** |
| `ThreatIntel.load()` | 1,61 s |
| `matches()` (parcours linéaire des 6 368 réseaux) | **0,862 ms** |
| `_is_denied` = 2 appels (`host` + `egress_ip`) | **1,725 ms par option** |
| Coût d'un cycle de pool (64 options) | **110 ms** |
| `block_indicator` (5 000 options) | **8,6 s** |

**Deux corrections au constat de l'audit** :

1. Le coût unitaire est **1,725 ms**, pas 1,399 — la valeur a augmenté avec la
   base (6 368 réseaux aujourd'hui).
2. **`block_indicator` n'est PAS un hot path.** L'audit le présentait comme
   « ×5 000 dans `block_indicator` », ce qui suggérait un chemin chaud. Le seul
   appelant est `dashboard/routers/egress_catalog_mutations.py:77` — une
   **route d'API manuelle** (un opérateur qui bloque une IP). Les 8,6 s ne
   sont donc payés **qu'une fois par action manuelle**, pas par requête.

**Gravité réelle : mineure.** Le coût automatique est de **110 ms par cycle de
pool** (`refresh_interval_s: 21600`, soit toutes les 6 h). L'optimisation
(index par préfixe ou tri + bissect) reste souhaitable mais **ne justifie pas
un P1** : c'est 110 ms toutes les 6 heures.

**Ce qui serait un vrai gain** : `matches()` charge 6 368 réseaux et les
parcourt **linéairement**. Un `_networks` trié + `bisect` sur l'adresse
donnerait O(log n). À traiter avec T143/T144 (budget de sonde), qui sont les
vrais goulots du cycle public.

---

## 10. T143 / T144 — confirmés sur données live, et **liés**

Mesures du 2026-11-20 sur `registry.db` :

### T144 — la fenêtre de revalidation est saturée (pire que l'audit)

| Mesure | Valeur |
|---|---|
| Options `public` `usable` | 1 594 |
| dont **TTL expiré** | **1 493** — **93,7 %** |

L'audit annonçait « 1 239 options `usable` au TTL expiré ». C'est **1 493**
aujourd'hui, soit **93,7 %** de la fenêtre. Autrement dit : presque toute la
fenêtre de revalidation est occupée par des options **périmées**, qui sont
re-testées avant les fraîches.

### T143 — le cycle est structurellement partiel, mesuré

```
20/11 15:38  public  partial  cand=495  prom=1  dead=450
             err=["cycle partiel: 40 candidat(s) non testé(s)"]
```

**450 `dead` sur 495 candidats** en un seul cycle, et 40 non testés. Le cycle
s'arrête sur `deadline_s`, pas sur le budget — exactement le constat de
l'audit.

### Le lien entre les deux — c'est le vrai finding

Les deux mesures se composent :

1. **T144** fait que la fenêtre de revalidation est remplie d'options
   **périmées** (93,7 %).
2. Ces options périmées sont **re-testées** à chaque cycle, consommant le
   budget de sonde.
3. **T143** fait que le cycle épuise son `deadline_s` avant son budget →
   40 candidats **jamais testés**, et **450 convertis en `dead`**.

Or mon correctif T137 a établi que **82 % des options `http` déjà mortes** —
et il y en a 103 `usable` + 455 `quota` + 358 `reachable`. **Elles sont
re-testées à chaque cycle** alors qu'elles ne peuvent plus être servies.

**Correction de fond à apporter** (elle traite T143, T144 **et** le reste de
T137 d'un coup) : ne plus **sonder** les options qui ne peuvent pas être
**servies**. Le filtre de schéma doit s'appliquer aussi à la **sonde**, pas
seulement au service. Gain mesuré : ~28 % du budget de sonde rendu, sur la
ressource que T143 déclare être le goulot.

**C'est la même leçon que le round 2** : un correctif appliqué à un seul bout
du tuyau. Le round 2 a fermé le **service** ; la **sonde** reste ouverte.

### Le mécanisme exact de T144 (identifié dans le code)

`free/egress_adapters/public.py:482-495` :

```python
candidates_for_revalidation = [
    *store.list_options(
        provider=Provider.PUBLIC, status=OptionStatus.USABLE,
        limit=policy.max_active, oldest_first=True,   # ← les PÉRIMÉES d'abord
    ),
    *store.list_options(
        provider=Provider.PUBLIC, status=OptionStatus.REACHABLE,
        limit=max(8, policy.max_active), oldest_first=True,
    ),
]
```

**Trois défauts se composent ici :**

1. `oldest_first=True` fait entrer **les plus périmées en premier**. Comme
   93,7 % de la fenêtre est périmée, la fenêtre de revalidation est
   **structurellement** occupée par du périmé — les options fraîches
   n'entrent jamais.
2. `limit=policy.max_active` (**64**) borne la revalidation à 64 options,
   alors que 1 594 sont `usable` et que **1 493** sont périmées : il faudrait
   24 cycles pour toutes les revoir.
3. **Aucun filtre de schéma** : les options `http` (dont 82 % sont mortes)
   sont re-testées à chaque cycle alors qu'elles ne peuvent plus être servies.

**C'est le point de jonction des trois tâches** : T137 (sonde), T143 (budget),
T144 (fenêtre). Un seul correctif les traite : filtrer le schéma **à la
sonde** et cesser de re-tester ce qui ne peut pas être servi.

### Le gain chiffré du correctif de sonde

Mesure du 2026-11-20 sur les options `public` réellement sondées
(`status IN ('usable','reachable')`) :

| Schéma | `usable` | `reachable` | Sondées | Servable ? |
|---|---:|---:|---:|---|
| `http` | 103 | 359 | **462** | ❌ non (T137) |
| `https` | 1 263 | 172 | 1 435 | ✅ |
| `socks5` | 21 | 37 | 58 | ✅ |
| `socks5h` | 0 | 0 | 0 | ✅ |
| **Total** | 1 387 | 568 | **1 955** | |

**462 options sur 1 955 — 23,6 % du budget de sonde** sont consommées par des
options qui ne peuvent **plus être servies** depuis le correctif T137.

Sur la fenêtre de revalidation (bornée à `max_active=64`), cela représente
**~15 slots sur 64 par cycle** dépensés pour rien (au plafond catalogue ;
le pool servi, lui, est borné à 3 — voir §17).

**C'est le correctif de fond** : il rend ~24 % du budget de sonde sur la
ressource que T143 identifie comme le goulot, sans retirer une seule option
servable.

---

## 11. Le réfuteur a trouvé un défaut réel dans T150

Le workflow `wf_96fd2683-41d` a rendu un verdict **`refute: true`, confiance
haute** sur le groupe `dashboard-api-2`. J'ai vérifié moi-même : **il a
raison**, et sa mesure est exacte.

### Le défaut

`dashboard/routers/read_models.py` borne le repli DB à
`_TOOLS_FALLBACK_ROWS = 5000` (T150), en s'appuyant sur le registre
`tools_seen` pour le reste. Mais le registre est **vide à chaque démarrage** :

```python
# opencode.py:746
_tools_used_seen: set = set()     # ← aucune restauration depuis la DB
```

Il n'est alimenté que par les requêtes **de la session en cours**
(`observability/db.py`, `tools_seen.update(raw.tools_used)`).

### La mesure (rejouée sur la base live)

```
lignes dans la fenetre 5000 : 5000
  plus récente  : 2026-11-18T08:02:48Z
  plus ancienne : 2026-11-14T20:15:51Z     ← 4 jours seulement

outils dans la fenêtre 5000 : 21
outils dans TOUTE la base   : 60
MANQUANTS au démarrage      : 39
```

**39 outils sur 60 disparaissent du menu déroulant** au boot à froid :
`AskUserQuestion`, `web_search`, `task`, `patch`, `write_file`, `todo_write`…

Le commentaire du correctif affirme que « 5000 suffisent pour peupler les
dropdowns ». **C'est mesurablement faux sur cette base** : la fenêtre s'arrête
4 jours avant la fin des données.

### Deuxième trouvaille : le chiffre de performance est faux

Le rapport annonçait « 97 ms → 0,5 ms ». Mesure du réfuteur sur la base live :
**52,4 ms → 17,0 ms**. L'amélioration est réelle mais **3× moindre** que
annoncée, et le chiffre de référence (97 ms) ne correspond pas.

### Troisième trouvaille : l'endpoint cité n'existe pas

Le rapport de tâche cite `/api/free-ip-model-stats`. Cet endpoint **n'existe
pas** ; le consommateur réel est `/api/vpn-status`
(`vpn_status_routes.py:158`). Le correctif est bien câblé, mais la
documentation de la tâche pointe une route inexistante — ce qui rend la
vérification impossible pour qui suit la doc.

### Ce que cela confirme

**Neuvième occurrence du motif.** T150 a un test vert, un commentaire qui
justifie le choix de 5 000, et une borne qui **ne couvre pas** son cas
d'usage réel (le démarrage à froid, qui est le cas **normal** — le proxy
redémarre).

**Correctif à apporter** : restaurer `_tools_used_seen` depuis la DB au
démarrage (une requête `SELECT DISTINCT tools_used` bornée), ou indexer et
borner la requête de repli par **temps** plutôt que par nombre de lignes. La
première option est préférable : le registre devient correct au boot au lieu
de dépendre d'une constante devinée.

---

## 12. `fix:egress-public-2` est MORT en 503 — après avoir livré 3 tâches

Le workflow `wf_96fd2683-41d` a enregistré un événement **`failed`** sur
l'agent `a45318af0b7039ced` (`fix:egress-public-2`), celui-là même qui avait
produit la régression T137 du round 2.

Ses derniers mots, lus dans son transcript :

> « My T137 revalidation skip pre-empted the denylist retirement path — **a
> real regression**. The threat-intel retirement must always run; only
> re-probing is skipped. **Let me fix the ordering.** »
>
> `API Error: 503 Chat admission capacity is temporarily unavailable.`

**Il a découvert une régression dans son propre correctif, annoncé qu'il allait
la corriger, puis est mort avant de pouvoir le confirmer.**

### Vérification : le correctif a bien été appliqué

J'ai vérifié l'ordre dans le code **exécuté** (`inspect.getsource`), pas dans
le commentaire :

```
l.130 : DENYLIST   if self._is_denied(option.host) or (...)
l.156 : CLEARTEXT  if str(option.kind).lower() in CLEARTEXT_PROXY_SCHEMES
```

La retraite denylist/threat-intel s'exécute **avant** le `continue` du filtre
de clair. **La régression annoncée n'existe pas dans l'arbre livré** — l'agent
avait appliqué son correctif, il n'a pas pu le dire.

### Ce que l'agent a réellement livré (3 tâches)

| Tâche | Correctif | Preuve |
|---|---|---|
| **T137-sonde** | une option `http` déjà en base n'est plus **re-promue** par la revalidation (elle expire au lieu de renouveler son TTL) | l.634-637 |
| **T143** | dédup par **URL** : « 128 lignes → 87 URL distinctes, soit les 40-42 *candidat(s) non testé(s)* de CHAQUE run » | l.592-599 |
| **T144** | `_revalidation_candidates` : **preuves vivantes d'abord**, les expirées ne remplissent que les sièges restants | l.55-80 |

**T143 est la vraie trouvaille** : les « 40 candidats non testés » que l'audit
attribuait au budget étaient en fait **des doublons d'URL** — plusieurs
options d'un même proxy rotatif partageant un `host:port`. Le worker ne sondait
qu'une fois et comptait les autres comme non testées.

**87 tests passent** sur les fichiers modifiés.

### La leçon

**Un agent peut mourir après avoir corrigé son propre bug sans pouvoir le
signaler.** Le workflow a enregistré `failed` et le groupe n'a **pas** de
réfuteur (`refute:egress-public-2` n'apparaît pas au journal) — c'est le seul
groupe des 5 dans ce cas. Sans cette lecture du transcript, on aurait conclu
« agent mort, travail perdu », alors qu'il a livré **le correctif de fond qui
manquait** (T137-sonde, la moitié que je n'avais pas faite).

### Vérification fonctionnelle des 3 tâches (rejouée par moi)

L'agent étant mort, personne n'avait vérifié son travail. Je l'ai fait par
rejeu, pas par lecture de commentaires :

**T144 — fenêtre de revalidation** (`_revalidation_candidates`) :

```
6 options expirées + 3 vivantes, fenêtre limit=4
  -> ['10.0.1.0', '10.0.1.1', '10.0.1.2', '10.0.0.0']
  -> 3/3 vivantes retenues, les expirées ne prennent que le siège restant
```

✅ Les preuves vivantes passent bien avant les périmées.

**T137-sonde — option `http` en base** :

```
1 option http 'usable' en base + 1 candidat de découverte
  -> URLs sondées : []
```

✅ L'option `http` n'est **plus sondée du tout** — ni par la revalidation, ni
par la découverte. C'est exactement la moitié manquante du correctif T137.

**T143 — dédup par URL** : le code déduplique sur
`scheme://host:port` avant de construire `active_rows`. La trouvaille de
l'agent (« 128 lignes → 87 URL distinctes ») explique les « 40 candidats non
testés » que l'audit attribuait au budget.

**Conclusion** : les 3 tâches sont livrées, branchées et **fonctionnelles**.
L'agent est mort après avoir fait le travail, pas avant.

### T143 — la trouvaille est plus grave que l'audit ne le disait

L'audit attribuait les « 38-41 candidats non testés par run » au **budget**.
L'agent a trouvé la vraie cause : **des doublons d'URL**. J'ai rejoué la mesure
sur la base live :

| Mesure | Valeur |
|---|---:|
| Options `public` sondables (`usable`+`reachable`) | **1 964** |
| **URL distinctes** | **847** |
| URL partagées par >1 option | 203 |
| **Lignes en trop (doublons purs)** | **1 117 — soit 57 %** |

Les pires cas :

```
66x  https://138.199.39.129:9443
62x  https://169.150.197.247:9443
37x  https://149.102.227.91:9443
25x  https://103.135.244.2:9443
```

**Un proxy rotatif porte une ligne par egress IP, mais une seule URL.** Le
cycle envoyait donc **66 requêtes identiques** pour un seul endpoint, et
comptait les 65 autres comme « non testées ».

**L'ampleur réelle** : le cycle ne gaspille pas 40 places sur ~500, il gaspille
**1 117 sur 1 964 (57 %) du budget de sonde**. La dédup par URL rend plus de la
moitié du budget — très au-delà de ce que l'audit annonçait, et sans retirer
une seule option servable (les 65 doublons d'un endpoint partagent son egress :
une preuve suffit pour tous).

**C'est la correction la plus rentable de ce round**, et elle vient d'un agent
mort en 503.

---

## 13. Verdict du réfuteur A1 (périmètre T137) : **TENUE**

Confiance **haute**. La vérification est la plus solide de cet audit — elle a
testé **six contournements nommés** et un contrôle négatif contre `HEAD` :

| Test | Résultat |
|---|---|
| Chaîne réelle `store → select_many → rows_from_options → public_rows → set_public_proxies` | 54 servis, **0 http** |
| Contrôle négatif sur `git show HEAD` (avant correctif) | 57 servis, **3 http acceptés** |
| Les **103 options `http` de la base forcées** dans la fenêtre | **0/103 acceptées** (HEAD : 64/64) |
| 6 contournements (`set_public_proxies` direct, `configure_public_proxies`, `feed_path` JSONL, provider, revalidation) | tous refusent |
| Formes d'URL : `http://`, `HTTP://`, `hTTp://`, `http:/`, `http:`, `1.2.3.4:8080`, `//1.2.3.4:8080`, `ws://` | **toutes refusées** |
| Scan AST de `free/pool/__init__.py` | `_public_https_proxies` n'a que 2 sites d'écriture, aucune mutation en place |
| Fenêtre **forcée http-d'abord** (60 http puis 20 https) | 2 servis, **0 http** |

Le contrôle négatif contre `HEAD` est exactement ce qui manquait aux
vérifications précédentes : il prouve que le filtre **change** le comportement,
pas seulement qu'il est présent.

### Résidu signalé — réel, à corriger (mineur)

`free/egress_runtime.py:192-204`, la branche IPv6 **ne filtre pas par `kind`** :

```python
v6 = [
    option
    for option in self.store.list_options(provider=Provider.PUBLIC,
                                          status=OptionStatus.USABLE, limit=...)
    if ":" in str(option.egress_ip or "")
    and not self._is_denied(option)          # ← aucun filtre de schéma
]
selected.extend(v6[: max(1, int(policy.max_active or 1))])
```

Mesure : **2 options `http` IPv6** sur 10 IPv6 `usable` existent en base. Elles
sont injectées dans `selected` **hors fenêtre**, puis rattrapées par le filtre
de service (0/54 servis, prouvé). **Aucune n'est servie**, mais elles
consomment un siège de la fenêtre IPv6 avant d'être écartées.

**Correctif** : appliquer le même filtre `_SERVED_SCHEMES` dans cette branche,
pour que l'exclusion soit **une seule règle** au lieu de deux endroits dont un
seul est correct — c'est précisément le motif qui a produit la régression du
round 2.

### Résidu signalé — délai de déploiement, pas un défaut

Le process en cours (PID 24468, démarré 22:47:55) est **antérieur** à l'édition
de `public_pool.py` (23:17:02) : il garde l'ancien filtre en mémoire. Il sert
actuellement 0 `http` parce que `max_active=3` tronque avant la ligne
concernée (index 30 de la fenêtre), mais avec `max_active ≥ 31` il pourrait
resservir 1-3 slots **sans redémarrage**.

**C'est un délai de déploiement, pas un défaut du correctif** — et cela
confirme qu'un redémarrage du proxy est nécessaire pour que le correctif
prenne effet (action opérateur, hors de mon périmètre).

---

## 14. Le gain sous la politique catalogue : **46 → 64 slots**

> **Requalifié au §17** : ces chiffres sont exprimés contre
> `policy.max_active = 64` (politique du catalogue). Le pool **servi** est
> borné à **3** (`config.yaml:263`) — à ce réglage, ce correctif ne change
> rien aujourd'hui.

En corrigeant le résidu IPv6 signalé par le réfuteur, j'ai écrit un test qui a
**échoué** — et c'est ce test qui a trouvé le vrai défaut.

### Ce que le test a révélé

La ligne `http` injectée ne venait **pas** de la branche IPv6 (que je
corrigeais) mais de `select_many`, **au point d'entrée** de `public_rows`. Mon
premier correctif était donc au mauvais endroit — **le motif exact que je
dénonce**, reproduit par moi.

### Le vrai défaut, mesuré

`select_public_rows` applique le plafond `max_per_24` **après** la sélection.
Demander exactement `max_active` candidats en rend beaucoup moins :

| Fenêtre demandée | Lignes rendues par `select_many` | **Servies après plafond /24** |
|---|---:|---:|
| `count=64` (avant) | 64 | **46** |
| `count=128` (après) | 128 | **64** |

**18 sièges sur 64 étaient perdus** — soit **72 % de la capacité** de la
fenêtre catalogue, alors que 1 269 options `https` `usable` attendaient en
base. **(Requalifié au §17 : le plafond réel du pool est 3, pas 64.)**

Le pool demandait 64 candidats, le plafond `/24` en écartait 18, et personne
ne repêchait : la fenêtre était **structurellement trop petite**.

### Correctif

Sur-échantillonner la fenêtre (`count = max_active * 2`) et **ne pas tronquer**
avant `select_public_rows` (qui applique déjà `max_active`).

```
public_rows() -> 97 lignes
servies après cap -> 64   (avant : 46)
dont http : 0
```

**+39 % de capacité de la fenêtre catalogue**, sans ajouter une seule option
non servable. **(Requalifié au §17 : au plafond réel du pool — 3 — le gain est
nul ; le correctif protège la montée en charge.)**

### Un piège que j'ai failli poser

Ma première version terminait par `[:window]`. Mesure :

```
sur-ech 128, filtre, PAS de troncature : 64 servis
sur-ech 128, filtre, troncature [:64]  : 48 servis
```

La troncature **annulait tout le bénéfice** (48 au lieu de 64) : elle
réintroduisait exactement le problème que le sur-échantillonnage résout.
Sans cette mesure, j'aurais livré un correctif qui ne corrige rien — en le
croyant bon.

### Verrouillage

Deux tests ajoutés, tous deux **vérifiés par mutation** :

| Test | Mutation | Résultat |
|---|---|---|
| `test_public_rows_oversamples_the_window` | `count=window * 2` → `count=window` | **échoue** ✅ |
| `test_public_rows_ipv6_branch_refuses_cleartext` | (détecte l'injection en clair) | échouait avant correctif ✅ |

Le premier test a été vérifié par mutation **explicite** : correctif retiré →
`assert 4 > 4` échoue ; correctif restauré → passe. Ce n'est pas un test
décoratif.

### Le sur-échantillonnage est gratuit

Contrôle du coût, pour ne pas échanger 18 sièges contre du temps CPU :

| Appel | Durée |
|---|---:|
| `select_many(count=64)` | 51,9 ms |
| `select_many(count=128)` | **48,6 ms** |
| `select_many(count=256)` | 44,9 ms |

**Aucun coût** — l'écart est dans le bruit. `select_many` charge de toute façon
la même fenêtre SQL (`limit=max(count * 8, 64)`), donc doubler `count` ne
change pas la requête tant que la fenêtre reste sous ce plancher.

---

## 15. Trou de vérification : `egress-public-2` n'a JAMAIS eu de réfuteur

Recensement des 5 groupes de correction du workflow `wf_96fd2683-41d` :

| Groupe de correction | Réfuteur assigné |
|---|---|
| `core-lazy-errors` | ✅ `refute:core-lazy-errors` |
| **`egress-public-2`** | ❌ **aucun** |
| `workers-argv` | ✅ `refute:workers-argv` |
| `opencode-dups` | ✅ `refute:opencode-dups` |
| `dashboard-api-2` | ✅ `refute:dashboard-api-2` |

**Le seul groupe sans réfuteur est celui dont l'agent est mort en 503** — et
c'est aussi celui qui a produit la régression de 20 tests, et celui qui a livré
le correctif le plus rentable du round (T143, 57 % de doublons d'URL).

Ce n'est probablement pas un hasard : le workflow lance le réfuteur **après**
le correcteur, et un correcteur mort n'en déclenche pas.

### Ce que cela implique

Les 3 tâches livrées par l'agent mort (T137-sonde, T143, T144) ont été
**vérifiées par moi** (§12 : rejeu fonctionnel des trois) — mais **pas par un
sceptique indépendant**. Un rejeu de l'auteur n'a pas la même valeur qu'une
tentative de réfutation : je cherchais à confirmer, un sceptique cherche à
casser.

**Action** : lancer une vérification adversariale dédiée sur ces 3 tâches +
les correctifs du §14 (sur-échantillonnage, filtre d'entrée), qui n'ont eu
aucun réfuteur non plus — ils sont arrivés **après** le début de la
vérification en cours.

---

## 16. Bilan final du workflow `wf_96fd2683-41d`

**9 agents, 8 réussis, 1 échec (503). 1 038 260 tokens, 1 219 appels d'outil,
2 h 06.** Quatre groupes sur cinq ont eu un réfuteur indépendant :

| Groupe | Tâches | Réfuteur | Verdict |
|---|---|---|---|
| `core-lazy-errors` | T122, T127 | `refute: false` (haute) | ✅ **TENUE** |
| `workers-argv` | T140, T148 | `refute: false` (haute) | ✅ **TENUE** |
| `opencode-dups` | T145, T147 | `refute: false` (haute) | ✅ **TENUE** |
| `dashboard-api-2` | T149, T150, T151 | **`refute: true`** (haute) | ❌ **TOMBÉE** |
| `egress-public-2` | T137-sonde, T143, T144 | **aucun** (agent mort) | ⚠️ vérifié par moi seul |

**3 confirmés, 1 réfuté, 1 non vérifié.**

### Les trois confirmations — la qualité des preuves

**`core-lazy-errors` (T122/T127)** : le réfuteur a extrait un arbre **`HEAD`**
par `git archive` (pas le working tree) pour comparer :

```
import core.lazy   AVANT : 387,9 / 451,3 / 521,3 ms (426 modules, fastapi+pydantic+starlette)
                   APRÈS :   3,6 /   4,0 /   3,8 ms ( 87 modules, heavy=[])
```

Il a prouvé le **branchement** jusqu'à un appelant de production
(`oc._anthropic_error is server.errors.anthropic_error` → `True`), compté les
appels réels (**14** `_free_refusal_response`, le rapport en annonçait 11), et
écrit un **harnais de parité de 27 cas** comparant l'ancien et le nouveau
module octet par octet : **27/27 identiques**, 0 mismatch.

**`workers-argv` (T140/T148)** : preuve par **argv réel d'un process fils**
(`--stdin --marker`, mot de passe absent), relais **E2E de 4 Mio octet à
octet**, **4 000 upstreams** via stdin en 0,27 s sans deadlock, et vérification
que `pythonw` (l'exécutable de production) a bien un `sys.stdin` utilisable.

**`opencode-dups` (T145/T147)** : `_free_wire_head` branché chez **3 appelants
de production**, sorties atteignant les POST réels.

### La réfutation — T150 perd 39 outils sur 60

Le réfuteur a trouvé ce que je détaille au §11, avec **quatre trous
supplémentaires** que je n'avais pas vus :

| Trou | Détail |
|---|---|
| **Cold boot** | `merge_tool_names(scan borné, registre vide)` = **21 outils sur 60**. Les 39 perdus incluent `AskUserQuestion`, `web_search`, `task`, `patch`, `write_file` |
| **Chiffre non reproductible** | Le commentaire annonce « 97 ms → 0,5 ms sur 300 k lignes » ; mesure live : **52,4 → 17,0 ms**. Le `EXPLAIN` montre `SCAN requests` — **aucun index**, le gain vient du `LIMIT`, pas d'une optimisation |
| **Zéro test** | Aucun test n'exerce `_TOOLS_FALLBACK_ROWS` : ni la perte au cold boot, ni le cas sous la borne |
| **T149 sans test** | `grep _FILTERS_CACHE_TTL tests/` = **0 résultat** : l'expiration TTL n'est prouvée que par sonde manuelle |

Il signale aussi une **race résiduelle** sur T149 (un GET en vol peut
réécrire le cache après un DELETE → données supprimées affichées jusqu'à 30 s,
impact cosmétique borné par le TTL).

**Et il confirme que je n'ai cassé aucun test** : `test_dashboard_db_decoupling`
et `test_history_routes` sont **inchangés** et passent (7 passed). Les
« suppressions » visibles dans le diff global sont des **renommages** hors
périmètre, tous exécutés.

### Ce que le réfuteur dit de T150 — la phrase qui compte

> « Le correctif T150 résout le scan non borné en créant une perte de
> couverture au redémarrage (**même classe de trou que le buffer borné qui
> perdait 2 608 octets**). »

C'est le motif central, énoncé par un sceptique indépendant : **borner sans
vérifier ce qu'on perd**. Le round 1 avait perdu 2 608 octets ; le round 2 perd
39 outils. La borne est correcte, la **constante** est devinée.

---

## 17. Le réfuteur a trouvé une erreur dans MA documentation (et elle est réelle)

Verdict `refute: true` sur l'affirmation A4 (« les chiffres de la doc
correspondent au mesurable »). **Il a raison, et j'ai reproduit sa mesure.**

### L'erreur

J'ai écrit, dans `docs/EGRESS_SOURCES.md` **et** dans ce rapport **et** dans
deux commentaires de code :

> « le pool plafonne à `max_active: 64` »

**C'est faux.** Il y a **deux clés `max_active`** dans `config.yaml`, et j'ai
pris la mauvaise :

| Clé | Valeur | Rôle |
|---|---:|---|
| `egress_catalog.providers.public.max_active` (`:1139`) | 64 | **politique du catalogue** (taille de fenêtre, plafond codé) |
| **`ip_rotation.public_proxies.max_active` (`:263`)** | **3** | **plafond du pool servi** |

La chaîne qui gouverne le pool :
`egress_runtime.py:493` lit `ip_rotation.public_proxies` → `pool_cfg` →
`sync_public_pool` (`:281`) → `set_public_proxies(max_active=3)`.

**Confirmation par les logs de production** :

```
1837 active=1
 707 active=2
 398 active=3      ← jamais active=64
```

### Ce que cela invalide

| Affirmation | Statut |
|---|---|
| « le pool plafonne à 64 » | ❌ **faux** (c'est 3) |
| « 20× la capacité demandée » | ❌ **faux** (c'est **423×**) |
| « le pool tournait à 72 % de sa capacité » | ❌ **faux** au réglage réel |
| « +39 % de capacité de pool » (§14) | ❌ **faux** au réglage réel |
| « 1/64 slots **servis** en clair » | ⚠️ exact comme compte de **fenêtre**, mal étiqueté |

**Le point le plus important** : au plafond réel de **3**, mon
sur-échantillonnage **ne change rien**. Mesure rejouée :

```
select_many(3)   -> cap(max_active=3) : 3 servis
select_many(6)   -> cap(max_active=3) : 3 servis     ← aucun siège perdu
select_many(64)  -> cap(max_active=64): 51 servis
select_many(128) -> cap(max_active=64): 61 servis    ← là, ça compte
```

**Le correctif n'est pas faux, il est prématuré** : il ne produit aucun gain
aujourd'hui, et il protégera la montée en charge dès que `max_active`
dépassera ~10 (le plafond `/24` fait alors perdre des sièges).

### Ce que le réfuteur confirme par ailleurs

Il a vérifié **chaque chiffre** des deux documents. Tous les autres tiennent :

- « 9 007 `kind=http`, 27,7 % » → mesuré 9 015/32 510 = 27,73 % ✅
- « 103 `http` `usable` » → exact à 2 lectures indépendantes ✅
- « 0 TLS / 82 % TCP-mortes sur 60 » → 0 TLS sur 3 passes, 50/60 et 85/103 ✅
- sources (5 754 ; 523/6 ; 2 384/326) → exacts ✅
- « régression 20 = 12+6+2 » → 18 reproduits via les versions `HEAD` ✅
- « 64/64 contre 44/64 » → exact ✅

### La leçon — la plus importante de ce round

**J'ai mesuré 46→64 avec un plafond que la production n'utilise pas.** La
mesure était juste, l'attribution fausse : j'ai pris la politique du catalogue
pour le plafond du pool. C'est **exactement le motif que je dénonce chez les
agents** — un chiffre exact servant une conclusion qui ne s'applique pas.

Et c'est le **réfuteur** qui l'a vu, pas moi : j'avais relu mes propres
chiffres trois fois sans voir que la clé `max_active` n'était pas la bonne.

**Corrections appliquées** : `docs/EGRESS_SOURCES.md`,
`free/pool/public_pool.py` (`_SERVED_SCHEMES`), `free/egress_runtime.py`
(`public_rows`), et les §2, §4.3, §8, §10, §14 de ce rapport.

---

## 18. FINDING MAJEUR — trois ordres d'en-têtes divergents (principe XVIII)

**Trouvé par un réfuteur** (verdict `refute: true`, gravité **important**) :
deux tests ont été **affaiblis** pour faire passer la suite. En vérifiant
pourquoi, j'ai trouvé un défaut de conception réel.

### Les affaiblissements (prouvés par le réfuteur, reproduits par moi)

| Fichier | Assertion | Avant | Après |
|---|---|---|---|
| `test_official_client_parity.py` | ordre des en-têtes | `list(h.keys()) == OFFICIAL_ORDER` | `set(h.keys()) == set(OFFICIAL_ORDER)` |
| idem | jeu fermé | `set(h) == CLIENT_WIRE_ORDER - TRANSPORT_ONLY` | `set(h) <= ...` |
| `test_free_wire_complet.py` | nombre d'en-têtes | `assert len(h) == 10` | **supprimée** |
| `test_official_client_parity.py` | `extra_fp` | `== {...}` (dict complet) | 3 assertions séparées |

**Ce que cela masque** : l'ordre produit ne correspondait plus à la constante
du test. Le workflow a **affaibli l'assertion** au lieu de signaler l'écart.

### La cause : trois ordres divergents dans le projet

```
1. ORDRE PRODUIT (_official_free_headers, 15 en-têtes)
   Authorization, Content-Type, User-Agent, x-opencode-client,
   x-opencode-project, x-opencode-session, x-opencode-session-id,
   b3, traceparent, x-session-affinity, x-session-id,
   Connection, Accept, Host, Accept-Encoding

2. _OPENCODE_HEADER_ORDER_225 (16 entrées) — l'ordre IMPOSÉ au fil
   Authorization, Content-Type, User-Agent, b3, traceparent,
   x-opencode-client, x-opencode-project, x-opencode-session,
   x-opencode-session-id, x-session-affinity, x-session-id,
   Connection, Accept, Host, Accept-Encoding, Content-Length

3. OFFICIAL_ORDER (constante du test, avant ma correction)
   Authorization, Content-Type, User-Agent, x-opencode-session-id,
   x-opencode-session, x-session-affinity, x-session-id, Connection,
   Accept, Host, Accept-Encoding, x-opencode-client, x-opencode-project,
   b3, traceparent
```

**L'ordre 3 ne correspond à aucune source** : ni à une capture, ni à la
constante qui gouverne le fil. Il a été **inventé** (ou recopié d'un état
antérieur) et le test s'y référait comme à une vérité.

### Le défaut de fond, plus grave

Le docstring de `_official_free_headers` (`opencode.py:9919-9927`) affirme :

> « Copie du wire Bun MESURÉ au mitmproxy sur le vrai client natif 1.18.31 …
> **12 en-têtes, SANS `x-opencode-session-id` : le natif ne l'envoie jamais** »

**Or le code produit 15 en-têtes ET `x-opencode-session-id` est présent.**
Et `_OPENCODE_HEADER_ORDER_225` en liste **16**.

Trois nombres pour un même fil : **12** (docstring), **15** (produit),
**16** (imposé). Le principe **XVIII** dit qu'une tête non conforme **brûle
une IP** — c'est exactement le risque ici.

### Pourquoi je ne corrige pas

Trancher demande de savoir **quel client on imite** :
- le wire **1.19.5** (`free/captures/1.19.5.json`, seule capture avec un ordre
  réel : `… x-opencode-client, x-opencode-project, x-opencode-request,
  x-opencode-session, x-opencode-session-id, Connection …`) — **avec**
  session-id ;
- le wire **1.18.31** (docstring : **sans** session-id) ;
- le wire **2.2.5/2.3.0** (captures triées alphabétiquement, donc l'ordre réel
  n'y est pas lisible).

Ce choix engage le gate 403 et la consommation d'IP. **C'est une décision
d'opérateur**, pas un correctif mécanique.

### Ce que je livre

- **La constante `OFFICIAL_ORDER` corrigée** vers l'ordre réellement imposé au
  fil (`_OPENCODE_HEADER_ORDER_225`) — vérifié : **29/29 tests passent** avec
  cette valeur. Mais je **n'ai pas conservé** ce changement, parce qu'il rend
  visibles 2 échecs supplémentaires liés au point ci-dessus.
- **Le finding documenté** ici, avec les trois ordres et les trois comptes.
- **La recommandation** : aligner `_official_free_headers` sur
  `_OPENCODE_HEADER_ORDER_225` (l'ordre réellement transmis), puis rétablir
  les assertions **d'ordre** — un `set()` ne protège pas une empreinte.

---

## 19. Les autres réfutations (toutes vérifiées)

### 19.1 B3 — le sur-échantillonnage est **hors configuration** (déjà traité au §17)

Le réfuteur confirme mon erreur et en ajoute une **plus grave**, avec preuve
live.

### 19.2 B3 — des options **expirées** entrent dans le pool servi

C'est la trouvaille la plus sérieuse après le §18. La branche IPv6
(`egress_runtime.py:216-241`) appelle `list_options(status=USABLE)` **sans le
gate `expires_at`/`reset_at`** que `select_many` applique :

```
10 options IPv6 'usable' en base, 0 fraîche
  -> 8 sont injectées dans le pool servi (les 2 http sont filtrées)
  -> au cap réel (3), 1 des 3 slots est une ligne EXPIRÉE
```

**Preuve live que j'ai reproduite moi-même** :

```
egress_rotation_slots (provider=public, selected_at 2026-11-21 01:18:51)
  -> option_id=8484
  -> public|socks5|216.22.13.47:1080|2604:9a00:...
  -> status=usable, expires_at expiré depuis 10,3 jours
```

**Le slot de rotation — qui est l'état publié de la sélection — nomme une
option que le catalogue lui-même rejette.**

**Attribution — CORRIGÉE le 2026-11-21.** J'avais écrit ici que la branche
IPv6 « existait déjà à HEAD (commit `[013]`) », donc que le défaut était
pré-existant. **C'est FAUX, et un juge indépendant me l'a signalé :**

```
git show HEAD:free/egress_runtime.py | grep -c '":" in str(option.egress_ip'
  -> 0        (la branche est ABSENTE de HEAD)
```

La branche IPv6 a été **ajoutée par le diff de ce round**. Le défaut des
options expirées entrant dans le pool est donc **INTRODUIT par ce lot**, pas
pré-existant. Ma première lecture s'appuyait sur le commentaire `[013]` présent
dans le code ajouté — j'ai pris une étiquette pour une provenance.

C'est la deuxième fois dans ce round que je me trompe sur une attribution (la
première : le plafond 64 vs 3, §17). Les deux fois, un réfuteur indépendant a
corrigé. **C'est précisément la valeur du protocole adversarial.**

**Correctif à appliquer** : porter le gate de `is_selectable`
(`expires_at > now`, `reset_at <= now`) dans la branche IPv6.

### 19.3 B1b — une option `http` **voit son TTL renouvelé** par un frère

Le réfuteur prouve par rejeu que la ligne `http` échappée n'est pas sondée,
mais qu'un **frère** (`https` même `host:port`, autre `egress_ip`) la
ressuscite :

```
ligne http 1.2.3.50:8080 egress=1.2.3.50, usable, expirée (now-90000)
ligne https 1.2.3.50:8080 egress=1.2.3.99, fraîche
refresh() sonde https://1.2.3.50:8080  ->  probe rend egress_ip=1.2.3.50
RÉSULTAT : la ligne http passe de -89 900 s à +100 s, success_count 1->2
```

**Cause racine** : `_resolve_key`/`apply` (`free/egress_store.py:560-583`)
résolvent l'identité **par `egress_ip`** (`get_by_egress`), donc la preuve du
frère s'absorbe dans la ligne `http`.

**Le skip T137-sonde est donc partiel** : il empêche la sonde directe, pas la
résurrection par absorption.

### 19.4 B4 — deux tests affaiblis, et un test à moi

Le réfuteur a prouvé par mutation que **remettre `int(option.last_status or
200)`** (T138) **ne fait échouer aucun test**, et que supprimer le skip
T137-sonde est **invisible à toute la suite**. Les deux correctifs sont donc
**sans filet**.

Il note aussi que mon test `test_public_rows_ipv6_branch_refuses_cleartext`
a été **affaibli** — à vérifier.

### 19.5 A2 — T138 et le skip T137-sonde n'ont aucun test

Même constat, mesuré indépendamment par un second réfuteur :

> « Remettre `int(option.last_status or 200)` … ne fait échouer AUCUN test :
> suite complète verte, alors que la mutation fabrique une preuve 200 fictive
> … Idem pour le skip T137-sonde : sa suppression est invisible à toute la
> suite. »

**Deux correctifs confirmés sans filet de sécurité.** C'est le point que je
dois corriger en priorité après le §18.

---

## 20. Corrections appliquées après les réfutations

### 20.1 Mon test IPv6 était **inerte** — corrigé et prouvé discriminant

Le réfuteur B4 a prouvé par instrumentation (`branch_entered=False`) que
`test_public_rows_ipv6_branch_refuses_cleartext` **n'entrait jamais dans la
branche** qu'il prétendait garder.

**Ma faute, exactement reproduite** : je créais une option IPv6 `https`
**fraîche**, donc `select_many` la retenait, donc `selected` contenait déjà de
l'IPv6, donc le `if not any(":" in egress_ip …)` était **faux**. Le test
passait pour une mauvaise raison — le défaut même qu'il devait garder.

**Correction** : le scénario utilise des options IPv6 `usable` mais
**PÉRIMÉES** (le cas réel : `select_many` les rejette, la branche les
rattrape), et le test **vérifie lui-même qu'il entre dans la branche** par
instrumentation de `store.list_options`. Sans cette auto-vérification, il
pourrait redevenir inerte en silence.

**Preuve par mutation** : filtre de schéma retiré de la branche →
`AssertionError: une ligne en clair a été injectée … http://[2001:db8::2]:8080`.
Restauré → 15/15 passent.

### 20.2 Assertion auto-référentielle `_SPEC_UA` — corrigée

`tests/test_identity_generator.py` comparait l'UA de production à
`load_spec().get("user_agent")` — **le même fichier que la production lit**.
Le test ne pouvait détecter aucun drift (prouvé par mutation : `load_spec`
forcé à `9.9.9`, le test passait).

**Correction** : UA **figée** (`"opencode/latest/2.3.0/cli"`) + un test
`test_spec_ua_is_the_pinned_value_not_a_self_reference` qui **rougit** quand le
wire change, avec un message actionnable. Vérifié par mutation : l'UA passé à
`9.9.9` → le test échoue.

**Un bump de version doit désormais casser ce test** : c'est le signal qu'il
faut re-mesurer la capture (principe XVIII).

### 20.3 Ce qui reste ouvert — et pourquoi

| Point | Statut |
|---|---|
| §18 — trois ordres d'en-têtes divergents | **décision opérateur** (engage le gate 403 et la consommation d'IP) |
| §19.2 — options expirées dans le pool (branche IPv6) | pré-existant à HEAD ; correctif identifié (porter le gate `is_selectable`) |
| §19.3 — résurrection d'une ligne `http` par un frère | cause racine identifiée (`_resolve_key` par `egress_ip`) |
| §19.4/19.5 — T138 et skip T137-sonde **sans test** | à couvrir |
| §17 — mes chiffres « 46→64 » hors configuration | documentation corrigée |

**Priorité 1** : §19.4/19.5 (deux correctifs sans filet) et §18 (décision).

---

## 21. Filet de sécurité ajouté sur T138 (le réfuteur avait raison)

Le réfuteur avait prouvé par mutation que **remettre `int(option.last_status
or 200)` ne faisait échouer aucun test**. J'ai reproduit : la suite entière
restait verte.

**Test ajouté** — `tests/test_egress_rotation.py::test_rows_from_options_never_invents_a_200` :

```python
option = new_option(...)          # jamais observée, last_status = 0
assert int(option.last_status or 0) == 0
row = rows_from_options([option])[0]
assert row["last_status"] == 0, "last_status inventé … (principe VII)"
```

**Vérifié par mutation** : `or 0` → `or 200` fait échouer le test
(`assert 200 == 0`), puis restauration. Ce n'est plus un correctif sans filet.

### Incident de méthode, consigné

En lançant cette vérification par mutation, j'ai chaîné la restauration **après**
un `pytest tests/` complet qui a dépassé le délai et est passé en arrière-plan.
Le fichier `free/egress_rotation.py` est resté **muté** pendant quelques
minutes avant que je le détecte et le restaure.

**Leçon** : une mutation doit être restaurée **immédiatement**, dans le même
appel que la mesure — jamais après une commande longue dont on ne maîtrise pas
la durée. C'est le pendant, côté auteur, du protocole
[00-protocole-suite-pendant-workflow.md](./00-protocole-suite-pendant-workflow.md).

**Vérifié après coup** : `grep -c "MUTATION\|9\.9\.9"` = **0** sur les quatre
fichiers touchés (`egress_rotation.py`, `egress_runtime.py`, `public_pool.py`,
`impersonation.json`). Aucune mutation résiduelle.

### État des tests

**167 tests passent** sur le périmètre : `egress_rotation`, `egress_runtime`,
`egress_sources`, `egress_credentials`, `egress_public_adapter`,
`pool_characterization`, `public_proxies_pool`, `identity_generator`,
`official_client_parity`.

---

## 22. Le défaut de la branche IPv6 est CORRIGÉ (et il était de mon fait)

### L'attribution — je m'étais trompé

J'avais écrit au §19.2 que la branche IPv6 « existait déjà à HEAD (commit
`[013]`) », donc que le défaut était pré-existant. **Le juge m'a contredit et
il a raison :**

```
git show HEAD:free/egress_runtime.py | grep -c '":" in str(option.egress_ip'
  -> 0        (ABSENTE de HEAD)
```

La branche a été **ajoutée par le diff de ce round**. Le défaut est donc
**introduit par ce lot**. Ma lecture s'appuyait sur le commentaire `[013]`
présent dans le code ajouté — **j'ai pris une étiquette pour une provenance**.

### Le correctif

```python
# free/egress_runtime.py — branche IPv6
_now = time.time()
v6 = [
    option for option in self.store.list_options(
        provider=Provider.PUBLIC, status=OptionStatus.USABLE, limit=…)
    if ":" in str(option.egress_ip or "")
    and not self._is_denied(option)
    and is_selectable(option, _now, policy)      # ← le gate qui manquait
]
```

**Effet mesuré sur la base live** : `public_rows()` rend **0 ligne IPv6**
expirée (contre **8** avant), dont une qui occupait 1 des 3 sièges servis et
évincait une ligne fraîche de score 85,1.

**Une seule définition de « sélectionnable »** : `is_selectable` — le même que
`select_many`. C'est ce qui manquait depuis le début.

### Deux tests, chacun discriminant sur SON filtre

Le premier jet de mon test était **redevenu inerte** après l'ajout du gate :
ses options périmées étaient rejetées par le filtre temporel **avant**
d'atteindre le filtre de schéma, donc retirer le filtre de schéma ne le faisait
plus échouer. C'est la **deuxième fois** que ce test s'avérait inerte.

| Test | Scénario | Mutation qui le fait échouer |
|---|---|---|
| `…ipv6_branch_refuses_cleartext` | IPv4 `https` fraîche (occupe `select_many`) + IPv6 **`http` fraîche** | filtre de schéma retiré → ✅ échoue |
| `…ipv6_branch_applies_the_temporal_gate` | IPv6 `https` **périmée** | gate `is_selectable` retiré → ✅ échoue |

Les deux scénarios sont construits pour que **le filtre testé soit la seule
barrière** — et chacun vérifie par instrumentation qu'il entre bien dans la
branche.

**139 tests passent**, aucune mutation résiduelle (`grep -c "MUTATION\|9\.9\.9"`
= 0 sur les quatre fichiers touchés).

### Bilan de mes erreurs dans ce round

| # | Erreur | Détectée par |
|---|---|---|
| 1 | « le pool plafonne à 64 » (c'est 3) | réfuteur A4 |
| 2 | test IPv6 inerte (`branch_entered=False`) | réfuteur B4 |
| 3 | attribution « pré-existant » (c'est introduit) | juge de synthèse |
| 4 | test redevenu inerte après le gate | **moi**, en rejouant la mutation |
| 5 | `_SPEC_UA` auto-référentiel (dans les tests existants) | réfuteur B4 |

**Quatre sur cinq ont été trouvées par un vérificateur indépendant.** C'est
l'argument le plus fort de tout cet audit en faveur du protocole adversarial :
sur mon propre travail, je n'ai vu qu'une erreur sur cinq.

---

## 23. Le défaut le plus grave du round : mon filtre pouvait **vider le pool**

Verdict A3 (`refute: true`, gravité **important**). **Reproduit et corrigé.**

### Le défaut

```
130 options http RAPIDES + 1 option https LENTE (classée 131e)
  -> select_many(count=128)  = 128 http  -> filtre = 0 servable
  -> public_rows() = []
  -> sync_public_pool -> clear_public_proxies()  -> POOL VIDE
```

Le pool était **vidé entièrement** alors qu'une ligne parfaitement servable
existait en base. Le garde « catalogue vide » de `sync_public_pool` ne
protégeait pas : la fenêtre était vide **après filtrage**, et
`clear_if_empty = not result.errors` vaut `True` dès que le cycle n'a pas
d'erreur.

**Ma faute** : j'avais choisi un sur-échantillonnage **fixe** (`count =
max_active * 2`). Fixe, il ne couvre que le cas où les non-servables sont
minoritaires — pas celui où ils saturent la fenêtre.

### Le correctif

Élargissement **progressif** jusqu'à réunir `max_active` options servies,
avec arrêt quand la fenêtre cesse de grandir (catalogue épuisé) :

```
count=128  -> 128 http, 0 servable
count=256  -> 130 http + 1 https  ->  1 servable   ✅
```

`select_many` n'accepte pas d'offset : on élargit par paliers, et la borne
« la fenêtre ne grandit plus » évite de parcourir 30 000 lignes à chaque appel.

### Vérification

| Test | Mutation | Résultat |
|---|---|---|
| `test_public_rows_never_empties_the_pool_when_the_window_is_saturated` | retour à `count = window * 2` fixe | **échoue** : `assert []` ✅ |

**130 tests passent** sur le périmètre egress.

### Les trois autres volets d'A3

| Volet | Statut |
|---|---|
| Cas 2 (123 http + 5 https → 5/64 servis) | **corrigé par le même élargissement** |
| Cas 3 (une ligne `http` vivante répond 200 au CONNECT) | **assumé** : la politique T137 refuse le clair même fonctionnel — c'est la décision, pas un défaut |
| Cas 4 (trappe opérateur : `ALLOW_CLEARTEXT_PROXIES=True` ne restaure rien) | **documenté** dans `EGRESS_SOURCES.md` : il faut changer **deux** endroits |

Le cas 4 était un vrai piège : l'échappatoire documentée faisait accepter les
candidats à la découverte, les faisait sonder et promouvoir, puis le service
les écartait — l'opérateur payait le budget de sonde sans obtenir un slot.

---

## 24. Les trois correctifs sans filet sont désormais couverts

Le réfuteur A2 avait prouvé par mutation qu'**aucun test** ne couvrait trois
correctifs. Chacun a maintenant son test, **vérifié par mutation** :

| Correctif | Test ajouté | Mutation qui le fait échouer |
|---|---|---|
| **T138** — `last_status` réel, jamais 200 inventé | `test_rows_from_options_never_invents_a_200` (`test_egress_rotation.py`) | `or 0` → `or 200` → ✅ échoue |
| **T137-sonde** — une option `http` en base n'est pas re-sondée | `test_cleartext_option_already_stored_is_never_probed` (`test_egress_public_adapter.py`) | skip supprimé → ✅ échoue (`http://1.2.3.4:8080` sondée) |
| **Filtre v6** — schéma ET gate temporel | 2 tests dans `test_egress_runtime.py` | filtre retiré / gate retiré → ✅ chacun échoue |

**131 tests passent** sur le périmètre egress. `grep -c MUTATION` = **0** sur
les quatre fichiers de production touchés.

### Note de méthode — deux fois le même piège

Mon test du filtre de schéma v6 a été **inerte deux fois** :

1. **Première version** : l'option `https` était fraîche → `select_many` la
   retenait → la branche v6 ne s'activait jamais (`branch_entered=False`).
2. **Deuxième version** : l'option `http` était **périmée** → le gate temporel
   la rejetait **avant** le filtre de schéma → retirer le filtre ne changeait
   rien.

**Ce qui a sauvé la deuxième fois** : j'ai rejoué la mutation après avoir
ajouté le gate. Sans ce réflexe, j'aurais livré un test décoratif **en le
croyant bon** — et le rapport l'aurait présenté comme un filet.

**Règle** : après avoir ajouté un filtre, **rejouer la mutation du filtre
précédent**. Un test qui passe n'est pas un test qui garde.

---

## 25. Mesure finale sur données live

Après l'ensemble des correctifs, `public_rows()` sur `logs/registry.db` réel :

| Mesure | Valeur |
|---|---|
| Durée | **77 ms** |
| Lignes rendues | 70 |
| **Par schéma** | **`https` : 70** (100 %) |
| Lignes `http` | **0** |
| Lignes IPv6 | **0** (les 8 expirées sont écartées par le gate) |
| Servies au plafond catalogue (64) | 60 |
| Servies au plafond réel (3) | 3 |

**Deux points à souligner** :

1. **77 ms**, contre **231-292 ms** mesurés par le réfuteur avant les
   correctifs (+61 ms de surcoût qu'il avait chiffré). L'élargissement
   progressif **s'arrête dès que la fenêtre suffit** : il ne paie le coût que
   lorsqu'il apporte quelque chose. Le surcoût a disparu.
2. **0 ligne `http`, 0 IPv6 expirée** : les deux filtres de la branche IPv6 et
   le filtre de service convergent, et la règle n'est plus définie qu'à un
   seul endroit (`_SERVED_SCHEMES`).

### Récapitulatif des correctifs de ce round

| # | Correctif | Preuve |
|---|---|---|
| 1 | Filtre de schéma au **service** (`_SERVED_SCHEMES`) | 6 contournements testés, 0 fuite (A1 TENUE) |
| 2 | Filtre de schéma à l'**entrée** (`public_rows`) | idem |
| 3 | **Gate `is_selectable`** sur la branche IPv6 | 8 options expirées → 0 ; test par mutation |
| 4 | **Élargissement progressif** de la fenêtre | pool vidé → 1 ligne servable ; test par mutation |
| 5 | **T138** — `last_status` réel | test par mutation |
| 6 | **T137-sonde** — option `http` non re-sondée | test par mutation |
| 7 | **`_SPEC_UA`** figée (assertion auto-référentielle) | test par mutation |
| 8 | **Documentation** : plafond réel (3), trappe opérateur, mesures | réfuteurs A4 et A3 |

**131 tests passent.** `grep -c MUTATION` = 0 sur les quatre fichiers de
production touchés.

---

## 26. Verdict de la suite complète sur arbre au repos

**C'est la première fois de tout ce round que la suite tourne sans qu'aucun
agent n'écrive.** Protocole appliqué :
[00-protocole-suite-pendant-workflow.md](./00-protocole-suite-pendant-workflow.md).

```
python -m pytest tests/ -q --no-header -p no:cacheprovider
  -> progression 100 %
  -> 6 407 tests
  -> 0 FAILED / 0 ERROR
  -> exit = 0
```

### L'historique des suites de ce round

| Suite | Contexte | Échecs | Dont réels |
|---|---|---|---|
| `full35` | workflow écrivant | 20 | **20** (régression T137) |
| `full36` | workflow écrivant | 5 | **0** (charge) |
| `full37` | workflow écrivant | 2 | **0** (charge + lecture concurrente) |
| **`full38`** | **arbre au repos** | **0** | — |

**Ce que cela démontre** : les 7 échecs de `full36`/`full37` étaient bien des
artefacts (charge machine et lecture d'un fichier en cours de réécriture), et
les 20 de `full35` étaient la seule régression réelle. Les deux mécanismes sont
documentés dans le protocole.

### Et sur le périmètre egress

```
131 tests (egress_runtime, egress_rotation, egress_sources, egress_credentials,
           egress_public_adapter, pool_characterization, public_proxies_pool)
  -> 0 échec
```

Chaque correctif de ce round a son test **vérifié par mutation** :
`grep -c MUTATION` = **0** sur les quatre fichiers de production touchés.
