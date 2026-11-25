"""Recognize one corroborated two-pack continuation in native Pi history.

This is transcript correlation, not sender authentication or task verification.
The commands and their outputs are inspected as text; nothing in them is run.
"""

from __future__ import annotations

import ast
import json
import re
import shlex

_PACK = r"[0-9a-f]{32}"
_ADDRESS = r"[A-Za-z0-9_.:-]+"
_NOTICE = re.compile(
    rf'<hc from="(?P<sender>{_ADDRESS})(?: {_ADDRESS})?">'
    rf"Herdr two-pack receipt saved for (?P<pack>{_PACK})/"
    r"(?P<role>coder|cleaner)/(?P<round>[1-9][0-9]*) "
    r"\((?P<status>done|pass)\)\. Decision: (?P<decision>active|completed)\. "
    r"Inspect with [^<>\r\n]+\.</hc>"
)
_BINDING_FILTER = "{id,phase,leader,workers,current,summary,round_count:(.rounds|length)}"
_COMPLETED_FILTER = (
    "{id,phase,leader,workers,branch,baseline,current,summary,"
    "assignments:[.assignments[]|{id,role,target,rejections,receipt}]}"
)
_TERMINALS_FILTER = ".terminals[]|{address,status,turn}"
_BINDING_FIELDS = {
    "id",
    "phase",
    "leader",
    "workers",
    "current",
    "summary",
    "rounds",
    "branch",
    "baseline",
    "project",
}
_RECEIPT_FIELDS = {
    "role",
    "round",
    "target",
    "worker:.receipt.worker",
    "receipt_id:.receipt.id",
    "status:.receipt.status",
    "commit:.receipt.result.commit",
    "checks:.receipt.result.checks",
}
_RELATIVE = r"\.\./[A-Za-z0-9_.-]+(?:/[A-Za-z0-9_.-]+)*"
_HELPER = r"/[A-Za-z0-9_./-]+/scripts/handoff\.py"


def _unique(pairs):
    obj = {}
    for key, value in pairs:
        if key in obj:
            raise ValueError("duplicate key")
        obj[key] = value
    return obj


def _constant(value):
    raise ValueError("invalid constant")


def _path(value):
    return re.fullmatch(r"/[A-Za-z0-9_./-]+", value) is not None and all(
        part not in (".", "..") for part in value.split("/")
    )


def _relative(value):
    return re.fullmatch(_RELATIVE, value) is not None and all(
        part not in (".", "..") for part in value.split("/")[1:]
    )


def _projection_fields(value, *, receipts=False):
    """Recognize a small declarative jq projection; never evaluate jq or shell."""
    value = "".join(value.split())
    if not value.startswith("{") or not value.endswith("}"):
        return None
    body = value[1:-1]
    if receipts:
        pattern = r"receipts:\[\.assignments\[\]\|\{([a-z:._,]+)\}\]"
        name, allowed, required = "receipts", _RECEIPT_FIELDS, _RECEIPT_FIELDS
    else:
        pattern = r"rounds:\[\.rounds\[\]\?\|\{([a-z,]+)\}\]"
        name, allowed, required = "rounds", {"role", "round", "status"}, set()
    nested = list(re.finditer(pattern, body))
    if len(nested) > 1 or (receipts and not nested):
        return None
    if nested:
        fields = nested[0].group(1).split(",")
        if (
            len(fields) != len(set(fields))
            or not set(fields) <= allowed
            or not required <= set(fields)
        ):
            return None
        body = body[: nested[0].start()] + name + body[nested[0].end() :]
    fields = body.split(",")
    allowed = (
        {"id", "phase", "leader", "workers", "branch", "receipts"} if receipts else _BINDING_FIELDS
    )
    required = allowed if receipts else {"phase", "leader", "workers", "current"}
    if len(fields) != len(set(fields)) or not set(fields) <= allowed or not required <= set(fields):
        return None
    return frozenset(fields)


def _same_relative_store(cwd, store, expression):
    """Compare literal path components only; do not resolve paths or expand shell."""
    if not isinstance(cwd, str) or not _path(cwd) or not isinstance(store, str) or not _path(store):
        return False
    if not isinstance(expression, tuple) or len(expression) != 2 or expression[0] != "realpath":
        return False
    relative = expression[1]
    if not isinstance(relative, str) or not _relative(relative):
        return False
    cwd_parts = [part for part in cwd.split("/") if part]
    store_parts = [part for part in store.split("/") if part]
    return bool(cwd_parts) and store_parts == cwd_parts[:-1] + relative.split("/")[1:]


