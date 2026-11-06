# Implementation Plan: Parité wire officielle de la jambe free

**Branch**: `003-official-client-parity` | **Date**: 2026-11-05 | **Spec**: [spec.md](./spec.md)  
**Audit**: [docs/AUDIT_IMPERSONATION_FORMAT_2026-11-05.md](../../docs/AUDIT_IMPERSONATION_FORMAT_2026-11-05.md)  
**Runbook incident**: [docs/PLAN_RETABLISSEMENT_FREE_2026-11-05.md](../../docs/PLAN_RETABLISSEMENT_FREE_2026-11-05.md)  
**Statut**: plan proposé, aucune modification de production appliquée.

---

## 0. Incident en cours (2026-11-05)

La jambe free est signalée **down** : l'opérateur ne voit que des 429 « quota dépassé », alors que le quota n'est pas prouvé épuisé. L'hypothèse historique est qu'une impersonation incomplète produit aussi un faux 429.

Les traces locales de 24 h nuancent le diagnostic :

- 2 377 requêtes, toutes émises par le smoke test `python-requests` sur `jev-1.13-free` ;
- 2 141 `502 all tunnels failed`, 227 `429` avec `Retry-After` de **42–58 s** ;
- aucun trafic client réel depuis le **2026-11-04 20:28 UTC**, date du dernier `deepseek-v4.1-flash-free` en **200**.

Un `Retry-After` de 42–58 s n'est pas le bucket journalier : c'est un **rate-limit court** par modèle/IP. Le 502 est un problème de tunnel. Le vrai `FreeUsageLimitError` journalier, lui, a un `Retry-After` de plusieurs heures.

Le corps `Free usage limit reached for jev-1.13-free. Retry after 54s.` est en fait produit **par le proxy lui-même** (`model_global_remaining`) : aucune requête ne part vers l'amont. C'est un cooldown en mémoire, pas un quota fournisseur. Le runbook inclut le **Plan M** pour le purger et vérifier qu'il n'est pas ré-armé par le 429 local.

La première campagne live (2026-11-05 22:47–22:52 UTC) a testé les profils exacts 2.2.5 CLI et desktop sur l'IP directe : les deux reçoivent `FreeUsageLimitError` (RA ~6 800–6 956 s), **aucun 403/`FreeTierError`**. Le corps et l'ordre exacts passent donc la couche signature ; c'est le bucket IP qui est épuisé. En revanche, l'infrastructure est bloquée : `0/6` stations utilisables, `AUTH_FAILED` OpenVPN, SOCKS `user rejected`, WireGuard sans DNS. Le prérequis est donc le **Plan N (credentials/egress)** ; sans une IP fraîche, on ne peut pas distinguer CLI et desktop au-delà de la couche quota.

**Mise à jour 2026-11-06** : token NordVPN accepté, credentials service régénérés. L'`AUTH_FAILED` est une **limite de sessions simultanées** : six stations + un conteneur de test = refus ; six stations arrêtées + un conteneur = auth immédiate. Les profils CLI **et** desktop exacts, testés dans le namespace VPN sur `87.120.102.86`, reçoivent le même `FreeUsageLimitError` (RA ~71 431 s) : le wire passe la couche signature, l'IP est épuisée. Le blocage restant est donc (1) la limite de sessions, (2) le quota par IP. Le superviseur doit être aligné sur la limite réelle et laisser l'allocateur trouver une IP fraîche.

**Clean-room (2026-11-06)** : client officiel Linux 1.19.5 neuf, workspace/HOME/conteneur neufs. IP directe fraîche : officiel **200 `pong`**, replay exact **200**. IP VPN partagée `64.238.204.228` : officiel **429**, replay exact **429**. Le proxy local en direct rend **200**. La parité de wire est donc prouvée sur une IP fraîche ; le 429 VPN est un quota d'IP.

