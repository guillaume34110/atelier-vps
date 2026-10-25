from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

import resources
import worker


class Planning(unittest.TestCase):
    def test_auto_plan_uses_the_measured_margin_once_without_reserving(self):
        with tempfile.TemporaryDirectory() as directory:
            root=Path(directory)
            request={'argv':['python3','check.py'],'project':'api','dependency_digest':'a'*64,
                     'ram_bytes':None,'cpus':1,'disk_bytes':16*resources.MIB}
            resources.write_json(root/'.ram-profiles.json', {resources.profile_key(request):{'peak':512*resources.MIB,'samples':1}})
            before=(root/'.ram-profiles.json').read_bytes()
            pool={'cpu_available':2,'ram_available':resources.GIB,'disk_available':resources.GIB,
                  'active':[],'active_count':0,'max_jobs':4,'ram_accounting':{}}
            state={'ok':True,'scope':{'id':'owner'},'maintenance':False,'pool':pool}
            with patch.object(worker,'ROOT',root), patch.object(worker,'public_status',return_value=state), patch.object(resources,'reserve') as reserve:
                result=worker.plan(request)
                self.assertTrue(result['admission']['allowed'])
                self.assertEqual(result['requested']['ram_bytes'],704*resources.MIB)
                self.assertEqual(result['requested']['ram_source'],'measured')
                result=worker.plan({**request,'cpus':4})
                self.assertEqual(result['admission']['reasons'],['cpu'])
                state['maintenance']=True
                self.assertIn('maintenance',worker.plan(request)['admission']['reasons'])
            reserve.assert_not_called()
            self.assertEqual((root/'.ram-profiles.json').read_bytes(),before)
            self.assertEqual([p.name for p in root.iterdir()],['.ram-profiles.json'])

    def test_plan_and_execution_share_request_validation(self):
        for request in [{'argv':[]},{'argv':['true'],'timeout':3601},
                        {'argv':['true'],'disk_bytes':-1},{'argv':['true'],'cpus':0},
                        {'argv':['true'],'ram_bytes':True}]:
            with self.subTest(request=request), self.assertRaises(ValueError):
                worker.plan(request)
