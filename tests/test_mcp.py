import json
import unittest
from unittest.mock import patch

from mcp import Client
import mcp_server


class MCPTests(unittest.IsolatedAsyncioTestCase):
    async def test_real_protocol_lists_tools_and_queries_restricted_gateway(self):
        with patch.object(mcp_server.vps, 'transport', return_value=(0, b'{"ok":true,"admit":true}')) as transport:
            async with Client(mcp_server.server) as client:
                tools = await client.list_tools()
                self.assertEqual({t.name for t in tools.tools}, {'vps_status', 'vps_run', 'vps_fetch', 'vps_release',
                    'admin_settings_get', 'admin_settings_preview', 'admin_settings_apply'})
                result = await client.call_tool('vps_status')
                self.assertFalse(result.is_error)
                self.assertTrue(result.structured_content['ok'])
                transport.assert_called_once()

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
        with patch.object(mcp_server.client_config, 'admin_host', side_effect=PermissionError('administrator required')), patch.object(mcp_server.vps, 'transport') as transport:
            async with Client(mcp_server.server) as client:
                result = await client.call_tool('admin_settings_get')
                self.assertTrue(result.is_error)
            transport.assert_not_called()