**Conséquence** : le plan de parité reste valide, mais il ne doit pas être exécuté avant le triage du 429 réel du client. Le runbook `docs/PLAN_RETABLISSEMENT_FREE_2026-11-05.md` est prioritaire : il classe le 429 (local / court / long / `FreeTierError` / 502) et choisit le plan A–N correspondant.

### 0.1 Chaîne de dépendance (les deux plans sont un seul plan)

```text
Plan N (egress/credentials)
   → IP fraîche
      → Plan A (wire 2.2.5 exact CLI ou desktop)
         → 200 + contenu
            → Plan L (canary 1 → 2 → 6 stations)
               → 6/6 client-utilisables
```

- Sans **N**, aucune IP fraîche : les tests A/B ne mesurent que le bucket de l'IP directe.
- Sans **A**, une IP fraîche peut quand même prendre un `FreeTierError` : le 200 ne prouve pas la parité.
- Sans **L**, un 200 isolé ne prouve pas la stabilité des six stations.
- **M** (cooldown local) et **J** (quota IP) sont des portes de triage, pas des alternatives à A/N.

C'est pourquoi les deux documents doivent être lus ensemble : `plan.md` décrit le wire, le runbook décrit l'ordre d'exécution et les blocages.

---

## 1. Résumé

Le proxy sait déjà copier le corps 2.2.5 et rejouer le TLS 1.18.31 / 1.19.5. Il ne sait pas **choisir un client entier**. Le plan remplace les trois décisions indépendantes (identité, UA, corps) par un **profil de wire unique**, résolu une fois par requête, et aligne le défaut sur **2.2.5 `cli`** — le profil déjà présent dans `free/captures/2.2.5.json`, `free/official_payload.json` et `free/impersonation.json`.

La trajectoire est volontairement en deux temps :

1. **Rendre le défaut cohérent** : tout le trafic free parle 2.2.5 `cli`, avec l'ordre, le corps, le TLS et les IDs corrects. On désactive la rotation de versions tant que les profils complets n'existent pas.
2. **Réintroduire la diversité versionnée** seulement quand chaque version possède son profil complet (UA + client + TLS + ordre + corps + titre), avec un test de cohérence par tuple.

Cela règle 3 des 4 incohérences sans ajouter de complexité, et prépare la diversité future sans refaire l'architecture.

**Ordre d'exécution réel** :

0. **Triage et rétablissement** (runbook incident) : classer le 429, tester le replay exact 2.2.5 sur une IP, canary 10 requêtes.
1. **Profil unique** et cible 2.2.5 `cli`.
2. **Privacy et sémantique** (project, prompt, trace, session).
3. **Outillage et tests** (sync, diff, e2e).
4. **Mode `compat`** pour les clients tiers.
5. **Diversité versionnée** (optionnelle, après stabilisation).

---

## 2. Contexte technique

**Langage** : Python 3.12, asyncio, FastAPI.  
**Dépendances** : `curl_cffi` 0.16.x (replay TLS), `httpx` (reste du proxy), pytest. Aucune dépendance nouvelle.  
**Surfaces** :

- `opencode.py` : `_official_free_headers`, `_free_wire_body`, `_free_fp_override`, `_get_free_fp_kwargs`, `_do_free_request_curl_cffi`.
- `free/identities.py` : `VERSION_SPECS`, `IpIdentity.official_provider_headers`, `session_header_items`.
- `free/impersonation.py` : `apply_spec`.
- `free/official_payload.json`, `free/impersonation.json`, `free/captures/2.2.5.json`.
- `scripts/opencode_client_sync.py`, `scripts/mint_capture.py`, `scripts/test_free_egress.py`.
- `tests/test_official_client_parity.py`, `tests/test_identity_generator.py`, `tests/test_per_ip_identity.py`, `tests/test_free_wire_complet.py`.

**Stockage** : le manifeste d'impersonation est un fichier JSON versionné ; les captures brutes restent dans `free/captures/`. Pas de nouvelle base.  
**Plateforme** : Windows + Docker Desktop ; le profil doit rester déterministe et testable hors ligne.  
**Performance** : le profil est mémoïsé par `(version, channel, distribution, generation)` ; aucune I/O sur le chemin chaud.  
**Contraintes** : le free est par IP ; le quota n'est pas global ; aucune conclusion sur une IP ne vaut pour le pool.

