import fcntl
import os
from pathlib import Path
import select
import socket
import tempfile
import threading
import time
import unittest
from unittest.mock import patch

import mac_protocol as wire
import mac_relay


class MacRelayTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.root = Path(os.path.relpath(self.temporary.name))
        (self.root / '.queue.lock').touch()
        self.in_read, self.in_write = os.pipe()
        self.out_read, self.out_write = os.pipe()
        self.errors = []
        self.thread = None
        if not hasattr(socket, 'SO_PEERCRED'):
            identity = patch.object(mac_relay, '_peer_user', return_value=os.getuid())
            identity.start()
            self.addCleanup(identity.stop)

    def read(self, size):
        data = bytearray()
        deadline = time.monotonic() + 5
        while len(data) < size:
            remaining = deadline - time.monotonic()
            if remaining <= 0 or not select.select([self.out_read], [], [], remaining)[0]:
                raise TimeoutError('private relay test did not respond: ' + repr(self.errors))
            part = os.read(self.out_read, size - len(data))
            if not part:
                raise EOFError('private relay test closed')
            data.extend(part)
        return bytes(data)

    def tearDown(self):
        if self.thread and self.thread.is_alive():
            os.close(self.in_write)
            self.in_write = None
            self.thread.join(timeout=3)
        for fd in (self.in_read, self.in_write, self.out_read, self.out_write):
            if fd is not None:
                os.close(fd)
        self.temporary.cleanup()

    def start(self, wait=2):
        def run():
            try:
                mac_relay.listen(self.root, {'action': 'listen', 'node_id': 'a' * 32},
                                 self.in_read, self.out_write, wait=wait)
            except Exception as error:
                self.errors.append(error)
        self.thread = threading.Thread(target=run)
        self.thread.start()
        self.assertEqual(self.read(1), b'R')
        with os.fdopen(os.dup(self.out_read), 'rb', buffering=0) as reader:
            self.assertEqual(wire.read_frame(reader), {'scope': 'owner', 'server_id': None})

    def test_private_connection_exchanges_bytes_and_protects_active_job(self):
        self.start()
        self.assertEqual(mac_relay.status(self.root), {'node_id': 'a' * 32, 'available': True})
        with socket.socket(socket.AF_UNIX, socket.SOCK_STREAM) as client:
            client.settimeout(2)
            client.connect(str(self.root / '.mac-relay/worker.sock'))
            self.assertEqual(self.read(1), b'J')
            self.assertEqual(mac_relay.status(self.root), {'node_id': 'a' * 32, 'available': False})
            with (self.root / '.queue.lock').open('a') as lock:
                with self.assertRaises(BlockingIOError):
                    fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
            client.sendall(b'input')
            self.assertEqual(self.read(5), b'input')
            os.write(self.in_write, b'result')
            self.assertEqual(client.recv(6), b'result')
        self.thread.join(timeout=3)
        self.assertFalse(self.thread.is_alive())
        self.assertEqual(self.errors, [])
        self.assertIsNone(mac_relay.status(self.root))

    def test_disconnected_or_idle_host_leaves_no_available_socket(self):
        self.start(wait=1)
        self.assertEqual(self.read(1), b'T')
        self.thread.join(timeout=3)
        self.assertFalse(self.thread.is_alive())
        self.assertEqual(self.errors, [])
        self.assertIsNone(mac_relay.status(self.root))

    def test_other_unix_user_is_rejected(self):
        with patch.object(mac_relay, '_peer_user', return_value=os.getuid() + 1):
            self.start()
            with socket.socket(socket.AF_UNIX, socket.SOCK_STREAM) as client:
                client.connect(str(self.root / '.mac-relay/worker.sock'))
                self.thread.join(timeout=3)
        self.assertFalse(self.thread.is_alive())
        self.assertEqual(len(self.errors), 1)
        self.assertIsInstance(self.errors[0], PermissionError)
        self.assertIsNone(mac_relay.status(self.root))

    def test_untrusted_directory_and_invalid_identity_are_rejected(self):
        with self.assertRaises(ValueError):
            mac_relay.listen(self.root, {'action': 'listen', 'node_id': '../other'},
                             self.in_read, self.out_write)
        external = self.root / 'preserve'
        external.mkdir()
        (self.root / '.mac-relay').symlink_to(external.resolve())
        with self.assertRaises(PermissionError):
            mac_relay.listen(self.root, {'action': 'listen', 'node_id': 'a' * 32},
                             self.in_read, self.out_write)
        self.assertEqual(list(external.iterdir()), [])


if __name__ == '__main__':
    unittest.main()
