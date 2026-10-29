"""Opt-in exact-build caching; global npm installations are read-only inputs."""

import argparse
import asyncio
import fcntl
import hashlib
import json
import os
from pathlib import Path
import re
import shlex
import subprocess
import sys
import tempfile

if __package__:
    from . import bridge
else:
    import bridge


def native_package(entry):
    root = entry.resolve().parent.parent
    manifest = json.loads((root / 'package.json').read_text())
    architectures = {'aarch64': 'arm64', 'x86_64': 'x64'}
    if os.uname().machine not in architectures:
        raise ValueError('Unsupported Linux architecture')
    alias = '@openai/codex-linux-' + architectures[os.uname().machine]
    candidates = [root / 'node_modules' / alias]
    candidates += [p / 'node_modules' / alias for p in root.parents]
    found = next((p for p in candidates if p.is_dir()), None)
    if found is None:
        raise ValueError('Cannot find the native dependency of the selected Codex installation')
    matches = [p for p in (found / 'vendor').iterdir() if (p / bridge.VOICE / 'manifest.json').is_file()]
    if len(matches) != 1:
        raise ValueError('Native Codex package is ambiguous')
    package, voice = bridge.package_at(matches[0], probe=True)
    if manifest['version'] != voice['appVersion']:
        raise ValueError('Codex entry point and native package versions differ')
    return package, voice


def windows_spec(entry, manifest, arch='x64'):
    version = manifest['appVersion']
    if not re.fullmatch(r'\d+\.\d+\.\d+(?:-[A-Za-z0-9.]+)?', version) or arch not in ('x64', 'arm64'):
        raise ValueError('Unsupported package version or Windows architecture')
    package = json.loads((entry.resolve().parent.parent / 'package.json').read_text())
    spec = package['optionalDependencies'].get('@openai/codex-win32-' + arch)
    expected = 'npm:@openai/codex@' + version + '-win32-' + arch
    if spec != expected:
        raise ValueError('No exact matching Windows dependency in the installed Codex package')
    return spec.removeprefix('npm:')


def verify_voice(root, manifest, *, installed=None):
    if manifest.get('schemaVersion') != 1 or not manifest.get('sha256'):
        raise ValueError('Unsupported voice manifest')
    for relative, expected in manifest['sha256'].items():
        part = Path(relative)
        if (part.is_absolute() or '..' in part.parts
                or not (part.is_relative_to(bridge.VOICE) or part == Path('bin/codex'))):
            raise ValueError('Voice manifest path leaves its resource directory')
        target = root / part
        if target.is_symlink() or not target.is_file() or not target.resolve().is_relative_to(root.resolve()):
            raise ValueError('Voice manifest does not name a physical file inside the package')
        if installed and relative == str(bridge.VOICE / 'bin/codex-voice-host'):
            if target.read_bytes() != installed['launcher'].encode():
                raise ValueError('Managed helper was changed')
            backup = target.with_name(target.name + '.original-' + manifest['buildCommit'])
            if backup.is_symlink() or bridge.digest(backup.read_bytes()) != expected:
                raise ValueError('Original managed helper backup is missing or damaged')
        elif bridge.digest(target.read_bytes()) != expected:
            raise ValueError('Voice resource hash mismatch')


def request_sync(config):
    result = subprocess.run(['systemctl', '--user', 'start', '--no-block', config['managed']['unit']],
                            capture_output=True, timeout=5)
    if result.returncode:
        raise OSError('Background voice updater is unavailable')


def prepared_entry(state, config, manifest):
    build = manifest['buildCommit']
    if config['managed'].get('ready') != build:
        return None
    base = state / 'builds' / build
    try:
        installed = bridge.load_state(base / 'relay')
        package = Path(installed['package'])
        entry = base / 'node_modules/@openai/codex/bin/codex.js'
        _, actual = native_package(entry)
        helper = package / bridge.VOICE / 'bin/codex-voice-host'
        if (actual['buildCommit'] != build or actual['appVersion'] != manifest['appVersion']
                or installed.get('managedBuild') != build or helper.is_symlink()
                or helper.read_bytes() != installed['launcher'].encode()
                or not package.resolve().is_relative_to(base.resolve())):
            return None
        if installed['launcher'].encode() != bridge.launcher(base / 'relay'):
            return None
        verify_voice(package, actual, installed=installed)
        return entry
    except (OSError, ValueError, KeyError):
        return None


