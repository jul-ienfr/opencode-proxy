"""Adapte les noms d'outils émis par l'amont au vocabulaire réel du client.

[2026-11-12] Mesure : la jambe free injecte la grille officielle (``bash``,
``read``, ``glob``…) pour franchir le gate. Le prompt officiel la décrit
longuement, et un petit modèle appelle donc ces noms même quand le client en
déclare d'autres. Or le client valide strictement :

* Codex → ``unsupported call: bash`` / ``unsupported call: read`` ;
* Claude Code → ``No such tool available: bash … call Bash instead`` ;
* opencode desktop → son outil shell s'appelle ``shell``, pas ``bash``.

On adapte la **sortie** uniquement (jamais l'entrée : le gate free reste
intact) :

* casse exacte du client (``bash`` → ``Bash``) ;
* même famille (``bash`` → ``shell`` / ``exec_command`` / ``Bash``) ;
* clé d'argument attendue (``command`` ↔ ``cmd``, ``path`` ↔ ``file_path``) ;
* repli shell pour ``read`` / ``glob`` / ``grep`` / ``write`` / ``edit`` /
  ``webfetch`` quand le client n'a que ``exec_command`` (Codex) ou ``Bash``
  (Claude Code sans ``WebFetch``), avec la syntaxe PowerShell ou POSIX.
"""

from __future__ import annotations

import base64
import json
import sys

_FAMILY_MEMBERS = {
    "shell": {
        "bash",
        "bash_tool",
        "shell",
        "shell_command",
        "exec",
        "exec_command",
        "run",
        "run_command",
        "terminal",
        "command",
        "powershell",
        "pwsh",
        "local_shell",
        "local_shell_call",
        "sh",
        "cmd",
    },
    "read": {
        "read",
        "read_file",
        "readfile",
        "read_text_file",
        "view_file",
        "open_file",
        "get_file_contents",
        "cat",
    },
    "glob": {
        "glob",
        "glob_files",
        "find_files",
        "file_search",
        "list_files",
        "list_dir",
        "list_directory",
        "ls",
        "find",
    },
    "grep": {
        "grep",
        "search",
        "search_files",
        "code_search",
        "find_in_files",
        "ripgrep",
        "rg",
    },
    "write": {
        "write",
        "write_file",
        "writefile",
        "create_file",
        "save_file",
        "create",
    },
    "edit": {
        "edit",
        "edit_file",
        "apply_patch",
        "apply_diff",
        "patch",
        "str_replace",
        "replace",
        "multiedit",
    },
    "question": {
        "question",
        "ask_user_question",
        "askuserquestion",
        "ask_user",
        "request_user_input",
    },
    "webfetch": {
        "webfetch",
        "web_fetch",
        "fetch",
        "fetch_url",
        "http_get",
        "url_fetch",
    },
    "websearch": {
        "websearch",
        "web_search",
        "search_web",
        "web_query",
    },
    "todo": {
        "todowrite",
        "todo_write",
        "todo",
        "update_plan",
    },
    "skill": {
        "skill",
        "skill_view",
        "load_skill",
        "skill_manage",
    },
    "subagent": {
        "subagent",
        "subagent_fork",
        "task",
        "agent",
        "multi_agent_v1",
    },
}

#: Ordre de préférence quand le client déclare plus d'un outil dans une famille.
#: opencode desktop expose ``shell`` **et** ``execute`` : ``execute`` est un
#: runtime JavaScript (``{"code": …}``), pas un shell — il ne doit jamais
#: recevoir une commande, sinon l'appel arrive vide après filtrage de schéma.
_FAMILY_PREFERENCE = {
    "shell": (
        "bash",
        "shell",
        "powershell",
        "pwsh",
        "exec_command",
        "terminal",
        "local_shell",
        "run_command",
        "sh",
        "cmd",
        "exec",
        "run",
        "command",
    ),
    "read": ("read", "read_file", "view_file", "read_text_file", "readfile"),
    "write": ("write", "write_file", "create_file", "save_file", "writefile"),
    "glob": ("glob", "glob_files", "find_files", "list_files", "list_dir"),
    "grep": ("grep", "search_files", "find_in_files", "ripgrep", "search"),
}

_FAMILY_OF = {
    member: family for family, members in _FAMILY_MEMBERS.items() for member in members
}

_SHELL_ARGS = ("command", "cmd", "script", "input", "args")
_READ_ARGS = ("file_path", "path", "filePath", "file", "filename", "target_file")
_WRITE_CONTENT_ARGS = ("content", "text", "data", "file_text")


