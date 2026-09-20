# Per-run Hermes storage

Tested with Hermes 0.21.3, upstream `64ea66b0`, on Linux using an existing API-key
provider. For **new** controlled work, prefer a private home when the following
preflight can preserve the user's model route, credentials, approvals and security
dependencies. Do not change global config, choose another provider/model, disable
security scanning or enable autoapproval to get past a setup problem. An unsupported
credential/runtime setup remains unresolved until reconciled; do not silently fall
back to a default-home model launch.

This scopes native config, sessions, skills, memories and logs. `HERMES_HOME` is not
an OS filesystem sandbox. Terminal commands, absolute plugin paths, MCP services,
external caches and escaped processes still need ordinary scope and supervision.
Foreground skill/memory tools can still write; authorize and account for those writes.

## Prepare before starting the model

Resolve the actual source profile with the existing launch environment and
`hermes config path`. A default `~/.hermes` assumption is insufficient. Choose an
owned run directory, the actual Hermes Python interpreter (with its existing YAML
dependency), and the already resolved scanner executable. Do not print `.env`,
auth stores or full config in receipts. Keep private directories mode 0700 and
credential copies mode 0600; keep them out of git, uploads and shared evidence.

The example below is for the tested API-key `.env` setup. Replace all four paths
with inspected values. It deliberately fails if the destination already exists.

```bash
ORCH_RUN_DIR=/absolute/owned/run
SRC_PROFILE=/absolute/current/hermes-profile
HERMES_PY=/absolute/hermes-agent/venv/bin/python
SCANNER_SOURCE=/absolute/current/resolved/tirith
PRIVATE_PROFILE="$ORCH_RUN_DIR/profiles/worker"
"$HERMES_PY" - "$SRC_PROFILE" "$PRIVATE_PROFILE" "$SCANNER_SOURCE" <<'PY'
import hashlib, os, shutil, sys
from pathlib import Path
src, dst, scanner = map(Path, sys.argv[1:])
assert all(p.is_absolute() for p in (src, dst, scanner))
assert dst.parent.name == 'profiles'
assert (src/'config.yaml').is_file() and (src/'.env').is_file()
assert scanner.is_file() and os.access(scanner, os.X_OK)
dst.parent.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
dst.parent.parent.chmod(0o700)
dst.parent.mkdir(mode=0o700, exist_ok=True)
dst.mkdir(mode=0o700)
for name in ('config.yaml', '.env', 'SOUL.md'):
    if (src/name).is_file():
        shutil.copyfile(src/name, dst/name)
        (dst/name).chmod(0o600)
(dst/'bin').mkdir(mode=0o700)
shutil.copyfile(scanner, dst/'bin/tirith')
(dst/'bin/tirith').chmod(0o700)
assert hashlib.sha256(scanner.read_bytes()).digest() == hashlib.sha256((dst/'bin/tirith').read_bytes()).digest()
print('Private copies prepared; verify configuration and resolved runtime before launch.')
PY
env HERMES_HOME="$PRIVATE_PROFILE" hermes config path
```

Require the printed path to equal `$PRIVATE_PROFILE/config.yaml` **before any
config mutation**. In this version an explicit home whose immediate parent is
literally `profiles` is respected before sticky `active_profile` selection. An
arbitrary other directory may be overridden; `--profile` may also override the env.
Recheck this behavior after upgrades.

Materialize only task-relevant skills as independent files, e.g.
`shutil.copytree(source, destination, symlinks=False)` for inspected skill trees;
check that no copied skill path remains a symlink. Do not link to a mutable user
skill directory or copy all private memories, histories, databases or profile trees.
Native profile cloning can retain skill links. This recipe does **not** clone
`auth.json`, OAuth refresh stores or external credential pools. For those setups,
verify a supported provider-specific authentication route without token duplication
or policy changes before running a task. The tested `.env` case is not evidence
that OAuth/provider plugins will work identically.

Disable the two distinct background systems only in the copy:

```bash
env HERMES_HOME="$PRIVATE_PROFILE" hermes config set auxiliary.background_review.enabled false
env HERMES_HOME="$PRIVATE_PROFILE" hermes config set curator.enabled false
"$HERMES_PY" - "$SRC_PROFILE" "$PRIVATE_PROFILE" <<'PY'
import copy, sys, yaml
from pathlib import Path
src, dst = map(Path, sys.argv[1:])
expected = copy.deepcopy(yaml.safe_load((src/'config.yaml').read_text()))
expected.setdefault('auxiliary', {}).setdefault('background_review', {})['enabled'] = False
expected.setdefault('curator', {})['enabled'] = False
assert yaml.safe_load((dst/'config.yaml').read_text()) == expected
print('Config matches source except for the two local background flags.')
PY
```

