# Contrat — Isolation d'identité par station

**Statut**: conception. Aucun code dans cette itération.  
**Priorité**: P0 — bloquant avant d'activer plusieurs stations.

## 1. Modèle de menace

Le provider (opencode.ai) voit, pour chaque requête :

- les en-têtes d'identité OpenCode ;
- le corps (prompt système, messages, outils) ;
- le TLS/ALPN et l'ordre des en-têtes ;
- l'IP/bloc d'egress ;
- le timing.

Il ne doit pas pouvoir déduire que deux stations sont la **même
installation** (même machine, même workspace, même utilisateur). Il peut
voir que deux requêtes utilisent le même modèle ou le même contenu — c'est
inévitable pour une conversation — mais pas un identifiant d'installation
commun.

## 2. Tuple d'identité par station

`StationIdentity(label)` :

| Champ | Génération | Contrainte |
|---|---|---|
| `project` | `HMAC-SHA256(secret, "project:"+label).hex()[:40]` | 40 hex, unique par station, jamais `global`/hash dépôt |
| `ses_` | mint canonique `…ffe`, état isolé par station, par `(station, conversation)` | 30 chars, jamais partagé |
| `msg_` | mint ascendant par requête | frais à chaque envoi |
| `trace` | `HMAC-SHA256(secret, "trace:"+label).hex()[:32]` | stable par station, jamais partagé |
| `span` | aléatoire par requête | par requête |
| `env_cwd` | `/workspace/<safe(label)>` | synthétique, pas de chemin réel |
| `ua` | profil vérifié (aujourd'hui 1.19.5) | partagé si un seul profil |
| `client` | `cli` canonique (capture 2.2.5) ; `desktop` hérité migré | partagé par conception |

`secret` : 32 octets aléatoires persistés dans `logs/identity_secret`
(0600, gitignoré), jamais envoyés.

## 3. Règles par vecteur

### 3.1 En-têtes

| En-tête | Règle |
|---|---|
| `Authorization` | `Bearer public` (partagé par conception) |
| `x-opencode-project` | **unique par station** |
| `x-opencode-session` | **unique par (station, conversation)** |
| `x-opencode-session-id` | même valeur que `x-opencode-session` |
| `x-session-affinity` | même valeur |
| `x-session-id` | même valeur |
| `x-opencode-request` / `msg_` | frais par requête |
| `b3` | trace de la station, span de la requête |
| `traceparent` | même trace, même span |
| `x-opencode-client` | valeur canonique du profil (capture 2.2.5 : `cli`) ; ne pas mélanger avec `desktop` |
| `x-opencode-org-id` | absent (free anonyme) |
| `x-forwarded-for` / `cf-*` / `x-real-ip` | absents |

Les profils **legacy18** (1.18.31) et **legacy19** (1.19.3) n'envoient ni
`x-opencode-project`, ni `x-opencode-request`, ni `b3`/`traceparent` : c'est
le wire réel capturé. Leur isolation repose sur `x-session-affinity` /
`x-session-id` (et `x-opencode-session-id` en 1.19.3), l'UA et le TLS. Le
quartet complet n'est utilisé que par 2.2.5/1.19.5.

### 3.2 Corps / prompt

Le prompt officiel est **templaté** :

```
You are an AI agent running in OpenCode, a coding agent harness. ...
Today's date: <UTC courante>
<env>
  Working directory: /workspace/<safe(label)>
</env>
```

Interdits dans le prompt envoyé :

- `C:\Users\`, `Downloads`, le vrai workspace ;
- le nom d'utilisateur réel (`<username>`) ;
- le hostname ;
- le hash du dépôt / project réel ;
- `Platform: win32`, `Is directory a git repo: yes`,
  `Workspace root folder`, `Prefer ...` ;
- le bloc `Instructions from:` et le contenu `AGENTS.md` (chemin réel +
  règles locales uniques) ;
- la date de capture (`Wed Nov 04 2026`).

Le bloc `<env>` reste littéral et contient `Working directory:` (gate).

### 3.3 Sessions curl / connexions

- une clé de pool curl par station (`label`/`egress`), pas de session
  partagée entre deux stations ;
- pas de cookies partagés ;
- pas de connexion keep-alive réutilisée entre stations.

### 3.4 Conversations

- clé d'affinité : `sha256(model | system | first_user | conversation_id)` ;
- la conversation reste sur **une** station, donc un `ses_`/project ;
- le round-robin ne s'applique qu'aux conversations nouvelles ou sans
  historique ;
- TTL `identity.affinity_ttl_s` (défaut 3600) ;
- failover uniquement si la station est inutilisable ; nouvelle identité,
  événement `conversation_failover` journalisé ;
- ne jamais envoyer le même historique en parallèle à deux stations.

## 4. Audit

`IdentityAudit` :

1. construire les tuples de toutes les stations actives ;
2. vérifier que `project`, `ses_`, `msg_`, `trace` sont **disjoints** ;
3. scanner le prompt templaté contre les sous-strings interdites ;
4. vérifier qu'aucune session curl/cookie n'est partagée ;
5. vérifier que `direct` désactivé n'est pas utilisé ;
6. résultat : `ok`, `duplicate_identity`, `prompt_leak`, `shared_session`.

Une station en échec est exclue du pool ; si l'audit échoue au démarrage,
le pool reste sur les stations valides (fail-safe, pas de fallback direct
silencieux).

## 5. Vecteurs partagés par conception

| Vecteur | Partagé | Justification |
|---|---|---|
| `Bearer public` | oui | free anonyme |
| TLS JA3/JA4 Bun | oui | un seul profil vérifié |
| UA/version | oui (aujourd'hui) | un seul profil vérifié |
| `x-opencode-client` | oui | valeur canonique unique (`cli` capture 2.2.5) |
| IP/bloc | non | par station |
| `project`/`ses_`/`msg_`/trace | non | par station |
| prompt/`<env>` | non | synthétique par station |

Si plusieurs profils UA/TLS vérifiés existent, le wrapper peut les répartir
par station ; ce n'est pas requis pour l'isolation des IDs.

**Risque résiduel explicite** : tant que le TLS/JA3 et l'UA sont partagés,
l'isolation des identifiants n'équivaut pas à une unlinkability réseau
complète. Le provider voit encore une empreinte cliente commune. L'audit DOIT
exposer `shared_vectors` (`tls_ja3`, `ua`, `client`, `bearer`) pour que la GUI
et les rapports ne prétendent pas à une isolation totale. La diversité UA/TLS
par station est traitée par le registre de profils vérifiés
([verified-profiles.md](./verified-profiles.md)), pas par une supposition.

## 6. Matrice de tests

| Test | Attendu |
|---|---|
| deux stations, même modèle | `project`/`ses_`/`trace` disjoints |
| 5 tours d'une conversation | même station, même `ses_` |
| 5 conversations distinctes | peuvent tourner sur 5 stations |
| prompt d'une station | contient `/workspace/<label>`, pas le vrai chemin |
| scan de sous-strings | aucun `C:\Users\`, `<username>`, hostname, hash |
| deux stations, même label | refus/config invalide |
| station dupliquée | audit `duplicate_identity`, exclue |
| failover | une seule nouvelle identité, log, pas de parallèle |
| sessions curl | clés distinctes par station |
| `direct` désactivé | aucun `socks5:direct` |

## 7. Critères d'acceptation

- **II-01** Deux stations n'ont aucun ID commun.
- **II-02** Une conversation reste sur une station.
- **II-03** Le prompt ne contient aucun marqueur de la machine réelle.
- **II-04** L'audit bloque/exclut une station qui viole II-01/II-03.
- **II-05** Les vecteurs partagés par conception sont documentés et limités.
- **II-06** Le failover ne duplique pas l'historique en parallèle.
