# PLAN — Raisonnement incomplet côté client (diagnostic + correctif)

> 2026-08-25 · Priorité P1 · Effort estimé : 1 à 1,5 j · Prérequis : aucun
>
> **STATUT : TERMINÉ ✅** — Phases B, C, D, E livrées. Gate finale :
> suite complète 778 passed / EXIT=0, drift golden vert
> (`tests/test_thinking_e2e.py` 12/12). Signatures locales HMAC sur les 4
> sites de blocs thinking synthétisés, `signature_delta` en streaming
> (y compris flush final sur fin brutale), strip sélectif multi-tours,
> `redacted_thinking` géré. Générateur de fixtures rendu déterministe
> (normalisation ids/epoch identique au test golden).

## 1. Diagnostic confirmé (sondes live + audit code)

### Preuves empiriques (proxy en prod, modèle mimo-v2.5, thinking enabled)

| Sonde | Reçu | Manquant |
|---|---|---|
| `/v1/messages` non-streaming | bloc thinking 183 chars ✓ puis text ✓ | champ `"signature"` ABSENT |
| `/v1/messages` streaming | `thinking_delta` 111 chars ✓, ordre des blocs ✓ | événement `signature_delta` : **0 émis** |

### Cause racine (audit agent)

Le mot `signature` n'existe **nulle part** dans opencode.py / protocol_mapping.py
(uniquement dans scripts/gen_golden_fixtures.py). Conséquences en cascade :

1. Blocs thinking **synthétisés** par le proxy (depuis `reasoning_content` upstream)
   sont servis sans signature → les clients Anthropic-compatibles (Kilo Code,
   Claude Code) traitent ces blocs comme invalides en multi-tours et les
   abandonnent → « le raisonnement ne remonte pas intégralement ».
2. Pas de gestion `redacted_thinking` (cas où le fournisseur chiffre son raisonnement).
3. Historique multi-tours : comportement non spécifié quand le client renvoie
   thinking+signature vers chaque type d'upstream.

### Contrainte fondamentale (à comprendre avant de coder)

La signature Anthropic est **cryptographique** (calculée par le modèle source).
Le proxy ne peut PAS forger une signature valide au sens Anthropic :

- Pour les clients : une signature locale (placeholder HMAC) suffit — le client
  ne valide pas, il stocke et re-transmet.
- Pour les upstreams : seul **Anthropic direct** valide réellement les
  signatures au tour suivant. Les endpoints zen/free n'en tiennent pas compte.

→ Stratégie retenue :
- **Synthétiser** une signature locale pour les blocs thinking générés par conversion
  (les clients restent compatibles),
- **Strippé** les blocs thinking synthétiques de l'historique renvoyé vers
  Anthropic direct (on ne lui ment jamais avec une fausse signature),
- **Passthrough intact** des blocs thinking originaux quand l'upstream EST
  Anthropic (signatures authentiques préservées).

## 2. Plan de correctif — 4 phases

### Phase B — Non-streaming (~2 h)
Fichier : `protocol_mapping.py::openai_to_anthropic`
1. Bloc thinking synthétisé depuis `reasoning_content` : ajouter
   `"signature": _local_signature(text)` (HMAC SHA256 clé dérivée de
   VPN_CONTROL_API_KEY + timestamp, base64) — helper `_local_signature()` dans
   protocol_mapping.
2. Mapper `reasoning_content chiffré/binaire` → bloc `redacted_thinking` {"data": …}
3. Golden fixture `resp_reasoning.json` mise à jour (champ signature attendu).

### Phase C — Streaming (~3 h)
Fichiers : chemins free (`opencode.py` curl_cffi wrap) + payés-B.
1. Avant chaque `content_block_stop` d'un bloc thinking : émettre
   `content_block_delta {type:"signature_delta", signature: <locale>}`.
2. Garantir l'émission même si le stream upstream se termine brutalement
   (flush final dans le wrapper de fermeture).
3. Vérifier l'ordre : thinking_delta* → signature_delta → content_block_stop.

### Phase D — Multi-tours (~3 h)
1. Table de correspondance `_thinking_provenance` : marquer les blocs thinking
   SYNTHÉTIQUES (hash du texte) vs ORIGINAUX (passthrough Anthropic).
2. Conversion entrée anthropic_to_openai : blocs SYNTHÉTIQUES → supprimés de
   l'historique envoyé aux upstreams openai-compatible (inutiles pour eux).
3. Upstream Anthropic DIRECT : blocs ORIGINAUX (signature authentique client)
   passent intacts ; blocs SYNTHÉTIQUES strippés aussi (jamais de fausse
   signature vers Anthropic).
4. redacted_thinking entrant : préservé tel quel vers Anthropic, strippé sinon.

### Phase E — Tests & validation (~2 h)
1. Probes `probe_thinking.py` / `probe_stream_thinking.py` transformés en
   pytest (`tests/test_thinking_e2e.py`, mock upstream) : assertions
   signature présente, ordre SSE, redacted_thinking.
2. Golden fixtures : cas thinking ajouté au contrat V1.
3. Multi-tours : test aller-retour (réponse → historique → nouvelle requête)
   vérifiant le strip sélectif.
4. Gate : suite complète EXIT=0 + drift vert.

## 3. Risques

| Risque | Mitigation |
|---|---|
| Signature locale rejetée par un client strict | Les clients ne valident pas cryptographiquement ; seul Anthropic serveur le fait — et on ne lui envoie jamais de faux blocs |
| Régression routière thinking: adaptive (custom_routes) | Vérifier que ce flag reste un simple passthrough (aucune logique attachée aujourd'hui) |
| Perf : HMAC par bloc | Négligeable (<0,1 ms) |

## 4. Hors périmètre

- Décodage de vraies signatures Anthropic (impossible, crypto propriétaire)
- TOTP/2FA des rôles gluetun (sans rapport)
