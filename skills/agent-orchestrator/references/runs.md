# Persistent runs and cross-watch recovery

Use sibling `scripts/runs.py` as `RUNS_TOOL`. A run retains related jobs, rounds,
watches, reviews and usage evidence. This tool never starts agents, sends input,
grants approval or cleans resources.

For usage completeness, [discover and audit source bindings](usage-sources.md) against
this run. Recovery alone does not bind controller/native sources or meter their calls.
At startup, retain a [metering plan](metering.md), even if initially empty; update its
explicit mappings and collect checkpoints at phase boundaries and handoff. Keep the
latest selected `plan_path` with the run handle. Registration is not collection.

Each indexed job now includes a `completion` summary: response publication, independent
acceptance, host settling, freshness and missing conditions. See [completion](completion.md)
for recording evidence. Historical closure is separate from the current host observation;
recovery never probes the live host or treats an expired observation as ready.
Run-level `complete` describes recovery completeness, not completion of the jobs;
check each job's `completion.ready_to_complete` and reasons separately.

## Create and retain the run handle

An initial `protocol.py prepare` without storage flags creates a private run under
`$XDG_STATE_HOME/agent-orchestrator/runs/`, falling back to
`~/.local/state/agent-orchestrator/runs/` when XDG_STATE_HOME is absent or relative.
It returns absolute `run_path` (run.json), `run_id` and `index_path` alongside the
request/prompt/result paths. Keep these handles in the handoff.

For multiple children, initialize one shared run first:

```bash
python3 "$RUNS_TOOL" init
python3 "$ORCH_TOOL" prepare --run "$RUN_FILE" --cwd "$PROJECT_CWD" \
  --parent-depth 0 --task-file "$TASK_FILE" --brief
```

Take `RUN_FILE` and `INDEX` from the returned `run_path` and `index_path`. Repeat
prepare with this run for sibling jobs. Each child retains independent job/round IDs.
Register each unsent initial request with `jobs.py register --index "$INDEX"` and
follow the [submission recipe](submission.md). The managed job must use its run index.

`prepare --previous` inherits the run. Preparation creates a candidate; `activate`
selects the active successor. Storage contains `rounds/`, `watches/`, `jobs/`, and
immutable request/watch registries. Reviews stay with their watch; usage stays under
the initial job round, within this same tree. Do not move these absolute-path records.

| Option | Behavior |
|---|---|
| Initial prepare without storage flags | New persistent run; no implicit reuse by cwd. |
| `prepare --run RUN_FILE` | Join an explicit run; follow-ups cannot switch runs. |
| `runs.py init --root PARENT` | Create a private run under an existing custom parent, e.g. a sandbox evidence directory. |
| `prepare --temporary` / `runs.py init --temporary` | Explicit short-lived run in system temp; its follow-ups/watch inherit it. |
| `prepare --root DIRECTORY` | Preserve legacy standalone placement; an existing managed rounds root retains its binding. |

Legacy unbound follow-ups retain their previous round parent unless `--root` is
explicit. They are not silently adopted into a run. Existing records are not migrated
or rewritten. Codex workspace-write targets still need the supported additional
writable-directory option for this run's exact rounds root, as its dictionary explains.

## Register replacement watches

```bash
python3 "$WATCH_TOOL" init --request "$REQUEST_A" --request "$REQUEST_B"
```

Same-run requests infer their run, store the watch under its watches root, and register
its immutable manifest. The reply includes `watch_path`, `watch_id` and `run_path`.
Use `--run "$RUN_FILE"` to assert membership explicitly.

When rounds change, stop the old managed observer, retain its evidence/reviews,
activate the successor, then initialize a new watch for current requests. Update
current job monitor bindings. The run retains both old and new watches even when
activation clears the old job-level monitor binding.

Explicit `watch init --root DIRECTORY` overrides placement; same-run requests still
register the watch with that run. Mixed-run inventories require explicit standalone
placement and have no automatic run attachment. Legacy unbound watches default to
the persistent `agent-orchestrator/watches/` directory; their `--temporary` is explicit.

If watch files were published but attachment failed, preserve them and retry:

```bash
python3 "$RUNS_TOOL" attach-watch --run "$RUN_FILE" --watch "$WATCH_FILE"
```

Only this run's pinned requests are accepted. Exact retries are idempotent; foreign
targets, changed manifests and conflicting IDs fail. Recovery reports unregistered
artifacts inside managed storage. For interrupted round attachment, reconcile the
saved request and `run-ref.json`, then retry `runs.py attach-request --run "$RUN_FILE"
--request "$REQUEST_FILE"`. It only accepts this run's round storage and records
membership; it does not infer acceptance or alter submission history. Do not resubmit.

## Recover current work and old questions

```bash
python3 "$RUNS_TOOL" list
python3 "$RUNS_TOOL" recover --run "$RUN_FILE" --limit 20
```

For custom parents use `list --root "$PARENT"`. Full/summary recovery and list create no directories and
perform no terminal reads. Recovery includes jobs, tracked rounds, all retained watches,
open actions, `waiting_user`, and health/errors. Closed jobs can still have unresolved
historical reviews.

Queue items include `event_ref=watch_id:seq`, exact `watch_path`, local `seq`, job/round,
review revision/note and `is_current_round` (null for unindexed jobs). Two watches can
both contain seq 2. Use the returned **watch_path + seq together** with `watch.py review`
or `review-status`; neither sequence nor message text is a global event identity.

