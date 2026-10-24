import errno
import hashlib
import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

import worker
from ops.provision_env import verify_digest
from ops.prepare_volume import quota_bytes
from ops.worker_storage_guard import remaining_budget


class DevelopmentEnvironmentTests(unittest.TestCase):
    def test_quota_uses_all_capacity_after_only_half_gib_reserve(self):
        budget = quota_bytes(25 * worker.GIB, 2 * worker.GIB)
        self.assertEqual(budget, int(26.5 * worker.GIB))
        with self.assertRaises(RuntimeError):
            quota_bytes(worker.GIB // 4, 26 * worker.GIB)

    def test_download_checksum_rejects_modified_archive(self):
        with tempfile.TemporaryDirectory() as tmp:
            p = Path(tmp) / "archive"
            p.write_bytes(b"original")
            digest = hashlib.sha256(b"original").hexdigest()
            verify_digest(p, digest)
            p.write_bytes(b"tampered")
            with self.assertRaises(ValueError):
                verify_digest(p, digest)

    def test_write_guard_checks_declared_remaining_quota_and_physical_floor(self):
        s = {"workspace_free_bytes": 20 * worker.MIB, "free_bytes": 10 * worker.GIB,
             "reserve_bytes": worker.GIB // 2, "used_bytes": 2 * worker.MIB}
        self.assertEqual(remaining_budget(s, 5 * worker.MIB, 2 * worker.MIB), 3 * worker.MIB)
        self.assertEqual(remaining_budget({**s, "workspace_free_bytes": 2 * worker.MIB}, 5 * worker.MIB, 0), worker.MIB)
        self.assertLess(remaining_budget({**s, "free_bytes": worker.GIB // 4}, 5 * worker.MIB, 0), 0)

    def test_receipt_survives_full_quota_without_deleting_job_data(self):
        with tempfile.TemporaryDirectory() as tmp, patch.object(worker, "ROOT", Path(tmp)):
            root = Path(tmp)
            self.assertTrue(worker.ensure_receipt_reserve())
            job = root / "job"
            job.mkdir()
            (job / "useful.data").write_bytes(b"keep")
            original = Path.write_text
            calls = []
            def write(path, text, *args, **kwargs):
                if path.name == "receipt.tmp" and not calls:
                    calls.append(True)
                    raise OSError(errno.EDQUOT, "quota exceeded")
                return original(path, text, *args, **kwargs)
            with patch.object(Path, "write_text", write):
                worker.receipt(job, {"status": "failed", "reason": "disk limit"})
            self.assertEqual(json.loads((job / "receipt.json").read_text())["status"], "failed")
            self.assertEqual((job / "useful.data").read_bytes(), b"keep")
            self.assertFalse((root / ".receipt-reserve").exists())
