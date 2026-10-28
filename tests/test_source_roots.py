import json
import os
from pathlib import Path
import subprocess
import tempfile
import unittest
from unittest.mock import patch

import vps


class SourceRoots(unittest.TestCase):
    def make_worktree(self, home):
        repo = home / 'repo'
        repo.mkdir()
        env = {key: value for key, value in os.environ.items() if not key.startswith('GIT_')}
        env.update({'HOME': str(home), 'GIT_CONFIG_NOSYSTEM': '1',
               'GIT_CONFIG_GLOBAL': os.devnull, 'GIT_AUTHOR_NAME': 'Fixture',
               'GIT_AUTHOR_EMAIL': 'fixture@example.invalid', 'GIT_COMMITTER_NAME': 'Fixture',
               'GIT_COMMITTER_EMAIL': 'fixture@example.invalid'})
        def git(*args):
            subprocess.run(['git', '-C', str(repo), *args], env=env, check=True,
                           stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        git('init')
        (repo / 'main.py').write_text('print(42)')
        git('add', 'main.py')
        git('commit', '-m', 'fixture')
        worktree = home / '.codex/worktrees/example/project'
        worktree.parent.mkdir(parents=True)
        git('worktree', 'add', '--detach', str(worktree))
        return repo, worktree

    def test_web_bridge_can_only_send_explicit_project_roots(self):
        with tempfile.TemporaryDirectory() as folder:
            root=Path(folder);project=root/'project';project.mkdir();(project/'main.py').write_text('print(42)')
            other=root/'other';other.mkdir();(other/'private.txt').write_text('never sent')
            with patch.dict(os.environ,{'ATELIER_VPS_SOURCE_ROOTS':json.dumps([str(project)])}):
                self.assertEqual([name for _,name in vps.inputs(project)],['main.py'])
                with self.assertRaises(ValueError):list(vps.inputs(other))
                (project/'outside').symlink_to(other,target_is_directory=True)
                self.assertEqual([name for _,name in vps.inputs(project)],['main.py'])

    def test_personal_state_and_private_key_names_are_never_archived(self):
        with tempfile.TemporaryDirectory() as folder:
            root=Path(folder);private=root/'settings';private.mkdir();(private/'config.json').write_text('{}')
            with patch.dict(os.environ,{'ATELIER_VPS_CONFIG':str(private/'config.json')}):
                with self.assertRaises(ValueError):list(vps.inputs(private))
            project=root/'project';project.mkdir()
            for name in ['main.py','id_ed25519_atelier_device','ssh_host_ed25519_key']:(project/name).write_text('fixture')
            hidden=project/'.codex';hidden.mkdir();(hidden/'state.txt').write_text('fixture')
            self.assertEqual([name for _,name in vps.inputs(project)],['main.py'])

    def test_registered_codex_worktree_uses_the_normal_secret_filter(self):
        with tempfile.TemporaryDirectory() as folder:
            home = Path(folder).resolve()
            _, project = self.make_worktree(home)
            (project / '.codex').mkdir()
            (project / '.codex' / 'state').write_text('private fixture')
            (project / '.ssh').mkdir()
            (project / '.ssh' / 'id_fixture').write_text('private fixture')
            (project / '.env').write_text('private fixture')
            (project / 'sub').mkdir()
            (project / 'sub' / 'task.py').write_text('pass')
            with patch.object(Path, 'home', return_value=home), patch.dict(
                    os.environ, {'ATELIER_VPS_CONFIG': str(home / '.config/atelier-vps/config.json'),
                                 'ATELIER_VPS_SOURCE_ROOTS': ''}, clear=False):
                self.assertEqual([name for _, name in vps.inputs(project)], ['main.py', 'sub/task.py'])
                self.assertEqual([name for _, name in vps.inputs(project / 'sub')], ['task.py'])
                for private in (home / '.codex', home / '.codex/worktrees', project / '.codex', project / '.ssh'):
                    with self.subTest(private=private), self.assertRaises(ValueError):
                        list(vps.inputs(private))

    def test_fake_or_unregistered_codex_directories_remain_private(self):
        with tempfile.TemporaryDirectory() as folder:
            home = Path(folder).resolve()
            _, project = self.make_worktree(home)
            fake = home / '.codex/worktrees/other/fake'
            fake.mkdir(parents=True)
            (fake / '.git').write_text((project / '.git').read_text())
            (fake / 'private').write_text('private fixture')
            linked = home / '.codex/worktrees/linked/project'
            linked.mkdir(parents=True)
            (linked / '.git').symlink_to(project / '.git')
            with patch.object(Path, 'home', return_value=home), patch.dict(
                    os.environ, {'ATELIER_VPS_CONFIG': str(home / '.config/atelier-vps/config.json'),
                                 'ATELIER_VPS_SOURCE_ROOTS': ''}, clear=False):
                for private in (fake, linked, fake.parent, home / '.codex/worktrees'):
                    with self.subTest(private=private), self.assertRaises(ValueError):
                        list(vps.inputs(private))

    def test_worktree_cannot_send_nested_client_config_or_hard_links(self):
        with tempfile.TemporaryDirectory() as folder:
            home = Path(folder).resolve()
            _, project = self.make_worktree(home)
            config = project / 'personal-config'
            config.mkdir()
            (config / 'config.json').write_text('private fixture')
            private = home / '.ssh'
            private.mkdir()
            (private / 'fixture').write_text('private fixture')
            with patch.object(Path, 'home', return_value=home), patch.dict(os.environ, {
                    'ATELIER_VPS_CONFIG': str(config / 'config.json'), 'ATELIER_VPS_SOURCE_ROOTS': ''}):
                self.assertEqual([name for _, name in vps.inputs(project)], ['main.py'])
                os.link(private / 'fixture', project / 'renamed')
                with self.assertRaisesRegex(ValueError, 'hard-linked'):
                    list(vps.inputs(project))

    def test_explicit_source_roots_still_apply_to_codex_worktrees(self):
        with tempfile.TemporaryDirectory() as folder:
            home = Path(folder).resolve()
            repo, project = self.make_worktree(home)
            with patch.object(Path, 'home', return_value=home), patch.dict(os.environ, {
                    'ATELIER_VPS_CONFIG': str(home / '.config/atelier-vps/config.json'),
                    'ATELIER_VPS_SOURCE_ROOTS': json.dumps([str(repo)])}):
                with self.assertRaises(ValueError):
                    list(vps.inputs(project))
