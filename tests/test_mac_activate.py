import importlib.util
import json
from pathlib import Path
import sys
import tempfile
import unittest
from unittest.mock import patch

directory = Path(__file__).resolve().parents[1] / 'macos/compute'
sys.path.insert(0, str(directory))
spec = importlib.util.spec_from_file_location('mac_activate', directory / 'activate.py')
activate = importlib.util.module_from_spec(spec)
spec.loader.exec_module(activate)


class ActivateTests(unittest.TestCase):
    def bundle(self, root):
        repo, bundle = root / 'repo', root / 'bundle'
        sources = activate.prepare.APP_FILES + ['macos/compute/VM.swift',
                                                'macos/compute/virtualization.entitlements']
        hashes = {}
        for name in sources:
            path = repo / name
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_text('source:' + name)
            hashes[name] = activate.task.digest(path)
            if name in activate.prepare.APP_FILES:
                target = bundle / 'app' / name
                target.parent.mkdir(parents=True, exist_ok=True)
                target.write_bytes(path.read_bytes())
        for name in ('assets/linux-image', 'assets/initramfs-virt', 'assets/modules/modloop-virt',
                     'assets/vm', 'assets/tools/usr/bin/python3.14', 'assets/tools/usr/bin/node'):
            path = bundle / name
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_text(name)
        database = bundle / 'assets/tools/lib/apk/db/installed'
        database.parent.mkdir(parents=True)
        database.write_text('P:python3\nV:3.14.10-r1\n\nP:nodejs\nV:24.22.1-r0\n')
        manifest = {'version': 1, 'sha256': activate.prepare.tree_digest(bundle), 'sources': hashes}
        (bundle / 'manifest.json').write_text(json.dumps(manifest))
        return repo, bundle

    def test_verified_copy_reuses_identical_version_and_preserves_modified_one(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            repo, bundle = self.bundle(root)
            with patch.object(activate.prepare, 'ROOT', repo), patch.object(activate.bridge, 'storage_available', return_value=True):
                destination = activate.install_bundle(bundle, root / 'worker', root / 'budget')
                self.assertEqual(activate.install_bundle(bundle, root / 'worker', root / 'budget'), destination)
                (destination / 'assets/vm').write_text('modified')
                with self.assertRaises(ValueError):
                    activate.install_bundle(bundle, root / 'worker', root / 'budget')
                self.assertEqual((destination / 'assets/vm').read_text(), 'modified')

    def test_disk_refusal_and_symlink_manifest_do_not_install(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            repo, bundle = self.bundle(root)
            with patch.object(activate.prepare, 'ROOT', repo), patch.object(activate.bridge, 'storage_available', return_value=False):
                with self.assertRaisesRegex(RuntimeError, 'disk'):
                    activate.install_bundle(bundle, root / 'worker', root / 'budget')
                self.assertEqual(list((root / 'worker/versions').iterdir()), [])
                (bundle / 'manifest.json').unlink()
                (bundle / 'manifest.json').symlink_to(repo / 'client_config.py')
                with self.assertRaises(ValueError):
                    activate.verify(bundle)


if __name__ == '__main__':
    unittest.main()
