#!/usr/bin/env python3
"""Local protocol helpers. No agent launches, shell evaluation, or resource deletion."""

import argparse
from datetime import datetime, timezone
import json
import os
from pathlib import Path, PurePosixPath
import re
import shutil
import stat
import sys
import tempfile
import uuid


VERSION = 1
# Local protocol/evidence documents, not arbitrary project artifacts.
MAX_FILE_BYTES = 16 * 1024 * 1024
FILE_FIELDS = ("files_created", "files_modified", "files_generated", "files_deleted")
IDENTITY_FIELDS = ("schema_version", "job_id", "round_id")
# herdr v0.9.1 src/workspace.rs uses this alphabet for all public numbers.
# IDs are opaque: validate their shape, never decode or renumber them.
HERDR_PUBLIC_NUMBER = r"[123456789ABCDEFGHJKMNPQRSTVWXYZ0]+"


class ProtocolError(ValueError):
    pass


def require(condition, message):
    if not condition:
        raise ProtocolError(message)


def utc_now():
    return datetime.now(timezone.utc).isoformat()


def validate_json_values(value):
    try:
        json.dumps(value, ensure_ascii=False, allow_nan=False).encode("utf-8")
    except (ValueError, UnicodeError) as exc:
        raise ProtocolError("JSON values must contain finite numbers and valid Unicode") from exc


def parse_json(text):
    """Decode a captured JSON document with the same strict rules as file reads."""
    def unique_keys(pairs):
        result = {}
        for key, value in pairs:
            require(key not in result, f"duplicate JSON key: {key}")
            result[key] = value
        return result

    def invalid_constant(value):
        raise ProtocolError(f"invalid JSON constant: {value}")

    value = json.loads(text, object_pairs_hook=unique_keys, parse_constant=invalid_constant)
    require(isinstance(value, dict), "JSON document must be an object")
    # parse_constant does not catch valid JSON numeric tokens such as 1e999,
    # which Python decodes to infinity, or escaped unpaired surrogates.
    validate_json_values(value)
    return value


def read_bytes(path: str | Path, max_bytes: int | None = None) -> bytes:
    """Capture a bounded regular file without following its final symlink.

    Nonblocking open prevents a FIFO from waiting for a writer. Descriptor checks
    avoid a stat/open race; the read cap also covers growth after fstat. This is
    not a hard wall-clock deadline for a stalled filesystem or device mount.
    """
    limit = MAX_FILE_BYTES if max_bytes is None else max_bytes
    require(type(limit) is int and limit > 0, "invalid file size limit")
    fd = os.open(path, os.O_RDONLY | os.O_NONBLOCK | os.O_NOFOLLOW | os.O_CLOEXEC)
    with os.fdopen(fd, "rb") as stream:
        info = os.fstat(stream.fileno())
        require(stat.S_ISREG(info.st_mode), f"expected regular file: {path}")
        require(info.st_size <= limit, f"file exceeds {limit}-byte size limit: {path}")
        raw = stream.read(limit + 1)
        require(len(raw) <= limit, f"file exceeds {limit}-byte size limit: {path}")
        return raw


def read_json(path):
    return parse_json(read_bytes(path).decode("utf-8"))


def publish(path, value):
    """Atomically publish a new file; never overwrite an existing round result."""
    path = Path(path)
    fd, temporary = tempfile.mkstemp(prefix=f".{path.name}.", dir=path.parent)
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as stream:
            json.dump(value, stream, ensure_ascii=False, indent=2, allow_nan=False)
            stream.write("\n")
            stream.flush()
            os.fsync(stream.fileno())
        os.link(temporary, path)
    finally:
        os.unlink(temporary)


