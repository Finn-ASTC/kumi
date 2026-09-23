# Acceptance and host settling

Before marking a job completed, record three separate facts: `response_published`,
`accepted_by_verifier`, `host_settled`. `jobs.py check-completion`, `recover` and
`runs.py recover` expose them. A success response alone cannot close a job as completed.
These are scoped controller observations: tools check identities, times and evidence
bytes, while the controller judges acceptance and observes the native host. They do
not discover all background activity or guarantee the absence of future writes.

## Independent acceptance

Use the [submission index](submission.md)'s current revision and live lease with
`jobs.py accept --input acceptance.json`. Every mutation returns a new revision.
Write the payload as data with an existing absolute evidence path:

```json
{
  "kind": "answer",
  "verdict": "accepted",
  "evidence_path": "/absolute/run/evidence/answer-review.txt",
  "note": "Checked the requested answer against the cited source."
}
```

The evidence file contains acceptance checks, findings and reviewer identity. `answer`
is for a response-only deliverable. Declared file changes/artifacts require `kind:
"delivery"` and an additional absolute `attempt_path` from [delivery verification](delivery.md).
Capture the handoff after the current response: its snapshot must bind the same job,
round, request hash and result hash. The helper reads back the attempt; only complete,
intact, passing evidence can be accepted. An initial baseline or another round's passing
attempt is insufficient. `verdict: "rejected"` retains a negative review.

Select the kind from the actual assignment: an empty child file list cannot prove a
file-producing task is read-only. Check the baseline/diff and whether the explicit
verification plan covers the requested outcome and integration version.

### Accepting a verification round

A verifier's own job needs acceptance too. The controller can perform it locally;
no additional agent is required. A response-only review can use `answer`. If the
verifier produced a report, oracle, harness or other files, use `delivery` for that
job. The author's accepted attempt cannot accept the verifier's different round.

Before the verifier starts, capture its own cwd with the agreed scope, for example
`--include . --exclude work`, and retain `V_BASELINE`. After its valid response and
writer handoff, use the **same scope** and storage outside its entire cwd:

```bash
python3 "$DELIVERY_TOOL" capture --request "$V_REQUEST" \
  --include . --exclude work --baseline "$V_BASELINE" --root "$V_STORE" \
  --handoff 'Verifier yielded its report, oracle and evidence for controller acceptance'
```

Retain `snapshot_path` as `V_SNAPSHOT`. Inspect warnings, the baseline comparison,
declared/excluded files and report. Pin the oracle/harness and fixture versions in
this snapshot. Define a controller-owned plan using their actual interface; exercise
both a known-correct case and a deliberately incorrect one. Require a specific
rejection outcome, not any nonzero exit that might just be a missing dependency.

For a minimal executable example, suppose the task specification says total=6 is
correct and the verifier's `oracle.py FILE` reads a JSON output, exits 0 when correct
and 1 when incorrect. This plan creates isolated fixtures and checks both outcomes:

```json
{
  "commands": [{
    "argv": ["python3", "-c", "import pathlib, subprocess, sys; source, build = map(pathlib.Path, sys.argv[1:]); good = build / 'good.json'; bad = build / 'bad.json'; good.write_text('{\"total\":6}'); bad.write_text('{\"total\":7}'); a = subprocess.run([sys.executable, str(source / 'oracle.py'), str(good)], timeout=5); b = subprocess.run([sys.executable, str(source / 'oracle.py'), str(bad)], timeout=5); print('oracle exits:', a.returncode, b.returncode); sys.exit(0 if (a.returncode, b.returncode) == (0, 1) else 1)", "{source}", "{build}"],
    "timeout_seconds": 15
  }],
  "artifacts": [],
  "dependencies": {"python3": null},
  "environment": {}
}
```

Save it as `V_PLAN` only for that interface/specification; adapt it to the real
task before use. The plan tests the oracle, not a whole product. For a verification
assignment on a built product, also rerun the pinned oracle/harness against the
controller's independent build of the agreed author snapshot and inspect the report
against the new logs. Bind source, fixture and artifact hashes; keep builds/outputs
in the fresh attempt's writable directories. Add every gate needed for the assigned
verification scope. Reading a PASS report or running only positive cases is insufficient.