---

## 3. Constitution Check

| Porte | État | Preuve / action |
|---|---|---|
| I. Disponibilité mesurée | ⚠️ | l'audit mesure le wire, pas le taux de 200 ; l'A/B gate est obligatoire (Phase 0) |
| II. Trois ressources séparées | ✅ | le profil ne touche ni station, ni session VPN, ni IP |
| III. Source de vérité unique | ❌ | 4 sources divergent (`impersonation.json`, `official_payload.json`, `VERSION_SPECS`, `free/captures`) → un manifeste unique (Phase 1) |
| IV. Reset fournisseur | ✅ | hors périmètre |
| V. IP exclusive | ✅ | le vault garde l'unicité ; le plan ajoute le project/trace par IP |
| VI. Diversité | ⚠️ | la rotation de versions est suspendue, la rotation d'IP reste |
| VII. Aucune preuve, aucun succès | ⚠️ | le diff de wire est une preuve de forme, pas de 200 ; l'A/B fournit la preuve de contenu |
| VIII. Autonomie / interrupteur | ⚠️ | le sync doit être canal-aware et échouer sur dérive |
| IX. Score ordonne | ✅ | hors périmètre |
| X. Dégradation explicite | ⚠️ | si le profil cible est indisponible, basculer sur le profil de rollback et le journaliser |

---

## 4. Décisions d'architecture

### 4.1 Un seul propriétaire du wire : `FreeWireProfile`

Créer un objet immuable, résolu une fois par requête :

```python
@dataclass(frozen=True)
class FreeWireProfile:
    channel: str                 # desktop | npm
    version: str                 # 2.2.5 | 1.19.5
    distribution: str            # standalone-zip | desktop-app | npm
    client: str                  # cli | desktop
    ua: str
    tls: dict                    # ja3, ja3_md5, sigalgs, bun, grease
    header_order: tuple[str, ...]
    header_template: dict        # valeurs statiques + marqueurs de session/trace
    body_template: dict          # clés, outils, max_tokens_field, tool_choice
    request_id_header: str | None
    trace_mode: str              # per-session
    session_mode: str            # per-conversation
    system_template: dict        # date, cwd, platform, git
```

`resolve_profile(identity)` lit le manifeste et retourne le profil. `_official_free_headers(profile, ids)` et `_free_wire_body(profile, client_body)` ne décident plus rien : ils appliquent.

**Pourquoi** : c'est la seule façon d'empêcher un UA 2.2.5 de rencontrer un TLS 1.18.31 ou un corps 1.19.5. Le test de cohérence devient trivial : vérifier le tuple, pas chaque constante isolément.

### 4.2 Cible canonique : 2.2.5 `cli`

Le dépôt a déjà :

- `free/captures/2.2.5.json` (zip standalone, `cli`) ;
- `free/official_payload.json` (corps 2.2.5, 13 outils) ;
- `free/impersonation.json` (UA `opencode/latest/2.2.5/cli`, client `cli`) ;
- le profil TLS 1.19.5 qui est **identique** au 2.2.5 mesuré.

Le défaut doit donc être **2.2.5 `cli`**. Le profil `desktop` (app bundlée) est une variante explicite, pas un mélange. Le profil 1.19.5 reste un rollback tant que 2.2.5 n'est pas validé en live.

**Conséquence** : `ROTATION_VERSIONS` ne doit plus alimenter le wire free. La rotation d'IP et de session reste ; la rotation de version est suspendue (`OPENCODE_CLIENT_VERSION=2.2.5` par défaut, ou une seule version dans le vault). On la réintroduira quand chaque version aura son profil complet.

### 4.3 Un manifeste unique, schema v2

Remplacer le couple `impersonation.json` + `official_payload.json` + constantes par un manifeste qui référence les captures :

