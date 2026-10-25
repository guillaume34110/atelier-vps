"""Real bounded Linux GUI probe; no provider sign-in or authentication code is opened."""
import binascii
import ctypes
from contextlib import nullcontext, redirect_stdout
import hashlib
import io
import json
import os
from pathlib import Path
import socket
import select
import subprocess
import struct
import sys
import threading
import time
import zlib

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import desktop_service as desktop
import resources
import worker


def receive(peer, size):
    value = bytearray()
    while len(value) < size:
        part = peer.recv(size - len(value))
        if not part:
            raise EOFError('private RFB connection closed')
        value.extend(part)
    return bytes(value)


def answer_challenge(password, challenge):
    # Use the installed OpenSSL primitive, rather than implementing a cipher.
    class Schedule(ctypes.Structure):
        _fields_ = [('words', ctypes.c_uint32 * 32)]
    block = ctypes.c_ubyte * 8
    crypto = ctypes.CDLL('libcrypto.so.3')
    crypto.DES_set_key_unchecked.argtypes = [ctypes.POINTER(block), ctypes.POINTER(Schedule)]
    crypto.DES_ecb_encrypt.argtypes = [ctypes.POINTER(block), ctypes.POINTER(block), ctypes.POINTER(Schedule), ctypes.c_int]
    key = block(*(int(f'{n:08b}'[::-1], 2) for n in password.encode()))
    schedule = Schedule(); crypto.DES_set_key_unchecked(ctypes.byref(key), ctypes.byref(schedule))
    answer = b''
    for offset in [0, 8]:
        source = block.from_buffer_copy(challenge[offset:offset + 8]); target = block()
        crypto.DES_ecb_encrypt(ctypes.byref(source), ctypes.byref(target), ctypes.byref(schedule), 1)
        answer += bytes(target)
    return answer


def capture(value, output, peer=None):
    with (nullcontext(peer) if peer is not None else socket.create_connection(('127.0.0.1', value['port']), timeout=15)) as peer:
        assert receive(peer, 12).startswith(b'RFB ')
        peer.sendall(b'RFB 003.008\n')
        count = receive(peer, 1)[0]; assert 0 < count < 32
        choices = receive(peer, count); assert 2 in choices and 1 not in choices
        peer.sendall(b'\x02')
        peer.sendall(answer_challenge(value['password'], receive(peer, 16)))
        assert receive(peer, 4) == b'\0\0\0\0', 'private VNC authentication failed'
        peer.sendall(b'\x01')
        header = receive(peer, 24)
        width, height = struct.unpack('>HH', header[:4])
        name_size, = struct.unpack('>I', header[20:])
        assert width == 1280 and height == 800 and name_size <= 4096
        receive(peer, name_size)
        peer.sendall(b'\0\0\0\0' + struct.pack('>BBBBHHHBBB3x', 32, 24, 0, 1, 255, 255, 255, 16, 8, 0))
        peer.sendall(b'\x02\0\0\x01' + struct.pack('>i', 0))
        peer.sendall(b'\x03\0' + struct.pack('>HHHH', 0, 0, width, height))
        pixels = bytearray(width * height * 4)
        for _ in range(20):
            kind = receive(peer, 1)[0]
            if kind == 2:
                continue
            if kind == 3:
                length, = struct.unpack('>3xI', receive(peer, 7)); assert length <= 65536
                receive(peer, length); continue
            assert kind == 0, 'unexpected RFB server message'
            rectangles, = struct.unpack('>xH', receive(peer, 3)); assert 0 < rectangles <= 512
            for _ in range(rectangles):
                x, y, w, h, encoding = struct.unpack('>HHHHi', receive(peer, 12))
                assert encoding == 0 and w > 0 and h > 0 and x + w <= width and y + h <= height
                data = receive(peer, w * h * 4)
                for row in range(h):
                    start = ((y + row) * width + x) * 4
                    pixels[start:start + w * 4] = data[row * w * 4:(row + 1) * w * 4]
            break
        else:
            raise RuntimeError('no real desktop framebuffer was received')
    colors = {bytes(pixels[i:i + 3]) for i in range(0, len(pixels), 400)}
    assert len(colors) > 8, 'the remote desktop is blank'
    rows = bytearray()
    for y in range(height):
        rows.append(0)
        for x in range(width):
            index = (y * width + x) * 4
            rows.extend((pixels[index + 2], pixels[index + 1], pixels[index]))
    def chunk(name, data):
        return struct.pack('>I', len(data)) + name + data + struct.pack('>I', binascii.crc32(name + data))
    png = b'\x89PNG\r\n\x1a\n' + chunk(b'IHDR', struct.pack('>IIBBBBB', width, height, 8, 2, 0, 0, 0))
    png += chunk(b'IDAT', zlib.compress(rows, 6)) + chunk(b'IEND', b'')
    output.write_bytes(png)
    return {'width': width, 'height': height, 'colors_sampled': len(colors),
            'vnc_password_required': True, 'image_sha256': hashlib.sha256(png).hexdigest()}


