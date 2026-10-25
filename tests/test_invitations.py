import base64
import json
import os
from pathlib import Path
import tempfile
import time
import unittest
from unittest.mock import patch

from ops import invitations as invites, team, team_settings as settings
import onboarding


def key(char):
    return 'ssh-ed25519 ' + base64.b64encode(b'\0\0\0\x0bssh-ed25519\0\0\0\x20' + char.encode() * 32).decode()


class Enrollment(unittest.TestCase):
    def setUp(self):
        temp = tempfile.TemporaryDirectory(); self.addCleanup(temp.cleanup)
        self.root = Path(temp.name); self.principal = 'avpi-' + 'a' * 24
        self.policy = {'mode': 'opportunistic', 'pool': {'cpus': 4, 'ram_bytes': 5 * 2**30, 'disk_bytes': 20 * 2**30, 'slots': 4},
                       'users': [{'id': 'owner', 'cpu_percent': 100, 'ram_percent': 100, 'disk_percent': 100, 'slots': 4}]}
        self.state = {'policy': self.policy, 'owner_public_key': key('a'),
                      'tenants': {'owner': {'ssh_user': 'usevps', 'enabled': True}}}
        self.record = {'member': 'owner', 'uid': 1234, 'expires': int(time.time()) + 1800,
                       'revision': team.revision(self.policy), 'server_id': 'a' * 64, 'used': False, 'public_key': None}
        for name, value in [('state.json', self.state), (self.principal + '.json', self.record)]:
            (self.root / name).write_text(json.dumps(value))
        patches = [patch.object(invites, 'ROOT', self.root), patch.object(invites, 'DEVICES', self.root / 'devices.json'),
                   patch.object(invites, 'server_identity', return_value='a' * 64), patch.object(invites, 'verify_principal'),
                   patch.object(settings, 'STATE', self.root / 'state.json'), patch.object(settings, 'PENDING', self.root / 'pending.json'),
                   patch.object(settings, 'read', side_effect=lambda p: json.loads(p.read_text())),
                   patch.object(settings, 'identity'), patch.object(settings, 'command')]
        for p in patches:
            mock = p.start(); self.addCleanup(p.stop)
        self.identity = settings.identity; self.command = settings.command

    def test_one_device_keeps_primary_key_and_never_changes_resources(self):
        result = invites.enroll(self.principal, key('b'))
        self.assertTrue(result['enrolled'])
        self.identity.assert_called_once_with('usevps', key('a'), existing=True)
        self.assertEqual(json.loads((self.root / 'state.json').read_text()), self.state)
        self.assertEqual(invites.additional_keys('usevps'), [key('b')])
        self.command.assert_called_once_with(['usermod', '--lock', self.principal])
        for pub in [key('b'), key('c')]:
            with self.assertRaises(PermissionError): invites.enroll(self.principal, pub)

    def test_expiration_revision_disabled_member_and_pending_update_refuse_access(self):
        variants = [{'expires': 1}, {'revision': 'c' * 64}, {'public_key': key('c')}]
        for change in variants:
            with self.subTest(change=change):
                (self.root / (self.principal + '.json')).write_text(json.dumps({**self.record, **change}))
                with self.assertRaises(PermissionError): invites.enroll(self.principal, key('b'))
        self.identity.assert_not_called()
        (self.root / (self.principal + '.json')).write_text(json.dumps(self.record))
        (self.root / 'pending.json').write_text('{}')
        with self.assertRaises(PermissionError): invites.enroll(self.principal, key('b'))
        (self.root / 'pending.json').unlink()
        self.state['tenants']['owner']['enabled'] = False
        (self.root / 'state.json').write_text(json.dumps(self.state))
        with self.assertRaises(PermissionError): invites.enroll(self.principal, key('b'))
        self.identity.assert_not_called()

    def test_interrupted_install_resumes_only_the_same_public_device(self):
        self.identity.side_effect = OSError('fixture interruption')
        with self.assertRaises(OSError): invites.enroll(self.principal, key('b'))
        with self.assertRaises(PermissionError): invites.enroll(self.principal, key('c'))
        self.identity.side_effect = None
        self.assertTrue(invites.enroll(self.principal, key('b'))['enrolled'])
        self.assertEqual(invites.additional_keys('usevps'), [key('b')])

    def test_key_cannot_cross_members(self):
        self.state['policy']['users'].append({'id': 'camille', 'public_key': key('c')})
        self.record['revision'] = team.revision(self.state['policy'])
        (self.root / 'state.json').write_text(json.dumps(self.state))
        (self.root / (self.principal + '.json')).write_text(json.dumps(self.record))
        with self.assertRaises(PermissionError): invites.enroll(self.principal, key('c'))
        self.identity.assert_not_called()

    def test_private_delivery_does_not_return_the_link(self):
        with patch.dict(os.environ, {'ATELIER_VPS_CONFIG': str(self.root / 'config.json')}), patch.object(onboarding, 'create_invitation', return_value=('private-fixture-link', {'expires': 1234})):
            result = onboarding.deliver_invitation('owner', 'paris', 'a' * 64)
        target = Path(result['private_file'])
        self.assertEqual(target.stat().st_mode & 0o777, 0o600)
        self.assertEqual(target.parent.stat().st_mode & 0o777, 0o700)
        self.assertNotIn('private-fixture-link', json.dumps(result))

    def test_administrator_overview_exposes_only_verified_counters(self):
        state = {'tenants': {'owner': {'enabled': True, 'container': 'own-worker',
                                      'ram_bytes': 5 * 2**30, 'disk_bytes': 20 * 2**30}}}
        sample = {'ok': True, 'scope': {'id': 'owner'}, 'used_bytes': 4096, 'private_log': 'private-fixture-log',
                  'pool': {'ram_accounting': {'used_bytes': 512}, 'ram_reserved': 1024,
                           'ram_available': 2048, 'cpu_reserved': 1, 'active_count': 1}}
        self.command.return_value = json.dumps(sample)
        result = settings.overview(state)
        self.assertTrue(result[0]['connected'])
        self.assertEqual(result[0]['ram_used'], 512)
        self.assertNotIn('private-fixture-log', json.dumps(result))
        sample['scope']['id'] = 'another-member'
        self.command.return_value = json.dumps(sample)
        self.assertFalse(settings.overview(state)[0]['connected'])
        self.command.reset_mock(); state['tenants']['owner']['enabled'] = False
        self.assertFalse(settings.overview(state)[0]['connected'])
        self.command.assert_not_called()