def _preflight_program(source):
    """Statically recognize a non-mutating Python guard, never run its code.

    The result maps assertion source lines to the literal values of the enclosing
    loop. Unsupported statements, calls, writes, output, or control flow reject
    the entire guard, including code after the failing assertion.
    """
    if len(source) > 4096:
        return None
    try:
        tree = ast.parse(source)
    except (SyntaxError, ValueError, RecursionError):
        return None
    allowed = {
        ast.Module,
        ast.Import,
        ast.alias,
        ast.Assign,
        ast.Expr,
        ast.For,
        ast.Assert,
        ast.Name,
        ast.Load,
        ast.Store,
        ast.Constant,
        ast.Tuple,
        ast.List,
        ast.Subscript,
        ast.Slice,
        ast.Call,
        ast.Attribute,
        ast.JoinedStr,
        ast.FormattedValue,
        ast.GeneratorExp,
        ast.comprehension,
        ast.Compare,
        ast.Eq,
        ast.NotEq,
        ast.In,
    }
    nodes = list(ast.walk(tree))
    if len(nodes) > 512 or any(type(node) not in allowed for node in nodes):
        return None
    reserved = {"os", "sys", "dict", "open", "print"}
    worker_names, stores, witnesses, prints = set(), {}, {}, []
    for node in nodes:
        if isinstance(node, ast.Import) and any(
            alias.name not in ("os", "sys") or alias.asname is not None for alias in node.names
        ):
            return None
        if isinstance(node, ast.Name) and isinstance(node.ctx, ast.Store):
            if node.id in reserved:
                return None
            stores[node.id] = stores.get(node.id, 0) + 1
        if isinstance(node, (ast.Attribute, ast.Subscript)) and isinstance(node.ctx, ast.Store):
            return None
        if isinstance(node, ast.comprehension) and (
            not _simple_target(node.target) or node.is_async
        ):
            return None
        if isinstance(node, ast.Assign):
            if any(not _simple_target(target) for target in node.targets):
                return None
            # The second positional argument is the restricted terminal address.
            if (
                len(node.targets) == 1
                and isinstance(node.targets[0], ast.Tuple)
                and len(node.targets[0].elts) == 2
                and isinstance(node.targets[0].elts[1], ast.Name)
                and isinstance(node.value, ast.Subscript)
                and isinstance(node.value.value, ast.Attribute)
                and isinstance(node.value.value.value, ast.Name)
                and node.value.value.value.id == "sys"
                and node.value.value.attr == "argv"
                and isinstance(node.value.slice, ast.Slice)
                and isinstance(node.value.slice.lower, ast.Constant)
                and node.value.slice.lower.value == 1
                and node.value.slice.upper is None
                and node.value.slice.step is None
            ):
                worker_names.add(node.targets[0].elts[1].id)
        if isinstance(node, ast.Attribute):
            if isinstance(node.value, ast.Name) and node.value.id in ("os", "sys"):
                allowed_attrs = {"os": {"environ", "getcwd", "readlink"}, "sys": {"argv"}}
                if node.attr not in allowed_attrs[node.value.id]:
                    return None
            elif node.attr not in {"split", "read", "encode", "decode"}:
                return None
        if isinstance(node, ast.Call):
            if node.keywords:
                return None
            if isinstance(node.func, ast.Name):
                if node.func.id == "print":
                    prints.append(node)
                elif node.func.id == "dict":
                    if len(node.args) != 1:
                        return None
                elif node.func.id == "open":
                    if (
                        len(node.args) != 2
                        or not isinstance(node.args[1], ast.Constant)
                        or node.args[1].value != "rb"
                        or not _proc_environment(node.args[0])
                    ):
                        return None
                else:
                    return None
            elif not isinstance(node.func, ast.Attribute) or node.func.attr not in {
                "split",
                "read",
                "encode",
                "decode",
                "getcwd",
                "readlink",
            }:
                return None
        if isinstance(node, ast.For):
            if (
                not isinstance(node.target, ast.Name)
                or not isinstance(node.iter, (ast.Tuple, ast.List))
                or not 0 < len(node.iter.elts) <= 16
                or any(
                    not isinstance(item, ast.Constant)
                    or not isinstance(item.value, str)
                    or re.fullmatch(r"[A-Z][A-Z0-9_]{0,63}", item.value) is None
                    for item in node.iter.elts
                )
                or len(node.body) != 1
                or not isinstance(node.body[0], ast.Assert)
                or not isinstance(node.body[0].msg, ast.Name)
                or node.body[0].msg.id != node.target.id
                or node.orelse
            ):
                return None
            witnesses[node.body[0].lineno] = (
                node.target.id,
                {item.value for item in node.iter.elts},
            )
        if isinstance(node, ast.Expr) and not (
            isinstance(node.value, ast.Call)
            and isinstance(node.value.func, ast.Name)
            and node.value.func.id == "print"
        ):
            return None
    if not witnesses or any(stores.get(name) != 1 for name, _ in witnesses.values()):
        return None
    if len(prints) > 1 or any(
        not _safe_guard_print(node, worker_names, stores)
        or not any(isinstance(stmt, ast.Expr) and stmt.value is node for stmt in tree.body)
        for node in prints
    ):
        return None
    return {line: values for line, (_, values) in witnesses.items()}


def _simple_target(node):
    return isinstance(node, ast.Name) or (
        isinstance(node, (ast.Tuple, ast.List)) and all(_simple_target(item) for item in node.elts)
    )


def _proc_environment(node):
    if not isinstance(node, ast.JoinedStr) or len(node.values) != 3:
        return False
    first, value, last = node.values
    return (
        isinstance(first, ast.Constant)
        and first.value == "/proc/"
        and isinstance(value, ast.FormattedValue)
        and isinstance(value.value, ast.Name)
        and value.format_spec is None
        and isinstance(last, ast.Constant)
        and last.value == "/environ"
    )


def _safe_guard_print(node, worker_names, stores):
    if len(node.args) != 1:
        return False
    value = node.args[0]
    parts = value.values if isinstance(value, ast.JoinedStr) else [value]
    return all(
        (
            isinstance(part, ast.Constant)
            and isinstance(part.value, str)
            and part.value.isprintable()
        )
        or (
            isinstance(part, ast.FormattedValue)
            and isinstance(part.value, ast.Name)
            and part.value.id in worker_names
            and stores.get(part.value.id) == 1
            and part.format_spec is None
        )
        for part in parts
    )


def _literal_tree(node):
    if isinstance(node, ast.Constant):
        return isinstance(node.value, (str, int, bool, type(None)))
    return isinstance(node, (ast.Tuple, ast.List)) and all(_literal_tree(x) for x in node.elts)


def _target_names(node):
    if isinstance(node, ast.Name):
        return {node.id}
    if isinstance(node, (ast.Tuple, ast.List)):
        return set().union(*(_target_names(item) for item in node.elts))
    return set()


def _verification_program(source):
    """Recognize a bounded local test driver without executing or trusting it."""
    if len(source) > 4096:
        return False
    try:
        tree = ast.parse(source)
    except (SyntaxError, ValueError, RecursionError):
        return False
    allowed = {
        ast.Module,
        ast.Import,
        ast.alias,
        ast.Assign,
        ast.Expr,
        ast.For,
        ast.Assert,
        ast.Name,
        ast.Load,
        ast.Store,
        ast.Constant,
        ast.Tuple,
        ast.List,
        ast.Starred,
        ast.Call,
        ast.keyword,
        ast.Attribute,
        ast.Compare,
        ast.Eq,
        ast.NotEq,
    }
    nodes = list(ast.walk(tree))
    if len(nodes) > 512 or any(type(node) not in allowed for node in nodes):
        return False
    reserved = {"json", "subprocess", "print", "dict"}
    loop_names, assignments, stores, calls = set(), {}, {}, []
    for node in nodes:
        if isinstance(node, ast.Import) and any(
            item.name not in ("json", "subprocess") or item.asname is not None
            for item in node.names
        ):
            return False
        if isinstance(node, ast.For):
            if not _simple_target(node.target) or not _literal_tree(node.iter) or node.orelse:
                return False
            loop_names.update(_target_names(node.target))
        if isinstance(node, ast.Name) and isinstance(node.ctx, ast.Store):
            if node.id in reserved:
                return False
            stores[node.id] = stores.get(node.id, 0) + 1
        if isinstance(node, ast.Assign):
            if len(node.targets) != 1 or not isinstance(node.targets[0], ast.Name):
                return False
            assignments[node.targets[0].id] = node.value
        if isinstance(node, ast.Attribute):
            if not isinstance(node.value, ast.Name) or (
                (node.value.id, node.attr) not in {("json", "dumps"), ("subprocess", "run")}
                and node.attr not in {"returncode", "stdout", "stderr"}
            ):
                return False
        if isinstance(node, ast.Call):
            if any(keyword.arg is None for keyword in node.keywords):
                return False
            if isinstance(node.func, ast.Name):
                if node.func.id not in ("print", "dict"):
                    return False
            elif isinstance(node.func, ast.Attribute) and isinstance(node.func.value, ast.Name):
                pair = (node.func.value.id, node.func.attr)
                if pair not in {("json", "dumps"), ("subprocess", "run")}:
                    return False
                if pair == ("subprocess", "run"):
                    calls.append(node)
            else:
                return False
    if not calls or any(count != 1 for count in stores.values()):
        return False
    for call in calls:
        if len(call.args) != 1 or not isinstance(call.args[0], ast.Name):
            return False
        argv = assignments.get(call.args[0].id)
        if not isinstance(argv, ast.List) or len(argv.elts) < 2:
            return False
        first, program, *arguments = argv.elts
        if (
            not isinstance(first, ast.Constant)
            or first.value != "python3"
            or not isinstance(program, ast.Constant)
            or not isinstance(program.value, str)
            or re.fullmatch(r"[A-Za-z0-9_][A-Za-z0-9_-]*\.py", program.value) is None
            or program.value == "handoff.py"
            or any(
                not (isinstance(arg, ast.Constant) and isinstance(arg.value, str))
                and not (
                    isinstance(arg, ast.Starred)
                    and isinstance(arg.value, ast.Name)
                    and arg.value.id in loop_names
                )
                for arg in arguments
            )
        ):
            return False
        options = {item.arg: item.value for item in call.keywords}
        if len(options) != len(call.keywords) or not set(options) <= {
            "capture_output",
            "text",
            "timeout",
            "check",
        }:
            return False
        if any(not isinstance(value, ast.Constant) for value in options.values()):
            return False
        if (
            "timeout" not in options
            or type(options["timeout"].value) not in (int, float)
            or not 0 < options["timeout"].value <= 300
            or any(type(options[key].value) is not bool for key in options if key != "timeout")
        ):
            return False
    return True


