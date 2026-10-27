import base64
import copy
import json
from pathlib import Path
from types import SimpleNamespace
import unittest
from unittest.mock import Mock, patch

from ops import coolify_cloud as cloud, team, team_settings as settings
KEY = 'ssh-ed25519 ' + base64.b64encode(b'\x00\x00\x00\x0bssh-ed25519\x00\x00\x00\x20' + b'x'*32).decode()

GIB = 1024**3
CONFIG = {'token': 'private-test-credential', 'base_application': 'base12345678', 'approved_revision': 'a'*40,
          'server_uuid': 'server123456', 'project_uuid': 'project12345', 'environment_uuid': 'env123456789', 'network_pool': '10.254.0.0/16'}
OWNER = {'uuid': CONFIG['base_application'], 'git_repository': 'guillaume34110/atelier-vps', 'git_branch': 'main',
         'git_commit_sha': 'a'*40, 'server_id': 7, 'environment_id': 8, 'settings': {'is_auto_deploy_enabled': False,
         'is_preview_deployments_enabled': False, 'is_raw_compose_deployment_enabled': True}}
BASE = {'container': 'ateliervps-worker', 'pids_limit': 768}
REQUEST = {'action': 'configure', 'tenant': 'steph', 'base_application': CONFIG['base_application'],
           'application': 'service12345', 'workspace': '/mnt/atelier-vps/.usevps-users/steph',
           'mount': '/mnt/atelier-vps', 'volume_id': 'volume-id', 'mode': 'allocated', 'host_ram_reserve_bytes': 8*GIB,
           'cpu_set': '3,4,5',
           'limits': {'cpus': 3, 'ram_bytes': 22*GIB, 'cpu_shares': 64, 'slots': 2}}


