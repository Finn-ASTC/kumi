# Explicit session records and read-only previews

Run these commands from the `agent-orchestrator` skill directory. No command here
starts a host, reads global chat history, resumes a model, or archives a session.

```bash
python3 scripts/sessions.py --run "$RUN_FILE" --limit 100 --offset 0
python3 scripts/registry.py list --run "$RUN_FILE"
python3 scripts/capabilities.py list --run "$RUN_FILE"
```

The session directory returns short task titles, job/round references and blockers.
Its job page is bounded; the run's registration and capability metadata are still
read in full. It is not a cross-file transactional snapshot. Evidence files may
be read for hash checks but their bodies, capability notes and closure evidence
are not returned in the directory.

## Register known identities

Choose a stable 32-character lowercase hexadecimal registration ID before the
first call and retain it for retries. Use exact identity evidence, never the most
recent native session or a cwd/title match. `STORE` is an explicit locator, not
proof that storage is isolated or that two copied stores are different sessions.

```bash
python3 scripts/registry.py register --run "$RUN_FILE" --id "$MAIN_REGISTRATION_ID" \
  --kind external --role coordinator --host "$MAIN_HOST" --store "$MAIN_STORE" \
  --profile "$MAIN_PROFILE" --session-id "$MAIN_NATIVE_SESSION_ID"

python3 scripts/registry.py register --run "$RUN_FILE" --id "$WORKER_REGISTRATION_ID" \
  --kind managed --role worker --host "$HOST" --store "$STORE" --profile "$PROFILE" \
  --session-id "$NATIVE_SESSION_ID" --job "$JOB_ID" --round "$ROUND_ID" \
  --parent "$MAIN_REGISTRATION_ID" --relationship kumi_delegation
```

An external `coordinator` or `user` has no job ownership. No registration grants
control. Other relationships (`native_child`, `continuation`, `handoff`) describe
explicit links; they do not transfer pending work, messages or input authority.
An exact retry remains valid after job closure; a new registration for a closed
job is refused. Bad parent links, cycles and redirected registry paths are errors.

If multiple registrations reference the active job/round, the directory reports
`ambiguous_registration` and does not choose by ID order or timestamp. All records
remain available for private reconciliation. This first version has no selection,
replacement or retirement command; do not edit immutable records to force a match.
Changed native session/profile also blocks binding. Historical registrations are
not automatically redirected to a successor round or session.

## Register capability evidence

```bash
python3 scripts/capabilities.py register --run "$RUN_FILE" --host "$HOST" \
  --profile "$PROFILE" --capability resume --status verified \
  --host-version "$TESTED_VERSION" --observed-at "$OBSERVED_AT" \
  --evidence-path "$ABSOLUTE_PROBE_EVIDENCE"
```

`OBSERVED_AT` must be an ISO 8601 timestamp with timezone, no later than now.
The evidence must exist as a bounded regular file, not a final symlink or FIFO.
New v2 records pin its SHA-256; reads recheck the bytes. They preserve the caller's
`status` and separately return `effective_status` and `evidence_status`. Modified
or missing evidence degrades a verified claim to unknown; legacy v1 unpinned
claims remain readable but are unknown. The records are immutable; retry exactly,
and retain conflicting evidence for review rather than overwriting an old claim.
Revisioned capability promotion/revocation within a run is not implemented yet.

The same contract applies to Codex, omp, Hermes and OpenCode OMO/pure. Custom
profiles have their own records and never inherit the default profile's claims.
`verified` means caller-reported verification with intact evidence; the helper
does not judge the truth of arbitrary evidence, probe installed binaries, or
establish applicability to the current version/store/configuration. Help text or
a synthetic fixture must not be described as a successful native probe.

## Interpreting blockers

`candidate` identifies a possible history lookup, not permission to resume.
Both `eligible` and `available` remain false while runtime adapters and checks
are absent. Verified evidence only removes the capability-evidence blocker;
`capability_applicability_not_verified` and adapter blockers remain.

Previews distinguish uncertain submission/control, closed jobs, conflicting
identities, missing/expired host observations, and a missing control lease from
unverified input ownership. A lease held by someone else grants the reader no
input right. Host evidence is checked separately from full delivery acceptance.
Message/handoff responsibilities, descendant scope and evidence retention remain
explicitly incomplete. A completed result or closed job does not prove the native
host stopped; registration, grouping and preview never authorize archive/delete.
