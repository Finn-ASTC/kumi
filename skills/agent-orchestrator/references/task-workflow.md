# Author, verifier and rework packets

Use these examples when a file-producing task needs independent verification and
focused rework. They use the existing six-field packet schema; they add no protocol
fields or implicit permissions. Read only the selected packet. Copy it into the run
and replace the example paths, owners and checks with actual facts before preparing
the prompt. Referenced files must exist and be readable by the target; `prepare`
validates the packet shape, not those facts or the contents of referenced files.

For every role, a factual error found after a valid response was published follows
the [published-report correction procedure](../../agent-controlled/references/protocol.md#correcting-a-published-report):
keep the response, notify the controller in normal output, then wait for a fresh
round/path. A correction notice does not replace the report or acceptance evidence.

| Packet | Required task facts within existing fields |
|---|---|
| [author](../assets/task-packets/author.json) | Objective, writable files/owner, delivery target/configurations, author checks, dependency state and blocked condition, handoff |
| [verify](../assets/task-packets/verify.json) | Exact snapshot/hash, configuration coverage and artifact checks, oracle version/environment, commands/fixture hashes, fresh evidence location, test owner, product verdict |
| [rework](../assets/task-packets/rework.json) | Defect/reproduction and failing configuration/version, newly granted scope, characterization replacement and ignored-test owner/closure, unchanged oracle and coverage for revalidation |

The invoice example puts longer facts in three small referenced records. Adapt their
names and format to the project; they are caller-owned inputs, not helper schemas:

- `acceptance-plan.json`: delivery target and required configurations; named author,
  verifier and controller gates; actual argv/timeouts, tool versions/environment,
  artifact checks, and exclusions with reasons/owners. Use the required slots in
  [delivery configuration coverage](delivery-configurations.md). Include project fmt,
  lint, supported-runtime or MSRV checks when the project requires them.
- `verification-inputs.json`: delivery manifest path/digest, oracle identity/version,
  fixture paths/digests, environment and new evidence parent. Final checks include
  the integrated source and fixtures. Follow [delivery verification](delivery.md).
- `defect.json`: rejected snapshot and attempt paths/digests, expected/observed
  behavior, reproducer, fixture identity and specific rework owner.

Keep short facts directly in the packet when a separate record would add needless
reads. Use `none` with a reason for an inapplicable dependency or ignored-test slot;
do not invent files, approvals, ready dependencies or passing checks to fill it.
The caller-owned acceptance facts are distinct from `delivery.py`'s four-field
execution plan. Map each required gate to an execution plan and its commands;
keep coverage/owner metadata out of that tool's strict schema.

## Apply and continue

```bash
python3 "$ORCH_TOOL" prepare --run "$RUN_FILE" --cwd "$AUTHOR_CWD" \
  --parent-depth 0 --task-packet "$AUTHOR_PACKET" --brief
python3 "$ORCH_TOOL" prepare --previous "$AUTHOR_REQUEST" \
  --task-packet "$REWORK_PACKET" --brief
```

Use the controller's received depth for nested work; `prepare` generates the child
depth. Register/claim initial work and activate a successor before input, as in
[submission](submission.md). Follow-ups keep the exact native conversation and
receive a fresh round/result path. Preparing a packet does not launch or submit it.

While the author works, the verifier can prepare independent fixtures in its own
scope and the controller can prepare integration gates and supervise both targets.
Name the current phase explicitly: fixture preparation can finish successfully
without accepting any author version. Final verification waits for writer handoff,
a fixed snapshot and pinned oracle. Separate terminal pages do not isolate writes.
Exercise a newly prepared oracle with a known-invalid output as well as a valid
example; record its actual rejection. Self-consistent fixture expectations alone
do not establish that the verifier rejects wrong result shapes.

On a failed product check, preserve the original attempt and report the defect;
a successfully executed review may return protocol `success` with product rejected.
Record acceptance separately. If the task specification and oracle disagree,
return `blocked` with both expectations and a minimal counterexample. The controller
resolves the specification or oracle version in a new round before implementation
continues; neither author nor verifier silently changes the expected result.

Rework grants explicit ownership of old characterization and ignored tests. Report
each replacement/reactivation result; unresolved exclusions stay visible with an
owner and closure condition. Reverify a fresh source snapshot with the same pinned
fixtures. Reproduction also runs in a fresh writable copy, never in the stored
snapshot; interpreter caches alone can invalidate a stored tree. An authorized
oracle change gets a new version and explains which earlier
results no longer compare. Controller acceptance also checks the integrated gates;
[host settling](completion.md) remains a separate completion condition.
