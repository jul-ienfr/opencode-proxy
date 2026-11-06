# Requirements Quality Checklist: Rotation catalogue, architecture et données

**Purpose**: Vérifier que la spec 002 est complète, testable, non ambiguë et
alignée sur la constitution avant d’implémenter les phases 2.5+.
**Created**: 2026-11-05
**Feature**: `specs/002-vpn-catalog-rotation/spec.md`

**Note**: `[x]` = critère de qualité des exigences revu et satisfait. Ce n’est
pas un statut d’implémentation (voir `../implementation-log.md`).

## Portée et objectifs

- [x] CHK001 Les objectifs O1–O11 sont explicitement mesurables.
- [x] CHK002 Les non-objectifs excluent clairement la jambe payante et le
      hors-périmètre porte/TLS.
- [x] CHK003 Les user stories couvrent 429, indépendance, catalogue,
      fallbacks, architecture/DB et montée en charge.
- [x] CHK004 Le périmètre « six stations client-utilisables » est défini sans
      ambiguïté.
- [x] CHK005 La définition de `client_usable` est unique et testable.

## Exigences fonctionnelles

- [x] CHK006 Toutes les stations peuvent utiliser n’importe quel pays, sans
      quota pays/continent.
- [x] CHK007 La seule contrainte dure est l’unicité de l’IP d’egress, et elle
      est explicite (identité = IP, pas hostname).
- [x] CHK008 Un 429 quota ne déclenche la rotation que de la station concernée,
      en parallèle des autres.
- [x] CHK009 Le démarrage conserve les stations déjà utilisables et ne complète
      que les manquantes.
- [x] CHK010 La rotation proactive est une option GUI désactivée par défaut.
- [x] CHK011 Le catalogue API + Gluetun est défini avec provenance et fraîcheur.
- [x] CHK012 Le manifeste du conteneur cible est vérifié avant chaque bind.
- [x] CHK013 Les fallbacks IP, DNS, catalogue, manifeste, bind, probe et
      superviseur sont spécifiés.
- [x] CHK014 Le tri-stack WireGuard → OpenVPN UDP → OpenVPN TCP est spécifié
      avec boucle par serveur.
- [x] CHK015 Le contrat GUI `option → clé → consommateur → effet → test` est
      explicite.
- [x] CHK016 Le score composite et ses poids par défaut sont fixés.
- [x] CHK017 La réutilisation d’une IP après `reset_at` est définie sans
      bascule proactive.
- [x] CHK018 L’architecture domaine/ports/adapters/use cases/workers est
      spécifiée.
- [x] CHK019 La base `registry.db` + `telemetry.db` est spécifiée comme source
      de vérité.
- [x] CHK020 La dette est suivie par un registre avec gates.

## Données et cycle de vie

- [x] CHK021 Les entités serveur, transport, endpoint, IP, quota, lease,
      observation, score, fallback et incident sont définies.
- [x] CHK022 Les transitions d’état d’une station et d’une IP sont explicites.
- [x] CHK023 La provenance et la fraîcheur sont obligatoires pour chaque
      serveur/endpoint/IP.
- [x] CHK024 Les contraintes d’unicité d’IP sont garanties en base.
- [x] CHK025 Les migrations, la rétention, le backup et l’intégrité sont
      spécifiés.
- [x] CHK026 Les lignes legacy sans hostname sont archivées, pas perdues.
- [x] CHK027 Les scores sont matérialisés à part des observations append-only.

## Non-fonctionnel

- [x] CHK028 Le remplacement 429 < 60 s p95 et le balayage < 24 h sont
      mesurables.
- [x] CHK029 Les SLO 5/6 et 6/6 sont chiffrés avec fenêtres.
- [x] CHK030 Les exigences de maintenabilité (pas de module > 1 500 lignes,
      domaine sans I/O) sont testables.
- [x] CHK031 Les exigences de fallback et d’indépendance sont testables.
- [x] CHK032 Les exigences GUI (0 option inerte) sont testables.

## Intégrations et dépendances

- [x] CHK033 Les sources API NordVPN, Gluetun et cache sont décrites avec
      leurs modes de défaillance.
- [x] CHK034 Les resolvers DNS et sites d’IP de secours sont décrits.
- [x] CHK035 Les méthodes de bind Compose/control API/Docker sont décrites.
- [x] CHK036 Le budget de sessions avec clé partagée est identifié comme test
      Phase 0, pas comme hypothèse silencieuse.
- [x] CHK037 La validation OpenVPN TCP est identifiée comme test Phase 0.

## Cas limites et erreurs

- [x] CHK038 Les cas 429 daily/transient/rate_limit/unknown sont distingués.
- [x] CHK039 Les cas no-egress, manifest_missing, dns_error et ip_unknown sont
      distincts.
- [x] CHK040 Les pannes DNS/IP/catalogue/bind/superviseur ont un comportement
      défini.
- [x] CHK041 La migration DB échouée a un rollback défini.
- [x] CHK042 Deux stations ne peuvent jamais se retrouver sur la même IP, y
      compris en promotion concurrente.
- [x] CHK043 Une IP performante après reset est réutilisée à la prochaine
      rotation, sans churn.

## Testabilité et acceptation

- [x] CHK044 Chaque user story a un Independent Test.
- [x] CHK045 Les scénarios Given/When/Then sont vérifiables.
- [x] CHK046 Les critères de succès SC-100 à SC-120 sont mesurables.
- [x] CHK047 Les tests unitaires/intégration/canary/soak sont prévus.
- [ ] CHK048 Le budget exact de sessions simultanées est mesuré (Phase 0).
- [ ] CHK049 Le support OpenVPN UDP/TCP réel est validé (Phase 0).
- [ ] CHK050 Le taux de faux positifs/négatifs du préflight est mesuré
      (Phase 0).

## Constitution et cohérence

- [x] CHK051 La constitution est amendée en v1.1.0 pour les décisions
      pays/IP/DB/fallback/GUI/architecture.
- [x] CHK052 Aucun principe de la constitution n’est contredit par la spec.
- [x] CHK053 Aucun hostname, pays ou préfixe IP n’est requis en dur.
- [x] CHK054 Aucun fallback payant n’est présenté comme solution free.
- [x] CHK055 Les clarifications C1–C20 sont intégrées et sans contradiction.
- [x] CHK056 Aucun placeholder `TODO`/`TBD` ne reste dans la spec.

## Notes

- CHK048–CHK050 sont des **tests empiriques**, pas des ambiguïtés de spec :
  ils restent ouverts jusqu’à la Phase 0.
- La checklist est un artefact de revue des exigences ; les statuts
  d’implémentation sont dans `../implementation-log.md`.