class CloudTeamTests(unittest.TestCase):
    def test_cloud_credential_file_is_private_root_owned_and_has_a_real_private_network_pool(self):
        path = Mock(); path.is_symlink.return_value=False
        path.lstat.return_value=SimpleNamespace(st_uid=0,st_mode=0o100600,st_size=500)
        path.read_text.return_value=json.dumps(CONFIG)
        self.assertEqual(cloud.configuration(path),CONFIG)
        for owner,mode,size in [(1,0o100600,500),(0,0o100640,500),(0,0o100600,9000)]:
            path.lstat.return_value=SimpleNamespace(st_uid=owner,st_mode=mode,st_size=size)
            with self.assertRaises(ValueError):cloud.configuration(path)
        path.lstat.return_value=SimpleNamespace(st_uid=0,st_mode=0o100600,st_size=500)
        path.read_text.return_value=json.dumps({**CONFIG,'network_pool':'127.0.0.0/16'})
        with self.assertRaises(ValueError):cloud.configuration(path)

    def adapter(self):
        command = Mock(return_value=json.dumps([{'Image': 'sha256:'+'b'*64}]))
        result = cloud.CloudTeam(dict(CONFIG), dict(BASE), command, Mock())
        member = {'uuid': 'service12345', 'description': result.marker('steph'), 'server_id': 7, 'environment_id': 8}
        def api(path, body=None, method=None):
            if path == 'applications/'+CONFIG['base_application']: return copy.deepcopy(OWNER)
            if path == 'services': return [] if body is None else {'uuid': member['uuid']}
            if path == 'services/'+member['uuid'] and body is None: return dict(member)
            return {'ok': True}
        result.api = Mock(side_effect=api)
        return result, member

    def test_member_manifest_keeps_hard_limits_private_mounts_and_never_pulls(self):
        worker = cloud.compose(REQUEST, 'sha256:'+'b'*64, 'service12345', 768)['services']['worker']
        self.assertEqual(worker['image'], 'sha256:'+'b'*64)
        self.assertEqual(worker['pull_policy'], 'never')
        self.assertEqual(worker['mem_limit'], worker['memswap_limit'])
        self.assertEqual(worker['mem_limit'], 22*GIB)
        self.assertEqual((worker['cpu_quota'], worker['cpu_period'], worker['cpu_shares']), (300000, 100000, 64))
        self.assertEqual(worker['network_mode'], 'service12345')
        self.assertEqual(worker['cpuset'], '3,4,5')
        self.assertEqual(len(worker['volumes']), 9)
        self.assertEqual(worker['volumes'][0], REQUEST['workspace']+':/workspace')
        self.assertTrue(worker['read_only'])
        self.assertEqual(worker['user'], '10001:10001')
        self.assertEqual(worker['pids_limit'], 768)
        self.assertEqual(worker['tmpfs'], ['/tmp:'+cloud.DESKTOP_TMPFS])
        self.assertEqual(worker['cap_drop'], ['ALL'])
        self.assertNotIn('ports', worker)
        self.assertEqual(worker['environment']['USEVPS_HOST_RAM_RESERVE_GIB'], '8.0')
        self.assertEqual(worker['environment']['USEVPS_MAX_CPUS'], '3')
        for image, network in [('latest', 'service12345'), ('sha256:'+'b'*64, 'host')]:
            with self.assertRaises(ValueError): cloud.compose(REQUEST, image, network)

    def test_configure_member_prepares_private_network_before_sending_compose(self):
        adapter, _ = self.adapter()
        result = adapter.run(dict(REQUEST))
        self.assertTrue(result['configured'])
        adapter.prepare_network.assert_called_once_with('steph', 'service12345')
        call = adapter.api.call_args
        self.assertEqual(call.args[0], 'services/service12345')
        self.assertEqual(call.args[2], 'PATCH')
        body = call.args[1]
        self.assertFalse(body['instant_deploy'])
        self.assertNotIn('connect_to_docker_network', body)
        raw = json.loads(base64.b64decode(body['docker_compose_raw']))
        self.assertEqual(raw['services']['worker']['network_mode'], 'service12345')

    def test_create_uses_legacy_cloud_fields_and_leaves_the_service_stopped(self):
        adapter, member = self.adapter()
        adapter.run({'action':'create','tenant':'steph','base_application':CONFIG['base_application']})
        body = next(call.args[1] for call in adapter.api.call_args_list
                    if call.args[0]=='services' and len(call.args)>1)
        self.assertNotIn('connect_to_docker_network', body)
        self.assertFalse(body['instant_deploy'])
        raw = json.loads(base64.b64decode(body['docker_compose_raw']))
        self.assertEqual(raw['services']['worker']['network_mode'], 'none')
        self.assertEqual(raw['services']['worker']['command'], ['false'])
        member['connect_to_docker_network'] = True
        with self.assertRaisesRegex(ValueError, 'unrelated'):
            adapter.run(dict(REQUEST))

    def test_owner_stays_git_pinned_and_sets_the_existing_private_owner_manifest(self):
        adapter, _ = self.adapter()
        adapter.run({**REQUEST, 'tenant': 'owner', 'application': CONFIG['base_application']})
        adapter.prepare_network.assert_called_once_with('owner', 'atelier-vps-owner')
        calls = [call for call in adapter.api.call_args_list if len(call.args)>1]
        self.assertEqual(calls[0].args[1], {'docker_compose_location': '/docker-compose.coolify-team.yaml'})
        env = {row['key']: row['value'] for row in calls[1].args[1]['data'] if not row['is_preview']}
        self.assertEqual((env['USEVPS_MAX_CPUS'], env['USEVPS_CPU_QUOTA']), ('3', '300000'))
        self.assertEqual(env['USEVPS_HOST_RAM_RESERVE_GIB'], '8.0')
        self.assertEqual(env['USEVPS_MODE'], 'allocated')

    def test_unrelated_service_or_owner_resource_cannot_be_changed(self):
        adapter, member = self.adapter()
        member['server_id'] = 123
        with self.assertRaisesRegex(ValueError, 'unrelated'): adapter.run(dict(REQUEST))
        self.assertFalse(adapter.prepare_network.called)
        self.assertTrue(all(len(call.args)==1 for call in adapter.api.call_args_list))
        adapter, _ = self.adapter()
        with self.assertRaisesRegex(ValueError, 'identity differ'):
            adapter.run({**REQUEST, 'application': CONFIG['base_application']})

    def test_create_is_durable_without_starting_an_unconfigured_worker(self):
        adapter, _ = self.adapter()
        result = adapter.run({'action': 'create', 'tenant': 'steph', 'base_application': CONFIG['base_application']})
        self.assertEqual(result, {'id':'service12345','container':'worker-service12345','network':'service12345'})
        create = next(call for call in adapter.api.call_args_list if call.args[0]=='services' and len(call.args)>1)
        self.assertFalse(create.args[1]['instant_deploy'])
        raw = json.loads(base64.b64decode(create.args[1]['docker_compose_raw']))
        self.assertEqual(raw['services']['worker']['command'], ['false'])
        self.assertFalse(any('/start' in call.args[0] for call in adapter.api.call_args_list))

    def test_existing_managed_service_is_reused_after_unknown_create_outcome(self):
        adapter, member = self.adapter(); original = adapter.api.side_effect
        adapter.api.side_effect = lambda path, body=None, method=None: [member] if path=='services' and body is None else original(path,body,method)
        result = adapter.run({'action':'create','tenant':'steph','base_application':CONFIG['base_application']})
        self.assertEqual(result['id'], member['uuid'])
        self.assertTrue(all(len(call.args)==1 for call in adapter.api.call_args_list))

    def test_unapproved_source_blocks_every_mutation(self):
        adapter, _ = self.adapter()
        adapter.config['approved_revision'] = 'c'*40
        with self.assertRaisesRegex(ValueError, 'approved pinned'): adapter.run(dict(REQUEST))
        self.assertTrue(all(len(call.args)==1 for call in adapter.api.call_args_list))

    def test_owner_server_uses_the_verified_destination_when_direct_server_id_is_null(self):
        adapter, _ = self.adapter()
        owner = copy.deepcopy(OWNER)
        owner.update(server_id=None, destination={'server_id':7,'server':{'uuid':CONFIG['server_uuid']}})
        adapter.api.return_value=owner; adapter.api.side_effect=None
        self.assertEqual(adapter.owner(CONFIG['base_application'])['server_id'],7)
        owner['destination']['server']['uuid']='other-server'
        with self.assertRaisesRegex(ValueError, 'another server'):adapter.owner(CONFIG['base_application'])

    def test_private_subnet_avoids_routes_and_other_projects_and_handles_null_ipam(self):
        def command(args):
            if args[:3]==['docker','network','ls']:return 'net1 net2'
            if args[:3]==['docker','network','inspect']:
                return json.dumps([{'IPAM':{'Config':None}}, {'IPAM':{'Config':[{'Subnet':'10.254.0.0/28'}]}}])
            return json.dumps([{'dst':'default'}, {'dst':'10.254.0.16/28'}])
        self.assertEqual(cloud.private_subnet(CONFIG, command), '10.254.0.32/28')


