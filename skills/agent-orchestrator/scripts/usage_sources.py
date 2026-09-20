"""Read-only run/native source discovery and explicit evidence-backed binding audits."""

import hashlib
from pathlib import Path
from typing import Any

import jobs
import native_usage
import protocol
import runs
import usage


def discover(run_path: str) -> dict[str, Any]:
    """Inventory pinned rounds, including unindexed candidates, without reading terminals."""
    run = runs.load_run(run_path)
    root = Path(run["run_path"]).parent
    subjects: dict[str, dict[str, Any]] = {}
    errors = []
    known_requests = set()
    subjects["controller:" + run["run_id"]] = dict(subject_id="controller:" + run["run_id"],
        kind="controller", request_path=None, native_candidates=[], ledger_sources=[])
    for member in sorted((root / "requests").glob("*.json")):
        try:
            record = protocol.read_json(member)
            path = record["request_path"]
            protocol.require(member.name == record["round_id"] + ".json" and
                             runs.run_for_request(path) == run, "round membership changed")
            known_requests.add(path)
            subject = "round:" + record["round_id"]
            protocol.require(subject not in subjects, "duplicate round registration")
            chain = usage.request_chain(path)
            index_binding = chain[0][0].parent / "job-index.json"
            if index_binding.exists() or index_binding.is_symlink():
                protocol.require(protocol.read_json(index_binding) == dict(version=1, job_id=record["job_id"],
                                 index_path=run["index_path"]), "job index binding changed")
                protocol.require((Path(run["index_path"]) / record["job_id"]).is_dir(),
                                 "bound job history is missing")
            sources = {entry["source"] for _, entry in usage.stored_samples(chain)
                       if entry["round_id"] == record["round_id"]}
            subjects[subject] = dict(subject_id=subject, kind="round", request_path=path,
                job_id=record["job_id"], round_id=record["round_id"], indexed_job=False,
                native_candidates=[], ledger_sources=sorted(sources))
        except (OSError, ValueError, KeyError, TypeError, IndexError) as exc:
            errors.append(dict(path=str(member), error=str(exc)))
    for directory in sorted(Path(run["rounds_root"]).iterdir()):
        if directory.is_dir() and str(directory / "request.json") not in known_requests:
            errors.append(dict(path=str(directory), error="unregistered or unreadable run round"))
    index = Path(run["index_path"])
    for directory in sorted(index.iterdir()):
        if not directory.is_dir() or not jobs.ID.fullmatch(directory.name):
            continue
        try:
            state = jobs.load(index, directory.name)
            for sequence in range(1, state["revision"] + 1):
                path = directory / f"{sequence:012d}.json"
                event = protocol.read_json(path)
                protocol.require(type(event.get("version")) is int and event["version"] == 1 and
                                 type(event.get("revision")) is int and event["revision"] == sequence and
                                 event.get("job_id") == directory.name and event.get("index_path") == str(index),
                                 "job history identity/revision changed")
                jobs.check_contract(event)
                subject = subjects.get("round:" + event["round_id"])
                protocol.require(subject is not None and subject["request_path"] == event["active_request"],
                                 "indexed round missing from run inventory")
                subject["indexed_job"] = True
                native = event["native"]
                protocol.require(isinstance(native, dict) and set(native) == {"session_id", "turn_id"},
                                 "invalid historical native identity")
                for value in native.values():
                    protocol.require(value is None or isinstance(value, str) and bool(value.strip()),
                                     "invalid historical native identity")
                if native["session_id"] is not None:
                    found = next((c for c in subject["native_candidates"] if
                                  (c["session_id"], c["turn_id"]) == (native["session_id"], native["turn_id"])), None)
                    if found is None:
                        subject["native_candidates"].append({**native, "revision_path": str(path)})
        except (OSError, ValueError, KeyError, TypeError, IndexError) as exc:
            errors.append(dict(path=str(directory), error=str(exc)))
    return dict(run_path=run["run_path"], run_id=run["run_id"], complete=not errors, errors=errors,
                subjects=list(subjects.values()), executes_commands=False,
                note="Native facts and ledger source names are candidates, not automatic bindings. "
                     "Prepared rounds are not proof of submission. No cwd/time/latest-session inference; "
                     "no transcript, launch arguments or lease tokens returned. Recheck after run changes.")


