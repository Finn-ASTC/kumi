# Hermes lifecycle observations

Version basis: local Hermes 0.21.3, reported upstream `64ea66b0`, inspected 2026-09-20.
The earlier Ctrl+C foreground-process test used 0.20.5. Recheck actual bindings after
upgrades; source inspection is not a new model/cancellation test.

The same 0.21.3 checkout was inspected again on 2026-09-23. Its native
`hermes_cli/approval_transport.py` passed synthetic request-binding, deny, timeout,
late-response and scope checks. This is a library-contract check, not an installed
approval service or TUI cancellation test. The documented
[approval transport](https://hermes-agent.nousresearch.com/docs/user-guide/features/plugins#approval-transports)
presents requests to a human; it does not grant plugin policy or model auto-approval.
Its request ID/digest and allowed choices are promising inputs for a future human
window bridge. Redacted display text is not proof of complete decision input.

For new controlled tasks, use the [per-run storage isolation recipe](isolation.md)
when existing routing, credentials, approval policy and runtime dependencies can be
preserved. It disables two automatic background systems in a private config copy;
it does not stop an already running default-home reviewer. Existing native sessions
must retain their original home. Resume with the exact **home + session ID** pair.

| Question | Concrete entry point | Boundary |
|---|---|---|
| Which session is this? | In the exact TUI, `/status`; retain full native session ID/path. `hermes sessions list --source cli --limit 20` lists candidates. | Recent time or matching cwd alone does not bind a pane. Resume the exact saved session. |
| Native work remaining? | `/agents` (alias `/tasks`), `/queue list`, recorded tool processes and UI. | Commands must reach the TUI, not a dialog; native work is separate from protocol depth. |
| Automatic future work? | `/goal status`, `/heartbeat status`, known recurring loop/background-session state. | Do not start `/bg`, goals, heartbeat or loops for a diagnostic. |
| Private skill mutations? | `hermes curator ledger --limit 20`, optionally `--skill <known-name>`; inspect scoped raw records for correlation. | Summary is not filtered by this job/session. Missing/disabled/unreadable ledger is not proof of no writes. |
| Curator status? | `hermes curator status` shows configuration and managed skills. | It does not certify this session's background-review thread stopped. Run/pause/resume/rollback/adopt mutate state. |

Use the actual Hermes home/profile, which may differ from `~/.hermes`. Default private
skill records are `skills/.curator_ledger.jsonl`. Retain before/after scope, entry IDs,
actor, paths and native session evidence. The CLI reader skips malformed ledger rows:
preserve read errors instead of interpreting an empty display as complete coverage.
Account for relevant memory/log/session side effects without copying unrelated private data.

Inspect exact native child records through the installed session store/export interface.
For a known full session ID, retain a local scoped export:

```bash
hermes sessions export "$EVIDENCE_FILE" --format jsonl --session-id "$NATIVE_SESSION_ID" --redact
```

`--lineage` applies to md/qmd compression lineage, not native task enumeration. Export
only identified sessions; do not add upload, deletion or overwrite options for inspection.
`parent_session_id` also represents compression/continuation: retain `source` and task
launch evidence before classifying a child as delegated work. Completed DB rows do not
prove external process exit. If no exact background check is available, record unknown.

The inspected `agent/turn_finalizer.py` starts eligible memory/skill review **after
delivery**. `agent/background_review.py` tracks it; a new live turn requests cancellation
but waits only a bounded time. After a timeout warning the foreground may continue.
A fresh prompt is not a reliable stop test. CLI startup can also run curator maintenance.

Continue observing after result publication. Record actual background completion or
cancellation evidence and later file changes before marking the [host receipt](../../agent-orchestrator/references/completion.md)
settled. Changes to delivered scope require a new handoff/verification; keep the old
result/snapshot intact. The old snapshot proves what was verified, not acceptance of
later live edits. Internal `skip_background_review` is a constructor parameter, not
evidence of a same-named CLI flag. Check supported run/profile controls; do not change
global config or approval policy to make an experiment pass.

For cancellation, inspect focus and use the current interrupt binding on owned work.
Read response, process/task state and partial files separately. `/stop` is documented
as killing registered background processes and interrupting async delegations, not an exact background-review-thread cancel API;
check its scope/ownership before using it. Goal/heartbeat/loop cancellation is separate.
Reuse requires input readiness, no owned writers/children or unresolved interaction,
and accounted side effects. Use `/exit` to retire only the owned target, then check
actual exit and remaining tool/native resources. `/exit --delete`, session deletion,
`/clear` and `/new` are not cleanup shortcuts; session boundaries may trigger memory work.

Source entry points in the installed tree: `hermes_cli/commands.py`,
`hermes_cli/cli_session_mixin.py`, `hermes_cli/subcommands/sessions.py`,
`hermes_cli/curator.py`, `tools/skill_ledger.py`, `agent/turn_finalizer.py`,
`agent/background_review.py`, `hermes_cli/cli_commands_mixin.py` stop/agents handlers,
`cli.py` startup curator hook.
