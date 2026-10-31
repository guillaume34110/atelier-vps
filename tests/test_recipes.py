import json
import os
from pathlib import Path
import socket
import subprocess
import sys
import tempfile
import unittest

import recipes


class Recipes(unittest.TestCase):
    def test_locked_node_recipes_and_explicit_local_requirements(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            (root / 'package.json').write_text(json.dumps({'scripts': {'test': 'node test.js', 'build': 'xcrun swiftc app.swift'},
                                                          'dependencies': {'demo': '1'}}))
            (root / 'package-lock.json').write_text('{}')
            _, found = recipes.discover(root)
            self.assertEqual(found['test']['argv'], ['npm', 'run', 'test'])
            self.assertEqual(found['test']['setup'][0][:2], ['npm', 'ci'])
            self.assertEqual(found['build']['platform'], 'local')
            (root / 'pnpm-lock.yaml').write_text('lockfileVersion: 9')
            with self.assertRaises(ValueError):
                recipes.discover(root)
            (root / recipes.CONFIG).write_text(json.dumps({'version': 1, 'recipes': {'test': {'argv': ['true']}}}))
            self.assertEqual(recipes.discover(root)[1]['test']['argv'], ['true'])

    def test_python_does_not_guess_unittest_for_unknown_tests(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp); (root / 'tests').mkdir()
            self.assertNotIn('test', recipes.discover(root)[1])
            (root / 'tests/test_example.py').write_text('import unittest\n')
            found = recipes.discover(root)[1]['test']
            self.assertEqual(found['argv'][:3], ['python3', '-m', 'unittest'])
            (root / 'requirements.txt').write_text('requests==2.32.3\n')
            self.assertNotIn('test', recipes.discover(root)[1])
            (root / 'requirements.txt').unlink()
            (root / 'tests/test_pytest.py').write_text('import pytest\n')
            self.assertNotIn('test', recipes.discover(root)[1])
            (root / 'pyproject.toml').write_text('[project]\nname="demo"\nversion="1"\ndependencies=["mlx"]\n')
            (root / 'uv.lock').write_text('version=1')
            found = recipes.discover(root)[1]['test']
            self.assertEqual(found['platform'], 'local')
            self.assertEqual(found['setup'], [['uv', 'sync', '--frozen']])

    def test_private_manifest_links_and_unknown_options_are_rejected(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp); other = root / 'other.json'; other.write_text('{}')
            config = root / recipes.CONFIG; config.symlink_to(other)
            with self.assertRaises(ValueError):
                recipes.discover(root)
            config.unlink(); os.link(other, config)
            with self.assertRaises(ValueError):
                recipes.discover(root)
            with self.assertRaises(ValueError):
                recipes.validate({'argv': ['true'], 'env': {'TOKEN': 'not-accepted'}})
            for path in ['../elsewhere', '.home/auth.json', '.env', 'CLAUDE.md', 'id_ed25519', 'out/key.pem']:
                with self.subTest(path=path), self.assertRaises(ValueError):
                    recipes.validate({'argv': ['true'], 'artifacts': [path]})
            for url in ['https://127.0.0.1:1234/', 'http://example.com/', 'http://u:p@127.0.0.1:1234/',
                        'http://127.0.0.1:1234/?token=x']:
                with self.subTest(url=url), self.assertRaises(ValueError):
                    recipes.validate({'argv': ['true'], 'services': [{'name': 'api', 'argv': ['true'], 'ready_url': url}]})

    def test_real_service_readiness_setup_and_cleanup(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            with socket.socket() as sock:
                sock.bind(('127.0.0.1', 0)); port = sock.getsockname()[1]
            value = {'argv': [sys.executable, '-c',
                f'import urllib.request; from pathlib import Path; assert urllib.request.urlopen("http://127.0.0.1:{port}/ready").read()==b"ready"; Path("verified").write_text("yes")'],
                'setup': [[sys.executable, '-c', 'from pathlib import Path; Path("ready").write_text("ready")']],
                'services': [{'name': 'api', 'argv': [sys.executable, '-m', 'http.server', str(port), '--bind', '127.0.0.1'],
                              'ready_url': f'http://127.0.0.1:{port}/ready', 'timeout': 5}]}
            result = subprocess.run([sys.executable, recipes.__file__, json.dumps(value)], cwd=root,
                                    env={**os.environ, 'USEVPS_ROOT': str(root)}, capture_output=True, timeout=15)
            self.assertEqual(result.returncode, 0, result.stderr.decode())
            self.assertEqual((root / 'verified').read_text(), 'yes')
            with socket.socket() as sock:
                self.assertNotEqual(sock.connect_ex(('127.0.0.1', port)), 0)

    def test_failed_service_never_starts_tests_or_stops_an_unrelated_process(self):
        other = subprocess.Popen([sys.executable, '-c', 'import time; time.sleep(20)'], start_new_session=True)
        try:
            with tempfile.TemporaryDirectory() as tmp:
                root = Path(tmp)
                value = {'argv': [sys.executable, '-c', 'from pathlib import Path; Path("should-not-exist").touch()'],
                         'services': [{'name': 'api', 'argv': [sys.executable, '-c', 'raise SystemExit(1)'],
                                       'ready_url': 'http://127.0.0.1:1/', 'timeout': 1}]}
                result = subprocess.run([sys.executable, recipes.__file__, json.dumps(value)], cwd=root,
                                        env={**os.environ, 'USEVPS_ROOT': str(root)}, capture_output=True, timeout=10)
                self.assertNotEqual(result.returncode, 0)
                self.assertFalse((root / 'should-not-exist').exists())
                self.assertIsNone(other.poll())
        finally:
            other.terminate(); other.wait(timeout=5)

    def test_parallel_recipes_get_distinct_ports_without_growing_lease_files(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            value = {'argv': [sys.executable, '-c',
                'import os,time,urllib.request; from pathlib import Path; u=os.environ["ATELIER_SERVICE_API_ORIGIN"]; '
                'assert urllib.request.urlopen(u).status==200; Path("origin").write_text(u); time.sleep(.5)'],
                'services': [{'name': 'api', 'argv': [sys.executable, '-m', 'http.server', '{port:api}', '--bind', '127.0.0.1'],
                              'ready_url': 'http://127.0.0.1:{port:api}/', 'timeout': 5}]}
            processes = []
            try:
                for name in ['first', 'second']:
                    work = root / name; work.mkdir()
                    processes.append(subprocess.Popen([sys.executable, recipes.__file__, json.dumps(value)], cwd=work,
                        env={**os.environ, 'USEVPS_ROOT': str(root)}, stdout=subprocess.PIPE, stderr=subprocess.PIPE))
                for process in processes:
                    _, errors = process.communicate(timeout=15)
                    self.assertEqual(process.returncode, 0, errors.decode())
                self.assertNotEqual((root / 'first/origin').read_text(), (root / 'second/origin').read_text())
                self.assertEqual((root / '.recipe-ports.lock').stat().st_size, 0)
            finally:
                for process in processes:
                    if process.poll() is None:
                        process.kill(); process.wait(timeout=5)


if __name__ == '__main__':
    unittest.main()
