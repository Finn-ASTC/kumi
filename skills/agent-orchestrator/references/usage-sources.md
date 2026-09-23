# Run actors and native source bindings

Use sibling `scripts/usage.py` as `USAGE_TOOL`. These two commands are read-only:
they do not launch agents, read terminals, import counters or update job/native records.
Use them when a hand-written project source manifest might have omitted the controller,
prepared rounds, historical native sessions or native descendants.

For repeated collection, [register a metering plan and run checkpoints](metering.md).
It retains these bindings together with explicit import mappings, reuses this audit
and stores reports; the two read-only commands below keep their existing behavior.

## Discover candidates

```bash
python3 "$USAGE_TOOL" discover-sources --run "$RUN_FILE"
```

Keep the `run_path` returned by [run preparation](runs.md). Discovery checks its pinned
request registry, round directories and every retained job revision. It lists:

| Subject | Meaning |
|---|---|
| `controller:<run_id>` | Required controller actor, even if no controller usage was recorded. |
| `round:<round_id>` | Every prepared round, including ones never activated/indexed. Preparation alone does not prove submission. |
| `native_candidates` | Historical session/turn IDs and revision paths; changes to the current session do not erase old candidates. |
| `ledger_sources` | Effective source names already recorded for that round. These do not prove ownership. |

`complete=false` and `errors` mean inventory damage or a registration gap. Reconcile the
actual saved records using the run recipe; do not hide the gap by narrowing the manifest.
`audit-sources` refuses an incomplete inventory. Discovery does not scan native storage;
choose exact log/database paths from retained submission/native receipts. Never infer a
binding from cwd, timestamps, titles, model names or the newest session file.

## Bind explicitly, then audit

Inspect each exact native session with [`inspect-native`](efficiency.md). Verify the
submission receipt against the run round and native turn/user-message ID. Retain that
evidence in a regular file and obtain its SHA-256 (`sha256sum "$RECEIPT_FILE"`).
Create a UTF-8 JSON manifest using actual absolute paths and IDs:

```json
{
  "version": 1,
  "run_path": "/absolute/run/run.json",
  "bindings": [
    {
      "subject_id": "round:actual-round-id",
      "host": "opencode",
      "session_id": "actual-worker-session",
      "log_path": "/absolute/native.db",
      "native_ids": ["actual-worker-user-message-id"],
      "evidence": {"path": "/absolute/worker-receipt.json", "sha256": "actual-64-hex-digest"}
    },
    {
      "subject_id": "controller:actual-run-id",
      "host": "opencode",
      "session_id": "actual-controller-session",
      "log_path": "/absolute/native.db",
      "native_ids": ["actual-controller-user-message-id"],
      "ledger_request_paths": ["/absolute/controller-audit-round/request.json"],
      "evidence": {"path": "/absolute/controller-receipt.json", "sha256": "actual-64-hex-digest"}
    }
  ]
}
```

```bash
python3 "$USAGE_TOOL" audit-sources --manifest "$BINDINGS_FILE"
```

The host is `codex`, `omp`, `opencode` or `hermes`. `native_ids` are explicit turn IDs
for Codex, user-message IDs for omp/OpenCode, and explicit turn IDs for the optional
[Hermes hook store](hermes-hooks.md). Hermes cumulative `state.db` requires **null**;
no round delta is inferred from it. One subject may bind several native
sessions, but each subject/source pair appears once. A shared session's native turn
must have only one subject owner. Use a single log path for each source in one audit.

Round bindings use their registered request only. For controller or native-descendant
subjects, `ledger_request_paths` explicitly selects the audit ledger request chains
where their usage belongs; predecessor rounds are included. The audit does not create
these contracts. Retain separate ledger contracts if needed; their existence does not
claim that an agent was submitted. Matching IDs in another actor's ledger cannot stand
in for that declared scope. Bindings without recorded calls cannot establish totals.

