# Run metering plans and checkpoints

Use sibling `scripts/usage.py` as `USAGE_TOOL`. Register a plan at run startup,
extend it when exact native identities become available, then collect at explicit
phase boundaries and before handoff. This connects the existing native importer
and source audit; it does not launch agents, approve operations or run a background
collector. Do not put native evidence, plans or reports in the public project.

## Start with visible gaps

Create a JSON file with the exact `run_path` returned by run initialization:

```json
{
  "version": 1,
  "run_path": "/absolute/run/run.json",
  "bindings": [],
  "imports": []
}
```

```bash
python3 "$USAGE_TOOL" register-metering --manifest "$METERING_FILE"
python3 "$USAGE_TOOL" checkpoint --plan "$PLAN_FILE" --label startup
```

Keep the returned `plan_path` as `PLAN_FILE`. An empty plan reports the unbound
controller and all prepared rounds; it does not claim zero cost. Registration
validates the run, binding evidence and declared import ownership, but does not
open native logs or collect counters. It cannot establish that a future native
turn exists or that the evidence's meaning is correct.

Plans are immutable under the run's `metering/plans/`, identified by their complete
JSON contents. Re-registering identical contents returns the same path. Extend the
input file and register again to get a new path; retain old paths/reports as history
and use the new path for subsequent checkpoints. There is no implicit latest-plan
selection. Never edit the retained plan in place.

## Add exact bindings and import mappings

`bindings` uses the existing [source binding schema](usage-sources.md): explicit
actor, host/session, native IDs, exact source path and a retained evidence hash.
Include the controller, each run round and discovered native descendants. For
controllers/descendants, prepare separate accounting-only contracts as described
there and retain their `ledger_request_paths`; do not submit these contracts.

`imports` embeds the existing [native import manifests](efficiency.md#inspect-and-import-native-usage).
For example, one entry is:

```json
{
  "version": 1,
  "host": "opencode",
  "session_id": "actual-worker-session",
  "log_path": "/absolute/native.db",
  "mappings": [
    {
      "native_id": "actual-user-message-id",
      "request_path": "/absolute/run/rounds/actual-round/request.json",
      "attempt_id": "initial",
      "purpose": "task",
      "role": "author",
      "phase": "implementation"
    }
  ]
}
```

Every import must match a binding's host, session, source path, native ID and ledger
scope. Role and phase fields are required here, but may be `null` when genuinely
unknown. Use explicit roles such as `controller`, `author`, `verifier` and phases
such as `planning`, `implementation`, `verification`, `approval`, `handoff` when
supported by evidence. These are attribution labels, not automatically inferred
from task text or the checkpoint name. Task retries use a distinct attempt ID and
`purpose: "retry"`; retain previous attempts.

Bind native child sessions after confirming the parent's native relationship;
unrelated `native:*` subjects are refused at checkpoint preflight. For Hermes
cumulative stores, retain a binding with `native_ids: null` but no additive import.
Keep [frozen observations](hermes-windows.md) separately: a cumulative snapshot is
not a round delta. A binding may intentionally have no import yet; missing calls
remain visible. Do not fabricate mappings to clear coverage warnings.

## Collect, inspect, resume

```bash
python3 "$USAGE_TOOL" checkpoint --plan "$PLAN_FILE" --label before-verification --dry-run
python3 "$USAGE_TOOL" checkpoint --plan "$PLAN_FILE" --label before-verification
```

Dry run writes nothing and reports **current**, not hypothetical post-import,
coverage. Real collection audits ownership first, imports explicitly selected
turns through the existing idempotent importer, then audits again. It prints a
compact summary and retains the complete audit/import receipts at `checkpoint_path`.
Each checkpoint is a cumulative observation: do not add checkpoint totals together.

- `collection_complete` means no preflight/import/audit error, not complete source
  or counter coverage. Exit 0 means a report was produced, including failed or
  incomplete collection. Check the fields before accepting a checkpoint.
- `unbound_subjects`, `issue_counts`, `source_gaps` and `rounds_without_usage` expose
  missing actors, new native turns, pending calls and ledger gaps. The retained audit
  includes exact native IDs. Known subtotals and groups cover recorded calls only;
  project totals remain null while coverage is incomplete.
- `non_cached_input` subtracts cached input per call only when both counters are
  known. It reports known subtotal, observed/unknown sample counts and a nullable
  total; subtracting two unrelated incomplete aggregate subtotals is not valid.
- A pending selected source fails its import; other valid imports can proceed.
  Unreadable evidence/source or invalid ownership at preflight blocks all collection.
  No stale pre-import audit is returned as final state if the final audit fails.
- Imports commit per call. An interrupted/failed batch may have already written
  samples; rerun the same plan after repair. `import_counts_complete: false` means
  the printed imported/duplicate counts omit a failed batch's partial progress;
  use the final ledger audit to inspect recorded state. No batch rollback is claimed.
- Conflicting counters/attribution are not overwritten. Use the existing
  [correction process](usage-corrections.md) and re-audit before continuing.

Use checkpoints at startup, after settled work/verification batches, before and
after handoff, and at final reporting. This reads complete selected source histories
and audits twice; do not attach it to a fast terminal polling loop. Sources can
advance between reads, so this is not a global atomic snapshot or provider bill.
New controller sessions, turns and mappings still need explicit registration;
unknown JSONL lineage and hidden host/plugin calls remain coverage limits.
