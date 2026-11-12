# Matrice de migration — quoi faire dans Gluetun plutôt que dans opencode-proxy-one

**Feature** : `016-gluetun-control-plane`
**Date** : 2026-11-11
**Question** : « Tout ce qui peut être fait directement dans Gluetun doit-il y
être fait, pour gagner en temps, qualité, performance et fiabilité ? »

**Réponse courte** : oui pour **tout ce qui est local à une station et sur le
chemin chaud du tunnel**. Non pour **tout ce qui dépend d'un état global ou
d'une ressource rare** (IP, lease, quota, score, SLO, choix inter-stations).
Le critère est simple :

> Le fork décide et exécute tout ce qui ne concerne **qu'une station et son
> tunnel**. L'orchestrateur décide tout ce qui concerne **la flotte, les IP et
> les ressources rares**.

---

## 1. Principe de répartition

```text
┌───────────────────────────── Gluetun (local, chaud, ms) ─────────────────────────────┐
│ transport ladder · reconnect · preflight · DNS ladder · egress proof · drain          │
│ same-server retry · handshake/interface watchdog · state recovery · local cooldown    │
│ firewall/kill switch · MTU/PMTUD · catalog cache · metrics/events locaux              │
└───────────────────────────────────────┬───────────────────────────────────────────────┘
                                        │ faits, événements, jobs
┌───────────────────────────────────────▼───────────────────────────────────────────────┐
│ opencode-proxy-one (global, froid, s)                                                 │
│ choix des candidats/ordre · score · IP/lease/quota/claims · warm pool/standby         │
│ rotation inter-stations · SLO/client_usable · limites de compte · dashboard           │
└───────────────────────────────────────────────────────────────────────────────────────┘
```

**Gain attendu** : moins d'aller-retours HTTP/Docker, moins de recréations de
conteneur, des reprises en millisecondes au lieu de secondes, un état atomique
et survit à l'indisponibilité de l'orchestrateur. **Risque évité** : ne pas
déplacer le quota, le claim ou le score dans le fork, ce qui créerait deux
autorités et des violations de la constitution III/IX.

---

## 2. À migrer dans Gluetun (chemin chaud)

| # | Capacité | Aujourd'hui | Dans Gluetun | Gain | Risque / garde-fou | Phase |
|---|---|---|---|---|---|---|
| M-01 | **Ladder de transports** WG → OVPN UDP → OVPN TCP → retour WG | Python `free/transport_ladder.py` + `vpn/manager.py` | `internal/vpn/transport` : essais ordonnés, budget par transport, breaker, retour au premier après cycle + backoff | Bascule en ms, sans redémarrage de conteneur ; un seul tunnel à la fois | Doit rester local : ne choisit pas un nouveau serveur/IP | 2b |
| M-02 | **Détection de tunnel mort** (handshake stale, interface absente, ping interne) | Watchdog Python, healthcheck Docker | Watchdog Go dans la boucle VPN, avec seuils et événements | Détection < 2 s, pas de `docker inspect` | Ne répare que la **même** cible ; l'escalade reste à l'orchestrateur | 2b |
| M-03 | **Reconnexion même serveur** avec backoff/jitter | Python relance/recrée | Boucle Go : retry borné, backoff, jitter, breaker | Supprime les recréations Compose | Budget borné ; après N échecs, événement + arrêt, pas de boucle | 2b |
| M-04 | **Préflight d'endpoint** (DNS, IP, TCP/UDP, credentials) | Partiel Python | Go, avant toute coupure | Évite les coupures inutiles | Aucune décision globale | 3 |
| M-05 | **Vérification d'egress** multi-sources + canary | Python `egress.py`, 5 couches | Go `internal/egress` | Preuve en parallèle, cache local, pas de quota | Le fork fournit un fait ; le claim reste au registre | 1 |
| M-06 | **DNS ladder** DoT/DoH/plain | Un seul type upstream | Go `internal/dns/ladder` | Failover par requête, pas de restart | Fail-closed, cache/filter au listener | 2 |
| M-07 | **Drain des connexions proxy** | Absent / Python | Go, compteur de connexions, timeout | Moins de requêtes coupées | Timeout borné | 3 |
| M-08 | **Persistance d'état** désiré/committé | Compose/env uniquement | Go `state.json` atomique | Survit au restart, pas de drift | Secrets non persistés en clair | 3 |
| M-09 | **Résolution de l'endpoint et fallback d'IPs** | Python / OS | Go, à partir du hostname et du catalogue | Préflight plus fiable | Ne pas confondre avec l'IP de sortie | 3 |
| M-10 | **Cooldown local court par hostname/transport** | Python | Go, table bornée, TTL | Évite de retenter en boucle le même host | Cooldown **global** et score restent au registre | 4 |
| M-11 | **Mesure de latence/handshake par serveur** | Python probes | Go, EWMA locale | Données fraîches, sans requête modèle | Consultatif ; le score reste au registre | 4 |
| M-12 | **Catalogue provider : fetch/cache/facettes** | Python lit des fichiers / API | Go (déjà stocké upstream) + API lecture | Source unique, pas de parsing Python | Refresh admin rate-limité | 4 |
| M-13 | **Kill switch, firewall, anti-fuite DNS/IPv6** | Déjà upstream | Renforcer par transport | Évite les fuites pendant la bascule | Ne pas affaiblir en mode ladder | 2b |
| M-14 | **MTU/PMTUD et réglages d'interface** | Déjà upstream | Automatique par transport | Moins d'échecs WireGuard | Aucun | 2b |
| M-15 | **Métriques/événements locaux** | Python + logs | Go Prometheus + SSE | Observabilité < 1 s | Pas de secret | 5 |
| M-16 | **Healthcheck interne** | Docker + Python | Go, état tunnel + DNS + egress | Verdict local plus juste | `client_usable` reste à 015 | 5 |

