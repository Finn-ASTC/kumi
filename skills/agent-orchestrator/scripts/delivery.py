#!/usr/bin/env python3
"""Capture scoped source handoffs and run explicit checks in fresh local copies.

No Git dependency. This is provenance tooling, not an operating-system sandbox.
Verification executes only the caller's explicit argv plan, never a shell string.
"""

import argparse
import hashlib
import json
import math
import os
from pathlib import Path, PurePosixPath
import platform
import re
import shutil
import signal
import stat
import subprocess
import sys
import tempfile
import time
from typing import Any
import uuid

import protocol


require = protocol.require


def relative(value: Any, allow_dot: bool = False) -> str:
    """Accept normalized project-relative paths, without parent traversal."""
    require(isinstance(value, str) and value and '\x00' not in value and '\\' not in value,
            'invalid relative path')
    path = PurePosixPath(value)
    require(not path.is_absolute() and '..' not in path.parts and path.as_posix() == value and
            (allow_dot or value != '.'), f'path must be normalized and relative: {value}')
    return value


def under(name: str, parent: str) -> bool:
    """Test lexical subtree membership for explicit includes and excludes."""
    return parent == '.' or name == parent or name.startswith(parent + '/')


def stamp(value: os.stat_result) -> tuple[int, ...]:
    """Detect replacement or writes even when a writer restores earlier bytes."""
    return (value.st_dev, value.st_ino, value.st_mode, value.st_size, value.st_mtime_ns, value.st_ctime_ns)


def file_record(path: Path, destination: Path | None = None) -> dict[str, Any]:
    """Hash/copy a stable regular file without following a final symlink."""
    before = path.lstat()
    require(stat.S_ISREG(before.st_mode), f'not a regular file: {path}')
    digest = hashlib.sha256()
    descriptor = os.open(path, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK)
    with os.fdopen(descriptor, 'rb') as source:
        require(stamp(before) == stamp(os.fstat(source.fileno())), f'file changed before read: {path}')
        output = destination.open('xb') if destination is not None else None
        try:
            for block in iter(lambda: source.read(65536), b''):
                digest.update(block)
                if output is not None:
                    output.write(block)
            if output is not None:
                output.flush()
                os.fsync(output.fileno())
            require(stamp(before) == stamp(os.fstat(source.fileno())) == stamp(path.lstat()),
                    f'file changed during read: {path}')
        finally:
            if output is not None:
                output.close()
    return {'kind': 'file', 'size': before.st_size, 'sha256': digest.hexdigest(),
            'executable': bool(before.st_mode & 0o111)}


def inventory(root: Path, include: list[str], exclude: list[str]) -> tuple[dict[str, Any], dict[str, Any]]:
    """Inventory selected files, directories and safe internal relative symlinks."""
    files: dict[str, Any] = {}
    stability: dict[str, Any] = {'.': stamp(root.stat())}
    for name in include:
        require(os.path.lexists(root / name), f'included path missing: {name}')

    def visit(directory: Path) -> None:
        for path in sorted(directory.iterdir()):
            name = path.relative_to(root).as_posix()
            if '.git' in path.relative_to(root).parts or any(under(name, e) for e in exclude):
                continue
            if not any(under(name, i) or under(i, name) for i in include):
                continue
            metadata = path.lstat()
            stability[name] = stamp(metadata)
            if stat.S_ISLNK(metadata.st_mode):
                target = os.readlink(path)
                require(not os.path.isabs(target) and path.resolve().is_relative_to(root) and path.exists(),
                        f'link must resolve inside source: {name}')
                files[name] = {'kind': 'symlink', 'target': target}
            elif stat.S_ISDIR(metadata.st_mode):
                files[name] = {'kind': 'directory'}
                visit(path)
            else:
                files[name] = file_record(path)
    visit(root)
    return files, stability


def copy_tree(source: Path, destination: Path, files: dict[str, Any]) -> None:
    """Make independent inodes; never hardlink a live checkout or another attempt."""
    destination.mkdir(mode=0o700)
    for name, entry in sorted(files.items()):
        target = destination / relative(name)
        target.parent.mkdir(parents=True, exist_ok=True)
        if entry['kind'] == 'directory':
            target.mkdir(exist_ok=True)
        elif entry['kind'] == 'symlink':
            target.symlink_to(entry['target'])
        else:
            require(file_record(source / name, target) == entry, f'source changed while copying: {name}')
            target.chmod(0o755 if entry['executable'] else 0o644)