def _bounded_verification_tail(tail):
    """Recognize only complete verification envelopes; ignore their output."""
    if re.fullmatch(r'test[ \t]+-n[ \t]+"\$receipt"', tail):
        return True
    checked = r'test[ \t]+"\$\(git rev-parse HEAD\)"[ \t]+=[ \t]+"\$commit";[ \t]*test[ \t]+-z[ \t]+"\$\(git status --porcelain\)"'
    match = re.fullmatch(
        r'commit=\$\(jq[ \t]+-er[ \t]+\'(?P<filter>[^\']+)\'[ \t]+<<<[ \t]+"\$receipt"\);[ \t]*'
        + checked
        + r';[ \t]*git[ \t]+diff[ \t]+--name-only[ \t]+(?:[0-9a-f]{40}|[0-9a-f]{64})[ \t]+"\$commit";[ \t]*'
        + r"python3[ \t]+-[ \t]+<<\'(?P<end>[A-Z_]+)\'\n(?P<program>[\s\S]*?)\n(?P=end)\n"
        + r"timeout[ \t]+[1-9][0-9]{0,2}[ \t]+python3[ \t]+-m[ \t]+unittest[ \t]+discover[ \t]+-s[ \t]+[A-Za-z0-9_-]+[ \t]+-v;[ \t]*"
        + checked,
        tail,
    )
    if not match:
        return False
    projection = match.group("filter")
    words = set(re.findall(r"[A-Za-z_]+", projection))
    return (
        match.group("end") not in match.group("program").split("\n")
        and re.fullmatch(r'[A-Za-z0-9_ .\[\]|=()<>"]+', projection) is not None
        and words
        <= {
            "select",
            "phase",
            "completed",
            "assignments",
            "receipt",
            "result",
            "commit",
            "unique",
            "length",
        }
        and _verification_program(match.group("program"))
    )


def _preflight_failure(message, witnesses):
    content = message.get("content")
    if (
        message.get("isError") is not True
        or not isinstance(content, list)
        or len(content) != 1
        or not isinstance(content[0], dict)
        or content[0].get("type") != "text"
        or not isinstance(content[0].get("text"), str)
        or len(content[0]["text"]) > 8192
    ):
        return False
    match = re.fullmatch(
        r'Traceback \(most recent call last\):\n[ \t]*File "<stdin>", '
        r"line (?P<line>[1-9][0-9]{0,3}), in <module>\nAssertionError: (?P<key>[A-Z][A-Z0-9_]{0,63})"
        r"\n\n\nCommand exited with code 1",
        content[0]["text"],
    )
    return bool(match) and match.group("key") in witnesses.get(int(match.group("line")), set())


def _read_only_word_count(command):
    """A plain wc invocation is not a helper invocation, even for its filename."""
    return (
        re.fullmatch(r"wc(?:[ \t]+-[clmw]+)?(?:[ \t]+[A-Za-z0-9_./-]+)+[ \t]*", command) is not None
    )


def _possible_pack_command(command):
    return bool(
        re.search(r"\btwo[ \t]+init\b", command)
        or re.search(
            r"(?<![A-Za-z0-9_.-])(?:/[A-Za-z0-9_./-]+/)?handoff\.py(?=$|[\s\"';|&])", command
        )
        or re.search(r"\$\{?helper\}?\b", command)
    )


