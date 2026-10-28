import gzip
import hashlib
import importlib.util
from pathlib import Path
import sys
import tempfile
import unittest
from unittest.mock import patch

directory = Path(__file__).resolve().parents[1] / 'macos/compute'
sys.path.insert(0, str(directory))
spec = importlib.util.spec_from_file_location('mac_prepare', directory / 'prepare.py')
prepare = importlib.util.module_from_spec(spec)
spec.loader.exec_module(prepare)


class PrepareTests(unittest.TestCase):
    def test_kernel_unpack_verifies_architecture_and_pinned_digest(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            image = b'\0' * 0x38 + b'ARM\x64' + b'content'
            compressed = root / 'compressed'
            compressed.write_bytes(b'header' + gzip.compress(image) + b'EFI trailer')
            with patch.dict(prepare.HASHES, {'linux-image': hashlib.sha256(image).hexdigest()}):
                prepare.kernel_image(compressed, root / 'image')
            self.assertEqual((root / 'image').read_bytes(), image)
            with patch.dict(prepare.HASHES, {'linux-image': '0' * 64}), self.assertRaises(ValueError):
                prepare.kernel_image(compressed, root / 'different')

    def test_bundle_digest_detects_changes_and_rejects_sensitive_names(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            (root / 'file').write_text('first')
            digest = prepare.tree_digest(root)
            (root / 'file').write_text('second')
            self.assertNotEqual(digest, prepare.tree_digest(root))
            (root / '.ssh').mkdir()
            with self.assertRaisesRegex(ValueError, 'private-looking'):
                prepare.tree_digest(root)


if __name__ == '__main__':
    unittest.main()
