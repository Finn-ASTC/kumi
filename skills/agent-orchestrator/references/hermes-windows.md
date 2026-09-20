# Hermes cumulative observation windows

Use sibling `scripts/usage.py` as `USAGE_TOOL`. These helpers preserve evidence and
compare persisted counters. They do **not** provide additive calls, reliable execution
intervals, round ownership, a flush barrier or billing totals. Cumulative native import
remains disabled; [source audits](usage-sources.md) retain `cumulative_only` for these
stores. Future calls can use the separate optional [hook recorder](hermes-hooks.md),
which allows scoped closed-turn import without reconstructing cumulative history.

## Capture exact sources without overwriting evidence

Choose the database and session ID from retained native receipts. Do not select by
cwd, time or newest session. The output directory must already exist; use a fresh file:

```bash
python3 "$USAGE_TOOL" hermes-snapshot --log "$HERMES_DB" \
  --session-id "$SESSION_ID" --output "$BEFORE_FILE"
```

The reply is `{"path": "/absolute/file.json", "sha256": "actual-digest"}`. Keep it.
Capture subsequent observations into new files with the same command. A pre-existing
destination is rejected. Publication is atomic and bounded to 16 MiB; the native database
is only read, including its WAL. Each snapshot stores session identity, observation time,
canonical database path/device/inode, and separate session/model-task cumulative views.
It does not include chat text or billing endpoints; bucket identities hash native route keys.
Device/inode detect common replacements, not restores in place or counter epochs.
Saved files remain ordinary local files; the helper refuses overwrite and checks hashes,
but does not provide filesystem-enforced immutability or signed evidence.

Retain observations taken before work, after observed completion, and later if persistence
may lag. These times describe reads; they do not establish which calls belong to a task.
Reading twice with unchanged counts does not prove the writer queue is drained. Do not
change host settings or submit another model request merely to force a usage update.

## Compare an ordered chain

Create a UTF-8 manifest using the exact returned receipts, in observation order:

```json
{
  "version": 1,
  "snapshots": [
    {"path": "/absolute/before.json", "sha256": "actual-before-digest"},
    {"path": "/absolute/after.json", "sha256": "actual-after-digest"},
    {"path": "/absolute/later.json", "sha256": "actual-later-digest"}
  ]
}
```

```bash
python3 "$USAGE_TOOL" hermes-windows --manifest "$WINDOW_MANIFEST"
```

Comparison reads only frozen files, validates hashes and normalized/raw counters,
and requires one store/session and strictly increasing timezone-aware observation times.
It reports adjacent pairs only: before→after and after→later. Duplicate or reordered
snapshots are rejected. Keep the earlier report if another snapshot arrives; create an
extended manifest. Reports may overlap each other and have no global interval ledger:
**do not add two reports, or add before→later to either smaller interval.**

Each window returns one `session` row and separate `models` rows, with model/provider/task
labels, `observed_delta`, `observed_api_call_delta` and `issues`. Empty task denotes the
native main-loop bucket when exposed; nonempty tasks identify native auxiliary categories.
These are overlapping views, not amounts to add. Output already contains reasoning;
normalized input includes raw input plus cache read/write, with cached input as a subset.

| Condition | Result |
|---|---|
| Any raw token or API-call counter decreased | `counter_decreased`; every delta in that row is null. |
| A model/route bucket appeared or disappeared | `missing_baseline` / `missing_endpoint`; no zero baseline inferred. |
| Session bucket identity changed | `bucket_identity_changed`; no cross-route subtraction. |
| A required counter or model inventory is absent | Null for unknown counters; `missing_counters` / `model_inventory_unknown`. |
| Parent/child inventory changes or cannot be enumerated | `lineage_changed` / `lineage_unknown`; inspect each exact session separately. |
| Later writes increase counters | Increase appears in the later observation pair; it is not assigned to a later task. |

A nonnegative difference is still only observed arithmetic: a reset followed by enough
new usage can remain invisible. Compression children may inherit counters/history; do
not sum parent and child. API-call counts can omit usage-less or auxiliary activity under
some native paths, so they cannot certify call coverage.

All reports have `measurement: "observed_cumulative_difference"`, `additive: false`,
`round_attributable: false`, null `round_totals` and explicit `limitations`. A zero observed
change is not verified zero work. Exit 0 means a report was produced, not that its counters
are complete. The saved hashes pin bytes; they are not signatures or proof of receipt meaning.

For cumulative-only evidence declare `hermes:<session_id>` with `sample_ids: null` in project inventories.
Known unmetered attempts may use null-counter samples; do not rename these windows to
`measurement: "delta"`, fabricate call IDs or distribute them by timestamps. Exact Hermes
round accounting needs native persisted call IDs/turn attribution, counter-epoch identity,
and flush/coverage evidence; these snapshots cannot reconstruct missing information.
