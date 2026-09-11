# Contrat `reasoning_content` par upstream (lot L13 — point B1)

> Statut : **contrat documenté et mesuré**. Décision de repli : **implémentée et
> testée** (voir §5), avec le même mécanisme « retry-once » que les items
> `reasoning` de `/responses`.
>
> Contexte : `reasoning_content` n'existe dans **aucune** spécification OpenAI.
> Le `delta` officiel ne contient que `content`, `function_call` (déprécié),
> `refusal`, `role`, `tool_calls` ; l'objet message n'a **aucun** champ de
> raisonnement. C'est une **convention vendeur** (DeepSeek, GLM, Kimi, Qwen…).
> Le proxy en fait **le** transport du raisonnement vers les cibles
> OpenAI-compatibles. C'est conforme à l'écosystème réel, mais pas à la spec —
> d'où ce document.

---

## 1. Pourquoi ce document existe

Le proxy a besoin de transporter le raisonnement d'un tour à l'autre, sans quoi
le modèle perd sa mémoire du tour précédent. Mesuré sur sonde live
multi-tours (`docs/PLAN-raisonnement.md` §5) : sur un historique où le
raisonnement était strippé, le modèle ne retrouvait **0 sur 2** fragments
distinctifs de son propre raisonnement du tour 1. Le strip « pour économiser des
tokens » était donc un comportement **anormal**.

La règle retenue est : **le raisonnement voyage**, sous la forme que l'upstream
sait lire. Cette forme dépend du protocole de la cible — d'où trois contrats
distincts ci-dessous, au lieu d'un seul.

---

## 2. Les trois contrats, mesurés

Sonde reproductible : `python _probe_l13.py` (racine du dépôt). Résultat obtenu
sur un historique multi-tours où l'assistant a raisonné au tour 1
(`RAISONNEMENT-1`), avec un bloc `thinking` synthétique (signature locale).

| Cible (protocole) | Ce que le proxy écrit | Le raisonnement survit ? |
|---|---|---|
| **OpenAI-compatible** (`/chat/completions`) — P2, P4 | `message.reasoning_content = <texte>` sur chaque message assistant porteur de `thinking` | **OUI** — `reasoning_content='RAISONNEMENT-1'` |
| **Anthropic** (`/v1/messages`) — P4 | bloc `content[].type == "thinking"` **avec le texte**, inséré en tête du message assistant | **OUI** — `blocs=['thinking', 'text']` |
| **Responses** (`/responses`) — P5 | item `{"type": "reasoning", "summary": [{"type": "summary_text", …}]}` inséré **immédiatement avant** l'item `output_text` du même tour, + marqueur interne `_has_synthetic_reasoning_items` | **OUI** — `types=['…','reasoning','…']` |

**Points de contrat importants :**

1. **La signature ne franchit jamais la frontière Anthropic→OpenAI.** Seul le
   **texte** devient `reasoning_content`. Une signature Anthropic est
   cryptographique : la recopier vers un upstream qui la validerait produirait un
   rejet. C'est pourquoi les blocs `thinking` **synthétiques** (signature locale
   fabriquée par le proxy) voyagent comme les originaux vers les cibles
   OpenAI-compatibles, mais `strip_synthetic_thinking` reste appelé sur la
   branche `protocol == "anthropic"`.
2. **Le champ n'est jamais émis vers Anthropic.** Mesuré :
   `reasoning_content` absent du corps produit par `openai_to_anthropic_request` ;
   le raisonnement y devient un bloc `thinking` natif. Émettre
   `reasoning_content` vers Anthropic serait un champ inconnu.
3. **Un historique assistant « thinking mais sans texte »** reçoit un
   placeholder `" "` (espace), et non `""` ni une absence de champ. Motif : un
   champ présent mais vide se distingue d'un champ absent pour plusieurs
   upstreams, qui traitent l'absence comme « pas de raisonnement » et
   réinitialisent la mémoire. Le placeholder préserve la **position** du champ.
   Sites : `mapping.py` L1103, L1362, L1376, L1389, L1398.
4. **Le raisonnement est lu dans les deux orthographes.** En entrée,
   `reasoning_content` **ou** `reasoning` sont acceptés
   (`msg.get("reasoning_content") or msg.get("reasoning")`, sites L1787, L2083,
   L2672). Certains clients utilisent `reasoning`.

---

## 3. Le problème des upstreams stricts

`reasoning_content` étant hors spec, un upstream peut le **rejeter** (400/422).
Le proxy avait déjà ce problème pour les items `reasoning` de `/responses` et
l'avait résolu par un **retry-once** : sur 400/422, l'item fautif est retiré et
la requête rejouée **une fois**, plutôt que de casser le tour entier (texte +
tool calls) pour tous les clients routés sur cet endpoint
(`opencode.py:9928-9949`, marqueur `_has_synthetic_reasoning_items`).

