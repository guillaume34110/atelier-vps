import os
from pathlib import Path
import subprocess
import tempfile
import unittest
from unittest.mock import patch

import worker
from ops.deploy_lock import deployment_lock


class LifecycleTests(unittest.TestCase):
    def test_deployment_and_work_share_one_nonblocking_lock(self):
        with tempfile.TemporaryDirectory() as tmp, patch.object(worker, "ROOT", Path(tmp)):
            with deployment_lock(Path(tmp)):
                self.assertTrue(worker.is_busy())
                with self.assertRaises(BlockingIOError):
                    with deployment_lock(Path(tmp)):
                        self.fail("two deployments acquired the same lock")
            self.assertFalse(worker.is_busy())

    def test_startup_rejects_missing_capacity_probe(self):
        with patch.object(worker, "volume_ok", return_value=True), patch.object(os, "geteuid", return_value=10001), patch.object(os, "getegid", return_value=10001), patch.dict(os.environ, {}, clear=True):
            with self.assertRaisesRegex(RuntimeError, "capacity probe is missing"):
                worker.validate_environment()

    def test_startup_rejects_writable_probe(self):
        with tempfile.TemporaryDirectory() as tmp, patch.object(worker, "ROOT", Path(tmp)), patch.object(worker, "volume_ok", return_value=True), patch.object(os, "geteuid", return_value=10001), patch.object(os, "getegid", return_value=10001), patch.dict(os.environ, {"USEVPS_VOLUME_STATS": tmp}):
            with self.assertRaisesRegex(RuntimeError, "read-only"):
                worker.validate_environment()


class GitHookTests(unittest.TestCase):
    def push(self, *refs):
        hook = Path(__file__).resolve().parents[1] / ".githooks/pre-push"
        data = "".join(f"refs/heads/work {'a'*40} {ref} {'0'*40}\n" for ref in refs)
        return subprocess.run(["sh", str(hook)], input=data, text=True, capture_output=True)

    def test_integration_and_forbidden_branch_pushes_are_blocked(self):
        for ref in ["refs/heads/main", "refs/heads/master", "refs/heads/codex/change"]:
            with self.subTest(ref=ref):
                self.assertNotEqual(self.push(ref).returncode, 0)
        self.assertNotEqual(self.push("refs/heads/work", "refs/heads/main").returncode, 0)

    def test_work_branches_and_tags_are_allowed(self):
        self.assertEqual(self.push("refs/heads/harden-worker-hooks", "refs/tags/main").returncode, 0)
