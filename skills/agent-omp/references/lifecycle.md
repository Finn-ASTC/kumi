# omp lifecycle observations

Version basis: executable `omp/18.2.6`, CLI help checked 2026-09-20. Earlier Esc/approval
tests used 18.2.3. The separately installed npm source tree is 17.3.5; do not infer that
its internals match the compiled 18.2.6 executable.

Use `/session info` in the input-ready target to retain its native identity and storage
path. Preserve cwd, launch argv and profile. Current help supports `--session-dir`,
`--resume=<ID or path>` and `--profile`; a profile also isolates auth/settings/caches,
and is not just session storage. Resume the exact session, not the most recent candidate.

Inspect the actual frame after publication. A product question, permission dialog,
queued work or native task notification can remain after valid result JSON. Resolve
actual options within existing authorization, or record waiting and relay the missing
decision. Do not send a slash command or new prompt into a dialog. Native `task` work
needs its own identifiers and completion records; protocol depth does not enforce it.

## Background process interface

Current help exposes daemon-supervised processes:

```bash
omp ps list --dir "$PROJECT_CWD" --json
omp ps info "$OWNED_PROCESS_NAME" --dir "$PROJECT_CWD" --json
omp ps logs "$OWNED_PROCESS_NAME" --dir "$PROJECT_CWD" --lines 80
```

`--dir` scopes by project, not job. Names alone do not prove ownership; retain returned
metadata and compare recorded launch identities. `--all` includes other projects and
global services. This interface does not enumerate every native task, thread, plugin
or detached shell; inspect separately recorded tool/task handles as well.

For an identified owned daemon process whose cancellation is authorized, current help
provides `omp ps stop "$OWNED_PROCESS_NAME" --dir "$PROJECT_CWD" --timeout 5`. Read it
back afterward. Foreground Esc and daemon stop are separate. Avoid broad kill/restart
or global-service operations when other work shares the host.

## Run-local controls and settling

Current help confirms `--config` overlays, `--no-title`, `--no-extensions`, `--tools`,
`--max-time` and `--approval-mode`. Select only options needed for the authorized run
and retain them. Disabling extensions changes hooks; no-title does not disable all
auxiliary work; max-time is not proof of child-process exit. `--advisor` is available
but its presence does not establish the effective profile. Do not enable auto-approval.

After publication keep checking UI, native tasks, owned processes and scoped files.
Record product dialogs as waiting and unavailable background/child coverage as unknown.
The [completion receipt](../../agent-orchestrator/references/completion.md) retains
native lineage and side-effect scopes, including relevant session/cache/plugin paths
outside cwd. An idle hook alone does not clear those checks.

Reuse requires input readiness and no remaining owned writer/interaction. For exit,
use `/exit` or `/quit` after resolving/cancelling owned work, then confirm actual exit
and daemon processes separately. Keep sessions and results; `/new`, `/clear`,
`/session delete`, `omp gc` and worktree clearing are not ordinary task cleanup.
