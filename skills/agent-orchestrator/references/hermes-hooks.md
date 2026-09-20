# Optional Hermes request usage recorder

Use this when future Hermes turns need explicit call receipts. Native `state.db`
contains cumulative views; it cannot reconstruct past calls. The bundled plugin
uses main-loop Hermes hooks without changing the host core or making model calls.
Use sibling `scripts/usage.py` as `USAGE_TOOL`.

For setup diagnostics and optional support levels, use the
[Hermes status recipe](hermes-support.md). It separates advertised hooks, plugin
files/configuration and store presence; none alone certifies actual capture.

## Install into the actual profile before launch

The plugin source is [agent-hermes/plugins/orch-usage](../../agent-hermes/plugins/orch-usage/).
It is runtime content, but installing six skills alone does not enable a Hermes plugin.
Resolve the intended profile using the same environment/flags as the future launch
and `hermes config path`. Verify that it is exactly `$PROFILE_HOME/config.yaml`.
For the root/default home pin `--profile default`; for a named profile use `-p NAME`.
For private homes follow [storage isolation](../../agent-hermes/references/isolation.md).

After verifying both absolute paths, check the destination before creating a link:

```bash
mkdir -p "$PROFILE_HOME/plugins"
ln -sT "$PLUGIN_SOURCE" "$PROFILE_HOME/plugins/orch-usage"
```

`PLUGIN_SOURCE` is the complete bundled `orch-usage` directory in the installed
runtime package. An existing destination must be inspected; preserve independent
files instead of replacing them. For an isolated run copy the plugin as independent
files, with no symlinks, into its own `plugins/orch-usage` directory. Copying only
`config.yaml` does not install plugins. Preserve other required plugins/configuration.

After the isolation recipe’s configuration parity check, enable with the same verified
profile selector. This plugin needs no built-in tool overrides; use the explicit denial:

```bash
hermes --profile default plugins enable orch-usage --no-allow-tool-override
# Named profile alternative: hermes -p NAME plugins enable orch-usage --no-allow-tool-override
# Private profiles/worker home alternative:
# env HERMES_HOME="$PROFILE_HOME" hermes plugins enable orch-usage --no-allow-tool-override
```

Verify plugin discovery in a fresh process (`hermes ... plugins list`). New processes
load the enabled hooks; an existing process may retain its previous registrations.
Do not restart an active user session merely to meter it. Disable through the same
profile's `plugins disable orch-usage`; existing records remain available.

## Inspect and bind receipts

The store is `$PROFILE_HOME/usage-hooks/events.sqlite3`, separate from native `state.db`.
Profile resolution happens for each callback. Each received event commits in its own
SQLite transaction with synchronous FULL; new directories/files use 0700/0600.
Only session/turn/request IDs, route labels, canonical numeric usage, lifecycle flags
and an optional submitted-text SHA-256 are retained. No conversation, request/response
objects, endpoints, credentials or exception text are saved; there is no network export.

```bash
python3 "$USAGE_TOOL" inspect-native --host hermes --log "$HOOK_STORE" \
  --session-id "$SESSION_ID"
```

Select the exact session from native launch/recovery receipts. `turn_receipts` gives
the native turn IDs, start/close state, sequences and `prompt_sha256`. Compare that hash
with the **exact string actually submitted**, including any retained trailing newline;
do not normalize it or substitute the task file for the rendered prompt. Non-string
user messages have no hash. A hash checks bytes, not signatures or ownership by itself.
Identical submitted strings may occur in different turns. Retain the submission/native
receipt establishing the round-to-native-turn binding; the hash alone cannot disambiguate them.

`call_receipts` maps native request IDs to persisted start/end sequences and outcomes.
Hermes can reuse one `api_request_id` across retries. Therefore a call's ledger identity
is source `hermes:<session_id>` plus sample `hook:<store_uuid>:<start_sequence>`.
Error then retry creates two occurrences; rereading or copying the intact store does
not create new samples. For a live SQLite copy use its backup API to a fresh destination,
not a raw copy that can omit WAL records; ordinary inspection needs no copy. A recreated
store or fork is not automatically reconciled.

## Import closed observed turns

Use the [native import manifest](efficiency.md) with `host: "hermes"`, the hook store's
absolute `log_path`, exact `session_id`, and explicit native **turn ID** as `native_id`.
Each mapping retains `request_path`, `attempt_id`, `purpose`, `role` and `phase`.
The ledger attempt identifies the orchestration attempt; native retries remain separate
calls inside it and are not automatically classified as `purpose: "retry"`.

