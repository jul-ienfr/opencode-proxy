# Requirements Quality Checklist: Egress utilisable par les clients

**Purpose**: Revue de la qualité des exigences (pas de l'implémentation) pour
la disponibilité client, l'émulation, la compatibilité et les providers.
**Created**: 2026-11-17
**Feature**: [spec.md](./spec.md) · [plan.md](./plan.md)

**Review Ownership**: checklist de revue ; `[x]` = critère de qualité des
exigences satisfait, pas une preuve d'implémentation.
**Marker Semantics**: `[x]` revu et satisfait ; `[ ]` à clarifier, corriger ou
évaluer.

# Checklist — 023-client-usable-egress

## Alignement constitution (v1.3.0)

- [ ] CHK001 **I** — « utilisable » est calculé depuis des preuves horodatées, jamais
  depuis `connected`/`healthy`.
- [ ] CHK002 **IV** — aucun reset estimé : `reset_at` vient du `Retry-After`
  fournisseur uniquement.
- [ ] CHK003 **V** — une IP n'appartient qu'à une station ; claim exclusif maintenu
  dans `ip_state`.
- [ ] CHK004 **VII** — un 200 sans contenu ou avec `event: error` ne prouve rien.
- [ ] CHK005 **VIII** — un seul réparateur ; aucune autre boucle ne recrée ou ne
  relance une station.
- [ ] CHK006 **X** — dégradation explicite, free-only, jamais de paiement silencieux.
- [ ] CHK007 **XIV** — nouveaux modules < 1 500 lignes, domaine sans I/O, adapters
  pour le réseau.
- [ ] CHK008 **XVI** — aucun processus visible ; `run_hidden` partout.
- [ ] CHK009 **XVII** — gates mesurables, dette suivie, doc/spec/CHANGELOG dans le
  même lot.
- [ ] CHK010 **XVIII** — une IP = un format ; A/B à IP neuve par variante ; sonde
  officielle exacte ; réserve jamais sondée ; brûlure ≥ 24 h documentée.

## Sécurité et intégrité

- [ ] CHK011 Aucun secret dans les logs, les preuves ou `ip_state`.
- [ ] CHK012 Corps amont conservé tronqué (`max_body_chars`), jamais un dump complet
  non borné.
- [ ] CHK013 Migration `registry.db` additive, sauvegarde `VACUUM INTO` vérifiée avant
  application.
- [ ] CHK014 Aucune recréation de conteneur pour une rotation (API gluetun seulement).

## Qualité

- [ ] CHK015 Chaque FR de la spec a un test automatisé ou une preuve live référencée.
- [ ] CHK016 Chaque bug corrigé a un test de non-régression (sentinel IP, strike=1,
  corps amont vide, compat sur proven).
- [ ] CHK017 Propriétés vérifiées : un seul état par IP, `burned` ⇒ zéro sélection,
  `compat` ⇒ zéro egress prouvé.
- [ ] CHK018 `config_audit --strict`, `platform_audit --strict`, `test_option_map`,
  ruff, `py_compile`, gate complète : tous verts.
- [ ] CHK019 Aucune option de configuration sans consommateur runtime et sans effet
  observable.

## Exploitation

- [ ] CHK020 Les quatre providers peuvent être activés/désactivés indépendamment.
- [ ] CHK021 Une désactivation prend effet en < 60 s et l'état est visible.
- [ ] CHK022 Le dashboard expose : états par bucket, classes de 429, rotations,
  libérations avec `evidence_ref`.
- [ ] CHK023 Le runbook `ops/runbooks/` décrit la procédure de vérification d'une IP
  neuve et l'interdiction d'A/B sur IP usée.
- [ ] CHK024 Le CHANGELOG documente l'amendement constitutionnel et la feature.
## Vocabulaire et couches

- [ ] CHK025 La revue distingue L0 TLS, L1 en-têtes, L2 wire, L3 identité, L4
  protocole, L5 outils, L6 capacités ; « impersonation » n'est pas utilisé
  pour désigner la conversion.
- [ ] CHK026 `docs/CLIENTS.md` documente chaque couche et chaque famille.

## Clients

- [ ] CHK027 Claude Code : `/v1/messages` stream et non-stream, `count_tokens`,
  `thinking`, `cache_control`, `tool_use`/`tool_result`.
- [ ] CHK028 Codex : `/v1/responses`, `response.*`, `reasoning.effort`,
  `prompt_cache_key`.
- [ ] CHK029 Kilo Code : `/v1/chat/completions`, SSE `[DONE]`, tools.
- [ ] CHK030 Hermes Agent : prompt long, outils custom, mapping aller/retour.
- [ ] CHK031 DeepSeek : `reasoning_content` sur chaque message assistant.
- [ ] CHK032 Aider : non-stream, sans outils, budget de contexte.
- [ ] CHK033 SDK OpenAI : en-têtes `x-stainless-*` tolérés, Bearer.
- [ ] CHK034 Client inconnu : profil `custom`, 422 local explicite, aucun egress.

## Outils et flux

- [ ] CHK035 0 appel non déclaré relayé ; 0 `tool_choice: none` amont.
- [ ] CHK036 Retour d'appel toujours au nom client d'origine, ids préservés.
- [ ] CHK037 SSE : aucun événement manquant/dupliqué/réordonné sur 100 flux par
  famille ; usage final présent.
- [ ] CHK038 Erreurs traduites par famille ; aucun 429 synthétique.

## Providers

- [ ] CHK039 NordVPN : rotation API, `StartedAt` inchangé, bucket `/32`, deux
  lecteurs d'IP.
- [ ] CHK040 WARP : `/64` IPv6 = bucket, IPv4 = capacité 0, enregistrement et santé.
- [ ] CHK041 Proxys : qualification, denylist, isolation, `max_active`, jamais en
  réserve.
- [ ] CHK042 Direct : 429 honnête avec `reset_at`, jamais en réserve,
  `enforce_vpn_only` respecté.
- [ ] CHK043 Wire identique (JA3/JA4, en-têtes, `wire_fp`) sur les quatre chemins.

## Notes

- Marquer `[x]` seulement après revue du critère de qualité des exigences.
- `/speckit.implement` lit l'état des cases comme porte et ne modifie pas les marqueurs.