def _tool_name(tool) -> str:
    if not isinstance(tool, dict):
        return ""
    name = tool.get("name")
    if isinstance(name, str) and name.strip():
        return name.strip()
    fn = tool.get("function")
    if isinstance(fn, dict):
        name = fn.get("name")
        if isinstance(name, str) and name.strip():
            return name.strip()
    return ""


def _tool_schema(tool) -> dict:
    if not isinstance(tool, dict):
        return {}
    params = tool.get("parameters")
    if not isinstance(params, dict):
        params = tool.get("input_schema")
    if not isinstance(params, dict):
        fn = tool.get("function")
        if isinstance(fn, dict):
            params = fn.get("parameters")
    return params if isinstance(params, dict) else {}


def _primary_arg(schema: dict, candidates) -> str:
    props = schema.get("properties")
    if not isinstance(props, dict) or not props:
        return candidates[0]
    required = schema.get("required")
    required = required if isinstance(required, list) else []
    for key in required:
        if key in candidates and key in props:
            return key
    for key in candidates:
        if key in props:
            return key
    return candidates[0]


def declared_tool_names(tools) -> list[str]:
    """Noms réellement déclarés, dans l'ordre (Chat, Responses ou Anthropic)."""
    names: list[str] = []
    for tool in tools or []:
        if not isinstance(tool, dict) or tool.get("type") == "namespace":
            continue
        name = _tool_name(tool)
        if name and name not in names:
            names.append(name)
    return names


def build_tool_alias_map(tools) -> dict:
    """Indexe les outils client pour remapper un appel amont sans ambiguïté."""
    names = declared_tool_names(tools)
    if not names:
        return {}
    by_name = {}
    for tool in tools or []:
        name = _tool_name(tool)
        if name and name not in by_name:
            by_name[name] = tool
    families: dict[str, list[str]] = {}
    args: dict[str, str] = {}
    props: dict[str, set[str]] = {}
    required: dict[str, tuple[str, ...]] = {}
    for name in names:
        family = _FAMILY_OF.get(name.lower())
        if family:
            families.setdefault(family, []).append(name)
        schema = _tool_schema(by_name.get(name))
        if family == "shell":
            args[name] = _primary_arg(schema, _SHELL_ARGS)
        elif family == "read":
            args[name] = _primary_arg(schema, _READ_ARGS)
        elif family in ("write", "edit"):
            args[name] = _primary_arg(schema, _READ_ARGS)
        elif family in ("glob", "grep"):
            args[name] = _primary_arg(schema, ("pattern", "regex", "query", "glob"))
        _props = schema.get("properties")
        if isinstance(_props, dict):
            props[name] = {str(k) for k in _props}
        _required = schema.get("required")
        if isinstance(_required, list):
            required[name] = tuple(str(k) for k in _required)
    for _family, _members in families.items():
        _order = {
            _name: _index
            for _index, _name in enumerate(_FAMILY_PREFERENCE.get(_family, ()))
        }
        _members.sort(key=lambda _n: _order.get(_n.lower(), len(_order) + 1))
    shell_names = families.get("shell") or []
    return {
        "exact": set(names),
        "lower": {name.lower(): name for name in reversed(names)},
        "families": families,
        "args": args,
        "props": props,
        "required": required,
        "shell": shell_names[0] if shell_names else None,
        "shell_arg": args.get(shell_names[0]) if shell_names else None,
    }


def adapt_tool_name(name: str, aliases: dict | None) -> str:
    """Nom à émettre vers le client (casse/famille), sans toucher aux args."""
    raw = str(name or "").strip()
    if not raw or not aliases:
        return raw
    if raw in aliases.get("exact", ()):
        return raw
    lower = raw.lower()
    target = aliases.get("lower", {}).get(lower)
    if target:
        return target
    family = _FAMILY_OF.get(lower)
    if family:
        candidates = aliases.get("families", {}).get(family) or []
        if candidates:
            return candidates[0]
    return raw


def needs_shell_fallback(name: str, aliases: dict | None) -> bool:
    """True si le client ne déclare aucun outil de cette famille mais un shell.

    Sert aux flux : les arguments doivent alors être retenus jusqu'à la fin de
    l'appel pour être convertis en commande (on ne peut pas réécrire
    ``{"url": …}`` fragment par fragment).
    """
    family = _FAMILY_OF.get(str(name or "").strip().lower())
    if family not in ("read", "glob", "grep", "write", "edit", "webfetch"):
        return False
    if not aliases:
        return False
    if aliases.get("families", {}).get(family):
        return False
    return bool(aliases.get("shell"))


