# Incremental recovery and delivery cursors

Use sibling `scripts/runs.py` as `RUNS_TOOL`. For repeated inspections of a large
managed run, delta mode reuses unchanged source contents and returns changes to
compact job/watch rows. It is opt-in and **writes disposable recovery state**.
Full recovery and `--summary` remain read-only. See [run recovery](runs.md) for
pending identities, complete/partial semantics and full-detail drill-down.

## Start, apply, and resume

```bash
python3 "$RUNS_TOOL" recover --run "$RUN_FILE" --delta --limit 10
```

The response returns `cursor_path` and `next_cursor`. Retain both **only after
successfully receiving and applying the response**. For this consumer's next read:

```bash
python3 "$RUNS_TOOL" recover --run "$RUN_FILE" --delta --limit 10 \
  --cursor "$CURSOR_FILE" --since "$LAST_RECEIVED_TOKEN"
```

Apply a response in this order:

1. If `reset=true`, discard the consumer's previous compact job/watch view. Read
   `reset_reason`; this response starts a new baseline, which may need more pages.
2. Apply `job_changes` by `job_id` and `watch_changes` by `watch_id`. `op=upsert`
   replaces that compact row with `value`; `op=remove` removes it from the view.
   Removal describes the recovered inventory, not permission to stop/delete a resource.
3. Inspect the current pending queues, counts, health and errors independently.
   Save the returned path/token pair, then resume with that token if
   `more_job_changes` or `more_watch_changes` is true. Each change list has its own
   `--limit`; `change_counts` includes the changes pending before this page.

Unreturned changes remain queued across calls. Already queued identities keep their
turn when earlier rows change again, so one frequently changing job does not starve
the rest. Changes merge to the latest compact row, not every intermediate transition.
Use watch events/review history for an event audit. Full round/target/completion
details are not part of this projection; retrieve them with ordinary recovery.

## Pending work is never a delivery delta

`action_required`, `waiting_user`, `errors`, totals, and aggregate health are current
on **every** reply, even when both change lists are empty. Pending payloads retain
exact watch_path + seq, job/round, notes, review revisions, related reviews and
evidence handles. A new permission does not inherit earlier approval; a delivery
cursor never handles or clears a review. Read old waiting-user notes before asking again.

Pending/error lists use the same independent `--action-offset`, `--waiting-offset`,
`--error-offset` pages as summary. Keep the returned delta token when browsing;
use each `next_*_offset` only when its corresponding `more_*` is true. Reset these
offsets to zero each inspection cycle and after record changes. Job/watch offsets
are rejected in delta mode: their remaining changes are carried by the cursor.
Neither kind of paging is an immutable snapshot.

`complete=false` and nonzero `counts.errors` remain visible beyond an empty error
page. Partial recovery emits valid upserts and pending reviews but **defers removals**;
missing/unreadable state is not evidence that a previously shown row was deleted.
The normal `--job` filter applies; run-wide integrity errors remain visible.

## Lost output, invalid state and concurrent readers

Each response rotates the token. Resume with the token from the last response the
consumer received, not a token read directly from the cursor file. If output was
lost after the cursor was saved, the old token mismatches: recovery clears its
delivery baseline and replays current rows with `reset=true`. Omitting `--since`
on an existing cursor also requests a fresh baseline.

Missing/corrupt/checksum-invalid cursors, incompatible cursor versions and changed
job filters reset the baseline and discard cached source contents. A supplied
cursor path must belong to this run's `recovery/<id>/cursor.json`; foreign paths
and symlinks fail rather than being overwritten. Without `--cursor`, a new private
consumer directory is allocated. Separate consumers should use separate cursors.

A nonblocking cursor lock serializes updates. Contention fails without advancing
the cursor; reread with the last received token. Content cache and delivery record
are separate atomically replaced files. The content file is rewritten only when
its payload changes. An interruption between their publications invalidates the
pair and resets on retry. If persistence fails, the command fails; do not infer
that the changes were delivered. A retry either resumes or resets conservatively.

## Read cost, time and storage

The cache holds at most 4 MiB of source bytes across 4,096 files. Overflow falls
back to ordinary source reads, preserving recovery. Each serialized cursor/cache
file is bounded at 32 MiB; an oversized delivery baseline fails with a diagnostic, so use summary
or a narrower job filter. Existing task/results/reviews are never modified by delta.

Each source lookup still opens the file and validates regular-file type and size.
Hits require matching device/inode, nanosecond mtime/ctime, size, mode, owner/group
and link count, with a second descriptor check. Restoring mtime alone does not
preserve a hit. Missing, replaced, redirected, oversized or nonregular sources do
not reuse old content. Directories are rescanned, so new rounds, watches, revisions
and unregistered artifacts remain discoverable.

The cache assumes coherent local filesystem metadata. Its checksum detects damage,
not malicious rewriting by an actor who can edit this controller's files. Cursor
files contain cached task/result/review content, use private permissions, and stay
inside the run; treat them as disposable local evidence, not a portable skill file.

File contents are cached, **not recovery decisions**: JSON validation, time-based
lease/monitor expiry, pending age and success freshness run again on every call.
Observer locks are checked live. Even with no source writes, delta can emit a changed
row when an observation expires. A clean delta does not establish current coverage
or actual observer lifetime: use the [supervision checkpoint](supervision.md) before
independent work. That checkpoint keeps its ordinary uncached reads.

`read_stats` separates source content reads/bytes, cache hits, retained cache size,
and separate cursor/cache read/write bytes. Hits include reuse within one call. Directories,
opens/stats, JSON parsing and the cursor's own serialization still cost work; this
is not a constant-time query, an OS cache-miss counter, or a guaranteed wall-time
improvement. Page limits bound rows, not bytes/tokens. Compare total work for the
actual run before assuming savings.
