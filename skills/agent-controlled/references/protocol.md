# Response protocol — schema version 1

This is the maintained wire contract. Historical September 13 reports describe the earlier unversioned protocol. Main topics: identity, fields, atomic publication, follow-ups, depth and migration.

## Identity and locations

One **job** is the controller's continuing conversation with one target for an objective. One **round** is one submitted task, clarification answer or revision. Retrying delivery is not automatically a new round; determine whether the original submission landed first.

`prepare` creates a private, unpredictable directory (mode 0700) containing:

| File | Written by | Purpose |
|---|---|---|
| `request.json` | Controller helper | Immutable task envelope, IDs, cwd, depth, path and preparation time. |
| `prompt.txt` | Controller helper | Complete single-line contract plus JSON-encoded request. |
| `resources.json` | Controller helper `record` | Job-bound resource IDs and ownership, after startup. |
| `submission.json` | Controller | Actual submission time, acceptance/error, deadline and monitoring owner. |
| `result.json` | Target | One immutable response to this round. |

The request's `result_path` is absolute; declared artifact paths are relative to its `cwd`. These are different rules. A process may run in a different shell directory, but must execute and report relative to the request's project cwd.

Helper file reads require regular files, reject final-component symlinks and cap each document at 16 MiB, including serialized receipts/evidence. Keep requests and responses concise; reference large logs and artifacts by path. FIFO/directory/oversized inputs are explicit errors. These file checks do not provide an OS sandbox or a hard filesystem timeout. Watch request fingerprints, legacy-watch replacement and independent file/terminal errors are described in [supervision](../../agent-orchestrator/references/supervision.md#file-boundaries-and-request-identity).

Use the current prompt as the authority. Child processes need not inherit any `AGENT_*` variables. An old `AGENT_RESULT_FILE` must not route a new response into an earlier file. Do not move an active round directory: absolute paths are part of the contract.

Request fields are `schema_version` (integer 1), `job_id` and `round_id` (32 lowercase UUID hex digits), `cwd`, `result_path`, `depth`, `max_depth`, `prepared_at`, and `task`. A `previous_request` path, when present, links a follow-up to its predecessor. Only a submission receipt establishes whether the transport accepted a request. The helper does not run a scheduler or watchdog.

Resource records include the job ID. `record`, follow-up preparation and cleanup all validate the same ownership/identity rules: exact herdr pane IDs such as `w1:p2` (workspace `w1`) or tmux IDs such as `%7`. Do not use current/focused aliases. A supplied workspace must match the pane's workspace. This avoids inconsistent commands but does not establish live ownership without checking the actual session.

Herdr resource records also support `tab` (`w1:t2`), `owns_tab`, `parent_pane` and `parent_tab`. Insider mode defaults to a newly created workspace in the same session; claiming that workspace requires a `parent_pane` from a different workspace. For a newly created tab, supply its workspace/tab and both parent IDs; the child tab must differ from the parent tab. Insider mode never owns the session. For an explicitly requested split, own only the new pane. Tab and pane IDs must share a workspace prefix; actual membership and creation are verified against the live server. Cleanup chooses the owned workspace, otherwise tab, otherwise pane, after checking for other live resources inside it.

Tmux resource records additionally carry `tmux_selector`: `[]` explicitly selects the default server, `["-S", "/absolute/socket"]` selects a socket path, and `["-L", "name"]` selects a named server. New records require a selector. Follow-ups and cleanup preserve it. Legacy records without it can be inspected, but cleanup fails until the controller recovers and records the verified selector; an absent selector never authorizes use of the default server.

These resource fields are an additive extension; request/result schema version remains 1. Older pane-only records without tab/parent fields remain readable, and absent `owns_tab` means false. Follow-ups preserve the recorded ownership and parent context. Controller receipts separately record supervision timestamps and pending UI questions; a native approval is not a new protocol round.

## Response fields

All fields in this example are required, including empty arrays and null reasons. The IDs below are illustrative; copy the actual request values.

```json
{
  "schema_version": 1,
  "job_id": "11111111111111111111111111111111",
  "round_id": "22222222222222222222222222222222",
  "status": "success",
  "output": "Implemented the change; the relevant checks passed.",
  "files_created": ["src/new.py"],
  "files_modified": ["tests/test_existing.py"],
  "files_generated": [],
  "files_deleted": [],
  "error": null,
  "blocked_reason": null,
  "completed_at": "2026-09-17T12:00:00Z"
}
```

| Field | Requirement |
|---|---|
| `schema_version`, `job_id`, `round_id` | Exact type and value match to the current request; mismatches are not responses to this round. |
| `status` | Exactly `success`, `error`, or `blocked`. |
| `output` | String containing answer, partial progress and relevant verification evidence. Avoid claiming tests that were not run. |
| Four `files_*` fields | Arrays of file-path strings, globally disjoint; empty is valid. |
| `error` | Nonempty string for `error`; null otherwise. |
| `blocked_reason` | Nonempty exact question for `blocked`; null otherwise. |
| `completed_at` | ISO8601 string with explicit timezone; diagnostic time, not the identity/freshness check. |

Optional additional metadata is allowed (for example `options`, `checks`, `artifacts`). It cannot override required fields. The validator checks JSON encodability but does not interpret the meaning of these extensions. JSON must be a single UTF-8 object with no Markdown fence, duplicate keys, NaN, Infinity, overflowed floating-point values or unpaired Unicode surrogates.

`blocked` resolves this round but does not finish the job. The target stops and returns to input. The controller answers in a new round. `success` and `error` also resolve a round; a later revision or recovery uses a new round.

## Net file changes, measured from the start of each round

| Situation | Declaration |
|---|---|
| New source/document retained at end | `files_created`. |
| Existing source/document changed | `files_modified`. |
| Relevant generated file retained and created/changed this round | `files_generated` only, even when committed (e.g. lockfiles or generated source). |
| Preexisting file removed, including a generated file | `files_deleted`. |
| File created and then removed in this round | No net file entry; mention consequential temporary effects in `output` if relevant. |
| Existing file removed and recreated with changes | `files_modified`, or `files_generated` if generated. |
| Rename | Old path in `files_deleted`; new path in created/generated. |
| File created last round, edited this round | `files_modified` (or generated), not created. |
| Child agent produces files | Include the delegated net effects in the parent's round report; parent verifies them and avoids duplicates. |
| No edits: answer/review/checks/no-change result | All arrays may be empty; put the actual answer in `output`. |

Exclude transient artifacts such as `__pycache__/`, `*.pyc`, `target/`, `node_modules/` and build caches from normal generated-file reporting. Relevant deliverables such as a requested binary, image or archive should still be reported, even if normally ignored by Git. Do not list whole directories or claim unmodified/preexisting caches as newly generated work.

Paths use `/`, must be normalized, and stay within cwd: no absolute paths, `.` components, `..`, trailing slash, duplicate separator, or symlinks resolving outside cwd. Directory removals are represented by the removed preexisting files. External deliverables need a suitable agreed cwd or explicit metadata and separate validation; do not hide them in an invalid relative path.

The validator checks shape, identity, path containment, and optionally existence/deletions. Basic reporting/readback and cleanup do not require cwd to still exist, so a target can report that the project disappeared. `--check-files` and preparation of new work require an existing cwd. The controller's baseline/diff determines whether a path was actually created/modified/deleted by this round. A symlink to an internal file can pass path checks; an adversarial process can still race filesystem checks. This is a cooperation protocol, not a security sandbox.

## Publishing without a partial or overwritten response

When this repository's helper is accessible, create a candidate JSON file using normal file-editing tools and run:

```bash
python3 "$ORCH_TOOL" write-result --request "$REQUEST_FILE" --input "$CANDIDATE_FILE"
```

The helper validates the response, writes, flushes and fsyncs a temporary file in the result directory, then atomically links it to `result.json`. It refuses to overwrite **any** existing destination. Only the published final name counts as a response; a temporary file never does. Requires a local filesystem supporting hard links; I/O failures are reported, not converted into success.

Without the helper, write UTF-8 JSON to a unique temporary file in the same directory, flush/close it, then atomically publish it. An atomic rename is acceptable with one designated writer after confirming no valid response exists. Never use shell `echo` with unescaped model output; use a JSON serializer. Do not change a valid `blocked` response into `success` at the same path.

If a response is malformed, the controller first preserves the raw file under a unique diagnostic name, waits until the target is not writing, then removes the malformed final name and asks for a **report-only** repair to the same request identity. Retry this at most twice. The repair must not rerun task side effects. Never archive/remove a valid response to reuse its path.

## Correcting a published report

Before publication, check the candidate report against its evidence. If you discover
a factual error only after publishing a **valid** response, keep the original bytes
and send a concise correction notice in normal output: identify the affected round,
the incorrect claim and the corrected fact with its evidence. Then return to input
and stop. Even a one-character hash typo does not permit replacing the response.

The controller retains the original report and notice, evaluates the impact, and
supplies a fresh round/path when a corrected structured report is needed. Do not
invent that round or rerun the original task. Report-only corrections declare only
their own net file changes; unchanged artifacts are not newly created files. A chat
notice is not a replacement result or acceptance. Actual artifact fixes require an
explicit task and new delivery evidence through the normal follow-up workflow.

Malformed-response repair above is a separate exception. A wrong factual claim in
otherwise valid JSON does not qualify for overwriting a valid response; preserve
any `result_changed` incident if a rewrite already occurred.

## Follow-up and freshness

`prepare --previous /absolute/old/request.json --task-file ...` requires the old result to be protocol-valid and preserves job/cwd/depth. Each new response path starts absent and has a new `round_id`. This prevents an old success or blocked response from satisfying the new round.

Existing resource records are checked before allocating a follow-up directory. If later preparation fails, the helper rolls back only its newly allocated private directory; earlier requests/results remain intact. The active request should change only after preparation succeeds.

It is still necessary to wait for the target's input readiness. A fast publish can precede a transport state transition. Consuming a result once does not authorize sending another prompt into a working TUI. One controller owns submission decisions for a target; the helper does not provide a distributed lock or exactly-once delivery.

## Depth convention

`max_depth` counts levels including the root. Root A is 0. With the default 3, B is 1 and C is 2; only depths less than 3 are allowed. Each child request receives `parent_depth + 1`. The caller supplies its received depth explicitly. Following up with B or launching a sibling of B does not increase B's depth.

The deepest permitted agent executes locally. A helper request to exceed the limit fails **before** any target/session is started. Do not reset the value, silently raise the maximum, or loop over the same unresolvable blocked question. The original controller may choose a different maximum when the user task calls for it; nested agents inherit that choice.

## Legacy compatibility

Older reports/agents used unversioned JSON and lacked `files_deleted` and identity fields. The new validator intentionally rejects those responses for a versioned round; missing identity cannot prove freshness.

- New controller → older target: the generated prompt carries the complete new contract, even without installed skills. If the target omits fields, request report repair; do not invent IDs on its behalf.
- Older controller → new target: if only a legacy contract or `AGENT_CONTROLLED=1` plus a nonempty `AGENT_RESULT_FILE` is supplied, preserve the requested unversioned format. Use its concrete path; never invent a versioned envelope. The older controller's freshness limitations remain.
- Never use one path concurrently for legacy and versioned jobs. If upgrading a live job, finish/stop its old round first, retain the old evidence and begin an explicitly versioned request.
