import contextlib
import io
import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

import client_config
import vps
from scripts.install import timeout_setting
from ops.render_compose import bindings
from scripts.checks import ssh_frontends


class ClientConfigurationTests(unittest.TestCase):
    def test_global_status_alias_keeps_the_whole_fleet(self):
        with patch('sys.argv', ['vps.py', 'status', '--global']), patch.object(
                vps, 'fleet_status', return_value={'ok': True}) as status, contextlib.redirect_stdout(io.StringIO()):
            self.assertEqual(vps.main(), 0)
            status.assert_called_once_with(None)
        with patch('sys.argv', ['vps.py', '--server', 'one', 'status', '--global']), contextlib.redirect_stderr(io.StringIO()):
            with self.assertRaises(SystemExit):
                vps.main()

    def test_qa_teardown_selects_only_its_own_ssh_frontends(self):
        listing = '100 1 docker\n101 100 /usr/bin/ssh\n102 101 ssh\n103 1 ssh\n104 1 python\n'
        with patch('scripts.checks.subprocess.check_output', return_value=listing):
            self.assertEqual(ssh_frontends(100), [101, 102])

    def test_binds_stay_on_the_added_volume_and_tools_are_read_only(self):
        values = bindings({'volume_mount': '/mnt/example'})
        self.assertEqual(len(values), 9)
        self.assertEqual(values[0], '/mnt/example/usevps:/workspace')
        self.assertTrue(all(x.endswith(':ro') for x in values[1:]))
        with self.assertRaises(ValueError):
            bindings({'volume_mount': '/'})
        with self.assertRaises(ValueError):
            bindings({'volume_mount': '/mnt/example', 'workspace': '/other-disk'})

    def test_personal_endpoints_and_alias_validation(self):
        with tempfile.TemporaryDirectory() as tmp:
            file = Path(tmp) / 'config.json'
            file.write_text(json.dumps({'ssh_host': 'my-worker'}))
            with patch.dict(client_config.os.environ, {'ATELIER_VPS_CONFIG': str(file)}, clear=True):
                self.assertEqual(client_config.host(), 'my-worker')
                with patch.dict(client_config.os.environ, {'ATELIER_VPS_HOST': '-oProxyCommand=bad'}):
                    with self.assertRaises(ValueError):
                        client_config.host()

    def test_mcp_timeout_update_preserves_other_configuration(self):
        with tempfile.TemporaryDirectory() as tmp:
            file = Path(tmp) / 'config.toml'
            file.write_text('model = "existing"\n[mcp_servers.other]\ncommand = "other"\n'
                            '[mcp_servers.atelier-vps]\ncommand = "uv"\ntool_timeout_sec = 60\n'
                            '[profiles.existing]\nmodel = "existing"\n')
            original_other = file.read_text().split('[mcp_servers.atelier-vps]')[0]
            timeout_setting(file)
            timeout_setting(file)
            value = file.read_text()
            self.assertTrue(value.startswith(original_other))
            self.assertEqual(value.count('tool_timeout_sec = 3700'), 1)
            self.assertIn('[profiles.existing]\nmodel = "existing"', value)
