# Research — Auto-Egress

**Date**: 2026-11-07
**But**: figer le nom, la sémantique du parallélisme et les règles de
sélection avant l'implémentation.

## D1 — Nom : Auto-Egress

**Decision**: nom UI **Auto-Egress**, valeur interne `auto_egress`.

**Rationale**: « Auto » dit que le mode choisit seul ; « Egress » décrit ce
qui est mutualisé (les sorties réseau). C'est court, non ambigu, et ça ne
promet pas un bonding.

**Alternatives considérées**:
- **Hybride** : évoque le split-tunneling, ambigu.
- **Multi-Egress** : descriptif mais moins clair sur l'automatisation.
- **Pool** : collision avec le pool d'IP existant.
- **Omni** : trop vague.
- **Fusion** : évoque une agrégation de liens, ce que le mode ne fait pas.

## D2 — Parallélisme : inter-requêtes + hedge, pas bonding

**Decision**: une requête HTTP utilise un seul egress. Le mode parallélise
les **requêtes concurrentes** et les **tentatives de secours** (hedge), pas les
paquets d'une même requête.

**Rationale**: l'API OpenCode est du HTTPS classique ; un bonding multi-lien
demanderait MPTCP ou un proxy agrégateur, hors périmètre et risqué pour le
ledger. Le hedge permet déjà de masquer un provider lent.

**Alternatives**:
- MPTCP/agrégation : complexe, incompatible avec les quotas par IP.
- Split par paquets : casse TLS/HTTP.
- Un seul provider par requête sans hedge : plus lent en cas d'échec.

## D3 — Pool unifié, un seul moteur

**Decision**: construire un `UnifiedEndpoint` par provider activé et réutiliser
`EgressProvider` (feature 007) pour la rotation/santé.

**Rationale**: NordVPN, WARP et proxies ont des cycles différents mais le même
contrat de sélection. Un second moteur dupliquerait les bugs.

**Alternatives**:
- Sélecteurs séparés par provider : duplication, incohérence.
- Un proxy agrégateur externe : dépendance, sécurité, ledger.

## D4 — Score de sélection

**Decision**: score = santé (40 %), quota restant (20 %), latence (15 %),
succès récents (15 %), poids provider (10 %). Les portes dures (reset, quota
épuisé, provider down, unicité) s'appliquent avant le score.

**Rationale**: cohérent avec la constitution IX (score ordonne, portes dures
décident) et avec le scoring existant. Les poids restent ajustables.

**Alternatives**:
- Round-robin pur : ignore la santé/latence.
- Least-connections seul : ignore le quota.
- Score sans portes : peut réutiliser un bloc épuisé.

## D5 — Free-first par défaut, pas d'exclusion

**Decision**: poids par défaut WARP 60 %, NordVPN 30 %, proxys 10 %. Les
providers restent tous utilisables s'ils sont sains ; le poids est une
préférence, pas une porte.

**Rationale**: WARP est gratuit et sans cap de données ; NordVPN/proxys
ajoutent de la capacité mais peuvent être limités. L'opérateur peut ajuster.

**Alternatives**:
- WARP only : sous-dimensionné.
- NordVPN first : coût/abonnement, contraire à la priorité free.
- Égalité stricte : vide WARP trop vite ou sur-utilise NordVPN.

## D6 — Caps de concurrence par provider

**Decision**: `max_concurrent` par provider, sémaphores dédiés, hedge
`hedge_max_attempts` borné. Un provider au cap est simplement moins choisi.

**Rationale**: évite qu'un provider rapide absorbe tout et épuise son quota ;
évite les rafales sur un provider fragile.

**Alternatives**:
- Cap global seulement : un provider peut être saturé.
- Aucun cap : rafales, 429.

## D7 — Quotas par provider, pas de fusion

**Decision**: chaque provider garde son ledger : IP NordVPN, bloc WARP `/64`,
IP proxy. Un `429` ne condamne que le bloc du provider concerné.

**Rationale**: les quotas sont fournisseur+IP ; fusionner les ledgers
créerait de faux blocs et des rotations inutiles.

**Alternatives**:
- Ledger unique : un 429 WARP bloquerait NordVPN.
- Quota global : faux, la capacité est par egress.

## D8 — Dédup d'egress physique

**Decision**: si deux providers sortent par la même IP/bloc, un seul est
sélectionné à la fois ; le doublon est signalé.

**Rationale**: deux endpoints sur le même egress ne doublent pas le quota ;
ils créent de la confusion et des 429 corrélés.

**Alternatives**:
- Faire confiance au provider : doublons invisibles.
- Interdire deux providers : trop strict (WARP/NordVPN peuvent partager par
  hasard).

## D9 — Direct : explicite seulement

**Decision**: `allow_direct=false` par défaut ; `fail_closed=true`. Le direct
n'est utilisé que si l'opérateur l'autorise explicitement et qu'aucun provider
n'est disponible.

**Rationale**: constitution X : pas de bascule silencieuse vers le direct ou
le payant. Le direct peut exposer l'IP résidentielle et fausser le ledger.

**Alternatives**:
- Direct en dernier recours automatique : fuite silencieuse.
- Direct interdit : peut bloquer un diagnostic explicite.

## D10 — Sticky par conversation

**Decision**: une conversation garde son egress tant qu'il est sain ; un
changement n'arrive qu'après échec/quota, et il est journalisé.

**Rationale**: préserve le cache et évite le rejeu de contexte ; réduit la
consommation de tokens.

**Alternatives**:
- Round-robin par requête : casse le cache.
- Sticky permanent : ne profite pas des providers frais.

## D11 — Providers existants seulement

**Decision**: le mode utilise les providers déjà configurés. Aucun nouvel
abonnement, aucune clé achetée, aucun compte empilé.

**Rationale**: la demande est d'utiliser ce qui est disponible ; le coût et
les CGU ne doivent pas augmenter.

**Alternatives**:
- Ajouter un VPN payant : hors périmètre.
- Empiler des comptes : CGU, suspension.

## D12 — Observabilité et attribution

**Decision**: journaliser pour chaque requête le provider, l'endpoint, le
bloc, le statut et la latence ; exposer la répartition et les switches.

**Rationale**: sans attribution, impossible de prouver quel provider sert et
de diagnostiquer un 429/403.

**Alternatives**:
- Logs agrégés : insuffisant.
- Métriques par provider seulement : pas de traçabilité requête.

## D13 — Changement de mode sans interruption

**Decision**: drain des requêtes en vol avant de basculer ; le mode est
persisté ; les nouvelles requêtes prennent le nouveau mode.

**Rationale**: éviter les erreurs et les sessions orphelines.

**Alternatives**:
- Switch brutal : erreurs, retries.
- Redémarrage complet : lourd, interrompt tout.
