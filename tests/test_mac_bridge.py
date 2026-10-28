import fcntl
import hashlib
import importlib.util
import json
import os
from pathlib import Path
import socket
import sys
import tempfile
import threading
import time
import unittest
from unittest.mock import patch

import mac_client
import mac_protocol as wire
from tests.test_mac_client import result_archive

directory = Path(__file__).resolve().parents[1] / 'macos/compute'
sys.path.insert(0, str(directory))
spec = importlib.util.spec_from_file_location('mac_bridge', directory / 'bridge.py')
bridge = importlib.util.module_from_spec(spec)
spec.loader.exec_module(bridge)


class BridgeTests(unittest.TestCase):
    def test_only_current_developer_scope_can_attach_the_mac(self):
        rows = [{'ssh_host': 'owner-vps', 'scope': 'owner'},
                {'ssh_host': 'steph-vps', 'scope': 'steph'}]
        with patch.object(bridge.client_config, 'active_connections', return_value=rows):
            self.assertTrue(bridge.permitted('owner-vps', 'owner'))
            self.assertFalse(bridge.permitted('steph-vps', 'owner'))
            self.assertFalse(bridge.permitted('removed-vps', 'owner'))
            self.assertFalse(bridge.authenticated('owner-vps', 'owner', {'scope': 'steph', 'server_id': None}))
            self.assertTrue(bridge.authenticated('owner-vps', 'owner', {'scope': 'owner', 'server_id': None}))
        rows[0]['server_id'] = 'a' * 64
        with patch.object(bridge.client_config, 'active_connections', return_value=rows):
            self.assertFalse(bridge.authenticated('owner-vps', 'owner', {'scope': 'owner', 'server_id': 'b' * 64}))
            self.assertTrue(bridge.authenticated('owner-vps', 'owner', {'scope': 'owner', 'server_id': 'a' * 64}))

    def prepare(self, root, *, guard_pid=999999999, corrupt=False):
        project = root / 'worker'
        workspace = project / 'output/task-tmp'
        workspace.mkdir(parents=True)
        job_id = 'a' * 32
        payload = result_archive()
        response = {'state': 'completed', 'job_id': job_id, 'archive_bytes': len(payload),
                    'archive_sha256': hashlib.sha256(payload).hexdigest()}
        (workspace / 'owner.json').write_text(json.dumps({'job_id': job_id, 'guard_pid': guard_pid}))
        (workspace / 'result-record.json').write_text(json.dumps(response))
        (workspace / 'result.tar').write_bytes(payload + (b'x' if corrupt else b''))
        lock = workspace / '.task.lock'
        lock.touch(mode=0o600)
        return project, workspace, job_id

    def exchange(self, project, job_id, output):
        left, right = socket.socketpair()
        errors = []

        def run():
            try:
                with right, right.makefile('rwb', buffering=0) as stream:
                    bridge.handle(stream, stream, project, {}, Path('/unused'))
            except Exception as error:
                errors.append(error)

        thread = threading.Thread(target=run)
        thread.start()
        try:
            with left:
                receipt = mac_client.recover(left, job_id, output)
        finally:
            thread.join(timeout=5)
        self.assertFalse(thread.is_alive())
        self.assertEqual(errors, [])
        return receipt

    def test_disconnected_task_can_be_recovered_and_only_then_released(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            project, workspace, job_id = self.prepare(root)
            output = root / 'result'
            output.mkdir()
            receipt = self.exchange(project, job_id, output)
            self.assertEqual(receipt['status'], 'passed')
            self.assertEqual((output / 'results/answer.txt').read_bytes(), b'answer')
            self.assertFalse(workspace.exists())

    def test_changed_result_or_live_guard_remains_preserved(self):
        for options in ({'corrupt': True}, {'guard_pid': os.getpid()}):
            with self.subTest(options=options), tempfile.TemporaryDirectory() as temporary:
                root = Path(temporary)
                project, workspace, job_id = self.prepare(root, **options)
                output = root / 'result'
                output.mkdir()
                with self.assertRaisesRegex(RuntimeError, 'result unavailable'):
                    self.exchange(project, job_id, output)
                self.assertTrue((workspace / 'result.tar').is_file())

    def test_global_worker_lock_refuses_a_second_task(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            request = {'job_id': 'a' * 32, 'argv': ['true'], 'paths': [], 'cpu': 1,
                       'ram_mib': 512, 'disk_mib': 128, 'timeout': 30,
                       'source_bytes': 1, 'source_sha256': '0' * 64}
            with bridge.lock_file(root / '.worker.lock') as lock:
                fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
                left, right = socket.socketpair()
                with left, right, left.makefile('rwb', buffering=0) as client, right.makefile('rwb', buffering=0) as server:
                    wire.write_frame(client, request)
                    bridge.handle(server, server, root, {}, Path('/unused'))
                    self.assertEqual(wire.read_frame(client), {'state': 'refused', 'reason': 'Mac busy'})

    def test_recovery_waits_for_the_active_task_lock(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            project, workspace, job_id = self.prepare(root)
            output = root / 'result'
            output.mkdir()
            result, errors = [], []
            with bridge.lock_file(project / '.worker.lock') as lock:
                fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
                def recover():
                    try:
                        result.append(self.exchange(project, job_id, output))
                    except Exception as error:
                        errors.append(error)
                thread = threading.Thread(target=recover)
                thread.start()
                time.sleep(.1)
                self.assertTrue(thread.is_alive())
                fcntl.flock(lock, fcntl.LOCK_UN)
            thread.join(timeout=5)
            self.assertFalse(thread.is_alive())
            self.assertEqual(errors, [])
            self.assertEqual(result[0]['status'], 'passed')
            self.assertFalse(workspace.exists())


if __name__ == '__main__':
    unittest.main()
