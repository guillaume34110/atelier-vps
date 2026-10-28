"""Conservative, on-demand admission for a private macOS worker."""
import math
import os
import re
import subprocess

GIB = 1024 ** 3
MIB = 1024 ** 2


def system(command):
    result = subprocess.run(command, stdout=subprocess.PIPE, stderr=subprocess.DEVNULL,
                            text=True, timeout=5, check=True)
    return result.stdout


def memory_stats(pressure, statistics, total):
    percent = re.search(r'System-wide memory free percentage:\s*(\d+)%', pressure)
    page = re.search(r'page size of (\d+) bytes', statistics)
    values = []
    for label in ('Pages free', 'Pages inactive', 'Pages speculative'):
        match = re.search(r'^' + re.escape(label) + r':\s*(\d+)\.', statistics, re.MULTILINE)
        if not match:
            raise ValueError('memory availability unavailable')
        values.append(int(match.group(1)))
    if not percent or not page or not 0 <= int(percent.group(1)) <= 100:
        raise ValueError('memory pressure unavailable')
    percent = int(percent.group(1))
    reclaimable = sum(values) * int(page.group(1))
    return percent, min(reclaimable, total * percent // 100)


def available(request, *, cores, load, total, percent, reclaimable):
    if not 1 <= cores <= 1024 or not math.isfinite(load) or load < 0 or total <= 0:
        raise ValueError('system capacity unavailable')
    spare_cpu = max(0, cores - math.ceil(load) - 1)
    spare_memory = max(0, reclaimable - max(4 * GIB, total // 4))
    if percent < 35:
        return False, 'memory pressure'
    if request['cpu'] > spare_cpu:
        return False, 'CPU busy'
    host_claim = (request['ram_mib'] * MIB * 5 + 3) // 4 + 64 * MIB
    if host_claim > spare_memory:
        return False, 'RAM unavailable'
    return True, None


def check(request):
    cores = os.cpu_count()
    total = int(system(['/usr/sbin/sysctl', '-n', 'hw.memsize']).strip())
    percent, reclaimable = memory_stats(system(['/usr/bin/memory_pressure', '-Q']),
                                        system(['/usr/bin/vm_stat']), total)
    return available(request, cores=cores, load=os.getloadavg()[0], total=total,
                     percent=percent, reclaimable=reclaimable)
