"""Opt-in recovery cache and delivery cursor; never caches decisions or live locks."""

import base64
from contextlib import contextmanager
import fcntl
import hashlib
import json
import os
from pathlib import Path
import stat
import tempfile
import time
from typing import Any, BinaryIO, Iterator
import uuid

import protocol


# Bump when cache validation or projected row semantics change incompatibly.
VERSION = 1
MAX_CACHE_BYTES = 4 * 1024 * 1024
MAX_CACHE_FILES = 4096
MAX_CURSOR_BYTES = 32 * 1024 * 1024


def encoded(value: Any) -> bytes:
    return json.dumps(value, sort_keys=True, ensure_ascii=False, allow_nan=False,
                      separators=(",", ":")).encode("utf-8")


def digest(value: Any) -> str:
    return hashlib.sha256(encoded(value)).hexdigest()


def signature(info: os.stat_result) -> list[int]:
    return [info.st_dev, info.st_ino, info.st_size, info.st_mtime_ns, info.st_ctime_ns,
            info.st_mode, info.st_uid, info.st_gid, info.st_nlink]


class ReadCache:
    """Reuse content only after opening and rechecking the current descriptor.

    Local filesystem metadata is the coherency boundary, not an authentication
    scheme. The cursor is disposable state owned by the controller.
    """

    def __init__(self) -> None:
        self.entries: dict[str, tuple[list[int], bytes]] = {}
        self.retained: dict[str, tuple[list[int], bytes]] = {}
        self.retained_bytes = 0
        self.stats = {"source_reads": 0, "source_bytes": 0, "cache_hits": 0}

    def seed(self, entries: dict[str, Any]) -> None:
        for path, entry in entries.items():
            self.entries.setdefault(path, (entry["signature"], base64.b64decode(entry["data"], validate=True)))

    def remember(self, path: str, stamp: list[int], raw: bytes) -> None:
        size = self.retained_bytes - len(self.retained.get(path, ([], b""))[1]) + len(raw)
        if size <= MAX_CACHE_BYTES and (path in self.retained or len(self.retained) < MAX_CACHE_FILES):
            self.retained[path] = (stamp, raw)
            self.retained_bytes = size
            self.entries[path] = (stamp, raw)

    def read(self, path: str | Path, stream: BinaryIO, info: os.stat_result, limit: int) -> bytes:
        key, stamp = os.path.abspath(path), signature(info)
        entry = self.entries.get(key)
        if entry is not None and entry[0] == stamp and signature(os.fstat(stream.fileno())) == stamp:
            raw = entry[1]
            protocol.require(len(raw) <= limit, f"file exceeds {limit}-byte size limit: {path}")
            self.stats["cache_hits"] += 1
            self.remember(key, stamp, raw)
            return raw
        self.stats["source_reads"] += 1
        raw = stream.read(limit + 1)
        self.stats["source_bytes"] += len(raw)
        protocol.require(len(raw) <= limit, f"file exceeds {limit}-byte size limit: {path}")
        if len(raw) == info.st_size and signature(os.fstat(stream.fileno())) == stamp:
            self.remember(key, stamp, raw)
        return raw

    def export(self) -> dict[str, Any]:
        return {p: {"signature": stamp, "data": base64.b64encode(raw).decode("ascii")}
                for p, (stamp, raw) in self.retained.items()}


def cursor_location(run: dict[str, Any], cursor_path: str | Path | None) -> Path:
    """Keep all writes in one private, disposable subdirectory of this run."""
    import runs
    root = Path(run["run_path"]).parent / "recovery"
    path = root / uuid.uuid4().hex / "cursor.json" if cursor_path is None else Path(cursor_path)
    protocol.require(path.is_absolute() and path.name == "cursor.json" and
                     path.parent.parent == root and runs.ID.fullmatch(path.parent.name),
                     "cursor must be this run's recovery/<id>/cursor.json")
    for directory in (root, path.parent):
        protocol.require(not directory.is_symlink(), "cursor directory cannot be a symlink")
        directory.mkdir(mode=0o700, exist_ok=True)
        protocol.require(directory.resolve() == directory, "cursor directory is redirected")
    protocol.require(not path.is_symlink(), "cursor cannot be a symlink")
    return path


