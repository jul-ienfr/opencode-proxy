# Registre de dette technique

**Feature** : `002-vpn-catalog-rotation`  
**Statut** : plan à valider — aucune implémentation  
**Règle** : chaque dette a une preuve, un impact, une phase et un critère de
sortie. Une dette non listée ici n’est pas « oubliée » : elle est hors
périmètre ou à ajouter.

Sévérité : **P0** = casse la disponibilité ou la vérité des données ;
**P1** = dégrade fortement la fiabilité/perf ; **P2** = maintenabilité.

| ID | Dette | Preuve | Impact | Sévérité | Phase | Sortie |
|---|---|---|---|---|---|---|
| D-01 | Plusieurs sources de vérité | `ip_registry.db`, `catalog_claims.db`, `free_pool.json`, `fresh_chain_state*.json`, `shared_rotation.json`, `latency_engine.json` | états divergents, décisions incohérentes | P0 | 2 | `registry.db` seule source ; JSON en lecture seule puis supprimés |
| D-02 | Monolithes | `opencode.py` 1,4 Mo ; `vpn/manager.py` 584 Ko ; `free/pool.py` 276 Ko | couplage, tests difficiles, régressions | P1 | 2–6 | extraction strangler ; aucun nouveau module > 1 500 lignes |
| D-03 | Hardcodes Albanie | `PROVEN_HOSTS`, `_is_gold_ip`, `_registry_hosts` | cantonnement, quotas épuisés | P0 | 1 | aucun littéral `al*`/`87.120.102` ; test régression |
| D-04 | Options GUI non prouvées | `use_nordvpn_api` seulement dashboard ; pas de contrat | faux contrôle, réglages ignorés | P1 | 1 | contrat GUI 100 % + `config_audit.py --strict` |
| D-05 | DB polluée | 3 286 lignes `hostname=''`, 3 549 `country=''` ; compteurs `tests/successes` non fiables | faux stock, mauvais score | P0 | 1 | lignes archivées/exclues ; score depuis `observations` |
| D-06 | Lease trop court | `catalog_claims.py` 15 s vs bind 60 s | reprise de hostname, binds croisés | P0 | 1 | lease > bind + renouvellement |
| D-07 | Deux propriétaires de réparation | `station_repair.enabled: true` + superviseur | churn, stations recréées | P0 | 1 | superviseur seul |
| D-08 | Gel global des rotations | `_upstream_event_active` bloque `_launch_rotation` | une station bloque les autres | P1 | 2 | jobs par station, pas de pause quota |
| D-09 | Fallback IP/DNS absent | `egress.py` SOCKS seulement ; DNS DoH 2 providers | `no-egress` faux, station jetée | P0 | 1 | chaînes IP/DNS complètes |
| D-10 | Auto stack non tri-stack | `_MIXED_SLOTS`, flips globaux ; `allocator.transports=wireguard` | une techno morte bloque le serveur | P1 | 2 | WG → OV UDP → OV TCP par serveur |
| D-11 | Base télémétrie géante | `requests.db` ~3 Go, `debug.log` volumineux | lenteur, disque, requêtes lentes | P1 | 1–2 | `telemetry.db` + rétention + agrégats |
| D-12 | Gluetun `latest` / manifeste fragile | image `latest`, `be160` absent du volume | crash loop `hostname not valid` | P1 | 1 | image épinglée + vérification manifeste |
| D-13 | JSON d’état legacy | `free_pool.json` 1 Mo, state files | pas transactionnel, corruption | P1 | 2 | migré en DB, JSON en export |
| D-14 | Flags/chemins morts | `auto_mixed_stacks`, `max_per_country`, `use_nordvpn_api`, `dual_station` | confusion, code mort | P2 | 4 | suppression ou marquage |
| D-15 | Tests de chaos absents | pas de test panne DNS/IP/API/Docker | régressions non détectées | P1 | 3–4 | tests chaos + canary |
| D-16 | Exceptions silencieuses | nombreux `except Exception: pass` | pannes invisibles | P2 | 4 | journalisation + remontée |
| D-17 | Secrets/logs | clés dans env, logs verbeux | fuite potentielle | P2 | 1 | redaction + audit secrets |
| D-18 | Pas de migrations/backups | `ensure_schema` ad hoc, pas de backup | perte de données | P0 | 1–2 | migrations versionnées + backup |
| D-19 | Station hors scope | station 7 en marche pour `station_count=6` | session/quotas gaspillés | P0 | 1 | stations hors scope arrêtées |
| D-20 | Dashboard lourd | `dashboard/api.py` 250 Ko, requêtes larges | latence, couplage | P2 | 4 | projections/read models |
| D-21 | Config dupliquée | `config.yaml` + `.env` + compose + credentials | divergences | P1 | 1 | génération unique + audit |
| D-22 | Pas de contrat de modules | imports croisés, pas de règle | couplage | P1 | 2 | test d’imports + architecture.md |
| D-23 | Clocks/timezones | mélange epoch/local | calculs de reset faux | P2 | 2 | UTC partout, clock injectée |
| D-24 | Logs non structurés | logs texte par station | diagnostic difficile | P2 | 3 | événements JSON + corrélation |
| D-25 | Croissance DB/logs | pas de rétention | disque plein | P1 | 1–2 | retention worker |

---

## Priorisation

### P0 — à faire en Phase 1

D-01 (fondation), D-03, D-05, D-06, D-07, D-09, D-18, D-19.

Objectif : arrêter les mauvaises décisions et les crash loops avant la refonte.

### P1 — Phase 2–3

D-02, D-04, D-08, D-10, D-11, D-12, D-13, D-15, D-21, D-22, D-25.

Objectif : sélecteur/DB/transport fiables, performance et testabilité.

### P2 — Phase 4–6

D-14, D-16, D-17, D-20, D-23, D-24.

Objectif : maintenabilité, observabilité, nettoyage final.

---

## Règles « zéro nouvelle dette »

1. Aucun nouveau module > 1 500 lignes.
2. Aucune nouvelle clé config sans consommateur + test GUI.
3. Aucun nouvel état autoritatif en JSON.
4. Aucun `except: pass` silencieux dans le nouveau code.
5. Aucun hardcode hostname/pays/IP dans la sélection.
6. Toute nouvelle table a PK, index, contrainte et migration.
7. Tout nouveau fallback a un timeout, un backoff et un événement.
8. Toute nouvelle option GUI a un test d’effet.

---

## Suivi

- `debt.md` est mis à jour à chaque phase ; une dette fermée garde sa ligne
  avec `statut=closed` et la date.
- Le gate de phase vérifie : 0 P0 ouverte, P1 planifiée, pas de nouvelle
  dette non listée.
- Le dashboard expose `debt_open_p0/p1/p2` (optionnel) pour l’opérateur.
