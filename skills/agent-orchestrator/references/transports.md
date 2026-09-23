# Transport recipes

Read only the mode being used. These are Bash recipes with variables bound to **actual returned IDs and paths**, not a script to paste without filling them in. Check installed `--help` when CLI behavior differs. Workspace/tab syntax was checked against local herdr 0.9.1. Validate each live target using the selected mode's checks below and the [lifecycle rules](lifecycle.md); CLI syntax alone does not prove readiness, completion or correct cleanup.

## Select a mode

| Condition | Mode | Isolation and ownership |
|---|---|---|
| `HERDR_ENV=1`, herdr available, current pane/session positively identified | Insider | New tab in the verified parent workspace by default; new workspace if requested. Own only newly created child resources. |
| herdr available, no usable current pane | Isolated | Start a unique named headless session and create its first workspace. |
| herdr unavailable or unusable before submission, tmux available | tmux | Dedicated named session; no agent state machine. |
| Neither usable | Stop with the concrete missing dependency. | Do not assume a target started. |

Use `command -v herdr`, `command -v tmux`, and the target executable to check availability. An environment variable alone does not prove a live server or current pane. If a startup fails, record/clean partial resources before another attempt. After a potentially accepted submission, recover the existing job before considering another transport.

## Display and viewing

Display is independent of transport, asynchronous execution and resource ownership. Interpret the user's preference in ordinary language; these are workflow choices, not new CLI flags:

| Preference | Behavior |
|---|---|
| Unspecified | In the identified herdr session, create a new tab in the parent workspace with `--no-focus`. If the parent cannot be positively identified, create an isolated workspace/session and give a concrete attach command. |
| New tab | Create a new tab in the identified parent workspace with `--no-focus`; preserve the parent tab. |
| Show the agent / switch to it | Create the new workspace/tab, then focus it when the user asks to switch. If a separate session is necessary, attach in an available, authorized user-facing terminal or give the exact attach command. |
| Split / side by side | Split the identified parent pane with `--no-focus`; this is an explicit layout choice. |
| Background / do not disturb | Keep the new workspace/tab unfocused in an identified session; otherwise use an isolated session. Retain viewing details; honor an explicit separate-session request. |

`--no-focus` preserves the user's current page and input focus; the new workspace/tab is available to select, not automatically on screen. A headless session has no user-facing viewer just because it contains a pane. No verified current session/parent means no guessing a focused container. This project's same-workspace/new-tab default is intentional, including when a generic herdr guide recommends sibling splits.

After interactive startup, report the agent/profile, session, workspace/tab/pane, and whether the target page was focused or is available to switch to. Fill commands with actual recorded values and shell-quote them; do not hand the user unresolved `$SESSION` placeholders. For explicit background requests, keep these details in the receipt and include the task handle in the next status update.

**herdr:** in an attached session, select the recorded new workspace or tab with the appropriate command (execute a focus command only when the user requests the switch):

```bash
herdr --session "$SESSION" workspace focus "$WORKSPACE"
# For a child created as a new tab:
herdr --session "$SESSION" tab focus "$TAB"
```

For a separate session, attach from a user terminal:

```bash
herdr session attach "$SESSION"
```

Identify the target workspace/pane as well, since a session can contain several agents. Attaching exposes the interactive UI; do not describe it as read-only. A new desktop terminal window needs a known installed terminal launcher and a usable desktop session; there is no generic window-opening command in this project. Do not run the attach command in the controller's noninteractive command runner and claim that a window appeared on the user's desktop.

**tmux:** run one of these in a separate user terminal, matching the server used at startup:

```bash
# Default server only:
tmux attach-session -r -t "=$SESSION"
# When the job was started with an explicit socket:
tmux -S "$SOCKET" attach-session -r -t "=$SESSION"
```

For `-L` startup, retain and use that exact socket name instead of substituting `-S` or the default server. The tested tmux 3.7c defines `-r` as read-only plus ignore-size, so the viewer cannot type into the agent or change the working terminal size. Do not use `-d` to detach other clients. Prefer an outside terminal to nesting an attach command inside the controller's existing tmux client. Detach the viewer using its configured detach binding (default `Ctrl+b`, then `d`); do not send the agent's `/exit` to stop watching.

Store the display preference, actual viewer state, target IDs and viewing argv in the controller receipt. For tmux, `record` requires exactly one of `--tmux-socket /absolute/path`, `--tmux-server name`, or `--tmux-default-server`; it persists the selector in `resources.json`, and follow-ups, observation and generated cleanup commands preserve it. Use that same selector for input and viewing. Legacy records without a selector remain readable, but cleanup refuses to guess: recover the verified selector from the original receipt and preserve an audited corrected resource record before cleanup. Check live identity before attaching to a retained handle; an already cleaned task has logs to inspect, not a terminal to recreate automatically.

