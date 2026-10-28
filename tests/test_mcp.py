import json
import unittest
from unittest.mock import patch

from mcp import Client
import mcp_server
from test_fleet import state


class MCPTests(unittest.IsolatedAsyncioTestCase):
    async def test_real_protocol_lists_tools_and_queries_restricted_gateway(self):
        profile={'id':'test','name':'Test','team':'Test','ssh_host':'test-worker','enabled':True,'managed':False}
        with patch.object(mcp_server.vps, 'transport', return_value=(0, json.dumps(state()).encode())) as transport, patch.object(mcp_server.client_config,'active_connections',return_value=[profile]):
            async with Client(mcp_server.server) as client:
                tools = await client.list_tools()
                self.assertEqual({t.name for t in tools.tools}, {'vps_status', 'vps_run', 'vps_wait', 'vps_fetch', 'vps_release',
                    'vps_project_prepare', 'vps_project_run',
                    'admin_settings_get', 'admin_settings_preview', 'admin_settings_apply', 'admin_invitation_create',
                    'vps_server_add','vps_connect_from_clipboard', 'vps_agent_desktop'})
                run = next(tool for tool in tools.tools if tool.name == 'vps_run')
                self.assertEqual(run.input_schema['properties']['arch']['default'], 'any')
                result = await client.call_tool('vps_status')
                self.assertFalse(result.is_error)
                self.assertTrue(result.structured_content['ok'])
                transport.assert_called_once()

    async def test_wait_forwards_reference_and_returns_completion(self):
        result = {'job_ref': 'preprod:' + 'a' * 32, 'status': 'passed', 'timed_out': False}
        with patch.object(mcp_server.vps, 'wait_for_job', return_value=result) as wait:
            async with Client(mcp_server.server) as client:
                response = await client.call_tool('vps_wait', {'job_id': result['job_ref'], 'timeout': 15})
                self.assertEqual(response.structured_content, result)
                wait.assert_called_once_with(result['job_ref'], 15, None)

    async def test_project_tools_forward_stored_reference(self):
        reference = 'preprod:demo:' + 'a' * 64
        with patch.object(mcp_server.vps, 'prepare_project', return_value=(0, {'project_ref': reference})) as prepare, \
                patch.object(mcp_server.vps, 'run_project', return_value=(0, {'status': 'passed'})) as run:
            async with Client(mcp_server.server) as client:
                result = await client.call_tool('vps_project_prepare', {'source': '/source', 'project_id': 'demo', 'disk_gib': 1})
                self.assertEqual(result.structured_content['project_ref'], reference)
                result = await client.call_tool('vps_project_run', {'project_ref': reference, 'argv': ['true'],
                                                                     'agent': 'Tests', 'disk_gib': 1})
                self.assertEqual(result.structured_content['status'], 'passed')
                prepare.assert_called_once()
                run.assert_called_once()

    async def test_release_requires_preserved_results_and_invalid_run_is_rejected(self):
        with patch.object(mcp_server.vps, 'transport') as transport:
            async with Client(mcp_server.server) as client:
                result = await client.call_tool('vps_release', {'job_id': 'a' * 32, 'results_preserved': False})
                self.assertTrue(result.is_error)
                result = await client.call_tool('vps_run', {'source': '/nonexistent', 'argv': ['true'],
                                                          'agent': 'Tests', 'disk_gib': 1})
                self.assertTrue(result.is_error)
                transport.assert_not_called()

    async def test_admin_tools_require_separate_authenticated_alias(self):
        with patch.object(mcp_server.client_config, 'active_connections', return_value=[{'id':'test','ssh_host':'test-worker'}]), patch.object(mcp_server.vps, 'transport') as transport:
            async with Client(mcp_server.server) as client:
                result = await client.call_tool('admin_settings_get')
                self.assertTrue(result.is_error)
            transport.assert_not_called()

    async def test_desktop_cannot_expose_tickets_or_stop_unconfirmed_work(self):
        with patch.object(mcp_server.vps, 'transport') as transport:
            async with Client(mcp_server.server) as client:
                for action in ['ticket', 'stream', 'stop']:
                    result = await client.call_tool('vps_agent_desktop', {'action': action})
                    self.assertTrue(result.is_error)
                for ram in [0, -1, 6]:
                    result = await client.call_tool('vps_agent_desktop', {'action': 'start', 'ram_gib': ram})
                    self.assertTrue(result.is_error)
            transport.assert_not_called()
