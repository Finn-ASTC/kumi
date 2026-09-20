# Supervision identity and audit

Use `watch.py pending` or `runs.py recover` between independent work blocks. Keep
an active observation owner. A durable queue cannot wake an ended controller.

## Checkpoint before work and input

Establish observation **before the first prompt and before each new round**:
create the watch from current requests/resources, start `watch.py run` through a
managed handle, wait for each target's first successful check, then register its
monitor owner/watch/expiry with `jobs.py update`. Use the actual bounded process
lifetime; declaring an owner does not start a process. The observer now records
its own instance, planned expiry and exit in `observer.json` beside `watch.json`.

Before an independent work block:

```bash
python3 "$ORCH_SCRIPTS/supervision.py" --run "$RUN_FILE" --work-seconds 15
```

The read-only checkpoint returns `can_work`, `work_budget_seconds`, `recheck_by`,
per-job `coverage`, queue counts and bounded event/evidence references. Exit 0
means a positive work budget at that observation; exit 1 means reconcile first;
exit 2 means the checkpoint itself failed. It does not read terminals, approve,
renew, cancel, or schedule another controller turn. It still scans full recovery
internally; this is not a delta or constant-cost query.

- Inspect open events and their live UI before unrelated work. Return within the
  budget (at most 30 seconds, 15 requested by default), even when useful independent
  work remains. If a long local action is necessary, keep it interruptible through
  a managed handle and checkpoint between waits.
- Existing `waiting_user` remains visible without blocking other independent
  work. Read its note before repeating a question; it is not approval for a new
  operation. Paginated details remain available through `runs.py recover`.
- A live lock with a stale/failed target read is unhealthy. The default freshness
  limit is 30 seconds. A success result or done UI does not remove coverage needs;
  only explicitly closed jobs leave the active coverage list.
- Unknown/dead UI is separately surfaced with zero work budget even when the
  transport read succeeded. Inspect it manually. A freshly reviewed, nonempty
  no-hook input prompt can justify that specific input; it does not establish
  readiness for a long independent work block. Missing/error/expired observation
  or missing ownership still needs repair before input.
- At 30 seconds before either the observer's planned expiry or the declared
  responsibility expiry, the default budget becomes zero. An owner declaration
  longer than the actual observer lifetime cannot hide that renewal requirement.
  Missing/legacy lifetime metadata, incomplete recovery and unknown health also
  require reconciliation; do not invent metadata for an old observer.

For handoff, prepare a replacement watch of the exact current rounds while the old
observer still runs. Start the new managed observer, confirm successful checks,
and move each monitor receipt to it. Then stop only the old owned observer handle.
Retain old watches/reviews and recover the whole run, so unanswered questions and
unreviewed events survive. A new watch may produce new events requiring review;
no old approval is transferred. To restart the same watch, first stop and join its
old observer, cover the gap with manual checks, then start/register/check again.

If coverage has already failed or expired, manually inspect active targets while
repairing it; do not begin another long work block. Observer PID is diagnostic and
may belong to another process namespace: use the original managed handle for stop
and join, not a guessed host PID. Actual expiry can overrun by a final read; this
does not extend the checkpoint's work budget. Clock inconsistencies fail closed.

These snapshots cannot ensure future observation or timely human/model attention.
An ended controller still needs an external wake-up mechanism. Treat zero-budget
output as a work boundary, not an automatic intervention or an SLA.

## File boundaries and request identity

New watches retain `request_sha256`, a hash of the exact request bytes read at
creation. Each sweep checks it before using the contract or reading the target
terminal. Changed request/resource identity stops that target's terminal I/O;
format-only request edits also count as a change.

A legacy watch without `request_sha256` reports `observation_error` asking for
recreation. This is distinct from `publication.json`, the first valid **result**
receipt described below. Do not add a current hash to an old manifest or delete its
history. Stop only the old observer through its owned process handle, keep manual
supervision active, and reconcile the original request/resources against the run/job
records and available evidence. If the original contract cannot be established,
resolve that uncertainty before creating a replacement. For a reconciled request:

```bash
python3 "$ORCH_SCRIPTS/watch.py" init --request "$REQUEST_FILE"
```

`ORCH_SCRIPTS` is this skill's `scripts` directory. Repeat `--request` for each
current target. Start the returned new watch through a managed background handle;
keep the old watch, events, reviews and publication receipt. Same-run watches are
registered automatically: continue using `runs.py recover --run "$RUN_FILE"` to
retain old `waiting_user` notes. For standalone watches, retain their paths and
query both old and new `pending` queues. Replacement never approves an old question
or resubmits the business task.

Protocol JSON and index evidence reads accept regular files up to **16 MiB per
file**. The reader uses nonblocking open, refuses a final-component symlink and
checks the opened descriptor and bounded bytes; a FIFO, directory or oversized
file reports an error without waiting for a writer. Store large logs/artifacts
separately and reference them from concise receipts. Keep serialized receipts and
watch evidence within the same limit. This does not isolate ancestor directories
or impose a hard deadline on a stalled filesystem. `run --duration` is checked
between sweeps, so it can include the final sweep's read time; retain an external
process handle and check per-target successful-check timestamps.