def diff(before: dict[str, Any], after: dict[str, Any]) -> dict[str, list[str]]:
    """Compare content and executable/link identities, not timestamps or authorship."""
    return {'added': sorted(after.keys() - before.keys()), 'deleted': sorted(before.keys() - after.keys()),
            'modified': sorted(k for k in before.keys() & after.keys() if before[k] != after[k])}


def allocate(root: str | Path | None, default: Path, source: Path, prefix: str) -> Path:
    """Allocate evidence outside the live source so capture cannot recurse into it."""
    base = Path(root).resolve() if root is not None else default.resolve()
    require(not base.is_relative_to(source), 'evidence root must be outside source cwd')
    base.mkdir(mode=0o700, parents=True, exist_ok=True)
    return Path(tempfile.mkdtemp(prefix=prefix, dir=base)).resolve()


def sealed(path: Path, value: dict[str, Any]) -> None:
    """Publish a manifest then a separate digest; interrupted publication is unusable."""
    protocol.publish(path, value)
    protocol.publish(path.with_name(path.stem + '-seal.json'), {'sha256': file_record(path)['sha256']})


def load_snapshot(path: str | Path) -> tuple[Path, dict[str, Any]]:
    """Validate manifest identity/digest before using its source paths."""
    path = Path(path).resolve()
    require(path.name == 'snapshot.json', 'expected snapshot.json')
    seal = protocol.read_json(path.with_name('snapshot-seal.json'))
    require(file_record(path)['sha256'] == seal.get('sha256'), 'snapshot manifest changed')
    value = protocol.read_json(path)
    require(type(value.get('version')) is int and value['version'] == 1, 'unsupported snapshot version')
    require(value.get('snapshot_path') == str(path) and value.get('source_path') == str(path.parent / 'source'),
            'snapshot paths changed')
    require(isinstance(value.get('files'), dict) and isinstance(value.get('snapshot_id'), str) and
            re.fullmatch('[0-9a-f]{32}', value['snapshot_id']), 'invalid snapshot identity/files')
    for name, entry in value['files'].items():
        relative(name)
        require(isinstance(entry, dict) and entry.get('kind') in ('file', 'directory', 'symlink'),
                'invalid snapshot entry')
    return path, value


def check(snapshot_path: str | Path) -> dict[str, Any]:
    """Verify the stored copy without reading the author's current checkout."""
    path, value = load_snapshot(snapshot_path)
    source = path.parent / 'source'
    require(source.resolve() == source and source.is_dir(), 'snapshot source redirected or missing')
    try:
        actual, _ = inventory(source, ['.'], [])
        changes = diff(value['files'], actual)
        return {'snapshot_path': str(path), 'valid': not any(changes.values()), 'changes': changes}
    except (OSError, ValueError, RuntimeError) as exc:
        return {'snapshot_path': str(path), 'valid': False, 'error': str(exc)}


