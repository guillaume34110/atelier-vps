import io
import fcntl
import json
from pathlib import Path
import tarfile
import tempfile
import unittest
from types import SimpleNamespace
from unittest.mock import patch

import vps
import worker


def archive(entries):
    out = io.BytesIO()
    with tarfile.open(fileobj=out, mode="w") as tar:
        for name, content, kind in entries:
            item = tarfile.TarInfo(name)
            item.type = kind
            item.size = len(content)
            item.linkname = "/etc/passwd" if kind == tarfile.SYMTYPE else ""
            tar.addfile(item, io.BytesIO(content) if kind == tarfile.REGTYPE else None)
    out.seek(0)
    return out


class ArchiveTests(unittest.TestCase):
    def test_disk_measurement_tolerates_atomic_replacements_but_not_permission_errors(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            useful = root / 'useful.data'
            useful.write_bytes(b'keep')
            (root / '.lease-changing.tmp').write_bytes(b'technical')
            original = Path.lstat
            def transient(path):
                if path.name == '.lease-changing.tmp':
                    raise FileNotFoundError(path)
                return original(path)
            with patch.object(Path, 'lstat', transient):
                self.assertEqual(worker.usage(root), useful.stat().st_blocks * 512)
            with patch.object(Path, 'lstat', side_effect=PermissionError('blocked')):
                with self.assertRaises(PermissionError):
                    worker.usage(root)

    def test_reject_paths_and_links(self):
        for name in ("../escape", "/etc/passwd", "foo/../../escape", "CLAUDE.md", ".git/config"):
            with self.subTest(name=name), self.assertRaises(ValueError):
                worker.safe_path(name)
        with tempfile.TemporaryDirectory() as tmp:
            with self.assertRaises(ValueError):
                worker.extract(archive([("link", b"", tarfile.SYMTYPE)]), Path(tmp))

    def test_extract_and_duplicate_rejection(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            worker.extract(archive([("src/main.py", b"print(1)", tarfile.REGTYPE)]), root)
            self.assertEqual((root / "src/main.py").read_bytes(), b"print(1)")
            with self.assertRaises(FileExistsError):
                worker.extract(archive([("src/main.py", b"replace", tarfile.REGTYPE)]), root)

    def test_input_limit(self):
        with tempfile.TemporaryDirectory() as tmp, patch.object(worker, "INPUT_LIMIT", 3):
            with self.assertRaises(ValueError):
                worker.extract(archive([("large", b"1234", tarfile.REGTYPE)]), Path(tmp))

    def test_source_filters_secrets_and_dependencies(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            (root / "main.py").write_text("print(1)")
            (root / ".env").write_text("secret")
            (root / 'deployment.local.json').write_text('{"private":"endpoint"}')
            (root / 'credentials.json').write_text('{"private":"credential fixture"}')
            (root / 'id_ed25519_fixture').write_text('not a private key')
            (root / "node_modules").mkdir()
            (root / "node_modules/package").write_text("dependency")
            (root / "external").symlink_to("/etc/passwd")
            self.assertEqual([n for _, n in vps.inputs(root)], ["main.py"])

    def test_bad_result_checksum_leaves_no_directory(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            data = archive([("manifest.json", json.dumps({"result": {"bytes": 4, "sha256": "bad"}}).encode(), tarfile.REGTYPE),
                            ("result", b"data", tarfile.REGTYPE)])
            file = root / "result.tar"
            file.write_bytes(data.read())
            with self.assertRaises(ValueError):
                vps.unpack_result(file, root / "download")
            self.assertFalse((root / "download").exists())

    def test_fetch_round_trip(self):
        with tempfile.TemporaryDirectory() as tmp, patch.object(worker, "ROOT", Path(tmp)):
            job = Path(tmp) / "jobs" / ("a" * 32)
            job.mkdir(parents=True)
            (job / "receipt.json").write_text('{"status":"passed"}')
            (job / "result.txt").write_text("processed")
            stream = io.BytesIO()
            worker.fetch({"id": "a" * 32, "paths": ["result.txt"]}, stream)
            file = Path(tmp) / "download.tar"
            file.write_bytes(stream.getvalue())
            self.assertEqual(vps.unpack_result(file, Path(tmp) / "download"), 1)
            self.assertEqual((Path(tmp) / "download/result.txt").read_text(), "processed")


class AdmissionTests(unittest.TestCase):
    def test_large_verified_project_quota_is_fully_available(self):
        with tempfile.TemporaryDirectory() as tmp, patch.object(worker, 'ROOT', Path(tmp)):
            probe = Path(tmp) / 'capacity'; probe.mkdir()
            def space(path):
                return SimpleNamespace(f_bavail=70 * worker.GIB, f_bfree=70 * worker.GIB,
                                       f_blocks=(80 if path == probe else 76) * worker.GIB, f_frsize=1)
            with patch.dict(worker.os.environ, {'USEVPS_VOLUME_STATS': str(probe), 'USEVPS_VOLUME_ID': 'test'}), \
                    patch.object(worker, 'volume_ok', return_value=True), patch.object(worker.os, 'statvfs', side_effect=space), \
                    patch.object(worker, 'available_ram', return_value=5 * worker.GIB):
                self.assertEqual(worker.capacity()['budget_bytes'], 76 * worker.GIB)
                self.assertEqual(worker.validate_job_request({'argv': ['true'], 'disk_bytes': 64 * worker.GIB})[2], 64 * worker.GIB)
                with self.assertRaises(ValueError):
                    worker.validate_job_request({'argv': ['true'], 'disk_bytes': 77 * worker.GIB})
                with patch.object(worker.os, 'statvfs', return_value=space(probe)):
                    self.assertEqual(worker.capacity()['budget_bytes'], 32 * worker.GIB)

    def test_project_quota_and_physical_disk_are_measured_separately(self):
        with tempfile.TemporaryDirectory() as tmp, patch.object(worker, "ROOT", Path(tmp)):
            capacity = Path(tmp) / "capacity"
            capacity.mkdir()
            def space(path):
                return SimpleNamespace(f_bavail=(27 if path == capacity else 7) * worker.GIB,
                                       f_bfree=(27 if path == capacity else 7) * worker.GIB,
                                       f_blocks=(29 if path == capacity else 7) * worker.GIB, f_frsize=1)
            with patch.dict(worker.os.environ, {"USEVPS_VOLUME_STATS": str(capacity), "USEVPS_VOLUME_ID": "test"}), patch.object(worker, "volume_ok", return_value=True), patch.object(worker.os, "statvfs", side_effect=space), patch.object(worker, "available_ram", return_value=5 * worker.GIB):
                s = worker.status()
                self.assertEqual(s["free_bytes"], 27 * worker.GIB)
                self.assertEqual(s["workspace_free_bytes"], 7 * worker.GIB)
                self.assertTrue(s["admit"])
                with patch.object(worker, "available_ram", return_value=worker.GIB):
                    self.assertFalse(worker.status()["admit"])
                with (Path(tmp) / '.queue.lock').open('a') as lock:
                    fcntl.flock(lock, fcntl.LOCK_EX)
                    self.assertFalse(worker.status()["admit"])

    def test_insufficient_project_quota_rejects_before_extract(self):
        s = {"free_bytes": 27 * worker.GIB, "reserve_bytes": 20 * worker.GIB,
             "workspace_free_bytes": worker.GIB // 2, "budget_bytes": 27 * worker.GIB, "used_bytes": 0, "host_available_ram": 5 * worker.GIB}
        with tempfile.TemporaryDirectory() as tmp, patch.object(worker, "ROOT", Path(tmp)), patch.object(worker, "capacity", return_value=s), patch.dict(worker.os.environ, {'USEVPS_QUEUE_WAIT_SECONDS': '0'}):
            with self.assertRaises(RuntimeError), patch.object(worker, "extract") as extract:
                worker.run_job({"argv": ["true"]}, io.BytesIO())
            extract.assert_not_called()

    def test_release_cannot_change_disk_usage_during_another_task(self):
        with tempfile.TemporaryDirectory() as tmp, patch.object(worker, "ROOT", Path(tmp)):
            path = Path(tmp) / "jobs" / ("b" * 32)
            path.mkdir(parents=True)
            (path / "receipt.json").write_text('{"status":"passed"}')
            with (Path(tmp) / ".queue.lock").open("a") as lock:
                fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
                with self.assertRaises(BlockingIOError):
                    worker.release_job({"id": path.name})
                self.assertTrue(path.exists())
            self.assertEqual(worker.release_job({"id": path.name}), {"released": path.name})
            self.assertFalse(path.exists())

    def test_invalid_request_cannot_start_process(self):
        with tempfile.TemporaryDirectory() as tmp, patch.object(worker, "ROOT", Path(tmp)), \
                patch.object(worker, 'capacity', return_value={'budget_bytes': 32 * worker.GIB}):
            for request in [{"argv": "sh"}, {"argv": ["true"], "timeout": 3601},
                            {"argv": ["true"], "disk_bytes": 33 * worker.GIB}]:
                with self.assertRaises(ValueError), patch("subprocess.Popen") as spawn:
                    worker.run_job(request, io.BytesIO())
                spawn.assert_not_called()

    def test_no_disk_headroom_rejects_before_extract(self):
        s = {"free_bytes": 20 * worker.GIB, "reserve_bytes": 20 * worker.GIB,
             "workspace_free_bytes": 7 * worker.GIB, "budget_bytes": 27 * worker.GIB, "used_bytes": 0, "host_available_ram": 5 * worker.GIB}
        with tempfile.TemporaryDirectory() as tmp, patch.object(worker, "ROOT", Path(tmp)), patch.object(worker, "capacity", return_value=s), patch.dict(worker.os.environ, {'USEVPS_QUEUE_WAIT_SECONDS': '0'}):
            with self.assertRaises(RuntimeError), patch.object(worker, "extract") as extract:
                worker.run_job({"argv": ["true"]}, io.BytesIO())
            extract.assert_not_called()

    def test_no_memory_headroom_rejects_before_extract(self):
        s = {"free_bytes": 30 * worker.GIB, "reserve_bytes": 20 * worker.GIB,
             "workspace_free_bytes": 7 * worker.GIB, "budget_bytes": 27 * worker.GIB, "used_bytes": 0, "host_available_ram": worker.GIB}
        with tempfile.TemporaryDirectory() as tmp, patch.object(worker, "ROOT", Path(tmp)), patch.object(worker, "capacity", return_value=s), patch.dict(worker.os.environ, {'USEVPS_QUEUE_WAIT_SECONDS': '0'}):
            with self.assertRaises(RuntimeError), patch.object(worker, "extract") as extract:
                worker.run_job({"argv": ["true"]}, io.BytesIO())
            extract.assert_not_called()


if __name__ == "__main__":
    unittest.main()