```json
{
  "schema": "opencode-impersonation/v2",
  "profiles": {
    "2.2.5-cli": {
      "channel": "desktop",
      "version": "2.2.5",
      "distribution": "standalone-zip",
      "client": "cli",
      "ua": "opencode/latest/2.2.5/cli",
      "tls": { "ja3": "...", "ja3_md5": "...", "sigalgs": ["..."], "bun": "1.4.4" },
      "header_order": ["Authorization", "Content-Type", "User-Agent", "b3", "..."],
      "header_template": { "Authorization": "Bearer public", "Content-Type": "application/json" },
      "session_headers": ["x-opencode-session", "x-opencode-session-id", "x-session-affinity", "x-session-id"],
      "trace_headers": ["b3", "traceparent"],
      "request_id_header": null,
      "body": {
        "primary": { "capture": "free/captures/2.2.5.json", "keys": ["model", "messages", "tools", "stream", "stream_options", "max_completion_tokens"], "tools_count": 13, "max_tokens_field": "max_completion_tokens", "max_tokens": 32000, "tool_choice": null },
        "title": { "capture": "free/captures/2.2.5-title.json", "keys": ["model", "messages", "stream", "stream_options"] }
      },
      "system": { "template": "free/captures/2.2.5-system.txt", "variables": ["date", "cwd", "platform", "git"] }
    }
  }
}
```

Les corps bruts restent dans `free/captures/` ; le manifeste ne stocke que les hashes, les clés et les références. Un `--check` compare les deux.

### 4.4 Le project `global` reste `global`

Supprimer l'override dans `apply_spec` :

```python
# ne jamais faire :
if current_project == "global":
    headers["x-opencode-project"] = captured_project
```

Deux options, à trancher par mesure :

