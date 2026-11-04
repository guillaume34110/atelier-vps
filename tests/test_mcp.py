import json
from pathlib import Path
import subprocess
import tempfile
import unittest
from unittest.mock import patch

from mcp import Client
import mcp_server
from test_fleet import state


class MCPTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        folder = tempfile.TemporaryDirectory()
        self.addCleanup(folder.cleanup)
        self.patch_journal = patch.dict('os.environ', {'ATELIER_VPS_ERROR_DIR': folder.name})
        self.patch_journal.start()
        self.addCleanup(self.patch_journal.stop)

    def assert_text_error(self, result):
        self.assertFalse(result.is_error)
        self.assertEqual([item.type for item in result.content], ['text'])
        self.assertEqual(result.structured_content['ok'], False)
        self.assertEqual(result.structured_content['status'], 'error')
        self.assertEqual(json.loads(result.content[0].text), result.structured_content)
        self.assertTrue(result.structured_content['error'])

    async def test_real_protocol_lists_tools_and_queries_restricted_gateway(self):
        profile={'id':'test','name':'Test','team':'Test','ssh_host':'test-worker','enabled':True,'managed':False}
        with patch.object(mcp_server.vps, 'transport', return_value=(0, json.dumps(state()).encode())) as transport, patch.object(mcp_server.client_config,'active_connections',return_value=[profile]):
            async with Client(mcp_server.server) as client:
                tools = await client.list_tools()
                self.assertEqual({t.name for t in tools.tools}, {'vps_status', 'vps_cleanup', 'vps_run', 'vps_wait', 'vps_fetch', 'vps_release',
                    'vps_project_prepare', 'vps_project_run', 'vps_task', 'vps_errors', 'vps_preview',
                    'admin_settings_get', 'admin_settings_preview', 'admin_settings_apply', 'admin_invitation_create',
                    'vps_server_add','vps_connect_from_clipboard', 'vps_agent_desktop'})
                run = next(tool for tool in tools.tools if tool.name == 'vps_run')
                self.assertEqual(run.input_schema['properties']['arch']['default'], 'any')
                result = await client.call_tool('vps_status')
                self.assertFalse(result.is_error)
                self.assertTrue(result.structured_content['ok'])
                transport.assert_called_once()

    async def test_agent_selects_custom_timeout_above_ten_minutes(self):
        import project_tasks
        with patch.object(project_tasks, 'execute', return_value=(0, {'status': 'planned'})) as execute:
            async with Client(mcp_server.server) as client:
                tools = await client.list_tools()
                for name in ('vps_run', 'vps_task', 'vps_project_prepare', 'vps_project_run'):
                    schema = next(tool for tool in tools.tools if tool.name == name).input_schema['properties']['timeout']
                    self.assertEqual((schema['minimum'], schema['maximum']), (1, 3600))
                arguments = {'source': '/source', 'recipe': 'test', 'agent': 'Tests',
                             'disk_gib': 1, 'timeout': 2400, 'plan_only': True}
                result = await client.call_tool('vps_task', arguments)
                self.assertEqual(result.structured_content['status'], 'planned')
                execute.assert_called_once_with('/source', 'test', 'Tests', 1, 1, None, 2400, None, 'any', True)
                execute.reset_mock()
                for invalid in (0, 3601, True, 1.5):
                    result = await client.call_tool('vps_task', {**arguments, 'timeout': invalid})
                    self.assert_text_error(result)
                execute.assert_not_called()

    async def test_wait_forwards_reference_and_returns_completion(self):
        result = {'job_ref': 'preprod:' + 'a' * 32, 'status': 'passed', 'timed_out': False}
        with patch.object(mcp_server.vps, 'wait_for_job', return_value=result) as wait:
            async with Client(mcp_server.server) as client:
                response = await client.call_tool('vps_wait', {'job_id': result['job_ref'], 'timeout': 15})
                self.assertEqual(response.structured_content, result)
                wait.assert_called_once_with(result['job_ref'], 15, None)

    async def test_cleanup_reports_busy_without_treating_it_as_success(self):
        report = {'ok': False, 'servers': [{'server': 'preprod', 'status': 'busy'}]}
        with patch.object(mcp_server.vps, 'cleanup_caches', return_value=report) as cleanup:
            async with Client(mcp_server.server) as client:
                result = await client.call_tool('vps_cleanup', {'server': 'preprod'})
            self.assertEqual(result.structured_content, report)
            self.assertFalse(result.is_error)
            self.assertEqual([part.type for part in result.content], ['text'])
            cleanup.assert_called_once_with('preprod')

    async def test_recipe_tool_uses_the_common_project_router(self):
        import project_tasks
        with patch.object(project_tasks, 'execute', return_value=(0, {'status': 'planned'})) as execute:
            async with Client(mcp_server.server) as client:
                result = await client.call_tool('vps_task', {'source': '/source', 'recipe': 'test',
                    'agent': 'Tests', 'disk_gib': 1, 'plan_only': True})
                self.assertEqual(result.structured_content['status'], 'planned')
                execute.assert_called_once_with('/source', 'test', 'Tests', 1, 1, None, 900, None, 'any', True)

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
        with self.assertNoLogs('mcp.server.mcpserver.server', level='ERROR'), \
                patch.object(mcp_server.vps, 'transport') as transport:
            async with Client(mcp_server.server) as client:
                result = await client.call_tool('vps_release', {'job_id': 'a' * 32, 'results_preserved': False})
                self.assert_text_error(result)
                self.assertIn('preserve useful results', result.content[0].text)
                result = await client.call_tool('vps_run', {'source': '/nonexistent', 'argv': ['true'],
                                                          'agent': 'Tests', 'disk_gib': 1})
                self.assert_text_error(result)
                self.assertIn('existing source directory', result.content[0].text)
                transport.assert_not_called()

    async def test_admin_tools_require_separate_authenticated_alias(self):
        with patch.object(mcp_server.client_config, 'active_connections', return_value=[{'id':'test','ssh_host':'test-worker'}]), patch.object(mcp_server.vps, 'transport') as transport:
            async with Client(mcp_server.server) as client:
                result = await client.call_tool('admin_settings_get')
                self.assert_text_error(result)
            transport.assert_not_called()

    async def test_desktop_cannot_expose_tickets_or_stop_unconfirmed_work(self):
        with self.assertNoLogs('mcp.server.mcpserver.server', level='ERROR'), \
                patch.object(mcp_server.vps, 'transport') as transport:
            async with Client(mcp_server.server) as client:
                for action in ['ticket', 'stream', 'stop']:
                    result = await client.call_tool('vps_agent_desktop', {'action': action})
                    self.assert_text_error(result)
                    self.assertIn('use status/start', result.content[0].text)
            transport.assert_not_called()

    async def test_guarded_fetch_refusal_is_actionable_and_does_not_submit_a_job(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            helper = root / '.local/bin/workspace-budget.py'
            helper.parent.mkdir(parents=True); helper.touch()
            with patch.object(mcp_server.Path, 'home', return_value=root), \
                    patch.object(mcp_server.vps, 'job_target', return_value=({'id': 'test', 'ssh_host': 'test-worker'}, 'a' * 32)), \
                    patch.object(mcp_server.vps, 'fetch_plan', return_value={'disk_bytes': 1, 'archive_bytes': 1}), \
                    patch.object(mcp_server.subprocess, 'run', return_value=subprocess.CompletedProcess([], 1)), \
                    patch.object(mcp_server.vps, 'run_on_server') as submit, \
                    self.assertNoLogs('mcp.server.mcpserver.server', level='ERROR'):
                async with Client(mcp_server.server) as client:
                    result = await client.call_tool('vps_fetch', {'job_id': 'test:' + 'a' * 32,
                        'paths': ['receipt.json'], 'project': directory})
                submit.assert_not_called()
            self.assert_text_error(result)
            self.assertIn('output/task-tmp', result.content[0].text)
            self.assertIn('do not resubmit', result.content[0].text)

    async def test_unexpected_desktop_failure_keeps_private_details_out_of_response(self):
        with patch('desktop_client.request', side_effect=ValueError('private-worker-detail api_key=private-secret-value')), \
                self.assertNoLogs('mcp.server.mcpserver.server', level='ERROR'):
            async with Client(mcp_server.server) as client:
                result = await client.call_tool('vps_agent_desktop')
        self.assert_text_error(result)
        self.assertNotIn('private-worker-detail', result.content[0].text)
        self.assertNotIn('private-secret-value', result.content[0].text)
        events = mcp_server.error_journal.report()['events']
        self.assertEqual(len(events), 1)
        self.assertEqual(events[0]['id'], result.structured_content['diagnostic_id'])
        self.assertIn('private-worker-detail', events[0]['error'])
        self.assertNotIn('private-secret-value', events[0]['error'])

    async def test_argument_validation_and_unknown_tools_return_text_errors(self):
        with patch.object(mcp_server.vps, 'transport') as transport:
            async with Client(mcp_server.server) as client:
                for name, arguments in [('vps_run', {}), ('vps_wait', {'job_id': 'a' * 32, 'timeout': 'invalid'}),
                                        ('unknown_tool', {})]:
                    with self.subTest(tool=name):
                        result = await client.call_tool(name, arguments)
                        self.assert_text_error(result)
            transport.assert_not_called()

    async def test_timeouts_and_public_routing_errors_are_actionable_without_private_details(self):
        failures = [TimeoutError('private-worker-detail'),
                    subprocess.TimeoutExpired(['private-command'], 30),
                    mcp_server.vps.RoutingError('No compatible server: status_timeout')]
        with self.assertNoLogs('mcp.server.mcpserver.server', level='ERROR'):
            async with Client(mcp_server.server) as client:
                for failure in failures:
                    with self.subTest(error=type(failure).__name__), \
                            patch.object(mcp_server.vps, 'wait_for_job', side_effect=failure):
                        result = await client.call_tool('vps_wait', {'job_id': 'a' * 32})
                        self.assert_text_error(result)
                        self.assertNotIn('private-', result.content[0].text)
                        self.assertTrue('status_timeout' in result.content[0].text or 'do not resubmit' in result.content[0].text)

    async def test_failed_job_receipt_keeps_its_status_and_reference(self):
        receipt = {'job_ref': 'preprod:' + 'a' * 32, 'status': 'failed', 'returncode': 1}
        with patch.object(mcp_server.vps, 'wait_for_job', return_value=receipt) as wait:
            async with Client(mcp_server.server) as client:
                result = await client.call_tool('vps_wait', {'job_id': receipt['job_ref']})
            self.assertFalse(result.is_error)
            self.assertEqual(result.structured_content, receipt)
            self.assertEqual([item.type for item in result.content], ['text'])
            wait.assert_called_once_with(receipt['job_ref'], 3600, None)