When request/resources remain valid, a result/pin read error and a terminal
observation are independent: `observation_error` and native-dialog `attention` can
coexist. Evidence retains `file_error` and `transport_error` separately. Repeated
unchanged file errors do not repeatedly raise the same observed dialog; incomplete
file/terminal checks do not advance `last_successful_check_at`.

## Published results must remain unchanged

The first **observed valid** response, including `blocked` or `error`, is pinned in
`publication.json` beside its request. The receipt retains exact UTF-8 result text,
SHA-256, job/round identity, request fingerprint and first observation time. Each
replacement watch for that round uses the same atomic no-overwrite receipt.

After that pin, different bytes (even formatting only), an invalid replacement or
deletion produce `result_changed`, priority 0 in pending/run recovery. The event
evidence's `publication` includes the pin path, first/current hash and `changed`;
the current hash is null for an observed missing file. File evidence remains usable
when terminal reads fail: a rewrite alert and `observation_error` can coexist.
An unreadable file/pin or invalid pin is an observation error, not permission to
reset the baseline. Preserve it and reconcile before accepting more work.

Inspect the original pin, event evidence, live result and actual deliverables.
Do not automatically accept the replacement, repeat the business task or rewrite
the old valid response. Correct a published report in a **fresh round** using the
normal follow-up protocol; preserve and review the incident. Restoring original
bytes emits a restoration `result_changed` in a watch that observed the change;
it never clears prior pending incidents. Replacement watches also retain old
incidents through run recovery; a standalone controller must retain all watches.

Before any valid publication has been observed, an invalid result can still use
the existing bounded report-only repair procedure. A valid report ends that exception.
This extends observation, not the completion gate: existing acceptance checks
detect changed result hashes, but handling an alert never supplies acceptance or
host-settling evidence. Do not close merely because a result validates again.

Limits: the result pin starts at first valid observation, including in a replacement
for a legacy watch. It cannot recover earlier unseen bytes or detect write-and-restore between
polls. It is not an OS write lock, signature or protection against coordinated
same-user modification of the result and receipts. Preserve the original events
as well as the pin; the helper never sends terminal input or repairs results.

## Three levels of attention

- `attention` is a suspected native dialog, including while a host reports working.
  It uses delimited command frames or line-shaped prompt cues (questions,
  numbered approval options, permission/login headings and paired controls).
  Approval/denial words inside task prose, tables or tool output alone do not
  trigger it. Inspect the full evidence and current UI before responding.
- State changes, read failures, invalid results, stalls and deadlines remain separate
  events. A valid blocked response is a round result, not a native dialog.
- `review_due` is a low-confidence changed-screen fallback. It starts after the
  configured review interval (30 seconds by default), doubles after each reminder,
  and caps at max(300 seconds, configured interval). State/dialog changes and read
  gaps restart it. Unknown UI can therefore take up to that cap plus observation
  delay to surface; inspect manually when a shorter bound is needed.

The observer still examines every target each sweep. Recognized permissions never
wait for the fallback timer. Continuous redraw at 15-second steps through 600 seconds
produces reminders at 30, 90, 210 and 450 seconds under defaults. These are replay
counts, not measured token savings or approval latency.

## Local groups and associated history

`attention` carries an `issue_id` for this observed occurrence and a
`correlation_key` derived from exact job, round, resource record and extracted
region. Command frames starting with `Command:` or the Codex-shaped run question
and ending with recognized controls retain every captured command/option line.
Numbers, arguments and multiline content are not normalized. Ordinary prose and
telemetry outside these frames do not alter their key. Other prompt-shaped lines
retain surrounding context; `blocked` still surfaces even without a recognized
cue. Unknown layouts retain the changed-screen fallback. This is heuristic, not a complete host
UI parser; the captured screen itself is bounded (80 lines/32,000 chars). Tmux
uses the visible screen (`-S 0`), excluding scrollback copies of cleared dialogs.
Wrapped, localized or novel prompts can miss the line rules; quoted examples
that reproduce a prompt's line layout can still match. Absence of `attention`
does not establish readiness, resolution or permission.

Changing the command/options, observing disappearance/reappearance, or recovering
from a blind read interval creates a new unreviewed occurrence. A replacement watch
also creates a new occurrence even when the correlation key matches. Disappearance
between observations cannot be inferred. Truncated/unknown screens require live
inspection; no text match authorizes input.
Detector updates can change extracted keys. Retain old pending events and their
reviews; a newly surfaced occurrence still needs review. Do not rewrite historical
keys or clear old queues to make the new detector appear quiet.

`review_due` uses a local issue for a continuous observation period; its correlation
key is local to that period. In pending/recover, earlier unreviewed reminders are
represented by the latest event and evidence. `occurrences` counts represented
reminders; `detected_at` is their earliest detection, `observed_at` the latest.
`first_seq` / `first_evidence_path` locate the start of the group; original event
files retain every intervening observation that emitted a reminder.
Review that latest **watch_path + seq** after inspecting its evidence. Its receipt
covers earlier unreviewed reminders in the same local issue. Explicit `waiting_user`
or `open` receipts remain separate. Newer reminders remain open even if an older
one was handled or waiting for a user. All original events and receipts stay on disk;
`events` remains the raw delivery stream. Queue counts count displayed work items.

