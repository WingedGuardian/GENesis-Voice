#!/usr/bin/env python3
"""Opt-in native Windows audio relay, with optional managed build updates."""
import argparse
import asyncio
import base64
from contextlib import contextmanager
import fcntl
import hashlib
import json
import os
from pathlib import Path
import re
import shlex
import shutil
import stat
import struct
import subprocess
import sys
import tempfile

DEFAULT_STATE = Path.home() / '.local/share/genesis/codex-voice'
VOICE = Path('codex-resources/voice')
SUPPORTED = {'0.163.0'}  # Private helper protocol; expand only after real device verification.
HELPER_ENV_KEYS = set(('SYSTEMROOT WINDIR HOME USERPROFILE LOCALAPPDATA APPDATA TEMP TMP TMPDIR '
                       'XDG_RUNTIME_DIR PULSE_SERVER PULSE_COOKIE PIPEWIRE_REMOTE DBUS_SESSION_BUS_ADDRESS '
                       'HTTP_PROXY HTTPS_PROXY ALL_PROXY NO_PROXY SSL_CERT_FILE SSL_CERT_DIR '
                       'REQUESTS_CA_BUNDLE CURL_CA_BUNDLE').split())


def helper_environment():
    environment = {k: v for k, v in os.environ.items() if k.upper() in HELPER_ENV_KEYS}
    environment.update(GST_PLUGIN_PATH='', GST_PLUGIN_PATH_1_0='', GST_PLUGIN_SYSTEM_PATH='',
                       GST_PLUGIN_SYSTEM_PATH_1_0='', GST_REGISTRY='/dev/null',
                       GST_REGISTRY_UPDATE='no', GST_REGISTRY_FORK='no')
    return environment


def digest(data):
    return hashlib.sha256(data).hexdigest()


def atomic_write(path, data, mode=0o600):
    fd, temporary = tempfile.mkstemp(dir=path.parent)
    try:
        with os.fdopen(fd, 'wb') as stream:
            os.fchmod(stream.fileno(), mode)
            stream.write(data)
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(temporary, path)
        directory = os.open(path.parent, os.O_DIRECTORY)
        try:
            os.fsync(directory)
        finally:
            os.close(directory)
    finally:
        if os.path.exists(temporary):
            os.unlink(temporary)


def package_at(directory=None, *, probe=False):
    if directory:
        candidates = [Path(directory).expanduser().resolve()]
    else:
        executable = Path(shutil.which('codex') or 'codex').resolve()
        candidates = [p for p in executable.parents if (p / VOICE / 'manifest.json').is_file()]
        if not candidates:
            npm = subprocess.run(['npm', 'root', '-g'], check=True, capture_output=True, text=True)
            candidates = list((Path(npm.stdout.strip()) / '@openai/codex/node_modules/@openai').glob('codex-linux-*/vendor/*'))
    matches = [p for p in candidates if (p / VOICE / 'manifest.json').is_file()]
    if len(matches) != 1:
        raise ValueError('Cannot uniquely discover the physical Codex package; pass --package')
    root = matches[0]
    manifest = json.loads((root / VOICE / 'manifest.json').read_text())
    if manifest['schemaVersion'] != 1 or (not probe and manifest['appVersion'] not in SUPPORTED):
        raise ValueError('Untested Codex helper protocol/version; supported: ' + ', '.join(sorted(SUPPORTED)))
    if not re.fullmatch(r'[a-f0-9]{40}', manifest['buildCommit']):
        raise ValueError('Invalid Codex build commit')
    return root, manifest


@contextmanager
def helper_lock(helper):
    with open(helper.with_name(helper.name + '.genesis.lock'), 'a') as stream:
        fcntl.flock(stream, fcntl.LOCK_EX)
        yield


def ssh_args(host, identity=None):
    if not host or host.startswith('-') or any(c.isspace() or ord(c) < 32 for c in host):
        raise ValueError('Use an SSH hostname/alias or user@host, without options or whitespace')
    args = [shutil.which('ssh') or '/usr/bin/ssh', '-T', '-o', 'BatchMode=yes', '-o',
            'StrictHostKeyChecking=yes', '-o', 'ConnectTimeout=5', '-o', 'ServerAliveInterval=15',
            '-o', 'ServerAliveCountMax=2']
    if identity:
        args += ['-i', str(Path(identity).expanduser().resolve())]
    return args + [host]


def remote_command(asset):
    if not re.fullmatch(r'[a-f0-9]{64}', asset):
        raise ValueError('Invalid Windows asset digest')
    # All shell text is fixed ASCII or hex. Windows resolves paths INSIDE Node,
    # so spaces, %, &, parentheses and Unicode never become shell syntax.
    return ('node.exe -e "require(require(\'node:path\').join(process.env.LOCALAPPDATA,'
            f"'Genesis','CodexVoice','{asset}','host.cjs'))\"")