def launch(args):
    fallback = str(Path(args.codex).expanduser().resolve())
    entry = fallback
    arguments = args.arguments
    if arguments[:1] == ['--']:
        arguments = arguments[1:]
    try:
        state = args.state_dir.expanduser().resolve()
        config = bridge.load_state(state)
        if config['managed'].get('enabled', True) is False:
            os.execv(fallback, [fallback, *arguments])
            return 0
        _, manifest = native_package(Path(fallback))
        prepared = prepared_entry(state, config, manifest)
        if prepared:
            entry = str(prepared)
        else:
            settings = config['managed']
            build = manifest['buildCommit']
            error = settings.get('error') if settings.get('attemptedBuild') == build else None
            if settings.get('ready') == build:
                error = 'Prepared voice build no longer validates'
            if error:
                print('Codex voice: ' + str(error) + '; starting normal Codex.', file=sys.stderr)
            request_sync(config)
    except (OSError, ValueError, KeyError, subprocess.SubprocessError) as error:
        print('Codex voice: ' + str(error) + '; starting normal Codex.', file=sys.stderr)
    os.execv(entry, [entry, *arguments])


async def qualify(config):
    process = await asyncio.create_subprocess_exec(
        *config['ssh'], config['command'], stdin=asyncio.subprocess.PIPE,
        stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.DEVNULL,
        env=bridge.helper_environment())
    try:
        await bridge.inspect_process(process, config, argparse.Namespace(audio=False))
    except EOFError as error:
        if process.returncode == 255:
            raise OSError('Windows SSH transport closed during qualification') from error
        raise ValueError('Native helper closed before completing protocol qualification') from error


def save(state, config):
    bridge.atomic_write(state / 'config.json', json.dumps(config).encode())


def sync(args):
    state = args.state_dir.expanduser().resolve()
    # A separate fd avoids holding config.json across atomic replacement.
    with open(state / 'sync.lock', 'a') as lock:
        try:
            fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            return 0
        config = bridge.load_state(state)
        settings = config['managed']
        if settings.get('enabled', True) is False:
            return 0
        entry = Path(settings['codex'])
        try:
            _, manifest = native_package(entry)
            build = manifest['buildCommit']
            if prepared_entry(state, config, manifest):
                return 0
            if settings.get('status') == 'held' and settings.get('attemptedBuild') == build:
                print('Codex voice: held; inspect status before retrying this build.', file=sys.stderr)
                return 0
            settings['attemptedBuild'] = build
            builds = state / 'builds'
            builds.mkdir(mode=0o700, exist_ok=True)
            final = builds / build
            if final.exists():
                candidate = {**config, 'managed': {**settings, 'ready': build}}
                if prepared_entry(state, candidate, manifest) is None:
                    raise ValueError('Published build failed validation; preserving it for inspection')
                asyncio.run(qualify(bridge.load_state(final / 'relay')))
                settings.update(ready=build, status='ready', error=None)
                save(state, config)
                return 0
            ssh = bridge.ssh_args(settings['host'], settings.get('identity'))
            arch = subprocess.run(ssh + ['node.exe -p process.arch'], check=True,
                                  capture_output=True, text=True, timeout=15,
                                  env=bridge.helper_environment()).stdout.strip()
            spec = windows_spec(entry, manifest, arch)
            with tempfile.TemporaryDirectory(prefix='.prepare-', dir=builds) as temporary:
                stage = Path(temporary)
                subprocess.run(['npm', 'install', '--prefix', str(stage), '--ignore-scripts',
                                '--no-audit', '--no-fund', '--save-exact', '@openai/codex@' + manifest['appVersion']],
                               check=True, timeout=7200, stdout=sys.stderr, stderr=sys.stderr)
                staged_entry = stage / 'node_modules/@openai/codex/bin/codex.js'
                root, staged_manifest = native_package(staged_entry)
                if (staged_manifest['buildCommit'], staged_manifest['appVersion']) != (build, manifest['appVersion']):
                    raise ValueError('Prepared Linux package does not match the requested build')
                verify_voice(root, staged_manifest)
                relay = stage / 'relay'
                options = argparse.Namespace(
                    state_dir=relay, package=str(root), host=settings['host'], identity=settings.get('identity'),
                    windows_package=None, managed=True,
                    provision={'spec': spec, 'version': manifest['appVersion'], 'build': build},
                    runtime_state=final / 'relay', runtime_package=final / root.relative_to(stage))
                bridge.install(options)
                installed = bridge.load_state(relay)
                asyncio.run(qualify(installed))
                _, current = native_package(entry)
                if current['buildCommit'] != build:
                    raise OSError('Codex changed during preparation; retrying the current version')
                os.rename(stage, final)
                directory = os.open(builds, os.O_DIRECTORY)
                try:
                    os.fsync(directory)
                finally:
                    os.close(directory)
            _, current = native_package(entry)
            if current['buildCommit'] != build:
                raise OSError('Codex changed during publication; retrying the current version')
            settings.update(ready=build, status='ready', error=None)
            save(state, config)
            print('Codex voice ready for the next launch: ' + manifest['appVersion'])
            return 0
        except (OSError, ValueError, KeyError, subprocess.SubprocessError) as error:
            settings.update(status='pending' if isinstance(error, (OSError, subprocess.SubprocessError)) else 'held',
                            error=str(error))
            save(state, config)
            print('Codex voice sync: ' + str(error), file=sys.stderr)
            return 1 if settings['status'] == 'pending' else 0


