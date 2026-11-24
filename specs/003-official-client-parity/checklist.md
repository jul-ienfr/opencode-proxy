# Checklist — Parité wire officielle (003), révision 2 (2026-11-17)

**Feature** : `003-official-client-parity`
**Portée** : politique deux versions + empreinte TLS mesurée par version.

## Versions et captures

- [x] `VERSION_SPECS` = **1.19.5 + 2.3.0** (plus que deux).
- [x] `PROBE_VERSIONS` = **1.19.5 + 2.3.0**.
- [x] Captures = **2.2.5.json + 2.3.0.json** (`--list`), les autres supprimées (`1.18.31`, `1.19.3`, `1.19.5`).
- [x] `free/impersonation.json` : version **2.3.0**, `client=cli`, `project=global`, `tls.ja3_md5=1523504b…`.
- [x] `free/official_payload.json` : capture 2.3.0, 43 504 o, sha `117db511…`, 13 outils.

## TLS par version

- [x] ClientHello **2.3.0 capturé** (listener local, SNI `localhost`, handshake non terminé) : JA3 MD5 `1523504b38f0fae0d881d4b6554aac1b`, 13 extensions, 11 sigalgs, groupes 4588/29/23/24, ALPN `http/1.1`.
- [x] `VERSION_SPECS["2.3.0"]` : `verified: True`, `tls_captured_at: 2026-11-17`, **pas** de `tls_inherited_from`.
- [x] `VERSION_SPECS["1.19.5"]` : profil vérifié conservé (wire 2.2.5).
- [x] Capture `2.2.5.json` : bloc `tls` annoté `source="1.19.5 prior capture…"` (client 2.2.5 plus installé — provenance explicite, pas une mesure fraîche).

## Cohérence wire

- [x] Sonde = même paire que le client : `probe._version_for` lit la spec, corps 2.3.0 + identité 2.3.0.
- [x] `_free_fp_override` : plus de branches 1.18.31 / 1.19.0 / 1.19.3 ; `2.3.0` traité comme TLS 1.19.5.
- [x] Ordres d'en-têtes `118` et `119_LEGACY` supprimés avec leurs versions.
- [x] Identité vault sur version supprimée → migrée vers la version canonique et re-persistée.

## Outillage

- [x] `--tls` (capture ClientHello), `--list/--json`, `--prune N`, `--restore V`, `--no-archive`, `--auto`, `--test`.
- [x] Diff étendu au corps (`body_bytes`/`body_sha256`) et au TLS (hors `captured_at`).
- [x] `--apply` archive chaque capture ; refus d'une capture sans version ; avertissement si version absente de `VERSION_SPECS`.
- [x] `--capture --tls --diff` **idempotent** (`format unchanged`).

## Gates

- [x] `config_audit --strict` : 0 dead / 0 missing.
- [x] `platform_audit --strict` : 0 violation (263 fichiers).
- [x] `test_option_map.py` : 4 passed.
- [x] 288 tests ciblés (identité, wire, probe, parité, e2e, multi-attempt, config store, socket guard, plan 011, zéro 429) verts.
- [x] `ruff` + `py_compile` propres sur les fichiers modifiés.
- [x] Gate complète `pytest -k "not docker"` (6 226 tests) : **3,3 min, 0 échec**. Débloquée par l'annulation des `_pin_escape_tasks` dans `close()`, l'isolation de la réserve client réelle (fixture conftest + `@pytest.mark.client_reserve`) et la stabilité du `ses_` (cache par conversation + session imposée).
- [x] Échec de gate trouvé et corrigé hors périmètre : `test_quota_bucket_pool.py::test_exhausted_bucket_blocks_station_but_not_tunnel_gate` dépendait de la réserve client réelle (IP factice filtrée) — rendu hermétique par `monkeypatch`.

## Preuves consignées

- [x] T708 — 429 synthétique `all_fallbacks_dead` supprimé : le breaker corrélé ignore les `sid < 0` (churn socks5/WARP). Client : **HTTP 200** sur deux requêtes identiques puis une requête distincte (`audit-202446`) ; `/health` 18/19 utilisables ; gate 6 226 tests, 0 échec.

- [x] `CHANGELOG.md` : entrée `[2026-11-17]`.
- [x] `free/captures/README.md` : politique deux versions + bloc `tls` + options du script.
- [x] Dernière gate complète : 6 226 tests, 3,3 min, 0 échec (T707).
