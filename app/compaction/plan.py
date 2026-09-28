"""app.compaction.plan — plan du résumeur de compaction (pur, DI).

Extrait de ``opencode._compaction_summarizer_plan`` (déplacement pur) : calcule
``(model, endpoint, protocol, api, is_free, seed)`` pour que le résumé de
compaction parte par la MÊME voie que la conversation.

- conversation free → résumeur free (id free + endpoint free, ``Bearer public``) ;
- conversation payante → résumeur payant (route inchangée) ;
- ``summarizer_model_override`` souverain quand il est posé (choix explicite de
  l'opérateur, y compris pour forcer le payant) ;
- endpoint ``/systemone`` (jev, corps typé ``{model,state,questions}``) : jamais
  de corps résumeur chat dessus → repli vers une cible free chat-compatible,
  jamais de bascule payante silencieuse ;
- endpoint free indéterminable → on garde la route d'origine en payant apparent
  (``is_free=False``) pour un échec fail-open, jamais de payant silencieux
  depuis une classe free.

Aucun import projet : ``route_fn``, ``model_config_fn``, ``resolve_free_fn``,
``free_endpoint_fn`` et ``is_free_fn`` sont injectés par le délégué
``opencode._compaction_summarizer_plan``. Ne lève jamais.
"""

from __future__ import annotations


def summarizer_plan(
    model_id,
    override=None,
    *,
    endpoint=None,
    protocol=None,
    api=None,
    route_fn=None,
    model_config_fn=None,
    resolve_free_fn=None,
    free_endpoint_fn=None,
    default_target="",
    is_free_fn=None,
):
    """Plan du résumeur : (model, endpoint, protocol, api, is_free, seed).

    ``seed`` est le nom à donner à la résolution free pour retrouver la même
    machinerie que la conversation (stations, hedge, cooldown, repli direct).
    ``None`` quand la cible free est déjà un id free.
    Compat : les 5 premiers éléments reprennent l'ordre historique
    ``(model, endpoint, protocol, is_free, seed)`` ; ``api`` est inséré en
    4e position pour la parité Responses.
    """
    try:
        _ov = override.strip() if isinstance(override, str) else ""
        m, ep, proto, ap = model_id, endpoint, protocol, api
        if _ov:
            _r = None
            if callable(route_fn):
                try:
                    _r = route_fn(_ov)
                except Exception:
                    _r = None
            if isinstance(_r, dict) and _r.get("model"):
                m = _r["model"]
                if callable(model_config_fn):
                    try:
                        _c = model_config_fn(m)
                        ep = _c.get("endpoint", ep)
                        proto = _c.get("protocol", proto)
                        if "api" in _c:
                            ap = _c.get("api", ap)
                    except Exception:
                        pass

        _is_free = False
        if callable(is_free_fn):
            try:
                _is_free = bool(is_free_fn(m))
            except Exception:
                _is_free = False
        if not _is_free:
            return m, ep, proto, ap, False, None

        _free_id = None
        if callable(resolve_free_fn):
            try:
                _free_id = resolve_free_fn(m)
            except Exception:
                _free_id = None
        _seed = m if _free_id else None
        if not _free_id:
            _free_id = m
        _fep = None
        if callable(free_endpoint_fn):
            try:
                _fep = free_endpoint_fn(_free_id)
            except Exception:
                _fep = None
        if not _fep:
            return m, ep, proto, ap, False, None
        if "/systemone" in str(_fep):
            _alt = str(default_target or "")
            if _alt and _alt != _free_id:
                _aep = None
                if callable(free_endpoint_fn):
                    try:
                        _aep = free_endpoint_fn(_alt)
                    except Exception:
                        _aep = None
                if _aep and "/systemone" not in str(_aep):
                    _aap = "responses" if "/responses" in str(_aep) else "chat"
                    return _alt, _aep, "openai", _aap, True, None
            return m, ep, proto, ap, False, None
        _fap = "responses" if "/responses" in str(_fep) else "chat"
        return _free_id, _fep, "openai", _fap, True, _seed
    except Exception:
        try:
            return model_id, endpoint, protocol, api, False, None
        except Exception:
            return None, None, None, None, False, None


__all__ = ["summarizer_plan"]
