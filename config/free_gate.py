"""Modèle explicite de la porte « free tier » de la passerelle opencode.ai.

Ce module est la **source de vérité unique** de ce que la jambe gratuite doit
envoyer pour être acceptée. Il ne contient ni URL ni valeur d'identité : il
décrit des *prédicats*, que `opencode.py` applique et que les tests vérifient.

Pourquoi un module dédié
------------------------
Pendant des mois la porte a été décrite dans un commentaire de `opencode.py`
(« UA + wire + corps »), avec un facteur manquant (l'IP de sortie) et un
facteur inventé (la version d'UA). Résultat : des 403 impossibles à
diagnostiquer et des tests qui verrouillaient un wire périmé.

Modèle mesuré (2026-10-20, sondes contrôlées via tunnels — voir
`docs/AUDIT_ALIGNEMENT_CLIENT_OFFICIEL_2026-10-20.md` §4.2) ::

    Gate = EgressReputation(ip) ∧ SessionIdentity(headers) ∧ BodyShape(body)

    EgressReputation : facteur externe, non décidable côté client. Une IP
        « directe » de datacenter est refusée même avec un wire parfait ; une
        IP de sortie de type résidentiel passe. Ce module ne peut que
        constater (« inconnu » hors contexte de tunnel).
    SessionIdentity  : au moins un en-tête d'identité de session reconnu.
        Le quartet `x-opencode-client/project/request/session` n'est PAS
        requis (mesuré : le jeu du client 1.19.3, qui ne l'envoie pas, passe).
    BodyShape        : `stream: true` ET au moins un outil de type `read` avec
        une définition réaliste. Un flux sans outils est refusé ; un outil
        `bash` seul ne suffit pas.

Ce que ce module interdit explicitement
--------------------------------------
* exiger une version d'UA précise (aucune n'est discriminante : 1.18.31 et
  1.19.3 passent l'une comme l'autre) ;
* exiger le quartet `x-opencode-*` (contrainte inexistante, charge de
  maintenance gratuite) ;
* décider d'un refus sans nommer le facteur en cause.
"""

from __future__ import annotations

from dataclasses import dataclass, field

#: En-têtes qui satisfont, à eux seuls, le facteur « identité de session ».
#: Mesuré le 2026-10-20 : la passerelle accepte indifféremment l'un des trois
#: premiers ; le quatrième est l'ancien nom (client 1.18.x) et reste accepté.
SESSION_IDENTITY_HEADERS: tuple[str, ...] = (
    "x-opencode-session-id",
    "x-session-affinity",
    "x-session-id",
    "x-opencode-session",
)

#: Quartet historique du client 1.18.x. Conservé ici pour pouvoir *vérifier*
#: qu'il n'est pas requis — pas pour l'exiger.
LEGACY_IDENTITY_HEADERS: tuple[str, ...] = (
    "x-opencode-client",
    "x-opencode-project",
    "x-opencode-request",
    "x-opencode-session",
)

#: Nom d'outil dont la présence satisfait le facteur « forme du corps ».
REQUIRED_SHIM_TOOL: str = "read"

#: Longueur minimale de description pour qu'un shim soit « réaliste ».
#: Un stub `{"name":"read","parameters":{}}` n'est pas crédible.
MIN_SHIM_DESCRIPTION: int = 40

#: Nombre minimal de paramètres attendus dans le schéma du shim.
MIN_SHIM_PARAMS: int = 1


def _lower_keys(headers: dict | None) -> dict:
    """Clés d'en-tête en minuscules → valeur (les noms HTTP sont insensibles à la casse)."""
    if not isinstance(headers, dict):
        return {}
    return {str(k).lower(): v for k, v in headers.items()}


def session_identity(headers: dict | None) -> list[str]:
    """Renvoie les en-têtes d'identité de session effectivement présents (non vides)."""
    low = _lower_keys(headers)
    return [h for h in SESSION_IDENTITY_HEADERS if str(low.get(h) or "").strip()]