def capture(request_path: str | Path, include: list[str], exclude: list[str], handoff: str,
            root: str | Path | None = None, baseline: str | Path | None = None) -> dict[str, Any]:
    """Capture after a scoped handoff and compare two source scans around the copy."""
    request_path = Path(request_path).resolve()
    request_sha256 = file_record(request_path)['sha256']
    request = protocol.load_request(request_path, require_cwd=True)
    source = Path(request['cwd']).resolve()
    require(isinstance(include, list) and bool(include) and isinstance(exclude, list), 'explicit includes required')
    include = sorted(set(relative(i, True) for i in include))
    exclude = sorted(set(relative(i, True) for i in exclude))
    require(isinstance(handoff, str) and bool(handoff.strip()) and len(handoff) <= 2000, 'handoff note required')
    previous = None
    if baseline is not None:
        _, previous = load_snapshot(baseline)
        require(check(baseline)['valid'], 'baseline copy changed')
        require(previous['source_root'] == str(source) and previous['scope'] == {'include': include, 'exclude': exclude},
                'baseline must have the same source root and scope')
    directory = allocate(root, request_path.parent / 'deliveries', source, 'snapshot-')
    try:
        before, stable_before = inventory(source, include, exclude)
        copy_tree(source, directory / 'source', before)
        after, stable_after = inventory(source, include, exclude)
        copied, _ = inventory(directory / 'source', ['.'], [])
        require(before == after == copied and stable_before == stable_after,
                'source changed during handoff; retain failure and retry after writers yield')
        response = None
        result_sha256 = None
        result_path = Path(request['result_path'])
        if os.path.lexists(result_path):
            result_sha256 = file_record(result_path)['sha256']
            response = protocol.validate_result(request, protocol.read_json(result_path))
            require(file_record(result_path)['sha256'] == result_sha256, 'response changed during capture')
        require(file_record(request_path)['sha256'] == request_sha256, 'request changed during capture')
        actual_diff = diff(previous['files'], copied) if previous is not None else None
        declared = {k: response[k] for k in protocol.FILE_FIELDS} if response is not None else None
        warnings = []
        if previous is None:
            warnings.append({'code': 'no_baseline',
                             'message': 'No baseline: actual_diff and declaration_comparison are unavailable.'})
        if response is None:
            warnings.append({'code': 'no_result',
                             'message': 'No result: declarations and declaration_comparison are unavailable.'})
        comparison = None
        if declared is not None and actual_diff is not None:
            changed = set().union(*(set(names) for names in actual_diff.values()))
            changed = {n for n in changed if copied.get(n, previous['files'].get(n, {})).get('kind') != 'directory'}
            reported = set().union(*(set(names) for names in declared.values()))
            def selected(name: str) -> bool:
                return (any(under(name, i) for i in include) and not any(under(name, e) for e in exclude)
                        and '.git' not in PurePosixPath(name).parts)
            comparison = {'undeclared_changes': sorted(changed - reported),
                          'declared_without_scoped_change': sorted(n for n in reported - changed if selected(n)),
                          'outside_snapshot_scope': sorted(n for n in reported if not selected(n)),
                          'category_mismatches': sorted(n for field, names in declared.items() for n in names
                              if n in changed and n not in (
                                  actual_diff['added'] if field == 'files_created' else
                                  actual_diff['modified'] if field == 'files_modified' else
                                  actual_diff['deleted'] if field == 'files_deleted' else
                                  actual_diff['added'] + actual_diff['modified'])),
                          'attribution': 'unknown; a scoped diff does not identify the writer'}
        value = {'version': 1, 'snapshot_id': uuid.uuid4().hex, 'snapshot_path': str(directory / 'snapshot.json'),
                 'source_path': str(directory / 'source'), 'source_root': str(source),
                 'job_id': request['job_id'], 'round_id': request['round_id'], 'request_path': str(request_path),
                 'request_sha256': request_sha256, 'captured_at': protocol.utc_now(),
                 'handoff': handoff, 'scope': {'include': include, 'exclude': exclude},
                 'always_excluded': '.git entries', 'files': copied, 'baseline':
                 {'path': str(Path(baseline).resolve()), 'sha256': file_record(Path(baseline).resolve())['sha256']}
                 if baseline is not None else None, 'actual_diff': actual_diff, 'declarations': declared,
                 'declaration_comparison': comparison, 'result_sha256': result_sha256, 'warnings': warnings,
                 'stability': 'source inventory/stat before and after copy matched captured content; not a filesystem transaction'}
        for name, entry in copied.items():
            if entry['kind'] == 'file':
                (directory / 'source' / name).chmod(0o555 if entry['executable'] else 0o444)
        sealed(directory / 'snapshot.json', value)
        return {k: value[k] for k in ('snapshot_path', 'snapshot_id', 'source_path', 'job_id', 'round_id', 'warnings')}
    except (OSError, ValueError, RuntimeError) as exc:
        protocol.publish(directory / 'failure.json', {'error': str(exc), 'recorded_at': protocol.utc_now()})
        raise protocol.ProtocolError(f'{exc}; capture evidence: {directory}') from exc


