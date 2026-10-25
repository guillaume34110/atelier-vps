import fcntl
import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

import dependency_cache as cache
import resources


class DependencyCacheTests(unittest.TestCase):
    def test_lockfile_scope_is_reused_across_commands_and_refreshed(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            request = dict(project='app', dependency_digest='a' * 64, argv=['npm', 'test'])
            with patch.object(cache.time, 'time', return_value=100):
                first = cache.scope(root, request)
            (first / 'dependency.tgz').write_bytes(b'reusable')
            with patch.object(cache.time, 'time', return_value=200):
                self.assertEqual(first, cache.scope(root, dict(request, argv=['npm', 'run', 'build'])))
            self.assertEqual(cache.last_used(first), 200)
            self.assertNotEqual(first, cache.scope(root, dict(request, dependency_digest='b' * 64)))
            self.assertEqual((first / 'dependency.tgz').read_bytes(), b'reusable')

    def test_only_verified_scopes_unused_for_30_days_are_removed(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            now = 5_000_000
            with patch.object(cache.time, 'time', return_value=now - cache.RETENTION_SECONDS):
                expired = cache.scope(root, {'project': 'old'})
            with patch.object(cache.time, 'time', return_value=now - cache.RETENTION_SECONDS + 1):
                useful = cache.scope(root, {'project': 'recent'})
            unknown = expired.parent / ('c' * 64)
            unknown.mkdir(); (unknown / 'keep').write_text('untracked data')
            jobs = root / 'jobs'; jobs.mkdir(); (jobs / 'result').write_text('preserve')
            legacy = root / 'cache' / 'npm'; legacy.mkdir(); (legacy / 'keep').write_text('legacy')
            with patch.object(cache.time, 'time', return_value=now):
                cache.maintain(root)
            self.assertFalse(expired.exists())
            self.assertTrue(useful.exists())
            self.assertTrue(unknown.exists())
            self.assertTrue((jobs / 'result').exists())
            self.assertTrue((legacy / 'keep').exists())
            self.assertEqual(cache.status(root)['removed_scopes'], 1)

    def test_live_work_and_deployment_locks_block_cleanup(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            with patch.object(cache.time, 'time', return_value=0):
                old = cache.scope(root, {'project': 'active'})
            with (root / '.queue.lock').open('a') as lock:
                fcntl.flock(lock, fcntl.LOCK_SH)
                cache.maintain(root)
                self.assertTrue(old.exists())
            with patch.object(resources, 'leases', return_value=[{'id': 'live'}]):
                cache.maintain(root)
                self.assertTrue(old.exists())
            cache.maintain(root)
            self.assertFalse(old.exists())

    def test_unknown_metadata_and_links_never_authorize_deletion(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            original = cache.scope(root, {'project': 'app'})
            marker = original / '.retention.json'
            for value in [[], {}, {'version': 1, 'last_used': float('nan')}, {'version': 2, 'last_used': 0}]:
                marker.write_text(json.dumps(value))
                cache.maintain(root)
                self.assertTrue(original.exists())
            external = root / 'valuable'; external.mkdir(); (external / 'keep').write_text('keep')
            marker.unlink(); marker.symlink_to(external / 'keep')
            (original.parent / ('d' * 64)).symlink_to(external, target_is_directory=True)
            cache.maintain(root)
            self.assertTrue((external / 'keep').exists())
            linked = root / 'linked'; linked.mkdir()
            (linked / 'cache').symlink_to(external, target_is_directory=True)
            with self.assertRaises(ValueError):
                cache.scope(linked, {})
