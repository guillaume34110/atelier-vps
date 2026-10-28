import importlib.util
import io
import json
import os
from pathlib import Path
import subprocess
import sys
import tarfile
import tempfile
import unittest

directory = Path(__file__).resolve().parents[1] / 'macos/compute'
sys.path.insert(0, str(directory))
spec = importlib.util.spec_from_file_location('mac_task', directory / 'task.py')
task = importlib.util.module_from_spec(spec)
spec.loader.exec_module(task)


class TaskTests(unittest.TestCase):
    @unittest.skipUnless(sys.platform.startswith('linux'), 'guest privilege gate requires Linux')
    def test_guest_denies_new_privileges_in_a_real_child(self):
        code = '''import importlib.util,os,tempfile
from pathlib import Path
spec=importlib.util.spec_from_file_location('guest',os.environ['GUEST_SCRIPT'])
guest=importlib.util.module_from_spec(spec);spec.loader.exec_module(guest)
with tempfile.TemporaryDirectory() as directory:
 guest.CGROUP=Path(directory);(guest.CGROUP/'cgroup.procs').touch()
 guest.os.setgroups=lambda _:None;guest.os.setgid=lambda _:None;guest.os.setuid=lambda _:None
 guest.drop_privileges()
 assert 'NoNewPrivs:\\t1' in Path('/proc/self/status').read_text()
'''
        environment = {**os.environ, 'GUEST_SCRIPT': str(directory / 'guest-exec.py')}
        subprocess.run([sys.executable, '-c', code], env=environment, check=True, timeout=10)

    def archive(self, root, member):
        path = root / 'input.tar'
        with tarfile.open(path, mode='w') as tar:
            tar.addfile(member, io.BytesIO(b'hello') if member.isfile() else None)
        destination = root / 'source'
        destination.mkdir()
        return path, destination

    def test_source_refuses_links_and_private_paths(self):
        for name, kind in [('code.py', tarfile.SYMTYPE), ('../outside', tarfile.REGTYPE),
                           ('.env', tarfile.REGTYPE)]:
            with self.subTest(name=name), tempfile.TemporaryDirectory() as temporary:
                root = Path(temporary)
                member = tarfile.TarInfo(name)
                member.type = kind
                member.size = 5 if member.isfile() else 0
                member.linkname = '/private' if member.issym() else ''
                archive, destination = self.archive(root, member)
                with self.assertRaises(ValueError):
                    task.unpack_source(archive, destination)
                self.assertEqual(list(destination.iterdir()), [])

    def test_result_directory_symlink_and_undeclared_output_are_rejected(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            share = root / 'share'
            share.mkdir()
            (share / 'stdout.log').write_text('')
            (share / 'receipt.json').write_text(json.dumps({'status': 'passed', 'returncode': 0,
                                                            'results': {'secret.txt': {'bytes': 0, 'sha256': ''}}}))
            (share / 'results').symlink_to(root, target_is_directory=True)
            with self.assertRaisesRegex(ValueError, 'result directory'):
                task.pack_result(share, root, {'job_id': 'a' * 32, 'paths': ['safe.txt']})
            (share / 'results').unlink()
            (share / 'results').mkdir()
            with self.assertRaisesRegex(ValueError, 'undeclared'):
                task.pack_result(share, root, {'job_id': 'a' * 32, 'paths': ['safe.txt']})


if __name__ == '__main__':
    unittest.main()