def status(args):
    state = args.state_dir.expanduser().resolve()
    config = bridge.load_state(state)
    _, manifest = native_package(Path(config['managed']['codex']))
    enabled = config['managed'].get('enabled', True)
    ready = enabled and prepared_entry(state, config, manifest) is not None
    recorded = config['managed'].get('status', 'pending')
    error = config['managed'].get('error')
    if not enabled:
        recorded = 'disabled'
    elif config['managed'].get('attemptedBuild') not in (None, manifest['buildCommit']):
        recorded, error = 'pending', None
    print(json.dumps({'version': manifest['appVersion'], 'buildCommit': manifest['buildCommit'],
                      'status': 'ready' if ready else recorded if recorded in ('held', 'disabled') else 'pending',
                      'relayState': str(state / 'builds' / manifest['buildCommit'] / 'relay') if ready else None,
                      'error': error}))
    return 0 if ready else 1


def manage(args):
    state = args.state_dir.expanduser().resolve()
    entry = Path(args.codex).expanduser().resolve()
    identity = str(Path(args.identity).expanduser().resolve()) if args.identity else None
    native_package(entry)
    bridge.ssh_args(args.host, identity)
    target = args.launcher.expanduser().absolute()
    if target.resolve() == entry:
        raise ValueError('Launcher must not replace the real Codex executable')
    state.mkdir(mode=0o700, parents=True, exist_ok=True)
    with open(state / 'sync.lock', 'a') as lock, bridge.helper_lock(state / 'config.json'):
        fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        previous = bridge.load_state(state) if (state / 'config.json').exists() else None
        desired = {'codex': str(entry), 'host': args.host, 'identity': identity, 'launcher': str(target)}
        if previous and any(previous.get('managed', {}).get(k) != v for k, v in desired.items()):
            raise ValueError('State manages another installation; preserving it')
        if target.exists() or target.is_symlink():
            current = target.read_bytes()
            recognized = (previous and (bridge.digest(current) == previous['managed']['launcherHash']
                          or current.decode() == previous['managed']['originalLauncher']))
            if not recognized and bridge.digest(current) != args.replace_launcher_sha256:
                raise ValueError('Existing launcher differs; supply its exact replacement hash')
        else:
            current = None
        unit = 'genesis-codex-voice-sync-' + hashlib.sha256(str(state).encode()).hexdigest()[:12] + '.service'
        source = Path(bridge.__file__).parent
        hashes = {}
        assets = {}
        for name in ('bridge.py', 'managed.py', 'host.cjs', 'setup.ps1'):
            data = (source / name).read_bytes()
            destination = state / name
            if destination.exists() or destination.is_symlink():
                expected = previous['managed'].get('assetHashes', {}).get(name) if previous else bridge.digest(data)
                if destination.is_symlink() or bridge.digest(destination.read_bytes()) != expected:
                    raise ValueError('Managed source asset changed; preserving it')
            hashes[name] = bridge.digest(data)
            if previous and hashes[name] != previous['managed'].get('assetHashes', {}).get(name):
                raise ValueError('Manager code changed; use a new --state-dir for this revision')
            assets[destination] = data
        # Validate the entire asset set before writing any of it. Manager code
        # stays fixed within this state; only native Codex builds auto-update.
        for destination, data in assets.items():
            bridge.atomic_write(destination, data)
        config = {'application': 'genesis-codex-voice', 'managed': {
            **desired, 'unit': unit, 'enabled': True,
            'originalLauncher': (previous['managed']['originalLauncher'] if previous
                                 else current.decode() if current is not None else None),
            'originalMode': (previous['managed']['originalMode'] if previous else
                             (target.stat().st_mode & 0o777) if current is not None else None),
            'assetHashes': hashes, 'status': 'pending'}}
        script = ('#!/bin/sh\n# genesis-codex-voice managed launcher\nexec ' + shlex.quote(sys.executable)
                  + ' ' + shlex.quote(str(state / 'bridge.py')) + ' launch --state-dir ' + shlex.quote(str(state))
                  + ' --codex ' + shlex.quote(str(entry)) + ' -- "$@"\n').encode()
        config['managed']['launcherHash'] = bridge.digest(script)
        unit_dir = Path.home() / '.config/systemd/user'
        unit_dir.mkdir(parents=True, exist_ok=True)
        unit_path = unit_dir / unit
        # systemd arguments use its own quoting, not shell quoting.
        def quote(s):
            return json.dumps(str(s)).replace('%', '%%').replace('$', '$$')
        command = ' '.join(quote(s) for s in (sys.executable, state / 'bridge.py', 'sync', '--state-dir', state))
        environment = json.dumps('PATH=' + os.environ.get('PATH', '/usr/bin:/bin')).replace('%', '%%')
        writable_state = json.dumps(str(state)).replace('%', '%%')
        unit_text = ('[Unit]\nDescription=Codex native voice build synchronization\nStartLimitIntervalSec=0\n'
                     '[Service]\nType=oneshot\nExecStart=' + command + '\nEnvironment=' + environment
                     + '\nTimeoutStartSec=7200\nRestart=on-failure\nRestartSec=5min\n'
                     'NoNewPrivileges=yes\nProtectSystem=strict\nReadWritePaths=%h ' + writable_state + '\n')
        if unit_path.is_symlink() or (unit_path.exists() and unit_path.read_text() != unit_text):
            raise ValueError('Updater unit differs from this installation; preserving it')
        config['managed']['unitHash'] = bridge.digest(unit_text.encode())
        bridge.atomic_write(unit_path, unit_text.encode())
        save(state, config)
        target.parent.mkdir(parents=True, exist_ok=True)
        bridge.atomic_write(target, script, 0o755)
        subprocess.run(['systemctl', '--user', 'daemon-reload'], check=True)
    request_sync(config)
    print('Managed voice installed; text starts immediately while matching helpers are prepared.')
    return 0


def unmanage(args):
    state = args.state_dir.expanduser().resolve()
    config = bridge.load_state(state)
    settings = config['managed']
    unit = Path.home() / '.config/systemd/user' / settings['unit']
    if unit.is_symlink() or bridge.digest(unit.read_bytes()) != settings['unitHash']:
        raise ValueError('Updater unit changed; preserving it and recovery state')
    settings['enabled'] = False
    save(state, config)
    subprocess.run(['systemctl', '--user', 'stop', settings['unit']], check=True)
    target = Path(settings['launcher'])
    if target.is_symlink() or bridge.digest(target.read_bytes()) != settings['launcherHash']:
        raise ValueError('Launcher changed; preserving it and all recovery state')
    original = settings['originalLauncher']
    if original is None:
        target.unlink()
    else:
        bridge.atomic_write(target, original.encode(), settings['originalMode'])
    unit.unlink()
    subprocess.run(['systemctl', '--user', 'daemon-reload'], check=True)
    settings['status'] = 'disabled'
    save(state, config)
    print('Managed launcher restored; immutable builds retained for active sessions and rollback.')
    return 0
