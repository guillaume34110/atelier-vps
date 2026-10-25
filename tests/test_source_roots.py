import json
import os
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

import vps


class SourceRoots(unittest.TestCase):
    def test_web_bridge_can_only_send_explicit_project_roots(self):
        with tempfile.TemporaryDirectory() as folder:
            root=Path(folder);project=root/'project';project.mkdir();(project/'main.py').write_text('print(42)')
            other=root/'other';other.mkdir();(other/'private.txt').write_text('never sent')
            with patch.dict(os.environ,{'ATELIER_VPS_SOURCE_ROOTS':json.dumps([str(project)])}):
                self.assertEqual([name for _,name in vps.inputs(project)],['main.py'])
                with self.assertRaises(ValueError):list(vps.inputs(other))
                (project/'outside').symlink_to(other,target_is_directory=True)
                self.assertEqual([name for _,name in vps.inputs(project)],['main.py'])

    def test_personal_state_and_private_key_names_are_never_archived(self):
        with tempfile.TemporaryDirectory() as folder:
            root=Path(folder);private=root/'settings';private.mkdir();(private/'config.json').write_text('{}')
            with patch.dict(os.environ,{'ATELIER_VPS_CONFIG':str(private/'config.json')}):
                with self.assertRaises(ValueError):list(vps.inputs(private))
            project=root/'project';project.mkdir()
            for name in ['main.py','id_ed25519_atelier_device','ssh_host_ed25519_key']:(project/name).write_text('fixture')
            hidden=project/'.codex';hidden.mkdir();(hidden/'state.txt').write_text('fixture')
            self.assertEqual([name for _,name in vps.inputs(project)],['main.py'])
