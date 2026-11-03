import fcntl
import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch
from types import SimpleNamespace

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
            (expired / 'pip').mkdir(); (expired / 'pip/wheel').write_bytes(b'reproducible')
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
            (old / 'npm').mkdir(); (old / 'npm/package').write_bytes(b'reproducible')
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

    def test_cleanup_keeps_resource_counters_readable_while_holding_the_idle_lock(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            with patch.object(cache.time, 'time', return_value=0):
                path = cache.scope(root, {'project': 'old'})
            (path / 'pip').mkdir(); (path / 'pip/wheel').write_bytes(b'package')
            delete = cache.shutil.rmtree
            def remove(target, *args, **kwargs):
                with (root / '.resources.lock').open('a') as counters:
                    fcntl.flock(counters, fcntl.LOCK_EX | fcntl.LOCK_NB)
                with (root / '.queue.lock').open('a') as queue:
                    with self.assertRaises(BlockingIOError):
                        fcntl.flock(queue, fcntl.LOCK_SH | fcntl.LOCK_NB)
                return delete(target, *args, **kwargs)
            with patch.object(cache.shutil, 'rmtree', side_effect=remove):
                result = cache.maintain(root)
            self.assertEqual(result['status'], 'completed')

    def test_scheduled_cleanup_retries_after_busy_but_does_not_rescan_a_completed_pass(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            with (root / '.queue.lock').open('a') as queue:
                fcntl.flock(queue, fcntl.LOCK_SH)
                self.assertEqual(cache.maintain(root, scheduled=True)['status'], 'busy')
            self.assertEqual(cache.maintain(root, scheduled=True)['status'], 'completed')
            with patch.object(cache.os, 'statvfs', side_effect=AssertionError('unnecessary scan')):
                self.assertEqual(cache.maintain(root, scheduled=True)['status'], 'not_due')

    def test_scheduled_cleanup_continues_bounded_passes_until_the_package_budget_is_met(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            with patch.object(cache.time, 'time', return_value=10_000_000 - 7200):
                for index in range(10):
                    path = cache.scope(root, {'project': str(index)})
                    (path / 'pip').mkdir(); (path / 'pip/wheel').write_bytes(b'package')
            with patch.object(cache.time, 'time', return_value=10_000_000), \
                    patch.object(cache.os, 'statvfs', return_value=SimpleNamespace(f_blocks=1, f_frsize=1, f_bavail=1)):
                first = cache.maintain(root, scheduled=True)
                self.assertEqual(first['removed_scopes'], 8)
                self.assertGreater(first['package_bytes'], first['budget_bytes'])
                second = cache.maintain(root, scheduled=True)
                self.assertEqual(second['removed_scopes'], 2)
                self.assertEqual(second['package_bytes'], 0)
                self.assertEqual(cache.maintain(root, scheduled=True)['status'], 'not_due')

    def test_budget_trims_old_package_caches_but_preserves_models_and_fresh_downloads(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            now = 5_000_000
            with patch.object(cache.time, 'time', return_value=now - 7200):
                old = cache.scope(root, {'project': 'old'})
            (old / 'pip').mkdir(); (old / 'pip/wheel').write_bytes(b'x' * 8192)
            (old / 'huggingface').mkdir(); (old / 'huggingface/model.bin').write_bytes(b'keep model')
            with patch.object(cache.time, 'time', return_value=now):
                fresh = cache.scope(root, {'project': 'fresh'})
            (fresh / 'uv').mkdir(); (fresh / 'uv/package').write_bytes(b'keep hot cache')
            with patch.object(cache.time, 'time', return_value=now), \
                    patch.object(cache.os, 'statvfs', return_value=SimpleNamespace(f_blocks=4096, f_frsize=1, f_bavail=4096)):
                result = cache.maintain(root)
            self.assertEqual(result['status'], 'completed')
            self.assertGreater(result['evicted_package_bytes'], 0)
            self.assertEqual(result['reclaimed_bytes'], 0)
            self.assertFalse((old / 'pip').exists())
            self.assertEqual((old / 'huggingface/model.bin').read_bytes(), b'keep model')
            self.assertTrue((fresh / 'uv/package').exists())

    def test_gradle_properties_and_projects_are_not_disposable_package_caches(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            with patch.object(cache.time, 'time', return_value=0):
                path = cache.scope(root, {'project': 'gradle'})
            (path / 'gradle/caches').mkdir(parents=True)
            (path / 'gradle/caches/module.jar').write_bytes(b'reproducible')
            (path / 'gradle/gradle.properties').write_text('keep configuration')
            cache.maintain(root)
            self.assertFalse((path / 'gradle/caches').exists())
            self.assertEqual((path / 'gradle/gradle.properties').read_text(), 'keep configuration')

    def test_expiration_preserves_untracked_files_and_linked_package_directories(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            with patch.object(cache.time, 'time', return_value=0):
                path = cache.scope(root, {'project': 'models'})
            (path / 'model.bin').write_bytes(b'keep')
            external = root / 'outside'; external.mkdir(); (external / 'keep').write_bytes(b'keep')
            (path / 'npm').symlink_to(external, target_is_directory=True)
            (path / 'gradle').symlink_to(external, target_is_directory=True)
            result = cache.maintain(root)
            self.assertEqual(result['status'], 'completed')
            self.assertTrue((path / 'model.bin').exists())
            self.assertTrue((external / 'keep').exists())
