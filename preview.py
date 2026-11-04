"""Deliver only verified PNG data, never an image representing an error."""
import os
from pathlib import Path
import stat
import struct
import zlib

import vps


def png(project, path):
    root, _ = vps.source_directory(Path(project).expanduser())
    raw = Path(path)
    if raw.is_absolute():
        raw = raw.relative_to(root)
    parts = vps.safe_path(str(raw)).parts
    if not parts or not parts[-1].lower().endswith('.png'):
        raise ValueError('choose an existing verified PNG path')
    if any(p.casefold() == 'claude.md' or p in {'.git', '.ssh', '.config', '.agents', '.codex', '.claude', '.desktops', '.agent-auth'} for p in parts):
        raise ValueError('choose a verified project PNG outside private directories')
    fd = os.open(root, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW)
    try:
        for part in parts[:-1]:
            child = os.open(part, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW, dir_fd=fd)
            os.close(fd); fd = child
        source = os.open(parts[-1], os.O_RDONLY | os.O_NOFOLLOW, dir_fd=fd)
        with os.fdopen(source, 'rb') as f:
            info = os.fstat(f.fileno())
            if not stat.S_ISREG(info.st_mode) or info.st_nlink != 1 or not 33 <= info.st_size <= 8 * 1024**2:
                raise ValueError('preview requires a regular PNG of at most 8 MiB')
            data = f.read(8 * 1024**2 + 1)
    finally:
        os.close(fd)
    if data[:8] != b'\x89PNG\r\n\x1a\n' or data[12:16] != b'IHDR':
        raise ValueError('preview requires a valid PNG, not an error placeholder')
    width, height, depth, color, compression, filtering, interlace = struct.unpack('>IIBBBBB', data[16:29])
    if not (0 < width <= 4096 and 0 < height <= 4096 and depth == 8 and color in (2, 6)
            and compression == filtering == interlace == 0):
        raise ValueError('preview supports noninterlaced 8-bit RGB/RGBA PNG up to 4096x4096')
    offset, compressed, ended = 8, bytearray(), False
    while offset + 12 <= len(data):
        size = int.from_bytes(data[offset:offset + 4], 'big')
        stop = offset + 12 + size
        if stop > len(data):
            raise ValueError('truncated PNG; preserve the transfer receipt')
        chunk = data[offset + 4:offset + 8 + size]
        if zlib.crc32(chunk) != int.from_bytes(data[offset + 8 + size:stop], 'big'):
            raise ValueError('PNG checksum mismatch; preserve the transfer receipt')
        if chunk[:4] == b'IDAT':
            compressed.extend(chunk[4:])
        if chunk[:4] == b'IEND':
            ended = size == 0 and stop == len(data)
            break
        offset = stop
    stride = 1 + width * (3 if color == 2 else 4)
    decoder = zlib.decompressobj()
    pixels = decoder.decompress(compressed, stride * height + 1)
    if not ended or not decoder.eof or decoder.unused_data or len(pixels) != stride * height or any(pixels[n] > 4 for n in range(0, len(pixels), stride)):
        raise ValueError('invalid PNG data; report text and do not retry image viewing')
    return data
