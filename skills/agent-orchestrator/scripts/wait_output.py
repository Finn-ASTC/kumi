#!/usr/bin/env python3
"""Wait for a complete checkpoint line in an authorized append-only UTF-8 log.

Read-only: never starts/stops the business process, grants approval or writes files.
"""
import argparse
import json
import math
import os
from pathlib import Path
import stat
import sys
import time

import protocol


MAX_LINE_BYTES = 64 * 1024
CHUNK_BYTES = 64 * 1024


def file_identity(info):
    return f"{info.st_dev}:{info.st_ino}"


def wait_output(path: str | Path, contains: list[str], after: int = 0,
                identity: str | None = None, timeout: float = 30,
                interval: float = 0.2) -> dict:
    """Return the first matching complete line, or a resumable timeout cursor.

    ``after`` counts consumed bytes through the last complete line. Partial lines
    are reread on resume. Identity and monotonic size checks detect replacement
    and observed truncation; append-only remains a caller precondition (in-place
    edits or truncate/regrow between checks cannot be reliably detected).
    """
    protocol.require(type(after) is int and after >= 0, "after must be a nonnegative byte offset")
    protocol.require(bool(contains) and all(isinstance(p, str) and p and
                     not any(c in p for c in '\r\n\0') for p in contains),
                     "contains must have nonempty literal single-line patterns")
    for number in (timeout, interval):
        protocol.require(type(number) in (int, float) and math.isfinite(number), "invalid wait interval")
    protocol.require(timeout >= 0 and interval > 0, "timeout must be nonnegative and interval positive")
    started = time.monotonic()
    deadline = started + timeout
    fd = None
    current_identity = None
    high_water = 0
    buffer = b''

    def result(status, matched=None, line=None):
        return {"status": status, "matched": matched, "line": line,
                "after": after, "identity": current_identity,
                "elapsed_seconds": time.monotonic() - started}

    try:
        while True:
            if fd is None:
                try:
                    fd = os.open(path, os.O_RDONLY | os.O_NONBLOCK | os.O_NOFOLLOW | os.O_CLOEXEC)
                except FileNotFoundError:
                    # An expected existing source disappearing is not a fresh log.
                    if identity is not None or after:
                        raise
                if fd is not None:
                    info = os.fstat(fd)
                    protocol.require(stat.S_ISREG(info.st_mode), "expected regular log file")
                    current_identity = file_identity(info)
                    protocol.require(identity is None or current_identity == identity, "log identity changed")
                    protocol.require(after <= info.st_size, "log truncated or offset beyond EOF")
                    if after:
                        protocol.require(os.pread(fd, 1, after - 1) == b'\n', "offset is not a line boundary")
                    os.lseek(fd, after, os.SEEK_SET)
                    high_water = info.st_size
            if fd is not None:
                info = os.stat(path, follow_symlinks=False)
                protocol.require(stat.S_ISREG(info.st_mode) and file_identity(info) == current_identity,
                                 "log replaced or rotated")
                protocol.require(info.st_size >= high_water, "log truncated")
                high_water = info.st_size
                chunk = os.read(fd, CHUNK_BYTES)
                buffer += chunk
                while b'\n' in buffer:
                    raw, buffer = buffer.split(b'\n', 1)
                    protocol.require(len(raw) <= MAX_LINE_BYTES, "log line exceeds size limit")
                    line = raw.decode('utf-8').removesuffix('\r')
                    after += len(raw) + 1
                    match = next((p for p in contains if p in line), None)
                    if match is not None:
                        return result('matched', match, line)
                protocol.require(len(buffer) <= MAX_LINE_BYTES, "log line exceeds size limit")
                if chunk and time.monotonic() < deadline:
                    continue
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                return result('timeout')
            time.sleep(min(interval, remaining))
    finally:
        if fd is not None:
            os.close(fd)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--path', required=True)
    parser.add_argument('--contains', action='append', required=True, help='literal substring; repeat for alternatives')
    parser.add_argument('--after', type=int, default=0, help='byte offset from previous result')
    parser.add_argument('--identity', help='dev:inode identity from previous result')
    parser.add_argument('--timeout', type=float, default=30)
    parser.add_argument('--interval', type=float, default=0.2)
    args = parser.parse_args()
    try:
        output = wait_output(args.path, args.contains, args.after, args.identity, args.timeout, args.interval)
        print(json.dumps(output, ensure_ascii=False, allow_nan=False))
        return 0 if output['status'] == 'matched' else 1
    except (OSError, ValueError, TypeError) as exc:
        print(json.dumps({'status': 'error', 'error': str(exc)}), file=sys.stderr)
        return 2


if __name__ == '__main__':
    sys.exit(main())
