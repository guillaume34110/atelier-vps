import errno
import json
import io
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

import telemetry


class TelemetryTests(unittest.TestCase):
    def test_transfer_counts_real_bytes_and_closes_without_claiming_a_connection(self):
        with tempfile.TemporaryDirectory() as tmp, patch.object(telemetry, 'process_token', return_value='123:10'):
            root = Path(tmp)
            with telemetry.Transfer(root, io.BytesIO(b'actual source'), 'upload', 'a' * 32, 'Agent A', 'app') as transfer:
                self.assertEqual(transfer.read(6), b'actual')
                transfer.sample(force=True)
                active = telemetry.snapshot(root, False)['transfers']
                self.assertEqual(active[0]['status'], 'running')
                self.assertEqual(active[0]['bytes'], 6)
                self.assertEqual(active[0]['job'], 'a' * 32)
                self.assertEqual(transfer.read(), b' source')
            done = telemetry.history(root, 'transfers')[0]
            self.assertEqual(done['bytes'], 13)
            self.assertEqual(done['status'], 'passed')
            self.assertIsNotNone(done['ended'])
            self.assertNotIn('argv', done)
            stream = io.BytesIO()
            with self.assertRaises(RuntimeError):
                with telemetry.Transfer(root, stream, 'download', 'a' * 32, 'Agent A', 'app') as transfer:
                    self.assertEqual(transfer.write(b'result'), 6)
                    raise RuntimeError('broken transfer')
            self.assertEqual(telemetry.history(root, 'transfers')[0]['status'], 'failed')
            self.assertEqual(stream.getvalue(), b'result')

    def test_transfer_history_is_bounded_and_dead_connections_are_closed(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            for n in range(20):
                with telemetry.Transfer(root, io.BytesIO(), 'download', 'a' * 32, 'É' * 64, 'É' * 64):
                    pass
            self.assertEqual(len(telemetry.history(root, 'transfers')), 16)
            with telemetry.Transfer(root, io.BytesIO(), 'upload', 'a' * 32, 'Agent', 'app'):
                with patch.object(telemetry, 'process_token', return_value=None):
                    stale = telemetry.history(root, 'transfers')[0]
                    self.assertEqual(stale['status'], 'failed')
                    self.assertIsNotNone(stale['ended'])

    def test_transfer_telemetry_failure_preserves_the_actual_stream(self):
        with tempfile.TemporaryDirectory() as tmp, patch.object(telemetry, 'record_activity', return_value=False):
            stream = io.BytesIO()
            with telemetry.Transfer(Path(tmp), stream, 'download', 'a' * 32, 'Agent', 'app') as transfer:
                transfer.write(b'keep')
            self.assertEqual(stream.getvalue(), b'keep')

    def record(self, number, **values):
        return {'id': f'{number:032x}', 'agent': 'Tests navigateur', 'project': 'frontend',
                'status': 'running', 'started': 1000, 'runner': '123:10', **values}

    def test_activity_is_bounded_and_does_not_expose_commands_or_secrets(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            for n in range(20):
                self.assertTrue(telemetry.record_activity(root, self.record(n, argv=['TOKEN=private-value'], secret='private-value')))
            records = telemetry.history(root)
            self.assertEqual(len(records), 8)
            self.assertEqual(records[0]['id'], f'{19:032x}')
            self.assertNotIn('private-value', (root / '.activity.json').read_text())
            telemetry.record_activity(root, self.record(19, status='failed', ended=1020))
            self.assertEqual(len(telemetry.history(root)), 8)
            self.assertEqual(telemetry.history(root)[0]['status'], 'failed')

    def test_invalid_or_oversized_metadata_is_ignored(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            for raw in ['broken', '{}', '[]' + ' ' * telemetry.ACTIVITY_LIMIT]:
                (root / '.activity.json').write_text(raw)
                self.assertEqual(telemetry.history(root), [])
            self.assertFalse(telemetry.record_activity(root, self.record(1, started=float('nan'))))
            self.assertFalse(telemetry.record_activity(root, self.record(1, id='../private')))
            label = telemetry.public_label('A\n\x00' + 'x' * 100, 'anonymous')
            self.assertLessEqual(len(label), 64)
            self.assertTrue(label.isprintable())

    def test_metadata_failure_does_not_replace_the_calculation_result(self):
        with tempfile.TemporaryDirectory() as tmp, patch.object(telemetry.tempfile, 'mkstemp', side_effect=OSError(errno.EDQUOT, 'full')):
            state = self.record(1, status='passed', ended=1001)
            self.assertFalse(telemetry.record_activity(Path(tmp), state))
            self.assertEqual(state['status'], 'passed')
            self.assertEqual([p.name for p in Path(tmp).iterdir()], ['.activity.lock'])

    def test_linux_and_cgroup_v2_counters_are_real_cumulative_samples(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp); proc = root / 'proc'; cg = root / 'cg'
            proc.mkdir(); cg.mkdir()
            (proc / 'stat').write_text('cpu 100 2 30 400 8 1 2 3 999 999\n')
            (proc / 'meminfo').write_text('MemTotal: 8388608 kB\n')
            (cg / 'cpu.stat').write_text('usage_usec 12000\nuser_usec 8000\n')
            (cg / 'memory.current').write_text('123456')
            (cg / 'memory.max').write_text(str(5 * 1024**3))
            telemetry.record_activity(root, self.record(1))
            with patch.object(telemetry, 'PROC', proc), patch.object(telemetry, 'CGROUP', cg), patch.object(telemetry, 'process_token', return_value='123:10'):
                s = telemetry.snapshot(root, True)
                self.assertEqual(s['cpu_total'], 546)
                self.assertEqual(s['cpu_idle'], 408)
                self.assertEqual(s['host_total_ram'], 8 * 1024**3)
                self.assertEqual(s['worker_cpu_usec'], 12000)
                self.assertEqual(s['worker_ram'], 123456)
                self.assertEqual(s['active']['agent'], 'Tests navigateur')
                self.assertIsNone(telemetry.snapshot(root, False)['active'])

    def test_dead_or_reused_pid_is_not_shown_as_an_active_agent_during_maintenance(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            telemetry.record_activity(root, self.record(1))
            for token in [None, '123:11']:
                with patch.object(telemetry, 'process_token', return_value=token):
                    self.assertIsNone(telemetry.snapshot(root, True)['active'])

    def test_process_identity_checks_start_time_and_rejects_zombies(self):
        with tempfile.TemporaryDirectory() as tmp:
            proc = Path(tmp); (proc / '123').mkdir()
            stat = proc / '123/stat'
            stat.write_text('123 (python with spaces) S ' + ' '.join(['0'] * 18 + ['42']))
            with patch.object(telemetry, 'PROC', proc):
                self.assertEqual(telemetry.process_token(123), '123:42')
                stat.write_text(stat.read_text().replace(') S ', ') Z '))
                self.assertIsNone(telemetry.process_token(123))

    def test_cgroup_v1_fallback_and_missing_metrics_preserve_activity(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp); cg = root / 'cg'
            (cg / 'cpuacct').mkdir(parents=True); (cg / 'memory').mkdir()
            (cg / 'cpuacct/cpuacct.usage').write_text('123000')
            (cg / 'memory/memory.usage_in_bytes').write_text('456')
            telemetry.record_activity(root, self.record(1, status='failed', ended=1001))
            with patch.object(telemetry, 'PROC', root / 'missing'), patch.object(telemetry, 'CGROUP', cg):
                s = telemetry.snapshot(root, False)
            self.assertEqual(s['worker_cpu_usec'], 123)
            self.assertEqual(s['worker_ram'], 456)
            self.assertNotIn('cpu_total', s)
            self.assertEqual(s['activity'][0]['status'], 'failed')


if __name__ == '__main__':
    unittest.main()
