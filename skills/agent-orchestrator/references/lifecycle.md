# Lifecycle, recovery and resource ownership

Topics: multi-round questions, async handoff, ambiguous submission, retries, timeouts/cancellation, concurrency/nesting, validation and cleanup.

Before completing a job, use [acceptance and host settling](completion.md) to bind independent verification and a fresh native observation. Hermes/omp have dedicated lifecycle references; Codex and OpenCode/OMO retain their native stop/recovery rules. Unknown background work is not a settled host.

| Host/profile | Identity and post-result observations | Stop/reuse boundary |
|---|---|---|
| Hermes | `/status`, `/agents`, queue/standing work, scoped curator ledger and native lineage; see [host reference](../../agent-hermes/references/lifecycle.md). | Foreground interrupt, async delegation/background stop, and background review are separate. Preserve unknown coverage. |
| omp | `/session info`, product UI, native task handles, project-scoped `omp ps`; see [host reference](../../agent-omp/references/lifecycle.md). | Esc is separate from daemon process stop; check both and actual input readiness. |
| Codex | Exact TUI/exec session, turn/item/process records and background terminal handles; see [native](../../agent-codex/references/native.md) and [dictionary](../../agent-codex/SKILL.md). | Turn cancellation may leave background work; exec timeout does not cancel the owned process. Recheck exact-session permissions on resume. |
| OpenCode pure / OMO | Native session, mode and role; OMO adds bg/team/goal/continuation and project-state records; see [native](../../agent-opencode/references/native.md) and [OMO](../../agent-opencode/references/omo.md). | Check actual interrupt binding. OMO stop-continuation can clear project state and cancel descendants asynchronously; it is not a scope-free exit. |

Record the actual version/profile for the row used. Retain parent/child native IDs,
side-effect evidence paths and observation gaps with the completion receipt. This
matrix routes checks; it does not certify all hosts or versions were jointly tested.

## Keep round, job and process states separate

- A **round** progresses through prepared → submitted/uncertain → response received → verified or rejected.
- A **job** can remain open across blocked responses, revisions and recovery. It ends with verified completion, reported failure, cancellation, or an explicitly retained handoff.
- The **process** can be working, waiting for input, showing an approval UI, dead or disconnected. It does not carry the round identity.

`success` from the target is a claim until verified. `blocked` is a response to consume once; it is not a signal to repeat the same answer every polling interval. A prepared follow-up is a candidate: only successful `jobs.py activate` selects it as the active round. Then stop treating the old response as current. On activation conflict, recover the index's active request and leave the losing candidate unsent.

## Questions and multi-round conversation

1. Validate the response against its request; record that the round was consumed.
2. Read `blocked_reason` and any partial changes. Answer within the already authorized scope if possible. Ask the user only when a missing preference/approval materially blocks the work.
3. Wait for the target to accept input. A file may be written shortly before the TUI becomes idle.
4. Prepare a new round using `--previous`. Its task should contain the answer and the remaining objective/constraints, especially if the target lost context.
5. With the current lease/revision, activate the candidate, commit `begin`, submit exactly once and record the acceptance receipt. A previous `blocked` file stays immutable as evidence.

If the same question returns without new information, inspect why the answer did not resolve it. Do not keep echoing it until the total timeout. A depth violation, unavailable dependency or missing permission needs a concrete recovery or a failure/decision report.

After `success`, a revision also gets a new round; report only that round's net changes. A valid `error` can lead to a new recovery round when side effects have been reconciled. Do not automatically retry the full task merely because reporting failed.

## Controller work and supervision

After confirmed submission, choose a concrete piece of work that does not depend on the child's result: investigate another module, implement files assigned to the controller, or prepare acceptance checks. Continue it in bounded chunks while the child works. If all remaining useful work depends on children, use a short wait, then resume the supervision loop. A new workspace/tab changes terminal layout, not file ownership or dependency order.