Watching does not transfer input ownership. If the user wants to type or answer an approval directly, pause controller input until the handoff is settled, then reconcile actual state before continuing. Closing/detaching a viewer is not task cancellation; opening one is not proof of readiness or completion. Noninteractive routes such as `codex exec` expose logs/events rather than a live TUI; choose the interactive route initially when a visible agent terminal is required.

## Shared preparation

First run `prepare` from the main skill. Bind `PROJECT_CWD`, `REQUEST_FILE`, `PROMPT_FILE` and `RESULT_FILE` to the returned values; `ORCH_TOOL` is the helper path. Use unique lowercase internal agent names such as `orch-` plus a UUID suffix. Agent names must match `[a-z][a-z0-9_-]{0,31}`.

### Name pages by their task and host

Set `LABEL` to **task purpose + agent type** in the user's language, such as `实现上传重试 · Codex` or `验证上传兼容性 · OpenCode (OMO)`. Every new herdr workspace/tab receives `--label "$LABEL"`. For duplicate purposes, add a short discriminator (`验证上传兼容性 · Codex · 2`). The task purpose comes first, so the user can scan the page list without opening terminals. The OpenCode suffix must reflect the actual OMO or pure profile.

Use the shared read-only planner before allocation (the runner uses this same helper):

```bash
python3 "$(dirname "$ORCH_TOOL")/pages.py" --session "$SESSION" --cwd "$PROJECT_CWD" \
  --purpose "验证上传兼容性" --kind codex \
  --parent-pane "$PARENT_PANE" --parent-tab "$PARENT_TAB"
```

It checks live parent pane/tab membership and existing titles, then returns `label`, `layout`,
`create_argv` and ownership flags. Execute `create_argv` as an argv array, never with `eval`.
The default `auto` chooses a tab with complete parent IDs; no parent chooses a workspace in the
explicitly selected isolated session. Partial/stale/mismatched parent identity fails; do not
silently drop it and guess another page. Use `--layout workspace` for an explicit new space,
or `--opencode-mode pure` for a pure-profile title. The planner reads metadata only and does
not allocate, focus, launch, or reserve labels. Serialize sibling allocations under the controller;
independent controllers can race on display labels, which are never identity or ownership keys.
Record the plan before execution and returned IDs immediately afterwards. Verify the child
`pane get` response's `tab_id` and `workspace_id` before starting its agent.

The visible label and the unique internal `AGENT` name are separate. Store the chosen label with the exact resource IDs in the controller receipt; labels are never resource selectors. Pass labels as quoted data, just like prompts.

Keep the page and label for a related follow-up. If its purpose changes, rename only the verified owned workspace/tab using `workspace rename "$WORKSPACE" "$LABEL"` or `tab rename "$TAB" "$LABEL"`, with the recorded `--session`. Update only the display receipt; immutable `resources.json` stays unchanged. A different host/profile requires a fresh launch with its own identity, never just a renamed existing host. Viewing an existing job uses its IDs and leaves its label and task unchanged. For a new workspace, give its initial tab the same task label after reading the returned tab ID.

Resolve launch arguments from the target dictionary before `agent start`. [OpenCode / OMO](../../agent-opencode/SKILL.md) has two profiles using the same kind: default `opencode` loads configured OMO; explicit pure uses `-- --pure` after the herdr startup options. In tmux pass `--pure` as a separate executable argument. Keep the chosen profile on fallback/resume. Pure disables external plugins, including the locally installed herdr reporting integration, so verify actual TUI readiness rather than assuming hook-based state is available.

Do not put raw task text into an executable shell command. This safe read treats even backticks and `$()` in the file as data:

```bash
ORCH_PROMPT=$(cat "$PROMPT_FILE")
```

The helper generates one physical line with escaped task newlines. Passing `"$ORCH_PROMPT"` as one argument does not recursively evaluate its contents. Do not `eval` it. If a prompt exceeds the installed transport/TUI input limit, send a short, single-line instruction to read the concrete **`prompt.txt` path**, including the request/result paths and identity for cross-checking. `prompt.txt` contains both the reporting rules and the task; `request.json` alone does not contain the reporting rules. Confirm the target can read the complete prompt before work. Do not silently truncate it.

## Insider herdr

