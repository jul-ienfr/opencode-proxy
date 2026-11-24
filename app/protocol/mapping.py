"""
Protocol mapping: Anthropic <-> OpenAI <-> Responses — FAÇADE.

[T213 2026-11-23] Le monolithe historique (6 329 l.) a été découpé en
sous-modules cohérents :

* ``mapping_io``              — socle : orjson/json, ids d'outils stables,
  usage/cache tokens, effort, schémas, cache-control Anthropic ;
* ``mapping_convert``         — conversion Anthropic→OpenAI + cache de
  conversion (hit/miss, LRU borné) ;
* ``mapping_anthropic``       — OpenAI→Anthropic, signatures locales, thinking
  synthétique ;
* ``mapping_responses``       — conversions Responses API ↔ Anthropic ;
* ``mapping_tools``           — sanitize des noms d'outils, scrubbing d'IDs,
  modalités de modèle, défauts d'échantillonnage ;
* ``mapping_relay``           — relais de champs optionnels, remap
  d'historique/outils ;
* ``mapping_responses_request`` — construction des requêtes/réponses
  Responses ↔ Chat/Anthropic ;
* ``mapping_stream``          — streaming Responses (état SSE, emitter).

Le CORPS des fonctions est identique à l'octet près au monolithe d'origine :
seul le découpage change. Cette façade ré-exporte l'intégralité des symboles
pour que les imports existants (``from app.protocol.mapping import X``)
continuent de fonctionner sans aucune modification des appelants.
"""

import sys as _sys
import types as _types

from . import mapping_anthropic as _p_anthropic
from . import mapping_convert as _p_convert
from . import mapping_io as _p_io
from . import mapping_relay as _p_relay
from . import mapping_responses as _p_responses
from . import mapping_responses_request as _p_responses_request
from . import mapping_stream as _p_stream
from . import mapping_tools as _p_tools

_LIB_PARTS = (
    _p_io,
    _p_convert,
    _p_anthropic,
    _p_responses,
    _p_tools,
    _p_relay,
    _p_responses_request,
    _p_stream,
)

# ── Injection inter-modules ────────────────────────────────────────────────
# Chaque fonction Python résout ses références libres dans les ``__globals__``
# de SON module. Les sous-modules s'appellent mutuellement (souvent en avant :
# ``anthropic_to_openai`` appelle ``filter_empty_anthropic_messages`` défini
# plus loin). On fusionne donc tous les symboles définis par les sous-modules,
# puis on injecte dans chaque module les noms qui lui manquent : les appels
# croisés, forward comme backward, se résolvent alors au runtime sans réécrire
# le corps des fonctions.
#
# Les globals MUTABLES (caches, compteurs) restent définis dans un seul
# sous-module et ne sont jamais réassignés ailleurs (seuls ``_get_encoding`` et
# l'incrément hit/miss utilisent ``global``) : l'objet partagé est injecté par
# référence, donc les mutations restent visibles partout.
_merged: dict = {}
for _part in _LIB_PARTS:
    _merged.update(vars(_part))

for _part in _LIB_PARTS:
    _namespace = vars(_part)
    for _name, _value in _merged.items():
        if _name.startswith("__"):
            continue
        if _name not in _namespace:
            _namespace[_name] = _value

# Ré-export de tous les symboles dans l'espace de noms de la façade.
for _name, _value in _merged.items():
    if _name.startswith("__"):
        continue
    globals()[_name] = _value


class _MappingFacade(_types.ModuleType):
    """Façade de module qui propage les écritures d'attributs aux sous-modules.

    ``monkeypatch.setattr(app.protocol.mapping, "yaml_get", fake)`` (ou tout
    autre patch ciblant la façade, comme les tests le faisaient sur le
    monolithe) doit rester visible par les fonctions des sous-modules : celles-ci
    résolvent leurs références libres dans les ``__globals__`` de LEUR module.
    On répercute donc chaque ``setattr`` sur la façade vers tous les
    sous-modules. Lecture/écriture via le nom de la façade et via un sous-module
    restent ainsi cohérentes.
    """

    def __setattr__(self, name, value):
        super().__setattr__(name, value)
        for _part in _LIB_PARTS:
            setattr(_part, name, value)


_sys.modules[__name__].__class__ = _MappingFacade