def identity_ok(headers: dict | None) -> bool:
    """Facteur « identité de session » : au moins un en-tête reconnu, non vide."""
    return bool(session_identity(headers))


def legacy_identity_present(headers: dict | None) -> list[str]:
    """Quartet historique présent (informatif : jamais une condition d'accès)."""
    low = _lower_keys(headers)
    return [h for h in LEGACY_IDENTITY_HEADERS if str(low.get(h) or "").strip()]


def shim_tools(body: dict | None) -> list[str]:
    """Noms des outils du corps dont la définition est « réaliste »."""
    if not isinstance(body, dict):
        return []
    tools = body.get("tools")
    if not isinstance(tools, list):
        return []
    out: list[str] = []
    for tool in tools:
        if not isinstance(tool, dict):
            continue
        # Deux formes admises : {"type":"function","function":{...}} et {"name":...}
        _raw_fn = tool.get("function")
        fn = _raw_fn if isinstance(_raw_fn, dict) else tool
        name = fn.get("name")
        if not isinstance(name, str) or not name:
            continue
        desc = fn.get("description")
        params = fn.get("parameters") or fn.get("input_schema") or {}
        props = params.get("properties") if isinstance(params, dict) else None
        realistic = (
            isinstance(desc, str)
            and len(desc.strip()) >= MIN_SHIM_DESCRIPTION
            and isinstance(props, dict)
            and len(props) >= MIN_SHIM_PARAMS
        )
        if realistic:
            out.append(name)
    return out


def body_ok(body: dict | None) -> bool:
    """Facteur « forme du corps » : `stream: true` ET un shim `read` réaliste."""
    if not isinstance(body, dict):
        return False
    if body.get("stream") is not True:
        return False
    return REQUIRED_SHIM_TOOL in shim_tools(body)


@dataclass(frozen=True)
class GateVerdict:
    """Diagnostic d'un envoi : quel facteur a décidé, et pourquoi.

    ``passed`` n'est vrai que si les trois facteurs sont satisfaits. Le facteur
    ``egress`` vaut ``"unknown"`` hors contexte de tunnel : on ne peut pas
    prouver la réputation d'une IP depuis le client, seulement la constater.
    """

    egress: str = "unknown"
    identity: bool = False
    body: bool = False
    identity_headers: tuple[str, ...] = field(default_factory=tuple)
    shim_tools: tuple[str, ...] = field(default_factory=tuple)

    @property
    def passed(self) -> bool:
        return self.identity and self.body and self.egress != "rejected"

    @property
    def failed_factors(self) -> list[str]:
        """Facteurs en échec, nommés — c'est ce qui rend un 403 diagnosticable."""
        out = []
        if self.egress == "rejected":
            out.append("egress")
        if not self.identity:
            out.append("identity")
        if not self.body:
            out.append("body")
        return out

    def as_log(self) -> dict:
        """Forme sérialisable pour les logs structurés (jamais envoyée au client)."""
        return {
            "gate": {
                "passed": self.passed,
                "failed": self.failed_factors,
                "egress": self.egress,
                "identity": self.identity,
                "body": self.body,
                "identity_headers": list(self.identity_headers),
                "shim_tools": list(self.shim_tools),
            }
        }


def diagnose(headers: dict | None, body: dict | None, egress: str = "unknown") -> GateVerdict:
    """Évalue la porte sur un envoi donné, sans réseau.

    Args:
        headers: en-têtes qui *partiront* vers l'amont (pas ceux du client).
        body: corps JSON qui *partira* vers l'amont.
        egress: ``"ok"`` (tunnel vérifié), ``"rejected"`` (403 constaté sur
            cette sortie) ou ``"unknown"`` (défaut, hors mesure).
    """
    return GateVerdict(
        egress=egress,
        identity=identity_ok(headers),
        body=body_ok(body),
        identity_headers=tuple(session_identity(headers)),
        shim_tools=tuple(shim_tools(body)),
    )
