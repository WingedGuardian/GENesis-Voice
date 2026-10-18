import argparse
import asyncio
import contextlib
import io
import json
from pathlib import Path
import shutil
import stat
import struct
import subprocess
import tempfile
import unittest
from unittest.mock import AsyncMock, Mock, patch

from bridges.codex_voice import bridge


class Installation(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.root = Path(self.temporary.name) / "package with ' & (%NAME%) Ω"
        self.state = Path(self.temporary.name) / "state with ' & (%NAME%) Ω"
        self.helper = self.root / bridge.VOICE / 'bin/codex-voice-host'
        self.helper.parent.mkdir(parents=True)
        self.original = b'original helper fixture'
        self.helper.write_bytes(self.original)
        self.helper.chmod(0o751)
        self.manifest = {'schemaVersion': 1, 'appVersion': '0.163.0', 'buildCommit': 'a' * 40,
                         'sha256': {str(bridge.VOICE / 'bin/codex-voice-host'): bridge.digest(self.original)}}
        self.save_manifest()
        self.remote = patch.object(bridge, 'setup_windows', return_value={**self.manifest, 'asset': 'b' * 64}).start()
        self.addCleanup(patch.stopall)
        self.args = argparse.Namespace(package=str(self.root), state_dir=self.state, host='windows-audio',
                                       identity=None, windows_package=None)
        self.output = contextlib.redirect_stdout(io.StringIO())
        self.output.__enter__()
        self.addCleanup(self.output.__exit__, None, None, None)

    def save_manifest(self):
        (self.root / bridge.VOICE / 'manifest.json').write_text(json.dumps(self.manifest))

    def test_roundtrip_idempotence_and_original_mode(self):
        bridge.install(self.args)
        installed = self.helper.read_bytes()
        bridge.install(self.args)
        self.assertEqual(self.helper.read_bytes(), installed)
        self.assertEqual(stat.S_IMODE((self.state / 'config.json').stat().st_mode), 0o600)
        bridge.uninstall(self.state)
        self.assertEqual(self.helper.read_bytes(), self.original)
        self.assertEqual(stat.S_IMODE(self.helper.stat().st_mode), 0o751)
        self.assertFalse((self.state / 'config.json').exists())

    def test_foreign_helper_and_second_state_are_preserved(self):
        self.helper.write_bytes(b'foreign executable')
        with self.assertRaisesRegex(ValueError, 'foreign modification'):
            bridge.install(self.args)
        self.remote.assert_not_called()
        self.assertEqual(self.helper.read_bytes(), b'foreign executable')
        self.helper.write_bytes(self.original)
        bridge.install(self.args)
        installed = self.helper.read_bytes()
        self.args.state_dir = self.state.with_name('other state')
        with self.assertRaisesRegex(ValueError, 'another state directory'):
            bridge.install(self.args)
        self.assertEqual(self.helper.read_bytes(), installed)

    def test_mismatched_and_untested_versions_do_not_patch(self):
        self.remote.return_value = {**self.manifest, 'buildCommit': 'c' * 40}
        with self.assertRaisesRegex(ValueError, 'builds differ'):
            bridge.install(self.args)
        self.assertEqual(self.helper.read_bytes(), self.original)
        self.assertFalse((self.state / 'config.json').exists())
        self.manifest['appVersion'] = '0.999.0'
        self.save_manifest()
        with self.assertRaisesRegex(ValueError, 'Untested'):
            bridge.install(self.args)

    def test_missing_or_corrupt_backup_never_restores(self):
        bridge.install(self.args)
        installed = self.helper.read_bytes()
        backup = self.helper.with_name(self.helper.name + '.original-' + self.manifest['buildCommit'])
        for damaged in (b'corrupt', None):
            if damaged is None:
                backup.unlink()
            else:
                backup.write_bytes(damaged)
            with self.assertRaises((ValueError, FileNotFoundError)):
                bridge.uninstall(self.state)
            self.assertEqual(self.helper.read_bytes(), installed)
            self.assertTrue((self.state / 'config.json').exists())

    def test_interruption_before_and_after_helper_replacement_recovers(self):
        write = bridge.atomic_write
        for failure_path, after in ((self.state / 'bridge.py', False), (self.helper, True)):
            def interrupted(path, data, mode=0o600):
                if path == failure_path and not after:
                    raise OSError('simulated interruption')
                write(path, data, mode)
                if path == failure_path and after:
                    raise OSError('simulated interruption')
            with patch.object(bridge, 'atomic_write', side_effect=interrupted):
                with self.assertRaisesRegex(OSError, 'interruption'):
                    bridge.install(self.args)
            bridge.install(self.args)
            bridge.uninstall(self.state)
            self.assertEqual(self.helper.read_bytes(), self.original)

    def test_uninstall_preserves_new_npm_build_and_foreign_edits(self):
        bridge.install(self.args)
        self.helper.write_bytes(b'foreign changed helper')
        with self.assertRaisesRegex(ValueError, 'changed after'):
            bridge.uninstall(self.state)
        self.assertTrue((self.state / 'config.json').exists())
        upgraded = b'new npm helper'
        self.helper.write_bytes(upgraded)
        self.manifest['buildCommit'] = 'c' * 40
        self.manifest['sha256'][str(bridge.VOICE / 'bin/codex-voice-host')] = bridge.digest(upgraded)
        self.save_manifest()
        bridge.uninstall(self.state)
        self.assertEqual(self.helper.read_bytes(), upgraded)

    def test_discovery_prefers_native_executable_and_no_npm_required(self):
        with patch.object(shutil, 'which', return_value=str(self.root / 'bin/codex')):
            with patch.object(subprocess, 'run') as invoke:
                self.assertEqual(bridge.package_at()[0], self.root)
                invoke.assert_not_called()

    def test_runtime_launcher_handles_paths_as_data(self):
        bridge.install(self.args)
        with patch.object(bridge.os, 'execv') as execute:
            bridge.run(self.state)
        self.assertIn('windows-audio', execute.call_args.args[1])
        self.assertNotIn('%NAME%', execute.call_args.args[1][-1])
        self.assertIn('process.env.LOCALAPPDATA', execute.call_args.args[1][-1])
        for host in ('-oProxyCommand=evil', 'host name', 'host\ncommand'):
            with self.assertRaises(ValueError):
                bridge.ssh_args(host)
        with self.assertRaises(ValueError):
            bridge.remote_command('not-a-content-digest')


class Frames(unittest.IsolatedAsyncioTestCase):
    async def test_request_deadline_includes_stalled_stdin(self):
        class Input:
            def write(self, data):
                pass

            async def drain(self):
                await asyncio.Event().wait()
        process = argparse.Namespace(stdin=Input(), stdout=asyncio.StreamReader())
        request = asyncio.create_task(bridge.exchange(process, {'type': 'hello'}, timeout=0.01))
        try:
            done, _ = await asyncio.wait({request}, timeout=0.5)
            self.assertIn(request, done, 'The request needed an external timeout to cancel drain')
            with self.assertRaises(TimeoutError):
                await request
        finally:
            request.cancel()

    async def test_oversize_reply_is_refused_before_reading_payload(self):
        class Input:
            def write(self, data):
                pass

            async def drain(self):
                pass
        output = asyncio.StreamReader()
        output.feed_data(struct.pack('>I', 128 * 1024 + 1))
        process = argparse.Namespace(stdin=Input(), stdout=output)
        with self.assertRaisesRegex(ValueError, 'exceeds'):
            await bridge.exchange(process, {'type': 'hello'})


class SocketSelection(unittest.TestCase):
    def test_doctor_honors_codex_home_and_explicit_socket(self):
        cases = [({}, Path('/example/home/.codex')),
                 ({'CODEX_HOME': ''}, Path('/example/home/.codex')),
                 ({'CODEX_HOME': '/example/custom codex home'}, Path('/example/custom codex home')),
                 ({'CODEX_HOME': 'relative-codex-home'}, Path('relative-codex-home'))]
        suffix = Path('app-server-control/app-server-control.sock')
        for environment, home in cases:
            with self.subTest(environment=environment):
                with patch.dict(bridge.os.environ, {'HOME': '/example/home', **environment}, clear=True):
                    with patch.object(bridge.sys, 'argv', ['bridge.py', 'doctor', '--audio']):
                        with patch.object(bridge, 'doctor', new_callable=AsyncMock) as check:
                            self.assertEqual(bridge.main(), 0)
                        self.assertEqual(check.call_args.args[0].socket, home / suffix)
        with patch.dict(bridge.os.environ, {'CODEX_HOME': '/example/custom-home'}):
            with patch.object(bridge.sys, 'argv', ['bridge.py', 'doctor', '--audio', '--socket', '/example/explicit.sock']):
                with patch.object(bridge, 'doctor', new_callable=AsyncMock) as check:
                    self.assertEqual(bridge.main(), 0)
                self.assertEqual(check.call_args.args[0].socket, Path('/example/explicit.sock'))


class Authentication(unittest.IsolatedAsyncioTestCase):
    def test_setup_uses_the_actual_scrubbed_helper_environment(self):
        with patch.dict(bridge.os.environ, {'HOME': '/example/home', 'SSH_AUTH_SOCK': '/example/agent',
                                           'PATH': '/example/bin', 'HTTP_PROXY': 'http://proxy.example'}):
            environment = bridge.helper_environment()
            self.assertNotIn('SSH_AUTH_SOCK', environment)
            self.assertNotIn('PATH', environment)
            self.assertEqual(environment['HTTP_PROXY'], 'http://proxy.example')
            self.assertEqual(environment['GST_REGISTRY'], '/dev/null')
            result = argparse.Namespace(returncode=1, stderr=b'authentication unavailable')
            with patch.object(subprocess, 'run', return_value=result) as invoke:
                with self.assertRaisesRegex(ValueError, 'SSH_AUTH_SOCK is not inherited'):
                    bridge.setup_windows(['/usr/bin/ssh', 'windows-audio'], None)
                self.assertEqual(invoke.call_args.kwargs['env'], environment)

    async def test_doctor_uses_scrubbed_environment_and_reaps_failed_helper(self):
        with tempfile.TemporaryDirectory() as directory:
            state = Path(directory)
            helper = state / 'package' / bridge.VOICE / 'bin/codex-voice-host'
            helper.parent.mkdir(parents=True)
            helper.write_text('owned test fixture')
            config = {'application': 'genesis-codex-voice', 'package': str(state / 'package'),
                      'launcher': 'owned test fixture', 'buildCommit': 'a' * 40}
            (state / 'config.json').write_text(json.dumps(config))
            process = argparse.Namespace(returncode=None, kill=Mock(), wait=AsyncMock())
            with patch.object(asyncio, 'create_subprocess_exec', new_callable=AsyncMock, return_value=process) as spawn:
                with patch.object(bridge, 'exchange', new_callable=AsyncMock, side_effect=ValueError('test failure')):
                    with self.assertRaisesRegex(ValueError, 'test failure'):
                        await bridge.doctor(argparse.Namespace(state_dir=state, audio=False))
            self.assertNotIn('SSH_AUTH_SOCK', spawn.call_args.kwargs['env'])
            self.assertNotIn('PATH', spawn.call_args.kwargs['env'])
            process.kill.assert_called_once()
            process.wait.assert_awaited_once()


@unittest.skipUnless(shutil.which('node'), 'Node required for native host subprocess checks')
class WindowsHost(unittest.TestCase):
    def test_raw_binary_environment_exit_and_metadata(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory) / 'native path & (%NAME%) Ω'
            voice = root / bridge.VOICE
            (voice / 'bin').mkdir(parents=True)
            commit = 'a' * 40
            (voice / 'manifest.json').write_text(json.dumps({'appVersion': '0.163.0', 'buildCommit': commit}))
            helper = voice / 'bin/codex-voice-host.exe'
            helper.write_text('#!/usr/bin/env python3\nimport os,sys\n'
                              f'if "--build-commit" in sys.argv: print("{commit}");sys.exit(0)\n'
                              'assert os.environ["GST_REGISTRY"]=="NUL"\n'
                              'assert os.environ["GST_PLUGIN_PATH"]==""\n'
                              'assert os.environ["GST_REGISTRY_FORK"]=="no"\n'
                              'assert "UNRELATED_TEST_SECRET" not in os.environ\n'
                              'sys.stdout.buffer.write(sys.stdin.buffer.read());sys.exit(7)\n')
            helper.chmod(0o755)
            # The child environment intentionally has no PATH; use a physical interpreter.
            helper.write_text(helper.read_text().replace('/usr/bin/env python3', bridge.sys.executable))
            source = Path(bridge.__file__).with_name('host.cjs').read_text().replace('__PACKAGE_ROOT__', json.dumps(str(root)))
            program = Path(directory) / 'host.cjs'
            program.write_text(source)
            description = subprocess.run(['node', str(program), '--describe'], capture_output=True, check=True)
            self.assertEqual(json.loads(description.stdout)['buildCommit'], commit)
            data = bytes(range(256)) * 8
            result = subprocess.run(['node', str(program)], input=data, capture_output=True,
                                    env={**bridge.os.environ, 'UNRELATED_TEST_SECRET': 'must-not-reach-helper'}, timeout=10)
            self.assertEqual(result.returncode, 7, result.stderr)
            self.assertEqual(result.stdout, data)
            (voice / 'manifest.json').write_text(json.dumps({'appVersion': '0.163.0', 'buildCommit': 'b' * 40}))
            refused = subprocess.run(['node', str(program), '--describe'], capture_output=True)
            self.assertNotEqual(refused.returncode, 0)
            self.assertEqual(refused.stdout, b'')


if __name__ == '__main__':
    unittest.main()