class LargerHostSettings(unittest.TestCase):
    def test_two_integer_allocations_use_distinct_cores_and_leave_host_headroom(self):
        policy = {'mode': 'allocated', 'users':[{'id':'owner'},{'id':'steph'}]}
        limits = {'owner':{'cpus':3,'enabled':True},'steph':{'cpus':3,'enabled':True}}
        masks = team.cpu_sets(policy, limits, list(range(8)))
        self.assertEqual(masks, {'owner':'0,1,2','steph':'3,4,5'})
        policy['mode'] = 'opportunistic'
        self.assertEqual(team.cpu_sets(policy, limits, list(range(8))), {})

    def test_fractional_quotas_use_real_host_ids_without_repeating_a_core_in_one_worker(self):
        policy = {'mode':'allocated', 'users':[{'id':str(n)} for n in range(4)]}
        limits = {str(n):{'cpus':1.5,'enabled':True} for n in range(4)}
        masks = team.cpu_sets(policy, limits, [2,4,6,8,10,12,14,16])
        self.assertEqual(masks, {'0':'2,4','1':'6,8','2':'10,12','3':'14,16'})
    def test_initialization_preserves_the_live_six_cpu_48_gib_owner_and_eight_gib_floor(self):
        state = {}
        current = {'ok': True, 'scope':{'id':'owner','mode':'opportunistic'}, 'pool':{
            'cpu_total':6,'max_jobs':4,'ram_accounting':{'worker_limit_bytes':48*GIB,'host_floor_bytes':8*GIB}}}
        base = {'mount':'/volume','container':'owner','workspace':'/volume/usevps'}
        def save(path, value):state.update(value)
        with patch.object(settings, 'STATE', Path('/nonexistent-atelier-test-state')), patch.object(settings, 'read', side_effect=lambda path: base if path==settings.BASE else state), \
             patch.object(settings, 'save', side_effect=save), patch.object(settings, 'verify_mount'), \
             patch.object(settings, 'quota_usage', return_value={34110:{'limit':76*GIB}}), \
             patch.object(settings, 'command', return_value=json.dumps(current)), patch.object(settings, 'identity'), \
             patch.object(Path, 'read_text', return_value='restrict,command="fixed" '+KEY):
            settings.initialize(CONFIG['base_application'], '/fixture/admin.pub')
        self.assertEqual(state['capacity'], {'cpus':6,'ram_bytes':48*GIB,'disk_bytes':76*GIB,'slots':4})
        self.assertEqual(state['tenants']['owner']['ram_bytes'],48*GIB)
        self.assertEqual(state['host_ram_reserve_bytes'],8*GIB)

    def test_ram_preview_uses_this_hosts_floor_without_double_counting_reclaimable_cache(self):
        with patch.object(Path, 'read_text', return_value='MemAvailable: '+str(52*GIB//1024)+' kB\n'):
            self.assertEqual(settings.allocatable_ram({'tenants':{},'host_ram_reserve_bytes':8*GIB}),44*GIB)
            self.assertEqual(settings.allocatable_ram({'tenants':{}}),51*GIB)
