"""Eight private failure cards; original receipts and job files are never purged."""
import fcntl
import hashlib
import io
import json
import math
import re
import tarfile

import resources
from telemetry import public_label

LIMIT = 32 * 1024
COUNT = 8
KINDS = {'memory', 'host_memory', 'disk', 'timeout', 'logs', 'restart', 'command', 'signal', 'internal'}


def redact(text):
    text = re.sub(r'\x1b\[[0-?]*[ -/]*[@-~]', '', text)
    text = ''.join(c for c in text if c.isprintable() or c in '\n\t')
    text = re.sub(r'-----BEGIN[^\n]*PRIVATE KEY-----.*?(?:-----END[^\n]*PRIVATE KEY-----|$)',
                  '[redacted private key]', text, flags=re.S)
    text = re.sub(r'(?i)((?:Bearer|Basic)\s+)[^\s,;]+', r'\1[redacted]', text)
    text = re.sub(r'''(?i)((?:password|passwd|secret(?:[_-]?access)?[_-]?key|secret|token|api[_-]?key|authorization|credential|signature)["']?\s*[:=]\s*["']?)[^\s,"']+''',
                  r'\1[redacted]', text)
    text = re.sub(r'(?i)(https?://)[^\s/@]+:[^\s/@]+@', r'\1[redacted]@', text)
    text = re.sub(r'(?:gh[pousr]_[A-Za-z0-9]{8,}|github_pat_[A-Za-z0-9_]+|sk-[A-Za-z0-9_-]{10,}|AKIA[A-Z0-9]{16})',
                  '[redacted]', text)
    text = re.sub(r'(?m)^[A-Za-z0-9+/=_-]{40,}$', '[redacted]', text)
    return text[:4096]


def valid(report, ident):
    return (isinstance(ident, str) and re.fullmatch('[0-9a-f]{32}', ident) is not None
            and isinstance(report, dict) and report.get('version') == 1 and report.get('id') == ident
            and report.get('kind') in KINDS
            and all(isinstance(report.get(k), str) and len(report[k]) <= size
                    for k, size in [('agent', 64), ('project', 64), ('reason', 1024), ('log_tail', 4096)])
            and type(report.get('returncode')) is int and -65535 <= report['returncode'] <= 65535
            and all(type(report.get(k)) in {int, float} and math.isfinite(report[k]) and 0 <= report[k] < 1e11
                    for k in ['started', 'ended'])
            and isinstance(report.get('resources'), dict) and len(report['resources']) <= 8
            and all(type(v) in {int, float} and math.isfinite(v) and 0 <= v < 2**50
                    for v in report['resources'].values()))


def reports(root):
    path = root / '.failure-details.json'
    try:
        if path.is_symlink() or path.stat().st_size > COUNT * LIMIT + 1024:
            return {}
        data = json.loads(path.read_text())
        if not isinstance(data, dict) or len(data) > COUNT:
            return {}
        return {k: v for k, v in data.items() if re.fullmatch('[0-9a-f]{32}', k) and valid(v, k)}
    except (OSError, ValueError):
        return {}


def build(path, state):
    reason = state.get('reason') or ''
    code = state.get('returncode', 1)
    kind = {'task memory limit': 'memory', 'host memory pressure': 'host_memory',
            'disk limit': 'disk', 'timeout': 'timeout', 'log limit': 'logs',
            'log capture failed': 'logs', 'worker restarted': 'restart',
            'quota prevents receipt': 'disk'}.get(reason, 'internal' if reason else 'signal' if code < 0 else 'command')
    tail = ''
    log = path / 'stdout.log'
    try:
        if not log.is_symlink():
            with log.open('rb') as source:
                source.seek(0, 2)
                source.seek(max(0, source.tell() - 4096))
                tail = source.read(4096).decode(errors='replace')
    except OSError:
        pass
    measured = state.get('resources') or {}
    reservation = state.get('reservation') or {}
    values = {k: measured[k] for k in ['ram_peak_bytes', 'growth_bytes', 'declared_disk_bytes', 'cpus'] if k in measured}
    values.update({k: reservation[source] for k, source in [('reserved_ram_bytes', 'ram_bytes')]
                   if source in reservation})
    if 'timeout' in state:
        values['timeout_seconds'] = state['timeout']
    report = dict(version=1, id=state['id'], kind=kind, returncode=code,
                  agent=public_label(state.get('agent'), 'Agent non identifié'),
                  project=public_label(state.get('project'), 'Projet non identifié'),
                  started=state['started'], ended=state['ended'], reason=redact(reason)[:1024],
                  resources=values, log_tail=redact(tail))
    if not valid(report, state['id']) or len(json.dumps(report).encode()) > LIMIT:
        raise ValueError('invalid failure details')
    return report


def remember(root, path, state):
    if state.get('status') != 'failed':
        return
    try:
        report = build(path, state)
        with (root / '.failure-details.lock').open('a') as lock:
            fcntl.flock(lock, fcntl.LOCK_EX)
            values = reports(root)
            values.pop(state['id'], None)
            values[state['id']] = report
            resources.write_json(root / '.failure-details.json', dict(list(values.items())[-COUNT:]))
    except (OSError, ValueError, KeyError, TypeError):
        pass  # Optional UI metadata must not alter the task's result or receipt.


def get(root, path):
    cached = reports(root).get(path.name)
    if cached:
        return cached
    receipt = path / 'receipt.json'
    if receipt.is_symlink() or receipt.stat().st_size > 65536:
        raise ValueError('failure details unavailable')
    state = json.loads(receipt.read_text())
    if state.get('status') != 'failed':
        raise ValueError('task has no failure details')
    return build(path, state)


def bundle(report):
    data = json.dumps(report).encode()
    manifest = json.dumps({'diagnostic.json': {'bytes': len(data), 'sha256': hashlib.sha256(data).hexdigest()}}).encode()
    output = io.BytesIO()
    with tarfile.open(fileobj=output, mode='w') as archive:
        for name, body in [('manifest.json', manifest), ('diagnostic.json', data)]:
            info = tarfile.TarInfo(name); info.size = len(body)
            archive.addfile(info, io.BytesIO(body))
    return output.getvalue()
