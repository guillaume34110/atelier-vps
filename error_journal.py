"""Private, bounded diagnostic metadata; original job data is never rotated."""
from collections import Counter
import fcntl
import hashlib
import json
import os
from pathlib import Path
import re
import stat
import time
import uuid

from diagnostics import redact

LIMIT = 1024 * 1024
COPIES = 15


def directory():
    path = Path(os.environ.get('ATELIER_VPS_ERROR_DIR', Path.home() / '.local/state/atelier-vps/errors'))
    path.mkdir(parents=True, exist_ok=True, mode=0o700)
    if path.is_symlink() or path.stat().st_uid != os.getuid() or path.stat().st_mode & 0o077:
        raise ValueError('error journal requires a private owned directory')
    return path


def regular(path):
    if path.exists() or path.is_symlink():
        info = path.lstat()
        if not stat.S_ISREG(info.st_mode) or info.st_nlink != 1 or info.st_uid != os.getuid():
            raise ValueError('unsafe error journal file')


def locked(root):
    fd = os.open(root / '.lock', os.O_WRONLY | os.O_CREAT | os.O_NOFOLLOW, 0o600)
    info = os.fstat(fd)
    if not stat.S_ISREG(info.st_mode) or info.st_nlink != 1 or info.st_uid != os.getuid():
        os.close(fd)
        raise ValueError('unsafe error journal lock')
    f = os.fdopen(fd, 'w')
    fcntl.flock(f, fcntl.LOCK_EX)
    return f


def record(operation, error, *, server=None, job_ref=None, code='error'):
    """Diagnostics must never change an operation's result."""
    try:
        root = directory()
        text = redact(str(error))
        text = re.sub(r'(?i)([?&](?:code|ticket|state)=)[^\s&#]+', r'\1[redacted]', text)
        event = {'id': uuid.uuid4().hex, 'time': time.time(), 'operation': redact(str(operation))[:64],
                 'code': redact(str(code))[:64], 'error': text}
        if isinstance(server, str) and re.fullmatch('[a-zA-Z0-9_.-]{1,64}', server):
            event['server'] = server
        if isinstance(job_ref, str) and re.fullmatch('[a-zA-Z0-9_.-]{1,64}:[a-f0-9]{32}', job_ref):
            event['job_ref'] = job_ref
        signature = re.sub('[a-f0-9]{32,64}|[0-9]+', '<id>', text)[:512]
        key = hashlib.sha256((event['operation'] + '\0' + event['code'] + '\0' + signature).encode()).hexdigest()
        with locked(root):
            journal = root / 'events.jsonl'
            totals = root / 'totals.json'
            regular(journal); regular(totals)
            if totals.exists() and totals.stat().st_size > LIMIT:
                raise ValueError('error totals exceed their budget')
            counts = json.loads(totals.read_text()) if totals.exists() else {}
            if not isinstance(counts, dict) or len(counts) > 257 or any(not isinstance(v, dict) for v in counts.values()):
                raise ValueError('invalid error totals')
            if key not in counts and len(counts) >= 256:
                key = 'other'
            previous = counts.get(key)
            counts[key] = {'count': (previous or {}).get('count', 0) + 1,
                           'first': (previous or {}).get('first', event['time']), 'last': event['time'],
                           'operation': event['operation'], 'code': event['code'], 'error': signature}
            data = (json.dumps(event, ensure_ascii=True) + '\n').encode()
            if journal.exists() and journal.stat().st_size + len(data) > LIMIT:
                for index in range(COPIES, 0, -1):
                    target = root / ('events.jsonl.' + str(index))
                    source = root / ('events.jsonl.' + str(index - 1)) if index > 1 else journal
                    regular(target); regular(source)
                    if index == COPIES:
                        target.unlink(missing_ok=True)
                    if source.exists():
                        source.replace(target)
            fd = os.open(journal, os.O_WRONLY | os.O_CREAT | os.O_APPEND | os.O_NOFOLLOW, 0o600)
            with os.fdopen(fd, 'ab') as out:
                out.write(data)
            temporary = root / '.totals.tmp'
            regular(temporary)
            fd = os.open(temporary, os.O_WRONLY | os.O_CREAT | os.O_TRUNC | os.O_NOFOLLOW, 0o600)
            with os.fdopen(fd, 'w') as out:
                json.dump(counts, out)
            temporary.replace(totals)
        return event['id']
    except (OSError, ValueError, TypeError):
        return None


def report(hours=24):
    if type(hours) is not int or not 1 <= hours <= 168:
        raise ValueError('report hours must be 1..168')
    root = directory()
    since = time.time() - hours * 3600
    events = []
    with locked(root):
        for index in range(COPIES, -1, -1):
            path = root / ('events.jsonl' + ('.' + str(index) if index else ''))
            regular(path)
            if not path.exists():
                continue
            if path.stat().st_size > LIMIT:
                raise ValueError('error journal exceeds its budget')
            for line in path.read_text().splitlines():
                try:
                    value = json.loads(line)
                    if value['time'] >= since:
                        events.append(value)
                except (ValueError, KeyError, TypeError):
                    continue
        totals = root / 'totals.json'
        regular(totals)
        if totals.exists() and totals.stat().st_size > LIMIT:
            raise ValueError('error totals exceed their budget')
        counts = json.loads(totals.read_text()) if totals.exists() else {}
    return {'ok': True, 'since': since, 'count': len(events),
            'by_operation': dict(Counter(x['operation'] for x in events)),
            'events': events[-512:], 'events_limited': len(events) > 512,
            'lifetime_totals': counts, 'scope': 'Client operations since this logger was installed; original remote receipts remain authoritative.'}
