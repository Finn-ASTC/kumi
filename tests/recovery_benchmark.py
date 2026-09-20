#!/usr/bin/env python3
"""Measure synthetic recovery display/content I/O; no models or terminals."""

import argparse
from contextlib import contextmanager
import json
from pathlib import Path
import sys
import tempfile
import time
from typing import Any, Callable, Iterator
from unittest.mock import patch


@contextmanager
def source_reads(protocol: Any) -> Iterator[dict[str, int]]:
    original = protocol.read_bytes
    counts = {"source_reads": 0, "source_bytes": 0}

    def read(*args: Any, **kwargs: Any) -> bytes:
        raw = original(*args, **kwargs)
        counts["source_reads"] += 1
        counts["source_bytes"] += len(raw)
        return raw

    with patch.object(protocol, "read_bytes", side_effect=read):
        yield counts


def measure(call: Callable[[], dict[str, Any]]) -> tuple[dict[str, Any], dict[str, Any]]:
    start = time.perf_counter()
    value = call()
    elapsed = time.perf_counter() - start
    size = len((json.dumps(value, ensure_ascii=False, allow_nan=False) + "\n").encode("utf-8"))
    return value, {"output_bytes": size, "elapsed_seconds": elapsed}


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--root", type=Path, required=True, help="existing parent for retained synthetic evidence")
    parser.add_argument("--skills-root", type=Path, default=Path(__file__).resolve().parents[1] / "skills",
                        help="six-skill package to exercise; may be an isolated copy")
    args = parser.parse_args()
    sys.path.insert(0, str(args.skills_root.resolve() / "agent-orchestrator/scripts"))
    import jobs
    import protocol
    import runs
    import watch
    directory = Path(tempfile.mkdtemp(prefix="recovery-benchmark-", dir=args.root.resolve()))
    task = directory / "task.txt"
    task.write_text("Inspect the fixture and return a concise report; no edits.", encoding="utf-8")
    rows = []
    for count in (1, 10, 50, 100):
        run = runs.init_run(directory)
        infos = []
        for i in range(count):
            info = protocol.prepare(argparse.Namespace(cwd=str(directory), parent_depth=0, max_depth=None,
                previous=None, task_file=str(task), root=None, run=run["run_path"]))
            jobs.register(run["index_path"], info["request_path"], now=100)
            protocol.record(argparse.Namespace(request=info["request_path"], mode="isolated", session="fixture",
                agent="worker", pane=f"w{i}:p1", workspace=None, owns_agent=True, owns_pane=True,
                owns_workspace=False, owns_session=False))
            infos.append(info)
        watch.init_watch([i["request_path"] for i in infos])
        row: dict[str, Any] = {"jobs": count, "watch_targets": count}
        for name, method in (("full", runs.recover), ("summary", runs.recover_summary)):
            with source_reads(protocol) as stats:
                result, metric = measure(lambda: method(run["run_path"], limit=1, now=101))
            assert result["complete"]
            row[name] = {**metric, **stats}
            (directory / f"{count}-{name}.json").write_text(json.dumps(result, ensure_ascii=False))
        cold, metric = measure(lambda: runs.recover_delta(run["run_path"], limit=1, now=101))
        row["delta_cold"] = {**metric, **cold["read_stats"]}
        previous = cold
        while previous["more_job_changes"] or previous["more_watch_changes"]:
            previous = runs.recover_delta(run["run_path"], cursor_path=previous["cursor_path"],
                                          since=previous["next_cursor"], limit=200, now=101)
        warm, metric = measure(lambda: runs.recover_delta(run["run_path"], cursor_path=previous["cursor_path"],
                                                       since=previous["next_cursor"], limit=1, now=101))
        assert warm["complete"] and not warm["reset"] and not warm["job_changes"] and not warm["watch_changes"]
        row["delta_warm"] = {**metric, **warm["read_stats"]}
        state = jobs.load(Path(run["index_path"]), infos[0]["job_id"])
        jobs.claim(run["index_path"], infos[0]["job_id"], state["revision"], "benchmark", 100, now=100)
        changed, metric = measure(lambda: runs.recover_delta(run["run_path"], cursor_path=warm["cursor_path"],
                                                          since=warm["next_cursor"], limit=1, now=101))
        assert changed["complete"] and len(changed["job_changes"]) == 1
        row["delta_one_changed"] = {**metric, **changed["read_stats"]}
        for name, value in (("cold", cold), ("warm", warm), ("changed", changed)):
            (directory / f"{count}-delta-{name}.json").write_text(json.dumps(value, ensure_ascii=False))
        rows.append(row)
    result = {"python": sys.version.split()[0], "limit": 1, "measurement_now": 101,
              "evidence_directory": str(directory), "measurements": rows}
    (directory / "measurements.json").write_text(json.dumps(result, indent=2))
    print(json.dumps(result, indent=2))


if __name__ == "__main__":
    main()
