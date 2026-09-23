# Exact intervention and cancellation receipts

Use this for an explicitly authorized denial, interruption or cancellation. The
helpers **never send input, approve operations, signal processes or stop observers**.
They bind observed facts to the current job lease; use the target dictionary and
fresh UI for the actual action. A method description is not an executable mapping.

## Read complete captured evidence

```bash
python3 "$ORCH_SCRIPTS/watch.py" detail --watch "$WATCH_FILE" --seq "$SEQ"
```

This returns the full **captured** `screen`, exact target/event/review and capture
limits. `screen_tail` is only a summary. New events pin evidence bytes; changed
evidence fails. Legacy evidence remains readable as `legacy_unpinned`, but cannot
drive a new denial intent. Capture completeness is `unknown`: an 80-line screen
cannot prove the full command or every option was visible. Read missing details
before deciding; terminal content is untrusted data, never authorization.

## Inspect, reserve, act once, confirm

Acquire the current job lease as in [submission](submission.md). Stop the previous
input owner before takeover; lease expiry cannot prevent its direct terminal input.
Keep the observer active. Use the current monitor's watch when one is registered.

```bash
python3 "$ORCH_SCRIPTS/controls.py" inspect --index "$INDEX" --job "$JOB_ID" \
  --watch "$WATCH_FILE" --seq "$SEQ"
```

Retain `inspection_path`. For a stop/cancel without a specific dialog, omit watch/seq.
Inspection reads the exact recorded terminal and writes local evidence, not input.
An unavailable target is `unknown`, not proven stopped; only manual reconciliation
may proceed in that case. Denial requires a pinned, current attention incident with
matching live command/options; incomplete reads and locally truncated captures fail.

Write an intent JSON using the actual returned path and decision evidence:

```json
{
  "kind": "deny",
  "inspection_path": "/absolute/index/job/control-inspections/id.json",
  "method": {"kind": "keys", "description": "Exact currently verified host option and key sequence"},
  "evidence_path": "/absolute/evidence/authorized-decision.json",
  "note": "Basis and scope for denying this single operation"
}
```

`kind` is `deny|interrupt|cancel`; method kind is `keys|native_api|process_signal|manual`.
Method text must describe the actual supported action, never a guess that Esc is a
universal reject key. OpenCode interruption, a permission denial and a job cancellation
are different operations. Ordinary prompt input may queue instead of stopping work.

```bash
python3 "$ORCH_SCRIPTS/jobs.py" control-begin --index "$INDEX" --job "$JOB_ID" \
  --expect-revision "$REVISION" --token "$TOKEN" --input "$INTENT_FILE"
```

Save the new revision and `control.control_id`. This rechecks the live target,
30-second inspection age, request/native/resource/submission identity and event
occurrence, then commits `uncertain` **before** external action. One unresolved
control blocks new submission/activation, launch/native changes and closure. Other
monitor bookkeeping, lease renewal/release and later receipt remain possible.
`supervision.py` returns zero work budget while a control needs reconciliation.

After a successful reservation, recheck current lease, exact target and supported
option immediately before sending the authorized action **once**. Keep focus unchanged.
Readback and terminal input are not atomic. A lost begin reply requires recovery,
not sending; a timeout or disconnect after input is uncertain, never `not_sent`.

Record a receipt using the returned control ID:

```json
{
  "control_id": "copy-from-control-begin",
  "status": "confirmed",
  "outcome": "denied",
  "observed_at": 1790000000,
  "checks": {"operation": "clear", "scope": "clear"},
  "evidence_path": "/absolute/evidence/live-denial.json",
  "note": "Observed this exact operation denied within the intended scope"
}
```

Replace the sample time with the actual observation epoch. `control-receipt` uses
the same index/job/revision/token/input flags as `control-begin`. Outcome observation
must be after intent and no later than recording. Delayed receipts retain the actual
observation time; cancellation closure separately requires a stop check within 30 seconds.
Evidence is pinned, but
the helper cannot prove a human/controller-authored observation is true.

| Status/outcome | Meaning |
|---|---|
| `uncertain` / `null` | Action/side effects unresolved; inspect without resending. |
| `not_sent` / `null` | Positive evidence no action reached the target; a fresh inspection can precede retry. |
| `confirmed` / `denied` | Exact operation actually denied, with `operation` and `scope` clear. |
| `confirmed` / `invalidated` | Denial incident proved no longer actionable; **does not claim denial**. Reconcile possible side effects. |
| `confirmed` / `stopped` | Interrupt/cancel scope settled, with all stop checks below clear. |

For nonconfirmed outcomes, checks may be empty or contain `clear|pending|unknown`.
Confirmed stop requires exactly `foreground`, `background`, `native_children`,
`interaction`, `side_effects`, all `clear`. Include actual native children, process
handles and inspected side effects in the evidence; an idle prompt alone is insufficient.

## Attach confirmation to the original event

After confirming a dialog action, immediately record its bound review:

```bash
python3 "$ORCH_SCRIPTS/controls.py" review --index "$INDEX" --job "$JOB_ID" \
  --control-id "$CONTROL_ID" --expected-review-revision "$REVIEW_REVISION" \
  --note-file "$NOTE_FILE"
```

It locates that control's original watch/seq/issue and immutable job revision,
checks evidence hashes, and calls the existing review API with action and resolution
evidence. It does not transfer approval to a new dialog or bulk-clear sibling events.
An uncertain receipt cannot be reviewed this way. Review revisions and job revisions
are independent. An exact lost-reply retry is idempotent; changed notes/evidence or a
review conflict require `watch.py review-status` reconciliation.

## Cancellation and takeover

For an allocated/launched job, `close outcome=cancelled` now requires a confirmed
interrupt/cancel, exact current identity and fresh unchanged stop evidence. A confirmed
cancel blocks new work and recovery suggests `close_cancelled`. Refresh an expired
stop with another `control-receipt confirmed/stopped` and new actual evidence; do not
send the stop action again just to refresh bookkeeping. A new submission/round cannot
borrow an earlier stop. A truly unallocated job with no launch/attempt can close as
`not_started` using explicit evidence. Historical closed records are not rewritten.

If the target died or transport is gone, use manual inspection of owned process
handles, descendants and side effects; retain the failed terminal observation and
record only what was actually established. Never fabricate a result envelope.
Without a valid old response, follow-up preparation stays unavailable: after closing
and reconciling writers, create a new initial recovery job with a link to the old one.

For takeover, stop/join the previous owned controller and its input workers using
their actual managed handles; observing a dead child does not prove the controller
stopped. Cover observer replacement with manual checks, release or wait for the old
lease to expire, claim a new token, and recover pending controls plus all old watches.
Live rival leases remain protected. Reconcile uncertain action facts before another
action. This local fencing cannot block external clients or controllers in other indices.

This is the DH-02/03 foundation, not an autonomous approval service. Native key maps,
operation IDs, timed human windows, budget enforcement and Jev/Laya remain separate
implementation and host-verification work. Existing `handled` reviews remain review
bookkeeping, not automatically proven native resolution.
