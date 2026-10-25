import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

from ops import install_team, team_settings as settings


class InstallationLocks(unittest.TestCase):
    def test_shared_helper_update_requires_idle_owner_and_all_member_locks(self):
        with tempfile.TemporaryDirectory() as folder:
            root = Path(folder); public = root / 'administrator.pub'; public.write_text('public fixture')
            state_path = root / 'state.json'; state_path.write_text('{}')
            base = {'container':'owner-worker', 'workspace':str(root / 'owner')}
            state = {'tenants':{}}
            argv = ['install_team.py','--application','app123','--admin-key',str(public)]
            with patch('sys.argv',argv), patch.object(install_team.os,'geteuid',return_value=0), patch.object(settings,'STATE',state_path), patch.object(settings,'LOCK',root/'team.lock'), patch.object(settings,'read',side_effect=lambda p: base if p == settings.BASE else state), patch.object(settings,'command') as command, patch.object(install_team,'install') as mutate:
                for status in [{'maintenance':False,'pool':{'active_count':0}}, {'maintenance':True,'pool':{'active_count':1}}]:
                    command.return_value=json.dumps(status)
                    with self.assertRaises(ValueError): install_team.main()
                    mutate.assert_not_called()
                command.return_value=json.dumps({'maintenance':True,'pool':{'active_count':0}})
                state['tenants']['member']={'workspace':str(root/'member')}
                with patch('ops.deploy_lock.deployment_lock',side_effect=BlockingIOError):
                    with self.assertRaises(BlockingIOError): install_team.main()
                mutate.assert_not_called()
                state['tenants']={}
                install_team.main()
                mutate.assert_called_once_with('app123',public)
