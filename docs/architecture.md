# Resource sharing

One bounded container per authenticated developer runs trusted jobs. Opportunistic
mode keeps one developer on spare host capacity; allocated mode separates workers,
private mounts and quotas. [Administrator setup and limits](team.md).
A short exclusive lock protects
the reservation ledger; each job keeps a shared deployment lock. Admission waits
at most 30 seconds. Deployments need the exclusive lock and cannot interrupt work.

Admission reserves CPU, RAM and the remaining declared disk peak atomically.
It reads actual container/CPU limits, project quota, physical space and host
MemAvailable. A supervisor allowance of 64 MiB per task slot and the configured
host RAM floor remain outside reservations. Four total slots is an upper bound, not a promise that
every workload fits.

Reserved cores are distinct. Idle cores are lent round-robin through Linux
process/thread affinity, refreshed while jobs run. Low container CPU shares let
neighboring projects reclaim CPU. Developer CPU weights total at most 128;
allocated containers also have individual CPU ceilings. Borrowing is not a
throughput guarantee.

Each job has its own source/result directory and temporary home. Language caches
are reused within that developer's worker, never across writable developer spaces.
Positive shared-cache growth is charged once and split across jobs
active at measurement time; it is not precise per-writer accounting. Disk guards
cover each job's directory plus its share of cache growth. Existing cache bytes
already reduce capacity. Releasing another result does not change a job's usage.

Automatic RAM reservations reuse the largest observed peak plus 25% and 64 MiB,
rounded up. New workloads start conservatively; explicit peaks remain available.
Reservations never shrink merely to fit free memory. Histories are bounded and
private; dependency-lock changes start a new profile. See the [RAM guide](team.md).

The worker samples descendant memory (PSS, falling back to RSS), disk, host RAM,
logs and time. It stops only descendants of the failed task, with a child
subreaper for detached children. Final checks also cover short commands. A killed
supervisor leaves an unverified lease and blocks new admission; a controlled
restart closes old processes and marks unfinished receipts failed. Receipts keep
the observed maximum separately from final RAM. Child resource counters retain
short-lived RSS peaks between samples; aggregate sub-sample peaks remain approximate.

Container memory and ext4 project quota are kernel-enforced. Per-job checks can
overshoot between samples. Jobs inside a developer's worker share an identity:
trusted code only. An inherited seccomp filter prevents project-ID and inode-flag
changes from evading the workspace quota, while retaining Docker's default filter.

Health verifies identity, mounts and toolchain independently of admission. Full
disks or low RAM must not prevent retrieval of finished results. Fetch verifies
manifests; release removes only the explicitly selected completed job. No
automatic personal-data, result or useful-cache purge is performed.

Managed dependency downloads live under `cache/managed/<project-lock-hash>`.
The hash uses the public project label and lockfile digest, not the command, so
tests and builds reuse a scope. Starting any task in that scope refreshes its
explicit last-used marker; this does not depend on filesystem atime. After 30
unused days, an hourly idle pass may remove the complete marked scope. Retention
is conservative at scope level, not a claim to observe each package import.
The queue lock is exclusive and nonblocking, and live leases independently block
cleanup. At most eight scopes are removed per pass. Links, missing/invalid markers,
legacy untracked caches, immutable tools and all job directories stay intact.
The next use recreates dependencies from lockfiles within its declared budget.

Monitoring exposes bounded public labels, reservations and eight recent jobs,
without arguments, logs, source contents or credentials. The widget derives CPU
usage from differences between real cumulative samples.