Use current runtime/session metadata and live reads to bind `SESSION`, `PARENT_PANE`, `PARENT_WORKSPACE` and `PARENT_TAB`. If the session/parent cannot be identified, choose isolated mode instead of guessing the default or focused session. Confirm CLI/server support before relying on tab commands.

```bash
herdr --session "$SESSION" status
herdr --session "$SESSION" pane get "$PARENT_PANE"
```

Require the returned `.result.pane.tab_id` and `.result.pane.workspace_id` to match the exact parent IDs; a shared workspace prefix is insufficient. Observation or approval handling must not focus a child or jump back to a fixed parent page.

**Default: a new tab in the parent workspace.**

```bash
herdr --session "$SESSION" tab create --workspace "$PARENT_WORKSPACE" \
  --cwd "$PROJECT_CWD" --label "$LABEL" --no-focus
```

Read `TAB` and `PANE` from `.result.tab` and `.result.root_pane`; `WORKSPACE` is the verified `PARENT_WORKSPACE`. Start the agent, then record:

```bash
python3 "$ORCH_TOOL" record --request "$REQUEST_FILE" --mode insider \
  --session "$SESSION" --agent "$AGENT" --pane "$PANE" --workspace "$WORKSPACE" \
  --tab "$TAB" --parent-pane "$PARENT_PANE" --parent-tab "$PARENT_TAB" \
  --owns-agent --owns-pane --owns-tab
```

Own the new tab, not the parent workspace. The helper requires exact, distinct parent/child tab IDs. Before recording, check that the returned pane actually belongs to that tab; workspace prefixes alone cannot prove tab membership. If the installed server cannot create the requested page, report the limitation rather than silently splitting the parent.

**Explicit workspace layout.** Create it without changing the parent page:

```bash
herdr --session "$SESSION" workspace create --cwd "$PROJECT_CWD" --label "$LABEL" --no-focus
```

Read `WORKSPACE`, `TAB` and `PANE` from the returned `.result.workspace`, `.result.tab` and `.result.root_pane` identities. Record them immediately, including if agent startup later fails.

```bash
herdr --session "$SESSION" tab rename "$TAB" "$LABEL"
herdr --session "$SESSION" agent start "$AGENT" --kind "$KIND" --pane "$PANE"
python3 "$ORCH_TOOL" record --request "$REQUEST_FILE" --mode insider \
  --session "$SESSION" --agent "$AGENT" --pane "$PANE" --workspace "$WORKSPACE" \
  --tab "$TAB" --parent-pane "$PARENT_PANE" --parent-tab "$PARENT_TAB" \
  --owns-agent --owns-pane --owns-workspace
```

The helper requires the owned workspace to differ from the recorded parent pane's workspace. It never allows insider session ownership. The new workspace's initial tab is covered by workspace ownership.

**Explicit split request only:**

```bash
herdr --session "$SESSION" pane split --pane "$PARENT_PANE" \
  --direction down --cwd "$PROJECT_CWD" --no-focus
```

Read the new ID from `.result.pane.pane_id` into `PANE`, record it immediately, then:

```bash
herdr --session "$SESSION" agent start "$AGENT" --kind "$KIND" --pane "$PANE"
python3 "$ORCH_TOOL" record --request "$REQUEST_FILE" --mode insider \
  --session "$SESSION" --agent "$AGENT" --pane "$PANE" \
  --parent-pane "$PARENT_PANE" --owns-agent --owns-pane
```

Check target readiness and JSON error envelopes, not just shell exit code. Split cleanup closes only the child pane. For all three layouts, preserve the parent pane/tab/workspace and session, and honor explicitly retained terminals.

## Isolated herdr

Generate and retain a unique `SESSION` before starting anything. Start this command using the execution harness's managed background-process facility:

```bash
herdr --session "$SESSION" server
```

It is a long-running server, not a foreground initialization command that returns immediately. Do not use a bare `&` where the harness rejects or kills unmanaged children. Retain the process handle. Startup is confirmed only after:

```bash
herdr --session "$SESSION" status
```

reports a running server. Retry readiness for a bounded startup window (for example 30 seconds) and inspect process failure if it does not become ready. The session owner records ownership immediately. Create the first workspace:

```bash
herdr --session "$SESSION" workspace create --cwd "$PROJECT_CWD" --label "$LABEL" --no-focus
```

Record the returned workspace ID as `WORKSPACE`, `.result.tab.tab_id` as `TAB`, and `.result.root_pane.pane_id` as `PANE`. A headless caller has no usable `--current` pane; do not use `split --current` for the first pane.

