"""Managed updates must never delay text launch or mutate the global package."""

import argparse
import asyncio
import importlib
import io
import json
import os
from pathlib import Path
import shutil
import tempfile
import unittest
from unittest.mock import AsyncMock, Mock, patch


class ManagedUpdates(unittest.TestCase):
    def setUp(self):
        self.managed = importlib.import_module('bridges.codex_voice.managed')
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.state = Path(self.tmp.name) / 'state'
        self.state.mkdir()
        self.codex = Path(self.tmp.name) / 'global/node_modules/@openai/codex/bin/codex.js'
        self.codex.parent.mkdir(parents=True)
        self.codex.write_text('global original')
        self.vendor = self.codex.parents[1] / 'node_modules/@openai/codex-linux-x64/vendor/linux'
        voice = self.vendor / 'codex-resources/voice'
        (voice / 'bin').mkdir(parents=True)
        (voice / 'bin/codex-voice-host').write_bytes(b'original')
        self.manifest = {'schemaVersion': 1, 'appVersion': '0.164.0', 'buildCommit': 'a' * 40,
                         'sha256': {'codex-resources/voice/bin/codex-voice-host':
                                    self.managed.bridge.digest(b'original')}}
        (voice / 'manifest.json').write_text(json.dumps(self.manifest))
        (self.codex.parents[1] / 'package.json').write_text(json.dumps({
            'version': '0.164.0', 'optionalDependencies': {
                '@openai/codex-win32-x64': 'npm:@openai/codex@0.164.0-win32-x64'}}))
        self.config = {'application': 'genesis-codex-voice', 'managed': {
            'codex': str(self.codex), 'package': str(self.vendor), 'host': 'windows-audio',
            'identity': None, 'unit': 'genesis-codex-voice-sync.service'}}
        self.save()

    def save(self):
        (self.state / 'config.json').write_text(json.dumps(self.config))

    def args(self):
        return argparse.Namespace(state_dir=self.state, codex=str(self.codex), arguments=['--model', 'example'])

    def test_pending_and_offline_start_text_without_waiting(self):
        with patch.object(self.managed, 'request_sync', side_effect=OSError('manager unavailable')):
            with patch.object(self.managed.os, 'execv') as execute:
                self.managed.launch(self.args())
        self.assertEqual(execute.call_args.args, (str(self.codex), [str(self.codex), '--model', 'example']))
        self.assertEqual(self.codex.read_text(), 'global original')

    def test_broken_config_still_launches_explicit_global_entry(self):
        (self.state / 'config.json').write_text('invalid')
        with patch.object(self.managed.os, 'execv') as execute:
            self.managed.launch(self.args())
        self.assertEqual(execute.call_args.args[0], str(self.codex))

    def test_private_entry_selected_only_when_manifest_and_launcher_verify(self):
        private = self.state / 'builds' / self.manifest['buildCommit']
        private.mkdir(parents=True)
        entry = private / 'codex.js'
        entry.write_text('private exact version')
        with patch.object(self.managed, 'prepared_entry', return_value=entry):
            with patch.object(self.managed, 'request_sync') as queue:
                with patch.object(self.managed.os, 'execv') as execute:
                    self.managed.launch(self.args())
        self.assertEqual(execute.call_args.args[0], str(entry))
        queue.assert_not_called()

    def test_package_alias_is_exact_not_guessed(self):
        self.assertEqual(self.managed.windows_spec(self.codex, self.manifest),
                         '@openai/codex@0.164.0-win32-x64')
        self.manifest['appVersion'] = '0.165.0'
        with self.assertRaises(ValueError):
            self.managed.windows_spec(self.codex, self.manifest)

    def test_hash_failure_is_held_before_any_remote_setup(self):
        helper = self.vendor / 'codex-resources/voice/bin/codex-voice-host'
        helper.write_bytes(b'foreign modification')
        remote = Mock()
        with patch.object(self.managed.bridge, 'setup_windows', remote):
            with self.assertRaises(ValueError):
                self.managed.verify_voice(self.vendor, self.manifest)
        remote.assert_not_called()
        self.assertEqual(helper.read_bytes(), b'foreign modification')

    def test_manifest_verifies_native_codex_as_well_as_voice_resources(self):
        executable = self.vendor / 'bin/codex'
        executable.parent.mkdir()
        executable.write_bytes(b'native codex')
        self.manifest['sha256']['bin/codex'] = self.managed.bridge.digest(b'native codex')
        self.managed.verify_voice(self.vendor, self.manifest)
        executable.write_bytes(b'changed')
        with self.assertRaises(ValueError):
            self.managed.verify_voice(self.vendor, self.manifest)

    def test_published_build_qualification_cannot_be_inferred_from_status(self):
        self.config['managed']['ready'] = self.manifest['buildCommit']
        self.config['managed']['status'] = 'ready'
        self.save()
        self.assertIsNone(self.managed.prepared_entry(self.state, self.config, self.manifest))

    def test_launch_preserves_environment_and_cwd(self):
        original = Path.cwd()
        with patch.dict(os.environ, {'CODEX_HOME': 'relative custom home', 'UNRELATED': 'keep'}):
            before = dict(os.environ)
            with patch.object(self.managed, 'request_sync'):
                with patch.object(self.managed.os, 'execv'):
                    self.managed.launch(self.args())
            self.assertEqual(dict(os.environ), before)
        self.assertEqual(Path.cwd(), original)

    def private_build(self):
        base = self.state / 'builds' / self.manifest['buildCommit']
        root = base / 'node_modules/@openai/codex'
        shutil.copytree(self.codex.parents[1], root)
        package = root / self.vendor.relative_to(self.codex.parents[1])
        relay = base / 'relay'
        relay.mkdir()
        script = self.managed.bridge.launcher(relay)
        helper = package / self.managed.bridge.VOICE / 'bin/codex-voice-host'
        helper.with_name(helper.name + '.original-' + self.manifest['buildCommit']).write_bytes(b'original')
        helper.write_bytes(script)
        installed = {'application': 'genesis-codex-voice', 'package': str(package),
                     'managedBuild': self.manifest['buildCommit'], 'buildCommit': self.manifest['buildCommit'],
                     'launcher': script.decode(), 'originalHash': self.managed.bridge.digest(b'original'),
                     'ssh': ['/usr/bin/ssh', 'windows-audio'], 'command': 'fixed test command'}
        (relay / 'config.json').write_text(json.dumps(installed))
        (relay / 'bridge.py').write_bytes(Path(self.managed.bridge.__file__).read_bytes())
        return base, package

    def test_crash_after_private_publication_reuses_it_without_installing_again(self):
        self.private_build()
        with patch.object(self.managed, 'qualify', new_callable=AsyncMock):
            with patch.object(self.managed.subprocess, 'run', return_value=argparse.Namespace(stdout='x64')) as run:
                self.assertEqual(self.managed.sync(self.args()), 0)
        current = json.loads((self.state / 'config.json').read_text())
        self.assertEqual(current['managed'].get('ready'), self.manifest['buildCommit'])
        self.assertFalse(any(c.args[0][0] == 'npm' for c in run.call_args_list))

    def test_ready_build_requires_a_verified_original_helper_backup(self):
        _, package = self.private_build()
        self.config['managed']['ready'] = self.manifest['buildCommit']
        self.assertIsNotNone(self.managed.prepared_entry(self.state, self.config, self.manifest))
        backup = package / self.managed.bridge.VOICE / ('bin/codex-voice-host.original-' + self.manifest['buildCommit'])
        backup.write_bytes(b'changed')
        self.assertIsNone(self.managed.prepared_entry(self.state, self.config, self.manifest))

    def test_old_ready_status_does_not_certify_a_new_global_build(self):
        self.config['managed'].update(ready='b' * 40, status='ready')
        self.save()
        output = io.StringIO()
        with patch('sys.stdout', output):
            self.assertEqual(self.managed.status(self.args()), 1)
        self.assertEqual(json.loads(output.getvalue())['status'], 'pending')

    def test_manage_can_resume_after_updater_unit_was_published(self):
        args = argparse.Namespace(state_dir=self.state, codex=str(self.codex), host='windows-audio',
                                  identity=None, launcher=Path(self.tmp.name) / 'bin/codex',
                                  replace_launcher_sha256=None)
        # A separate state is required for initial provisioning.
        (self.state / 'config.json').unlink()
        real_save = self.managed.save
        with patch.object(self.managed.Path, 'home', return_value=Path(self.tmp.name)):
            with patch.object(self.managed, 'save', side_effect=OSError('interrupted before config')):
                with self.assertRaises(OSError):
                    self.managed.manage(args)
            with patch.object(self.managed, 'save', real_save):
                with patch.object(self.managed.subprocess, 'run', return_value=argparse.Namespace(returncode=0)):
                    self.assertEqual(self.managed.manage(args), 0)
        self.assertTrue(args.launcher.is_file())

    def sync_dependencies(self, callback=None):
        def command(argv, **kwargs):
            if argv[0] == 'npm':
                stage = Path(argv[argv.index('--prefix') + 1])
                shutil.copytree(self.codex.parents[1], stage / 'node_modules/@openai/codex')
                self.assertIn('--ignore-scripts', argv)
                self.assertIn('@openai/codex@0.164.0', argv)
                return argparse.Namespace(returncode=0)
            return argparse.Namespace(returncode=0, stdout='x64')
        self.enterContext(patch.object(self.managed.subprocess, 'run', side_effect=command))
        self.enterContext(patch.object(self.managed.bridge, 'setup_windows',
                                      return_value={**self.manifest, 'asset': 'b' * 64}))
        return self.enterContext(patch.object(self.managed, 'qualify', new_callable=AsyncMock, side_effect=callback))

    def test_manage_quotes_literal_systemd_paths_and_restores_owned_launcher(self):
        self.state = Path(self.tmp.name) / 'voice-${USER}-%literal'
        target = Path(self.tmp.name) / 'bin/codex'
        target.parent.mkdir()
        target.write_bytes(b'#!/bin/sh\nexit 7\n')
        target.chmod(0o711)
        args = argparse.Namespace(state_dir=self.state, codex=str(self.codex), host='windows-audio',
                                  identity=None, launcher=target,
                                  replace_launcher_sha256=self.managed.bridge.digest(target.read_bytes()))
        with patch.object(self.managed.Path, 'home', return_value=Path(self.tmp.name)):
            with patch.object(self.managed.subprocess, 'run', return_value=argparse.Namespace(returncode=0)):
                self.assertEqual(self.managed.manage(args), 0)
                config = json.loads((self.state / 'config.json').read_text())
                unit = Path(self.tmp.name) / '.config/systemd/user' / config['managed']['unit']
                self.assertIn('voice-$${USER}-%%literal', unit.read_text())
                retained = self.state / 'builds/retained'
                retained.mkdir(parents=True)
                self.assertEqual(self.managed.unmanage(args), 0)
        self.assertEqual(target.read_bytes(), b'#!/bin/sh\nexit 7\n')
        self.assertEqual(target.stat().st_mode & 0o777, 0o711)
        self.assertFalse(unit.exists())
        self.assertTrue(retained.is_dir())
        self.assertFalse(json.loads((self.state / 'config.json').read_text())['managed']['enabled'])

    def test_sync_prepares_private_install_and_never_patches_global_package(self):
        before = {p.relative_to(self.codex.parents[1]): p.read_bytes()
                  for p in self.codex.parents[1].rglob('*') if p.is_file()}
        probe = self.sync_dependencies()
        self.assertEqual(self.managed.sync(self.args()), 0)
        config = json.loads((self.state / 'config.json').read_text())
        prepared = self.managed.prepared_entry(self.state, config, self.manifest)
        self.assertIsNotNone(prepared)
        self.assertTrue(prepared.is_relative_to(self.state / 'builds'))
        self.assertEqual(before, {p.relative_to(self.codex.parents[1]): p.read_bytes()
                                  for p in self.codex.parents[1].rglob('*') if p.is_file()})
        probe.assert_awaited_once()

    def test_global_upgrade_during_qualification_is_pending_and_preserves_new_package(self):
        def upgrade(config):
            manifest = {**self.manifest, 'buildCommit': 'c' * 40}
            (self.vendor / self.managed.bridge.VOICE / 'manifest.json').write_text(json.dumps(manifest))
        self.sync_dependencies(upgrade)
        self.assertEqual(self.managed.sync(self.args()), 1)
        config = json.loads((self.state / 'config.json').read_text())
        self.assertEqual(config['managed']['status'], 'pending')
        self.assertNotIn('ready', config['managed'])
        self.assertEqual((self.vendor / self.managed.bridge.VOICE / 'bin/codex-voice-host').read_bytes(), b'original')

    def test_config_publication_failure_recovers_the_prepared_build(self):
        self.sync_dependencies()
        original = self.managed.save
        failed = False
        def interrupt(state, config):
            nonlocal failed
            if config['managed'].get('status') == 'ready' and not failed:
                failed = True
                raise OSError('interrupted after immutable build publication')
            return original(state, config)
        with patch.object(self.managed, 'save', side_effect=interrupt):
            self.assertEqual(self.managed.sync(self.args()), 1)
        self.assertEqual(self.managed.sync(self.args()), 0)
        config = json.loads((self.state / 'config.json').read_text())
        self.assertIsNotNone(self.managed.prepared_entry(self.state, config, self.manifest))

    def test_corrupt_private_build_is_held_without_repeated_installation(self):
        base, _ = self.private_build()
        (base / 'relay/config.json').write_text('{}')
        with patch.object(self.managed.subprocess, 'run') as command:
            self.assertEqual(self.managed.sync(self.args()), 0)
            self.assertEqual(self.managed.sync(self.args()), 0)
        command.assert_not_called()
        self.assertEqual(json.loads((self.state / 'config.json').read_text())['managed']['status'], 'held')

    def test_windows_provisioning_classifies_network_and_validation_failures(self):
        cases = [(255, b'', OSError), (1, b'{"held":false,"error":"network"}', OSError),
                 (1, b'{"held":true,"error":"hash mismatch"}', ValueError),
                 (1, b'incomplete reply', OSError)]
        for code, output, expected in cases:
            with self.subTest(code=code, output=output):
                result = argparse.Namespace(returncode=code, stdout=output, stderr=b'fixture failure')
                with patch.object(self.managed.bridge.subprocess, 'run', return_value=result):
                    with self.assertRaises(expected):
                        self.managed.bridge.setup_windows(['/usr/bin/ssh', 'windows-audio'], None,
                                                          provision={'version': '0.164.0'})

    def test_disabled_launcher_removes_only_its_own_argument_separator(self):
        self.config['managed']['enabled'] = False
        self.save()
        args = self.args()
        args.arguments = ['--', '--help']
        with patch.object(self.managed.os, 'execv') as execute:
            self.managed.launch(args)
        self.assertEqual(execute.call_args.args[1], [str(self.codex), '--help'])

    def test_disabled_and_old_held_status_are_not_ready_for_current_release(self):
        self.private_build()
        self.config['managed'].update(ready=self.manifest['buildCommit'], enabled=False, status='disabled')
        self.save()
        output = io.StringIO()
        with patch('sys.stdout', output):
            self.assertEqual(self.managed.status(self.args()), 1)
        self.assertEqual(json.loads(output.getvalue())['status'], 'disabled')
        self.config['managed'].update(enabled=True, ready=None, attemptedBuild='b' * 40, status='held', error='old error')
        self.save()
        output = io.StringIO()
        with patch('sys.stdout', output):
            self.assertEqual(self.managed.status(self.args()), 1)
        self.assertEqual(json.loads(output.getvalue())['status'], 'pending')
        self.assertIsNone(json.loads(output.getvalue())['error'])

    def test_manager_code_revision_is_refused_before_any_asset_overwrite(self):
        (self.state / 'config.json').unlink()
        args = argparse.Namespace(state_dir=self.state, codex=str(self.codex), host='windows-audio',
                                  identity=None, launcher=Path(self.tmp.name) / 'bin/codex', replace_launcher_sha256=None)
        with patch.object(self.managed.Path, 'home', return_value=Path(self.tmp.name)):
            with patch.object(self.managed.subprocess, 'run', return_value=argparse.Namespace(returncode=0)):
                self.managed.manage(args)
            source = Path(self.tmp.name) / 'new-manager'
            source.mkdir()
            for name in ('bridge.py', 'managed.py', 'host.cjs', 'setup.ps1'):
                shutil.copy(Path(self.managed.bridge.__file__).parent / name, source / name)
            (source / 'bridge.py').write_bytes((source / 'bridge.py').read_bytes() + b'\n# new revision\n')
            before = {p.name: p.read_bytes() for p in self.state.iterdir() if p.is_file()}
            with patch.object(self.managed.bridge, '__file__', str(source / 'bridge.py')):
                with patch.object(self.managed.subprocess, 'run', return_value=argparse.Namespace(returncode=0)):
                    with self.assertRaisesRegex(ValueError, 'state'):
                        self.managed.manage(args)
            self.assertEqual(before, {p.name: p.read_bytes() for p in self.state.iterdir() if p.is_file()})

    def test_qualification_eof_classifies_ssh_and_native_failure(self):
        for code, expected in [(255, OSError), (1, ValueError)]:
            with self.subTest(code=code):
                process = argparse.Namespace(returncode=None, kill=Mock())
                async def exited():
                    process.returncode = code
                    return code
                process.wait = AsyncMock(side_effect=exited)
                with patch.object(self.managed.asyncio, 'create_subprocess_exec', new_callable=AsyncMock, return_value=process):
                    with patch.object(self.managed.bridge, 'exchange', new_callable=AsyncMock,
                                      side_effect=asyncio.IncompleteReadError(b'', 4)):
                        with self.assertRaises(expected):
                            asyncio.run(self.managed.qualify({'ssh': ['ssh'], 'command': 'fixture', 'buildCommit': 'a' * 40}))


if __name__ == '__main__':
    unittest.main()
