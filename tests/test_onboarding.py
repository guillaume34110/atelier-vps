import json
import os
from pathlib import Path
import sys
import tempfile
import time
import unittest
from unittest.mock import patch

import client_config as config
import onboarding as join


class Onboarding(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory(); self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name)
        environment = patch.dict(os.environ, {'ATELIER_VPS_CONFIG': str(self.root/'config.json')})
        environment.start(); self.addCleanup(environment.stop)
        for key in ['ATELIER_VPS_HOST', 'ATELIER_VPS_ADMIN_HOST']: os.environ.pop(key, None)
        self.device = join.access_request('Ordinateur de Camille')
        self.payload = {'name':'Paris', 'team':'Équipe Paris', 'hostname':'vps.example.test', 'port':22,
                        'user':'avps-camille', 'scope':'camille', 'host_key':self.device['public_key'],
                        'device_fingerprint':self.device['fingerprint']}

    def state(self, scope='camille'):
        return {'ok':True, 'pool':{}, 'scope':{'id':scope}, 'server_id':'a'*64}

    def test_real_key_generation_only_exports_public_material(self):
        request = join.read_request(self.device['request'])
        self.assertEqual(request['public_key'], self.device['public_key'])
        self.assertFalse(self.device['private_key_transferred'])
        pub = next((self.root/'keys').glob('*.pub'))
        self.assertEqual(pub.with_suffix('').stat().st_mode & 0o777, 0o600)
        self.assertNotIn('PRIVATE KEY', self.device['request'])

    def test_pinned_join_is_idempotent_and_preserves_admin_configuration(self):
        link=join.encode('connect', self.payload)
        with patch.object(join, 'status', return_value=self.state()) as status:
            row=join.accept(link)
            self.assertEqual(status.call_args.args[1][-2:], ['-S','none'])
            row['admin_ssh_host']='paris-admin';config.save_connection(row)
            again=join.accept(link)
        self.assertEqual(again['admin_ssh_host'], 'paris-admin')
        self.assertEqual(len(config.connections()), 1)
        path=config.connection_dir(row['id'])/'ssh.conf';text=path.read_text()
        self.assertIn('StrictHostKeyChecking yes', text)
        self.assertIn('ForwardAgent no', text)
        self.assertIn('IdentitiesOnly yes', text)
        self.assertEqual(path.stat().st_mode & 0o777, 0o600)
        self.assertFalse(list(self.root.glob('.join-*')))

    def test_wrong_device_and_cross_workspace_cannot_register(self):
        wrong={**self.payload,'device_fingerprint':'SHA256:'+'b'*43}
        with patch.object(join,'status') as status, self.assertRaises(ValueError):
            join.accept(join.encode('connect',wrong))
        status.assert_not_called()
        with patch.object(join,'status',return_value=self.state('other')), self.assertRaises(ValueError):
            join.accept(join.encode('connect',self.payload))
        self.assertFalse((self.root/'connections.json').exists())
        self.assertFalse(list(self.root.glob('.join-*')))
        self.assertEqual(len(list((self.root/'keys').glob('*.pub'))),1)

    def test_same_device_reuses_its_key_across_servers(self):
        other=join.access_request('Même ordinateur')
        self.assertEqual(other['fingerprint'],self.device['fingerprint'])
        self.assertEqual(len(list((self.root/'keys').glob('*.pub'))),1)

    def test_links_reject_privileged_accounts_options_and_unexpected_fields(self):
        for change in [{'user':'root'},{'user':'avps-admin'},{'hostname':'-oProxyCommand=evil'},
                       {'port':True},{'scope':'../owner'},{'host_key':'command="evil" '+self.device['public_key']},
                       {'private_key':'forbidden'},{'name':'Paris\nHost evil'}]:
            with self.subTest(change=change), patch.object(join,'status') as status, self.assertRaises(ValueError):
                join.accept(join.encode('connect',{**self.payload,**change}))
            status.assert_not_called()
        for text in ['atelier-vps://connect#'+ 'a'*8192,'https://example.test/','atelier-vps://connect#!!!']:
            with self.assertRaises(ValueError):join.accept(text)

    def test_administrator_must_have_applied_exact_key_and_host_pin(self):
        row={'id':'paris','name':'Paris','team':'Équipe Paris','ssh_host':'paris-worker','admin_ssh_host':'paris-admin'}
        config.save_connection(row)
        state={'policy':{'users':[{'id':'camille','public_key':self.device['public_key']}]},
               'tenants':{'camille':{'enabled':True,'ssh_user':'avps-camille'}},'pending':None}
        resolved=b'hostname vps.example.test\nport 22\nproxyjump none\nproxycommand none\n'
        scan=('vps.example.test '+self.device['public_key']+'\n').encode()
        debug=('debug1: Server host key: ssh-ed25519 '+self.device['fingerprint']+'\n').encode()
        with patch('vps.admin_request',return_value=state) as admin, patch.object(join,'bounded_output',side_effect=[resolved,scan,debug]):
            result=join.invitation(self.device['request'],'camille','paris')
        admin.assert_called_once_with('get', 'paris')
        self.assertFalse(result['grants_access'])
        self.assertEqual(join.decode(result['invitation'],'connect',self.payload.keys())['scope'],'camille')
        with patch('vps.admin_request',return_value=state), patch.object(join,'bounded_output',side_effect=[resolved,scan,b'debug1: wrong key']):
            with self.assertRaises(ValueError):join.invitation(self.device['request'],'camille','paris')
        state['pending']={'affected':['camille']}
        with patch('vps.admin_request',return_value=state), patch.object(join,'bounded_output') as ssh:
            with self.assertRaises(ValueError):join.invitation(self.device['request'],'camille','paris')
        ssh.assert_not_called()

    def join_payload(self):
        return {'name':'Paris', 'team':'Équipe Paris', 'hostname':'vps.example.test', 'port':22,
                'host_key':self.device['public_key'], 'principal':'avpi-'+'a'*24,
                'password':'z'*43, 'expires':int(time.time())+1800,
                'member':'camille', 'user':'avps-camille', 'server_id':'a'*64}

    def test_register_alias_authenticates_developer_and_separate_administrator(self):
        for user in ['root','ubuntu','avps-admin']:
            with self.subTest(user=user), patch.object(join,'bounded_output',return_value=('user '+user+'\n').encode()), patch.object(join,'status') as status:
                with self.assertRaises(PermissionError): join.add_alias('paris','Paris','Paris','paris-worker')
                status.assert_not_called()
        with patch.object(join,'bounded_output',return_value=b'user avps-camille\n'), patch.object(join,'status',return_value=self.state()) as status:
            row=join.add_alias('paris','Paris','Paris','paris-worker')
            self.assertEqual(row['server_id'],'a'*64)
            self.assertEqual(status.call_args.args[1][-2:],['-S','none'])
            with patch('vps.json_request',return_value=(1,{'server_id':'a'*64})), self.assertRaises(PermissionError):
                join.add_alias('lyon','Lyon','Lyon','lyon-worker','lyon-admin')

    def test_one_paste_enrolls_only_public_material_with_a_pinned_host(self):
        payload=self.join_payload()
        receipt={'enrolled':True,'member':'camille','server_id':'a'*64,'public_key':self.device['public_key']}
        with patch.object(join,'bounded_output',return_value=json.dumps(receipt).encode()) as ssh, patch.object(join,'accept_connect',return_value={'connected':True}) as accept:
            self.assertTrue(join.accept(join.encode('join',payload))['connected'])
        command=ssh.call_args.args[0]
        self.assertNotIn(payload['password'], ' '.join(command))
        self.assertIn('StrictHostKeyChecking=yes',command)
        self.assertIn('ClearAllForwardings=yes',command)
        self.assertEqual(json.loads(ssh.call_args.kwargs['input']),{'public_key':self.device['public_key']})
        accept.assert_called_once()
        self.assertEqual(accept.call_args.kwargs,{'expected_server':'a'*64})
        self.assertFalse(list(self.root.glob('.enroll-*')))

    def test_one_use_link_validates_before_network_and_resumes_lost_receipt(self):
        payload=self.join_payload()
        for change in [{'expires':1},{'user':'root'},{'member':'admin'},{'password':42},{'server_id':'invalid'}]:
            with self.subTest(change=change), patch.object(join,'bounded_output') as ssh, self.assertRaises(ValueError):
                join.accept(join.encode('join',{**payload,**change}))
            ssh.assert_not_called()
        with patch.object(join,'bounded_output',side_effect=RuntimeError('lost receipt')), patch.object(join,'accept_connect',return_value={'resumed':True}) as accept:
            self.assertTrue(join.accept(join.encode('join',payload))['resumed'])
        self.assertEqual(accept.call_args.kwargs['expected_server'],'a'*64)

    def test_onboarding_transport_enforces_real_output_and_time_bounds(self):
        self.assertEqual(join.bounded_output([sys.executable,'-c',"print('ok')"]),b'ok\n')
        with self.assertRaises(ValueError):join.bounded_output([sys.executable,'-c',"print('x'*2000)"],limit=1000)
        with self.assertRaises(TimeoutError):join.bounded_output([sys.executable,'-c','import time;time.sleep(10)'],timeout=.05)
