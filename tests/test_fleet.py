import copy
import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

import vps


def state(identity='a', ram=3*vps.GIB, active=0, arch='aarch64'):
    return {'ok':True,'admit':True,'maintenance':False,'busy':bool(active),'server_id':identity*64,
        'scope':{'id':'owner','mode':'opportunistic'},'capabilities':{'planning':True,'ram_profiles':True},
        'telemetry':{'arch':arch},'pool':{'cpu_total':4,'cpu_reserved':active,'cpu_available':4-active,
        'ram_total':5*vps.GIB,'ram_reserved':active*vps.GIB,'ram_available':min(ram,(5-active)*vps.GIB),
        'disk_available':8*vps.GIB,'disk_reserved':0,'active_count':active,'max_jobs':4,
        'active':[{'id':str(i)*32,'agent':'Tests','project':'API','started':1} for i in range(active)]}}


class Fleet(unittest.TestCase):
    def setUp(self):
        self.profiles=[{'id':name,'name':name.title(),'team':'Équipe '+name,'ssh_host':name+'-worker',
                        'enabled':True,'managed':False} for name in ['paris','lyon']]
        for name in ['connections','active_connections']:
            p=patch.object(vps.client_config,name,side_effect=lambda:self.profiles)
            p.start();self.addCleanup(p.stop)
        p=patch.object(vps.client_config,'save_connection',side_effect=lambda row:row)
        p.start();self.addCleanup(p.stop)
        self.states={'paris-worker':state('a',active=1),'lyon-worker':state('b')}
        self.calls=[]
        p=patch.object(vps,'transport',side_effect=self.transport);p.start();self.addCleanup(p.stop)
        self.request={'argv':['true'],'timeout':60,'project':'api','dependency_digest':'a'*64,
                      'cpus':1,'ram_bytes':None,'disk_bytes':vps.GIB}

    def transport(self,host,operation,request=None,**kwargs):
        self.calls.append((host,operation,copy.deepcopy(request)))
        value=self.states[host]
        if isinstance(value,Exception):raise value
        if operation=='status':result=value
        elif operation=='plan':
            pool=value['pool'];ram=request.get('ram_bytes') or vps.GIB
            allowed=(request['cpus']<=pool['cpu_available'] and ram<=pool['ram_available'] and request['disk_bytes']<=pool['disk_available'])
            result={'ok':True,'scope':value['scope'],'admission':{'allowed':allowed},
                    'requested':{'cpus':request['cpus'],'ram_bytes':ram,'disk_bytes':request['disk_bytes'],'ram_source':'unprofiled'}}
        elif operation=='run':
            if request.get('project_create'):
                return 0,json.dumps({'id':'d'*32,'status':'passed','managed_project':request['project_create']}).encode()
            if request.get('project_from'):
                self.assertIsNone(kwargs.get('source'))
                return 0,json.dumps({'id':'e'*32,'status':'passed'}).encode()
            return 1,json.dumps({'id':'c'*32,'status':'failed','returncode':7}).encode()
        elif operation=='fetch' and 'wait_timeout' in request:
            result={'kind':'job_wait','id':request['id'],'status':'passed','timed_out':False,'argv':['secret']}
        else:raise AssertionError(operation)
        return 0,json.dumps(result).encode()

    def test_global_excludes_duplicate_aliases_and_offline_samples(self):
        self.states['lyon-worker']=copy.deepcopy(self.states['paris-worker'])
        result=vps.fleet_status()
        self.assertEqual(result['global']['servers_connected'],1)
        self.assertEqual(result['global']['cpu_total'],4)
        self.assertEqual(result['connections'][1]['duplicate_of'],'paris')
        self.states['lyon-worker']=TimeoutError('private diagnostic must not leak')
        result=vps.fleet_status()
        self.assertEqual(result['global']['ram_available'],3*vps.GIB)
        self.assertFalse(result['connections'][1]['connected'])
        self.assertNotIn('private diagnostic',json.dumps(result))

    def test_shared_mac_is_counted_once_without_inventing_pool_capacity(self):
        self.states['paris-worker']['mac']={'node_id':'c'*32,'available':True}
        self.states['lyon-worker']['mac']={'node_id':'c'*32,'available':False}
        result=vps.fleet_status()
        self.assertEqual((result['global']['macs_connected'],result['global']['macs_available']),(1,1))
        self.assertEqual(result['global']['cpu_total'],8)
        self.assertEqual(result['connections'][0]['mac']['node_id'],'c'*32)
        self.states['lyon-worker']['mac']['node_id']='invalid'
        result=vps.fleet_status()
        self.assertFalse(result['connections'][1]['connected'])

    def test_complete_claim_is_placed_on_one_fresh_compatible_server(self):
        selected=vps.select_server(self.request)
        self.assertEqual(selected['id'],'lyon')
        self.assertEqual(selected['request'],self.request)
        plans=[x for x in self.calls if x[1]=='plan']
        self.assertEqual(len(plans),2)
        self.assertTrue(all(x[2]['ram_bytes'] is None and x[2]['disk_bytes']==vps.GIB for x in plans))

    def test_large_sources_select_updated_workers_before_submission(self):
        request = {**self.request, 'source_bytes': 65 * 1024 ** 2}
        with self.assertRaisesRegex(RuntimeError, 'update_worker_for_budgeted_transfers'):
            vps.select_server(request)
        self.assertFalse(any(call[1] == 'run' for call in self.calls))
        self.states['paris-worker']['capabilities']['transfer_budgets'] = True
        self.assertEqual(vps.select_server(request)['id'], 'paris')

    def test_global_ram_cannot_be_combined_or_claims_shrunk(self):
        request={**self.request,'ram_bytes':4*vps.GIB}
        with self.assertRaises(RuntimeError):vps.select_server(request)
        self.assertFalse(any(x[1]=='run' for x in self.calls))
        self.assertEqual(request['ram_bytes'],4*vps.GIB)

    def test_executed_failure_is_returned_once_with_its_original_server(self):
        rc,result=vps.run_on_server(self.request,Path('.'))
        self.assertEqual(rc,1)
        self.assertEqual(result['job_ref'],'lyon:'+ 'c'*32)
        self.assertEqual(result['connection'],'lyon')
        self.assertEqual(len([x for x in self.calls if x[1]=='run']),1)

    def test_wait_uses_original_authenticated_server_and_requires_capability(self):
        self.states['lyon-worker']['capabilities']['job_wait'] = True
        result = vps.wait_for_job('lyon:' + 'c' * 32, 3)
        self.assertEqual(result['job_ref'], 'lyon:' + 'c' * 32)
        self.assertEqual(result['status'], 'passed')
        self.assertNotIn('argv', result)
        self.assertEqual([call[0] for call in self.calls], ['lyon-worker', 'lyon-worker'])
        with self.assertRaises(RuntimeError):
            vps.wait_for_job('paris:' + 'c' * 32, 1)
        with self.assertRaises(ValueError):
            vps.wait_for_job('lyon:' + 'c' * 32, -1)

    def test_managed_project_is_pinned_to_its_server_and_rejects_stale_sources(self):
        self.states['lyon-worker']['capabilities']['project_sources'] = True
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp); (root / 'main.py').write_text('print(1)\n')
            rc, prepared = vps.prepare_project(root, 'demo', 1, server='lyon')
            self.assertEqual(rc, 0)
            reference = prepared['project_ref']
            self.assertTrue(reference.startswith('lyon:demo:'))
            rc, result = vps.run_project(reference, ['true'], 'Tests', 1, source=root)
            self.assertEqual((rc, result['status'], result['project_ref']), (0, 'passed', reference))
            request = [call[2] for call in self.calls if call[1] == 'run'][-1]
            self.assertEqual(request['dependency_digest'], vps.dependency_digest(root))
            (root / 'main.py').write_text('changed\n')
            with self.assertRaises(ValueError):
                vps.run_project(reference, ['true'], 'Tests', 1, source=root)
            with self.assertRaises(ValueError):
                vps.run_project(reference, ['true'], 'Tests', 1, server='paris')
        self.assertFalse(any(host == 'paris-worker' and action == 'run' for host, action, _ in self.calls))

    def test_architecture_and_unknown_server_identity_are_not_guessed(self):
        self.states['lyon-worker']['telemetry']['arch']='x86_64'
        self.assertEqual(vps.select_server(self.request)['id'],'lyon')
        self.calls.clear()
        self.assertEqual(vps.select_server(self.request,arch='aarch64')['id'],'paris')
        self.assertFalse(any(h=='lyon-worker' and op=='plan' for h,op,_ in self.calls))
        del self.states['paris-worker']['server_id']
        result=vps.fleet_status()
        self.assertEqual(result['global']['servers_connected'],1)
        self.assertTrue(result['connections'][0]['identity_required'])

    def test_bad_counters_do_not_contribute_to_global_totals(self):
        self.states['paris-worker']['pool']['ram_available']='invalid'
        result=vps.fleet_status()
        self.assertEqual(result['global']['servers_connected'],1)
        self.assertEqual(result['global']['ram_available'],3*vps.GIB)

    def test_portable_default_refuses_unknown_architectures(self):
        self.states['paris-worker']['telemetry']['arch'] = None
        self.states['lyon-worker']['telemetry']['arch'] = 'unsupported'
        with self.assertRaisesRegex(RuntimeError, 'architecture'):
            vps.select_server(self.request)
        self.assertFalse(any(op in {'plan', 'run'} for _, op, _ in self.calls))

    def test_job_references_keep_disabled_servers_and_reject_cross_server_use(self):
        self.profiles[0]['enabled']=False
        profile,ident=vps.job_target('paris:'+ 'c'*32)
        self.assertEqual(profile['id'],'paris');self.assertEqual(ident,'c'*32)
        with self.assertRaises(ValueError):vps.job_target('c'*32)
        with self.assertRaises(ValueError):vps.job_target('paris:'+ 'c'*32,server='lyon')
        with self.assertRaises(ValueError):vps.job_target('paris:../../secret')

    def test_admin_target_is_verified_before_preview_or_apply(self):
        self.profiles[0].update(admin_ssh_host='paris-admin',server_id='a'*64)
        replies=[]
        def request(host,op,body=None,**kw):
            replies.append((host,body))
            return 0,{'server_id':'b'*64}
        with patch.object(vps,'json_request',side_effect=request):
            with self.assertRaises(PermissionError):vps.admin_request('apply','paris',policy={},expected_revision='x')
        self.assertEqual(replies,[('paris-admin',{'action':'get','expected_server_id':'a'*64})])
        replies.clear()
        with patch.object(vps,'json_request',return_value=(0,{'server_id':'a'*64})) as request:
            vps.admin_request('preview','paris',policy={})
        self.assertEqual(request.call_args.args[2]['expected_server_id'],'a'*64)
        self.assertEqual(request.call_args.args[2]['action'],'preview')