def _retry_command(arguments):
    """Recognize fixed safe fragments, treating realpath expressions as opaque identities.

    No shell is executed or expanded. In particular, a receipt projection is useful
    only as the *first* output of its guarded, fail-fast producer, not arbitrary JSON
    printed by the commands that follow it.
    """
    if not isinstance(arguments, dict) or set(arguments) not in (
        {"command"},
        {"command", "timeout"},
    ):
        return None
    if "timeout" in arguments and (
        type(arguments["timeout"]) not in (int, float) or not 0 < arguments["timeout"] <= 300
    ):
        return None
    text = arguments.get("command")
    if not isinstance(text, str) or len(text) > 8192 or "\r" in text:
        return None

    # This grammar recognizes only the bounded shell envelope. The here-doc
    # program is independently checked as a non-mutating Python guard.
    failed = re.fullmatch(
        r"set[ \t]+-e;[ \t]*for[ \t]+(?P<worker>[a-z_][a-z0-9_]*)[ \t]+in[ \t]+"
        + r"(?P<coder>"
        + _ADDRESS
        + r")[ \t]+(?P<cleaner>"
        + _ADDRESS
        + r");[ \t]*"
        + r'do[ \t]+(?P<pid>[a-z_][a-z0-9_]*)=\$\(hc[ \t]+get[ \t]+"\$(?P=worker)"'
        + r"[ \t]+--json[ \t]*\|[ \t]*jq[ \t]+-r[ \t]+\.foreground\.pid\);[ \t]*"
        + r'python3[ \t]+-[ \t]+"\$(?P=pid)"[ \t]+"\$(?P=worker)"[ \t]+<<\'(?P<end>[A-Z_]+)\'\n'
        + r'(?P<program>[\s\S]*?)\n(?P=end)\ndone;[ \t]*test[ \t]+-z[ \t]+"\$\(git status --porcelain\)";[ \t]*python3[ \t]+'
        + r"(?P<helper>"
        + _HELPER
        + r')[ \t]+"\$PWD/(?P<store>'
        + _RELATIVE
        + r')"[ \t]+'
        + r'two[ \t]+init[ \t]+--project[ \t]+"\$PWD"[ \t]+--card[ \t]+"\$PWD/(?P<card>'
        + _RELATIVE
        + r')"[ \t]+--checks[ \t]+"\$PWD/(?P<checks>'
        + _RELATIVE
        + r')"[ \t]+--coder[ \t]+(?P=coder)[ \t]+--cleaner[ \t]+(?P=cleaner)',
        text,
    )
    if failed:
        values = failed.groupdict()
        if values["end"] in values["program"].split("\n"):
            return None
        witness = _preflight_program(values["program"])
        if (
            not _path(values["helper"])
            or any(not _relative(values[k]) for k in ("store", "card", "checks"))
            or values["coder"] == values["cleaner"]
            or values["worker"] == values["pid"]
            or not witness
        ):
            return None
        return (
            "preflight",
            None,
            None,
            values["helper"],
            ("realpath", values["store"]),
            ("$PWD", values["coder"], values["cleaner"], witness),
        )

    init = re.fullmatch(
        rf'set[ \t]+-e;[ \t]*test[ \t]+-z[ \t]+"\$\(git status --porcelain\)";[ \t]*'
        rf"helper=(?P<helper>{_HELPER});[ \t]*"
        + r"[ \t]+".join(
            (
                "python3",
                r'"\$helper"',
                rf'"\$\(realpath (?P<store>{_RELATIVE})\)"',
                "two",
                "init",
                "--project",
                r'"\$PWD"',
                "--card",
                rf'"\$\(realpath (?P<card>{_RELATIVE})\)"',
                "--checks",
                rf'"\$\(realpath (?P<checks>{_RELATIVE})\)"',
                "--coder",
                rf"(?P<coder>{_ADDRESS})",
                "--cleaner",
                rf"(?P<cleaner>{_ADDRESS})",
            )
        ),
        text,
    )
    if init:
        values = init.groupdict()
        if (
            not _path(values["helper"])
            or any(not _relative(values[k]) for k in ("store", "card", "checks"))
            or values["coder"] == values["cleaner"]
        ):
            return None
        return (
            "init",
            None,
            None,
            values["helper"],
            ("realpath", values["store"]),
            ("$PWD", values["coder"], values["cleaner"]),
        )

    start = re.fullmatch(
        r"set -e -o pipefail; helper=(?P<helper>"
        + _HELPER
        + r'); python3 "\$helper" "\$\(realpath (?P<store>'
        + _RELATIVE
        + r')\)" two (?P<pack>'
        + _PACK
        + r") start \| jq -c \.",
        text,
    )
    if start:
        values = start.groupdict()
        if _path(values["helper"]) and _relative(values["store"]):
            return (
                "start",
                values["pack"],
                None,
                values["helper"],
                ("realpath", values["store"]),
                None,
            )
        return None

    # Older direct-path histories can include a start preceded by a read-only
    # resume, and an arbitrary number of read-only inventory polls.
    legacy_start = re.fullmatch(
        r"set -e; helper=(?P<helper>"
        + _HELPER
        + r"); store=(?P<store>/[A-Za-z0-9_./-]+); "
        + r"run=(?P<pack>"
        + _PACK
        + r'); python3 "\$helper" "\$store" two "\$run" resume '
        + r"\| jq '[^']*'; "
        + r'python3 "\$helper" "\$store" two "\$run" start',
        text,
    )
    if legacy_start:
        values = legacy_start.groupdict()
        if _path(values["helper"]) and _path(values["store"]):
            return "legacy_start", values["pack"], None, values["helper"], values["store"], None
        return None
    legacy_poll = re.fullmatch(
        r"(?:hc read "
        + _ADDRESS
        + r" --lines 0 \| awk 'NF' \| tail -[1-9][0-9]*; )?"
        + r"(?:git status --short; )?hc wait "
        + _ADDRESS
        + r" --timeout [1-9][0-9]{0,2} --json; "
        + r"python3 (?P<helper>"
        + _HELPER
        + r") (?P<store>/[A-Za-z0-9_./-]+) two (?P<pack>"
        + _PACK
        + r") resume \| jq '[^']*'(?:; hc ls --json \| jq -c '[^']*')?",
        text,
    )
    if legacy_poll:
        values = legacy_poll.groupdict()
        if _path(values["helper"]) and _path(values["store"]):
            return "legacy_poll", values["pack"], None, values["helper"], values["store"], None
        return None

    # Read-only polls may appear any number of times. Only declarative
    # projections with the required identities provide a worker binding.
    poll = re.fullmatch(
        r"(?:(?:hc wait (?P<waited>"
        + _ADDRESS
        + r") --timeout [1-9][0-9]{0,2} --json; )"
        + r"|(?:set -e; git diff --stat [0-9a-f]{40,64}\.\.[0-9a-f]{40,64}; ))?"
        + r"helper=(?P<helper>"
        + _HELPER
        + r'); python3 "\$helper" "\$\(realpath (?P<store>'
        + _RELATIVE
        + r')\)" two (?P<pack>'
        + _PACK
        + r") resume \| jq -c '(?P<filter>[^']*)'"
        + r"(?P<tail>; hc wait --help \| head -[1-9][0-9]*|; hc ls --json \| jq -c '[^']*')?",
        text,
    )
    if poll:
        values = poll.groupdict()
        if not _path(values["helper"]) or not _relative(values["store"]):
            return None
        fields = _projection_fields(values["filter"])
        tail = values["tail"]
        inventory = re.fullmatch(r"; hc ls --json \| jq -c '([^']*)'", tail or "")
        safe_inventory = bool(inventory) and "".join(inventory.group(1).split()) in (
            _TERMINALS_FILTER,
            "[" + _TERMINALS_FILTER + "]",
        )
        kind = (
            "retry_binding"
            if values["waited"] and fields and (not tail or safe_inventory)
            else "poll"
        )
        return (
            kind,
            values["pack"],
            values["waited"],
            values["helper"],
            ("realpath", values["store"]),
            (fields, safe_inventory),
        )

    # A safe prefix establishes what produced the first output. The subsequent
    # shell is never parsed and cannot provide the projected receipt evidence.
    completed = re.match(
        r"set -euo pipefail; helper=(?P<helper>"
        + _HELPER
        + r"); store=\$\(realpath (?P<store>"
        + _RELATIVE
        + r")\); run=(?P<pack>"
        + _PACK
        + r'); receipt=\$\(python3 "\$helper" "\$store" two "\$run" resume\); '
        + r"""printf '%s\\n' "\$receipt" \| jq -ce '(?P<filter>[^']*)'; """,
        text,
    )
    if completed:
        values = completed.groupdict()
        tail = text[completed.end() :]
        if (
            _path(values["helper"])
            and _relative(values["store"])
            and _projection_fields(values["filter"], receipts=True) is not None
            and _bounded_verification_tail(tail)
        ):
            return (
                "retry_completed",
                values["pack"],
                None,
                values["helper"],
                ("realpath", values["store"]),
                None,
            )
    return None


