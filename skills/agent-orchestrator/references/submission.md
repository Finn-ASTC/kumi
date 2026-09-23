# Submission index and recovery

Use `scripts/jobs.py` beside `protocol.py` for controller receipts. It never launches
agents, sends terminal input or grants approval. Acceptance checks read existing delivery evidence; they do not run build/test commands. Request/result
v1 stays unchanged. The index holds registered jobs, a single active round per job,
attempts, launch/native identity, monitoring responsibility and immutable revisions.

Completion adds `accept`, `host` and read-only `check-completion`; see [acceptance and host settling](completion.md). Completed closure requires accepted output and fresh scoped host evidence as well as success. Allocated/launched cancelled jobs now require a confirmed stop; see [intervention receipts](controls.md). Legacy missing facts remain unknown; historical closed records are not rewritten.

## One persistent handle

Use the [persistent run](runs.md) returned by initial preparation, or initialize one
with `runs.py init` and pass its `run_path` as `prepare --run` for all related children.
Use its returned `index_path` as `INDEX`, shared by all controllers. Follow-ups and
same-run watches inherit this storage; explicit `--temporary` selects a short-lived
experiment. Retain the run handle in the async handoff. Do not move round directories:
contracts contain absolute result paths. Explicit legacy `--root` placement still works.

Register every **unsent initial request before launch/input**:

```bash
python3 "$JOBS_TOOL" register --index "$INDEX" --request "$REQUEST_FILE"
python3 "$JOBS_TOOL" list --index "$INDEX"
python3 "$JOBS_TOOL" recover --index "$INDEX" --job "$JOB_ID"
```

Set `JOBS_TOOL` to the absolute `scripts/jobs.py` path. Copy job IDs and revisions from
JSON replies. `register` is repeatable for the same initial request/index; an initial
round's `job-index.json` binds it to that canonical index. A copied index is not a
second authority. Unregistered legacy runs require read-only reconciliation first:
do not register a possibly-sent request as new `prepared` work. Absence of a receipt
cannot prove that historical input never happened.

`list` reports all registered jobs, including closed and unreadable entries. It does
not discover arbitrary `/tmp` rounds or read terminal state. `recover` returns current
metadata, the active request, result validity, lease/monitor expiry and a suggested next
action. It does not change acceptance based on a result. Null metadata means unknown;
an absent/expired monitor does not imply that a live supervisor exists.

CLI replies show the active round and its latest attempt/receipt by default, with total
round/attempt counts and the history directory. `recover --history` includes all round
and attempt metadata when needed. Read full evidence files only for the relevant event.

## Claim, record, begin, send, reconcile

1. Acquire ownership before starting or continuing external work:

   ```bash
   python3 "$JOBS_TOOL" claim --index "$INDEX" --job "$JOB_ID" \
     --owner "$CONTROLLER_ID" --expect-revision "$REVISION" --lease-seconds 300
   ```

   Save the returned `lease.token` and `revision`. Every mutation below requires both
   `--token "$TOKEN"` and `--expect-revision "$REVISION"`. Replace `REVISION` with the
   returned revision after **each** successful mutation. A stale revision, live rival
   lease or expired token fails. On any lost reply, recover before deciding; never
   replay terminal input as a way to retry local bookkeeping. Recover intentionally
   omits the token. If a claim reply was lost, wait for expiry and acquire a new lease.

2. Record the exact launch argv/profile, native IDs and monitor as they become known
   using `update --input "$METADATA_FILE"`. Use a file-editing tool to write UTF-8 JSON;
   do not interpolate user text into shell code. Supply only changed top-level fields,
   with complete nested objects. Example shape:

   ```json
   {
     "launch": {"argv": ["omp"], "profile": "existing-config"},
     "native": {"session_id": null, "turn_id": null},
     "monitor": {"owner": "controller-session", "watch_path": null, "expires_at": null},
     "deadline": null
   }
   ```

   Times are Unix epoch seconds; record actual future deadlines/expiries when known.
   A watch path must name a watch containing this exact active request/job/round. It is
   a retained handle, not a claim that the observer is alive. Keep actual process
   handles and supervision checks as described in the lifecycle/efficiency references.
   The launch argv is data, never evaluated by this tool. Preserve native profiles
   such as OpenCode OMO/pure in `launch.profile` and actual argv.

3. After interactive readiness and `protocol.py record`, reserve the submission:

   ```bash
   python3 "$JOBS_TOOL" begin --index "$INDEX" --job "$JOB_ID" \
     --token "$TOKEN" --expect-revision "$REVISION"
   ```

   This commits a fresh `attempt_id` with `submission=uncertain` **before** input can
   leave the controller. Save the returned revision and the last attempt ID. Check
   that your lease remains live and the exact target is ready, then send the existing
   prompt **once** using the transport recipe. Preserve the transport output in a new
   evidence file, including timeouts/errors. If the controller is interrupted after
   `begin`, treat the attempt as uncertain even if it might not have sent anything.

