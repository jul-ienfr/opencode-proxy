# Format des probes free

> 2026-11-02. Contrat vérifié sur le code et sur la capture officielle
> `free/official_payload.json` (client OpenCode 1.19.5, desktop).

## 1. Corps d'inférence officiel

Toute probe qui teste le **gate free** (`StationProbe`, chaînes
`fresh_chain`, canary, discover) envoie la copie officielle complète :

- 11 outils, dans cet ordre exact :
  `bash, edit, glob, grep, read, skill, task, todowrite, webfetch, websearch, write`
- prompt système officiel (21 604 octets, « You are opencode… »)
- `stream: true`
- `stream_options: {"include_usage": true}`
- `max_tokens: 32000`
- `tool_choice: "auto"`
- `model` remplacé par le modèle testé, sans toucher aux autres clés

Une version réduite (un seul outil, pas de prompt système, outils triés,
`stream: false`) est refusée par le gate. Le probe ne filtre ni ne trie cette
grille : `_free_wire_body()` ne trie que les outils **fournis par le client**
sur la jambe générale, jamais la copie officielle injectée.

Le seul nom retiré avant l'envoi free est `subagent_fork` /
`subagent-fork` (refus déguisé en quota). Il n'est pas dans la grille
officielle.

Mesures de bisection à ne pas reproduire dans une probe :

- un seul outil (`read` seul, `bash` seul) → 403 ;
- paires partielles `[bash,grep]`, `[bash,edit]`, `[read,edit]` → 403 ;
- moitié de grille `[task..write]` → 403 ;
- `safe8 + subagent_fork` → 429 déguisé.

La probe doit donc envoyer la grille complète, jamais une version réduite
« pour aller plus vite ».

La copie du projet est la capture 1.19.5 avec `websearch` (11 outils). Un
client npm par défaut sans websearch en envoie 10 : `websearch` est un extra
officiel toléré, pas un outil obligatoire. La probe utilise exactement la
même copie que la jambe free générale (11), pour que test et production
parlent le même wire.

## 2. En-têtes officiels

Profil 1.19.5 / desktop :

```text
Authorization: Bearer public
Content-Type: application/json
User-Agent: opencode/1.19.5 ai-sdk/provider-utils/4.0.23 runtime/bun/1.4.4
x-opencode-client: desktop
x-opencode-project: global | <40-hex project ID>
x-opencode-request: msg_…
x-opencode-session: ses_…
x-opencode-session-id: ses_…
Connection: keep-alive
Accept: */*
Host: opencode.ai
Accept-Encoding: gzip, deflate, br, zstd
```

Ordre wire 1.19.x : `Authorization, Content-Type, User-Agent,
x-opencode-client, x-opencode-project, x-opencode-request,
x-opencode-session, x-opencode-session-id, Connection, Accept, Host,
Accept-Encoding, Content-Length`.

1.18.31 n’envoie **pas** `x-opencode-session-id` : 12 en-têtes, avec
`Authorization … x-opencode-session, Connection, Accept, Host,
Accept-Encoding, Content-Length`.

Un client officiel envoie `global` **hors dépôt git**, et un project ID
40 hex dans un dépôt (`.git/opencode`, hash du remote ou commit racine). La
probe applique exactement la même dérivation que la jambe générale :
`project_for(egress_ip, _OPENCODE_PROJECT, 0)`. Elle ne réutilise jamais la
session réelle (identité `persist=False`), mais le project ID correspond à
celui qu’enverrait le client pour cette IP.

Le handler free ne lit pas `x-opencode-project` et le supprime avant l’appel
au modèle : ce champ ne crée pas de bucket de quota et ne peut pas, à lui
seul, provoquer un 429. Il est copié par fidélité, pas par nécessité.

## 3. Octets exacts du corps

Le client officiel (Bun) émet du **JSON compact UTF-8** :
séparateurs `,`/`:`, aucun espace, aucun `indent`, clés dans l'ordre du
client, caractères UTF-8 non échappés.

`curl_cffi` avec `json=` sérialise en `indent=2` : c'est **interdit** pour la
jambe free. La probe envoie `content=` avec :

- le corps brut capturé `_body_raw` quand il est présent (substitution du
  modèle par remplacement du littéral JSON, pas de re-sérialisation) ;
- sinon `_serialize_json_body()` (orjson compact), la même fonction que la
  jambe free générale.

Preuves :

- capture 1.19.5 : `Content-Length: 46010` ; `orjson.dumps(body)` = 46 010
  octets ;
- captures fraîches 1.18.31 et 1.19.5 : `_body_raw == orjson.dumps(body)`
  octet pour octet (SHA-256 identique).

## 4. Impersonation TLS

Les probes utilisent le même replay Bun que la jambe free générale :

- `curl_cffi` avec `chrome131` comme porteur des comportements natifs ;
- JA3/JA4 et sigalgs 1.19.5 ;
- `http_version: v1` ;
- `header_order` 1.19.5 ;
- `tls_grease: false`.

Fonction de référence : `opencode._get_free_fp_kwargs(version)`.

La probe ne fige plus 1.19.5 : elle choisit une version officielle
**déterministe par egress IP** parmi `1.19.5, 1.19.3, 1.18.31, 1.19.0`
(`PROBE_VERSIONS`). La même station garde le même profil ; deux stations
peuvent légitimement ressembler à deux installations officielles différentes.
Les en-têtes/IDs viennent de `free.identity_generator.generate_profile()`, et
le TLS (`ja3`, sigalgs, Bun, header_order) de `_get_free_fp_kwargs(version)` :
les deux sont toujours appariés à la même version.

## 5. Un 429 ne prouve pas que le format est bon

Le gate peut renvoyer `FreeUsageLimitError` / 429 pour un vrai quota **ou**
pour un refus de forme déguisé (corps réduit, outils dans le mauvais ordre,
version/TLS/en-têtes non officiels). Un 403 n'est donc pas la seule signature
d'un format invalide, et un 429 n'est pas une preuve d'acceptation.

Pour conclure, il faut la même IP, au même moment, avec la copie officielle
exacte en contrôle : si le contrôle exact prend 429, c'est le quota ; si le
contrôle exact prend 200 et la variante 429, c'est la forme. C'est pour cela
que la probe envoie la grille complète et le replay TLS officiel, et pas une
version « minimale » plus rapide.

## 6. Qui utilise quoi

| Probe | Corps | Impersonation |
|---|---|---|
| `StationProbe` (gate free) | copie officielle complète | oui, 1.19.5 |
| `fresh_chain` / canary / discover | via `StationProbe` | oui |
| `free.probe.probe_station` (qualité, `robots.txt`) | GET zéro quota | oui, même replay |
| dashboard SOCKS5/HTTPS test | GET `api.ipify` + `opencode.ai` | oui, même replay |
| `nordvpn_sources_probe` | sources documentaires | non concerné |

Les probes de connectivité n'envoient pas le corps d'inférence : ce sont des
GET zéro quota. Elles utilisent néanmoins le même TLS/headers de transport
pour ne pas être refusées sur la seule signature réseau.