def setup_windows(ssh, windows_package, *, provision=None):
    source = Path(__file__).with_name('host.cjs').read_bytes()
    options = base64.b64encode(json.dumps({'package': windows_package, **(provision or {})}).encode()).decode()
    script = Path(__file__).with_name('setup.ps1').read_text()
    script = script.replace('__OPTIONS__', options).replace('__SOURCE__', base64.b64encode(source).decode())
    loader = '& ([ScriptBlock]::Create([Console]::In.ReadToEnd()))'
    encoded = base64.b64encode(loader.encode('utf-16-le')).decode()
    result = subprocess.run(ssh + ['powershell.exe -NoProfile -NonInteractive -EncodedCommand ' + encoded],
                            input=script.encode('utf-8'), capture_output=True,
                            timeout=7200 if provision else 45, env=helper_environment())
    if result.returncode:
        if provision:
            try:
                failure = json.loads(result.stdout.decode('utf-8-sig'))
            except (ValueError, UnicodeDecodeError):
                failure = {}
            if result.returncode == 255 or not isinstance(failure, dict) or failure.get('held') is not True:
                raise OSError('Windows provisioning unavailable; retrying the matching build')
        raise ValueError('Windows setup failed in Codex helper environment (SSH_AUTH_SOCK is not inherited; '
                         'use a file identity or explicit SSH IdentityAgent): ' + result.stderr.decode(errors='replace'))
    return json.loads(result.stdout.decode('utf-8-sig'))


def launcher(state):
    return ('#!/bin/sh\n# genesis-codex-voice\nexec ' + shlex.quote(sys.executable) + ' ' +
            shlex.quote(str(state / 'bridge.py')) + ' run --state-dir ' + shlex.quote(str(state)) + '\n').encode()


def load_state(state):
    config = json.loads((state / 'config.json').read_text())
    if config['application'] != 'genesis-codex-voice':
        raise ValueError('State directory belongs to another application')
    return config


def install(args):
    state = args.state_dir.expanduser().resolve()
    managed = getattr(args, 'managed', False)
    root, manifest = package_at(args.package, probe=managed)
    helper = root / VOICE / 'bin/codex-voice-host'
    if helper.resolve() != helper:
        raise ValueError('The packaged helper must be a physical file')
    state.mkdir(parents=True, exist_ok=True, mode=0o700)
    with helper_lock(helper), helper_lock(state / 'config.json'):
        previous = load_state(state) if (state / 'config.json').exists() else None
        if previous and previous['package'] != str(root):
            raise ValueError('State directory already manages another package; use another --state-dir')
        if not previous and (state / 'bridge.py').exists() and (state / 'bridge.py').read_bytes() != Path(__file__).read_bytes():
            raise ValueError('State directory contains an unowned bridge.py; use another --state-dir')
        current = helper.read_bytes()
        expected = manifest['sha256'][str(VOICE / 'bin/codex-voice-host')]
        ours = previous and current == previous['launcher'].encode()
        if ours and previous['buildCommit'] != manifest['buildCommit']:
            raise ValueError('Manifest changed while the old relay remains; restore the original package first')
        if digest(current) != expected and not ours:
            raise ValueError('Helper has a foreign modification or another state directory owns it')
        ssh = ssh_args(args.host, args.identity)
        remote = (setup_windows(ssh, args.windows_package, provision=args.provision) if managed
                  else setup_windows(ssh, args.windows_package))
        if (remote['buildCommit'], remote['appVersion']) != (manifest['buildCommit'], manifest['appVersion']):
            raise ValueError('Linux and Windows Codex builds differ; update both before installing')
        command = remote_command(remote['asset'])
        backup = helper.with_name(helper.name + '.original-' + manifest['buildCommit'])
        if backup.exists():
            if backup.is_symlink() or digest(backup.read_bytes()) != expected:
                raise ValueError('Original helper backup does not match the package manifest')
        elif ours:
            raise ValueError('Original helper backup is missing; refusing to adopt the relay as backup')
        else:
            atomic_write(backup, current, stat.S_IMODE(helper.stat().st_mode))
        script = launcher(getattr(args, 'runtime_state', state))
        config = {'application': 'genesis-codex-voice', 'package': str(getattr(args, 'runtime_package', root)), 'ssh': ssh,
                  'command': command, 'buildCommit': manifest['buildCommit'], 'originalHash': expected,
                  'originalMode': stat.S_IMODE(backup.stat().st_mode), 'launcher': script.decode()}
        if managed:
            config['managedBuild'] = manifest['buildCommit']
        atomic_write(state / 'config.json', json.dumps(config).encode())
        atomic_write(state / 'bridge.py', Path(__file__).read_bytes())
        atomic_write(helper, script, config['originalMode'])
    print('Installed. Use your normal SSH Codex window and F8. Run doctor --audio to verify devices.')


