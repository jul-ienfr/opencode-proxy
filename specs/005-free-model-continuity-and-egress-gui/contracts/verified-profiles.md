# Contrat — Générateur de profils vérifiés

**Statut**: conception. Aucun code dans cette itération.  
**Priorité**: P0.5 — après l'isolation d'identité, avant l'activation multi-station.

## 1. Objectif

Le générateur existant (`free/identity_generator.py`) sait produire des
profils complets, mais son drapeau `verified` est **statique** : il vient de
`VERSION_SPECS` et de `VERIFIED_CLIENTS`, pas d'une preuve live contre le
gate. Le générateur de profils **vérifiés** ajoute une couche de preuve :

- seuls des profils ayant répondu **200 avec contenu** sur plusieurs blocs
  d'egress distincts sont émis ;
- chaque profil a une provenance, une empreinte de capture, une date de
  vérification et une durée de validité ;
- un profil `stale` ou `rejected` n'est jamais assigné à une station ;
- un profil est **cohérent de bout en bout** : UA, suffixe Bun, JA3, ordre
  des en-têtes, corps, en-têtes de session appartiennent au même client.

## 2. Ce qui existe déjà

| Élément | Rôle | Limite |
|---|---|---|
| `free/identity_generator.py` | génère un profil complet (headers, TLS, corps) | `verified` statique ; corps 2.2.5 utilisé pour toutes les versions |
| `free/identities.py::VERSION_SPECS` | versions 1.18.31/1.18.35/1.19.0/1.19.3/1.19.5, Bun, JA3, sigalgs | 5 versions mais 2 familles TLS |
| `ROTATION_VERSIONS` | `("1.19.5",)` | une seule version réellement utilisée |
| `VERIFIED_CLIENTS` | `("desktop",)` | `cli`/`app` dans l'enum, non capturés live |
| `free/captures/2.2.5.json` | template 2.2.5, 13 outils, `cli` | vérifié live |
| `free/captures/1.19.5.json` | corps 1.19.5, 11 outils, `desktop` | vérifié live |
| `scripts/mint_capture.py --verify` | mint + envoi + attente 200 | vérifie une capture, pas un registre |
| `scripts/test_free_egress.py` | tests `--station`, `--mint`, `--repeat` | pas de matrice de profils |

> `VERSION_SPECS` s'arrête à 1.19.5 : le profil **2.2.5** vient de
> `free/impersonation.json` / `free/captures/2.2.5.json` et doit être décrit
> dans le registre comme une définition séparée, pas ajouté de force à
> `VERSION_SPECS` sans capture complète.

## 3. Matrice des candidats

| Profil | Client | Bun | JA3 | Corps | État actuel |
|---|---|---|---|---|---|
| 1.18.31 | desktop | 1.3.14 | `d871…` | 10 outils (`free/captures/1.18.31.json`) | **archive**, retiré du registre actif |
| 1.19.0 | desktop | 1.3.14 | `d871…` | 10 outils | non vérifié |
| 1.19.3 | desktop | 1.3.14 | `d871…` | 10 outils (`free/captures/1.19.3.json`) | **archive**, retiré du registre actif |
| 1.19.5 | desktop | 1.4.4 | `1523…` | 11 outils (`free/captures/1.19.5.json`) | **vérifié live, 2 blocs** |
| 2.2.5 | cli | 1.4.4 | `1523…` | 13 outils | **vérifié live, 2 blocs** |

### Limite structurelle

Il n'y a aujourd'hui que **deux familles TLS** dans les captures :
Bun 1.3.14 (`d871…`) et Bun 1.4.4 (`1523…`). Vérifier plusieurs profils donne
de la diversité **UA/client/corps**, pas une diversité TLS illimitée. Pour
obtenir plus de familles, il faut capturer d'autres clients officiels ou
d'autres runtimes ; il ne faut jamais inventer un JA3 ni mélanger un UA et un
TLS de versions différentes.

## 4. Modèle

### 4.1 `ProfileCandidate`

| Champ | Type | Rôle |
|---|---|---|
| `profile_id` | string | `oc-1.19.5-desktop-11` |
| `version` | string | version OpenCode |
| `client` | `desktop/cli/app` | valeur du header |
| `body_capture` | path + sha256 | corps exact de cette version |
| `tls_family` | string | `bun-1.3.14` / `bun-1.4.4` |
| `ja3` / `ja3_md5` | string | ClientHello attendu |
| `sigalgs` | list | signature algorithms |
| `header_order` | list | ordre exact |
| `session_headers` | list | quartet/trio selon version |

### 4.2 `ProfileEvidence`

| Champ | Type |
|---|---|
| `block` | `/64` ou `/32` |
| `status` | HTTP |
| `content_ok` | bool |
| `error_frame` | bool |
| `latency_ms` | int |
| `response_sha256` | string |
| `checked_at` | timestamp |
| `verdict` | `pass/quota/reject` |

### 4.3 `VerifiedProfile`

| Champ | Type |
|---|---|
| `profile_id` | string |
| `status` | `verified/stale/rejected` |
| `evidence` | list[ProfileEvidence] |
| `verified_at` / `expires_at` | timestamp |
| `source_capture` | path |
| `capture_sha256` | string |
| `tls_family` | string |
| `shared` | bool (réutilisé par plusieurs stations) |

### 4.4 `ProfileRegistry`

Fichier `free/profiles/verified_profiles.json`, versionné, écriture atomique
(`.tmp` + replace), jamais édité à la main en production.

