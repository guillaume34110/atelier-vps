import hashlib
import io
import json
from pathlib import Path
import socket
import tarfile
import tempfile
import threading
import unittest
from unittest.mock import patch

import mac_client
import mac_protocol as wire


def result_archive(corrupt=False):
    data = b'answer'
    receipt = {'status': 'passed', 'returncode': 0, 'reason': None,
               'results': {'answer.txt': {'bytes': len(data),
                           'sha256': '0' * 64 if corrupt else hashlib.sha256(data).hexdigest()}}}
    contents = {'receipt.json': json.dumps(receipt).encode(), 'stdout.log': b'done\n',
                'results/answer.txt': data}
    archive = io.BytesIO()
    with tarfile.open(fileobj=archive, mode='w') as tar:
        for name, content in contents.items():
            info = tarfile.TarInfo(name)
            info.size = len(content)
            tar.addfile(info, io.BytesIO(content))
    return archive.getvalue()


class ClientTests(unittest.TestCase):
    def test_private_exchange_filters_source_and_verifies_result_before_ack(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            source = root / 'source'
            source.mkdir()
            (source / 'code.py').write_text('print(42)')
            (source / '.env').write_text('fake-test-value')
            output = root / 'result'
            output.mkdir()
            request = {'job_id': 'a' * 32, 'argv': ['python3', 'code.py'], 'paths': ['answer.txt'],
                       'cpu': 1, 'ram_mib': 512, 'disk_mib': 128, 'timeout': 30}
            left, right = socket.socketpair()
            errors = []

            def host():
                try:
                    with right, right.makefile('rb', buffering=0) as reader, right.makefile('wb', buffering=0) as writer:
                        received = wire.request(wire.read_frame(reader))
                        wire.write_frame(writer, {'state': 'accepted'})
                        source_data = io.BytesIO()
                        digest = wire.copy_exact(reader, source_data, received['source_bytes'], wire.SOURCE_LIMIT)
                        self.assertEqual(digest, received['source_sha256'])
                        source_data.seek(0)
                        with tarfile.open(fileobj=source_data) as archive:
                            self.assertEqual(archive.getnames(), ['code.py'])
                        payload = result_archive()
                        digest = hashlib.sha256(payload).hexdigest()
                        wire.write_frame(writer, {'state': 'completed', 'job_id': received['job_id'],
                                                  'archive_bytes': len(payload), 'archive_sha256': digest})
                        wire.write_all(writer, payload)
                        self.assertEqual(wire.read_frame(reader), {'state': 'received',
                                         'job_id': received['job_id'], 'archive_sha256': digest})
                        wire.write_frame(writer, {'state': 'released', 'job_id': received['job_id']})
                except Exception as error:
                    errors.append(error)

            thread = threading.Thread(target=host)
            thread.start()
            receipt = mac_client.execute(lambda: left, source, output, request)
            thread.join(timeout=5)
            self.assertFalse(thread.is_alive())
            self.assertEqual(errors, [])
            self.assertEqual(receipt['status'], 'passed')
            self.assertEqual((output / 'results/answer.txt').read_bytes(), b'answer')

    def test_corrupted_output_is_preserved_but_not_accepted(self):
        with tempfile.TemporaryDirectory() as temporary:
            output = Path(temporary)
            with self.assertRaisesRegex(ValueError, 'digest mismatch'):
                mac_client.unpack_results(io.BytesIO(result_archive(corrupt=True)), output)
            self.assertEqual((output / 'results/answer.txt').read_bytes(), b'answer')

    def test_refusal_is_known_before_any_source_is_transferred(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            source, output = root / 'source', root / 'result'
            source.mkdir(); output.mkdir()
            (source / 'code.py').write_text('print(42)')
            request = {'job_id': 'b' * 32, 'argv': ['true'], 'paths': [], 'cpu': 1,
                       'ram_mib': 512, 'disk_mib': 128, 'timeout': 30}
            left, right = socket.socketpair()
            def host():
                with right, right.makefile('rwb', buffering=0) as stream:
                    wire.read_frame(stream)
                    wire.write_frame(stream, {'state': 'refused', 'reason': 'Mac busy'})
            thread = threading.Thread(target=host)
            thread.start()
            with self.assertRaises(mac_client.TaskRefused):
                mac_client.execute(lambda: left, source, output, request)
            thread.join(timeout=5)
            self.assertFalse(thread.is_alive())
            self.assertEqual(list(output.iterdir()), [])

    def test_recovery_waits_on_the_server_without_restarting_the_task(self):
        with patch.object(mac_client.mac_relay, 'status', side_effect=[None, {'available': False},
                                                                       {'available': True}]) as status, \
                patch.object(mac_client.time, 'sleep') as sleep:
            mac_client.wait_for_relay(Path('/private-worker'), 30)
        self.assertEqual(status.call_count, 3)
        self.assertEqual(sleep.call_count, 2)
        with self.assertRaises(ValueError):
            mac_client.wait_for_relay(Path('/private-worker'), 3601)


if __name__ == '__main__':
    unittest.main()
