---
name: agent-orchestrator
description: "Drive terminal agents such as omp, hermes, opencode and codex through herdr or tmux. Use for cross-tool delegation, multiple terminal agents, or continuing an existing delegated job, including asynchronous work."
---

# Agent Orchestrator

Use a terminal transport to submit a task, collect a response for the exact round, and independently verify the outcome. This implementation covers local agents sharing a filesystem. Remote hosts need an explicit file-transfer protocol.

## Read the relevant instructions

- Before the first submission: [response protocol](../agent-controlled/references/protocol.md).
- For selecting and starting a transport: [transport recipes](references/transports.md); read only the selected mode.
- For follow-ups, async jobs, failures, concurrency or cleanup: [lifecycle and recovery](references/lifecycle.md).
- For scoped task packets, repeated terminal supervision and native token accounting: [efficiency tools](references/efficiency.md). Load the needed section; avoid replaying full logs or unrelated reference files into context.
- For fixed source handoffs, independent build/cache directories and command/artifact evidence: [delivery verification](references/delivery.md).
- For author/verifier/rework packets, dependency gates and test ownership: [task workflow templates](references/task-workflow.md).
- Before marking work completed: [acceptance and host settling](references/completion.md), including the selected host's lifecycle checks.
- Target dictionaries: [omp](../agent-omp/SKILL.md), [hermes](../agent-hermes/SKILL.md), [OpenCode / OMO](../agent-opencode/SKILL.md), [Codex](../agent-codex/SKILL.md). In the current user's environment `opencode` defaults to oh-my-opencode/oh-my-openagent; pure requires `--pure`. Both use herdr kind `opencode`. Read that dictionary before selecting startup arguments. For other kinds, check the executable's help and actual TUI bindings; herdr support alone does not establish a tested dictionary.

Resolve references relative to this skill's directory. If a skill loader is unavailable, read the files directly. Install all six sibling skills together.

## Invariants

1. The prompt contains the **actual absolute result path and complete reporting contract**. Environment variables are optional hints; they cannot override the current prompt.
2. Every round has a fresh `round_id` and result path. A matching valid response resolves that round: `success`/`error` ends the requested work; `blocked` requests a decision. Replying to `blocked` creates a **new round**, even in the same TUI conversation.
3. One in-flight round per target. `idle`/`done` means input may be accepted, not task success. Never submit while the previous round is working or its submission outcome is uncertain.
4. Record resource ownership and parent identity. Insider mode may own a newly created workspace, tab or split pane; it never owns the caller's containers or session. Only an isolated session's creator can retire it, after every job using it has finished.
5. Carry depth from the received contract, not a shell variable default. Root is 0; default `max_depth=3` allows A(0) → B(1) → C(2). C can work locally but cannot create D.
6. Preserve the user's scope, prior authorization and execution permissions. Ask only for genuinely missing decisions or required approval. Do not change approval settings to bypass a prompt.
7. After accepted submission, advance independent controller work while supervising every active child. Use the read-only observer for repeated checks and handle its events; native approvals can appear without a `blocked` hook or result file. Resume manual supervision if the observer stops.

## Normal workflow

### 1. Prepare the task and workspace

Specify the result, acceptance checks, constraints, target kind and writable scope. Send the child only relevant facts and source/evidence paths, not the parent's conversation or other agents' full logs. A read-only answer may be the entire deliverable. For edits, record an initial filesystem/Git baseline so preexisting user changes are not attributed to the agent.

For file deliveries, define [configuration coverage](references/delivery-configurations.md) before dispatch: the actual delivery target, applicable build/feature/platform/runtime gates, owners, artifact checks and uncovered items. Development test counts alone do not accept a differently configured delivery.

Read-only agents can share a cwd. Give concurrent writers independent worktrees/directories or explicitly disjoint file ownership; distinct result files do **not** prevent source-file conflicts. Creating a terminal workspace does not isolate a Git checkout.

Set `ORCH_TOOL` to the absolute path of `scripts/protocol.py` beside this skill. Recipes require Python 3.10+ and Bash. Write the task into a UTF-8 file with a file-editing tool; do not interpolate user text into shell source.

```bash
python3 "$ORCH_TOOL" prepare --cwd "$PROJECT_CWD" \
  --parent-depth 0 --task-file "$TASK_FILE" --brief
```

For nesting, replace `0` with **your own received depth** and pass the received `--max-depth`. Sibling tasks, follow-ups and retries do not increment your own depth. The helper refuses a child at the limit before creating round files.

The returned JSON contains `request_path`, `prompt_path`, `result_path`, IDs and depth. Persist the returned paths; do not guess them. The helper creates a private round directory and a prompt without literal line breaks. Its JSON-encoded task preserves multiline text and shell metacharacters.

Initial preparation defaults to a private persistent run and returns `run_path`/`index_path`. For multiple children, initialize one run with `scripts/runs.py init` and pass `--run "$RUN_FILE"` to their prepares. Follow-ups inherit it. See [persistent runs](references/runs.md) for storage overrides and explicit temporary experiments. Register each unsent initial request with `scripts/jobs.py register` using that run's index, then claim before launch/input. Follow the [submission index recipe](references/submission.md); `prepared` is not proof of submission.