```bash
python3 "$DELIVERY_TOOL" verify --snapshot "$V_SNAPSHOT" --plan "$V_PLAN"
python3 "$DELIVERY_TOOL" inspect --attempt "$V_ATTEMPT"
```

`V_ATTEMPT` is the returned `attempt_path`. After checking complete, intact, passing
evidence and the actual assignment, write `V_ACCEPTANCE` with absolute paths:

```json
{
  "kind": "delivery",
  "verdict": "accepted",
  "attempt_path": "/absolute/verifier-delivery/verifications/verify-ID/attempt.json",
  "evidence_path": "/absolute/controller/verifier-review.txt",
  "note": "Reviewed verification deliverables and reran the assigned checks, including oracle rejection controls."
}
```

```bash
python3 "$JOBS_TOOL" accept --index "$INDEX" --job "$V_JOB" \
  --expect-revision "$V_REVISION" --token "$V_TOKEN" --input "$V_ACCEPTANCE"
```

The tool binds V's current job/round, request hash and result hash to V's delivery
snapshot and attempt. A baseline, the author's attempt or a failed attempt cannot
satisfy this acceptance. Record a separate current host observation and use
`check-completion`/`close` below; accepted verification output does not settle its host.

## Per-round review and follow-up

Before activating a successor to a `success` response, record a valid review with
`accept`: either `accepted` for the reviewed result or `rejected` for rework. Missing,
changed or mismatched evidence blocks activation. A rejected delivery may use a
complete, intact **failed** verification attempt; accepted delivery still requires a
passing attempt. Valid `blocked` and `error` responses can continue without success
acceptance. `prepare --previous` alone remains preparation, not activation or input.

Activation stores the predecessor's exact result hash and review status at the time
of transition. It clears current acceptance and host observations for the successor.
This review gate does not settle the host: check input readiness and retain supervision
before sending. `close completed` continues to require fresh scoped host evidence.

Query any indexed round, including legacy history, without a lease or terminal I/O:

```bash
python3 "$JOBS_TOOL" round-status --index "$INDEX" --job "$JOB_ID" --round-id "$OLD_ROUND"
```

Omitting `--round-id` selects the active round. The response separates current evidence
validity (`acceptance.status`: accepted/rejected/missing/invalid/not_applicable) from
the original `transition`. `not_applicable` means a valid blocked/error response has
no success acceptance. Legacy transitions without a checked receipt report
`unknown_legacy`; later annotations cannot make them appear reviewed at that time.
Exit 0 means the query succeeded, including missing/rejected/invalid reviews; inspect
the status. This is not `check-completion` and makes no live-host readiness claim.

To backfill or revise a review, use the **current job revision and live lease**, but
select the old round explicitly. The payload is the same answer/delivery review
shown above; delivery must use that old round's own snapshot and verification attempt.

```bash
python3 "$JOBS_TOOL" accept --index "$INDEX" --job "$JOB_ID" \
  --round-id "$OLD_ROUND" --expect-revision "$REVISION" --token "$TOKEN" \
  --input "$OLD_ACCEPTANCE_FILE"
```

The returned revision advances, but the current round, submission, native identity,
watch and completion remain unchanged. `recorded_at` is the actual annotation time;
`backfilled: true` and `recorded_revision` locate it. Old revision files and the
original transition receipt stay unchanged. Query the selected round again to inspect
the result; default mutation replies retain only current-round details. Full index
history retains superseded reviews. Prepared but never activated rounds and another
job's rounds cannot be annotated. Known historical result hashes must still match;
legacy unreviewed results without a retained hash can only be reviewed as observed now.

For an already closed job, acquire a limited review lease:

```bash
python3 "$JOBS_TOOL" claim --index "$INDEX" --job "$JOB_ID" \
  --expect-revision "$REVISION" --owner "$REVIEWER_ID" --acceptance-only
```