def _command(arguments):
    """Recognize only the observed init and resume command forms; do not execute them."""
    if not isinstance(arguments, dict) or set(arguments) not in (
        {"command"},
        {"command", "timeout"},
    ):
        return None
    if "timeout" in arguments:
        timeout = arguments["timeout"]
        if type(timeout) not in (int, float) or not 0 < timeout <= 300:
            return None
    command = arguments["command"]
    if not isinstance(command, str) or len(command) > 4096 or any(c in command for c in "`\\\n\r"):
        return None
    try:
        lexer = shlex.shlex(command, posix=True, punctuation_chars=";|")
        lexer.whitespace_split = True
        lexer.commenters = ""
        tokens = list(lexer)
    except ValueError:
        return None

    # The saved init used two literal local variables, not an arbitrary shell expansion.
    if len(tokens) == 22 and tokens[:3] == ["set", "-e", ";"]:
        root, helper = tokens[3], tokens[5]
        if (
            not root.startswith("root=")
            or not helper.startswith("helper=")
            or tokens[4] != ";"
            or tokens[6:9] != [";", "python3", "$helper"]
            or not _path(root[5:])
            or not _path(helper[7:])
        ):
            return None
        expanded = []
        for token in tokens[9:]:
            if token.startswith("$root/"):
                expanded.append(root[5:] + token[5:])
            elif "$" in token:
                return None
            else:
                expanded.append(token)
        tokens = ["python3", helper[7:], *expanded]
    elif any("$" in token for token in tokens):
        return None

    if len(tokens) == 15 and tokens[0] == "python3" and tokens[3:5] == ["two", "init"]:
        if (
            not _path(tokens[1])
            or not tokens[1].endswith("/scripts/handoff.py")
            or any(not _path(tokens[i]) for i in (2, 6, 8, 10))
            or [tokens[i] for i in (5, 7, 9, 11, 13)]
            != ["--project", "--card", "--checks", "--coder", "--cleaner"]
            or any(re.fullmatch(_ADDRESS, tokens[i]) is None for i in (12, 14))
            or tokens[12] == tokens[14]
        ):
            return None
        return "init", None, None, tokens[1], tokens[2], (tokens[6], tokens[12], tokens[14])
    if (
        len(tokens) == 6
        and tokens[0] == "python3"
        and _path(tokens[1])
        and tokens[1].endswith("/scripts/handoff.py")
        and _path(tokens[2])
        and tokens[3] == "two"
        and re.fullmatch(_PACK, tokens[4])
        and tokens[5] == "resume"
    ):
        return "direct", tokens[4], None, tokens[1], tokens[2], None

    if tokens[:4] == ["set", "-o", "pipefail", ";"]:
        tokens = tokens[4:]
    if len(tokens) != 24:
        return None
    if (
        tokens[:2] != ["hc", "wait"]
        or re.fullmatch(_ADDRESS, tokens[2]) is None
        or tokens[3] != "--timeout"
        or re.fullmatch(r"[1-9][0-9]{0,2}", tokens[4]) is None
        or tokens[5:8] != ["--json", ";", "python3"]
        or any(not _path(p) for p in tokens[8:10])
        or not tokens[8].endswith("/scripts/handoff.py")
        or tokens[10] != "two"
        or re.fullmatch(_PACK, tokens[11]) is None
        or tokens[12:15] != ["resume", "|", "jq"]
        or tokens[16:23] != [";", "hc", "ls", "--json", "|", "jq", "-c"]
        or "".join(tokens[23].split()) != _TERMINALS_FILTER
    ):
        return None
    projection = "".join(tokens[15].split())
    if projection == _BINDING_FILTER:
        kind = "binding"
    elif projection == _COMPLETED_FILTER:
        kind = "completed"
    else:
        return None
    return kind, tokens[11], tokens[2], tokens[8], tokens[9], None


def _values(message):
    content = message.get("content")
    if (
        message.get("isError") is not False
        or not isinstance(content, list)
        or len(content) != 1
        or not isinstance(content[0], dict)
        or content[0].get("type") != "text"
        or not isinstance(content[0].get("text"), str)
        or len(content[0]["text"]) > 262144
    ):
        return None
    text = content[0]["text"]
    decoder = json.JSONDecoder(object_pairs_hook=_unique, parse_constant=_constant)
    values = []
    try:
        while text.strip():
            text = text.lstrip()
            value, end = decoder.raw_decode(text)
            values.append(value)
            if len(values) > 256:
                return None
            text = text[end:]
    except (ValueError, RecursionError):
        return None
    return values or None


def _objects(message):
    values = _values(message)
    return values if values and all(isinstance(v, dict) for v in values) else None


def _origin(message, command):
    objects = _objects(message)
    if objects is None or len(objects) != 1:
        return None
    origin = objects[0]
    _, _, _, _, store, _ = command
    if isinstance(store, tuple):
        # The literal realpath expression is compared across calls, not evaluated.
        store = origin.get("directory")
        if not isinstance(store, str) or not _path(store):
            return None
    if (
        set(origin)
        != {"id", "run", "directory", "database", "phase", "branch", "baseline", "card_version"}
        or not isinstance(origin["id"], str)
        or re.fullmatch(_PACK, origin["id"]) is None
        or origin["run"] != origin["id"]
        or origin["directory"] != store
        or origin["database"] != store.rstrip("/") + "/two-pack.sqlite3"
        or origin["phase"] != "ready"
        or not isinstance(origin["branch"], str)
        or not origin["branch"]
        or not isinstance(origin["baseline"], str)
        or re.fullmatch(r"[0-9a-f]{40}|[0-9a-f]{64}", origin["baseline"]) is None
        or type(origin["card_version"]) is not int
        or origin["card_version"] != 1
    ):
        return None
    return origin