- **fidèle** : garder `global` (c'est ce que fait un client hors dépôt) ;
- **anti-corrélation** : `sha256(f"global|{ip}")[:40]`, format 40 hex valide, distinct par IP.

Le choix par défaut recommandé est **fidèle** (`global`), car le gate ne lit probablement pas le project et la valeur réelle est une fuite. Si l'unicité est nécessaire, utiliser le dérivé par IP, jamais le hash du dépôt.

### 4.5 Le prompt système est un template, pas une capture

Séparer le prompt en :

- **partie stable** : le texte officiel (identique à la capture, hash de référence) ;
- **variables** : `Today's date`, `Working directory`, `Workspace root folder`, `Platform`, `Is directory a git repo`.

Les variables sont rendues depuis le profil (date UTC, cwd synthétique par IP/génération, plateforme du profil, git cohérent avec le project). Le chemin réel de l'opérateur ne doit jamais être rendu.

**Risque** : si le gate hash le prompt complet, le templating casse le 200. Mitigation : Phase 0 mesure trois variantes (date seule, date + cwd, tout) avant de généraliser.

### 4.6 Trace et session

- `trace_id` : stocké dans l'identité (ou la session), réutilisé sur toutes les requêtes de la session ; `span_id` mint par requête ; `b3` et `traceparent` dérivés du même couple.
- `session` : stable par conversation, persistée ; la rotation de 30 min devient un kill-switch (`OPENCODE_FREE_SESSION_ROTATE_S=0` pour désactiver) et non le défaut.
- `msg_` (1.19.5 uniquement) : dérivé stable de `(IP, task_id)` ou conservé par tâche ; jamais partagé entre IP.

### 4.7 Mode `compat` explicite

Deux modes, choisis par profil et journalisés :

- **`official`** : le client est le client officiel (prompt + outils officiels). Le corps passe tel quel, aucun shim.
- **`compat`** : le client est tiers. Le proxy envoie les 13 outils officiels **complets** (descriptions/schémas réels), ajoute les outils client après, et ajoute une note. `tool_choice: auto` seulement si le profil cible l'exige (1.19.5) ; sinon omis (2.2.5).

Les placeholders `properties: {}` / « DO NOT CALL » sont supprimés du chemin `compat` par défaut. Ils ne peuvent revenir que si l'A/B prouve qu'ils passent et que les définitions complètes échouent.

---

## 5. Matrice de divergences et remédiation

| ID | Divergence | Cible | Action | Priorité | Preuve |
|---|---|---|---|---|---|
| D1 | UA/body 2.2.5 + TLS/version 1.18–1.19 | un profil cohérent | `FreeWireProfile` + cible 2.2.5 `cli` ; suspendre `ROTATION_VERSIONS` | P0 | diff tuple |
| D2 | `b3`/`traceparent` après `Content-Length` | positions 4–5 | reconstruire le dict + `header_order` 2.2.5 complet | P0 | e2e wire |
| D3 | placeholders `properties: {}` | définitions officielles complètes | remplacer dans `compat` ; A/B placeholders vs complets | P0 | A/B gate |
| D4 | `tool_choice: auto` sur 2.2.5 | absent | ne pas ajouter en mode 2.2.5 | P1 | body diff |
| D5 | project `global` → hash réel | `global` ou dérivé IP | supprimer l'override `apply_spec` | P0 | unit + wire |
| D6 | chemin réel + date figée | template | templater date/cwd/platform/git | P0 | grep sortant |
| D7 | trace ID par requête | stable par session | stocker `trace_id` dans l'identité | P1 | 2 requêtes |
| D8 | session 30 min | stable par conversation | TTL long / rotation sur erreur | P1 | test conversation |
| D9 | `msg_` par egress | stable par message (1.19.5) | dériver de `(IP, task_id)` | P2 | test retry |
| D10 | `cli` forcé | `cli` ou `desktop` selon distribution | champ `distribution` + profil | P1 | capture |
| D11 | titre/summary non versionnés | templates dédiés | capturer `title`/`compaction` | P1 | body diff |
| D12 | `x-opencode-parent-session-id` absent | présent pour sous-agent | ajouter si `parentID` modélisé | P3 | cas fork |
| D13 | `x-opencode-directory`/`workspace` | absents des captures | retirer ou aligner avec le cwd du prompt | P2 | header diff |
| D14 | `/responses` non vérifié | template responses | capturer un modèle responses | P3 | capture |
| D15 | sync mono-canal, ordre perdu | manifeste canal-aware | réécrire `opencode_client_sync.py` | P1 | `--check` |
| D16 | tests sur chemin sans IP | wire réel per-IP | test e2e curl_cffi + tuple | P0 | CI |

---

## 6. Plan par phases

### Phase 0 — Triage incident + A/B (bloquant)

**But** : rétablir d'abord, mesurer ensuite. Le runbook complet est `docs/PLAN_RETABLISSEMENT_FREE_2026-11-05.md` §1–§4.

1. **Trier le 429 du client** (pas celui du smoke test) :
   - `FreeUsageLimitError` + `Retry-After` long → bucket IP, rotation ;
   - `Free usage limit reached for <model>. Retry after 42s` → rate-limit court, backoff ;
   - `FreeTierError` / 403 → wire, replay exact ;
   - `502 all tunnels failed` → tunnel, pas quota.
2. **Replay exact 2.2.5 `cli`** sur une IP connue : 16 en-têtes dans l'ordre, 13 outils complets, pas de `tool_choice`, `max_completion_tokens`, TLS 1.19.5, date du jour, project `global`/dérivé IP.

```powershell
python scripts/test_free_egress.py --proxy direct --model deepseek-v4.1-flash-free --repeat 1
python scripts/test_free_egress.py --station 3 --model deepseek-v4.1-flash-free --repeat 1
```
3. Si 200 + contenu → canary 10 requêtes → 2 stations → 6 stations.
4. Si 403/429 → A/B ciblé, une variable à la fois :
   - A : proxy actuel (hybride) ;
   - B : 2.2.5 `cli` exact ;
   - C : 2.2.5 `desktop` exact ;
   - D : 1.19.5 exact (rollback) ;
   - E : placeholders vs définitions complètes ;
   - F : date figée vs courante, chemin réel vs synthétique, project `global` vs réel.
5. 20 échantillons minimum par variante, témoin intercalé ; mesurer 200/403/429 court/429 long/502, `Retry-After`, TTFT, contenu non vide.
6. Consigner dans `specs/003-official-client-parity/measurements.md`.

**Critère de sortie** : un 200 + contenu est reproduit sur une IP, puis 10/10 ; on sait si D1/D2/D3/D6 sont fonctionnels. Sans ce résultat, ne pas changer le défaut.

### Phase 1 — Profil unique et cible 2.2.5 (P0)

1. Créer `free/wire_profile.py` : dataclass, `resolve_profile`, chargement/mémoïsation du manifeste.
2. Créer `free/impersonation_v2.json` (ou étendre `free/impersonation.json` avec `schema: v2`) et un chargeur.
3. Brancher `_official_free_headers` et `_free_wire_body` sur le profil ; supprimer les décisions locales d'UA/client/ordre.
4. Ajouter la constante d'ordre 2.2.5 complète et l'utiliser dans `_free_fp_override`.
5. Geler `ROTATION_VERSIONS` sur `("2.2.5",)` ou forcer `OPENCODE_CLIENT_VERSION=2.2.5` ; garder 1.19.5 en rollback explicite.
6. Ajouter le profil TLS 2.2.5 (réutiliser `_JA3_BUN_114` / Bun 1.4.4).
7. Test de cohérence : `(version, ua, client, tls, header_order, body_keys, tools)` identique au profil.

**Déjà fait (2026-11-06)** : ordre 2.2.5 `_OPENCODE_HEADER_ORDER_225` + passage des en-têtes à `_get_free_fp_kwargs` (wire e2e exact, test `test_official_wire_e2e.py`), project `global` préservé, trace ID stable par session, `clear_model_global`, et `ROTATION_VERSIONS` gelé sur `1.19.5` (seul TLS identique au 2.2.5). Restent : template de corps versionné, prompt templaté, et le test de tuple complet.

**Critère de sortie** : le diff normalisé proxy ↔ `2.2.5-1.json` est vide sur les noms, l'ordre, les clés, les outils et les schémas ; le test e2e per-IP passe.

### Phase 2 — Privacy et sémantique (P0/P1)

1. Supprimer l'override project `global` ; choisir `global` ou dérivé IP.
2. Templater le prompt système ; test unitaire qui interdit le chemin réel et la date figée.
3. Stocker `trace_id` dans l'identité ; test sur deux requêtes.
4. Passer la session en TTL long / par conversation ; test de conversation > 30 min.
5. Aligner `x-opencode-directory` avec le cwd synthétique, ou le retirer.

**Critère de sortie** : aucun chemin réel, aucun project réel, trace stable, session stable.

### Phase 3 — Outillage et tests (P1)

1. Réécrire `scripts/opencode_client_sync.py` :
   - supporter `--cli` desktop et npm ;
   - détecter `--standalone` par `--help` ou par version ;
   - capturer `title` + `primary` (+ `compaction` si présent) ;
   - persister `header_order`, `client`, `project`, `tls`, `body_keys`, `tool_names`, `body_sha256` ;
   - `--check` retourne 2 si le manifeste est incomplet ou si le diff est non vide.
2. Ajouter `scripts/opencode_wire_diff.py` : normalise IDs/trace/Content-Length et imprime un diff lisible.
3. Ajouter `tests/test_official_wire_e2e.py` : capture locale curl_cffi sur le chemin per-IP, diff contre le fixture.
4. Mettre à jour `tests/test_official_client_parity.py`, `tests/test_identity_generator.py`, `tests/test_free_wire_complet.py` :
   - supprimer `OFFICIAL_ORDER` hybride ;
   - tester le chemin per-IP ;
   - tester le tuple de cohérence ;
   - séparer 2.2.5 et 1.19.5.
5. CI : `pytest` + `opencode_client_sync.py --check` (hors réseau si possible, fixtures).

**Critère de sortie** : un changement d'UA, d'ordre ou d'outil fait échouer un test ; le sync détecte une release.

### Phase 4 — Mode `compat` propre (P1/P2)

1. Séparer `official` et `compat` dans le profil.
2. En `compat`, utiliser les 13 définitions complètes + outils client ; mesurer le budget.
3. Supprimer les placeholders par défaut (ou les garder derrière un flag `OPENCODE_FREE_PLACEHOLDERS=1`).
4. Ajouter un log structuré : mode, profil, nombre d'outils, taille du corps, hash du système.
5. Documenter dans `docs/FREE_PROBE_FORMAT.md` et `docs/identity-generator.md`.

**Critère de sortie** : le mode est visible dans les logs ; les définitions sont réalistes ; l'A/B confirme.

### Phase 5 — Diversité versionnée (P3, optionnelle)

1. Capturer et stocker les profils complets 1.18.31 / 1.19.0 / 1.19.3 / 1.19.5 (headers, ordre, corps, titre, TLS, IDs).
2. Ajouter `profile` au vault ; `version_for(ip)` retourne un profil complet, pas un label.
3. Test de cohérence par tuple pour chaque version.
4. Réactiver la rotation de versions seulement si chaque tuple est prouvé.

**Critère de sortie** : 4 profils complets, 4 tests de cohérence, A/B sans régression.

---

### Phase 6 — Campagne de tests complète (transverse)

La campagne détaillée est dans `docs/PLAN_RETABLISSEMENT_FREE_2026-11-05.md` §5. Elle doit être exécutée **après chaque phase** et avant le canary 6 stations.

| Niveau | Ce qu'on vérifie | Commande / preuve |
|---|---|---|
| Unitaire | ordre 16 en-têtes, tuple cohérent, project `global`, prompt sans chemin, trace stable, corps 13 outils | `pytest tests/test_official_client_parity.py tests/test_free_wire_complet.py tests/test_free_official_payload.py tests/test_tls_empreinte_binaire.py tests/test_per_ip_identity.py -q` |
| Contrat | diff normalisé proxy ↔ `2.2.5-1.json` et ↔ `1.19.5-1.json` | `scripts/opencode_wire_diff.py` |
| E2E local | wire réel per-IP via curl_cffi, ordre compris | `python .artifacts/audit-2026-11-05/probe_e2e.py` |
| TLS | ClientHello local, JA3/MD5 | `python .artifacts/audit-2026-11-05/capture_tls.py ...` |
| Intégration | SSE→JSON, non-stream, titre, conversation > 30 min, deux IP isolées | pytest + canary |
| Canary live | 1 IP, 1 puis 10 requêtes, 200 + contenu | runbook §4 |
| Charge | 6 stations, 8 requêtes/station, pas de hedge storm | runbook §5.5 |
| Privacy | aucun chemin/project/AGENTS.md/ID partagé | `rg` sur les requêtes sortantes |
| Rollback | 1.19.5, legacy, header_order off | runbook §5.7 |

**Critère de sortie** : tous les niveaux verts, 10/10 canary, puis 6/6 stations.

---

## 7. Fichiers touchés (prévisionnel)

| Fichier | Changement |
|---|---|
| `free/wire_profile.py` | nouveau : dataclass + resolver |
| `free/impersonation.json` | schema v2, profils, ordre, client, project, tls |
| `free/official_payload.json` | référencé par hash ; date/cwd séparés |
| `free/identities.py` | `VERSION_SPECS` → profils ; `official_provider_headers` reçoit le profil |
| `free/impersonation.py` | `apply_spec` → `apply_profile` (ordre, trace, project) |
| `opencode.py` | `_official_free_headers`, `_free_wire_body`, `_free_fp_override`, `_get_free_fp_kwargs` |
| `scripts/opencode_client_sync.py` | canal-aware, multi-requêtes, ordre, `--check` |
| `scripts/opencode_wire_diff.py` | nouveau : diff normalisé |
| `tests/test_official_client_parity.py` | wire réel per-IP, tuple |
| `tests/test_official_wire_e2e.py` | nouveau : capture locale |
| `tests/test_identity_generator.py` | profils cohérents, pas 1.19.5 + corps 2.2.5 |
| `tests/test_free_wire_complet.py` | ordre 2.2.5 réel |

---

## 8. Critères d'acceptation

0. **Rétablissement** : 200 + contenu sur une IP avec le Plan A, puis 10/10, puis 6/6 stations client-utilisables ; zéro 502 sur le chemin client.
1. **Wire 2.2.5 `cli`** : diff vide contre `2.2.5-1.json` (noms, ordre, clés, outils, schémas), hors valeurs variables normalisées.
2. **TLS** : JA3/MD5 du profil 2.2.5 = `1523504b...` ; 1.18.31 = `d871d02...` si conservé.
3. **Cohérence** : aucun tuple `(version, ua, client, tls, body)` impossible ; test dédié.
4. **Privacy** : aucun chemin réel, aucun project réel, aucun ID partagé entre IP.
5. **Trace** : même trace ID sur deux requêtes d'une session ; spans différents.
6. **Session** : pas de rotation en cours de conversation.
7. **Outils** : 13 définitions réalistes en `compat` ; pas de `properties: {}` sans preuve.
8. **Sync** : `--check` détecte une release et échoue sur manifeste incomplet.
9. **A/B** : pas de régression de 200 vs baseline (≥ 20 échantillons par variante).
10. **Rollback** : `OPENCODE_FREE_WIRE_STYLE=legacy` et `OPENCODE_CLIENT_VERSION=1.19.5` fonctionnent sans redéploiement.

---

## 9. Mesure et preuve

Le plan ne considère pas un wire conforme comme un succès. La preuve minimale est :

- **forme** : diff normalisé vide (Phase 1/3) ;
- **contenu** : 200 + contenu non vide + pas de trame `event: error` (Phase 0) ;
- **non-régression** : taux de 200 de la variante B ≥ baseline A, sur la même IP et la même fenêtre ;
- **privacy** : grep des requêtes sortantes (chemin, project, IDs) vide.

Les résultats sont stockés dans `specs/003-official-client-parity/measurements.md` avec les captures brutes dans `.artifacts/audit-<date>/`.

---

## 10. Risques et rollback

| Risque | Mitigation | Rollback |
|---|---|---|
| Le gate exige le prompt exact | A/B date seule puis cwd ; garder le prompt figé | `OPENCODE_FREE_SYSTEM_TEMPLATE=off` |
| Les 13 définitions complètes dépassent le budget | mesurer la taille ; ajuster `free.max_wire_bytes` | placeholders derrière un flag |
| Le profil 2.2.5 `cli` ne passe pas | garder 1.19.5 en rollback | `OPENCODE_CLIENT_VERSION=1.19.5` |
| L'ordre 2.2.5 casse curl_cffi | tester `header_order` sur HTTP et HTTPS | `OPENCODE_FREE_HEADER_ORDER_ON=0` |
| La rotation de version était utile à l'anti-corrélation | conserver IP/session/project par IP | réactiver Phase 5 |

---

## 11. Complexity Tracking

| Violation | Pourquoi | Alternative rejetée |
|---|---|---|
| Un manifeste + un resolver | 4 sources divergent aujourd'hui ; le tuple doit être prouvé | garder les constantes : impossible de garantir la cohérence |
| Cible unique 2.2.5 au lieu de 4 versions | les profils complets n'existent pas ; un hybride est pire qu'un profil unique | rotation actuelle : combinaisons impossibles |
| Templates de prompt | fuite du chemin réel + date figée | garder le payload figé : fuite et tell |
| Test e2e curl_cffi | les tests unitaires valident un chemin qui n'est pas la production | tests de dict : ne voient pas l'ordre libcurl |