def tool_call_target(name: str, aliases: dict | None) -> str:
    """Nom à émettre immédiatement (bloc ``tool_use``/``content_block_start``).

    Applique la casse/famille du client, puis bascule sur son outil shell quand
    la famille n'existe pas chez lui (``webfetch`` → ``Bash``).
    """
    target = adapt_tool_name(name, aliases)
    if needs_shell_fallback(str(name or ""), aliases):
        return str((aliases or {}).get("shell") or target)
    return target


def _parse_object(arguments) -> tuple[dict | None, bool]:
    if isinstance(arguments, dict):
        return dict(arguments), True
    text = str(arguments or "").strip()
    if not text:
        return {}, True
    try:
        value = json.loads(text)
    except Exception:
        return None, False
    return (value, True) if isinstance(value, dict) else (None, False)


def _first(obj: dict, keys) -> object:
    for key in keys:
        value = obj.get(key)
        if isinstance(value, str) and value.strip():
            return value
    return ""


def _ps_quote(value) -> str:
    return "'" + str(value).replace("'", "''") + "'"


def _sh_quote(value) -> str:
    return "'" + str(value).replace("'", "'\\''") + "'"


def _quoting(shell_kind: str):
    return _ps_quote if shell_kind in ("powershell", "pwsh", "cmd") else _sh_quote


def _shell_kind_for_target(target: str, shell_kind: str) -> str:
    """Langage à générer d'après l'outil shell réellement visé.

    Claude Code déclare ``Bash`` avant ``PowerShell`` : une commande
    PowerShell envoyée à ``Bash`` échouerait, et inversement.
    """
    low = str(target or "").lower()
    if low in ("powershell", "pwsh"):
        return "powershell"
    if low in ("bash", "sh", "zsh", "fish"):
        return "posix"
    if low == "cmd":
        return "cmd"
    return shell_kind


def _leaf(pattern: str) -> str:
    return str(pattern or "").replace("\\", "/").rsplit("/", 1)[-1]


