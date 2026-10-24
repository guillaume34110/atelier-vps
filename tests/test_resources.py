from contextlib import ExitStack
import fcntl
import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

import resources as r
import worker


class ResourceTests(unittest.TestCase):
    def setUp(self):
        self.stack = ExitStack()
        self.root = Path(self.stack.enter_context(tempfile.TemporaryDirectory()))
        self.stack.enter_context(patch.object(r, 'cpu_capacity', return_value=4))
        self.stack.enter_context(patch.object(r, 'memory_capacity', return_value=4 * r.GIB))
        self.stack.enter_context(patch.object(r, 'process_token', side_effect=lambda pid: f'{pid}:1'))
        self.stack.enter_context(patch.dict(r.os.environ, {'USEVPS_QUEUE_WAIT_SECONDS': '0'}))
        self.addCleanup(self.stack.close)
        self.capacity = {'host_available_ram': 7 * r.GIB, 'workspace_free_bytes': 10 * r.GIB,
                         'free_bytes': 12 * r.GIB, 'reserve_bytes': r.GIB // 2}

    def claim(self, ident, **request):
        return r.reserve(self.root, ident * 32, {'cpus': 1, 'ram_bytes': r.GIB,
                                               'disk_bytes': r.GIB, **request}, lambda: self.capacity)

    def test_claims_are_atomic_and_unused_cpu_is_lent(self):
        with self.claim('a') as first, self.claim('b'):
            pool = r.view(self.root, self.capacity)
            self.assertEqual(pool['active_count'], 2)
            self.assertEqual(pool['cpu_available'], 2)
            self.assertEqual(pool['ram_reserved'], 2 * r.GIB)
            masks = r.allocations(pool['active'], [0, 1, 2, 3])
            self.assertEqual({*masks['a' * 32], *masks['b' * 32]}, {0, 1, 2, 3})
            self.assertFalse(set(masks['a' * 32]) & set(masks['b' * 32]))
            with self.assertRaises(RuntimeError), self.claim('c', cpus=3):
                pass
            with self.assertRaises(RuntimeError), self.claim('c', ram_bytes=3 * r.GIB):
                pass
            with self.assertRaises(RuntimeError), self.claim('c', disk_bytes=9 * r.GIB):
                pass
        self.assertEqual(r.view(self.root, self.capacity)['active_count'], 0)

    def test_shared_cache_growth_is_charged_once_and_workspace_is_independent(self):
        r.write_json(self.root / '.cache-accounting.json', 0)
        with self.claim('a') as first, self.claim('b') as second:
            first.update(20 * r.MIB, 100, [], 200)
            second.update(30 * r.MIB, 900, [], 200)
            entries = {x['id']: x for x in r.leases(self.root)}
            self.assertEqual(entries['a' * 32]['disk_used'], 200)
            self.assertEqual(entries['b' * 32]['disk_used'], 1000)
            self.assertEqual(sum(x['cache_bytes'] for x in entries.values()), 200)
            first.update(20 * r.MIB, 100, [], 100)
            second.update(30 * r.MIB, 900, [], 200)
            self.assertEqual(sum(x['cache_bytes'] for x in r.leases(self.root)), 200)
        self.assertFalse((self.root / '.cache-accounting.json').exists())

    def test_dead_supervisor_blocks_admission_without_releasing_unknown_children(self):
        with self.claim('a'):
            with patch.object(r, 'process_token', return_value=None):
                pool = r.view(self.root, self.capacity)
                self.assertTrue(pool['active'][0]['orphaned'])
                self.assertFalse(r.fits(pool, 1, r.GIB, r.GIB))

    def test_maintenance_excludes_jobs_but_completed_cleanup_can_overlap_jobs(self):
        with (self.root / '.queue.lock').open('a') as lock:
            fcntl.flock(lock, fcntl.LOCK_EX)
            with self.assertRaises(RuntimeError), self.claim('a'):
                pass
        path = self.root / 'jobs' / ('b' * 32)
        path.mkdir(parents=True)
        (path / 'receipt.json').write_text(json.dumps({'status': 'passed'}))
        with self.claim('a'), patch.object(worker, 'ROOT', self.root):
            self.assertEqual(worker.release_job({'id': path.name}), {'released': path.name})
            self.assertFalse(path.exists())
            self.assertEqual(r.view(self.root, self.capacity)['active_count'], 1)

    def test_corrupt_reservations_fail_closed(self):
        (self.root / '.leases').mkdir()
        (self.root / '.leases' / ('a' * 32 + '.json')).write_text('{}')
        with self.assertRaises(RuntimeError):
            r.view(self.root, self.capacity)