Before asking a question, read `waiting_user` and `related_reviews`. The latter
contains up to three recent matching receipts plus `related_review_count`, including
handled events, across registered watches in run recovery. Each reference carries
its original watch path and sequence, note, status, review time and outcome evidence.
Use `review-status` and its history directory for older revisions. Association is an
advisory aid: a new event retains revision 0/open, and no approval or input is copied.
Different rounds/resources/commands are not automatically associated. Preserve old
question notes through run recovery and reconcile those cases explicitly.

Apply existing user authorization to the exact live operation. Ask only for a
missing decision, record that question once, and continue other work. Never infer
approval from `handled`, a correlation key, a prior seq, or a command's first line.
Unrecognized and multiline commands require examination of the complete operation.
These tools never send approval/input.

## Separate detection, review, action and resolution

Event `observed_at` is read completion, not the native dialog's true onset. Queue
`detected_at` records the first represented detection. A receipt's `recorded_at`
is the actual time that review was recorded (shown as `reviewed_at` in queues).
None is proof an operation succeeded.

Record the attempted action in a separate file with target identity, exact action,
action time, authorization basis and transport outcome. Pass it as
`--action-evidence FILE`. This records a reference and SHA-256, not executable input.
After independently checking the current target/result, record resolution evidence:

```bash
python3 "$WATCH_TOOL" review --watch "$WATCH_FILE" --seq "$SEQ" \
  --status handled --expected-revision "$REVISION" --note-file "$NOTE_FILE" \
  --action-evidence "$ACTION_LOG" --resolution-evidence "$RESOLUTION_LOG" \
  --resolved-at "$RESOLUTION_EPOCH"
```

The resolution time must be Unix seconds between event observation and receipt
recording. `--resolution-evidence` and `--resolved-at` must appear together and
require `handled`. `handled` by itself leaves resolution unknown; it also supports
reviewed false positives. Outcome fields belong to each immutable receipt, so supply
all evidence intended for a new revision; older revisions remain in history.
Files must exist when recorded and their hashes are pinned. Preserve those files;
the tool validates the reference shape, not the truth of controller-authored evidence.
Exact retries include evidence hashes; changing evidence is not an idempotent retry.

### Record each confirmed outcome before the next item

Keep this short sequence adjacent in the controller's work. Once the live check
has confirmed an outcome, the next bookkeeping operation is its `review` write,
before a full recovery scan, another dialog, unrelated work or a final report.
Do not accumulate confirmations for end-of-batch recording.

1. Read the event and live operation; retain the exact watch path, seq, revision,
   job/round and resource identity. Examine the complete command/options against
   existing user authorization. If a decision is missing, record the specific
   question as `waiting_user` immediately and preserve existing unanswered notes.
2. For an authorized response, save action evidence with those identities, the
   exact response, authorization basis, actual send time and transport outcome.
   Keep the occurrence `open` while resolution is uncertain; an `open` review
   with `--action-evidence` can preserve the attempt across a controller restart.
   Sent input alone does not verify native resolution.
3. Independently inspect the same target after the response. Save the actual
   observation time, identity, live screen/result and the conclusion it supports
   to a new resolution file. Dialog departure can resolve that prompt; it does
   not prove command success, task acceptance or host shutdown. If the target
   cannot be read or the old dialog persists, keep it open and reconcile the
   uncertain attempt before deciding whether to send anything again.
4. Immediately run the `review` command above for that exact occurrence with
   both evidence files and the observation's `--resolved-at`. Check its exit and
   receipt. Use the revision returned by any preceding `open` write. A lost
   reply permits only an exact retry; a revision conflict requires rereading
   `review-status`, not overwriting or resending native input.
5. Review any companion blocked-state event separately against its own evidence.
   It may be recorded as handled bookkeeping referring to the exact still-open
   attention event, without claiming resolution. Never bulk-handle a job, round
   or correlation group. A new operation or reappearing dialog keeps its own
   open occurrence even if visible in the same confirmation screen.
6. Recover pending events, inspect new items, and run the supervision checkpoint
   before independent work. Existing `waiting_user` does not authorize new input.

For a reviewed false positive, write `handled` with a specific explanation and
no invented action or resolution time. The review API still accepts `handled`
without resolution evidence for compatibility and bookkeeping; controllers must
not use that form to describe an unverified approval as resolved. These are
controller-authored observations, not automatically validated truth or an SLA.

## Timing with a slow target

Each target stores `read_started_at`, `last_checked_at` (read completion),
`last_successful_check_at`, `read_duration_seconds` (monotonic duration), and error.
Completed target event files/health are published while other reads remain in flight;
`events`/`wait` can consume them immediately. `run` stdout still batches a sweep.
At most eight reads run concurrently; a sweep still waits for its reads before the
next sweep. These fields expose transport delay, not a guarantee of model wake-up
or approval responsiveness. Legacy records retain their old timestamp semantics.
