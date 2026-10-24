"""Stack a tiny seccomp filter: a workspace owner must not change quota IDs."""
import ctypes
import errno
import platform


def protect_quota():
    architectures = {'aarch64': (0xC00000B7, 29), 'arm64': (0xC00000B7, 29),
                     'x86_64': (0xC000003E, 16), 'amd64': (0xC000003E, 16)}
    try:
        audit, ioctl = architectures[platform.machine().lower()]
    except KeyError:
        raise RuntimeError('quota protection requires Linux ARM64 or x86_64') from None

    class Filter(ctypes.Structure):
        _fields_ = [('code', ctypes.c_ushort), ('jt', ctypes.c_ubyte),
                    ('jf', ctypes.c_ubyte), ('k', ctypes.c_uint32)]

    class Program(ctypes.Structure):
        _fields_ = [('length', ctypes.c_ushort), ('filters', ctypes.POINTER(Filter))]

    # seccomp_data: nr at 0, arch at 4, args[1] (ioctl request) at 24.
    # Reject compatibility ABIs so they cannot use different syscall numbers.
    instructions = [
        (0x20, 0, 0, 4), (0x15, 1, 0, audit), (0x06, 0, 0, 0x50000 | errno.ENOSYS),
        (0x20, 0, 0, 0), (0x35, 0, 1, 0x40000000), (0x06, 0, 0, 0x50000 | errno.ENOSYS),
        (0x15, 0, 4, ioctl), (0x20, 0, 0, 24),
        (0x15, 3, 0, 0x401C5820),  # FS_IOC_FSSETXATTR: project ID and inheritance.
        (0x15, 2, 0, 0x40086602),  # FS_IOC_SETFLAGS on a 64-bit ABI.
        (0x15, 1, 0, 0x40046602),
        (0x06, 0, 0, 0x7FFF0000), (0x06, 0, 0, 0x50000 | errno.EPERM),
    ]
    code = (Filter * len(instructions))(*(Filter(*row) for row in instructions))
    program = Program(len(instructions), ctypes.cast(code, ctypes.POINTER(Filter)))
    libc = ctypes.CDLL(None, use_errno=True)
    if libc.prctl(38, 1, 0, 0, 0) or libc.prctl(22, 2, ctypes.byref(program), 0, 0):
        raise OSError(ctypes.get_errno(), 'cannot protect workspace quota attributes')