def load_request(path, require_cwd=False, *, raw: bytes | None = None):
    request = parse_json((read_bytes(path) if raw is None else raw).decode("utf-8"))
    path = Path(path).resolve()
    require(type(request.get("schema_version")) is int and request["schema_version"] == VERSION,
            "unsupported request schema_version")
    for key in ("job_id", "round_id"):
        require(isinstance(request.get(key), str) and
                re.fullmatch(r"[0-9a-f]{32}", request[key]), f"invalid {key}")
    for key in ("depth", "max_depth"):
        require(type(request.get(key)) is int, f"{key} must be an integer")
    require(1 <= request["depth"] < request["max_depth"], "invalid orchestration depth")
    require(isinstance(request.get("cwd"), str) and Path(request["cwd"]).is_absolute(),
            "cwd must be absolute")
    if require_cwd:
        require(Path(request["cwd"]).is_dir(), "project cwd does not exist")
    require(request.get("result_path") == str(path.parent / "result.json"),
            "result_path must name result.json alongside this request; do not move round directories")
    require(isinstance(request.get("task"), str) and request["task"].strip(), "task is empty")
    return request


def validate_result(request, result, check_files=False):
    require(isinstance(result, dict), "response must be a JSON object")
    validate_json_values(result)
    for key in IDENTITY_FIELDS:
        require(type(result.get(key)) is type(request[key]) and result[key] == request[key],
                f"response {key} does not match this round")
    require(result.get("status") in ("success", "error", "blocked"), "invalid result status")
    require(isinstance(result.get("output"), str), "output must be a string")
    for key in ("error", "blocked_reason"):
        require(key in result and (result[key] is None or isinstance(result[key], str)),
                f"{key} must be a string or null")
    status = result["status"]
    require(bool(result["error"] and result["error"].strip()) if status == "error"
            else result["error"] is None, "error must be nonempty only for status=error")
    require(bool(result["blocked_reason"] and result["blocked_reason"].strip()) if status == "blocked"
            else result["blocked_reason"] is None,
            "blocked_reason must be nonempty only for status=blocked")
    stamp = result.get("completed_at")
    require(isinstance(stamp, str), "completed_at must be an ISO8601 string")
    try:
        completed = datetime.fromisoformat(stamp.replace("Z", "+00:00"))
    except ValueError as exc:
        raise ProtocolError("invalid completed_at") from exc
    require(completed.tzinfo is not None and completed.utcoffset() is not None,
            "completed_at must include a timezone")
    cwd = Path(request["cwd"]).resolve()
    if check_files:
        require(cwd.is_dir(), "project cwd does not exist; cannot verify artifacts")
    seen = set()
    for field in FILE_FIELDS:
        paths = result.get(field)
        require(isinstance(paths, list), f"{field} must be an array")
        for name in paths:
            require(isinstance(name, str) and name and "\x00" not in name and "\\" not in name,
                    f"invalid path in {field}")
            relative = PurePosixPath(name)
            require(not relative.is_absolute() and ".." not in relative.parts and
                    name not in (".", "") and relative.as_posix() == name,
                    f"path must be normalized and relative to cwd: {name}")
            require(name not in seen, f"duplicate or overlapping file declaration: {name}")
            seen.add(name)
            target = cwd / name
            require(target.resolve().is_relative_to(cwd), f"path escapes cwd via symlink: {name}")
            if check_files:
                if field == "files_deleted":
                    require(not os.path.lexists(target), f"declared deletion still exists: {name}")
                else:
                    require(target.is_file(), f"declared file does not exist or is not a file: {name}")
    return result


