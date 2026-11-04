import base64
from concurrent.futures import ThreadPoolExecutor
import io
import json
import os
from pathlib import Path
import struct
import subprocess
import sys
import tarfile
import tempfile
import threading
import time
import unittest
from unittest.mock import patch
import zlib

from mcp import Client
import error_journal
import mcp_server
import resources
import vps
import worker


def png():
    def chunk(kind, data):
        return struct.pack('>I', len(data)) + kind + data + struct.pack('>I', zlib.crc32(kind + data))
    return b'\x89PNG\r\n\x1a\n' + chunk(b'IHDR', struct.pack('>IIBBBBB', 1, 1, 8, 2, 0, 0, 0)) + chunk(b'IDAT', zlib.compress(b'\x00\x12\x34\x56')) + chunk(b'IEND', b'')


class Journal(unittest.TestCase):
    def test_concurrent_redacted_events_and_rotation_preserve_totals_and_originals(self):
        with tempfile.TemporaryDirectory() as temp, patch.dict(os.environ, {'ATELIER_VPS_ERROR_DIR': temp}), patch.object(error_journal, 'LIMIT', 1024), patch.object(error_journal, 'COPIES', 2):
            root = Path(temp); original = root / 'original-receipt.json'; original.write_text('preserve')
            def write(_):
                return error_journal.record('tool.vps_run', 'api_key=private-value Bearer secret-value https://example.test/?code=human-code', job_ref='default:' + 'a' * 32)
            with ThreadPoolExecutor(max_workers=4) as pool:
                self.assertTrue(all(pool.map(write, range(24))))
            report = error_journal.report()
            self.assertEqual(sum(x['count'] for x in report['lifetime_totals'].values()), 24)
            self.assertLess(report['count'], 24)
            self.assertEqual(original.read_text(), 'preserve')
            for p in root.glob('events.jsonl*'):
                raw = p.read_text()
                for secret in ['private-value', 'secret-value', 'human-code']:
                    self.assertNotIn(secret, raw)
                self.assertEqual(p.stat().st_mode & 0o777, 0o600)

    def test_unsafe_logging_never_changes_the_operation(self):
        with tempfile.TemporaryDirectory() as temp, patch.dict(os.environ, {'ATELIER_VPS_ERROR_DIR': temp}):
            p = Path(temp); target = p / 'target'; target.write_text('untouched'); (p / 'events.jsonl').symlink_to(target)
            self.assertIsNone(error_journal.record('tool.vps_run', 'failure'))
            self.assertEqual(target.read_text(), 'untouched')


class Preview(unittest.IsolatedAsyncioTestCase):
    async def test_real_protocol_delivers_png_and_reports_bad_paths_as_text_only(self):
        with tempfile.TemporaryDirectory() as temp, tempfile.TemporaryDirectory() as logs, patch.dict(os.environ, {'ATELIER_VPS_ERROR_DIR': logs}):
            root = Path(temp); image = root / 'good.png'; image.write_bytes(png())
            bad = root / 'bad.png'; bad.write_bytes(png()[:-4] + b'bad!')
            link = root / 'linked.png'; link.symlink_to(image)
            async with Client(mcp_server.server) as client:
                result = await client.call_tool('vps_preview', {'project': temp, 'path': 'good.png'})
                self.assertFalse(result.is_error)
                self.assertEqual([x.type for x in result.content], ['image'])
                self.assertEqual(base64.b64decode(result.content[0].data), png())
                for name in ['missing.png', 'bad.png', 'linked.png', '../outside.png', '.ssh/key.png']:
                    result = await client.call_tool('vps_preview', {'project': temp, 'path': name})
                    self.assertFalse(result.is_error)
                    self.assertEqual([x.type for x in result.content], ['text'])
                    self.assertFalse(result.structured_content['ok'])
                    self.assertTrue(result.structured_content['diagnostic_id'])
                errors = await client.call_tool('vps_errors')
                self.assertEqual(errors.structured_content['count'], 5)


