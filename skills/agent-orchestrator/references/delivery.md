# Fixed source handoffs and verification evidence

Use sibling `scripts/delivery.py` as `DELIVERY_TOOL` for file-producing work whose
verification must remain tied to a specific version. Python 3.10+ on local Unix is
required. Git is optional. This tool does not launch agents or approve native dialogs;
`verify` **does execute** the explicit local commands in your verification plan.

## Capture the agreed source scope

Before editing, prepare the round, identify all writers and take an initial scoped
snapshot. At delivery, have the writers yield that scope during capture. After a
successful capture they may resume editing the original checkout; verify the copy.
Record the real handoff in the note. A note is the caller's statement, not a lock.

```bash
python3 "$DELIVERY_TOOL" capture --request "$REQUEST_FILE" \
  --include . --exclude target --exclude .venv \
  --handoff 'Initial baseline; assigned writers have not started'
```

Retain `snapshot_path` as `BASELINE`. For a delivery whose changes/declarations will
be reviewed, `--baseline` is required by this workflow. After the child delivers,
use the same scope:

```bash
python3 "$DELIVERY_TOOL" capture --request "$REQUEST_FILE" \
  --include . --exclude target --exclude .venv --baseline "$BASELINE" \
  --handoff 'Assigned writers yielded this scope for the delivery copy'
```

Retain the returned `snapshot_path` as `SNAPSHOT_FILE`. The default storage is a
fresh `snapshot-*` directory under the request's `deliveries/`; `--root PARENT`
chooses an external parent. Storage must be outside the entire live cwd. When legacy
round storage lies inside the project, supply an external root. Nothing is appended
to an old snapshot. Failed captures retain `failure.json` and partial files without
a usable sealed snapshot; retry into a new directory after reconciling the writer.

`--include` and `--exclude` may repeat. They name exact normalized relative files or
subtrees, not globs. `.` means the whole tree. `.git` entries are always omitted;
other ignored/untracked files are included unless explicitly excluded. Include
lockfiles, fixtures, shared modules and build scripts that verification actually uses.
Directories, regular file hashes/sizes/executable bits and safe internal relative
symlinks are captured. Absolute, dangling or escaping links and special files fail;
link targets must also be present in the captured scope. Files are copied, never
hardlinked. Snapshot regular files become read-only; verification gets writable copies.

Capture compares file content and stat identity before/after copying, then compares
the saved tree. Detected changes reject the capture. This is a consistency check for
cooperating writers, not an atomic filesystem snapshot or protection against hostile
writers; arrange a real handoff. Mode/mtime/ctime checks detect ordinary edits that
restore previous bytes, but do not establish OS-enforced exclusivity.

The manifest records job/round, request digest, scope, handoff, per-entry hashes and
optional result digest/`declarations`. With `--baseline`, `actual_diff` records scoped
added/deleted/modified paths. Baseline and delivery must have the same source root
and scope. With both baseline and a valid response, `declaration_comparison` reports undeclared changes,
wrong declaration categories, declarations without a scoped change, and declarations
outside the snapshot scope. It does not infer the writer or prove attribution;
each file field maps to these actual diff categories:

| Declaration | Allowed category |
|---|---|
| `files_created` | `added` only |
| `files_modified` | `modified` only |
| `files_deleted` | `deleted` only |
| `files_generated` | `added` or `modified` |

Read **all four** lists. A newly generated lockfile may correctly appear only in
`files_generated`; absence from `files_created` does not make it undeclared.

`capture` returns `warnings` and seals the same list in the manifest. Each entry
has a stable `code` and an explanatory `message`:

| Code | Evidence unavailable |
|---|---|
| `no_baseline` | `actual_diff` and `declaration_comparison` are null |
| `no_result` | `declarations` and `declaration_comparison` are null |

Both warnings are expected for an initial snapshot before any response. Capture
still succeeds for that use and for explicitly scoped existence/hash preservation;
exit 0 means the copy was captured, not that changes or declarations were checked.
At delivery, reconcile each warning before claiming those dimensions were verified.
Missing a pre-work baseline cannot be repaired by labeling a later snapshot as the
original baseline. Preserve the gap and have the controller decide the limited
acceptance possible from the available evidence. Do not infer an empty diff from
null. An empty warnings list does not mean there are no declaration discrepancies;
inspect the comparison and judge the requested outcome separately.

Legacy manifests may omit warnings. Inspect their baseline/result/comparison fields;
an absent warning list is not evidence of complete coverage. Do not rewrite their seals.

For multi-module integration, capture the agreed **integrated** source tree and its
fixtures after integration. Passing checks on individual component snapshots does
not establish an integrated pass.

## Verify in a fresh attempt

