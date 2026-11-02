# Générateur d'identités OpenCode (free)

Ce document décrit la matrice d'impersonation mesurée sur les clients officiels
OpenCode et le générateur `free/identity_generator.py`.

## Matrice mesurée (captures mitm, 2026-11-01)

Le provider `opencode` reçoit **le même corps** sur les quatre versions :

```text
body principal : model, max_tokens=32000, messages, tools, tool_choice, stream, stream_options
tools          : 11 — bash, edit, glob, grep, read, skill, task, todowrite, webfetch, websearch, write
système        : 21 604 caractères
titre          : 2 477 octets, 0 outil, temperature=0.5, reasoning_effort=low
```

Les différences sont dans les en-têtes :

| Version | UA / Bun | `x-opencode-session-id` | Client observé |
|---|---|---|---|
| 1.18.31 | `opencode/1.18.31 ... runtime/bun/1.3.14` | non | `desktop` |
| 1.19.0 | `opencode/1.19.0 ... runtime/bun/1.3.14` | oui | `desktop` |
| 1.19.3 | `opencode/1.19.3 ... runtime/bun/1.3.14` | oui | `desktop` |
| 1.19.5 | `opencode/1.19.5 ... runtime/bun/1.4.4` | oui | `desktop` |
| 1.18.35 | inférée de la branche 1.18.x | non | non capturé |

Ordre exact des en-têtes (1.19.0+) :

```text
Authorization
Content-Type
User-Agent
x-opencode-client
x-opencode-project
x-opencode-request
x-opencode-session
x-opencode-session-id
Connection
Accept
Host
Accept-Encoding
Content-Length
```

Aucun `x-session-affinity`, `x-session-id`, `x-opencode-directory` ou
`x-opencode-workspace` n'est envoyé sur l'inférence du provider `opencode`.

## TLS (ClientHello capturé par tunnel CONNECT)

Le ClientHello est en clair avant TLS : un tunnel TCP suffit, sans MITM ni
certificat intercepté. Deux familles Bun :

| Version | Bun | JA3-MD5 | Courbes | Sigalgs | Padding ext 21 |
|---|---|---|---|---|---|
| 1.18.31 / 1.18.35 | 1.3.14 | `d871d02cecbde59abbf8f4806134addf` | 29-23-24 | 9 | oui |
| 1.19.0 / 1.19.3 | 1.3.14 | `d871d02cecbde59abbf8f4806134addf` | 29-23-24 | 9 | oui |
| 1.19.5 | 1.4.4 | `1523504b38f0fae0d881d4b6554aac1b` | 4588-29-23-24 | 11 | non |

JA3 1.19.5 :

```text
771,4865-4866-4867-49195-49199-49196-49200-52393-52392-49161-49171-49162-49172-156-157-47-53,0-23-65281-10-11-35-16-5-13-18-51-45-43,4588-29-23-24,0
```

Sigalgs 1.19.5 : `ecdsa_secp256r1_sha256, rsa_pss_rsae_sha256,
rsa_pkcs1_sha256, ecdsa_secp384r1_sha384, rsa_pss_rsae_sha384,
rsa_pkcs1_sha384, rsa_pss_rsae_sha512, rsa_pkcs1_sha512, ed25519,
ecdsa_secp521r1_sha512, rsa_pkcs1_sha1`. GREASE = false ; ALPN = `http/1.1`.

Vérifié sur le wire proxy :

- profil 1.19.5 → JA3-MD5 `1523504b…` identique au client officiel ;
- profil 1.19.3 → JA3-MD5 `d871d02c…` identique au client officiel.

## Utilisation par la probe

`StationProbe` (`free/ip_registry/adapters/probe.py`) appelle
`generate_profile()` avec une version **déterministe par egress IP** parmi
`1.19.5, 1.19.3, 1.18.31, 1.19.0`. Le profil fournit les en-têtes et les IDs ;
`opencode._get_free_fp_kwargs(version)` fournit le TLS/JA3/Bun/header_order
apparié. Une même IP garde son profil ; deux stations peuvent donc ressembler
à deux installations officielles différentes, sans identité aléatoire par
requête.

## Générer des profils

```bash
# Un profil 1.19.5
python -m free.identity_generator --version 1.19.5 --client desktop

# Quatre versions, client desktop, IDs frais à chaque profil
python -m free.identity_generator \
  --versions 1.18.31,1.19.0,1.19.3,1.19.5 \
  --clients desktop --count 4 --json

# Profil avec le corps officiel complet
python -m free.identity_generator --version 1.19.5 --include-body

# Plusieurs clients (cli/app sont dans l'enum binaire, non capturés live)
python -m free.identity_generator --versions 1.19.5 --clients desktop,cli --count 4
```

Chaque profil contient : version, client, `verified`, Bun, présence de
`x-opencode-session-id`, IP, génération, project, session, en-têtes ordonnés et
résumé du corps (11 outils / 21 604 chars).

`generate_many()` alterne versions et clients, et incrémente la génération :
le project, le `ses_` et le `msg_` sont neufs à chaque profil. Le générateur
n'écrit rien sur disque (`persist=False`).

## Utilisation par le proxy

Le proxy et le générateur partagent **la même primitive** :
`IpIdentity.official_provider_headers()`.

- `opencode._official_free_headers()` l'appelle pour chaque IP d'egress ;
- `free.identities.version_for()` choisit désormais parmi les quatre versions
  mesurées (`1.19.5/1.19.3/1.18.31/1.19.0`), plus seulement les deux legacy ;
- `StationProbe` appelle `generate_profile()` avec une version déterministe
  par egress IP et préfère la version persistée du vault pour cette IP ;
- `_get_free_fp_kwargs(version)` ajuste l'ordre libcurl (12 en-têtes 1.18.x,
  13 en-têtes 1.19+) ;
- `free/official_payload.json` fournit le corps 1.19.5 (11 outils, système
  21 604 chars) ;
- une requête de titre est détectée et traverse sans grille principale.

Vérification sur serveur de capture : le proxy émet exactement les en-têtes
1.19.5 ci-dessus et un corps
`model,max_tokens,messages,tools,tool_choice,stream,stream_options` avec
11 outils et le système 21 604 chars.

## Limite actuelle

Le générateur rend l'identité HTTP parfaite, mais ne crée pas de quota free :
le provider renvoie `FreeUsageLimitError` même au client officiel 1.19.5, sur
l'IP locale comme sur les IPs VPN. Le plafond est côté service (identité free
publique / compte / origine réseau), pas dans les champs d'identité.

## Quota et reset

L'identité HTTP/TLS est distincte de l'identité de quota. En free, le provider
compte les requêtes par **IP d'egress** (`x-real-ip`, IPv6 tronquée au /64) et
reset à **00:00 UTC** ; Bun, le projet, la session et le message ne sont pas
des clés de quota. Le proxy applique ce modèle via `free/quota_ledger.py`
(bucket `(IP, jour UTC)`, persistance, reset minuit UTC). Détail, preuves et
matrice d'expériences : [`docs/quota-identity.md`](quota-identity.md).