def _snapshot(message, command):
    objects = _objects(message)
    if objects is None:
        return None
    kind, pack, waited, _, _, _ = command
    if kind == "direct":
        if len(objects) != 1:
            return None
        snapshot = objects[0]
        if (
            set(snapshot)
            != {
                "id",
                "phase",
                "leader",
                "workers",
                "current",
                "summary",
                "version",
                "pack",
                "pack_version",
                "project",
                "project_id",
                "rounds",
                "branch",
                "baseline",
                "assignments",
                "card_version",
                "card_versions",
                "card",
                "checks",
                "created_at",
                "updated_at",
            }
            or type(snapshot["version"]) is not int
            or snapshot["version"] != 1
            or snapshot["pack"] != "two"
            or type(snapshot["pack_version"]) is not int
            or snapshot["pack_version"] != 1
            or type(snapshot["rounds"]) is not int
            or snapshot["rounds"] < 1
            or not isinstance(snapshot["project"], str)
            or not isinstance(snapshot["assignments"], dict)
            or not isinstance(snapshot["card_versions"], list)
            or not isinstance(snapshot["card"], dict)
            or not isinstance(snapshot["checks"], dict)
            or type(snapshot["card_version"]) is not int
        ):
            return None
        assignments = snapshot["assignments"]
        for key, item in assignments.items():
            if (
                not isinstance(item, dict)
                or item.get("role") not in ("coder", "cleaner")
                or type(item.get("round")) is not int
                or item["round"] <= 0
                or key != f"{item['role']}:{item['round']}"
                or item.get("id") != f"{pack}/{item['role']}/{item['round']}"
            ):
                return None
        snapshot = {**snapshot, "assignments": list(assignments.values())}
        kind = "binding" if snapshot["phase"] == "active" else "completed"
        if kind == "binding" and (
            not isinstance(snapshot["current"], str) or snapshot["current"] not in assignments
        ):
            return None
        objects = None
    else:
        if len(objects) < 5:
            return None
        wait, snapshot, *terminals = objects
    if objects is not None and (
        not {"children", "ended", "exit_code", "reason", "signal", "status", "terminal", "turn"}
        <= wait.keys()
        or set(wait)
        - {
            "children",
            "ended",
            "exit_code",
            "reason",
            "signal",
            "status",
            "terminal",
            "turn",
            "from",
        }
        or wait["terminal"] != waited
        or type(wait["turn"]) is not int
        or not isinstance(wait["children"], list)
        or type(wait["ended"]) is not bool
    ):
        return None
    if (
        snapshot.get("id") != pack
        or not isinstance(snapshot.get("workers"), dict)
        or set(snapshot["workers"]) != {"coder", "cleaner"}
        or any(
            not isinstance(x, str) or re.fullmatch(_ADDRESS, x) is None
            for x in (snapshot.get("leader"), *snapshot["workers"].values())
        )
    ):
        return None
    addresses = {snapshot["leader"], *snapshot["workers"].values()}
    if objects is None and any(
        item.get("target") != snapshot["workers"][item["role"]] for item in snapshot["assignments"]
    ):
        return None
    if len(addresses) != 3:
        return None
    if objects is not None and (
        any(
            set(t) != {"address", "status", "turn"}
            or not isinstance(t["address"], str)
            or not isinstance(t["status"], str)
            or type(t["turn"]) is not int
            for t in terminals
        )
        or not addresses.issubset({t["address"] for t in terminals})
        or len({t["address"] for t in terminals}) != len(terminals)
        or waited != snapshot["workers"]["coder" if kind == "binding" else "cleaner"]
    ):
        return None
    if objects is None:
        if kind == "completed" and (
            snapshot["phase"] != "completed" or snapshot["current"] is not None
        ):
            return None
    elif kind == "binding":
        if (
            set(snapshot)
            != {"id", "phase", "leader", "workers", "current", "summary", "round_count"}
            or snapshot["phase"] != "active"
            or type(snapshot["round_count"]) is not int
            or snapshot["round_count"] < 1
        ):
            return None
    elif (
        set(snapshot)
        != {
            "id",
            "phase",
            "leader",
            "workers",
            "branch",
            "baseline",
            "current",
            "summary",
            "assignments",
        }
        or snapshot["phase"] != "completed"
        or snapshot["current"] is not None
    ):
        return None
    return kind, snapshot


def _retry_snapshot(message, command):
    kind, pack, waited, _, _, _ = command
    if kind == "retry_completed":
        content = message.get("content")
        if (
            message.get("isError") is not False
            or not isinstance(content, list)
            or len(content) != 1
            or not isinstance(content[0], dict)
            or content[0].get("type") != "text"
            or not isinstance(content[0].get("text"), str)
            or len(content[0]["text"]) > 262144
        ):
            return None
        lines = content[0]["text"].splitlines()
        if not lines:
            return None
        try:
            snapshot = json.loads(lines[0], object_pairs_hook=_unique, parse_constant=_constant)
            # A second projection could be stderr or a later print rather than
            # the jq result; it cannot independently identify the first one.
            for line in lines[1:]:
                try:
                    other = json.loads(line, object_pairs_hook=_unique, parse_constant=_constant)
                except (ValueError, RecursionError):
                    continue
                if isinstance(other, dict) and set(other) == {
                    "id",
                    "phase",
                    "leader",
                    "workers",
                    "branch",
                    "receipts",
                }:
                    return None
        except (ValueError, RecursionError):
            return None
        if (
            not isinstance(snapshot, dict)
            or set(snapshot) != {"id", "phase", "leader", "workers", "branch", "receipts"}
            or snapshot["id"] != pack
            or snapshot["phase"] != "completed"
        ):
            return None
        return "retry_completed", snapshot

    objects = _values(message)
    if kind in ("start", "legacy_start"):
        if (
            objects is None
            or len(objects) != (1 if kind == "start" else 2)
            or any(not isinstance(obj, dict) for obj in objects)
        ):
            return None
        data = objects[-1]
        match = (
            re.fullmatch(rf"{pack}/coder/([1-9][0-9]*)", data.get("assignment", ""))
            if isinstance(data.get("assignment"), str)
            else None
        )
        delivery = data.get("delivery")
        if (
            match is None
            or not isinstance(data.get("target"), str)
            or re.fullmatch(_ADDRESS, data["target"]) is None
            or not isinstance(delivery, dict)
            or type(delivery.get("number")) is not int
            or delivery["number"] <= 0
            or delivery.get("status") != "delivered"
            or data.get("receipt_saved") is not False
        ):
            return None
        return kind, {"pack": pack, "round": match.group(1), "target": data["target"]}
    fields, inventory = command[5]
    if (
        objects is None
        or len(objects) < 2
        or (not inventory and len(objects) != 2)
        or not all(isinstance(obj, dict) for obj in objects[:2])
        or any(not isinstance(obj, (dict, list)) for obj in objects[2:])
    ):
        return None
    wait, snapshot = objects[:2]
    if (
        not {"children", "ended", "exit_code", "reason", "signal", "status", "terminal", "turn"}
        <= wait.keys()
        or set(wait)
        - {
            "children",
            "ended",
            "exit_code",
            "reason",
            "signal",
            "status",
            "terminal",
            "turn",
            "from",
        }
        or wait["terminal"] != waited
        or type(wait["turn"]) is not int
        or not isinstance(wait["children"], list)
        or type(wait["ended"]) is not bool
        or set(snapshot) != fields
        or snapshot["phase"] not in ("active", "completed")
        or ("id" in snapshot and snapshot["id"] != pack)
        or ("rounds" in snapshot and not isinstance(snapshot["rounds"], (list, int)))
    ):
        return None
    return "retry_binding", snapshot


