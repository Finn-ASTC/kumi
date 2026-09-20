# Hermes support levels and optional metering

Ordinary Hermes orchestration uses the task contract, transport, supervision and
host lifecycle recipes. Metering is optional. Missing usage hooks or records leave
cost unknown and do not prevent dispatch, recovery, or result verification.

| Level | Supported scope | Required evidence |
|---|---|---|
| Orchestration | Isolated launch, tasks, supervision, results, recovery | Verified transport/native identity and task receipts |
| Main-loop metering | Observed requests; closed-turn import and deduplication | Main-loop hooks, enabled matching collector, validated turn receipts |
| Auxiliary cumulative observations | Native model/task buckets, separate from call receipts | Exact-session read-only native database inspection; no round attribution |
| Auxiliary metering | Separate observed responses, unassigned to rounds | Optional `on_aux_usage` contract and validated auxiliary receipts |
| Complete usage | Not certified by current tools | Missing calls, controller/children, ownership and delivery coverage still unresolved |

## Default integration: stock host, optional collector, read-only evidence

Keep the installed Hermes source unchanged. Use its existing hooks with the optional
collector for main-loop receipts. For native cumulative observations, set
`USAGE_TOOL` to this package's `scripts/usage.py` and inspect the exact session:

```bash
python3 "$USAGE_TOOL" inspect-native --host hermes \
  --log "$PROFILE_HOME/state.db" --session-id "$SESSION_ID"
```

Report the returned session and model/task views separately. Auxiliary buckets show
only recorded cumulative usage; absent buckets are not zero. Do not add overlapping
views, combine them with call receipts, or assign their differences to rounds. For
retained snapshots use the [cumulative observation recipe](hermes-windows.md).

A request to improve usage auditing alone does not select source patches, runtime
function replacement, provider replacement or proxy routing. Those are separate
integration work with their own explicit scope; missing metering is not a reason to
introduce them during ordinary setup. Keep native patches in isolated experiments.

Native Relay is a separate candidate integration, not a default collector fallback.
Validate its selected version, event fields, process-wide/profile behavior, output
content and delivery before adopting it. Discovering the interface is not acceptance
of its coverage. Historical auxiliary receipts remain readable after a hook is removed;
their presence does not mean new auxiliary calls are still being captured.

## Check the intended host and profile

Use this at initial setup or after changing the profile, interpreter, checkout or
collector. Keep the result with setup evidence; do not run it on every watch/poll.
First resolve the intended profile with the same Hermes environment/selector and
`hermes config path`, following the [installation recipe](hermes-hooks.md).

Run the package's `scripts/hermes_status.py` with the **intended Hermes Python
interpreter**, explicit source checkout and already-resolved profile directory:

```bash
"$HERMES_PYTHON" "$ORCH_SCRIPTS/hermes_status.py" \
  --hermes-root "$HERMES_CHECKOUT" --profile-home "$PROFILE_HOME"
```

`ORCH_SCRIPTS` is the absolute `scripts/` directory of the installed
`agent-orchestrator` skill. `HERMES_PYTHON` and `HERMES_CHECKOUT` must describe the
actual launch installation, not whichever system Python or source copy is newest.
This probe needs the host's Python dependencies (including PyYAML); ordinary
orchestrator tools continue to use standard-library Python. Run in a fresh process.

The diagnostic imports the host's advertised hook registry, compares the complete
profile-local collector with this package, reads explicit YAML opt-in/deny-list
settings, and checks store-file presence. It does not discover/execute plugins,
read conversation/database contents, call models, migrate config, enable plugins,
apply native patches, or restart sessions. Hermes safe mode is reported separately.
Output contains only selected paths and status fields, never raw configuration,
host import output, exception messages, endpoints or credentials.

## Interpret independent observations

| Field | Meaning |
|---|---|
| `host.*` | Advertised hook names in the selected checkout; import/origin failures are unknown |
| `plugin.files` | `matches_package`, `missing`, `different` or `unreadable`; modified code is not executed |
| `plugin.configured_enabled` | Explicit configuration intent; null if parsing/dependency/config shape is unknown |
| `layers.main_loop/auxiliary.status` | `candidate`: checked prerequisites met; `unavailable`: a negative prerequisite; `unknown`: insufficient evidence |
| `stores.*` | Presence only; `present_uninspected` also covers empty, corrupt or historical files |

A known missing hook, missing profile-local plugin, disabled configuration or safe
mode makes that metering layer `unavailable`, even if another prerequisite is unknown.
Without a known negative, modified/unreadable plugin files or unknown host/config
make it `unknown`. The individual observations remain visible in either case.

**Candidate does not prove capture.** The diagnostic does not resolve project/pip
plugin precedence, inspect native discovery, validate hook payload semantics, or
inspect an already-running process. `capture_verified:false`, `coverage_complete:false`
and `totals:null` remain explicit. A changed plugin can be independently reviewed;
`different` is not proof it is broken. Missing/malformed explicit configuration may
also behave differently after a native migration; the probe does not run migrations.
Importing the registry does not check all Hermes dependencies: even a system Python
with only enough dependencies for this probe can report candidate. The caller must
still select the actual launch interpreter; its path is included in the report.

Exit 0 means the diagnostic produced a report, including unavailable/unknown levels.
It is not a readiness or capture-success code. Invalid CLI paths/arguments fail normally.
`layers.orchestration.health:not_checked` means transport health needs its own checks;
`metering_required:false` is not a blanket task-acceptance decision.

For stock Hermes without `on_aux_usage`, use main-loop metering if its prerequisites
are met and keep auxiliary per-call/round cost unknown. Native cumulative observations
can still be inspected separately; `layers.auxiliary` describes the optional receipt
hook layer, not native cumulative-data availability. Enabling the collector cannot add the
missing native hook. If the host import is unknown, recheck the selected interpreter
and checkout; do not relabel unknown as unsupported or install a patch automatically.

A disabled collector may leave old stores. A missing store means no evidence,
never zero cost. Validate exact-session main-loop or auxiliary records with the
[receipt commands](hermes-hooks.md), and keep cumulative snapshots separate.
Only fresh verified task/native receipts establish actual capture for that task.
If the user specifically requires complete cost auditing, report unmet coverage;
ordinary orchestration can continue at its independently verified level.