def _binding(value: Any) -> None:
    """Validate explicit identities and a bounded frozen receipt, without exposing its content."""
    required = {"subject_id", "host", "session_id", "log_path", "native_ids", "evidence"}
    protocol.require(isinstance(value, dict) and required <= set(value) <= required | {"ledger_request_paths"},
                     "invalid source binding fields")
    for name in ("subject_id", "session_id", "log_path"):
        native_usage.nonempty(value[name], name)
    protocol.require(value["host"] in ("codex", "omp", "opencode", "hermes"), "unsupported binding host")
    protocol.require(Path(value["log_path"]).is_absolute(), "log_path must be absolute")
    ids = value["native_ids"]
    protocol.require((ids is None and value["host"] == "hermes") or isinstance(ids, list) and bool(ids) and
                     all(isinstance(i, str) and bool(i.strip()) for i in ids) and len(set(ids)) == len(ids),
                     "native_ids must be explicit unique IDs, or null for Hermes cumulative evidence")
    proof = value["evidence"]
    protocol.require(isinstance(proof, dict) and set(proof) == {"path", "sha256"}, "invalid binding evidence")
    native_usage.nonempty(proof["path"], "evidence path")
    protocol.require(Path(proof["path"]).is_absolute(), "evidence path must be absolute")
    actual = hashlib.sha256(protocol.read_bytes(proof["path"])).hexdigest()
    protocol.require(proof["sha256"] == actual, "binding evidence hash mismatch")
    paths = value.get("ledger_request_paths", [])
    protocol.require(isinstance(paths, list) and all(isinstance(p, str) and Path(p).is_absolute() for p in paths),
                     "ledger_request_paths must be absolute request paths")


