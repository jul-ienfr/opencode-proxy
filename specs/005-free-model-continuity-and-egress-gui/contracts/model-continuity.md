# Contrat — Continuité du modèle free

**Statut**: conception. Aucun code dans cette itération.

## Entrées

| Source | Contenu |
|---|---|
| `FREE_MODELS` | ids gratuits actuellement routés |
| `FREE_MODEL_MAP` | mappings explicites payant → free |
| `FREE_DISCOVERY_DEFAULT_TARGET` | target runtime |
| `config/free_models_probed.json` | verdicts `alive/limited/dead`, statut, raison, date |
| `config.yaml` `free_discovery` | `default_target`, `auto_default_target`, `preference`, `pinned_target` |

**Ne pas fetcher le catalogue.** La découverte existante
(`ensure_free_models_on_boot`, probe) est la seule source.

## Sortie

`ModelSelection` :

| Champ | Type | Rôle |
|---|---|---|
| `target` | string | modèle free actif |
| `source` | `config`/`auto`/`pinned`/`none` | origine |
| `previous` | string | ancien target |
| `reason` | string | `kept`, `missing`, `dead`, `no_alive`, `pinned` |
| `changed_at` | timestamp | date du changement |

## Algorithme

```
si pinned_target : return pinned
si target ∈ FREE_MODELS et probe(target) != dead : return kept
candidats = [m ∈ FREE_MODELS | probe(m) == alive]
si candidats vides : return no_alive (aucune écriture)
score = (même_famille, preference_index, -probe_age, id)
choisir le meilleur
si auto_default_target : persister free_discovery.default_target
propager FREE_DISCOVERY_DEFAULT_TARGET
```

## Règles

- **MC-01** Une entrée explicite de `FREE_MODEL_MAP` n'est jamais modifiée.
- **MC-02** Aucun modèle payant n'est choisi.
- **MC-03** `limited` et `unproven` ne sont pas choisis s'il existe un `alive`.
- **MC-04** Si aucun `alive`, l'état `no_free_model` est exposé ; le target
  courant n'est pas supprimé.
- **MC-05** La persistance est atomique (`config.yaml` + `.tmp` + replace).
- **MC-06** Un verrou empêche deux réconciliations concurrentes.
- **MC-07** Le resolver ne tourne qu'après la fin d'un cycle de découverte/probe
  (ou sur demande explicite).
- **MC-08** L'ancien target est conservé dans `previous` pour rollback.

## Persistance

```yaml
free_discovery:
  default_target: <target>
  auto_default_target: true
  pinned_target: ""        # non vide = pin, pas de remplacement
  preference:
  - deepseek-v4.1-flash-free
  - glm-5.3-flash-free
  - big-pickle
```

## API

| Méthode | Route | Effet |
|---|---|---|
| `POST` | `/api/free-models/reconcile` | force la réconciliation, renvoie `ModelSelection` |
| `PUT` | `/api/free-models/default` | `{"mode":"auto"}` ou `{"mode":"pin","target":"..."}` |
| `GET` | `/api/free-models` | ajoute `active_default`, `default_source`, `last_switch`, `pinned` |

## Échecs

| Cas | Réponse |
|---|---|
| catalogue/probe indisponible | `503 catalog_unavailable`, aucune écriture |
| aucun `alive` | `409 no_free_model`, target conservé |
| target pinné absent | `409 pinned_unavailable`, pas de remplacement silencieux |
| écriture config impossible | `500 persist_failed`, runtime inchangé |

## Tests

- target `alive` → conservé, aucune écriture ;
- target absent → remplaçant `alive` choisi et persisté ;
- target `dead` → remplaçant ;
- aucun `alive` → `no_free_model`, pas de payant ;
- mapping explicite → jamais réécrit ;
- deux réconciliations concurrentes → une seule écriture ;
- GUI : `active_default`/`source`/`previous` corrects.
