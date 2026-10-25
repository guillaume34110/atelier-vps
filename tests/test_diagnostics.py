import hashlib
import io
import json
from pathlib import Path
import tarfile
import tempfile
import unittest
from unittest.mock import patch

import diagnostics
import telemetry
import worker


class DiagnosticsTests(unittest.TestCase):
    def state(self, number=1, **values):
        state = dict(id=f'{number:032x}', status='failed', agent='Tests privés', project='app',
                    started=100, ended=110, reason='task memory limit', returncode=-15,
                    reservation={'ram_bytes': 128 * 1024**2}, timeout=30,
                    resources={'ram_peak_bytes': 160 * 1024**2})
        state.update(values)
        return state

    def test_failure_categories_do_not_guess_oom_from_a_signal_alone(self):
        with tempfile.TemporaryDirectory() as tmp:
            for reason, code, expected in [('disk limit', 1, 'disk'), ('timeout', -15, 'timeout'),
                    ('host memory pressure', -15, 'host_memory'), ('log limit', -15, 'logs'),
                    ('worker restarted', 1, 'restart'), (None, 7, 'command'), (None, -9, 'signal')]:
                report = diagnostics.build(Path(tmp), self.state(reason=reason, returncode=code))
                self.assertEqual(report['kind'], expected)

    def test_real_failure_detail_explains_measured_limit_and_redacts_credentials(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp)
            (path / 'stdout.log').write_text('AssertionError: expected 42\nTOKEN=private-marker\nAuthorization: Bearer hidden-bearer\nAPI_KEY="hidden-key"\nhttps://user:hidden-url@example.test\n\x1b[31mfailed\x1b[0m')
            report = diagnostics.build(path, self.state())
            self.assertEqual(report['kind'], 'memory')
            self.assertEqual(report['resources']['ram_peak_bytes'], 160 * 1024**2)
            self.assertEqual(report['resources']['reserved_ram_bytes'], 128 * 1024**2)
            self.assertIn('AssertionError: expected 42', report['log_tail'])
            for secret in ['private-marker', 'hidden-bearer', 'hidden-key', 'hidden-url', '\x1b']:
                self.assertNotIn(secret, json.dumps(report))

    def test_authenticated_fetch_retains_card_after_explicit_job_release(self):
        with tempfile.TemporaryDirectory() as tmp, patch.object(worker, 'ROOT', Path(tmp)):
            root = Path(tmp); state = self.state(); job = root / 'jobs' / state['id']; job.mkdir(parents=True)
            (job / 'stdout.log').write_text('Private failure detail for owner only')
            worker.receipt(job, state)
            worker.release_job({'id': state['id']})
            self.assertFalse(job.exists())
            with self.assertRaises(ValueError):
                worker.fetch({'id': state['id'], 'paths': ['diagnostic.json', 'stdout.log']}, io.BytesIO())
            output = io.BytesIO()
            worker.fetch({'id': state['id'], 'paths': ['diagnostic.json']}, output)
            with tarfile.open(fileobj=io.BytesIO(output.getvalue())) as archive:
                manifest = json.load(archive.extractfile('manifest.json'))
                body = archive.extractfile('diagnostic.json').read()
            self.assertEqual(manifest['diagnostic.json']['sha256'], hashlib.sha256(body).hexdigest())
            self.assertEqual(json.loads(body)['id'], state['id'])
            self.assertNotIn('Private failure detail', json.dumps(telemetry.snapshot(root, False)))

    def test_only_eight_derived_cards_are_retained_original_job_files_remain(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            for number in range(20):
                job = root / 'jobs' / f'{number:032x}'; job.mkdir(parents=True)
                (job / 'stdout.log').write_text('x' * 12000 + '\nUseful final failure')
                diagnostics.remember(root, job, self.state(number))
            self.assertEqual(len(diagnostics.reports(root)), 8)
            self.assertIn(f'{19:032x}', diagnostics.reports(root))
            self.assertTrue((root / 'jobs' / f'{0:032x}' / 'stdout.log').exists())
            for report in diagnostics.reports(root).values():
                self.assertLessEqual(len(report['log_tail']), 4096)
                self.assertIn('Useful final failure', report['log_tail'])

    def test_old_receipt_fallback_and_optional_metadata_failures_do_not_change_result(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp); state = self.state(); job = root / 'jobs' / state['id']; job.mkdir(parents=True)
            (job / 'receipt.json').write_text(json.dumps(state))
            (root / '.failure-details.json').write_text('[]')
            self.assertEqual(diagnostics.get(root, job)['kind'], 'memory')
            with patch.object(diagnostics.resources, 'write_json', side_effect=OSError('quota full')):
                diagnostics.remember(root, job, state)
            self.assertEqual(state['status'], 'failed')
            self.assertEqual(json.loads((job / 'receipt.json').read_text()), state)
            (root / '.failure-details.json').unlink()
            (root / '.failure-details.json').symlink_to(job / 'receipt.json')
            self.assertEqual(diagnostics.reports(root), {})
            with self.assertRaises(FileNotFoundError):
                diagnostics.get(root, root / 'jobs' / ('a' * 32))
