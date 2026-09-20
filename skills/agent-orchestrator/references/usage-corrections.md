# Correcting recorded usage

Use when native counters arrive after a null sample was recorded, or evidence shows the recorded attempt/role/phase/round was wrong. `record` and `import-native` still reject conflicts. Preserve source/sample IDs, original files and evidence; corrections append a new effective version.

Set `USAGE_TOOL` to the sibling `scripts/usage.py`. Inspect the call first:

```bash
python3 "$USAGE_TOOL" history --request "$LATEST_REQUEST" --source "$NATIVE_SOURCE_ID" --sample-id "$NATIVE_SAMPLE_ID"
```

The result includes `original`, `effective`, `revision`, and correction history with reasons and evidence hashes. Copy the current revision exactly. The request selects the job ledger; it can expose job-wide records outside that request's predecessor chain. Summaries still select only rounds in the supplied chain.

Create a correction manifest. `request_path` at the top selects the source ledger through a known request; include the original round in that chain when correcting legacy per-round samples. Each update's `request_path` selects the destination round in the same job. The replacement is a **complete normalized sample**, not a patch; retain fields that remain valid.

```json
{
  "version": 1,
  "request_path": "/absolute/latest/request.json",
  "correction_id": "usage-fix-001",
  "reason": "Retained native export now contains counters; dispatch receipt confirms verifier role",
  "evidence": [
    {"path": "/absolute/frozen-usage-evidence.json", "sha256": "actual-64-lowercase-hex-file-sha256"}
  ],
  "updates": [
    {
      "request_path": "/absolute/correct-round/request.json",
      "expected_revision": "actual-64-lowercase-hex-revision-from-history",
      "sample": {
        "source": "opencode:actual-session-id",
        "sample_id": "actual-assistant-message-id",
        "measurement": "delta",
        "attempt_id": "verification-retry-1",
        "purpose": "retry",
        "role": "verifier",
        "phase": "verification",
        "provider": "actual-provider",
        "model": "actual-model",
        "input_tokens": 100,
        "cached_input_tokens": 60,
        "output_tokens": 20
      }
    }
  ]
}
```

Use actual hashes and native values; unavailable values stay null. Evidence is a retained regular file, at most 16 MiB, with its byte SHA-256 verified before submission. Prefer a frozen export or a compact evidence extract with precise native identities. Do not hash a live SQLite main file as if it included its WAL. Evidence supports review; a matching hash does not prove the claimed interpretation is correct.

If an attempt has several calls, include every affected call in one `updates` array with its own revision. All effective calls in a round/attempt must share purpose/role/phase. Partial reclassification is rejected without applying any update. Counter corrections may increase or decrease values when supported by evidence.

```bash
python3 "$USAGE_TOOL" correct --manifest "$CORRECTION_MANIFEST" --dry-run
python3 "$USAGE_TOOL" correct --manifest "$CORRECTION_MANIFEST"
python3 "$USAGE_TOOL" history --request "$LATEST_REQUEST" --source "$NATIVE_SOURCE_ID" --sample-id "$NATIVE_SAMPLE_ID"
python3 "$USAGE_TOOL" summary --request "$LATEST_REQUEST"
```

`correct` serializes with ordinary recording and atomically publishes one event for the entire update group. Its event lives under the first round's `usage/corrections/`; original `usage/calls/` and legacy samples remain unchanged. `summary` and `project-summary` count each effective sample once, with its corrected attribution. A same-job round move can leave the original round without recorded usage, so its coverage becomes unknown.

| Result | Next action |
|---|---|
| Dry-run passes | Submit the same manifest; dry-run reserves nothing and has no committed revision. |
| Stale revision | Read history again, reconcile the competing change and issue a newly reviewed correction. Do not merely substitute the new hash. |
| Same correction ID and identical manifest | Idempotent replay, including recovery from a lost success receipt; it does not reapply or roll back later corrections. |
| Same correction ID with changed content | Use a new correction ID after review; IDs cannot be reused for a different intent. |
| Original import still conflicts | Align it with the effective counters and attribution; reimport never overrides corrections automatically. |

Correction events form a checked sequence of fingerprints. Missing events, changed bases and stale revisions fail closed; this is not a signature against malicious filesystem rewriting. Retain original request paths and ledger files. Evidence paths/hashes are recorded at submission and are not re-read on every summary. Do not mix old and new usage readers/writers after a correction: old readers only see the original samples.

Cross-job transfers/deletions and source alias or fork-history reconciliation are not implemented. Cross-job conflicting effective records still fail project aggregation. Keep unresolved cases explicit; do not overwrite history or mint another sample ID for an already counted call. Hermes cumulative snapshots remain ineligible for round delta corrections.