```bash
python3 "$USAGE_TOOL" import-native --manifest "$IMPORT_FILE" --dry-run
python3 "$USAGE_TOOL" import-native --manifest "$IMPORT_FILE"
```

Selected turns need both lifecycle boundaries, paired calls and no integrity issues.
An unselected live turn does not block a closed selected turn. Missing boundaries or
request endings remain pending; orphan/duplicate terminal events, overlapping starts,
late events after closure and deleted event sequences prevent valid selected imports.
A later close does not repair a previously observed integrity issue.
Failed calls, missing usage, all-zero placeholders and MoA aggregate paths retain null
counters. Input includes cache read/write; cached input is a subset. Hermes output
already includes reasoning. Unknown counts are never filled from cumulative snapshots.

## Coverage remains scoped

Every main-loop recorder report retains `coverage_complete:false`, scope
`observed_main_loop_hooks`, and limitations for auxiliary calls, provider-internal
retries, native children, hook delivery and history before enablement. Hermes may catch
plugin errors or time out callbacks. `persisted_sequence` proves only locally committed
events, not delivery of every hook, a native flush barrier or provider billing coverage.
Known scoped call sums do not certify the total cost of the host/run.

For [source audits](usage-sources.md), hook-store bindings require explicit native turn
IDs; cumulative `state.db` bindings still require `native_ids:null`. Hook sources retain
`native_scope_incomplete` and unknown lineage, so whole-run certified totals remain null.
Use one exact store path per source in an audit. Keep cumulative snapshots/windows as
separate, nonadditive evidence; never sum them with recorded calls. Old history without
hook records still uses the [cumulative observation recipe](hermes-windows.md).

## Optional auxiliary response observations

This capability requires a host that explicitly exposes **`on_aux_usage`** with the
contract below. Stock Hermes 0.21.3 at `64ea66b03d` does not expose it. On that host
the plugin keeps recording main-loop events only; copying or enabling this plugin
does not add a native hook. Check the actual host capability, not the version label:

```bash
# Run using the same Python environment and checkout as the intended Hermes process.
python -c 'from hermes_cli.plugins import VALID_HOOKS; print("on_aux_usage" in VALID_HOOKS)'
```

The required observer contract emits a stable `observation_id` for one accounting
observation, exact `session_id`, `turn_id:null`, `task`, response `model`, optional
`provider_hint`, and canonical `usage` or null. Input excludes cache read/write;
output already includes reasoning. This observes responses at auxiliary accounting,
before response-shape validation; it does not certify a successful title or approval.
`provider_hint` is an accounting route hint, not verified fallback attribution.
MoA reference/aggregator calls already included in main-loop accounting are excluded.
Absent/all-zero native usage remains null. The plugin never patches the host.

With a compatible host and the plugin enabled before the call, inspect the separate store:

```bash
python3 "$USAGE_TOOL" hermes-aux \
  --log "$PROFILE_HOME/usage-hooks/auxiliary.sqlite3" --session-id "$SESSION_ID"
```

`receipts` lists observed responses with source `hermes-aux:<session_id>` and sample
identity `aux:<observation_id>`. Repeated delivery of the same ID/content keeps one
row; conflicting content is rejected. A consistent SQLite backup retains the IDs.
Separate accounting observations get different IDs even if a provider reuses its
response ID; they are not a guaranteed inventory of physical provider attempts.

All receipts currently have `native_turn_id:null` and remain **unassigned**. The
command is inspection only: these records do not enter round ledgers or source-audit
bindings. `observed_totals` sums just those receipts; any missing counter keeps that
aggregate unknown. `totals:null` and `coverage_complete:false` remain explicit.
Keep main-loop, auxiliary and cumulative views separate; do not infer ownership from
time, active turns, or cumulative differences. No store means no recorded evidence,
not zero auxiliary cost. Past title/approval totals cannot recover individual receipts.

Calls without accounting context, provider errors without responses, internal retries,
streaming/direct-accounting paths (including background review), native children,
lost hook deliveries and history before enablement remain unknown. Hook timeouts/errors
may lose observations. `persisted_sequence` proves local commits only; there is no
native flush barrier or all-host completeness guarantee.