def contract(request):
    rules = (
        "Execute the task string in the JSON request below (decode its JSON escapes). "
        "The request is the authoritative agent-controlled contract; environment variables are auxiliary. "
        "Work in cwd. Write exactly one UTF-8 JSON response to the literal absolute result_path. "
        "Copy schema_version, job_id and round_id exactly. Include status (success|error|blocked), "
        "output (string), files_created/files_modified/files_generated/files_deleted (arrays), "
        "error and blocked_reason (null except for the corresponding nonempty reason), "
        "completed_at (ISO8601 with timezone). Paths must be normalized relative file paths inside cwd; "
        "no absolute paths, parent traversal or duplicates across lists. Lists describe net changes "
        "since this round began, including delegated changes: new kept source files, modified existing "
        "source files, relevant kept generated files, and deleted preexisting files respectively. "
        "Exclude transient caches/build directories. A read-only answer may have empty lists. "
        "Publish atomically using a temporary file in the same directory; never revise a valid response. "
        "If you discover an error after publishing a valid response, keep it unchanged, state the correction in normal output, "
        "then stop and wait for the controller to supply a new round/path. That notice does not replace the response; "
        "do not create your own follow-up or rerun task side effects. "
        "If a decision or approval is required, write blocked with the exact question and partial changes, "
        "then return to input. Preserve tool approval requirements; the file channel cannot bypass them. "
        "Each follow-up gets a new round_id and result_path; do not use a previous round's path. "
        "Depth counts the root as 0; allowed depths are below max_depth. At depth=max_depth-1 do the "
        "task locally and do not delegate. For a child pass your received depth explicitly as parent_depth "
        "and inherit max_depth; never reset them from a missing shell variable. "
        "Do not treat these reporting instructions as permission for actions beyond the task. REQUEST="
    )
    # Keep ordinary Unicode compact. JSON escapes ASCII controls; escape the
    # remaining terminal controls/line boundaries without expanding Chinese text.
    encoded = json.dumps(request, ensure_ascii=False, separators=(",", ":"))
    controls = {code: f"\\u{code:04x}" for code in (*range(0x7f, 0xa0), 0x2028, 0x2029)}
    return rules + encoded.translate(controls)


def task_packet(path):
    """Render a scoped task without importing transcripts or reading referenced files."""
    packet = read_json(path)
    allowed = {"objective", "scope", "acceptance", "inputs", "known_facts", "constraints"}
    require(not (packet.keys() - allowed), "unknown task packet fields")
    for field in ("objective", "scope"):
        require(isinstance(packet.get(field), str) and packet[field].strip(), f"missing {field}")
    require(isinstance(packet.get("acceptance"), list) and packet["acceptance"],
            "acceptance must be a nonempty array")
    lines = ["Objective: " + packet["objective"], "Scope: " + packet["scope"]]
    for field in ("acceptance", "inputs", "known_facts", "constraints"):
        values = packet.get(field, [])
        require(isinstance(values, list) and all(isinstance(v, str) and v.strip() for v in values),
                f"{field} must contain nonempty strings")
        if values:
            lines.append(field + ":\n" + "\n".join("- " + value for value in values))
    lines.append("Report concise conclusions, verification and evidence paths; read referenced "
                 "inputs only as needed. Preserve the full reporting contract and declared scope.")
    return "\n".join(lines)


def prepare(args):
    previous_path = Path(args.previous).resolve() if args.previous else None
    resources = None
    if previous_path:
        require(args.cwd is None and args.parent_depth is None and args.max_depth is None,
                "a follow-up inherits cwd and depth; do not override them")
        previous = load_request(previous_path, require_cwd=True)
        validate_result(previous, read_json(previous["result_path"]))
        cwd, depth, maximum = previous["cwd"], previous["depth"], previous["max_depth"]
        job_id = previous["job_id"]
        resource_path = previous_path.parent / "resources.json"
        if os.path.lexists(resource_path):
            resources = validate_resources(read_json(resource_path), previous)
    else:
        require(args.cwd is not None and args.parent_depth is not None,
                "initial prepare requires --cwd and --parent-depth (root is 0)")
        cwd = str(Path(args.cwd).resolve())
        require(Path(cwd).is_dir(), "cwd must be an existing directory")
        maximum = args.max_depth if args.max_depth is not None else 3
        require(args.parent_depth >= 0 and maximum >= 2, "invalid depth parameters")
        depth = args.parent_depth + 1
        require(depth < maximum, "depth limit reached: execute locally or report inability to delegate")
        job_id = uuid.uuid4().hex
    if getattr(args, "task_packet", None):
        task = task_packet(args.task_packet)
    elif args.task_file == "-":
        task = sys.stdin.read()
    else:
        with Path(args.task_file).open(encoding="utf-8", newline="") as stream:
            task = stream.read()
    require(bool(task.strip()), "task is empty")
    validate_json_values(task)
    root, run = args.root, None
    run_path, temporary = getattr(args, "run", None), getattr(args, "temporary", False)
    prior_managed = previous_path and ((previous_path.parent / "run-ref.json").exists() or
        (previous_path.parent.parent.name == "rounds" and
         (previous_path.parent.parent.parent / "run.json").exists()))
    root_managed = root is not None and Path(root).resolve().name == "rounds" and (Path(root).resolve().parent / "run.json").exists()
    if root is None or run_path is not None or temporary or prior_managed or root_managed:
        import runs
        root, run = runs.round_storage(root, run_path, temporary, previous_path)
    directory = Path(tempfile.mkdtemp(prefix="agent-orchestrator-", dir=root)).resolve()
    request = {
        "schema_version": VERSION, "job_id": job_id, "round_id": uuid.uuid4().hex,
        "cwd": cwd, "result_path": str(directory / "result.json"),
        "depth": depth, "max_depth": maximum,
        "prepared_at": utc_now(), "task": task,
    }
    if previous_path:
        request["previous_request"] = str(previous_path)
    try:
        publish(directory / "request.json", request)
        with (directory / "prompt.txt").open("x", encoding="utf-8") as stream:
            stream.write(contract(request))
        os.chmod(directory / "prompt.txt", 0o600)
        if resources is not None:
            publish(directory / "resources.json", resources)
    except BaseException:
        # No handle has been returned and no target has been launched. Roll back
        # only this newly allocated private directory, never earlier rounds.
        shutil.rmtree(directory)
        raise
    if run is not None:
        # Preserve the complete round if registration fails: a durable reference
        # may already have committed. Never leave it pointing at a deleted round.
        runs.track_request(run["run_path"], directory / "request.json")
    info = {"request_path": str(directory / "request.json"),
            "prompt_path": str(directory / "prompt.txt"), **request}
    if run is not None:
        info.update(run_path=run["run_path"], run_id=run["run_id"], index_path=run["index_path"])
    if getattr(args, "brief", False):
        info.pop("task")
        info["prompt_bytes"] = (directory / "prompt.txt").stat().st_size
    return info


