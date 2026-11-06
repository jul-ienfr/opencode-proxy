# Contrat — Noms d'outils et alias

**Statut**: divergence ouverte `T-409`, non implémentée sur la jambe free.

## État actuel

- Les outils client sont relayés **verbatim** dans la copie free.
- Les 13 outils officiels sont ajoutés devant ; un outil client qui porte un
  nom officiel exact est conservé à sa place, sinon un placeholder est ajouté.
- Aucun alias `fn_*` n'est appliqué sur le chemin free.
- Les chemins payant/Responses utilisent déjà `sanitize_tool_names` et
  `restore_tool_name` (`app/protocol/mapping.py`).

## Mesures

| Mesure | Résultat |
|---|---|
| `read_file` sur `deepseek-v4.1-flash-free` (2026-11-06) | **200**, `tool_calls: ["read_file"]` |
| `exec_command` sur le même modèle | **200** |
| xljiulang : `exec_command` nu | échoue (429) |
| xljiulang : `fn_execCommand_ex` | passe |
| xljiulang : `ex_exec_command` | échoue encore |

Conclusion : l'alias n'est pas une loi universelle ; il dépend du modèle ou de
la route. Ne pas l'ajouter tant qu'un modèle free ne le réclame pas.

## Si un modèle le réclame

1. Dans `_free_wire_body`, après la construction de la grille, aliaser chaque
   outil client :
   - `snake_case` → `fn_camelCase_ex` ;
   - nom trop long → troncature + hash SHA-1 8 hex ;
   - conserver la correspondance dans `_TOOL_NAME_MAP_KEY`.
2. Dans les réponses (JSON et SSE), restaurer le nom d'origine via
   `restore_tool_name`.
3. Ne jamais aliaser les 13 noms officiels : ils sont le gate.
4. Tester : un appel d'outil réel, un stream avec `tool_calls`, un tool result
   de suivi.

## Références

- `app/protocol/mapping.py` : `sanitize_tool_names`, `restore_tool_name`,
  `_TOOL_NAME_MAP_KEY`.
- `xljiulang/opencode-proxy` : `docs/PROTOCOL_NOTES.md`, section « Codex 专属
  工具名组合 ».
