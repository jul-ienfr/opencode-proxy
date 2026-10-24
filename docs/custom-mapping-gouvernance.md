# Gouvernance du custom mapping (`custom_routes`)

> Statut : RÈGLE PROJET — en vigueur depuis le 2026-10-17.
> Déclencheur : incident du 2026-10-17 (§4). Cette règle fait foi : elle prime
> sur toute interprétation antérieure présente dans les docstrings de tests.

## 1. La règle

`config.yaml › custom_routes` est le **territoire de l'utilisateur**.

- L'utilisateur choisit ses réécritures de modèles (`match` → `model`), y compris
  les redirs vers des ids *free* (ex. `deepseek-v4-flash` →
  `deepseek-v4.1-flash-free`, les alias `mimo-v2.5`/`qwen3.7-max`/`glm-5.1`/
  `muse-spark-1.2/1.3-contributor` → `glm-5.3-flash-free`). C'est une décision
  humaine, délibérée, et elle a valeur de référence.
- **Aucun agent, script, test, mécanisme de découverte ou d'auto-persist ne
  modifie, ne « répare » ni ne restaure le contenu de `custom_routes`.**
- Un état du dépôt (y compris le `config.yaml` committé dans git) n'est jamais
  une autorité pour juger le mapping courant : le yaml déployé EST la vérité.
- Le contenu n'est jamais « suspect » : un lien payant → free par custom route
  est un usage normal de la fonctionnalité, pas un shadow accidentel.

## 2. Hiérarchie de résolution (mesurée)

Ordre réel de la résolution d'un id demandé :

1. `custom_routes` — une route dont le `match` contient l'id le réécrit vers la
   cible `model` déclarée en **un seul hop** (pas de passage transitif en boucle
   au sein des `custom_routes`) ;
2. `free_model_map` — si aucun `custom_routes` ne couvre l'id, la jambe
   *free-first* vient de la map ;
3. identité — si rien ne couvre l'id, il est servi tel quel.

⚠️ Cas attendu de « casse bruyamment » : une route déclarant une cible absente
de `models:` est une erreur de saisie détectée par le test dédié
(`test_every_declared_route_is_followed_literally`) — c'est un contrôle de
validité, jamais un jugement du contenu.

## 3. Risque de perte : le persist depuis la mémoire d'un daemon

Un daemon proxy/dashboard vit hors de ce dépôt (processus `pythonw`, port HTTP
4000 au 2026-10-17). Sa mémoire conserve l'état chargé au démarrage ; le
persist peut arriver à tout moment (hot-reload détecté, dashboard, discovery
auto-persist) et **dump l'état mémoire complet** dans `config.yaml`.

Conséquence pratique : **éditer `config.yaml` à la main pendant que le proxy
tourne est non fiable** (écrasement à la prochaine persist, ou mélange
d'états — observé le 2026-10-17 : deux routes re-pointées vers
`mimo-v2.6-flash` sans intervention humaine identifiable).

Procédures d'édition durables du mapping :

1. **Via le dashboard** (recommandé) — l'état mémoire re-persiste de façon
   cohérente avec ce que le proxy exécute vraiment ;
2. **Proxy arrêté** — arrêter le daemon, éditer `config.yaml`, redémarrer ;
3. **Jamais d'édition manuelle en plein vol** : elle peut être écrasée par la
   prochaine persist du daemon. Si un test ou une doc « contredit » le mapping
   de l'utilisateur, c'est le test ou la doc qui est en faute — jamais la
   config.

## 4. Chronologie de l'incident du 2026-10-17 (factuel)

| heure (≈) | fait |
|---|---|
| 09:26–09:27 | boot du daemon (proxy + dashboard, `pythonw`, port 4000) |
| 10:04–10:12 | l'agent réécrit les 6 `custom_routes` vers les valeurs de HEAD (glm-5.3-flash-free / deepseek-v4-flash), en interprétant les 5 tests trip-wires rouges comme une dérive de config à corriger |
| 10:12:31 | une écriture externe non identifiée (persist mémoire) re-pointe `mimov25` et `musespark13contributor` vers `mimo-v2.6-flash`, en plein milieu des éditions de l'agent |
| ~10:14 | l'utilisateur rappelle la souveraineté du custom mapping ; l'agent annule ses éditions |

**État du disque à la clôture de l'incident (l'agent n'édite plus `config.yaml`
après ce point)** : `mimov25` → `mimo-v2.6-flash` et `musespark13contributor` →
`mimo-v2.6-flash` (valeurs issues de l'écriture non identifiée, à vérifier et
re-pointer par l'utilisateur dans son GUI si elles ne sont pas les siennes) ;
`deepseekv4flash` → `deepseek-v4.1-flash-free`, `qwen37max` / `glm51` /
`musespark12contributor` → `glm-5.3-flash-free` (choix de l'utilisateur).

Causes identifiées : (a) mauvais verdict de l'agent — le mapping déployé est
autoritaire ; (b) le contrat de `tests/test_custom_routes_shadow.py` (Lot Z
V1b) épinglait des cibles du mapping comme des invariants, ce qui n'est plus
acceptable : **la config est une décision de l'utilisateur, pas un invariant
de test** ; (c) fenêtre d'édition concurrente daemon / fichier ouvert.

Corrections apportées (dans ce dépôt) :

- `docs/custom-mapping-gouvernance.md` (ce document) ;
- `tests/test_custom_routes_shadow.py` — le contrat devient : « le runtime
  exécute **exactement** ce que le yaml déclare », sans épingler aucune cible
  ni juger le contenu (respect de la souveraineté) ;
- `tests/test_free_leg_effort_probe_isolation.py` /
  `tests/test_free_leg_effort_recompute.py` — les e2e du témoin d'effort
  séquestrent les custom_routes de la config déployée pour leur couple
  **harnais** payant/free (scénario simulé) sans muter la config ni le disque.

## 5. Pour tout futur écrivain automatique (discovery, agents, outils)

- Tout hook de persist/discovery doit être **additif strict** en `models:` et
  `free_model_map:` et n'écrire **jamais** la branche `custom_routes` (sauf
  explicitement pilotée par l'utilisateur via le dashboard).
- Un agent qui observe une divergence entre le mapping déployé et un test doit
  : (1) considérer la config comme la fontaine de vérité ; (2) signaler la
  divergence à l'utilisateur ; (3) adapter le **test** (jamais la config) —
  avec l'accord de l'utilisateur en cas d'ambiguïté.
- Les écritures concurrentes (daemon en vie) sont un danger documenté au §3.