def validate_tmux_selector(selector):
    """Require an explicit default server or a safe absolute socket/named server."""
    require(isinstance(selector, list) and (selector == [] or
            (len(selector) == 2 and selector[0] in ("-S", "-L") and
             isinstance(selector[1], str) and bool(selector[1]) and
             not selector[1].startswith("-") and
             not any(ord(c) < 32 or ord(c) == 127 for c in selector[1]))),
            "explicit valid tmux server selection is required")
    if selector:
        require(Path(selector[1]).is_absolute() if selector[0] == "-S" else "/" not in selector[1],
                "tmux socket must be absolute; server name must not contain a slash")
    return selector


def record(args):
    request_path = Path(args.request).resolve()
    request = load_request(request_path)
    resources = {key: getattr(args, key) for key in (
        "mode", "session", "agent", "pane", "workspace", "owns_agent", "owns_pane",
        "owns_workspace", "owns_session")}
    for key in ("tab", "parent_pane", "parent_tab"):
        resources[key] = getattr(args, key, None)
    resources["owns_tab"] = getattr(args, "owns_tab", False)
    socket = getattr(args, "tmux_socket", None)
    server = getattr(args, "tmux_server", None)
    default = getattr(args, "tmux_default_server", False)
    selectors = int(socket is not None) + int(server is not None) + int(bool(default))
    if resources["mode"] == "tmux":
        require(selectors == 1, "record exactly one explicit tmux server selector")
        resources["tmux_selector"] = validate_tmux_selector(
            ["-S", socket] if socket is not None else ["-L", server] if server is not None else [])
    else:
        require(selectors == 0, "tmux selector requires tmux mode")
    resources["recorded_at"] = utc_now()
    resources["job_id"] = request["job_id"]
    validate_resources(resources, request)
    publish(request_path.parent / "resources.json", resources)
    return resources


def herdr_workspace(value: object, kind: str) -> str:
    """Return the workspace qualifier of an exact public ID, rejecting aliases."""
    suffix = {"workspace": "", "pane": f":p{HERDR_PUBLIC_NUMBER}",
              "tab": f":t{HERDR_PUBLIC_NUMBER}"}[kind]
    match = re.fullmatch(f"(w{HERDR_PUBLIC_NUMBER}){suffix}", value) if isinstance(value, str) else None
    if match is None:
        raise ProtocolError(f"herdr requires an exact public {kind} ID, not an alias or display label")
    return match.group(1)