def _retry_accepted(origin, start, binding, completed, notices, answer):
    coder, cleaner = notices
    workers = binding.get("workers")
    if (
        not isinstance(workers, dict)
        or binding.get("phase") != "active"
        or set(workers) != {"coder", "cleaner"}
        or any(
            not isinstance(x, str) or re.fullmatch(_ADDRESS, x) is None
            for x in (binding.get("leader"), *workers.values())
        )
        or len({binding["leader"], *workers.values()}) != 3
        or start["pack"] != origin["id"]
        or start["target"] != workers["coder"]
        or binding["current"] != "coder:" + start["round"]
        or any(
            n["pack"] != origin["id"]
            or n["round"] != start["round"]
            or n["sender"] != workers[n["role"]]
            for n in notices
        )
        or (coder["role"], coder["status"], coder["decision"]) != ("coder", "done", "active")
        or (cleaner["role"], cleaner["status"], cleaner["decision"])
        != ("cleaner", "pass", "completed")
        or completed.get("id") != origin["id"]
        or completed.get("leader") != binding["leader"]
        or completed.get("workers") != workers
        or completed.get("branch") != origin["branch"]
    ):
        return False
    receipts = completed.get("receipts")
    if not isinstance(receipts, list) or len(receipts) != 2:
        return False
    found = {}
    for receipt in receipts:
        if not isinstance(receipt, dict):
            return False
        role = receipt.get("role")
        if (
            role not in ("coder", "cleaner")
            or role in found
            or type(receipt.get("round")) is not int
            or str(receipt["round"]) != start["round"]
            or receipt.get("target") != workers[role]
            or receipt.get("worker") != workers[role]
            or type(receipt.get("receipt_id")) is not int
            or receipt["receipt_id"] <= 0
            or receipt.get("status") != ("done" if role == "coder" else "pass")
            or not isinstance(receipt.get("commit"), str)
            or re.fullmatch(r"[0-9a-f]{40}|[0-9a-f]{64}", receipt["commit"]) is None
            or not isinstance(receipt.get("checks"), list)
            or any(
                not isinstance(c, dict) or ("commit" in c and c["commit"] != receipt["commit"])
                for c in receipt["checks"]
            )
        ):
            return False
        found[role] = receipt
    if (
        found["coder"]["receipt_id"] == found["cleaner"]["receipt_id"]
        or found["coder"]["commit"] != found["cleaner"]["commit"]
        or not isinstance(answer, dict)
        or answer.get("status") != "completed"
        or answer.get("pack_id") != origin["id"]
        or answer.get("lead_id") != binding["leader"]
        or answer.get("branch") != origin["branch"]
        or answer.get("checked_commit") != found["coder"]["commit"]
        or not isinstance(answer.get("signoffs"), dict)
        or set(answer["signoffs"]) != {"coder", "cleaner"}
    ):
        return False
    return all(
        isinstance(answer["signoffs"][role], dict)
        and answer["signoffs"][role].get("terminal_id") == workers[role]
        and type(answer["signoffs"][role].get("receipt_id")) is int
        and answer["signoffs"][role]["receipt_id"] == found[role]["receipt_id"]
        and answer["signoffs"][role].get("status") == found[role]["status"]
        for role in ("coder", "cleaner")
    )


def _accepted(binding, completed, notices):
    if any(completed[k] != binding[k] for k in ("id", "leader", "workers")):
        return False
    coder, cleaner = notices
    if (
        coder["pack"] != binding["id"]
        or cleaner["pack"] != binding["id"]
        or coder["round"] != cleaner["round"]
        or binding["current"] != "coder:" + coder["round"]
        or (coder["role"], coder["status"], coder["decision"]) != ("coder", "done", "active")
        or (cleaner["role"], cleaner["status"], cleaner["decision"])
        != ("cleaner", "pass", "completed")
        or any(n["sender"] != binding["workers"][n["role"]] for n in notices)
    ):
        return False
    assignments = completed.get("assignments")
    if not isinstance(assignments, list) or len(assignments) != 2:
        return False
    receipts, receipt_ids = {}, set()
    for assignment in assignments:
        if not isinstance(assignment, dict):
            return False
        role = assignment.get("role")
        if role not in ("coder", "cleaner") or role in receipts:
            return False
        receipt = assignment.get("receipt")
        rejected = assignment.get("rejections")
        if (
            assignment.get("id") != f"{binding['id']}/{role}/{coder['round']}"
            or assignment.get("target") != binding["workers"][role]
            or not isinstance(receipt, dict)
            or type(receipt.get("id")) is not int
            or receipt["id"] <= 0
            or receipt.get("status") != ("done" if role == "coder" else "pass")
            or receipt.get("worker") != binding["workers"][role]
            or not isinstance(receipt.get("result"), dict)
            or not isinstance(receipt.get("notification"), dict)
            or receipt["notification"].get("status") != "delivered"
            or not isinstance(rejected, list)
            or any(
                not isinstance(r, dict) or type(r.get("id")) is not int or r["id"] <= 0
                for r in rejected
            )
        ):
            return False
        ids = [receipt["id"], *(r["id"] for r in rejected)]
        if len(set(ids)) != len(ids) or receipt_ids.intersection(ids):
            return False
        receipt_ids.update(ids)
        receipts[role] = receipt
    # Plain resume exposes the assignment handoff as well as the two receipts.
    # A conflicting source or input commit cannot corroborate that handoff.
    if "project" in completed:
        for assignment in assignments:
            role = assignment["role"]
            source = assignment.get("source_receipt")
            if (
                "source_receipt" not in assignment
                or assignment.get("input_commit") != receipts[role]["result"].get("input_commit")
                or (role == "coder" and source is not None)
                or (
                    role == "cleaner"
                    and (type(source) is not int or source != receipts["coder"]["id"])
                )
            ):
                return False
    coder_result, cleaner_result = (receipts[role]["result"] for role in ("coder", "cleaner"))
    commits = (
        completed.get("baseline"),
        coder_result.get("input_commit"),
        coder_result.get("commit"),
        cleaner_result.get("input_commit"),
        cleaner_result.get("commit"),
    )
    return (
        all(isinstance(c, str) and re.fullmatch(r"[0-9a-f]{40}|[0-9a-f]{64}", c) for c in commits)
        and commits[0] == commits[1]
        and commits[2] == commits[3]
    )