def _synthesize_shell(family: str, obj: dict, shell_kind: str) -> dict | None:
    """Traduit read/glob/grep en commande shell pour un client sans cet outil."""
    quote = _quoting(shell_kind)
    windows = shell_kind in ("powershell", "pwsh", "cmd")
    if family == "read":
        path = _first(obj, _READ_ARGS)
        if not path:
            return None
        command = (
            f"Get-Content -LiteralPath {quote(path)}"
            if windows
            else f"cat -- {quote(path)}"
        )
        return {"command": command}
    if family == "glob":
        pattern = str(_first(obj, ("pattern", "glob", "query")) or "")
        base = str(_first(obj, ("path", "dir", "directory", "cwd")) or ".")
        if not pattern or pattern in ("**/*", "**", "*", "**/*.*"):
            command = (
                f"Get-ChildItem -Path {quote(base)} -Recurse -File | Select-Object -ExpandProperty FullName"
                if windows
                else f"find {quote(base)} -type f"
            )
        else:
            command = (
                f"Get-ChildItem -Path {quote(base)} -Recurse -File -Filter {quote(_leaf(pattern))} "
                "| Select-Object -ExpandProperty FullName"
                if windows
                else f"find {quote(base)} -type f -name {quote(_leaf(pattern))}"
            )
        return {"command": command}
    if family == "grep":
        pattern = str(_first(obj, ("pattern", "query", "regex")) or "")
        if not pattern:
            return None
        base = str(_first(obj, ("path", "dir", "directory", "cwd")) or ".")
        if windows:
            command = (
                f"Get-ChildItem -Path {quote(base)} -Recurse -File | "
                f"Select-String -Pattern {quote(pattern)}"
            )
        else:
            command = f"grep -rn -- {quote(pattern)} {quote(base)}"
        return {"command": command}
    if family == "webfetch":
        # Claude Code ne déclare pas ``WebFetch`` dans son runtime courant :
        # l'appel ``webfetch`` de la grille officielle arrivait tel quel et le
        # client répondait « No such tool available: webfetch ». On récupère le
        # contenu via son outil shell (curl côté POSIX, Invoke-WebRequest côté
        # PowerShell), ce qui préserve le contrat « renvoie le corps de l'URL ».
        # ``path`` : le placeholder officiel du gate ne déclare que cette clé,
        # et un petit modèle la remplit avec l'URL à récupérer.
        url = _first(obj, ("url", "uri", "href", "link", "path"))
        if not url:
            return None
        if shell_kind in ("powershell", "pwsh"):
            command = f"(Invoke-WebRequest -UseBasicParsing -Uri {quote(url)}).Content"
        else:
            command = f"curl -sL --max-time 60 {quote(url)}"
        return {"command": command}
    if family == "write":
        path = _first(obj, _READ_ARGS)
        content = _first(obj, _WRITE_CONTENT_ARGS)
        if not path or content == "":
            return None
        payload = base64.b64encode(str(content).encode()).decode()
        if windows:
            command = (
                f"[IO.File]::WriteAllBytes({quote(path)}, "
                f"[Convert]::FromBase64String({quote(payload)}))"
            )
        else:
            command = f"printf '%s' {quote(payload)} | base64 -d > {quote(path)}"
        return {"command": command}
    if family == "edit":
        path = _first(obj, _READ_ARGS)
        old = _first(obj, ("oldString", "old_string", "old", "find"))
        new = _first(obj, ("newString", "new_string", "new", "replace"))
        if not path or old == "" or new == "":
            return None
        replace_all = bool(obj.get("replaceAll") or obj.get("replace_all"))
        old_b64 = base64.b64encode(str(old).encode()).decode()
        new_b64 = base64.b64encode(str(new).encode()).decode()
        if windows:
            # Remplacement exact (première occurrence par défaut, toutes si
            # replaceAll) sans passer par une regex ni du quoting de contenu.
            command = (
                f"$p={quote(path)}; "
                "$t=[IO.File]::ReadAllText($p); "
                f"$o=[Text.Encoding]::UTF8.GetString([Convert]::FromBase64String({quote(old_b64)})); "
                f"$n=[Text.Encoding]::UTF8.GetString([Convert]::FromBase64String({quote(new_b64)})); "
                "$i=$t.IndexOf($o); "
                "if($i -lt 0){throw 'oldString not found'}; "
                f"if(${'true' if replace_all else 'false'}){{$t=$t.Replace($o,$n)}}"
                "else{$t=$t.Substring(0,$i)+$n+$t.Substring($i+$o.Length)}; "
                "[IO.File]::WriteAllText($p,$t)"
            )
        else:
            flag = "1" if replace_all else "0"
            command = (
                "python3 -c \"import base64,sys;"
                "p,o,n=sys.argv[1],base64.b64decode(sys.argv[2]),base64.b64decode(sys.argv[3]);"
                "t=open(p,'rb').read();i=t.find(o);"
                "assert i>=0,'oldString not found';"
                "open(p,'wb').write(t.replace(o,n) if sys.argv[4]=='1' "
                "else t[:i]+n+t[i+len(o):])\" "
                f"{quote(path)} {quote(old_b64)} {quote(new_b64)} {quote(flag)}"
            )
        return {"command": command}
    return None


