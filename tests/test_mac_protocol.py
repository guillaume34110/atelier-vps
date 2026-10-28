import hashlib
import io
import struct
import unittest

import mac_protocol as wire


class PartialStream(io.BytesIO):
    def read(self, size=-1):
        return super().read(min(size, 3) if size >= 0 else size)

    def write(self, data):
        return super().write(data[:3])


class ProtocolTests(unittest.TestCase):
    def request(self):
        return {'job_id': 'a' * 32, 'argv': ['python3', 'check.py'], 'paths': ['report.txt'],
                'cpu': 1, 'ram_mib': 512, 'disk_mib': 128, 'timeout': 30,
                'source_bytes': 5, 'source_sha256': hashlib.sha256(b'hello').hexdigest()}

    def test_short_reads_and_writes_preserve_frames_and_digest(self):
        stream = PartialStream()
        wire.write_frame(stream, {'status': 'ready'})
        wire.copy_exact(io.BytesIO(b'hello'), stream, 5, 5)
        stream.seek(0)
        self.assertEqual(wire.read_frame(stream), {'status': 'ready'})
        out = PartialStream()
        self.assertEqual(wire.copy_exact(stream, out, 5, 5), hashlib.sha256(b'hello').hexdigest())
        self.assertEqual(out.getvalue(), b'hello')

    def test_truncation_and_oversized_messages_fail(self):
        with self.assertRaises(EOFError):
            wire.copy_exact(io.BytesIO(b'a'), io.BytesIO(), 2, 2)
        with self.assertRaises(ValueError):
            wire.read_frame(io.BytesIO(struct.pack('!I', wire.FRAME_LIMIT + 1)))
        with self.assertRaises(ValueError):
            wire.copy_exact(io.BytesIO(), io.BytesIO(), 2, 1)

    def test_requests_reject_credentials_traversal_and_invalid_reservations(self):
        wire.request(self.request())
        for path in ['../outside', '/absolute', 'a/CLAUDE.md', '.ssh/id_ed25519', '.env.test']:
            with self.subTest(path=path), self.assertRaises(ValueError):
                wire.request({**self.request(), 'paths': [path]})
        for change in [{'cpu': True}, {'ram_mib': 0}, {'disk_mib': 4097},
                       {'source_bytes': wire.SOURCE_LIMIT + 1}, {'timeout': 3601},
                       {'job_id': '../other'}, {'extra': 1}]:
            with self.subTest(change=change), self.assertRaises(ValueError):
                wire.request({**self.request(), **change})


if __name__ == '__main__':
    unittest.main()
