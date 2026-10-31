import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

import project_tasks
import projects
import recipes
import vps


class ProjectRouting(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory(); self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)
        self.write({'version': 1, 'id': 'demo', 'recipes': {'test': {'argv': ['true'], 'artifacts': ['result.json']}}})

    def write(self, value):
        (self.root / recipes.CONFIG).write_text(json.dumps(value))

    def selected(self, request, server=None, arch='any'):
        self.claim = request
        return {'id': 'preprod', 'request': dict(request), 'plan': {'project': {'present': self.present}},
                'state': {}}

    def test_first_run_then_reuse_does_not_upload_again(self):
        self.present = False
        def execute(selected, source):
            self.source = source
            self.upload = selected['request']['project_task']['upload']
            return 0, {'status': 'passed', 'id': 'a' * 32, 'managed_project': selected['request']['project_task']}
        with patch.object(vps, 'select_server', side_effect=self.selected), patch.object(vps, 'run_selected', side_effect=execute):
            rc, result = project_tasks.execute(self.root, 'test', 'Tests', 1)
            self.assertEqual(rc, 0)
            self.assertEqual(result['route']['mode'], 'stored')
            self.assertTrue(self.upload)
            self.assertEqual(self.source, self.root)
            digest = self.claim['project_task']['revision']
            self.present = True
            _, result = project_tasks.execute(self.root, 'test', 'Tests', 1)
            self.assertEqual(result['route']['mode'], 'reused')
            self.assertFalse(self.upload)
            self.assertIsNone(self.source)
            self.assertEqual(result['project_ref'], 'preprod:demo:' + digest)

    def test_plan_and_local_requirement_never_execute_any_command(self):
        self.present = False
        with patch.object(vps, 'select_server', side_effect=self.selected), patch.object(vps, 'run_selected') as run:
            _, result = project_tasks.execute(self.root, 'test', 'Tests', 1, plan_only=True)
            self.assertEqual(result['status'], 'planned')
            run.assert_not_called()
        self.write({'version': 1, 'recipes': {'test': {'argv': ['xcodebuild'], 'platform': 'local'}}})
        with patch.object(vps, 'select_server') as select, patch.object(vps, 'run_selected') as run:
            rc, result = project_tasks.execute(self.root, 'test', 'Tests', 1)
            self.assertEqual((rc, result['status']), (2, 'local_required'))
            select.assert_not_called(); run.assert_not_called()

    def test_large_source_stays_remote_without_creating_an_oversized_snapshot(self):
        self.present = False
        with (self.root / 'data').open('wb') as out:
            out.truncate(2 * 1024 ** 2)
        def execute(selected, source):
            self.assertNotIn('project_task', selected['request'])
            self.assertEqual(source, self.root)
            return 0, {'status': 'passed'}
        with patch.object(vps, 'select_server', side_effect=self.selected), patch.object(vps, 'run_selected', side_effect=execute):
            _, result = project_tasks.execute(self.root, 'test', 'Tests', 3 / 1024)
            self.assertEqual(result['route']['mode'], 'temporary')

    def test_edits_change_the_version_and_executed_failure_is_not_retried(self):
        self.present = False
        with patch.object(vps, 'select_server', side_effect=self.selected), patch.object(vps, 'run_selected', return_value=(1, {'status': 'failed'})) as run:
            rc, _ = project_tasks.execute(self.root, 'test', 'Tests', 1)
            self.assertEqual(rc, 1); run.assert_called_once()
            before = self.claim['project_task']['revision']
            (self.root / 'new.py').write_text('changed')
            project_tasks.execute(self.root, 'test', 'Tests', 1, plan_only=True)
            self.assertNotEqual(before, self.claim['project_task']['revision'])
            run.assert_called_once()

    def test_one_job_stores_sources_and_working_copies_stay_isolated(self):
        cache = self.root / 'worker'; cache.mkdir()
        work = self.root / 'first'; work.mkdir(); (work / 'main').write_text('original')
        digest, _ = projects.fingerprint(projects.tree_files(work))
        value = {'id': 'demo', 'revision': digest, 'upload': True}
        created = projects.ensure_work(cache, value, work, 'a' * 32, 1024 ** 2, lambda: None)
        self.assertTrue(created['created'])
        second = self.root / 'second'; second.mkdir()
        reused = projects.ensure_work(cache, {**value, 'upload': False}, second, 'b' * 32, 1024 ** 2, lambda: None)
        self.assertFalse(reused['created'])
        (work / 'main').write_text('changed')
        self.assertEqual((second / 'main').read_text(), 'original')
        self.assertTrue(projects.describe(cache, 'demo', digest)['present'])
        source, _ = projects.verify(cache, 'demo', digest)
        self.assertEqual((source / 'main').read_text(), 'original')
        with self.assertRaises(ValueError):
            projects.ensure_work(cache, {**value, 'upload': False}, work, 'c' * 32, 1024 ** 2, lambda: None)