### M-01 — Ladder de transports : spécification cible

C'est la demande explicite : **utiliser automatiquement WireGuard, OpenVPN UDP
et OpenVPN TCP, prendre le premier qui fonctionne, et si le premier échoue,
passer au suivant, puis revenir au premier et réessayer**.

```text
policy = [wireguard, openvpn-udp, openvpn-tcp]   # défaut, configurable
cycle:
  for transport in policy:
      if breaker[transport].open: continue
      preflight(transport)
      teardown_current()          # un seul tunnel à la fois
      apply(transport, server)
      wait_tunnel(deadline)
      if tunnel_ok and egress_ok:
          commit; breaker[transport].success(); return
      teardown_current()
      breaker[transport].failure(reason)
  # cycle épuisé
  emit("transport.cycle_exhausted")
  backoff(jitter)
  restart cycle at policy[0]      # retour au premier, comme demandé
```

Règles :

- **Un seul tunnel** : le transport précédent est arrêté avant le suivant.
- **Ordre par défaut** : `wireguard,openvpn-udp,openvpn-tcp`.
- **Budget par transport** : `transport_attempt_timeout_s` (défaut 30 s WG,
  30 s UDP, 45 s TCP) et `transport_max_attempts` (défaut 2).
- **Retour au premier** : après un cycle complet, backoff exponentiel avec
  jitter (base 30 s, plafond 300 s), puis reprise à `policy[0]`.
- **Breaker** : un transport qui échoue N fois est `open` pendant un cooldown ;
  `half_open` après cooldown pour une tentative de sonde.
- **Erreurs** : `auth` (ne pas boucler sur le même transport ; tenter les
  autres), `udp_blocked`, `tcp_refused`, `tls`, `handshake_timeout`,
  `no_egress`, `interface_down`. Chaque erreur a une raison typée.
- **Pas de sélection de serveur** : le ladder ne change que le transport. Si
  tous les transports échouent, le job passe `exhausted`/`rolled_back` et
  l'orchestrateur décide d'un autre serveur/IP.
- **Visibilité** : `GET /v1/vpn/transport`, événements `transport.attempt`,
  `transport.connected`, `transport.failed`, `transport.cycle_exhausted`,
  métriques par transport.
- **Compatibilité** : `OPENVPN_*`/`WIREGUARD_*` restent valides ; un
  `VPN_TYPE` explicite force un seul transport (pas de ladder).
- **Shadow** : le ladder Python de 015 est conservé en observation, puis
  décommissionné du chemin chaud ; il ne doit jamais lancer un second tunnel.

### M-02/M-03 — Watchdog local et reconnexion

- Détecte : interface absente, handshake WireGuard > 180 s, statut VPN non
  `running`, DNS local injoignable, egress non prouvé.
- Réessaie **le même serveur et le même transport** avec backoff, puis passe au
  transport suivant (M-01).
- Après `local_reconnect_max` (défaut 3), émet `tunnel.repair_needed` et
  s'arrête ; l'orchestrateur (ou 015) décide d'une rotation.
- Ne recrée jamais le conteneur ; ne choisit jamais une autre IP.

### M-13/M-14 — Sécurité et interface

- Le firewall est réappliqué à chaque changement de transport, avant que
  l'interface ne soit considérée `up`.
- Aucune fenêtre où le trafic sort sans kill switch.
- MTU/PMTUD recalculé par transport ; WireGuard peut nécessiter une valeur
  différente d'OpenVPN.

---

## 3. À garder dans opencode-proxy-one (global, ressource rare)

