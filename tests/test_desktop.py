import json
import hashlib
from pathlib import Path
import tempfile
import unittest
from unittest.mock import Mock, patch

import accounts
import desktop_service as desktop
import desktop_runtime
import resources
import worker


class DesktopTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name)
        self.directory = self.root / '.desktops'
        self.directory.mkdir()
        self.state = self.directory / 'session.json'
        self.patch = patch.multiple(desktop, ROOT=self.root, DIRECTORY=self.directory, STATE=self.state)
        self.patch.start()

    def tearDown(self):
        self.patch.stop(); self.temp.cleanup()

    def test_public_state_never_exposes_credentials(self):
        self.state.write_text(json.dumps({'id': 'a' * 32, 'phase': 'ready',
            'port': 28001, 'password': 'PRIVATE', 'request': {'argv': ['private']}}))
        with patch.object(desktop, 'active', return_value=True):
            value = desktop.status()
        self.assertTrue(value['active'])
        self.assertNotIn('password', value)
        self.assertNotIn('port', value)
        self.assertNotIn('request', value)
        self.assertNotIn('PRIVATE', json.dumps(value))

    def test_wrong_session_never_connects_to_a_socket(self):
        self.state.write_text(json.dumps({'id': 'a' * 32, 'phase': 'ready', 'port': 28001}))
        with patch.object(desktop.socket, 'create_connection') as connect:
            with self.assertRaises(PermissionError):
                desktop.bridge('b' * 32)
            connect.assert_not_called()

    def test_closed_or_orphaned_session_has_no_ticket(self):
        self.state.write_text(json.dumps({'id': 'a' * 32, 'password': 'PRIVATE', 'phase': 'ready'}))
        with patch.object(desktop, 'active', return_value=False):
            with self.assertRaises(PermissionError):
                desktop.dispatch({'action': 'ticket', 'id': 'a' * 32})

    def test_maintenance_refuses_before_spawning(self):
        with patch.object(worker, 'status', return_value={'maintenance': True, 'admit': False}), \
                patch.object(desktop.subprocess, 'Popen') as spawn:
            with self.assertRaises(RuntimeError):
                desktop.start()
            spawn.assert_not_called()

    def test_program_exit_cannot_leave_a_healthy_empty_desktop(self):
        running = Mock(returncode=None); running.poll.return_value = None
        desktop.check_programs({'chatgpt': running})
        exited = Mock(returncode=1); exited.poll.return_value = 1
        with self.assertRaisesRegex(RuntimeError, r'chatgpt \(exit 1\)'):
            desktop.check_programs({'window manager': running, 'chatgpt': exited})

    def test_unknown_program_is_refused_before_accessing_the_worker(self):
        with patch.object(worker, 'status') as status:
            with self.assertRaises(ValueError):
                desktop.start('sh;id')
            status.assert_not_called()

    def test_reservation_refusal_does_not_start_another_worker(self):
        state = {'maintenance': False, 'admit': True, 'pool': {}}
        refused = {'allowed': False, 'reasons': ['ram']}
        with patch.object(worker, 'status', return_value=state), \
                patch.object(resources, 'ram_request', return_value=(worker.GIB, 'unprofiled')), \
                patch.object(resources, 'admission', return_value=refused), \
                patch.object(desktop.desktop_runtime, 'location', return_value=(self.root / 'runtime', {})), \
                patch.object(desktop.subprocess, 'Popen') as spawn:
            with self.assertRaises(resources.AdmissionError):
                desktop.start()
            spawn.assert_not_called()

    def test_profile_and_one_margin_are_reused(self):
        request = {'argv': ['python3', '/app/desktop_service.py', 'run'],
                   'project': 'atelier-desktop', 'ram_bytes': None}
        resources.remember_ram(self.root, request, worker.GIB, True)
        first, source = resources.ram_request(self.root, request)
        again, _ = resources.ram_request(self.root, dict(request))
        self.assertEqual(source, 'measured')
        self.assertEqual(first, again)
        self.assertEqual(first, 1344 * worker.MIB)

    def test_measured_claim_remains_automatic_and_first_claim_is_bounded(self):
        for source, learned, wanted, override in [('measured', 1344 * worker.MIB, 1344 * worker.MIB, None),
                                        ('unprofiled', worker.GIB, int(2.5 * worker.GIB), None),
                                        ('measured', 1344 * worker.MIB, 2 * worker.GIB, 2 * worker.GIB)]:
            with self.subTest(source=source), \
                    patch.object(desktop, 'active', side_effect=[False, True, True]), \
                    patch.object(worker, 'status', return_value={'maintenance': False, 'admit': True, 'pool': {}}), \
                    patch.object(resources, 'ram_request', return_value=(learned, source)), \
                    patch.object(resources, 'admission', return_value={'allowed': True}) as admission, \
                    patch.object(desktop.desktop_runtime, 'location', return_value=(self.root / 'runtime', {})), \
                    patch.object(desktop.subprocess, 'Popen'):
                value = desktop.start(ram_bytes=override)
                self.assertEqual(admission.call_args.args[2], wanted)
                self.assertEqual(value['ram_bytes'], wanted)
                saved = json.loads(self.state.read_text())['request']
                self.assertEqual(saved['ram_bytes'], None if source == 'measured' and override is None else wanted)
                self.assertEqual(len(saved['dependency_digest']), 64)

    def test_api_billing_is_removed_from_provider_environment(self):
        value = accounts.account_environment({'OPENAI_API_KEY': 'x', 'ANTHROPIC_API_KEY': 'y',
            'ANTHROPIC_AUTH_TOKEN': 'z', 'CLAUDE_CODE_USE_BEDROCK': '1', 'KEEP': 'value'})
        self.assertEqual(value, {'KEEP': 'value', 'DISABLE_AUTOUPDATER': '1'})

    def test_private_state_is_bounded_and_symlinks_are_refused(self):
        self.state.write_bytes(b'x' * 4097)
        with self.assertRaises(ValueError):
            desktop.record()
        self.state.unlink(); self.state.symlink_to(self.root / 'elsewhere')
        with self.assertRaises(ValueError):
            desktop.record()


    def test_generated_launchers_never_follow_a_user_symlink(self):
        original = self.root / 'keep'; original.write_text('preserved')
        launcher = self.root / 'launcher'; launcher.symlink_to(original)
        with self.assertRaises(ValueError):
            desktop.write_script(launcher, 'replacement')
        self.assertEqual(original.read_text(), 'preserved')

    def test_managed_browser_rejects_paths_outside_the_runtime(self):
        marker = self.root / 'installed.json'
        for name in ['/usr/bin/browser', '../outside']:
            marker.write_text(json.dumps({'browser': name}))
            with self.assertRaises(ValueError):
                desktop_runtime.browser(self.root)

    def test_remote_codex_rules_apply_outside_the_home_project_and_preserve_custom_rules(self):
        (self.root / 'AGENTS.md').write_text('reserved desktop rules')
        desktop.install_global_instructions(self.root)
        rules = self.root / '.codex/AGENTS.md'
        self.assertIn(str(self.root / 'AGENTS.md'), rules.read_text())
        rules.write_text('custom rules')
        desktop.install_global_instructions(self.root)
        self.assertEqual(rules.read_text(), 'custom rules')

    def test_global_instructions_do_not_follow_a_profile_symlink(self):
        target = self.root / 'keep'; target.mkdir()
        (self.root / '.codex').symlink_to(target, target_is_directory=True)
        with self.assertRaises(ValueError):
            desktop.install_global_instructions(self.root)
        self.assertEqual(list(target.iterdir()), [])

    def test_real_desktop_dependency_count_keeps_its_download_byte_limit(self):
        archives = self.root / 'archives'; archives.mkdir()
        for i in range(452):
            (archives / (str(i) + '.deb')).touch()
        self.assertEqual(len(desktop_runtime.package_archives(self.root, [])), 452)
        huge = Mock(); huge.stat.return_value.st_size = desktop_runtime.GIB + 1
        with self.assertRaises(ValueError):
            desktop_runtime.package_archives(self.root, [huge])
        for i in range(452, 513):
            (archives / (str(i) + '.deb')).touch()
        with self.assertRaises(ValueError):
            desktop_runtime.package_archives(self.root, [])

    def test_only_owned_closed_software_stages_are_removed(self):
        spec = {'version': 'test'}
        fingerprint = hashlib.sha256(json.dumps(spec, sort_keys=True).encode()).hexdigest()
        closed = self.root / ('.desktop-incoming-' + 'b' * 32); closed.mkdir()
        resources.write_json(closed / '.atelier-stage.json', {'spec': fingerprint, 'job': 'c' * 32, 'runner': '999999:1'})
        (closed / 'package').write_text('replaceable software')
        unknown = self.root / ('.desktop-incoming-' + 'd' * 32); unknown.mkdir()
        (unknown / 'personal').write_text('keep')
        with patch.object(resources, 'leases', return_value=[]), \
                patch.object(desktop_runtime, 'process_token', return_value=None):
            desktop_runtime.cleanup_stages(self.root, self.root, spec)
        self.assertFalse(closed.exists())
        self.assertEqual((unknown / 'personal').read_text(), 'keep')

    def test_live_installation_stage_is_preserved(self):
        spec = {'version': 'test'}
        stage = self.root / ('.desktop-incoming-' + 'a' * 32); stage.mkdir()
        resources.write_json(stage / '.atelier-stage.json', {
            'spec': hashlib.sha256(json.dumps(spec, sort_keys=True).encode()).hexdigest(),
            'job': 'a' * 32, 'runner': '999999:1'})
        with patch.object(resources, 'leases', return_value=[{'id': 'a' * 32}]), \
                patch.object(desktop_runtime, 'process_token', return_value=None):
            desktop_runtime.cleanup_stages(self.root, self.root, spec)
        self.assertTrue(stage.exists())

    def test_stop_cannot_signal_a_process_outside_its_reservation(self):
        ident = 'a' * 32
        (self.root / 'jobs' / ident).mkdir(parents=True)
        for parent, signalled in [(111, True), (333, False)]:
            with patch.object(worker, 'ROOT', self.root), \
                    patch.object(resources, 'leases', return_value=[{'id': ident, 'runner': '111:3'}]), \
                    patch.object(worker, 'process_token', return_value='222:4'), \
                    patch.object(Path, 'read_text', return_value=f'222 (python3) S {parent} 0 0'), \
                    patch.object(desktop.os, 'kill') as kill:
                if signalled:
                    desktop.stop_program(ident, {'process': '222:4'})
                    kill.assert_called_once_with(222, desktop.signal.SIGTERM)
                else:
                    with self.assertRaises(PermissionError):
                        desktop.stop_program(ident, {'process': '222:4'})
                    kill.assert_not_called()

    def test_cancelled_setup_does_not_train_a_small_desktop_ram_profile(self):
        ident = 'a' * 32
        self.state.write_text(json.dumps({'id': ident, 'phase': 'preparing', 'request': {}}))
        def calculation(*args, **kwargs):
            self.assertFalse(kwargs['profile_ready']())
            self.state.write_text(json.dumps({'id': ident, 'phase': 'ready'}))
            self.assertTrue(kwargs['profile_ready']())
            return {'status': 'passed'}
        with patch('quota_protection.protect_quota'), patch.object(worker, 'validate_environment'), \
                patch.object(worker, 'run_job', side_effect=calculation):
            desktop.supervise(ident)


if __name__ == '__main__':
    unittest.main()