```bash
herdr --session "$SESSION" tab rename "$TAB" "$LABEL"
herdr --session "$SESSION" agent start "$AGENT" --kind "$KIND" --pane "$PANE"
python3 "$ORCH_TOOL" record --request "$REQUEST_FILE" --mode isolated \
  --session "$SESSION" --agent "$AGENT" --pane "$PANE" --workspace "$WORKSPACE" --tab "$TAB" \
  --owns-agent --owns-pane --owns-workspace --owns-session
```

For a later independent job sharing this owned session, record its own workspace/pane but **omit `--owns-session`**. The creator must maintain the active-job inventory and wait for all jobs before session teardown. Never stop/delete a preexisting user's session because its name resembles an orchestration name.

Environment injection is optional. If needed for an older target, `workspace create` and `pane split` accept `--env AGENT_CONTROLLED=1 --env "AGENT_RESULT_FILE=$RESULT_FILE"`. These values become stale on follow-up; the new prompt overrides them. New requests do not require injection.

## herdr submission, observation and UI

Wrap business prompt delivery with the [submission index](submission.md): claim the
job, commit `begin` before input, then record an evidence-backed `receipt`. Apply the
same sequence to tmux below. Transport commands alone do not update the index; a lost
reply leaves uncertainty and must not trigger another prompt.

### Startup timeout with a command still in the shell

On the tested herdr/fish combination, `agent start` typed `hermes` or `omp` but failed to execute it, then timed out. Before restarting or switching transports, inspect:

```bash
herdr --session "$SESSION" pane process-info --pane "$PANE"
herdr --session "$SESSION" pane read "$PANE" --source recent --lines 80
```

Only if the foreground process is still the shell **and** the current editable line is the exact startup command just submitted, send one literal carriage return as text. For Bash:

```bash
herdr --session "$SESSION" pane send-text "$PANE" $'\r'
```

The real tests found `ctrl+j` did not execute the pending fish command. Do not send a carriage return when an agent/tool/dialog may already be running. Wait for the actual target prompt and detected kind, then bind its intended name with `agent rename "$PANE" "$AGENT"` if startup never registered it. Verify `agent get` afterward. Record this as startup failure followed by recovery; retrying `agent start` could duplicate input. Stop after one recovery attempt if readiness remains uncertain.

### Submission and observation

```bash
herdr --session "$SESSION" agent get "$AGENT"
herdr --session "$SESSION" agent prompt "$AGENT" "$ORCH_PROMPT"
herdr --session "$SESSION" agent get "$AGENT"
```