| # | Capacité | Pourquoi elle reste | Conséquence si déplacée |
|---|---|---|---|
| G-01 | Choix/ordre des candidats et score composite | Constitution IX ; `registry.db` est l'autorité | Deux classements divergents. |
| G-02 | Claims d'IP, leases, unicité | Constitution V ; état global | Deux stations peuvent partager une IP. |
| G-03 | Quota par IP, `reset_at`, ledger | Constitution IV ; reset fournisseur | Sondes inutiles, IP brûlées. |
| G-04 | Warm pool et standby inter-stations | Feature 015 ; ressource de flotte | Le fork ne peut pas créer une 2e session. |
| G-05 | Choix de la station à rotationner et timing | Vue flotte | Rotations simultanées, 6/6 perdu. |
| G-06 | `client_usable`, SLO, incidents | Mesure transverse | Verdict local non comparable. |
| G-07 | Limites de compte/sessions (6) | Ressource rare | 7e session, violation dure. |
| G-08 | Dashboard, GUI, corrélation | Vue globale | Duplication d'UI et d'état. |
| G-09 | Credentials/account provider | Secret global | Duplication de secrets. |

---

## 4. Hybrides (mesure au fork, décision au registre)

| # | Capacité | Fork | Registre |
|---|---|---|---|
| H-01 | Stats serveur | Mesure tentatives/succès/latence EWMA | Score composite, ordre. |
| H-02 | Cooldown | Court, local, TTL (secondes/minutes) | Long, global, après 429/quota (heures). |
| H-03 | Catalogue | Fetch/cache/facettes | Snapshot pour la planification. |
| H-04 | Santé tunnel | État local temps réel | SLO 1 h/24 h/7 j/30 j. |
| H-05 | Rotation | Exécute la liste et prouve | Choisit la liste, le budget et le moment. |
| H-06 | Egress | Consensus + canary | Claim/lease et verdict. |

---

## 5. Ce qui ne doit surtout pas migrer

- **Le score composite** : le fork ne doit jamais trier une liste reçue.
- **Le quota et le `reset_at`** : le fork ne sonde pas l'API modèle.
- **Le claim d'IP** : le fork ne réserve pas une IP pour une autre station.
- **La 7e session/station** : aucune autonomie ne peut en créer.
- **Le verdict `client_usable`** : il reste mesuré par 015.
- **Le make-before-break inter-stations** : il reste dans la feature 015.
- **Le payant/secours payant** : jamais, même en cas d'échec total.

---

## 6. Gains mesurables attendus

| Métrique | Avant (Python/Docker) | Après (Gluetun local) | Gain |
|---|---|---|---|
| Bascule même transport | 8–20 s + recréation possible | ≤ 8 s p95, sans recréation | Temps, fiabilité |
| Bascule de transport | 20–60 s (restart) | ≤ 20 s p95 | Temps |
| Détection tunnel mort | 10–30 s (watchdog) | ≤ 2 s | Fiabilité |
| Failover DNS | restart 10–30 s | ≤ 2 s par requête | Disponibilité |
| Preuve d'egress | 5 couches Python, 3–10 s | consensus Go ≤ 3 s, cache 10 s | Temps, qualité |
| Recréations Compose | fréquentes en réparation | 0 dans le chemin chaud | Fiabilité |
| État après restart | perdu (env) | restauré (state) | Qualité |
| Dépendance à l'orchestrateur | forte | le tunnel survit ; décisions globales différées | Résilience |

---

## 7. Séquence de migration recommandée

1. **Phase 1** : egress verification (M-05) — prérequis de toute preuve.
2. **Phase 2** : DNS ladder (M-06) — supprime les boucles de healthcheck.
3. **Phase 2b** : **transport ladder** (M-01/M-02/M-03) — la demande explicite,
   avec tests de blocage UDP/TCP et retour au premier transport.
4. **Phase 3** : SwitchJob, préflight, drain, state (M-04/M-07/M-08).
5. **Phase 4** : catalogue/rotation/cooldown local (M-09→M-12).
6. **Phase 5** : événements, métriques, sécurité (M-15/M-16).
7. **Phase 6** : l'orchestrateur passe en shadow puis active ; le ladder Python
   de 015 est décommissionné du chemin chaud.

---

## 8. Critères d'acceptation de la migration

- [ ] Un seul tunnel actif à tout instant (aucun double namespace).
- [ ] `wireguard` échoué → `openvpn-udp` → `openvpn-tcp` → retour `wireguard`,
      avec backoff, sans redémarrage de conteneur.
- [ ] Un transport bloqué (UDP) n'empêche pas TCP de fonctionner.
- [ ] Un échec d'auth ne boucle pas indéfiniment ; il passe au transport
      suivant puis signale `repair_needed`.
- [ ] Le ladder ne change jamais de serveur/IP de lui-même.
- [ ] Le fork ne re-score jamais une liste et ne touche jamais au quota.
- [ ] Le chemin chaud Python n'appelle plus `docker exec`/`docker cp`.
- [ ] Les six stations restent `client_usable` pendant le rollout.
- [ ] Le ladder Python de 015 n'est plus qu'un observateur, puis est retiré.