def continuation(turns, index, task, submitted_texts, cwd):
    """Return the actual final (id, text), or None if any boundary is uncertain."""
    task_id, development = task.get("id"), task.get("development_task")
    if not isinstance(development, str) or not development or not isinstance(task_id, str):
        return None
    prefix = (
        f"BRIDGE_TASK_ID={task_id}\nDevelopment task {development}; "
        "this is one round of the SAME task. Do not approve permission/trust dialogs, merge or deploy. "
        "Use your normal tools, extensions and skills for this task. "
    )
    if (
        not task["submitted_text"].startswith(prefix)
        or (task.get("recovery") or {}).get("state") == "restored"
        or index + 2 >= len(turns)
    ):
        return None
    original, coder, cleaner = turns[index : index + 3]
    if original["final"] or original["aborted"] or coder["final"] or coder["aborted"]:
        return None
    if any(t["text"] in submitted_texts for t in (coder, cleaner)):
        return None
    notices = [_NOTICE.fullmatch(t["text"]) for t in (coder, cleaner)]
    if not all(notices) or not cleaner["final"] or cleaner["aborted"]:
        return None
    if index + 3 < len(turns) and _NOTICE.fullmatch(turns[index + 3]["text"]):
        return None
    notices = [m.groupdict() for m in notices]
    try:
        answer = json.loads(
            cleaner["final"][1], object_pairs_hook=_unique, parse_constant=_constant
        )
    except (TypeError, ValueError, RecursionError):
        return None
    if (
        not isinstance(answer, dict)
        or answer.get("task_id") != task_id
        or answer.get("kind") != "result"
        or not isinstance(answer.get("answer"), dict)
    ):
        return None

    calls, seen_results, recognized_results, snapshots, final_count = {}, set(), set(), [], 0
    sequence = 0
    for position, turn in enumerate((original, coder, cleaner)):
        for event in turn["events"]:
            sequence += 1
            role = event.get("role")
            if role == "system" or role == "assistant" and event.get("stopReason") == "aborted":
                return None
            if role == "assistant":
                content = event["content"]
                if (
                    event["stopReason"] == "stop"
                    and event.get("endTurn") is not False
                    and any(block.get("type") == "text" for block in content)
                    and not any(block.get("type") == "toolCall" for block in content)
                ):
                    final_count += 1
                    text = "".join(
                        block["text"] for block in content if block.get("type") == "text"
                    )
                    if position != 2 or text != cleaner["final"][1] or final_count != 1:
                        return None
                for block in content:
                    if block.get("type") == "toolCall":
                        call_id = block["id"]
                        if call_id in calls:
                            return None
                        command = _command(block["arguments"]) or _retry_command(block["arguments"])
                        raw = block["arguments"].get("command")
                        if (
                            command is None
                            and isinstance(raw, str)
                            and not _read_only_word_count(raw)
                            and _possible_pack_command(raw)
                        ):
                            return None
                        if command is not None and (
                            event.get("stopReason") != "toolUse" or block["name"] != "bash"
                        ):
                            return None
                        calls[call_id] = (block["name"], command, sequence)
            elif role == "toolResult":
                call_id = event["toolCallId"]
                if call_id in seen_results:
                    return None
                seen_results.add(call_id)
                call = calls.get(call_id)
                if call is None or call[0] != event["toolName"]:
                    return None
                if call[0] == "bash" and call[1] is not None:
                    command = call[1]
                    if command[0] == "init":
                        origin = _origin(event, command)
                        snapshot = ("init", origin) if origin is not None else None
                    elif command[0] == "preflight":
                        if not _preflight_failure(event, command[5][3]):
                            return None
                        snapshot = ("preflight", None)
                    elif command[0] in ("poll", "legacy_poll"):
                        # A failed read-only poll is not completion evidence.
                        snapshot = (command[0], None)
                    elif command[0] in (
                        "start",
                        "legacy_start",
                        "retry_binding",
                        "retry_completed",
                    ):
                        snapshot = _retry_snapshot(event, command)
                    else:
                        snapshot = _snapshot(event, command)
                    if snapshot is None:
                        return None
                    kind, data = snapshot
                    recognized_results.add(call_id)
                    snapshots.append((position, kind, command, data, call[2], sequence))
    if set(calls) != seen_results or recognized_results != {
        call_id for call_id, call in calls.items() if call[1] is not None
    }:
        return None
    init_calls = [call for call in calls.values() if call[1] is not None and call[1][0] == "init"]
    origins = [item for item in snapshots if item[1] == "init"]
    bindings = [item for item in snapshots if item[1] == "binding"]
    completions = [item for item in snapshots if item[1] == "completed"]
    preflights = [item for item in snapshots if item[1] == "preflight"]
    starts = [item for item in snapshots if item[1] == "start"]
    retry_bindings = [item for item in snapshots if item[1] == "retry_binding"]
    retry_completions = [item for item in snapshots if item[1] == "retry_completed"]
    polls = [item for item in snapshots if item[1] == "poll"]
    legacy_starts = [item for item in snapshots if item[1] == "legacy_start"]
    legacy_polls = [item for item in snapshots if item[1] == "legacy_poll"]
    if len(init_calls) != 1 or len(origins) != 1 or len(preflights) > 1:
        return None
    origin_pos, _, origin_command, origin, origin_start, origin_end = origins[0]
    project, coder_worker, cleaner_worker = origin_command[5]
    if origin_pos != 0 or (project != "$PWD" and project != cwd):
        return None
    if preflights:
        position, _, command, _, _, end = preflights[0]
        if (
            position != 0
            or end >= origin_start
            or command[3:5] != origin_command[3:5]
            or command[5][:3] != origin_command[5]
        ):
            return None
    if starts or retry_bindings or retry_completions or polls or preflights:
        if (
            bindings
            or completions
            or legacy_starts
            or legacy_polls
            or len(starts) != 1
            or not retry_bindings
            or len(retry_completions) != 1
            or not _same_relative_store(cwd, origin["directory"], origin_command[4])
        ):
            return None
        start_pos, _, start_command, start, start_begin, start_end = starts[0]
        binding_pos, _, binding_command, binding, binding_begin, binding_end = retry_bindings[0]
        completed_pos, _, completed_command, completed, completed_begin, _ = retry_completions[0]
        if (
            start_pos != 0
            or binding_pos != 0
            or completed_pos != 2
            or not origin_end
            < start_begin
            <= start_end
            < binding_begin
            <= binding_end
            < completed_begin
            or any(
                command[3:5] != origin_command[3:5] or command[1] != origin["id"]
                for command in (
                    start_command,
                    completed_command,
                    *(item[2] for item in retry_bindings),
                    *(item[2] for item in polls),
                )
            )
            or any(item[4] <= origin_end for item in polls)
            or any(
                item[4] <= start_end
                or item[3].get("leader") != binding.get("leader")
                or item[3].get("workers") != binding.get("workers")
                or any(
                    item[3].get(key, origin[key]) != origin[key] for key in ("branch", "baseline")
                )
                or item[3].get("project", cwd) != cwd
                for item in retry_bindings
            )
            or binding_command[2] != coder_worker
            or binding.get("workers") != {"coder": coder_worker, "cleaner": cleaner_worker}
            or not _retry_accepted(origin, start, binding, completed, notices, answer["answer"])
        ):
            return None
        return cleaner["final"]
    if len(bindings) != 1 or len(completions) != 1:
        return None
    binding_pos, _, binding_command, binding, binding_start, binding_end = bindings[0]
    _, _, completed_command, completed, completed_start, _ = completions[0]
    if (
        len(legacy_starts) > 1
        or any(
            item[2][3:5] != origin_command[3:5]
            or item[2][1] != origin["id"]
            or item[4] <= origin_end
            for item in (*legacy_starts, *legacy_polls)
        )
        or any(
            item[0] != 0
            or item[5] >= binding_start
            or item[3]["target"] != coder_worker
            or binding["current"] != "coder:" + item[3]["round"]
            for item in legacy_starts
        )
        or binding_pos != 0
        or not origin_end < binding_start <= binding_end < completed_start
        or origin_command[3:5] != binding_command[3:5]
        or origin_command[3:5] != completed_command[3:5]
        or origin["id"] != binding["id"]
        or origin["id"] != completed["id"]
        or binding["workers"] != {"coder": coder_worker, "cleaner": cleaner_worker}
        or any(
            snapshot.get("branch", origin["branch"]) != origin["branch"]
            or snapshot.get("baseline", origin["baseline"]) != origin["baseline"]
            or snapshot.get("project", project) != project
            for snapshot in (binding, completed)
        )
    ):
        return None
    if not _accepted(binding, completed, notices):
        return None
    return cleaner["final"]