Maintain one inventory of all active children, with each active request, exact target, deadline, `last_checked_at`, `next_check_at` and any `pending_question`. Default to a check approximately every **15 seconds per child**, with a normal maximum gap of **30 seconds**. For repeated terminal checks, use the [event supervisor](efficiency.md#event-supervisor) to read targets outside model context; record its watch path, managed process handle, expiry and last consumed event cursor. Handle new events between work blocks, including conservative UI reviews. An observer read is not evidence that a model reviewed or approved the screen.

On resume and between work blocks, also read [durable pending reviews](efficiency.md#durable-pending-reviews). Delivery cursors do not resolve events. Record `waiting_user` after asking a missing decision once, then `handled` only after actual reconciliation; inspect existing waiting notes before asking again. Preserve the watch handle and immutable review receipts across handoff. Review version checks prevent stale receipt writes but do not replace the single owner for terminal input.

Sweep immediately after startup/submission and on a blocked/unknown event, a notification, or stalled progress. If the observer fails or expires, resume the manual sweep below. Batch independent reads across children; one child's approval or long task must not starve the others. Record a missed check and reconcile promptly if tool latency prevents the intended cadence.

At each manual sweep, or when reconciling an observer event:

1. Validate any current-round result; record the actual status without treating a missing file as proof of work.
2. Read target state and the current terminal bottom/visible UI, even when state is `working`. Check for native permission, trust, login and question dialogs, as well as process exit or lack of progress. For pure OpenCode or unreliable hooks, screen/process evidence remains essential.
3. Handle an observed dialog within existing authorization. If it needs a user decision, relay the exact action/question once, store it as pending, and continue other work. On subsequent sweeps check for changes without repeating the same question or answering on the user's behalf. A native UI approval continues the same round; only an already published valid `blocked` result requires a fresh round for the answer.
4. Record the check and next due time, consume/verify any response, then return to independent work. Keep monitoring live UI/process state until owned work has actually settled, including activity after publication.

Keep local tools interruptible: launch long tests/builds using a supported managed background handle or short yielding call, then inspect child agents between polls. A 5-minute blocking tool call or wait cannot meet the supervision schedule. When no local work remains, use waits of about 15 seconds and recheck every child afterward; do not wait indefinitely or watch only one target. Readiness checks before sending input are still required.

For an authorized append-only business log, the [checkpoint waiter](supervision.md#waiting-for-native-checkpoints)
can return on a complete marker line while the original process keeps running.
Retain its byte cursor and the original managed process handle; timeout is not cancellation.

`protocol.py` has no timer. `watch.py run` polls only while its managed process remains alive and within its duration; its files do not automatically notify a model. Ending the controller turn requires an actual supported event/monitor handoff or an accurate retained handle as described below. Do not claim continuous supervision without such a mechanism.

## Async submission and handoff

Use the [submission index](submission.md) as the controller receipt: register an unsent initial request, claim local ownership, retain actual launch/native/monitor metadata, and commit `begin` before terminal input. Record acceptance with `receipt` and evidence. One explicit persistent index is shared by every controller in the run. It retains active_request, job/round, pinned resources, attempt IDs, uncertainty, deadlines and immutable history. Unknown metadata is null. Watch events/reviews retain observation and question handling times separately.

On recovery, [runs.py recover](runs.md) gathers registered jobs and pending reviews from both old and new watches; check its errors and waiting-user notes first. `jobs.py list` / `recover` provide detailed job/round and response validity. These commands neither submit nor claim ownership. A missing send reply or expired lease does not turn uncertainty into rejection. Acquire a new lease only after expiry/release and confirm the old controller has stopped input; local fencing protects index writes, not arbitrary herdr/tmux calls. Use `activate` to select one direct follow-up before sending it. Preserve transport errors and the previous request/attempt history.

For terminal jobs also retain the layout choice, child and parent workspace/tab/pane IDs, actual viewer state, exact viewing argv and any tmux `-S` path or `-L` name; see [display and viewing](transports.md#display-and-viewing). Include a concrete viewing entry in the startup update unless the user requested quiet background execution. Reconnecting a viewer preserves the existing round; detaching it does not end the job or release the controller's monitoring responsibility. Honor an explicit request to retain the terminal after completion and record the retained resources instead of applying automatic cleanup to them.

The protocol helper prepares/validates files and plans cleanup. The optional observer performs bounded polling and local event deduplication; it does not schedule agents, automatically notify a model, provide distributed locks, approve actions or cancel targets. If the controller must end its turn, either establish an actual supported monitor or give the user a retained handle and accurate instructions to resume/check it later. Do not say “I will keep monitoring” without a running mechanism.

On resume, read the index's current request, submission receipt and resource ownership record. Validate any result before attaching. Resource records must carry the same job ID, exact pane IDs and consistent workspace IDs. Confirm the session/pane still belongs to that job; names can be reused after termination. If records are missing or invalid, inspect read-only state and reconstruct them only from verified information instead of guessing or closing resources. Legacy unindexed jobs are not automatically safe to register as unsent work.

## Acceptance, duplicate execution and retry budget

| Observation | Meaning | Next action |
|---|---|---|
| Valid matching response immediately after send | Task may have completed quickly. | Consume/verify it; `done` is acceptable. |
| CLI success plus active execution | Accepted, still in progress. | Continue independent work and scheduled supervision. |
| Explicit rejection before any input was sent | Not accepted. | Fix readiness/parameters; retry once input is ready. |
| CLI timeout/disconnect/stalled return, no response yet | Acceptance may be uncertain. | Inspect exact target state/output; do not resubmit blindly. |
| Wrong IDs in the result | Misrouted/stale response. | Preserve it, stop treating it as completion, investigate the writer. |
| Missing/invalid report but task output visibly complete | Reporting failure. | Request a report-only rewrite when input is ready. |

A send is not idempotent. The same prompt can execute twice if resent after a timeout. JSON round IDs prevent accepting a stale response; they do not prevent duplicate side effects by an agent. If acceptance remains uncertain, reconcile artifacts and seek a decision where needed before starting replacement work.

Reporting repair has a default budget of **two attempts**, each bounded (for example 120 seconds) and within the total job deadline. Preserve invalid raw output before clearing its final filename. Ask for only the final JSON for the same request/path/IDs, with no task re-execution. An existing valid response is never a repair target.

If repair is exhausted, inspect screen evidence and independently verify any artifacts. Report the protocol failure and the verification actually achieved. Screen fallback must not silently turn an unverified task into success. Starting another target requires first establishing that the original cannot continue writing.

## Deadlines and cancellation

Historical timing examples: trivial report ≈12 s; small project ≈150 s; multi-round Rust work ≈11 min. Use these as planning data, not promises. Start with 5 min per round and 30 min total; adjust deadlines to task size and user constraints. Keep the supervision cadence above even for long jobs. Use bounded calls and periodic progress updates. A pending approval does not automatically pause the job deadline.

- A client wait timing out does not terminate the task. Inspect state, output and result. Extend deliberately if useful work is progressing, or initiate cancellation; record the choice.
- User cancellation is not a target `error` result. Record it in controller state. Use the target's documented interruption command, then check for input readiness/process exit and any last response/partial changes.
- A target may emit an `error` describing the interruption, or emit no response at all. Preserve either observation; the controller's cancellation record remains distinct from business failure. Check owned tool processes and delayed writes, not just the agent's return to its prompt.
- Do not send `/exit` into a working agent and assume it is a stop signal. Do not close a container while a tool may still be writing unless termination is the explicitly chosen cancellation action.
- If the process dies, retain evidence and report partial effects. Do not fabricate a target success/error envelope to satisfy validation. A controller failure record is separate. If only cwd disappears while the target is alive, it can still publish an error; basic result readback and cleanup remain available, while file verification and new work require a restored cwd.
- If no valid response exists after timeout/death, `prepare --previous` intentionally fails. After confirming the old process cannot write and reconciling side effects, prepare a new initial job with the real parent depth; include a concise recovery task and link the old handle in the controller's receipt.

## Concurrency, nested work and shared files

Only one active round per target, and one submission owner per job. Multiple agents can run concurrently with separate round directories. For overlapping writes, serialize or use separate checkouts/worktrees and explicitly integrate their changes. Do not overwrite the user's existing uncommitted work during integration.

Agents communicating through this protocol exchange files or have the controller relay messages. There is no direct agent-to-agent messaging transport. State which artifact or response the next agent should read; never imply a direct channel exists.

Depth comes from the prompt. Pass it explicitly when preparing a child. The helper guards its own launch preparation, but arbitrary manual launches can bypass it; this is not a process-level enforcement boundary. A deepest-level agent can still finish local work. Include delegated effects in the parent's report and independently verify them.

The session owner maintains the inventory of active jobs. Child jobs own their panes/workspaces only; they must not stop a shared session. Nested agents cleaning their child panes must leave their own controller pane intact.

For OpenCode with OMO, include plugin-native background task/session IDs, teams and continuation state in this inventory. `bg_...`, `ses_...`, pane IDs and protocol IDs are different identities. A parent idle event or final JSON does not prove plugin children stopped. Settle owned writers before publishing, and inspect any post-result activity. OMO's `/stop-continuation` can clear project boulder state and asynchronously cancel descendants; apply the [OMO stop procedure](../../agent-opencode/references/omo.md#受控轮次后台任务与停止) instead of treating it as a generic harmless cleanup command.

## Verification and failure attribution

Check matching identity/schema first, then artifacts and the actual answer. `validate --check-files` checks listed files individually and verifies declared deletions are absent. It does not guess by filename extension. Compare against initial `git status --porcelain`/diff or a directory inventory, including untracked files.

For reviews/answers with no edits, assess reasoning against the referenced source. For implementation tasks, inspect changes and run appropriate tests or a smoke check. Neither a nonempty file list nor a target's claimed passing tests proves success. Preserve a failed check as a verification failure and request a new corrective round if appropriate.

For a fixed delivery version, use [delivery snapshots and verification](delivery.md). Capture only after the assigned writers yield the agreed scope; after capture, verification uses its own copy. Each verification attempt gets fresh build/cache/evidence directories, explicit command outcomes and artifact hashes. A scoped before/after diff does not establish authorship, and a component pass does not validate an uncaptured integrated tree.

Source paths are constrained to cwd, but trusted agents may intentionally create external artifacts for a user task. Such scope must be explicit and verified separately or use a suitable cwd. The helper is not a sandbox, malware scanner or guarantee against racing filesystem changes.

## Cleanup by ownership

Run cleanup only after completion/cancellation, not at async handoff. Read `resources.json` and get the helper's `cleanup-plan`; it emits data, never executes commands. Recheck resource identity against live state and the active-job inventory.

| Resource situation | Allowed cleanup |
|---|---|
| Insider new workspace (default) | Exit owned child; close only the recorded child workspace after checking its contents. Parent workspace/tab/pane and session remain. |
| Insider new tab | Exit owned child; close only the recorded child tab. Parent tab, workspace and session remain. |
| Insider explicitly requested split | Exit owned child; close that pane only. Parent pane/tab/workspace and session remain. |
| Workspace created in a shared isolated session | Exit owned target; close own workspace when no other job/user resource is inside. Do not stop session. |
| Session created exclusively by this controller | After all its jobs finish, exit owned agents, close owned workspaces, stop that named session, then delete it. |
| Dedicated tmux session | Exit target using its actual binding; kill that exact owned session. Never `kill-server`. |
| User-provided existing agent/pane/session | Do not exit/close it unless cleanup was explicitly authorized and ownership recorded. |
| Startup failed after a subset of allocations | Reconcile the startup journal; clean only resources whose creation succeeded. |

All herdr operations include the recorded `--session`. tmux session cleanup uses an exact-match target (`=name`) and pane cleanup uses a concrete `%id`. Do not use focus, name prefixes or glob matching to select resources.

Exit commands come from the target dictionary/current TUI help. If the target is blocked in a dialog, inspect and resolve/cancel it first. A failure at one cleanup step is not permission to kill a broader parent container. Record leftovers with their exact IDs; retries recheck whether a resource is already gone and whether its name has been reused.

Ownership flags are the controller's recorded evidence, not proof of exclusive use. Before closing any workspace/tab/session, ensure it contains no other active job or user resource. The helper rejects insider ownership of the parent workspace/tab when parent identities are supplied and requires those identities for new container ownership. Live creation evidence and tab/pane membership still need checking. A copied resource record on a follow-up is a snapshot; the active-job inventory determines whether cleanup is now allowed.

Keep request/results, verification evidence and receipts until reporting/recovery is complete. Then remove only exact round directories created for this job, if retention is no longer needed. Do not run broad deletion under `/tmp`, remove user deliverables, or delete another job's results. The helper does not clean completed jobs or execute its plan; its only automatic directory rollback is a new, unreturned round whose preparation failed.

For [Codex](../../agent-codex/references/native.md), retain the exact native session ID and never use `resume --last` for concurrent jobs. Settle owned native children and background commands before publication. Exiting one owned CLI must not terminate the shared app-server daemon or other user sessions. A noninteractive process timeout needs separate cancellation and descendant verification.