4. Check the result and live target immediately. Record the observation:

   ```bash
   python3 "$JOBS_TOOL" receipt --index "$INDEX" --job "$JOB_ID" \
     --token "$TOKEN" --expect-revision "$REVISION" --input "$RECEIPT_FILE"
   ```

   Receipt payload (copy the actual attempt ID and evidence path):

   ```json
   {
     "attempt_id": "copy-from-begin",
     "status": "uncertain",
     "kind": "timeout",
     "evidence_path": "/absolute/run/evidence/send-timeout.txt",
     "note": "Transport timed out; acceptance has not been confirmed."
   }
   ```

| Observation | `status` / `kind` | Next step |
|---|---|---|
| Matching valid result, including immediate `done` | `accepted` / `matching_result` | Verify/consume the response; no resend. The tool independently validates the result. |
| Exact target shows accepted/active task | `accepted` / `target_active` | Keep independent work and supervision running. |
| Timeout, disconnect, or only a CLI success with no target evidence | `uncertain` / `timeout`, `transport_error` or `unconfirmed` | Inspect target/result/artifacts; no resend. |
| Explicit evidence input never reached the agent, such as startup still in shell | `rejected` / `not_sent` | Fix startup/readiness; begin a fresh attempt only after confirming no task landed. |

Evidence files must exist; receipts retain their paths and SHA-256. The tool cannot
interpret a terminal screenshot or certify the controller's `target_active`/`not_sent`
judgment. A missing result, `idle`, timeout or expired lease is insufficient evidence
of rejection. An accepted attempt cannot be changed to rejected. A new `begin` is
allowed only for `prepared` or proven `rejected` work, with no result file. Invalid
results require investigation rather than another business submission.

## Follow-up and ownership transfer

For a valid blocked/error/success response, resolve the decision or revision, verify
input readiness and retain the current ownership lease. Use `prepare --previous`
(which inherits the run), then activate its returned request **before sending**:

```bash
python3 "$JOBS_TOOL" activate --index "$INDEX" --job "$JOB_ID" \
  --token "$TOKEN" --expect-revision "$REVISION" --input "$ACTIVATION_FILE"
```

`ACTIVATION_FILE` contains `{"request_path":"/absolute/new-round/request.json"}`.
Activation requires a valid response to the currently active predecessor, the same
job/cwd/depth/resources, and a fresh round. A success predecessor also needs a valid
accepted or rejected review; blocked/error may continue without success acceptance.
The transition retains that review status and exact result hash; see
[per-round review and follow-up](completion.md#per-round-review-and-follow-up) for
historical queries and annotations. Activation preserves history, clears the old watch
and native turn binding, and makes the successor `prepared`. Competing preparations
can leave unused directories, but only one direct successor can become active.
On conflict, recover and use the recorded active request; never send the losing prompt.
Bind the new watch, then run `begin → transport → receipt` for the active round.

| Command | Payload / behavior |
|---|---|
| `renew --input FILE` | `{"lease_seconds":300}`; only a still-live token can renew. |
| `release` | Releases local ownership, preserving acceptance and attempts. |
| `control-begin` / `control-receipt --input FILE` | Bind deny/interrupt/cancel intent and confirmation to exact identity under the current lease; see [controls](controls.md). Neither executes input. |
| `close --input FILE` | `outcome=completed|failed|cancelled`, `evidence_path`, `note`; completed requires the completion gates, allocated/launched cancelled requires a confirmed scoped stop. Failed bookkeeping alone does not prove writers stopped. Executes no cleanup. |

After expiry or release, a new `claim` issues a new token. The old token cannot mutate
the index. **Expiry does not fence external terminal input**: confirm the old controller
has stopped sending before taking over the target. This is cooperative local ownership,
not distributed locking or an exactly-once transport. A paused controller may still
hold an old prompt; the index cannot revoke its ability to call herdr/tmux directly.

All jobs sharing a terminal run must share the index. `begin` rejects a target already
recorded by another open job in that index, including its tmux server selector. This
does not detect other indices, unregistered jobs, or resource reuse outside the tool.
Recheck live target and parent identities before input or cleanup.

## Storage and failures

Each mutation publishes one immutable `INDEX/JOB/REVISION.json`, protected by a short
local filesystem lock and a revision check. Snapshots retain earlier rounds, attempts
and receipt evidence without replaying terminal logs. Keep this on a local filesystem;
network filesystem locking is not supported. A process crash leaves the previous or
next complete revision, never an in-place partial overwrite. A committed update whose
reply was lost is found by `recover`.

Exit 0 is successful bookkeeping/readback, not business success. Exit 2 means a required
file is missing; exit 3 means conflict, invalid state or another local failure. Missing
contracts/resources and replaced pinned files remain visible as errors. Preserve the
index, round records and referenced evidence together; do not rewrite historical
requests to repair an index. Use `runs.py recover --run RUN_FILE` for pending items
across registered watches, and use each item's exact watch handle with `review`.
Automatic discovery of native sessions remains a separate capability.
