import os
import tempfile
import unittest
from pathlib import Path

import projects


class ProjectTests(unittest.TestCase):
    def test_versions_are_verified_and_each_task_gets_an_isolated_copy(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            work = root / 'work'; work.mkdir()
            (work / 'main.py').write_text('print(1)\n')
            (work / 'nested').mkdir(); (work / 'nested/a.txt').write_text('source')
            digest, original = projects.fingerprint(projects.tree_files(work))
            stored = projects.store(root, 'demo', digest, work, 'a' * 32)
            self.assertTrue(stored['created'])
            checks = []
            copy = root / 'job'; copy.mkdir()
            projects.materialize(root, 'demo', digest, copy, 1024 * 1024, lambda: checks.append(True))
            self.assertTrue(checks)
            (copy / 'main.py').write_text('changed in task')
            source, entries = projects.verify(root, 'demo', digest)
            self.assertEqual(entries, original)
            self.assertEqual((source / 'main.py').read_text(), 'print(1)\n')
            with self.assertRaises(ValueError):
                projects.materialize(root, 'demo', digest, root / 'small', 1, lambda: None)
            (source / 'main.py').write_text('tampered')
            with self.assertRaises(ValueError):
                projects.verify(root, 'demo', digest)

    def test_source_changes_and_private_paths_never_become_a_version(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp); work = root / 'work'; work.mkdir()
            (work / 'a.txt').write_text('before')
            digest, _ = projects.fingerprint(projects.tree_files(work))
            (work / 'a.txt').write_text('after')
            with self.assertRaises(ValueError):
                projects.store(root, 'demo', digest, work, 'b' * 32)
            self.assertEqual((work / 'a.txt').read_text(), 'after')
            with self.assertRaises(ValueError):
                projects.fingerprint([(work / 'a.txt', 'CLAUDE.md')])
            with self.assertRaises(ValueError):
                projects.fingerprint([(work / 'a.txt', '.ssh/id_ed25519')])
            with self.assertRaises(ValueError):
                projects.identifier('../demo')

    def test_links_and_versions_cannot_replace_existing_sources(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp); work = root / 'work'; work.mkdir()
            (work / 'a').write_text('unchanged')
            digest, _ = projects.fingerprint(projects.tree_files(work))
            projects.store(root, 'demo', digest, work, 'a' * 32)
            (work / 'a').write_text('unchanged')
            self.assertFalse(projects.store(root, 'demo', digest, work, 'b' * 32)['created'])
            self.assertEqual((work / 'a').read_text(), 'unchanged')
            source, _ = projects.verify(root, 'demo', digest)
            (source / 'link').symlink_to(work / 'a')
            with self.assertRaises(ValueError):
                projects.verify(root, 'demo', digest)

    def test_unowned_project_directory_is_never_adopted(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp); parent = root / '.atelier-managed-projects'; parent.mkdir()
            data = parent / 'important.txt'; data.write_text('keep')
            with self.assertRaises(ValueError):
                projects.directory(root, 'demo', create=True)
            self.assertEqual(data.read_text(), 'keep')

    def test_special_files_and_linked_parent_are_rejected(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp); (root / 'outside').mkdir()
            (root / 'outside' / 'data').write_text('not part of source')
            work = root / 'work'; work.mkdir()
            (work / 'linked').symlink_to(root / 'outside', target_is_directory=True)
            with self.assertRaises(ValueError):
                projects.fingerprint([(work / 'linked' / 'data', 'linked/data')], root=work)
            os.mkfifo(work / 'pipe')
            with self.assertRaises(ValueError):
                projects.fingerprint([(work / 'pipe', 'pipe')], root=work)
