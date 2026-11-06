# Contrat — Garde `<env>`

**Fonctions**: `_free_env_block_valid`, `_free_ensure_env_block`,
`_free_wire_body` (`opencode.py`).  
**Source**: probes rianilham28/opencode2api (2026-10-25, re-capturé 2026-11-05)
et xljiulang/opencode-proxy (2026-11-06), reproduits par nos tests.

## Règle

Le gate free exige, dans **un** message système :

```
<env>
  Working directory: <valeur libre>
</env>
```

Contraintes :

- tags littéraux `<env>` et `</env>` ;
- ligne `Working directory:` exacte (casse comprise) ;
- valeur libre (`/`, `/tmp`, métadonnée) ;
- **seul le premier couple compte** : un `<env></env>` vide, ou
  `<env x="1">`, devant un couple valide empoisonne la requête ;
- le bloc peut être n'importe où dans le message (notre prompt officiel le
  place au milieu) ;
- un couple valide dans un message système suffit.

## Algorithme

```
pour chaque message système, dans l'ordre :
    trouver le premier "<env"
    si absent : continuer
    si le token n'est pas exactement "<env>" : retourner INVALIDE
    trouver le premier "</env>" après lui
    si absent : retourner INVALIDE
    retourner "Working directory:" ∈ contenu entre les tags
retourner INVALIDE
```

Si invalide :

```
insérer en tête un message système :
    {"role":"system","content":"<env>\n  Working directory: /\n</env>"}
```

Le prompt officiel n'est **jamais** modifié.

## Exemples

| Contenu système | Verdict |
|---|---|
| `<env>\n  Working directory: /\n</env>` | valide |
| `You are…\n\n<env>\n  Working directory: /workspace\n</env>` | valide |
| `<env></env>\n<env>\n  Working directory: /tmp\n</env>` | **invalide** |
| `<env x="1">\n  Working directory: /tmp\n</env>` | **invalide** |
| `<env>\n  Working directory: /tmp\n` (pas de fermeture) | **invalide** |
| `<environment>\n  Working directory: /tmp\n</environment>` | **invalide** |
| `<env>\n  Working Directory: /tmp\n</env>` (majuscule) | **invalide** |
| aucun message système | **invalide** → insertion |

## Tests

- `test_env_guard_adds_block_when_official_marker_has_no_env`
- `test_env_guard_keeps_existing_valid_block`
- `test_env_guard_rejects_empty_first_pair_before_valid_one`
- `test_env_guard_rejects_malformed_first_tag`

## Divergence connue

`alztrk` exige que le contenu **commence** par `<env>` ; c'est plus strict que
la mesure. Notre prompt officiel place le bloc au milieu et passe. Ne pas
adopter cette contrainte.