def audit(manifest_path: str) -> dict[str, Any]:
    """Cross-check bindings, descendants and recorded attribution; never import or mutate."""
    manifest = protocol.read_json(manifest_path)
    protocol.require(set(manifest) == {"version", "run_path", "bindings"} and
                     type(manifest["version"]) is int and manifest["version"] == 1, "invalid source audit manifest")
    native_usage.nonempty(manifest["run_path"], "run_path")
    protocol.require(Path(manifest["run_path"]).is_absolute(), "run_path must be absolute")
    inventory = discover(manifest["run_path"])
    protocol.require(inventory["complete"], "run source inventory is incomplete; inspect discover-sources errors")
    subjects = {s["subject_id"]: s for s in inventory["subjects"]}
    bindings = manifest["bindings"]
    protocol.require(isinstance(bindings, list), "bindings must be an array")
    logs: dict[str, dict[str, Any]] = {}
    origins = {}
    seen = set()
    request_paths = {s["request_path"] for s in subjects.values() if s["request_path"] is not None}
    issues: list[dict[str, Any]] = []
    bound: dict[str, list[dict[str, Any]]] = {}
    ledger_scopes: dict[tuple[str, str], set[tuple[str, str]]] = {}
    for binding in bindings:
        _binding(binding)
        source = binding["host"] + ":" + binding["session_id"]
        subject = binding["subject_id"]
        protocol.require((subject, source) not in seen, "duplicate subject/source binding")
        seen.add((subject, source))
        protocol.require(subject in subjects or subject == "native:" + source, "unknown binding subject")
        protocol.require(subject not in subjects or subjects[subject]["kind"] != "round" or
                         not binding.get("ledger_request_paths"), "round bindings use their registered request only")
        scope = set()
        for path in binding.get("ledger_request_paths", []):
            chain = usage.request_chain(path)
            scope.update((r["job_id"], r["round_id"]) for _, r in chain)
            request_paths.add(str(Path(path).resolve()))
        ledger_scopes[(subject, source)] = scope
        if source in logs:
            protocol.require(origins[source] == binding["log_path"], "same source bound to different log paths")
        else:
            logs[source] = (native_usage.inspect_log(binding["host"], binding["log_path"], binding["session_id"])
                            if binding["host"] == "hermes" and binding["native_ids"] is None else
                            native_usage.read_log(binding["host"], binding["log_path"], binding["session_id"]))
            origins[source] = binding["log_path"]
        log = logs[source]
        if binding["host"] == "hermes":
            protocol.require((log.get("measurement") == "cumulative_snapshot") == (binding["native_ids"] is None),
                             "Hermes hook stores require explicit native turn IDs; cumulative stores require null")
        if binding["native_ids"] is not None:
            protocol.require(set(binding["native_ids"]) <= set(log["native_ids"]), "bound native turn not found")
        bound.setdefault(subject, []).append(binding)

    # Walk only relationships from explicitly rooted bindings. Arbitrary native
    # sessions cannot become run members just by pointing at one another.
    reachable = {b["host"] + ":" + b["session_id"] for subject, values in bound.items()
                 if subject in subjects for b in values}
    queue = sorted(reachable)
    lineage_unknown = []
    child_sources = set()
    lineage: dict[str, set[str]] = {}
    for source in queue:
        log = logs.get(source)
        if log is None:
            continue
        children = log.get("child_session_ids")
        if children is None:
            lineage_unknown.append(source)
            continue
        for child in children:
            native_usage.nonempty(child, "child session ID")
            child_source = log["host"] + ":" + child
            child_sources.add(child_source)
            lineage.setdefault(source, set()).add(child_source)
            if child_source in logs and logs[child_source].get("parent_session_id") != log["session_id"]:
                issues.append(dict(code="native_parent_mismatch", source=child_source,
                                   expected_parent=log["session_id"],
                                   observed_parent=logs[child_source].get("parent_session_id")))
            if child_source not in reachable:
                reachable.add(child_source)
                queue.append(child_source)
    # Remove roots in topological order; any remaining graph contains a cycle.
    degrees = dict.fromkeys(reachable, 0)
    for children in lineage.values():
        for child in children:
            degrees[child] += 1
    roots = sorted(s for s, degree in degrees.items() if degree == 0)
    for source in roots:
        for child in sorted(lineage.get(source, set())):
            degrees[child] -= 1
            if degrees[child] == 0:
                roots.append(child)
    if len(roots) != len(reachable):
        issues.append(dict(code="native_lineage_cycle",
                           sources=sorted(s for s, degree in degrees.items() if degree)))
    for subject in bound:
        if subject not in subjects:
            protocol.require(subject.removeprefix("native:") in child_sources,
                             "native subject is not a discovered descendant of a bound run actor")
    covered_sources = {b["host"] + ":" + b["session_id"] for b in bindings}
    unbound = sorted([s for s in subjects if s not in bound] +
                     ["native:" + s for s in child_sources if s not in covered_sources])

    # Native user/turn identities must belong to one explicit actor, even when
    # several jobs share one native session. Counters cannot establish ownership.
    owners: dict[tuple[str, str], set[str]] = {}
    for subject, values in bound.items():
        for binding in values:
            source = binding["host"] + ":" + binding["session_id"]
            for native_id in binding["native_ids"] or []:
                owners.setdefault((source, native_id), set()).add(subject)
    for (source, native_id), actors in owners.items():
        if len(actors) > 1:
            issues.append(dict(code="conflicting_turn_owners", source=source, native_id=native_id,
                               subjects=sorted(actors)))
    for subject, info in subjects.items():
        for candidate in info["native_candidates"]:
            if not any(b["session_id"] == candidate["session_id"] and
                       (candidate["turn_id"] is None or b["native_ids"] is None or candidate["turn_id"] in b["native_ids"])
                       for b in bound.get(subject, [])):
                issues.append(dict(code="unbound_native_candidate", subject_id=subject, **candidate))

    sources = []
    inspections = []
    expected_owners: dict[tuple[str, str], set[str]] = {}
    native_samples: dict[tuple[str, str], dict[str, Any]] = {}
    for source, log in logs.items():
        if log.get("measurement") == "cumulative_snapshot":
            sources.append(dict(source=source, sample_ids=None))
            inspections.append(dict(source=source, measurement="cumulative_snapshot", native_ids=None,
                                    child_session_ids=log.get("child_session_ids")))
            issues.append(dict(code="cumulative_only", source=source))
            continue
        if log.get("coverage_complete") is False:
            issues.append(dict(code="native_scope_incomplete", source=source, limitations=log.get("limitations", [])))
        if log.get("integrity_issues"):
            issues.append(dict(code="native_hook_integrity", source=source, details=log["integrity_issues"]))
        samples = [c["sample"]["sample_id"] for c in log["calls"]]
        sources.append(dict(source=source, sample_ids=sorted(samples)))
        unassigned = sorted(i for i in log["native_ids"] if (source, i) not in owners)
        if unassigned:
            issues.append(dict(code="unassigned_native_turns", source=source, native_ids=unassigned))
        if log.get("pending"):
            issues.append(dict(code="pending_native_calls", source=source, pending=log["pending"]))
        called = {call["native_id"] for call in log["calls"]}
        for native_id in log["native_ids"]:
            if (source, native_id) in owners and native_id not in called:
                issues.append(dict(code="no_native_calls", source=source, native_id=native_id))
        for call in log["calls"]:
            key = (source, call["sample"]["sample_id"])
            native_samples[key] = call["sample"]
            expected_owners[key] = owners.get((source, call["native_id"]), set())
        inspections.append(dict(source=source, measurement="delta", native_ids=log["native_ids"],
                                sample_ids=sorted(samples), pending=log.get("pending", []),
                                child_session_ids=log.get("child_session_ids")))
    for source in lineage_unknown:
        issues.append(dict(code="native_lineage_unknown", source=source))
    for subject in unbound:
        sources.append(dict(source="unbound:" + subject, sample_ids=None))
    if issues:
        sources.append(dict(source="audit-unresolved:" + inventory["run_id"], sample_ids=None))
    project = dict(version=1, request_paths=sorted(request_paths), sources=sources)
    checked_files = set()
    for path in project["request_paths"]:
        chain = usage.request_chain(path)
        selected = {r["round_id"] for _, r in chain}
        for file, entry in usage.stored_samples(chain):
            if entry["round_id"] not in selected or file.resolve() in checked_files:
                continue
            checked_files.add(file.resolve())
            key = (entry["source"], entry["sample_id"])
            native = native_samples.get(key)
            if native is not None:
                mismatched = [k for k in usage.COUNTERS if native[k] != entry[k]]
                if mismatched:
                    issues.append(dict(code="native_counter_mismatch", source=key[0], sample_id=key[1],
                                       fields=mismatched, recorded_round=entry["round_id"]))
            for owner in sorted(expected_owners.get(key, set())):
                if owner in subjects and subjects[owner]["kind"] == "round":
                    if (entry["job_id"], entry["round_id"]) != (subjects[owner]["job_id"], subjects[owner]["round_id"]):
                        issues.append(dict(code="wrong_round", source=key[0], sample_id=key[1],
                                           expected_round=subjects[owner]["round_id"], recorded_round=entry["round_id"]))
                elif (entry["job_id"], entry["round_id"]) not in ledger_scopes[(owner, key[0])]:
                    issues.append(dict(code="wrong_ledger_scope", source=key[0], sample_id=key[1],
                                       subject_id=owner, recorded_round=entry["round_id"]))
    complete = not unbound and not issues
    if issues and not any(s["source"].startswith("audit-unresolved:") for s in sources):
        sources.append(dict(source="audit-unresolved:" + inventory["run_id"], sample_ids=None))
    summary = usage.summarize_manifest(project) if request_paths else dict(samples=0,
        known_subtotals=dict.fromkeys(usage.COUNTERS, 0), totals=dict.fromkeys(usage.COUNTERS),
        source_coverage_complete=False, counter_coverage_complete=dict.fromkeys(usage.COUNTERS, False))
    if not complete:
        summary["totals"] = dict.fromkeys(usage.COUNTERS)
        summary["counter_coverage_complete"] = dict.fromkeys(usage.COUNTERS, False)
    return dict(run_path=inventory["run_path"], inventory_complete=True, binding_coverage_complete=complete,
                unbound_subjects=unbound, issues=issues, native_lineage_unknown=lineage_unknown,
                subjects=inventory["subjects"], inspections=inspections, project_manifest=project, usage=summary,
                executes_commands=False, note="Bindings use caller-verified receipts; hashes prove retained bytes, not intent. "
                    "Coverage is limited to tracked rounds, explicit controller/ledger bindings and discovered native descendants. "
                    "No whole-project or hidden-host completeness claim. Full native sessions are inventoried; unrelated history "
                    "is not automatically excluded. Re-audit after native/run/ledger changes; this report is not a live guarantee.")
