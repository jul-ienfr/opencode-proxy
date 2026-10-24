"""V6.3.4 — SLO de latence : transformer une mesure en verdict.

**Pourquoi ce module existe.** La session vient de passer plusieurs heures a
discuter d'une impression de lenteur, puis a du aller interroger la base et faire
des appels pour trancher. La mesure existait deja (`leg_ms:<jambe>:<modele>`,
V8.3) mais personne ne la comparait a une cible : il fallait un humain pour lire
les percentiles et conclure.

**Ce que ce module ajoute.** Une fonction pure qui prend les series mesurees et
rend un verdict : `ok`, `degraded`, `breached`, `unknown`. Plus de debat possible
sur « est-ce lent ? » — la reponse est un champ du `/health`.

**La regle de decision, et sa justification.**
  * `p50` au-dela de la cible -> **breached** : le cas typique est mauvais, ce
    n'est pas une queue, c'est le regime.
  * `p95` seul au-dela          -> **degraded** : le regime tient, la queue casse.
  * moins de 5 mesures          -> **unknown** : deux appels ne font pas une
    mediane, et un verdict sur deux points est pire qu'aucun verdict.

Le seuil de 5 n'est pas arbitraire : c'est le point ou une mediane cesse de
valoir exactement une des valeurs observees.
"""

from __future__ import annotations

__all__ = ["DEFAULT_TARGETS", "MIN_SAMPLES", "evaluate", "series_from_health"]

#: Cibles du plan (V6.3.4) : p50 < 4 s, p95 < 12 s sur prompt court.
DEFAULT_TARGETS = {"p50_ms": 4000.0, "p95_ms": 12000.0}

#: En dessous, pas de verdict.
MIN_SAMPLES = 5


def _as_series(entries) -> list[dict]:
    """Normalise les entrees (dict de /health ou tuples) en lignes lisibles."""
    rows = []
    if not isinstance(entries, dict):
        return rows
    for key, value in entries.items():
        try:
            if isinstance(value, dict):
                n = int(value.get("n", 0))
                p50 = float(value.get("p50_ms", 0.0))
                p95 = float(value.get("p95_ms", p50))
            else:  # (n, p50, p95, p99)
                n = int(value[0])
                p50 = float(value[1])
                p95 = float(value[2])
            rows.append({"key": str(key), "n": n, "p50_ms": p50, "p95_ms": p95})
        except Exception:
            continue
    return rows


def evaluate(entries, targets: dict | None = None) -> dict:
    """Verdict SLO sur des series mesurees. Pur, ne leve jamais.

    ``entries`` : ``{"leg_ms:free:modele": {"n", "p50_ms", "p95_ms"}}`` (la forme
    exposée par ``/health``) ou des tuples ``(n, p50, p95, p99)``.
    """
    tgt = dict(DEFAULT_TARGETS)
    try:
        if isinstance(targets, dict):
            for name in ("p50_ms", "p95_ms"):
                if name in targets:
                    tgt[name] = float(targets[name])
    except Exception:
        pass

    series = []
    # None = aucun verdict exploitable pour l'instant. Sans ce `None`, un
    # payload entièrement « unknown » (que des séries jeunes) sortirait `ok` —
    # c'est-à-dire « tout va bien » alors qu'on n'a rien mesuré de concluant.
    worst = None
    order = {"unknown": 0, "ok": 1, "degraded": 2, "breached": 3}

    for row in _as_series(entries):
        if row["n"] < MIN_SAMPLES:
            verdict = "unknown"
        elif row["p50_ms"] > tgt["p50_ms"]:
            verdict = "breached"
        elif row["p95_ms"] > tgt["p95_ms"]:
            verdict = "degraded"
        else:
            verdict = "ok"
        row["verdict"] = verdict
        row["p50_target_ms"] = tgt["p50_ms"]
        row["p95_target_ms"] = tgt["p95_ms"]
        series.append(row)
        # `unknown` n'écrase jamais un vrai verdict : une série jeune ne doit pas
        # masquer une série cassée. Mais s'il n'y a QUE des séries jeunes, le
        # verdict global doit être `unknown`, jamais `ok`.
        if verdict != "unknown" and (worst is None or order[verdict] > order[worst]):
            worst = verdict

    series.sort(key=lambda r: (order[r["verdict"]], -r["p50_ms"]), reverse=True)
    breached = [r["key"] for r in series if r["verdict"] in ("breached", "degraded")]
    if worst is None:
        worst = "unknown"
    return {
        "status": worst,
        "targets": tgt,
        "min_samples": MIN_SAMPLES,
        "series": series,
        "offenders": breached,
    }


def series_from_health(payload: dict) -> dict:
    """Extrait les séries ``leg_ms:*`` d'un payload `/health` (pur)."""
    out = {}
    try:
        source = (payload or {}).get("latency_by_leg") or {}
        for key, value in source.items():
            if str(key).startswith("leg_ms:"):
                out[key] = value
    except Exception:
        pass
    return out