After confirming acceptance, do independent controller work and use the [event supervisor](efficiency.md#event-supervisor) for repeated reads; handle new events with a saved cursor. Without a live observer, follow the [manual supervision loop](lifecycle.md#controller-work-and-supervision). When no independent work remains and observation is manual, use a short wait on the already submitted target:

```bash
herdr --session "$SESSION" agent wait "$AGENT" --timeout 15000
```

After the wait returns or times out, inspect results and sweep all active children. Do not reissue `agent prompt` to wait. `agent prompt --wait` does **not** track task rounds; if called while the target is already working, that existing turn's completion can satisfy the wait. Never use it to queue work behind an active round or let one long wait prevent supervising other targets.

On each manual sweep, inspect both state and the current bottom-of-terminal UI, even if there is no result and state remains `working`. A live observer performs equivalent reads without copying every snapshot into model context:

```bash
herdr --session "$SESSION" agent get "$AGENT"
herdr --session "$SESSION" agent read "$AGENT" --source detection --lines 80
```

`detection` is useful for concise status; it may omit earlier output or dialog details. Read `visible` / `recent` when needed; for an unrecognized agent, use the exact pane's read surface. Screen text is diagnostic evidence, not a substitute for a verified current-round response. Do not wait for a `blocked` hook to look for native approvals.

If a UI has already blocked the target, `agent prompt` may fail with `agent_blocked`. Read the actual dialog first. After a decision is authorized, select/type the corresponding response:

```bash
herdr --session "$SESSION" pane send-text "$PANE" "$ANSWER"
herdr --session "$SESSION" agent send-keys "$AGENT" enter
```

For a selection dialog use the observed navigation bindings, not a fixed number of Down presses. Existing user authorization can answer routine confirmation; new scope or missing approval requires the user. Record the question once, keep that target pending and continue other independent work. After UI approval, check that the same round resumes; do not prepare or resubmit the business task. Do not blindly auto-confirm and do not switch to a global permissive mode.

## tmux fallback

Bind `AGENT_BIN` to the selected executable's absolute path and `SESSION` to a unique name. Start a dedicated session directly with the executable, avoiding an intermediate interactive shell and guessed startup delay:

```bash
tmux new-session -d -s "$SESSION" -c "$PROJECT_CWD" -x 200 -y 50 /usr/bin/env "$AGENT_BIN"
tmux list-panes -t "=$SESSION" -F '#{pane_id} #{pane_dead} #{pane_current_command}'
```

The separate `/usr/bin/env` and executable arguments make tmux use direct argument execution. With only one shell-command argument, tmux invokes a shell; even a quoted path containing spaces can then fail to start. Confirm `/usr/bin/env` exists on the host, or use its actual absolute path.

Record the exact pane ID (such as `%7`) in `PANE`; use it for subsequent input/reads. Confirm the target is at its actual input prompt before submission. An alive process alone does not prove TUI readiness. Initial model selection, login and trust dialogs need handling within the existing authorization.

```bash
python3 "$ORCH_TOOL" record --request "$REQUEST_FILE" --mode tmux \
  --session "$SESSION" --agent "$AGENT" --pane "$PANE" \
  --tmux-default-server --owns-agent --owns-pane --owns-session
BUFFER="orch-$ROUND_ID"
tmux load-buffer -b "$BUFFER" "$PROMPT_FILE"
tmux paste-buffer -p -d -b "$BUFFER" -t "$PANE"
sleep 0.15
tmux send-keys -t "$PANE" Enter
```

Bind `ROUND_ID` to the prepared identity. The uniquely named paste buffer transfers the single-line file without placing a long prompt inside the tmux command message; `-p` adds bracketed-paste markers when the target requests them, and `-d` removes that buffer after pasting. The tested 150 ms pause allows the TUI to process the paste before Enter; it does not prove readiness or submission. Check each command before the next step. If loading fails, do not paste; if pasting fails, inspect the target before resending and delete only this job's buffer when no longer needed. Never use the default shared paste buffer for concurrent jobs.

Real OpenCode testing found that immediate Enter could leave `/exit` unsubmitted in the input box. Only after inspecting the current editable input and confirming that exact command is still pending may the controller send Enter once. Do not blindly repaste commands or resubmit business work.

For short input, `tmux send-keys -t "$PANE" -l -- "$ORCH_PROMPT"` is also valid, but long escaped prompts can exceed tmux's command-message limit. Enter is always a separate key. The buffer path avoids that transport limit, not the target TUI's own input capacity. Do not send multiline task text directly, or treat text as a shell command. For diagnostic reads:

```bash
tmux capture-pane -t "$PANE" -p -S -200
tmux list-panes -t "=$SESSION" -F '#{pane_id} #{pane_dead} #{pane_current_command}'
```

Capture the active pane; use `capture-pane -a` only if the saved alternate-screen contents are relevant. Some TUIs need a continuous `pipe-pane` log for history. It is optional: its sink is itself a shell command, so use a fixed controlled filename and proper shell quoting if enabling it. Preserve raw evidence; ANSI stripping is only for display and is not a reliable completion detector.

To verify pane cleanup, compare the exact owned pane ID against a successful `tmux list-panes -a -F '#{pane_id}'` inventory on the same server/socket. The tested tmux returned exit code 0 with empty output for `display-message -t <missing pane>`; that command's return code cannot establish pane existence. If the server itself exited, verify that separately rather than treating every inventory error as successful cleanup.

tmux has no native agent `blocked`/`idle` state. Poll the round result with a deadline and inspect the pane if progress stalls. A file-based question still works if the target follows the contract. Async tmux requires an active controller/watchdog or an explicitly acknowledged later-resume workflow; do not promise unattended monitoring that does not exist.

## Existing target or unavailable dictionary

If asked to resume an existing controlled job, load its request/resource/submission records and inspect live state before input. If explicitly asked to drive a user's existing agent, record all resource ownership as false unless ownership was actually transferred; cleanup must not exit or close it. Do not interrupt unrelated work to acquire a target.

Use the target dictionary's tested exit syntax. In particular, opencode's exit binding is version-dependent; inspect help. A generic cleanup step must not assume every agent implements `/exit`.

## Codex-specific scope

[Codex](../../agent-codex/SKILL.md) uses herdr kind `codex`, or direct argv in tmux. For workspace-write, add the dedicated round-root with `--add-dir` so results outside cwd can be published. Preserve native approvals and model configuration. Its optional `codex exec` route has process/session receipts instead of terminal resource IDs; do not mislabel JSONL events as a protocol result or fabricate pane ownership.