def uninstall(state):
    config = load_state(state)
    helper = Path(config['package']) / VOICE / 'bin/codex-voice-host'
    with helper_lock(helper), helper_lock(state / 'config.json'):
        if load_state(state) != config:
            raise ValueError('Installation changed while waiting for its lock; retry uninstall')
        current = helper.read_bytes()
        if current == config['launcher'].encode():
            backup = helper.with_name(helper.name + '.original-' + config['buildCommit'])
            if backup.is_symlink() or digest(backup.read_bytes()) != config['originalHash']:
                raise ValueError('Cannot restore: original helper backup is missing or damaged')
            atomic_write(helper, backup.read_bytes(), config['originalMode'])
        elif digest(current) != config['originalHash']:
            manifest = json.loads((Path(config['package']) / VOICE / 'manifest.json').read_text())
            if digest(current) != manifest['sha256'][str(VOICE / 'bin/codex-voice-host')]:
                raise ValueError('Helper changed after installation; preserving it and the recovery state')
        for name in ('config.json', 'bridge.py'):
            (state / name).unlink(missing_ok=True)
    print('Linux helper restored. Verified backups and shared, inert Windows assets retained.')


def run(state):
    config = load_state(state)
    _, manifest = package_at(config['package'], probe=config.get('managedBuild') == config['buildCommit'])
    if manifest['buildCommit'] != config['buildCommit']:
        raise ValueError('Codex changed builds; reinstall the bridge after updating both hosts')
    os.execv(config['ssh'][0], config['ssh'] + [config['command']])


async def exchange(process, message, timeout=5):
    payload = json.dumps(message).encode()
    async with asyncio.timeout(timeout):
        process.stdin.write(struct.pack('>I', len(payload)) + payload)
        await process.stdin.drain()
        header = await process.stdout.readexactly(4)
        size = struct.unpack('>I', header)[0]
        if size > 128 * 1024:
            raise ValueError('Helper frame exceeds protocol limit')
        return json.loads(await process.stdout.readexactly(size))


async def audio_check(process, config, socket):
    try:
        from websockets.asyncio.client import unix_connect
    except ImportError as error:
        raise ValueError('doctor --audio requires websockets>=13; install it in your Python environment') from error
    async with unix_connect(str(socket), max_size=32 * 1024 * 1024) as ws:
        events = []
        next_id = 0

        async def call(method, params):
            nonlocal next_id
            next_id += 1
            async with asyncio.timeout(45):
                await ws.send(json.dumps({'id': next_id, 'method': method, 'params': params}))
                while True:
                    message = json.loads(await ws.recv())
                    if message.get('id') == next_id:
                        if 'error' in message:
                            raise ValueError(method + ' failed')
                        return message['result']
                    events.append(message)

        await call('initialize', {'clientInfo': {'name': 'codex-voice-doctor', 'version': '1'},
                                 'capabilities': {'experimentalApi': True}})
        await ws.send(json.dumps({'method': 'initialized'}))
        response = await call('thread/start', {'ephemeral': True, 'cwd': str(Path.home())})
        thread_id = response['thread']['id']
        try:
            offer = await exchange(process, {'type': 'startTransport'}, 30)
            await call('thread/realtime/start', {'threadId': thread_id, 'clientManagedHandoffs': True,
                       'outputModality': 'audio', 'includeStartupContext': False, 'version': 'v3',
                       'transport': {'type': 'webrtc', 'sdp': offer['sdp']}})
            async with asyncio.timeout(45):
                while True:
                    message = events.pop(0) if events else json.loads(await ws.recv())
                    if message.get('params', {}).get('threadId') != thread_id:
                        continue
                    if message.get('method') == 'thread/realtime/error':
                        raise ValueError('Realtime negotiation failed')
                    if message.get('method') == 'thread/realtime/sdp':
                        answer = message['params']['sdp']
                        break
            ready = await exchange(process, {'type': 'applyAnswer', 'sdp': answer}, 30)
            if ready['type'] != 'transportReady':
                raise ValueError('WebRTC transport did not connect')
            opened = await exchange(process, {'type': 'openDevices', 'selection': {
                'microphone': None, 'speaker': None, 'channel': None}})
            if opened['type'] != 'devicesOpened':
                raise ValueError('Windows devices did not open')
            await exchange(process, {'type': 'setAudioControls', 'controls': {
                'microphoneMuted': True, 'speakerSuppressed': True}})
            for _ in range(20):
                await asyncio.sleep(0.5)
                if (await exchange(process, {'type': 'inspectAudio'}))['type'] != 'audioState':
                    raise ValueError('Windows audio devices stopped')
            print('PASS: WebRTC and both Windows devices stable for 10 seconds (muted/suppressed).')
        finally:
            await call('thread/realtime/stop', {'threadId': thread_id})