`auxiliary.background_review.enabled` controls post-turn memory/skill review;
`curator.enabled` controls curator maintenance, including startup. Disabling only
one leaves the other eligible. A config read failure in background review defaults
to enabled, so YAML text alone is insufficient: check effective runtime settings.
Before the first model call, retain source/private fingerprints and inspect:

- `hermes config path` under the launch environment; expected model/provider and
  approval settings, comparing values locally without emitting secrets;
- effective `agent.background_review.load_background_review_settings()[0]` and
  `agent.curator.is_enabled()` using the installed Hermes interpreter/import path;
  both must be false in this private home;
- configured security paths and env overrides, including `TIRITH_BIN`. In this
  version `tools.tirith_security._load_security_config()` and network-free
  `_resolve_locally(configured_path, warn_missing=False)` reveal effective settings
  and `(resolved_path, may_install)`. These are internal, version-specific APIs.
  Require an executable with the intended source hash, and no required installation.
  Check other configured executables/plugins for the task too.

Copying `bin/tirith` alone does not override an explicit configured path or PATH
precedence. Resolve what will actually run. Preserve existing enabled/fail-open
policy; if required security dependencies are absent, fix setup before launch.
The first isolation experiment omitted the scanner: Hermes warned and later
auto-downloaded a different version. That startup failed runtime parity. Replacing
it with the original binary and exact-session resume passed; do not rely on
automatic installation or warning disappearance as proof of equivalent scanning.

## Launch and recover with the same home

Use an explicitly task-named workspace/tab and the normal orchestration ownership,
submission and watcher protocol. For a newly owned isolated herdr server, start its
managed process with this environment **before** creating/starting the agent:

```bash
env HERMES_HOME="$PRIVATE_PROFILE" herdr --session "$OWNED_SESSION" server
```

Retain the managed server handle. Merely prefixing a later herdr client command
does not change the environment of an already running server. For a shared server
or tmux, explicitly set the environment in the owned target's launch command and
verify it there; do not modify the parent shell/global server. Observe the actual
child home before allowing business work, plus model/approval/security preflight.

For direct launch in the assigned cwd:

```bash
env HERMES_HOME="$PRIVATE_PROFILE" hermes
# After a normal exit, in the same cwd and launch environment:
env HERMES_HOME="$PRIVATE_PROFILE" hermes --resume "$NATIVE_SESSION_ID"
```

Persist the absolute home, full native ID and launch argv/profile in the job record.
The PID changes on resume. Native runtime/session records can corroborate identity
when the herdr hook omits it; do not substitute protocol ID, pane ID or newest session.
An already existing default-home session stays in its original home: do not move
its DB into a new profile or assume the same ID exists there. Resume is not migration.

For an existing **default/root home** (not a `profiles/<name>` home), sticky profile
selection can also redirect a resume. Pin the default profile explicitly, check
the actual path, then resume with the identical environment/flags:

```bash
env HERMES_HOME="$ORIGINAL_PROFILE" hermes --profile default config path
# Require exactly $ORIGINAL_PROFILE/config.yaml before continuing.
env HERMES_HOME="$ORIGINAL_PROFILE" hermes --profile default --resume "$NATIVE_SESSION_ID"
```

Do not add `--profile default` to a private `profiles/worker` launch: it selects
that directory's root instead of worker. Verify the effective path for every
recovery; an environment variable alone is not evidence of the selected home.

## Observe and retire

Check the [lifecycle reference](lifecycle.md) even with both flags disabled. Retain
scoped before/after file hashes, native session/child records and curator ledger
actors; distinguish authorized foreground writes from background maintenance.
Export only the exact session under the same environment. A disabled flag or empty
ledger alone cannot prove that no process/plugin wrote elsewhere.

Keep the watcher active through acceptance and native exit/reuse checks. Exit the
owned TUI normally with `/exit`, verify real processes and native records, account
for side effects, then close the job and owned transport. Preserve the private home
for exact recovery/evidence; any later credential removal must follow the user's
retention policy. Never delete sessions or alter global approvals as cleanup.
