"""V8.1 — écart entre le catalogue de l'éditeur et ce que le proxy route.

**Le problème que ce module rend visible.** Le catalogue de l'éditeur dit ce qui
est *proposé* ; `config.yaml` dit ce qui est *routé*. Les deux dérivent en silence :
un modèle retiré du catalogue reste routé et renvoie un `404 Model not found`
difficile à relire ; un modèle nouveau reste invisible jusqu'à ce que quelqu'un le
remarque à la main — ce qui est arrivé avec `muse-wonder-1.0` pendant cette session.

**La distinction qui compte, et que ce module refuse de mélanger :**
  * « dans le catalogue, non routé » n'est **pas** une erreur : c'est un
    **candidat**. La session a mesuré que sur 81 ids free non exposés, un seul
    (`big-pickle`) répondait réellement. Un candidat n'est pas un manque.
  * « routé, absent du catalogue » **est** une erreur : le proxy enverra une
    requête pour un modèle que l'éditeur ne propose plus. C'est le seul cas
    actionnable sans mesure.

Confondre les deux ferait produire au endpoint des listes de 80 lignes dont
personne ne peut rien faire — le défaut exact qui a rendu l'audit initial bruyant.
"""

from __future__ import annotations

__all__ = ["annotate", "compute", "summarise"]


def _as_set(values) -> set[str]:
    out: set[str] = set()
    try:
        for value in values or ():
            text = str(value).strip()
            if text:
                out.add(text)
    except Exception:
        pass
    return out


def compute(catalog_ids, routed_ids, leg: str = "") -> dict:
    """Écart pour une jambe. Pur, ne lève jamais.

    ``catalog_ids`` : ids proposés par l'éditeur (clés du snapshot).
    ``routed_ids``  : ids que le proxy sait router aujourd'hui.
    """
    catalogue = _as_set(catalog_ids)
    routes = _as_set(routed_ids)
    candidates = sorted(catalogue - routes)
    orphans = sorted(routes - catalogue)
    common = sorted(catalogue & routes)
    return {
        "leg": leg,
        "catalog_count": len(catalogue),
        "routed_count": len(routes),
        "common_count": len(common),
        "candidates": candidates,
        "orphans": orphans,
        # La liste des communs n'est utile qu'à la marge : on la compte, on ne
        # l'énumère pas (80+ lignes illisibles pour zéro décision).
        "status": "orphans" if orphans else ("candidates" if candidates else "aligned"),
    }


def summarise(sections) -> dict:
    """Vue d'ensemble multi-jambes : ``{"status", "legs", "orphans", "candidates"}``."""
    legs = [s for s in (sections or []) if isinstance(s, dict)]
    orphans = {f"{s.get('leg') or '?'}::{name}" for s in legs for name in (s.get("orphans") or [])}
    candidates = sum(len(s.get("candidates") or []) for s in legs)
    if orphans:
        status = "orphans"
    elif candidates:
        status = "candidates"
    else:
        status = "aligned"
    return {
        "status": status,
        "orphan_count": len(orphans),
        "candidate_count": candidates,
        "orphans": sorted(orphans),
        "legs": legs,
    }


def annotate(section: dict, observed_ids) -> dict:
    """Sépare les orphelins « snapshot périmé » des orphelins « à vérifier ».

    Un id absent du snapshot mais **observé en succès** ne prouve pas qu'il est
    mort : il prouve que le snapshot est plus vieux que la réalité. Cas réel :
    `jev-1.13-free` répond en 4 s et n'apparaît pas dans le snapshot du
    2026-10-20 — sans cette distinction, le endpoint l'aurait signalé comme
    cassé et fait perdre du temps à tout le monde.

    Pur, ne modifie pas l'entrée.
    """
    observed = _as_set(observed_ids)
    out = dict(section or {})
    orphans = list(out.get("orphans") or [])
    stale = [name for name in orphans if name in observed]
    verifier = [name for name in orphans if name not in observed]
    out["orphans_stale_snapshot"] = stale
    out["orphans_to_verify"] = verifier
    return out