def _remap_args(
    family: str,
    target_family: str,
    target: str,
    obj: dict,
    aliases: dict,
    *,
    strict: bool = True,
) -> dict:
    """Renomme les clés d'arguments attendues par l'outil cible."""
    out = dict(obj)
    if target_family == "shell" and family != "shell":
        command = _first(out, _SHELL_ARGS)
        for key in _SHELL_ARGS:
            out.pop(key, None)
        if command:
            out[aliases.get("args", {}).get(target, "command")] = command
        required = aliases.get("required", {}).get(target) or ()
        if "description" in required and not str(out.get("description") or "").strip():
            seed = command if isinstance(command, str) and command else "Run command"
            out["description"] = seed.strip().splitlines()[0][:80]
    elif family == "shell" and target_family == "shell":
        props = aliases.get("props", {}).get(target) or set()
        command = _first(out, _SHELL_ARGS)
        key = aliases.get("args", {}).get(target)
        if command and key:
            for source in _SHELL_ARGS:
                if source != key:
                    out.pop(source, None)
            out[key] = command
        # Noms de champs propres au client (Harness ``timeoutMs``,
        # ``run_in_background`` ; Codex ``yield_time_ms``).
        if "timeoutMs" in props and "timeout" in out:
            out["timeoutMs"] = out.pop("timeout")
        if "yield_time_ms" in props and "timeout" in out:
            out["yield_time_ms"] = out.pop("timeout")
        if "run_in_background" in props and "background" in out:
            out["run_in_background"] = out.pop("background")
        required = aliases.get("required", {}).get(target) or ()
        if "description" in required and not str(out.get("description") or "").strip():
            seed = command if isinstance(command, str) and command else "Run command"
            out["description"] = seed.strip().splitlines()[0][:80]
    elif family in ("read", "write", "edit") and target_family == family:
        path = _first(out, _READ_ARGS)
        if path:
            for key in _READ_ARGS:
                out.pop(key, None)
            out[aliases.get("args", {}).get(target, "file_path")] = path
        if family == "write":
            content = _first(out, _WRITE_CONTENT_ARGS)
            if content != "":
                props = aliases.get("props", {}).get(target)
                key = "content"
                if props:
                    for candidate in _WRITE_CONTENT_ARGS:
                        if candidate in props:
                            key = candidate
                            break
                for candidate in _WRITE_CONTENT_ARGS:
                    out.pop(candidate, None)
                out[key] = content
        elif family == "edit":
            # Claude Code attend ``old_string``/``new_string``/``replace_all``
            # là où la grille officielle émet ``oldString``/``newString``/
            # ``replaceAll``.
            props = aliases.get("props", {}).get(target) or set()
            for snake, camel in (
                ("old_string", "oldString"),
                ("new_string", "newString"),
                ("replace_all", "replaceAll"),
            ):
                if snake in props and camel in out:
                    out[snake] = out.pop(camel)
    elif family == "grep":
        # Claude Code nomme ``glob`` le filtre de fichiers que la grille
        # officielle appelle ``include``.
        props = aliases.get("props", {}).get(target) or set()
        if "glob" in props and "include" in out:
            out["glob"] = out.pop("include")
    elif family == "websearch":
        props = aliases.get("props", {}).get(target) or set()
        if "queries" in props and "query" in out:
            query = out.pop("query")
            out["queries"] = query if isinstance(query, list) else [query]
    elif family == "skill":
        props = aliases.get("props", {}).get(target) or set()
        if "skill" in props:
            for source in ("id", "name"):
                if source in out:
                    out["skill"] = out.pop(source)
                    break
        elif "name" in props and "id" in out:
            out["name"] = out.pop("id")
    elif family == "subagent":
        props = aliases.get("props", {}).get(target) or set()
        if "run_in_background" in props and "background" in out:
            out["run_in_background"] = out.pop("background")
    # Un client strict (Codex : additionalProperties=false) refuse les clés
    # inconnues : on ne garde que celles du schéma cible quand on le connaît.
    # Un simple changement de casse (Bash → Bash) ne doit rien filtrer.
    props = aliases.get("props", {}).get(target) if strict else None
    if props:
        out = {key: value for key, value in out.items() if key in props}
    return out