Read existing `waiting_user` notes before asking again. Reconcile a new observation
with related earlier questions; do not copy an old handled/approved decision to it.
Recovery adds advisory `related_reviews` for matching exact job/round/resources and
dialog content, including handled receipts from older watches. It preserves each
watch's independent event/review identity. `related_review_count` counts matches;
the three latest receipts are shown, with paths to read their full histories.
A changed command, changed resource or successor round does not inherit this key.
Local low-confidence reminder groups are compacted as described in
[supervision](supervision.md). Check live target identity/UI and existing authorization
before acting; matching a key never authorizes input or suppresses a new dialog.

The two queues have independent `--limit` pages: pass `next_action_offset` through
`--action-offset`, and `next_waiting_offset` through `--waiting-offset`, while the
corresponding `more_*` is true. Reset offsets after event/review changes. `--job JOB_ID`
filters items; run-wide integrity errors stay visible. Counts cover all matching valid
items, and advancing delivery cursors never clears pending reviews.

`complete=false` means some manifest/storage/review/health information failed validation
or could not be read. Inspect `errors`; counts then cover only successfully read items.
Valid reviews remain visible even if their watch state.json is missing. A partial or
empty response is not proof that nothing requires attention.

Exit 0 means the recovery command ran, including a partial response with errors.
`complete=true` means the retained records were readable/consistent, not that
observers are healthy or targets finished. An observation failure recorded correctly
can coexist with `complete=true`; inspect health fields separately.

## Compact recovery for routine inspection

```bash
python3 "$RUNS_TOOL" recover --run "$RUN_FILE" --summary --limit 10
```

`--summary` is opt-in. Without it, recovery keeps its full jobs/rounds/watches output
and only pages the two pending queues. Summary still reads the entire recovery;
it reduces displayed rows, not disk scanning, and is not a delta query.
For repeated inspections, [incremental recovery](recovery-delta.md) provides
`recover --delta`, with a source-content cache and durable job/watch change cursor.
That separate opt-in mode writes private cursor state; its pending queues always repeat.

Summary retains `run_id`, `run_path`, `index_path`, `complete`, and the exact pending
review payloads (including notes, revisions, related reviews and evidence handles).
It omits round rows and detailed job completion records. Each watch row contains
diagnostic counts instead of its target array. All five displayed lists are
independently limited by `--limit` (1–200):

| List | More flag | Next offset | CLI option |
|---|---|---|---|
| `jobs` | `more_jobs` | `next_job_offset` | `--job-offset` |
| `watches` | `more_watches` | `next_watch_offset` | `--watch-offset` |
| `errors` | `more_errors` | `next_error_offset` | `--error-offset` |
| `action_required` | `more_action_required` | `next_action_offset` | `--action-offset` |
| `waiting_user` | `more_waiting_user` | `next_waiting_offset` | `--waiting-offset` |

Keep the same run/filter/limit while paging; pass each returned next offset only
when its more flag is true. Nonzero job/watch/error offsets require `--summary`.
Start all offsets at zero on each inspection cycle and after any record change.
These are reads of changing storage, not a frozen snapshot or durable cursors.
Long notes/errors retain their content: row bounds are not a strict byte/token cap.

`counts` includes all matching readable jobs/rounds/watches, unindexed rounds,
pending totals and all run-wide errors, even beyond the displayed pages.
`job_counts` includes unreadable/closed jobs, submissions, results and expired or
unknown monitor bindings. `health` aggregates all selected retained watches,
including historical watches and repeated targets: inactive observer locks,
unreadable health files, read errors, unknown/dead/unrecognized states, and
missing/stale/future success timestamps. Age uses `checked_at` at recovery start;
`success_stale` means at least `success_max_age_seconds` (30 seconds) old.
Time-based diagnostics recompute on every call even without file changes.

`--job` scopes these records/counts while retaining run-wide integrity errors.
When `complete=false`, totals only describe records recovered successfully;
unreadable targets are not counted as healthy or inferred from a missing array.
An empty error page does not override `counts.errors` or `complete=false`.
Even `complete=true` with zero diagnostic counters is **not a supervision gate**:
summary does not validate monitor ownership, actual observer lifetime or current
coverage. Use the [supervision checkpoint](supervision.md) before independent work.

For round/target/completion detail, use full recovery scoped to the job, then read
the exact watch occurrence before acting:

```bash
python3 "$RUNS_TOOL" recover --run "$RUN_FILE" --job "$JOB_ID" --limit 10
python3 "$WATCH_TOOL" review-status --watch "$WATCH_FILE" --seq "$SEQ"
```

## Separate monitoring health from recovered records

`observer_active` is a read-only snapshot of the watch's existing lock. A sweep or a
bounded observer may hold it; it does not prove a model is awake or making progress.
Per target, recovery reports `last_checked_at` for attempted sweeps (including errors),
`last_successful_check_at` for successful contract/resource and terminal observation,
and `last_error`, plus `read_started_at` and `read_duration_seconds` for each completed read. Result validity is separate. Legacy state without a success timestamp
remains unknown until a successful new sweep. New observations use each target's read-completion time; durations use a monotonic
clock. Legacy timestamps retain their historical sweep-start meaning until observed
again. Event files and target health are saved as reads complete, so a slow target
need not conceal the fast target's event until the entire sweep returns. Read timing
is not native dialog onset, controller wake-up time or approval-response latency.

Renew the managed observer or resume manual supervision when needed, handling events
between independent work blocks. Files alone cannot wake an ended controller turn.
After reconciliation/verification and native host settling, use the existing ownership
cleanup plan. These run commands have no resource deletion or retention policy.