Use the returned token and revision for `accept`, then release the lease. This lease
permits only `accept`, `renew` and `release`, including when used on an open job; it
cannot submit, activate, close or record host activity. It cannot take over a live
lease. The original `closed` record and `closed.completion` remain unchanged; a later
negative review can disagree with that historical closure without reopening the job
or certifying its current quality. No new host observation is fabricated.

## Scoped host observation

Read the selected host reference: [Hermes](../../agent-hermes/references/lifecycle.md),
[omp](../../agent-omp/references/lifecycle.md), [Codex](../../agent-codex/references/native.md),
[OpenCode](../../agent-opencode/references/native.md), plus [OMO](../../agent-opencode/references/omo.md)
when enabled. Keep native session IDs separate from protocol and pane IDs. Use
`jobs.py update` to retain the exact session/turn and launch profile. A settled
observation requires recorded transport resources and a native session ID.

Write fresh evidence containing the exact target/version, observation time, relevant
screen/process/task records, native child lineage and observed side effects, including
relevant paths outside cwd. Distinguish authorized changes from unexplained ones.
Then submit `jobs.py host --input host.json` with all fields below:

| Field | Value |
|---|---|
| `host`, `host_version` | `hermes`, `omp`, `codex` or `opencode`; actual version string |
| `status` | `active`, `waiting`, `unknown` or `settled` |
| `disposition` | `null` unless settled; then `reusable` or `exited` |
| `observed_at`, `valid_until` | Unix seconds; observation no later than now, validity at most 300 seconds after observation |
| `checks` | Exactly `foreground`, `background`, `native_children`, `interaction`, `side_effects`; each `clear`, `pending` or `unknown` |
| `children` | Array of `{session_id, parent_session_id, status}`; status `active`, `settled` or `unknown` |
| `side_effect_paths` | Array of absolute paths included in the observation scope |
| `evidence_path`, `note` | Existing evidence file and explanation of the actual observation |

Settled requires fresh, clear checks and all listed children settled. The execution
checks mean scoped owned work stopped executing/writing. Interaction clear means no
unresolved permission/question, queued work or product dialog and an input-ready or
exited target. Side effects clear means observed changes were accounted for within
authorization, not zero writes. An empty children array means none found within the
documented scope, not universal proof that the host never spawned work.

Expiry is a ceiling, not a recommended wait. Record newly observed activity as
active/waiting/unknown immediately; continue supervision and reconcile review records.
Unknown coverage remains unknown. Do not send a fresh prompt as a background-stop test.

## Check, close and recover

```bash
python3 "$JOBS_TOOL" accept --index "$INDEX" --job "$JOB_ID" \
  --expect-revision "$REVISION" --token "$TOKEN" --input "$ACCEPTANCE_FILE"
# Copy the returned revision before the next mutation.
python3 "$JOBS_TOOL" host --index "$INDEX" --job "$JOB_ID" \
  --expect-revision "$REVISION" --token "$TOKEN" --input "$HOST_FILE"
python3 "$JOBS_TOOL" check-completion --index "$INDEX" --job "$JOB_ID"
```

Check exits 0 when ready, 1 when incomplete, 2 for missing required local files, 3 for
invalid input/state. It returns reasons without terminal I/O. `close` with outcome
completed checks again under the current lease; supply its usual evidence_path/note.
Delivery readback hashes source/logs/artifacts, adding local I/O for large deliveries.
The recovery `next_action` is a general submission/response suggestion, not the completion
verdict. Watch pending/review queues track event handling separately; neither an empty
queue nor `handled` supplies acceptance or host evidence. Consult `ready_to_complete`.

Changed result/review bytes invalidate acceptance. A changed native session/turn/profile
invalidates the host observation. Follow-up activation clears both facts but preserves
old immutable revisions. Recover before resuming; expired host observations need a
fresh check. Keep watching until actual handoff/exit.

Historical v1 jobs remain readable; missing facts are unknown. New completed closures
retain `closed.completion` as the decision-time check; the current completion view can
later expire or report damaged evidence. Historical closure never grants current reuse.
Failed/cancelled closure retains its existing evidence contract and does not certify
host settling; cleanup still needs live ownership and cancellation checks.
