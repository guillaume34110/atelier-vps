import contextlib
import io
import json
import os
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

from scripts import install
import client_config


class Installer(unittest.TestCase):
    def test_invitation_first_install_has_no_invented_server_and_keeps_existing_access(self):
        with tempfile.TemporaryDirectory() as folder:
            home = Path(folder)
            config = home / '.config/atelier-vps/config.json'
            with patch.object(Path, 'home', return_value=home), patch.object(install, 'codex_binary', return_value=None), patch.object(install.shutil, 'which', return_value=None), patch.dict(os.environ, {'ATELIER_VPS_CONFIG': str(config)}):
                with patch('sys.argv', ['install.py']), contextlib.redirect_stdout(io.StringIO()):
                    install.main()
                self.assertEqual(client_config.connections(), [])
                self.assertTrue((home / '.local/bin/atelier-vps').is_file())
                registry = config.with_name('connections.json')
                self.assertEqual(registry.stat().st_mode & 0o777, 0o600)
                with patch('sys.argv', ['install.py', '--host', 'configured-worker']), contextlib.redirect_stdout(io.StringIO()):
                    install.main()
                self.assertEqual(client_config.connections()[0]['ssh_host'], 'configured-worker')
                registry.unlink()
                config.write_text(json.dumps({'ssh_host':'existing-worker','admin_ssh_host':'existing-admin'}))
                with patch('sys.argv', ['install.py']), contextlib.redirect_stdout(io.StringIO()):
                    install.main()
                self.assertEqual(client_config.host(), 'existing-worker')
                self.assertEqual(client_config.connections()[0]['admin_ssh_host'], 'existing-admin')
