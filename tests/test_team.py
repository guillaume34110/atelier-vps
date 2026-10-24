import base64
import copy
import fcntl
import json
import os
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch
from types import SimpleNamespace

from ops import gateway, team, team_settings as settings
from ops.deploy_lock import deployment_lock
import worker

KEY = 'ssh-ed25519 ' + base64.b64encode(b'\x00\x00\x00\x0bssh-ed25519\x00\x00\x00\x20' + b'x' * 32).decode()
OTHER_KEY = 'ssh-ed25519 ' + base64.b64encode(b'\x00\x00\x00\x0bssh-ed25519\x00\x00\x00\x20' + b'y' * 32).decode()
CAPACITY = {'cpus': 4, 'ram_bytes': 5 * team.GIB, 'disk_bytes': 26 * team.GIB, 'slots': 4}


def policy():
    return {'mode': 'allocated', 'pool': dict(CAPACITY), 'users': [
        {'id': name, 'public_key': KEY if name == 'owner' else OTHER_KEY, 'cpu_percent': 50, 'ram_percent': 50,
         'disk_percent': 50, 'slots': 2} for name in ['owner', 'bob']]}


class TeamTests(unittest.TestCase):
    def test_percentages_have_explicit_denominators_and_disabled_data_stays_reserved(self):
        value = policy()
        result = team.allocations(value, CAPACITY)
        self.assertEqual(result['bob']['cpus'], 2)
        self.assertEqual(result['bob']['ram_bytes'], 5 * team.GIB // 2)
        self.assertEqual(result['bob']['disk_bytes'], 13 * team.GIB)
        self.assertEqual(result['bob']['ssh_user'], 'avps-bob')
        self.assertEqual(sum(value['cpu_shares'] for value in result.values()), 128)
        value['users'][1]['enabled'] = False
        value['users'][0]['disk_percent'] = 100
        with self.assertRaisesRegex(ValueError, 'disabled developers retain'):
            team.allocations(value, CAPACITY)

    def test_refuse_overcommit_invalid_id_key_and_unbounded_parallelism(self):
        for mutation in [lambda p: p['users'][0].update(ram_percent=51),
                         lambda p: p['users'][0].update(cpu_percent=float('nan')),
                         lambda p: p['users'][0].update(id='../bob'),
                         lambda p: p['users'][0].update(id='admin'),
                         lambda p: p['users'][0].update(public_key='command="sh" ' + KEY),
                         lambda p: p['users'][1].update(public_key=KEY),
                         lambda p: p['users'][0].update(slots=3),
                         lambda p: p['pool'].update(ram_bytes=6 * team.GIB)]:
            value = policy(); mutation(value)
            with self.subTest(value=value), self.assertRaises(ValueError):
                team.allocations(value, CAPACITY)

    def test_both_modes_and_changed_worker_selection_preserve_neighbors(self):
        previous = policy(); candidate = copy.deepcopy(previous)
        candidate['users'][0]['public_key'] = KEY + ' rotated-label'
        self.assertEqual(team.changes(previous, candidate), ['owner'])
        candidate['users'].pop()
        with self.assertRaisesRegex(ValueError, 'preserve existing data'):
            team.changes(previous, candidate)
        value = policy(); value['mode'] = 'opportunistic'
        with self.assertRaisesRegex(ValueError, 'exactly one'):
            team.allocations(value, CAPACITY)
        value['users'][1]['enabled'] = False
        self.assertTrue(team.allocations(value, CAPACITY)['owner']['enabled'])

    def test_stale_settings_cannot_mutate_or_preview_host(self):
        with patch.object(settings, 'preview') as preview:
            with self.assertRaisesRegex(ValueError, 'revision changed'):
                settings.apply({'policy': policy()}, policy(), 'stale')
            preview.assert_not_called()

    def test_preview_refuses_disk_or_inode_quota_below_preserved_data(self):
        state = {'policy': policy(), 'capacity': CAPACITY, 'owner_public_key': KEY,
                 'tenants': {'owner': {'project_id': 34110}}}
        for used, inodes in [(14 * team.GIB, 10), (team.GIB, 300000)]:
            with patch.object(settings, 'read', return_value={'mount': '/mnt/data'}), patch.object(settings, 'verify_mount'), patch.object(settings, 'quota_usage', return_value={34110: {'used': used, 'inodes': inodes}}), patch.object(settings, 'command') as mutate:
                with self.assertRaisesRegex(ValueError, 'quota below existing data'):
                    settings.preview(state, policy())
                mutate.assert_not_called()

    def test_allocated_commitments_must_preserve_real_host_memory_floor(self):
        state = {'policy': policy(), 'capacity': CAPACITY, 'owner_public_key': KEY,
                 'tenants': {'owner': {'project_id': 34110, 'container': 'own'}}}
        physical = SimpleNamespace(f_bavail=30 * team.GIB, f_frsize=1)
        with patch.object(settings, 'read', return_value={'mount': '/mnt/data'}), patch.object(settings, 'verify_mount'), patch.object(settings, 'quota_usage', return_value={34110: {'used': 0, 'inodes': 10}}), patch.object(settings.os, 'statvfs', return_value=physical), patch.object(settings.Path, 'read_text', return_value='MemAvailable: 4194304 kB\n'), patch.object(settings, 'inspect', return_value=None):
            with self.assertRaisesRegex(ValueError, '2 GiB host floor'):
                settings.preview(state, policy())

    def test_reclaimable_worker_cache_is_not_counted_twice_as_available_ram(self):
        candidate = policy(); candidate['pool']['ram_bytes'] = 2 * team.GIB
        state = {'policy': policy(), 'capacity': CAPACITY, 'owner_public_key': KEY,
                 'tenants': {'owner': {'project_id': 34110, 'container': 'own'}}}
        physical = SimpleNamespace(f_bavail=30 * team.GIB, f_frsize=1)
        def counters(args):
            return str(5 * team.GIB // 2) if args[-1].endswith('memory.current') else 'anon 536870912\nfile 2147483648\n'
        with patch.object(settings, 'read', return_value={'mount': '/mnt/data'}), patch.object(settings, 'verify_mount'), patch.object(settings, 'quota_usage', return_value={34110: {'used': 0, 'inodes': 10}}), patch.object(settings.os, 'statvfs', return_value=physical), patch.object(settings, 'inspect', return_value={'State': {'Running': True}}), patch.object(settings, 'command', side_effect=counters):
            with patch.object(settings.Path, 'read_text', return_value='MemAvailable: 3145728 kB\n'):
                with self.assertRaisesRegex(ValueError, '2 GiB host floor'):
                    settings.preview(state, candidate)
            with patch.object(settings.Path, 'read_text', return_value='MemAvailable: 4194304 kB\n'):
                self.assertEqual(settings.preview(state, candidate)['policy']['pool']['ram_bytes'], 2 * team.GIB)

    def test_active_work_blocks_every_settings_mutation(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            state = {'policy': policy(), 'tenants': {'owner': {'workspace': str(root)}}}
            candidate = copy.deepcopy(state['policy']); candidate['users'][0]['ram_percent'] = 40
            plan = {'affected': ['owner'], 'candidate_revision': team.revision(candidate)}
            with (root / '.queue.lock').open('a') as active:
                fcntl.flock(active, fcntl.LOCK_SH)
                with patch.object(settings, 'preview', return_value=plan), patch.object(settings, 'read', return_value={}):
                    with patch.object(settings, 'PENDING', root / 'pending.json'), patch.object(settings, 'save') as save, patch.object(settings, 'coolify') as api:
                        with self.assertRaisesRegex(ValueError, 'active work'):
                            settings.apply(state, candidate, team.revision(state['policy']))
                        save.assert_not_called(); api.assert_not_called()

    def test_ordinary_ssh_identity_cannot_select_another_developer(self):
        state = {'tenants': {'owner': {'ssh_user': 'usevps', 'enabled': True},
                             'bob': {'ssh_user': 'avps-bob', 'enabled': True}}}
        def read(path):
            return json.dumps(state if str(path).endswith('atelier-vps-team.json') else {})
        with patch.dict(os.environ, {'SSH_ORIGINAL_COMMAND': 'status', 'SUDO_USER': 'unknown'}), patch.object(gateway.Path, 'exists', return_value=True), patch.object(gateway.Path, 'read_text', autospec=True, side_effect=read), patch.object(gateway.subprocess, 'check_output') as access:
            with self.assertRaises(PermissionError):
                gateway.main()
            access.assert_not_called()

    def test_allocated_status_excludes_host_and_other_developers(self):
        state = {'scope': {'id': 'bob', 'mode': 'allocated'}, 'busy': False,
                 'pool': {'ram_available': 100, 'ram_accounting': {'host_available_bytes': 900, 'host_floor_bytes': 200}},
                 'workspace_free_bytes': 500, 'host_available_ram': 900, 'free_bytes': 1000,
                 'reserve_bytes': 512, 'volume_id': 'host-volume'}
        telemetry = {'host_total_ram': 8000, 'cpu_total': 1000, 'cpu_idle': 900,
                     'system_disk_total': 40000, 'system_disk_free': 5000, 'worker_ram': 100,
                     'activity': [], 'worker_ram_limit': 1000}
        with patch.object(worker, 'status', return_value=state), patch.object(worker, 'snapshot', return_value=telemetry), patch.dict(os.environ, {'USEVPS_CPU_LIMIT': '0.5'}):
            value = worker.public_status()
        self.assertNotIn('host_total_ram', value['telemetry'])
        self.assertNotIn('system_disk_free', value['telemetry'])
        self.assertNotIn('host_available_bytes', value['pool']['ram_accounting'])
        self.assertEqual(value['free_bytes'], 500)
        self.assertEqual(value['telemetry']['worker_cpu_limit'], 0.5)

    def test_admin_helper_rejects_unprivileged_execution_before_read(self):
        with patch.object(settings.os, 'geteuid', return_value=10001), patch.object(settings, 'read') as read:
            with self.assertRaises(PermissionError):
                settings.main()
            read.assert_not_called()

    def test_administrator_lock_never_follows_user_workspace_symlinks(self):
        for existing in [True, False]:
            with tempfile.TemporaryDirectory() as directory:
                root = Path(directory); workspace = root / 'workspace'; workspace.mkdir()
                outside = root / 'outside'
                if existing:
                    outside.write_text('preserve')
                (workspace / '.queue.lock').symlink_to(outside)
                with self.assertRaises(OSError), deployment_lock(workspace):
                    pass
                self.assertEqual(outside.exists(), existing)
                if existing:
                    self.assertEqual(outside.read_text(), 'preserve')
