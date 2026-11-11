# Catalogue des sources de proxys publics

**Dernière revue : 2026-11-09.** Le fichier éditable est
[`config/egress_sources.yaml`](../config/egress_sources.yaml) ; `config.yaml`
ne contient plus la liste, seulement `egress_catalog.providers.public.sources_file`.

Pour une liste **manuelle** de proxys, éditer aussi
`config/egress_proxies.txt` (une entrée par ligne, `host:port` ou
`socks5://host:port`) : la source `local_text` est relue toutes les 5 minutes,
sans réseau intermédiaire.

## Méthode

1. Recherche GitHub/GitLab/web (mots-clés `free proxy list`, `socks5 list`,
   `https proxy list`, `updated hourly`) et lecture des dépôts.
2. Vérification HTTP du 2026-11-09 : statut 200, format texte/JSON, premiers
   enregistrements, volume, fréquence annoncée.
3. Priorité aux sources :
   - mises à jour automatiquement (5 min à 6 h) ;
   - avec validation amont (ProxyScrape, Geonode, Proxifly, fresh-proxy-list) ;
   - au format brut stable (raw.githubusercontent, API JSON) ;
   - avec protocoles explicites (`http`, `https`, `socks5`).
4. Les forums et pages HTML (Reddit, BlackHatWorld, free-proxy-list.net) ne
   sont **pas** des sources automatiques : pas de format stable, contenu
   changeant, risque de HTML/JS. Ils servent de veille, pas d'ingestion.
5. Chaque source est ensuite re-testée par le vrai gate free ; le classement
   final reste celui des observations, pas la réputation de la liste.

## Sources retenues (priorité haute → basse)

| Priorité | Source | Protocole | Format | Remarque |
|---:|---|---|---|---|
| 100 | Proxifly HTTPS | https | txt | revalidé toutes les 5 min |
| 100 | Proxifly SOCKS5 | socks5 | txt | revalidé toutes les 5 min |
| 95 | fresh-proxy-list (vakhov) | mixte | json | flags http/ssl/socks5, uptime, latence |
| 90 | TheSpeedX HTTP | http | txt | volume historique |
| 90 | TheSpeedX SOCKS5 | socks5 | txt | volume historique |
| 90 | Geonode API | mixte | json | `protocols`, `lastChecked`, 500/page |
| 85 | ErcinDedeoglu HTTPS | https | txt | listes journalières |
| 85 | ErcinDedeoglu SOCKS5 | socks5 | txt | listes journalières |
| 85 | monosans SOCKS5 | socks5 | txt | journalier |
| 80 | OpenProxyList HTTPS | https | txt | volume important |
| 80 | OpenProxyList SOCKS5 | socks5 | txt | volume important |
| 80 | hookzof SOCKS5 | socks5 | txt | SOCKS5 uniquement |
| 75 | jetkai HTTPS | https | txt | online-proxies |
| 75 | jetkai SOCKS5 | socks5 | txt | online-proxies |
| 70 | ProxyScrape HTTPS | https | txt | API v4 |
| 70 | ProxyScrape SOCKS5 | socks5 | txt | API v4 |
| 65 | hideip.me HTTPS | https | txt | format `ip:port:Pays` |
| 60 | hideip.me SOCKS5 | socks5 | txt | format `ip:port:Pays` |
| 50 | ShiftyTR HTTPS | https | txt | secours |
| 45 | roosterkid HTTPS | https | txt | petit volume |
| 45 | ShiftyTR SOCKS5 | socks5 | txt | secours |
| 40 | roosterkid SOCKS5 | socks5 | txt | petit volume |

### Veille désactivée

| Source | Raison |
|---|---|
| `mmpx12` HTTPS/SOCKS5 | préfixe de réponse Cloudflare (`error code: 502/1015`) |
| `VPSLabCloud` HTTPS | chemin 404 au 2026-11-09, à reconfirmer |

## Rendement des sources

Chaque source conserve dans `registry.db` :

- `candidates_count` : candidats ingérés (cumul des fetches) ;
- `usable_count` : options actuellement `usable` attribuées à cette source ;
- `yield_pct` : `usable_count / candidates_count`.

L’UI affiche `usable/candidates (yield %)` à côté de chaque source. Une source
à rendement nul après plusieurs cycles peut être désactivée dans le fichier
ou descendue en priorité. Le rendement est un indicateur de sélection, pas une
preuve : seule l’observation free `200` compte.

## Notation des IP

Chaque option reçoit un score persistant (`egress_options.score`) calculé sur
les observations récentes, comme WARP/NordVPN :

```text
score = 40 % taux de 200
      + 25 % latence (budget 8 s)
      + 20 % débit (neutre si absent)
      + 10 % stabilité
      +  5 % fraîcheur
```

- Le score est recalculé à chaque observation.
- La sélection (`egress_rotation.select_many`) trie par score décroissant.
- Il est visible dans l’API (`score`) et dans la table du dashboard.
- Une IP `quota`/`auth`/`dead` n’est jamais sélectionnée, quel que soit son
  score.

## Ajouter une source

1. Éditer `config/egress_sources.yaml`.
2. Renseigner `id`, `type`, `url`, `scheme`, `interval_s`, `priority`.
3. Le fichier est relu à chaque cycle si son mtime a changé : une source
   ajoutée/désactivée est prise en compte sans redémarrage. Les sources sont
   aussi stockées dans `registry.db` pour l’UI/API.
4. Vérifier les URLs et compter les candidats sans toucher au pool :

```powershell
python scripts/egress_catalog.py sources-check
python scripts/egress_catalog.py sources-check --json
```

L’ajout via l’UI/API écrit dans `registry.db` et est utilisé au cycle suivant
sans redémarrage ; le fichier reste la source de vérité pour un déploiement
propre.

## Sécurité

- URLs limitées à HTTP/HTTPS, sans loopback, réseau privé, link-local ni
  `file://` (garde SSRF).
- Aucune sonde publique ne traverse le processus principal : worker isolé.
- Aucun cookie/clé dans une requête free ; seul `Bearer public` + `ping`.
- TLS destination vérifié ; un proxy SOCKS5/HTTPS malveillant ne peut pas lire
  le contenu sans casser le certificat.