def validate_plan(plan: Any) -> None:
    """Require an explicit finite argv plan and declared dependency context."""
    require(isinstance(plan, dict) and set(plan) == {'commands', 'artifacts', 'dependencies', 'environment'},
            'plan requires commands, artifacts, dependencies and environment')
    protocol.validate_json_values(plan)
    commands = plan['commands']
    require(isinstance(commands, list) and bool(commands), 'at least one check command is required')
    for command in commands:
        require(isinstance(command, dict) and set(command) == {'argv', 'timeout_seconds'}, 'invalid command fields')
        argv, timeout = command['argv'], command['timeout_seconds']
        require(isinstance(argv, list) and bool(argv) and all(isinstance(a, str) and '\x00' not in a for a in argv)
                and bool(argv[0]), 'command argv must be a nonempty string array')
        require(type(timeout) in (int, float) and math.isfinite(timeout) and 0 < timeout <= 3600,
                'command timeout must be 0 < seconds <= 3600')
    require(isinstance(plan['artifacts'], list), 'artifacts must be an array')
    for name in plan['artifacts']:
        relative(name)
        require(name.startswith('build/'), 'declared artifacts must be under this attempt build/')
    require(len(set(plan['artifacts'])) == len(plan['artifacts']), 'duplicate artifacts')
    require(isinstance(plan['dependencies'], dict) and all(isinstance(k, str) and bool(k) and
            (v is None or isinstance(v, str)) for k, v in plan['dependencies'].items()), 'invalid dependency versions')
    require(isinstance(plan['environment'], dict), 'environment must be an object')
    reserved = {'HOME', 'CODEX_HOME', 'ORCH_SOURCE_DIR', 'ORCH_BUILD_DIR', 'ORCH_CACHE_DIR',
                'TMPDIR', 'XDG_CACHE_HOME', 'PYTHONDONTWRITEBYTECODE'}
    for key, value in plan['environment'].items():
        require(isinstance(key, str) and re.fullmatch('[A-Za-z_][A-Za-z_0-9]*', key) and key not in reserved and
                isinstance(value, str) and '\x00' not in value, 'invalid or reserved environment override')


def reference(path: Path) -> dict[str, Any]:
    """Pin evidence bytes while keeping large logs out of the controller response."""
    return {'path': str(path), **file_record(path)}


def execute(command: dict[str, Any], source: Path, environment: dict[str, str], evidence: Path,
            expand: Any) -> dict[str, Any]:
    """Run one authorized argv with logs and bounded cleanup of its process group."""
    evidence.mkdir(mode=0o700)
    argv = [expand(a) for a in command['argv']]
    started = time.monotonic()
    record: dict[str, Any] = {'argv': argv, 'cwd': str(source), 'started_at': protocol.utc_now(),
                              'timeout_seconds': command['timeout_seconds'], 'exit_code': None}
    protocol.publish(evidence / 'started.json', record)
    process = None
    with (evidence / 'stdout.log').open('xb') as stdout, (evidence / 'stderr.log').open('xb') as stderr:
        try:
            if Path(argv[0]).is_absolute():
                program = Path(argv[0])
            elif '/' in argv[0]:
                program = source / argv[0]
            else:
                search = os.pathsep.join(str(Path(part) if Path(part).is_absolute() else source / part)
                                         for part in environment.get('PATH', os.defpath).split(os.pathsep))
                found = shutil.which(argv[0], path=search)
                if found is None:
                    raise FileNotFoundError(f'executable not found in verification PATH: {argv[0]}')
                program = Path(found)
            record['executable'] = reference(program.resolve())
            process = subprocess.Popen(argv, executable=str(program.resolve()), cwd=source, env=environment, stdin=subprocess.DEVNULL,
                                       stdout=stdout, stderr=stderr, start_new_session=True)
            record['exit_code'] = process.wait(timeout=command['timeout_seconds'])
            record['outcome'] = 'exited'
        except subprocess.TimeoutExpired:
            record['outcome'] = 'timeout'
        except OSError as exc:
            record.update(outcome='launch_error', error=str(exc))
        finally:
            if process is not None:
                # Retire children in this owned process group even after a normal
                # parent exit. A process that deliberately escapes is not contained.
                try:
                    os.killpg(process.pid, signal.SIGKILL)
                except ProcessLookupError:
                    pass
                process.wait()
                if record['exit_code'] is None:
                    record['exit_code'] = process.returncode
    record.update(finished_at=protocol.utc_now(), duration_seconds=time.monotonic() - started,
                  stdout=reference(evidence / 'stdout.log'), stderr=reference(evidence / 'stderr.log'))
    protocol.publish(evidence / 'result.json', record)
    return record