class Streaming(unittest.TestCase):
    def test_duplicate_identity_cannot_replace_an_active_reservation(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp); (root / '.leases').mkdir(); ident = 'a' * 32
            old = root / '.leases' / (ident + '.json'); old.write_text('original admitted reservation')
            values = {'cpus': 1, 'ram_bytes': 64 * 1024**2, 'disk_bytes': 1024, 'ram_source': 'explicit'}
            with patch.object(resources, 'requested', return_value=values), patch.object(resources, 'cpu_capacity', return_value=1), patch.object(resources, 'memory_capacity', return_value=1024**3):
                with self.assertRaisesRegex(ValueError, 'identity already exists'):
                    with resources.reserve(root, ident, {}, lambda: self.fail('duplicate must not reach admission')):
                        self.fail('duplicate reservation admitted')
            self.assertEqual(old.read_text(), 'original admitted reservation')

    def test_upload_drains_ssh_errors_concurrently_and_compresses_complete_source(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp); (root / 'source.bin').write_bytes(os.urandom(256 * 1024))
            real = subprocess.Popen
            code = "import sys,json,tarfile,io; q=json.loads(sys.stdin.buffer.readline()); sys.stderr.write('x'*131072); sys.stderr.flush(); raw=sys.stdin.buffer.read(); t=tarfile.open(fileobj=io.BytesIO(raw)); names=t.getnames(); print(json.dumps({'gzip':raw[:2].hex(),'names':names}))"
            def spawn(_, **kwargs):
                return real([sys.executable, '-c', code], **kwargs)
            with patch.object(vps.subprocess, 'Popen', side_effect=spawn), patch.object(vps.shutil, 'which', return_value=None), patch.object(vps.client_config, 'ssh_options', return_value=[]):
                rc, raw = vps.transport('test', 'run', {'disk_bytes': 1024**2, 'source_complete': True}, source=root, timeout=5)
            value = json.loads(raw)
            self.assertEqual(rc, 0)
            self.assertEqual(value['gzip'], '1f8b')
            self.assertEqual(value['names'], ['source.bin', '.usevps-source-complete'])

    def test_upload_deadline_covers_a_receiver_that_never_reads(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp); (root / 'source.bin').write_bytes(os.urandom(256 * 1024)); real = subprocess.Popen; spawned = []
            def spawn(_, **kwargs):
                p = real([sys.executable, '-c', 'import time;time.sleep(30)'], **kwargs); spawned.append(p); return p
            started = time.monotonic()
            with patch.object(vps.subprocess, 'Popen', side_effect=spawn), patch.object(vps.shutil, 'which', return_value=None), patch.object(vps.client_config, 'ssh_options', return_value=[]):
                with self.assertRaises(TimeoutError):
                    vps.transport('test', 'run', {'disk_bytes': 1024**2}, source=root, timeout=.2)
            self.assertLess(time.monotonic() - started, 3)
            self.assertIsNotNone(spawned[0].poll())
            self.assertFalse(any(t.name == 'atelier-source-transfer' and t.is_alive() for t in threading.enumerate()))

    def test_missing_source_completion_is_rejected_before_execution(self):
        for complete, after in [(False, False), (True, False), (True, True)]:
            archive = io.BytesIO()
            with tarfile.open(fileobj=archive, mode='w:gz') as tar:
                tar.addfile(tarfile.TarInfo('source.txt'))
                if complete:
                    tar.addfile(tarfile.TarInfo('.usevps-source-complete'))
                if after:
                    tar.addfile(tarfile.TarInfo('late.txt'))
            archive.seek(0)
            with tempfile.TemporaryDirectory() as temp:
                if complete and not after:
                    worker.extract(archive, Path(temp), require_complete=True)
                    self.assertFalse((Path(temp) / '.usevps-source-complete').exists())
                else:
                    with self.assertRaises(ValueError):
                        worker.extract(archive, Path(temp), require_complete=True)

    def test_missing_receipt_has_an_actionable_refusal(self):
        with patch.object(vps, 'json_request', side_effect=[(0, {'capabilities': {'transfer_budgets': True}}), (1, {'ok': False, 'error': 'job not found or already released'})]):
            with self.assertRaisesRegex(vps.RoutingError, 'already released.*do not resubmit'):
                vps.fetch_plan('test', 'a' * 32, ['stdout.log'])