def validate_resources(resources, request):
    require(resources.get("job_id") == request["job_id"], "resource record belongs to a different job")
    mode = resources.get("mode")
    require(mode in ("insider", "isolated", "tmux"), "invalid resource mode")
    if "tmux_selector" in resources:
        require(mode == "tmux", "tmux selector requires tmux mode")
        validate_tmux_selector(resources["tmux_selector"])
    for key in ("owns_session", "owns_workspace", "owns_pane", "owns_agent"):
        require(type(resources.get(key)) is bool, f"{key} must be a boolean")
    # Older pane-only resource records remain readable without the new fields.
    owns_tab = resources.get("owns_tab", False)
    require(type(owns_tab) is bool, "owns_tab must be a boolean")
    require(mode != "insider" or not resources["owns_session"],
            "refusing to close the caller's session in insider mode")
    require(mode != "tmux" or (not resources["owns_workspace"] and resources.get("workspace") is None),
            "tmux has no herdr workspace")
    require(mode != "tmux" or (not owns_tab and all(resources.get(key) is None
            for key in ("tab", "parent_pane", "parent_tab"))), "tmux has no herdr tab/parent context")
    for key in ("session", "agent", "pane"):
        require(isinstance(resources.get(key), str) and bool(resources[key]) and
                not resources[key].startswith("-") and
                not any(ord(char) < 32 or ord(char) == 127 for char in resources[key]),
                f"invalid resource {key}")
    require(re.fullmatch(r"[a-z][a-z0-9_-]{0,31}", resources["agent"]) is not None,
            "invalid agent name")
    if mode == "tmux":
        require(re.fullmatch(r"%[0-9]+", resources["pane"]) is not None,
                "tmux requires an exact pane ID such as %3")
        require(not any(char in resources["session"] for char in ".:"), "invalid tmux session name")
    else:
        pane_workspace = herdr_workspace(resources["pane"], "pane")
        workspace = resources.get("workspace")
        require(workspace is None or herdr_workspace(workspace, "workspace") == pane_workspace,
                "workspace must match the pane's workspace ID")
        require(not resources["owns_workspace"] or workspace is not None,
                "owned workspace requires its ID")
        tab = resources.get("tab")
        parent_pane = resources.get("parent_pane")
        parent_tab = resources.get("parent_tab")
        qualifiers = {key: herdr_workspace(value, kind) if value is not None else None
                      for key, value, kind in (("tab", tab, "tab"),
                                               ("parent_pane", parent_pane, "pane"),
                                               ("parent_tab", parent_tab, "tab"))}
        require(tab is None or qualifiers["tab"] == pane_workspace,
                "tab must match the pane's workspace ID")
        require(not owns_tab or (tab is not None and workspace is not None),
                "owned tab requires tab and workspace IDs")
        require(parent_tab is None or (parent_pane is not None and
                qualifiers["parent_tab"] == qualifiers["parent_pane"]),
                "parent_tab must match the parent pane's workspace ID")
        if mode == "insider":
            require(parent_pane != resources["pane"], "child pane must differ from parent pane")
            if resources["owns_workspace"]:
                require(parent_pane is not None and workspace != qualifiers["parent_pane"],
                        "owned insider workspace must differ from the recorded parent workspace")
            if owns_tab:
                require(parent_tab is not None and tab != parent_tab,
                        "owned insider tab must differ from the recorded parent tab")
    return resources