async def doctor(args):
    state = args.state_dir.expanduser().resolve()
    config = load_state(state)
    helper = Path(config['package']) / VOICE / 'bin/codex-voice-host'
    if helper.read_bytes() != config['launcher'].encode():
        raise ValueError('Relay is no longer installed; rerun install after checking Codex versions')
    process = await asyncio.create_subprocess_exec(str(helper), stdin=asyncio.subprocess.PIPE,
                                                 stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.DEVNULL,
                                                 env=helper_environment())
    await inspect_process(process, config, args)


async def inspect_process(process, config, args):
    """Shared native protocol qualification; device opening remains opt-in."""
    try:
        for message, expected, deadline in [({'type': 'hello', 'protocol': 1, 'buildCommit': config['buildCommit']}, 'ready', 5),
                                            ({'type': 'initializeRuntime'}, 'runtimeReady', 30)]:
            if (await exchange(process, message, deadline))['type'] != expected:
                raise ValueError('Helper handshake/runtime initialization failed')
        for kind in ('input', 'output'):
            result = await exchange(process, {'type': 'listDevices', 'kind': kind})
            if not any(device['isDefault'] for device in result['devices']):
                raise ValueError('No default Windows ' + kind + ' device')
        print('PASS: matching helper, private runtime, default input/output enumeration.')
        if args.audio:
            await audio_check(process, config, args.socket)
        else:
            print('Audio connection/device opening NOT tested. Use doctor --audio for that check.')
        if (await exchange(process, {'type': 'close'}))['type'] != 'closed':
            raise ValueError('Helper did not close cleanly')
        await asyncio.wait_for(process.wait(), 5)
        if process.returncode:
            raise ValueError('Helper exited unsuccessfully')
    except EOFError:
        # EOF can precede delivery of SSH's process-exit notification. Let that
        # finish before cleanup so the updater can distinguish transport exit
        # 255 from a native/protocol failure. Reuse the normal close deadline.
        try:
            await asyncio.wait_for(process.wait(), 5)
        except asyncio.TimeoutError:
            pass
        raise
    finally:
        if process.returncode is None:
            process.kill()
            await process.wait()


def parse_args():
    parser = argparse.ArgumentParser(description=__doc__)
    actions = parser.add_subparsers(dest='action', required=True)
    for name in ('install', 'run', 'uninstall', 'doctor', 'manage', 'launch', 'sync', 'status', 'unmanage'):
        command = actions.add_parser(name)
        command.add_argument('--state-dir', type=Path, default=DEFAULT_STATE)
        if name == 'install':
            command.add_argument('--host', required=True, help='Existing SSH alias or user@Windows-host')
            command.add_argument('--identity', help='Optional existing SSH private key path')
            command.add_argument('--package', help='Physical Linux vendor package root (auto-discovered by default)')
            command.add_argument('--windows-package', help='Windows vendor root or npm @openai/codex directory')
        if name == 'doctor':
            command.add_argument('--audio', action='store_true', help='Negotiate live WebRTC and open devices muted')
            codex_home = Path(os.environ.get('CODEX_HOME') or Path.home() / '.codex')
            command.add_argument('--socket', type=Path, default=codex_home / 'app-server-control/app-server-control.sock')
        if name in ('manage', 'launch'):
            command.add_argument('--codex', required=True, help='Real npm Codex entry point, not the voice launcher')
        if name == 'manage':
            command.add_argument('--host', required=True)
            command.add_argument('--identity')
            command.add_argument('--launcher', type=Path, required=True)
            command.add_argument('--replace-launcher-sha256', help='Explicit ownership check when replacing an existing launcher')
        if name == 'launch':
            command.add_argument('arguments', nargs=argparse.REMAINDER)
    return parser.parse_args()


def main():
    args = parse_args()
    try:
        if args.action in ('manage', 'launch', 'sync', 'status', 'unmanage'):
            if __package__:
                from . import managed
            else:
                import managed
            return getattr(managed, args.action)(args)
        elif args.action == 'install':
            install(args)
        elif args.action == 'uninstall':
            uninstall(args.state_dir.expanduser().resolve())
        elif args.action == 'run':
            run(args.state_dir.expanduser().resolve())
        else:
            asyncio.run(doctor(args))
    except (OSError, ValueError, KeyError, subprocess.SubprocessError, EOFError, asyncio.IncompleteReadError) as error:
        print('Codex voice bridge: ' + str(error), file=sys.stderr)
        return 1
    return 0


if __name__ == '__main__':
    sys.exit(main())