```json
{
  "schema": 1,
  "updated_at": "2026-11-06T10:00:00Z",
  "profiles": [
    {
      "profile_id": "oc-2.2.5-cli-13",
      "version": "2.2.5",
      "client": "cli",
      "tls_family": "bun-1.4.4",
      "ja3_md5": "1523504b38f0fae0d881d4b6554aac1b",
      "body_capture": "free/captures/2.2.5.json",
      "status": "verified",
      "verified_at": "2026-11-06T10:00:00Z",
      "expires_at": "2026-11-13T10:00:00Z",
      "evidence": [
        {"block": "2a09:bac5:32d4:be::/64", "status": 200, "content_ok": true, "error_frame": false}
      ]
    }
  ]
}
```

## 5. Vérification

Pour chaque candidat :

1. **Bloc frais** : choisir un egress hors cooldown, distinct des blocs déjà
   utilisés pour ce profil.
2. **Requête minimale** : construire le wire du profil, mints `ses_`/`msg_`
   neufs, envoyer une requête `read`/chat officielle.
3. **Verdict** : `200` + contenu non vide + aucun `event: error` = `pass`.
   `429` = `quota`, jamais `reject` ; changer de bloc.
   `403/400` répété sur plusieurs blocs = `reject`.
4. **Répétition** : au moins **2 passes sur 2 blocs distincts** pour
   `verified`.
5. **Preuve** : conserver bloc, statut, latence, hash de réponse, date.
6. **TTL** : `expires_at` (défaut 7 jours ou nouvelle release client) ;
   au-delà, `stale`, ré-vérifier avant assignation.

> Une série d'échecs sur une IP ne vaut pas pour le pool. Aucun profil n'est
> déclaré mort depuis un seul bloc.

## 6. Générateur

`generate_profile(station_label)` :

```
registry = load_verified_registry()
if not registry.verified:
    return canonical_profile()          # 1.19.5/2.2.5 vérifié
index = HMAC(secret, "profile:" + label) % len(registry.verified)
return registry.verified[index]
```

Règles :

- **VP-01** Le générateur n'émet que des profils `verified` et non `stale`.
- **VP-02** Un profil est assigné à une station pour sa durée de vie ; pas de
  changement au milieu d'une conversation (sauf failover).
- **VP-03** Si `M >= N` (profils vérifiés >= stations), aucun profil n'est
  partagé.
- **VP-04** Si `M < N`, la réutilisation est autorisée mais marquée
  `shared_profile` dans l'audit ; on préfère des stations distinctes à une
  flotte incomplète.
- **VP-05** Aucun mélange : UA ↔ Bun ↔ JA3 ↔ ordre ↔ corps ↔ session headers
  doivent venir du même profil.
- **VP-06** Un profil `rejected` est retiré ; fallback canonical, jamais
  payant.
- **VP-07** Le registre est la seule source de vérité ; `VERSION_SPECS` reste
  la définition technique, pas la preuve.
- **VP-08** L'audit expose `profile_id`, `tls_family`, `shared_profile`,
  `stale`, `rejected`.
- **VP-09** La capture fournit la **forme** (UA, TLS, ordre, corps), jamais
  l'identité : `project`, `ses_`, `msg_` et trace sont mintés par
  `StationIdentity`, pas recopiés du fichier de capture.
- **VP-10** Le runtime ne sélectionne que les **deux dernières versions**
  vérifiées `>= 1.19.5` : aujourd'hui `2.2.5` (13 outils) et `1.19.5`
  (11 outils). 1.19.3/1.18.31 restent vérifiés mais sont retirés du runtime.

## 7. Audit de cohérence

| Vérification | Rejet si |
|---|---|
| UA ↔ version | `opencode/latest/<version>/<client>` incohérent |
| UA ↔ Bun | suffixe Bun différent de `VERSION_SPECS[version]` |
| JA3 ↔ Bun | famille TLS différente |
| ordre des en-têtes ↔ version | ordre 2.2.5 avec session 1.19.x, ou inverse |
| corps ↔ version | 13 outils avec un profil 1.19.x, ou 11 avec 2.2.5 |
| session headers ↔ version | `x-opencode-session-id` absent en 1.19.0+ |
| profil ↔ station | deux stations partagent un profil sans `shared_profile` |

## 8. Matrice de tests

| Test | Attendu |
|---|---|
| candidat 200 sur 2 blocs | `verified` |
| candidat 200 sur 1 bloc puis 429 | reste `candidate`, pas `rejected` |
| candidat 403 sur 3 blocs | `rejected`, jamais assigné |
| profil `stale` | exclu, fallback canonical |
| UA 2.2.5 + JA3 1.3.14 | rejeté (tuple impossible) |
| corps 13 outils + version 1.19.3 | rejeté |
| 4 stations, 4 profils vérifiés | 4 profils distincts |
| 6 stations, 2 profils vérifiés | réutilisation, `shared_profile` visible |
| registre absent/corrompu | fallback canonical, pas de multi-station |
| deux stations, même label | refus/config invalide |

## 9. Critères d'acceptation

- **VP-AC1** Aucun profil non vérifié n'est émis.
- **VP-AC2** Un `429` n'est jamais compté comme un rejet de profil.
- **VP-AC3** Chaque profil vérifié a au moins deux preuves sur des blocs
  distincts.
- **VP-AC4** Aucun tuple mixte (UA/TLS/corps) n'est assigné.
- **VP-AC5** Une station garde son profil pendant une conversation.
- **VP-AC6** Les profils partagés sont visibles dans l'audit.
- **VP-AC7** Le fallback reste free et canonical.
- **VP-AC8** La limite de diversité TLS est documentée et exposée.