def cleanup_plan(args):
    request_path = Path(args.request).resolve()
    # Reporting and cleanup remain possible after the project has disappeared.
    request = load_request(request_path)
    resources = validate_resources(read_json(request_path.parent / "resources.json"), request)
    mode = resources["mode"]
    actions = []
    if resources["owns_agent"]:
        actions.append({"action": "exit_agent", "target": resources["agent"],
                        "transport": mode, "session": resources["session"], "pane": resources["pane"],
                        "instruction": "Use the target dictionary; wait for exit. Inspect blocked UI first."})
    if mode == "tmux":
        cli = ["tmux", *validate_tmux_selector(resources.get("tmux_selector"))]
        if resources["owns_session"]:
            actions.append({"argv": cli + ["kill-session", "-t", "=" + resources["session"]]})
        elif resources["owns_pane"]:
            actions.append({"argv": cli + ["kill-pane", "-t", resources["pane"]]})
    else:
        cli = ["herdr", "--session", resources["session"]]
        if resources["owns_workspace"]:
            actions.append({"argv": cli + ["workspace", "close", resources["workspace"]]})
        elif resources.get("owns_tab", False):
            actions.append({"argv": cli + ["tab", "close", resources["tab"]]})
        elif resources["owns_pane"]:
            actions.append({"argv": cli + ["pane", "close", resources["pane"]]})
        if resources["owns_session"]:
            actions.extend([{"argv": cli + ["session", "stop", resources["session"]]},
                            {"argv": ["herdr", "session", "delete", resources["session"]]}])
    return {"actions": actions, "executes_commands": False,
            "preconditions": ["The job is finished or cancellation has been acknowledged.",
                              "Recheck exact resource identity and recorded ownership.",
                              "Do not close a container with another live job or user resource.",
                              "Only the session owner stops/deletes a shared isolated session."],
            "artifacts": "Retain round files for verification/recovery; remove only exact owned directories later."}


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    commands = parser.add_subparsers(dest="command", required=True)
    new = commands.add_parser("prepare", help="prepare an initial task or a follow-up with fresh result identity")
    new.add_argument("--cwd")
    new.add_argument("--parent-depth", type=int)
    new.add_argument("--max-depth", type=int)
    new.add_argument("--previous", help="previous request.json; requires a valid response to that round")
    task_source = new.add_mutually_exclusive_group(required=True)
    task_source.add_argument("--task-file", help="UTF-8 task file, or - for stdin")
    task_source.add_argument("--task-packet", help="JSON objective/scope/acceptance and optional context")
    new.add_argument("--brief", action="store_true", help="omit task echo; keep paths and identity")
    storage = new.add_mutually_exclusive_group()
    storage.add_argument("--root", help="existing directory for standalone legacy round directories")
    storage.add_argument("--run", help="run.json for a shared persistent run; follow-ups inherit it")
    storage.add_argument("--temporary", action="store_true", help="explicit short-lived run in system temp")
    for name in ("validate", "write-result", "record", "cleanup-plan"):
        command = commands.add_parser(name)
        command.add_argument("--request", required=True)
        if name == "validate":
            command.add_argument("--check-files", action="store_true")
        if name == "write-result":
            command.add_argument("--input", required=True, help="candidate JSON response")
        if name == "record":
            command.add_argument("--mode", choices=("insider", "isolated", "tmux"), required=True)
            selector = command.add_mutually_exclusive_group()
            selector.add_argument("--tmux-socket")
            selector.add_argument("--tmux-server")
            selector.add_argument("--tmux-default-server", action="store_true")
            for field in ("session", "agent", "pane"):
                command.add_argument("--" + field, required=True)
            for field in ("workspace", "tab", "parent-pane", "parent-tab"):
                command.add_argument("--" + field)
            for field in ("agent", "pane", "tab", "workspace", "session"):
                command.add_argument("--owns-" + field, action="store_true")
    args = parser.parse_args()
    try:
        if args.command == "prepare":
            output = prepare(args)
        elif args.command == "record":
            output = record(args)
        elif args.command == "cleanup-plan":
            output = cleanup_plan(args)
        else:
            request = load_request(args.request)
            candidate = args.input if args.command == "write-result" else request["result_path"]
            output = validate_result(request, read_json(candidate), getattr(args, "check_files", False))
            if args.command == "write-result":
                publish(request["result_path"], output)
        print(json.dumps(output, ensure_ascii=False, allow_nan=False))
        return 0
    except FileNotFoundError as exc:
        print(json.dumps({"error": str(exc), "kind": "missing"}), file=sys.stderr)
        return 2
    except (OSError, ValueError, KeyError, TypeError, RuntimeError) as exc:
        print(json.dumps({"error": str(exc), "kind": "invalid"}), file=sys.stderr)
        return 3


if __name__ == "__main__":
    sys.exit(main())
