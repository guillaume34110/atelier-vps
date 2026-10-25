import json
import os
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

import client_config as config


class Connections(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.environment = patch.dict(os.environ, {'ATELIER_VPS_CONFIG': str(self.root / 'config.json')})
        self.environment.start(); self.addCleanup(self.environment.stop)
        for key in ['ATELIER_VPS_HOST', 'ATELIER_VPS_ADMIN_HOST']:
            os.environ.pop(key, None)

    def row(self, name='paris'):
        return {'id': name, 'name': name.title(), 'team': 'Équipe Paris', 'ssh_host': 'atelier-' + name}

    def test_legacy_access_is_preserved_when_adding_a_server(self):
        (self.root / 'config.json').write_text(json.dumps({'ssh_host': 'existing', 'admin_ssh_host': 'admin-existing'}))
        self.assertEqual(config.connections()[0]['admin_ssh_host'], 'admin-existing')
        config.save_connection(self.row())
        self.assertEqual([r['id'] for r in config.connections()], ['default', 'paris'])
        self.assertEqual(config.host(), 'existing')
        self.assertEqual(config.connection('atelier-paris')['id'], 'paris')
        self.assertEqual((self.root / 'connections.json').stat().st_mode & 0o777, 0o600)

    def test_disabled_access_remains_resolvable_for_old_results(self):
        row=config.save_connection(self.row())
        row['enabled']=False; config.save_connection(row)
        self.assertEqual(config.active_connections(), [])
        self.assertEqual(config.connection('paris')['ssh_host'], 'atelier-paris')

    def test_registry_bounds_unique_aliases_and_atomic_failure(self):
        config.save_connection(self.row())
        before=(self.root / 'connections.json').read_bytes()
        other=self.row('lyon'); other['ssh_host']='atelier-paris'
        with self.assertRaises(ValueError): config.save_connection(other)
        self.assertEqual((self.root / 'connections.json').read_bytes(), before)
        (self.root / 'connections.json').write_text('x' * 131073)
        with self.assertRaises(ValueError): config.connections()

    def test_malformed_values_cannot_become_ssh_options(self):
        for update in [{'ssh_host': '-oProxyCommand=evil'}, {'id': '../bad'}, {'name': 'a\nb'},
                       {'managed': 1}, {'scope': []}, {'server_id': 123}, {'private_key': 'forbidden'},
                       {'admin_ssh_host': 123}]:
            with self.subTest(update=update), self.assertRaises(ValueError):
                config.save_connection({**self.row(), **update})

    def test_managed_config_must_be_a_bounded_regular_file(self):
        row=config.save_connection({**self.row(), 'managed': True})
        with self.assertRaises(ValueError): config.ssh_options(row['ssh_host'])
        directory=config.connection_dir('paris');directory.mkdir(parents=True)
        path=directory/'ssh.conf';path.write_text('Host atelier-paris\n')
        self.assertEqual(config.ssh_options(row['ssh_host']), ['-F', str(path)])
        path.unlink(); path.symlink_to(self.root/'config.json')
        with self.assertRaises(ValueError): config.ssh_options(row['ssh_host'])

    def test_unknown_profiles_and_duplicate_registry_ids_fail_closed(self):
        config.save_connection(self.row())
        with self.assertRaises(ValueError): config.connection('missing')
        row=config.connections()[0]
        (self.root/'connections.json').write_text(json.dumps({'version':1, 'connections':[row,row]}))
        with self.assertRaises(ValueError): config.connections()
