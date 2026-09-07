"""app.streaming — SSE handlers + StreamingResponse (Phase 7 refonte).

[Phase 7] Domicile canonique : ``streaming.sse`` (pompe déplacée depuis
``opencode.py``). Cette façade ré-exporte la pompe ; les wrappers fins
(``_sse_keepalive``/``_sse_coalesce``, défauts config) restent côté hôte.
"""

from streaming.sse import _sse_pump as _sse_pump
from streaming.sse import sse_pump as sse_pump

__all__ = ["_sse_pump", "sse_pump"]