def verify(snapshot_path: str | Path, plan: dict[str, Any], root: str | Path | None = None) -> dict[str, Any]:
    """Run a new verification attempt; never reuse historical source/cache/evidence."""
    validate_plan(plan)
    path, snapshot = load_snapshot(snapshot_path)
    require(check(path)['valid'], 'snapshot copy changed; refusing verification')
    base = Path(root).resolve() if root is not None else path.parent / 'verifications'
    require(not base.is_relative_to(path.parent / 'source'), 'verification root must be outside snapshot source')
    directory = allocate(base, path.parent / 'verifications', Path(snapshot['source_root']), 'verify-')
    source = directory / 'source'
    copy_tree(path.parent / 'source', source, snapshot['files'])
    copied, source_stability = inventory(source, ['.'], [])
    require(copied == snapshot['files'] and check(path)['valid'], 'snapshot changed while making verification copy')
    for name in ('build', 'cache', 'tmp', 'evidence'):
        (directory / name).mkdir(mode=0o700)
    replacements = {name: str(directory / name) for name in ('source', 'build', 'cache', 'tmp', 'evidence')}

    def expand(value: str) -> str:
        for name, replacement in replacements.items():
            value = value.replace('{' + name + '}', replacement)
        return value

    overrides = {'ORCH_SOURCE_DIR': str(source), 'ORCH_BUILD_DIR': str(directory / 'build'),
                 'ORCH_CACHE_DIR': str(directory / 'cache'), 'TMPDIR': str(directory / 'tmp'),
                 'XDG_CACHE_HOME': str(directory / 'cache'), 'PYTHONDONTWRITEBYTECODE': '1',
                 **{k: expand(v) for k, v in plan['environment'].items()}}
    manifest = {'version': 1, 'attempt_id': uuid.uuid4().hex, 'attempt_path': str(directory / 'attempt.json'),
                'snapshot_path': str(path), 'snapshot_sha256': file_record(path)['sha256'],
                'job_id': snapshot['job_id'], 'round_id': snapshot['round_id'], 'created_at': protocol.utc_now(),
                'plan': plan, 'source_path': str(source), 'environment_overrides': overrides,
                'runtime': {'platform': platform.platform(), 'helper_python': sys.version,
                            'ambient_environment': 'inherited, not serialized; dependency versions are caller declarations'}}
    sealed(directory / 'attempt.json', manifest)
    records = []
    source_unchanged = True
    source_changes: list[dict[str, Any]] = []
    for index, command in enumerate(plan['commands'], 1):
        record = execute(command, source, {**os.environ, **overrides}, directory / 'evidence' / f'{index:04d}', expand)
        records.append(record)
        try:
            after, after_stability = inventory(source, ['.'], [])
            source_after = {'files': after}
            changes = diff(snapshot['files'], after)
            metadata_changed = sorted(n for n in source_stability.keys() | after_stability.keys()
                                      if source_stability.get(n) != after_stability.get(n))
            unchanged = not any(changes.values()) and not metadata_changed
        except (OSError, ValueError, RuntimeError) as exc:
            source_after = {'error': str(exc)}
            changes, unchanged, metadata_changed = {'error': str(exc)}, False, []
        source_changes.append({'command': index, 'changes': changes, 'metadata_changed': metadata_changed})
        source_unchanged = source_unchanged and unchanged
        if not unchanged or record['outcome'] != 'exited' or record['exit_code'] != 0:
            break
    protocol.publish(directory / 'evidence' / 'source-after.json', source_after)
    artifacts, artifact_errors = {}, []
    for name in plan['artifacts']:
        target = directory / name
        try:
            require(target.resolve().is_relative_to(directory / 'build'), 'artifact escaped build directory')
            artifacts[name] = reference(target)
        except (OSError, ValueError, RuntimeError) as exc:
            artifact_errors.append({'path': name, 'error': str(exc)})
    try:
        snapshot_check = check(path)
    except (OSError, ValueError, RuntimeError) as exc:
        snapshot_check = {'valid': False, 'error': str(exc)}
    snapshot_valid = snapshot_check['valid']
    passed = (source_unchanged and snapshot_valid and not artifact_errors and len(records) == len(plan['commands'])
              and all(r['outcome'] == 'exited' and r['exit_code'] == 0 for r in records))
    result = {'version': 1, 'attempt_path': str(directory / 'attempt.json'), 'snapshot_path': str(path),
              'passed': passed, 'finished_at': protocol.utc_now(), 'commands': records,
              'source_unchanged': source_unchanged, 'source_changes': source_changes,
              'source_after': reference(directory / 'evidence' / 'source-after.json'),
              'snapshot_valid': snapshot_valid, 'snapshot_check': snapshot_check, 'artifacts': artifacts, 'artifact_errors': artifact_errors,
              'evidence_directory': str(directory / 'evidence'),
              'meaning': 'explicit checks against this snapshot; not complete product acceptance or OS isolation'}
    sealed(directory / 'result.json', result)
    return result