For a structured brief, replace `--task-file` with `--task-packet "$PACKET"`: required objective/scope/acceptance and optional inputs/known_facts/constraints. `--brief` removes the redundant controller-facing task echo; the child still receives the complete contract. See [task packets](references/efficiency.md#small-task-packets).

### 2. Start and record the target

Choose insider herdr only when the current pane and session are identifiable; otherwise create an isolated herdr session if available, or use tmux. Read the selected [transport recipe](references/transports.md). Confirm server readiness, pane creation and target interactive readiness.

In an identified herdr session, default to a **new workspace per child**, using `--no-focus` to keep the controller's current page. If the user prefers tabs, create a new tab in the identified parent workspace. Split the parent tab only on an explicit request for a split/side-by-side layout. Record the actual workspace/tab/pane IDs and give the exact viewing command; a selectable page is not an already focused page. For isolated sessions, provide the attach command. See the [display policy](references/transports.md#display-and-viewing). Viewing an existing task must not launch another agent or resubmit its prompt.

Name each new workspace/tab by its task, in the user's language: for example `实现上传重试` or `验证上传兼容性`. Pass the label explicitly when creating the page; add a short discriminator for duplicate purposes. Keep the unique internal agent name separate from this readable label. Related follow-ups retain their page and label. See [page naming](references/transports.md#name-pages-by-their-task) for creation, initial-tab naming and owned-page renaming.

Record returned IDs immediately after each allocation, including partial startup. After startup use `record` to persist ownership beside `request.json`. Record the exact session even for insider commands. Do not silently switch transport after a task may have landed.

For Codex, provision the private round-root as an additional writable directory when using workspace-write; read its dictionary for exec event streams and exact-session recovery.

Also retain the executable, launch argv and any target profile in the controller receipt. For OpenCode record `opencode_mode=omo|pure`, current internal agent and native session; preserve that profile on follow-up/recovery. Plugin workflows may auto-resume existing plans or keep background tasks running after the main turn appears idle; inspect the target-specific lifecycle before treating it as ready.

### 3. Submit one round

Before input, establish observation for the current round: initialize its watch,
start the observer through a managed handle, confirm a successful check of every
target, and register each monitor owner/expiry. Use the
[supervision checkpoint](references/supervision.md#checkpoint-before-work-and-input)
to check actual observer lifetime and target freshness. Prepare this before the
first prompt; a fast task can finish before a later observer starts.

Read `prompt.txt` as data and pass it as one argument; no `eval`, command-string concatenation or unquoted expansion. Every herdr command uses the recorded `--session`.

Before the transport command, commit `jobs.py begin` with the current revision and live ownership token. It reserves an attempt as `uncertain`; save its ID and preserve the transport output. Follow the [submission index recipe](references/submission.md) for exact arguments.

```bash
ORCH_PROMPT=$(cat "$PROMPT_FILE")
herdr --session "$SESSION" agent prompt "$AGENT" "$ORCH_PROMPT"
```

Async submission is the default example. With `--wait`, use a bounded timeout and still validate the result separately. Record acceptance through `jobs.py receipt` with evidence: valid matching response or accepted/active target, explicit not-sent evidence, or uncertainty. The request's `prepared_at` and a CLI success alone are not confirmed acceptance.

Immediately check the current result and `agent get`/`agent list`. Fast work may already be `done` with a valid result. If there is neither a response nor evidence of accepted/active execution, inspect the screen and submission error before resending.

Once accepted, proceed with independent analysis, implementation within your file ownership, or verification preparation. Identify which later steps actually depend on the child. Keep local work interruptible so supervision continues; separate terminal pages do not isolate file writes.

### 4. Work concurrently, supervise and interpret

Keep the [event supervisor](references/efficiency.md#event-supervisor) established before submission running through its managed handle. It checks results, state and UI about every 15 seconds, persisting only meaningful events and evidence. Before each independent work block, run `scripts/supervision.py --run "$RUN_FILE"` and obey its work budget/recheck time. A zero budget calls for event handling or coverage repair first; it never grants approval. Consume new events with a saved cursor; keep normal attention-response gaps within 30 seconds. Suspected dialogs and periodic changed-screen reviews need controller judgment even if the hook says `working`.

On resume and between work blocks, use `scripts/runs.py recover --run "$RUN_FILE"` to gather [pending reviews](references/efficiency.md#durable-pending-reviews) from current and old watches, independently of delivery cursors. Keep watch_path + seq together; new watches can reuse local sequence numbers. Record decisions already asked as `waiting_user`; record `handled` after live reconciliation. Read existing notes before repeating a question. A partial recovery reports `complete=false` and errors; it is not an empty queue. Legacy standalone watches still use `watch.py pending`. These receipts do not authorize or perform terminal input.

Retain the observer process handle and expiry; renew it while children remain active. If it is unavailable, failed or expired, use the [manual supervision loop](references/lifecycle.md#controller-work-and-supervision) immediately. Event files alone cannot wake a controller whose turn has ended. Read detailed screen evidence when the compact event is insufficient, without replaying unchanged screens on every poll.

Handle the actual dialog within existing authorization, or promptly relay the missing decision and continue other independent work. A native approval resumes the current round; a valid `blocked` JSON needs a new round for its answer. Only enter bounded waiting when the next useful work depends on the child and no independent work remains; after each short wait, sweep all active children.

After a native response, inspect its live outcome and [record that exact occurrence immediately](references/supervision.md#record-each-confirmed-outcome-before-the-next-item), before another dialog or a full recovery scan. Preserve separate action/resolution evidence; sent input alone leaves resolution uncertain. New occurrences remain open.

```bash
python3 "$ORCH_TOOL" validate --request "$REQUEST_FILE"
```

Exit 0 means a **protocol-valid response**, including `blocked` and `error`; inspect `status`. Exit 2 means a required file is missing; exit 3 means invalid data or another local error. Distinguish a missing response from a lost request/cwd using the error text.

| Current round result | Target state | Action |
|---|---|---|
| Missing | `working` | Continue independent work and periodic UI checks within the deadline; do not resend. |
| Missing | `idle` / `done` | Read output: plain-text question, missing report, or submission failure. |
| Missing | UI `blocked` | Inspect the dialog; resolve decisions/approvals within existing authorization. |
| Invalid / partial | Any | Preserve evidence. Wait while writing/working; request repair only when input is ready. |
| Valid `success` | Any | Verify output and side effects. Wait for input readiness before another round. |
| Valid `error` | Any | Record failure and partial changes; choose bounded recovery or report the cause. |
| Valid `blocked` | Any | Read the question once, resolve it, wait for input readiness, prepare a new round. |
| Any | dead / `unknown` / disconnected | Inspect process/session and result separately; do not infer success or blindly restart. |

Use deadlines (baseline: 5 min per round, 30 min per job) and keep the user informed during long work. The observer only reports events; the controller retains monitoring, decision and cancellation responsibility. A CLI timeout does not cancel the target. See [lifecycle and recovery](references/lifecycle.md).

### 5. Verify and follow up

```bash
python3 "$ORCH_TOOL" validate --request "$REQUEST_FILE" --check-files
```

This checks declared file existence/deletions and containment. It does not prove correctness, attribution, absence of unreported edits, or test truthfulness. Compare the baseline, inspect the diff/answer, and run checks appropriate to the task. Empty file arrays are legitimate for explanation, review and no-change tasks. For file-producing work, independently confirm the requested artifact exists and is correct.

For file-producing work that needs version-bound acceptance, follow [delivery verification](references/delivery.md): retain a scoped initial baseline, capture the agreed handoff, then run the explicit verification plan in a fresh source/build/cache copy. Check the actual scoped diff separately from child-declared files. Preserve failed attempts and bind the verifier report to its snapshot and evidence paths.

For a clarification answer, revision, or recovery after a valid error, write a new task file:

```bash
python3 "$ORCH_TOOL" prepare --previous "$REQUEST_FILE" --task-file "$FOLLOWUP_FILE" --brief
```

This inherits job, cwd, depth and run storage, copies the resource record, and generates new round IDs/paths. It requires a valid response to the preceding round. After a success response, record accepted/rejected review evidence with `jobs.py accept` before activating its successor; blocked/error continuation needs no success acceptance. With the current job lease, use `jobs.py activate` to select this direct successor before `begin` and transport input. A competing activation fails; recover the recorded active request and leave any losing preparation unsent. `prepare` alone does not serialize submissions. See [per-round review](references/completion.md#per-round-review-and-follow-up) for historical queries and backfill.

Keep the same native conversation for a related continuation and send the new requirements plus necessary facts; update the watch to the new active request. Record actual native call counters and attempt IDs using the [usage ledger](references/efficiency.md#native-usage-ledger), including unmetered attempts as null counters. Do not estimate tokens from bytes or double-count cached input.

### 6. Finish or hand off

Record independent acceptance with `jobs.py accept` and a fresh scoped native observation with `jobs.py host`. Use [completion checks](references/completion.md): `jobs.py check-completion` reports publication, acceptance and host settling separately; `close` with outcome `completed` enforces all three. Read-only answers use answer acceptance; file-producing tasks bind a passing delivery attempt. Unknown background/child activity stays unknown. A historical completed job does not prove current input readiness.

Report verified success, failure, cancellation, or an async handle with its current state. Continuing async jobs retain their sessions and round records; the controller's turn ending is not a cleanup signal.

When the job is finished or cancellation is acknowledged:

```bash
python3 "$ORCH_TOOL" cleanup-plan --request "$REQUEST_FILE"
```

This returns a reviewable plan and **executes nothing**. Recheck identities, ownership and other active jobs. Use the target's actual exit command, then close only the owned child workspace, tab or pane. Preserve the parent pane, tab, workspace and session. Retain evidence until verification/reporting are complete. See the lifecycle reference for partial-startup recovery.