If an actor has no ledger contract, write a small UTF-8 task file identifying this run,
actor and the evidence being accounted for. Prepare a standalone audit contract in an
existing absolute directory dedicated to these ledgers:

```bash
python3 "$ORCH_TOOL" prepare --root "$AUDIT_LEDGER_ROOT" --cwd "$PROJECT_CWD" \
  --parent-depth 0 --task-file "$AUDIT_TASK_FILE" --brief
```

Here `ORCH_TOOL` is sibling `scripts/protocol.py`. Retain the returned `request_path`
for that actor's `ledger_request_paths` and native import mappings. Do not register or
submit this accounting-only contract. Use a separate contract for each actor; preparation
neither collects telemetry nor establishes ownership without the retained native evidence.

The evidence hash verifies retained bytes (maximum 16 MiB), not the receipt's meaning.
The caller must check that the receipt actually establishes the claimed ownership.
Evidence text, launch arguments, lease tokens and conversations are not emitted.

## Descendants and session boundaries

For OpenCode/native Hermes SQLite, the audit follows direct child IDs of explicitly bound run
actors. An unbound child appears as `native:<host>:<session_id>` in `unbound_subjects`.
Bind it either to its actual run round or to that native subject with an explicit
ledger scope. Once bound, its children are inspected too. Arbitrary unrelated native
subjects are rejected; this is not a way to adopt sessions by a guessed relationship.
Cycles or disagreeing parent identities remain audit issues.

Codex/omp JSONL, OpenCode export and the Hermes hook store do not establish a complete child inventory.
`native_lineage_unknown` remains a gap even when all visible calls are imported.
Hermes session/model cumulative views are not added to descendant counts or round ledgers.

Each inspected **full native session** is inventoried. Unselected historical turns in
a shared session appear as `unassigned_native_turns`; they are not silently excluded or
assigned to this run. This version has no verified sub-session exclusion mechanism.
Prepared but unsent rounds also stay visible; do not fabricate bindings to clear them.
Pending calls and selected turns without completed calls remain unknown, never zero.

## Read and resolve the report

The report contains the discovered subjects, inspected native IDs/sample IDs,
`unbound_subjects`, `issues`, a generated `project_manifest` and the effective `usage`.

- `inventory_complete`: tracked run records were readable and consistent.
- `binding_coverage_complete`: discovered actors/candidates are bound with no ownership,
  lineage or comparison issues. It does not by itself mean calls have been imported.
- `usage.source_coverage_complete`: the generated expected-call inventory matches the
  selected ledgers. An ordinary hand-written `project-summary` checks only its declared
  inventory and can miss omitted actors.
- `usage.counter_coverage_complete` / `usage.totals`: counters must also be available,
  rounds metered and all audit gaps resolved. `known_subtotals` are recorded observations,
  not certified totals; mismatches may make them unreliable until reconciled.

`wrong_round` and `wrong_ledger_scope` locate attribution problems. `native_counter_mismatch`
compares the native normalized counters with the effective ledger version, including
unknown versus known values. `conflicting_turn_owners`, `unbound_native_candidate`,
`unassigned_native_turns`, `pending_native_calls`, `no_native_calls`, `cumulative_only`,
`native_scope_incomplete` (Hermes observed hooks), `native_hook_integrity`
and lineage issues all prevent a complete total.

Use the separate explicit [native import manifest](efficiency.md) for missing completed
calls (`import-native --dry-run`, then `import-native`). For changed existing samples,
review [`history` and evidence-backed corrections](usage-corrections.md); do not overwrite
records or change sample IDs to bypass a conflict. Cross-job transfers and source aliases
are not supported. Re-run this audit after import/correction and whenever native/run/ledger
state changes. Separate sessions are read separately; this is not a global live snapshot.

Exit 0 means the command produced a report, including incomplete coverage. Check its
fields before making claims. Coverage is limited to tracked rounds, explicit controller
bindings and discovered descendants; it does not prove all plugin/background/hidden host
calls, every project session or provider billing. A bound controller in one run does not
prove that no other controller sessions participated.