def inspect(attempt_path: str | Path) -> dict[str, Any]:
    """Read back pinned logs/artifacts; incomplete work never becomes a passing verdict."""
    path = Path(attempt_path).resolve()
    require(path.name == 'attempt.json', 'expected attempt.json')
    errors = []
    output: dict[str, Any] = {'attempt_path': str(path), 'complete': False,
                             'evidence_valid': False, 'passed': None, 'errors': errors}
    try:
        for manifest_path in (path, path.with_name('result.json')):
            seal = protocol.read_json(manifest_path.with_name(manifest_path.stem + '-seal.json'))
            require(file_record(manifest_path)['sha256'] == seal.get('sha256'), 'attempt manifest/result changed')
        attempt = protocol.read_json(path)
        result = protocol.read_json(path.with_name('result.json'))
        require(attempt.get('attempt_path') == result.get('attempt_path') == str(path), 'attempt identity changed')
        require(attempt.get('source_path') == str(path.parent / 'source'), 'attempt source path changed')
        require(file_record(Path(attempt['snapshot_path']))['sha256'] == attempt['snapshot_sha256'],
                'bound snapshot manifest changed')
        require(check(attempt['snapshot_path'])['valid'], 'bound snapshot copy changed')
        references = [result['source_after'], *result['artifacts'].values()]
        for command in result['commands']:
            references.extend([command['stdout'], command['stderr']])
        for ref in references:
            target = Path(ref['path'])
            try:
                require(target.is_absolute() and target.resolve().is_relative_to(path.parent), 'evidence escaped attempt')
                require(reference(target) == ref, f'evidence changed: {target}')
            except (OSError, ValueError, RuntimeError) as exc:
                errors.append(str(exc))
        try:
            require((path.parent / 'source').resolve() == path.parent / 'source', 'verification source redirected')
            actual, _ = inventory(path.parent / 'source', ['.'], [])
            saved = protocol.read_json(path.parent / 'evidence' / 'source-after.json')
            require(actual == saved.get('files'), 'verification source changed after evidence recording')
        except (OSError, ValueError, RuntimeError) as exc:
            errors.append(str(exc))
        output.update(complete=True, evidence_valid=not errors, passed=result['passed'] and not errors,
                      snapshot_path=attempt['snapshot_path'], recorded_passed=result['passed'],
                      command_count=len(result['commands']), artifact_count=len(result['artifacts']),
                      result_path=str(path.with_name('result.json')))
    except (OSError, ValueError, RuntimeError, KeyError, TypeError) as exc:
        errors.append(str(exc))
    return output


def main() -> int:
    """Expose bounded snapshot/copy/check operations with JSON results."""
    parser = argparse.ArgumentParser(description=__doc__)
    commands = parser.add_subparsers(dest='command', required=True)
    capture_cli = commands.add_parser('capture', help='capture an explicit scoped handoff outside source cwd')
    capture_cli.add_argument('--request', required=True)
    capture_cli.add_argument('--include', action='append', required=True)
    capture_cli.add_argument('--exclude', action='append', default=[])
    capture_cli.add_argument('--handoff', required=True)
    capture_cli.add_argument('--baseline')
    capture_cli.add_argument('--root')
    check_cli = commands.add_parser('check', help='validate the stored source copy without reading live source')
    check_cli.add_argument('--snapshot', required=True)
    verify_cli = commands.add_parser('verify', help='execute an explicit argv plan in a fresh verification copy')
    verify_cli.add_argument('--snapshot', required=True)
    verify_cli.add_argument('--plan', required=True)
    verify_cli.add_argument('--root')
    inspect_cli = commands.add_parser('inspect', help='read back pinned verification evidence and verdict')
    inspect_cli.add_argument('--attempt', required=True)
    args = parser.parse_args()
    try:
        if args.command == 'capture':
            output = capture(args.request, args.include, args.exclude, args.handoff, args.root, args.baseline)
            code = 0
        elif args.command == 'check':
            output = check(args.snapshot)
            code = 0 if output['valid'] else 1
        elif args.command == 'inspect':
            output = inspect(args.attempt)
            code = 0 if output['complete'] and output['evidence_valid'] and output['passed'] else 1
        else:
            output = verify(args.snapshot, protocol.read_json(args.plan), args.root)
            code = 0 if output['passed'] else 1
        print(json.dumps(output, ensure_ascii=False))
        return code
    except (OSError, ValueError, RuntimeError, KeyError, TypeError) as exc:
        print(json.dumps({'error': str(exc)}, ensure_ascii=False), file=sys.stderr)
        return 2


if __name__ == '__main__':
    raise SystemExit(main())