def adapt_tool_call(
    name: str,
    arguments,
    aliases: dict | None,
    *,
    shell_kind: str = "powershell",
    allow_shell_fallback: bool = True,
) -> tuple[str, object]:
    """Adapte un appel amont complet (nom + arguments) au client.

    Retourne ``(nom, arguments)``. Les arguments sont rendus sous la même forme
    qu'en entrée (``str`` si l'entrée était une chaîne JSON) et inchangés dès
    qu'on ne sait pas les réécrire sans risque.
    """
    original = str(name or "").strip()
    if not original or not aliases:
        return original, arguments
    target = adapt_tool_name(original, aliases)
    family = _FAMILY_OF.get(original.lower())
    target_family = _FAMILY_OF.get(target.lower())
    obj, ok = _parse_object(arguments)
    if not ok:
        return target, arguments

    before = dict(obj)

    if (
        family in ("read", "glob", "grep", "write", "edit", "webfetch")
        and not aliases.get("families", {}).get(family)
        and allow_shell_fallback
        and aliases.get("shell")
    ):
        # Aucun équivalent déclaré par le client : repli shell si possible.
        # (Si le client déclare l'outil — même via un autre nom/casse — on ne
        # synthétise jamais de commande à sa place.)
        target = aliases["shell"]
        target_family = "shell"
        synthesized = _synthesize_shell(
            family, obj, _shell_kind_for_target(target, shell_kind)
        )
        if synthesized is not None:
            obj = synthesized
        else:
            target = adapt_tool_name(original, aliases)
            target_family = _FAMILY_OF.get(target.lower())

    cross_family = target_family != family
    if cross_family:
        obj = _remap_args(family or "", target_family or "", target, obj, aliases)
    elif family == "shell":
        # Même famille shell : clé de commande, champs de timeout/background
        # et description requise varient selon le client (Bash, shell, pwsh,
        # exec_command). On applique le schéma cible quand il est connu.
        if aliases.get("props", {}).get(target):
            obj = _remap_args("shell", "shell", target, obj, aliases)
        else:
            command = _first(obj, _SHELL_ARGS)
            key = aliases.get("args", {}).get(target)
            if command and key and key not in obj:
                for source in _SHELL_ARGS:
                    obj.pop(source, None)
                obj[key] = command
    elif family in ("read", "write", "edit"):
        # Même famille : renommer la clé de chemin (et, pour edit, les clés
        # old/new attendues par le client), puis filtrer sur son schéma.
        if aliases.get("props", {}).get(target):
            obj = _remap_args(family, target_family or family, target, obj, aliases)
        else:
            path = _first(obj, _READ_ARGS)
            key = aliases.get("args", {}).get(target)
            if path and key and key not in obj:
                for source in _READ_ARGS:
                    obj.pop(source, None)
                obj[key] = path
    elif family == "grep":
        obj = _remap_args(family, target_family or family, target, obj, aliases)
    elif family == "glob":
        obj = _remap_args(family, target_family or family, target, obj, aliases)
    elif family in ("question", "webfetch", "websearch", "todo", "skill", "subagent"):
        obj = _remap_args(family, target_family or family, target, obj, aliases)

    if obj == before:
        return target, arguments
    return target, json.dumps(obj, ensure_ascii=False, separators=(",", ":"))


def adapt_chat_completion(
    resp,
    aliases: dict | None,
    *,
    shell_kind: str = "powershell",
) -> bool:
    """Adapte en place les tool_calls d'une réponse Chat (ou d'un objet Responses).

    Retourne ``True`` si au moins un nom/argument a changé, pour que l'appelant
    sache qu'il doit re-sérialiser le corps au lieu de relayer les octets amont.
    """
    if not isinstance(resp, dict) or not aliases:
        return False
    changed = False

    def _adapt(tc: dict) -> None:
        nonlocal changed
        fn = tc.get("function") if isinstance(tc.get("function"), dict) else None
        if fn is not None:
            holder, name_key, args_key = fn, "name", "arguments"
        else:
            holder, name_key, args_key = tc, "name", "arguments"
        name = holder.get(name_key)
        if not isinstance(name, str) or not name:
            return
        new_name, new_args = adapt_tool_call(
            name, holder.get(args_key) or "", aliases, shell_kind=shell_kind
        )
        if new_name != name:
            holder[name_key] = new_name
            changed = True
        if new_args != (holder.get(args_key) or ""):
            holder[args_key] = new_args
            changed = True

    for choice in resp.get("choices") or []:
        if not isinstance(choice, dict):
            continue
        msg = choice.get("message")
        if isinstance(msg, dict):
            for tc in msg.get("tool_calls") or []:
                if isinstance(tc, dict):
                    _adapt(tc)
    for item in resp.get("output") or []:
        if isinstance(item, dict) and item.get("type") == "function_call":
            _adapt(item)
    return changed


def detect_shell_kind(body) -> str:
    """PowerShell ou POSIX, d'après le contexte d'environnement du client."""
    texts: list[str] = []

    def _collect(value, depth: int) -> None:
        if depth > 6 or len(texts) > 4000:
            return
        if isinstance(value, str):
            texts.append(value)
        elif isinstance(value, dict):
            for child in value.values():
                _collect(child, depth + 1)
        elif isinstance(value, list):
            for child in value:
                _collect(child, depth + 1)

    if isinstance(body, dict):
        for key in ("messages", "input", "system"):
            _collect(body.get(key), 0)
    blob = "\n".join(texts).lower()
    if "<shell>powershell" in blob or "<shell>pwsh" in blob or "<shell>cmd" in blob:
        return "powershell"
    if any(marker in blob for marker in ("<shell>bash", "<shell>zsh", "<shell>sh<", "<shell>fish")):
        return "posix"
    if "windows" in blob:
        return "powershell"
    if "linux" in blob or "darwin" in blob or "macos" in blob:
        return "posix"
    return "powershell" if sys.platform == "win32" else "posix"