def capture_session(ident):
    if not ident or worker.ROOT.joinpath('.volume-id').read_text().strip() != 'ci-volume':
        raise PermissionError('session capture is only permitted on the isolated CI volume')
    from quota_protection import protect_quota
    protect_quota(); worker.validate_environment()
    state = desktop.record()
    if not state or state['id'] != ident or state.get('phase') != 'ready' or not desktop.active(state):
        raise PermissionError('the CI desktop is not ready')
    pids = Path('/sys/fs/cgroup')
    process_limit = int((pids / 'pids.max').read_text())
    process_count = int((pids / 'pids.current').read_text())
    limit_hits = int(dict(x.split() for x in (pids / 'pids.events').read_text().splitlines())['max'])
    assert process_limit == 512 and process_count <= process_limit - 32 and limit_hits == 0, 'GUI must leave control-plane process headroom'
    private = io.StringIO()
    with redirect_stdout(private):
        desktop.dispatch({'action': 'ticket', 'id': ident})
    ticket = json.loads(private.getvalue())
    assert ticket['id'] == ident
    class Peer:
        def __init__(self, process):
            self.process = process
        def recv(self, size):
            if not select.select([self.process.stdout], [], [], 15)[0]:
                raise TimeoutError('private desktop stream timed out')
            return os.read(self.process.stdout.fileno(), size)
        def sendall(self, data):
            self.process.stdin.write(data); self.process.stdin.flush()
    with subprocess.Popen(['python3', '/app/worker.py', 'desktop'], stdin=subprocess.PIPE,
                          stdout=subprocess.PIPE, stderr=subprocess.DEVNULL) as process:
        try:
            process.stdin.write(json.dumps({'action': 'stream', 'id': ident}).encode() + b'\n'); process.stdin.flush()
            time.sleep(2)
            result = capture(ticket, worker.job_path(ident) / 'work/desktop.png', Peer(process))
        finally:
            process.terminate()
            try:
                process.wait(timeout=3)
            except subprocess.TimeoutExpired:
                process.kill(); process.wait(timeout=3)
    result.update(status='passed', provider=state['provider'], transport='worker desktop stdio bridge',
                  account_provided=False, provider_sign_in_verified=False, inference_api_key_configured=False,
                  process_limit=process_limit, processes_at_capture=process_count, process_limit_hits=limit_hits)
    (worker.job_path(ident) / 'work/desktop-proof.json').write_text(json.dumps(result, indent=2))
    print(json.dumps({'capture': 'passed', 'provider': state['provider']}))


def main():
    provider = sys.argv[1] if len(sys.argv) == 2 else 'chatgpt'
    if provider not in desktop.PROVIDERS:
        raise ValueError('select one official desktop program')
    ident = os.environ.get('USEVPS_JOB_ID')
    if not ident or os.getuid() != 10001:
        raise PermissionError('run the GUI probe as a normally reserved VPS calculation')
    directory = Path.cwd() / '.desktop-probe'
    directory.mkdir(mode=0o700); (directory / 'home').mkdir(mode=0o700)
    desktop.DIRECTORY = directory; desktop.STATE = directory / 'session.json'
    resources.write_json(desktop.STATE, {'id': ident, 'provider': provider, 'phase': 'preparing', 'started': time.time()})
    done = threading.Event(); result = {}; failure = []
    def verify():
        try:
            deadline = time.monotonic() + 1100
            while not done.wait(.25):
                state = desktop.record()
                if state.get('phase') == 'ready':
                    time.sleep(2)
                    result.update(capture(state, Path('desktop.png')))
                    return
                if time.monotonic() > deadline:
                    raise TimeoutError('real GUI did not become ready')
        except Exception as exc:
            failure.append(type(exc).__name__ + ': ' + str(exc))
        finally:
            (worker.job_path(ident) / '.desktop-stop').touch()
    thread = threading.Thread(target=verify, daemon=True); thread.start()
    try:
        desktop.run(ident)
    finally:
        done.set(); thread.join(timeout=20)
    if failure or not result:
        raise RuntimeError(failure[0] if failure else 'GUI probe did not complete')
    result.update(status='passed', provider=provider, account_provided=False, provider_sign_in_verified=False,
                  inference_api_key_configured=False)
    Path('desktop-proof.json').write_text(json.dumps(result, indent=2))
    print(json.dumps(result))


if __name__ == '__main__':
    if len(sys.argv) == 3 and sys.argv[1] == '--capture-session':
        capture_session(sys.argv[2])
    else:
        main()