First define the delivered target, configuration coverage, gate owners and exclusions
using [delivery configuration coverage](delivery-configurations.md). Development
tests alone do not accept an optimized, packaged or differently configured delivery.
The execution plan below runs declared checks; it does not discover omitted gates.

Write a UTF-8 JSON plan with these four required fields. Each command has an argv
array and a timeout in seconds (greater than 0, at most 3600). Replace versions and
commands with the actual project's checks. Dependency versions are declarations;
use null for unknown and include lockfiles in source scope. Version-query commands
can be part of the plan to retain their real output.

```json
{
  "commands": [
    {"argv": ["cc", "--version"], "timeout_seconds": 10},
    {"argv": ["cc", "main.c", "-o", "{build}/app"], "timeout_seconds": 60},
    {"argv": ["{build}/app", "--self-test"], "timeout_seconds": 10}
  ],
  "artifacts": ["build/app"],
  "dependencies": {"cc": null},
  "environment": {}
}
```

```bash
python3 "$DELIVERY_TOOL" verify --snapshot "$SNAPSHOT_FILE" --plan "$PLAN_FILE"
```

Every invocation allocates a fresh `verify-*` directory under the snapshot's
`verifications/` (or `--root PARENT`). It contains `source/`, `build/`, `cache/`,
`tmp/`, `evidence/`, `attempt.json` and final `result.json`. Variants and retries do
not share these directories. Commands run sequentially in the copied source;
stop on nonzero exit, timeout, launch error or changed source. The attempt path
is printed on completion. An interrupted attempt without a sealed result is incomplete.

`{source}`, `{build}`, `{cache}`, `{tmp}`, `{evidence}` expand within argv and explicit
environment values. No shell evaluation occurs. Shell operators are literal argv
text unless the caller deliberately selects a shell executable. Commands must remain
within the user's existing authorization; this helper grants no extra permission.

The runner sets `ORCH_SOURCE_DIR`, `ORCH_BUILD_DIR`, `ORCH_CACHE_DIR`, `TMPDIR`,
`XDG_CACHE_HOME` and `PYTHONDONTWRITEBYTECODE=1`. Other process environment values are
inherited but not serialized. Plan `environment` values are explicitly recorded;
use non-secret build settings. For example, Cargo needs
`"CARGO_TARGET_DIR": "{build}/cargo"`; point other tool-specific writable caches at
`{cache}`. Merely creating a private cache does not force a tool to use it. Do not
reuse absolute artifact/cache paths from an earlier attempt. HOME/CODEX_HOME and
runner-owned variables cannot be overridden in the plan. Keep credentials and
authorization with the existing execution environment.

Put outputs in `build/` and declare individual artifact paths beneath it. Logs,
resolved executable path/digest, argv, cwd, command times, timeout/outcome and real
exit code are recorded without copying full logs into controller output. The attempt
pins the snapshot and plan, explicit environment, platform and helper Python version.
It does not infer dependency completeness or hash every system library. The command
executable may itself be an interpreter; source scripts must be included in scope.

After each command, content and stat comparison detect source changes, including
ordinary write-and-restore operations. A command exiting 0 with changed source is
not a verification pass; checks that generate caches or reformat source need separate
output paths or a new capture/review. The runner retires its own process group on
completion/timeout. It does not contain processes that deliberately escape, global
caches, network access or external writes. Continue host supervision independently.

`passed=true` means all listed commands exited 0, captured source stayed unchanged,
the stored snapshot still validated, and declared artifacts were readable and hashed.
It means these checks passed, not that they constitute complete product acceptance.
Retain original failed attempts. A successful retry creates new evidence; source
corrections require a new delivery snapshot, and protocol follow-ups retain fresh
round identities as usual.

## Read back and recover evidence

```bash
python3 "$DELIVERY_TOOL" check --snapshot "$SNAPSHOT_FILE"
python3 "$DELIVERY_TOOL" inspect --attempt "$ATTEMPT_FILE"
```

`check` reads the stored copy, not today's author checkout. `inspect` validates sealed
attempt/result metadata, the pinned snapshot, verification source, log and artifact
hashes. It exposes `recorded_passed`, `evidence_valid`, `complete`, and current `passed`.
An original failed check can have valid evidence; changed artifacts invalidate the
current pass. Missing/corrupt result metadata yields `complete=false`, `passed=null`.
Evidence hashes detect changes; they are not a signature against same-user tampering.

CLI exit 0 means capture succeeded, the snapshot is valid, or the verification/readback
passed, respectively. Exit 1 means a failed check or unusable/incomplete readback;
exit 2 means invalid input/local error. Preserve JSON and evidence, not just the code.
Use the resulting paths in the verifier's report and job closure evidence. Run recovery
still enumerates jobs/rounds/watches; it does not automatically certify these deliveries.