**Écart constaté (avant ce lot) :** ce filet existait pour `/responses` mais
**pas** pour `reasoning_content` sur `/chat/completions`. Un historique porteur
de `reasoning_content` partait donc sans aucun repli vers une cible Chat
stricte. Vérifié par sonde : `anthropic_to_openai` ne pose **aucun** marqueur de
repli (`marqueur de repli present : []`), et `grep _has_synthetic` sur
`opencode.py` ne remonte que des sites liés à `/responses`.

---

## 4. Politique de repli retenue

**Décision : le raisonnement est un enrichissement, jamais un bloquant.**

| Cas | Comportement |
|---|---|
| L'upstream accepte `reasoning_content` (cas normal, écosystème réel) | transmis tel quel ; mémoire du raisonnement préservée |
| L'upstream le rejette (400/422) | **retry-once** sans le champ ; le tour aboutit avec texte + tool calls, seule la mémoire du raisonnement est perdue |
| Cible Anthropic | jamais de `reasoning_content` ; bloc `thinking` natif (le strip synthétique s'applique) |
| Cible Responses | item `reasoning` natif + filet retry-once déjà en place |

Le repli **dégrade** (perte de mémoire du raisonnement) au lieu d'**échouer**
(500 pour tous les clients). C'est cohérent avec la règle appliquée partout
ailleurs dans le proxy : une capacité non supportée par la cible se dégrade
proprement, elle ne casse pas la requête.

---

## 5. Implémentation

Le filet est aligné sur celui de `/responses` : un marqueur interne privé,
posé par le convertisseur quand il a effectivement injecté du raisonnement, puis
consommé par le handler sur 400/422.

- **Pose** : `anthropic_to_openai` (`app/protocol/mapping.py`) positionne
  `_has_synthetic_reasoning_items = True` sur le corps Chat **si et seulement si**
  au moins un message porte du `reasoning_content`. Aucun marqueur n'est posé
  pour une requête sans raisonnement — donc aucune requête ordinaire n'est
  rejouée.
- **Non-sérialisation** : `oc._serialize_json_body` retire la clé privée avant
  d'encoder (elle n'est **jamais** transmise à l'amont — sinon elle produirait
  précisément le 400 `unknown parameter` qu'on cherche à éviter).
- **Consommation** : sur 400/422, et seulement si le marqueur est présent :
  - corps avec `input` (chemin `/responses`) → retrait des items `reasoning` ;
  - corps avec `messages` (chemin `/chat/completions`) → retrait du champ
    `reasoning_content` de **tous** les messages ;
  - puis **un seul** rejeu de la requête.

Garde-fous : le retry est **unique** (pas de boucle : si l'upstream rejette
encore, l'erreur est rendue telle quelle en 400), et il n'a lieu **que** si le
marqueur est présent.

**Tests de verrouillage** (`tests/test_e2e_protocol_matrix.py`, préfixe `test_l13_`) :

| Test | Ce qu'il prouve |
|---|---|
| `test_l13_reasoning_content_travels_to_chat_upstream` | le raisonnement voyage bien vers Chat, et la signature locale ne franchit pas la frontière |
| `test_l13_marker_never_reaches_the_wire` | le marqueur est posé sur le dict **mais absent** des octets sérialisés |
| `test_l13_strict_upstream_400_triggers_retry_without_reasoning` | 400 → 2 appels amont, le 2nd sans `reasoning_content`, et le tour **aboutit** |
| `test_l13_retry_is_once_only` | un upstream qui rejette encore ne provoque **pas** de boucle : 2 appels, 400 rendu |
| `test_l13_no_marker_means_no_retry_for_plain_requests` | une requête sans raisonnement n'est **pas** rejouée (1 seul appel) |

Vérifié par **mutation** : neutraliser la branche `messages` du repli fait rougir
`test_l13_strict_upstream_400_triggers_retry_without_reasoning` ; la restaurer
repasse au vert. Le verrou est donc réellement porteur.

---

## 6. Ce que ce document ne prétend pas

1. **Aucun upstream réel n'a été testé en live ici.** Les contrats §2 sont
   mesurés au niveau de la **conversion** (le corps produit), pas de la réponse
   d'un serveur distant. Un upstream réel qui rejetterait `reasoning_content`
   n'a pas été observé ; c'est précisément pourquoi le repli est **défensif** et
   non fondé sur une liste d'upstreams réputés stricts.
2. **La liste des upstreams qui acceptent/ignorent le champ n'est pas
   tabulée**, faute de source normative : `reasoning_content` n'est dans aucune
   spec, donc « qui l'accepte » n'est pas documentable autrement qu'en
   interrogeant chaque fournisseur. Le proxy ne fait donc **aucune** hypothèse
   par modèle : il transmet, et se replie sur rejet.
3. **Le placeholder `" "`** est un choix de conception hérité, conservé pour ne
   pas régresser la parité multi-tours. Il n'est pas adossé à une spec.