@contextmanager
def cursor_lock(path: Path) -> Iterator[None]:
    fd = os.open(path.with_name(".lock"), os.O_RDWR | os.O_CREAT | os.O_NOFOLLOW | os.O_CLOEXEC, 0o600)
    with os.fdopen(fd, "rb") as stream:
        protocol.require(stat.S_ISREG(os.fstat(stream.fileno()).st_mode), "invalid cursor lock")
        fcntl.flock(stream.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
        yield


def load_cursor(path: Path) -> tuple[dict[str, Any] | None, str | None, int, int]:
    """Reject malformed/bit-rotted caches as a whole; never partially seed them."""
    import runs
    raw = b""
    cache_raw = b""
    try:
        raw = protocol.read_bytes(path, MAX_CURSOR_BYTES)
        envelope = protocol.parse_json(raw.decode("utf-8"))
        state = envelope["state"]
        protocol.require(isinstance(state, dict) and digest(state) == envelope["sha256"], "cursor checksum mismatch")
        if type(state.get("version")) is not int or state["version"] != VERSION:
            return None, "version_changed", len(raw), 0
        protocol.require(isinstance(state.get("token"), str) and runs.ID.fullmatch(state["token"]), "invalid cursor token")
        protocol.require(isinstance(state.get("scope"), dict), "invalid cursor scope")
        protocol.require("files" not in state, "unexpected inline content cache")
        cache_raw = protocol.read_bytes(path.with_name("contents.json"), MAX_CURSOR_BYTES)
        protocol.require(hashlib.sha256(cache_raw).hexdigest() == state["cache_sha256"], "content cache checksum mismatch")
        cache = protocol.parse_json(cache_raw.decode("utf-8"))
        protocol.require(isinstance(cache, dict) and len(cache) <= MAX_CACHE_FILES, "invalid cursor cache")
        total = 0
        for key, entry in cache.items():
            protocol.require(Path(key).is_absolute() and isinstance(entry, dict), "invalid cached path")
            stamp = entry["signature"]
            protocol.require(isinstance(stamp, list) and len(stamp) == 9 and all(type(n) is int for n in stamp),
                             "invalid cached signature")
            content = base64.b64decode(entry["data"], validate=True)
            protocol.require(len(content) == stamp[2], "invalid cached content size")
            total += len(content)
        protocol.require(total <= MAX_CACHE_BYTES, "cursor cache too large")
        for kind in ("jobs", "watches"):
            delivered, pending = state["delivered"][kind], state["pending"][kind]
            protocol.require(isinstance(delivered, dict) and all(runs.ID.fullmatch(k) and
                             isinstance(v, str) and len(v) == 64 for k, v in delivered.items()), "invalid delivery baseline")
            protocol.require(isinstance(pending, list) and all(isinstance(k, str) and runs.ID.fullmatch(k) for k in pending)
                             and len(set(pending)) == len(pending), "invalid delivery backlog")
        state["files"] = cache
        return state, None, len(raw), len(cache_raw)
    except FileNotFoundError:
        return None, "cursor_invalid" if raw else "cursor_missing", len(raw), 0
    except (OSError, ValueError, KeyError, TypeError, UnicodeError):
        return None, "cursor_invalid", len(raw), len(cache_raw)


def write_atomic(path: Path, raw: bytes) -> None:
    protocol.require(not path.is_symlink(), "cursor cannot be a symlink")
    fd, temporary = tempfile.mkstemp(prefix=".cursor-", dir=path.parent)
    try:
        with os.fdopen(fd, "wb") as stream:
            stream.write(raw)
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(temporary, path)
        directory_fd = os.open(path.parent, os.O_RDONLY | os.O_DIRECTORY)
        try:
            os.fsync(directory_fd)
        finally:
            os.close(directory_fd)
    finally:
        if os.path.exists(temporary):
            os.unlink(temporary)


def save_cursor(path: Path, state: dict[str, Any]) -> tuple[int, int]:
    """Write content only if changed; interrupted pairs invalidate on next read."""
    contents = encoded(state["files"])
    content_hash = hashlib.sha256(contents).hexdigest()
    stored = {k: v for k, v in state.items() if k != "files"}
    stored["cache_sha256"] = content_hash
    raw = encoded({"state": stored, "sha256": digest(stored)})
    protocol.require(max(len(raw), len(contents)) <= MAX_CURSOR_BYTES,
                     "cursor exceeds size bound; use summary or narrower job scope")
    content_bytes = 0
    if state.get("cache_sha256") != content_hash:
        write_atomic(path.with_name("contents.json"), contents)
        content_bytes = len(contents)
    write_atomic(path, raw)
    return len(raw), content_bytes


def changes(rows: list[dict[str, Any]], kind: str, state: dict[str, Any],
            limit: int, complete: bool) -> tuple[list[dict[str, Any]], int]:
    identity = "job_id" if kind == "jobs" else "watch_id"
    current = {row[identity]: row for row in rows}
    hashes = {key: digest(row) for key, row in current.items()}
    delivered = state["delivered"][kind]
    changed = {key for key, value in hashes.items() if delivered.get(key) != value}
    if complete:
        changed.update(delivered.keys() - current.keys())
    pending = [key for key in state["pending"][kind] if key in changed]
    known = set(pending)
    pending.extend(sorted(changed - known))
    page = []
    for key in pending[:limit]:
        if key in current:
            page.append({"op": "upsert", identity: key, "value": current[key]})
            delivered[key] = hashes[key]
        else:
            page.append({"op": "remove", identity: key})
            del delivered[key]
    state["pending"][kind] = pending[limit:]
    return page, len(pending)


def recover(run_path: str | Path, limit: int = 20, action_offset: int = 0,
            waiting_offset: int = 0, error_offset: int = 0, job_id: str | None = None,
            now: float | None = None, cursor_path: str | Path | None = None,
            since: str | None = None) -> dict[str, Any]:
    """Recompute recovery from live metadata and reuse unchanged file contents."""
    import reviews
    import runs
    protocol.require(type(limit) is int and 1 <= limit <= 200, "invalid recovery limit")
    protocol.require(all(type(v) is int and v >= 0 for v in (action_offset, waiting_offset, error_offset)), "invalid offset")
    protocol.require(job_id is None or (isinstance(job_id, str) and runs.ID.fullmatch(job_id)), "invalid job filter")
    protocol.require(since is None or (cursor_path is not None and isinstance(since, str) and runs.ID.fullmatch(since)),
                     "--since needs a cursor path and valid token")
    current = time.time() if now is None else now
    reviews.timestamp(current)
    cache = ReadCache()
    with protocol.cached_reads(cache):
        run = runs.load_run(run_path)
    path = cursor_location(run, cursor_path)
    scope = {"run_path": run["run_path"], "run_id": run["run_id"], "job_id": job_id}
    with cursor_lock(path):
        state, reason, cursor_bytes, content_bytes = load_cursor(path)
        if cursor_path is None:
            reason = "initial"
        elif state is not None:
            if state["scope"] != scope:
                reason = "scope_changed"
            elif since is None:
                reason = "token_required"
            elif state["token"] != since:
                reason = "token_mismatch"
        if reason is not None:
            state = {"version": VERSION, "scope": scope, "delivered": {"jobs": {}, "watches": {}},
                     "pending": {"jobs": [], "watches": []}, "files": {}}
        cache.seed(state["files"])
        with protocol.cached_reads(cache):
            full = runs.recover(run["run_path"], limit, action_offset, waiting_offset, job_id, current)
        summary = runs.summarize_recovery(full, limit, current, error_offset=error_offset)
        for key in ("jobs", "watches", "more_jobs", "more_watches", "next_job_offset", "next_watch_offset"):
            summary.pop(key)
        job_rows = [{k: v for k, v in row.items() if k != "completion"} for row in full["jobs"]]
        watch_rows = [{k: v for k, v in row.items() if k != "targets"} |
                      {"health": runs.watch_diagnostics(row, current)} for row in full["watches"]]
        job_changes, job_count = changes(job_rows, "jobs", state, limit, full["complete"])
        watch_changes, watch_count = changes(watch_rows, "watches", state, limit, full["complete"])
        state.update(token=uuid.uuid4().hex, files=cache.export())
        written, content_written = save_cursor(path, state)
        summary.update(mode="delta", reset=reason is not None, reset_reason=reason,
                       cursor_path=str(path), next_cursor=state["token"],
                       job_changes=job_changes, watch_changes=watch_changes,
                       change_counts={"jobs": job_count, "watches": watch_count},
                       more_job_changes=job_count > limit, more_watch_changes=watch_count > limit,
                       read_stats={**cache.stats, "retained_files": len(cache.retained),
                                   "retained_bytes": cache.retained_bytes, "cursor_read_bytes": cursor_bytes,
                                   "cursor_write_bytes": written, "cache_read_bytes": content_bytes,
                                   "cache_write_bytes": content_written}, writes_cursor=True)
        summary["note"] = ("Apply reset before changes; resume only with the last received cursor token. "
                           "Pending queues, totals, errors and health are current and repeat independently of changes. "
                           "Changes describe compact rows, not an event history or job completion. "
                           "Directory scans, opens and metadata checks still run; cursor I/O is separate. "
                           "Use supervision.py before independent work; approval never transfers by text or cursor.")
        return summary
