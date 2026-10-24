# Resource sharing

One bounded container runs several trusted jobs. A short exclusive lock protects
the reservation ledger; each job keeps a shared deployment lock. Admission waits
at most 30 seconds. Deployments need the exclusive lock and cannot interrupt work.

Admission reserves CPU, RAM and the remaining declared disk peak atomically.
It reads actual container/CPU limits, project quota, physical space and host
MemAvailable. A 256 MiB supervisor allowance and the configured host RAM floor
remain outside reservations. Four jobs is an upper bound, not a promise that
every workload fits.

Reserved cores are distinct. Idle cores are lent round-robin through Linux
process/thread affinity, refreshed while jobs run. Low container CPU shares let
neighboring projects reclaim CPU. Borrowing is not a throughput guarantee.

Each job has its own source/result directory and temporary home. Language caches
are reused. Positive shared-cache growth is charged once and split across jobs
active at measurement time; it is not precise per-writer accounting. Disk guards
cover each job's directory plus its share of cache growth. Existing cache bytes
already reduce capacity. Releasing another result does not change a job's usage.

The worker samples descendant memory (PSS, falling back to RSS), disk, host RAM,
logs and time. It stops only descendants of the failed task, with a child
subreaper for detached children. Final checks also cover short commands. A killed
supervisor leaves an unverified lease and blocks new admission; a controlled
restart closes old processes and marks unfinished receipts failed.

Container memory and ext4 project quota are kernel-enforced. Per-job checks can
overshoot between samples. Jobs share an identity and caches: trusted code only.

Health verifies identity, mounts and toolchain independently of admission. Full
disks or low RAM must not prevent retrieval of finished results. Fetch verifies
manifests; release removes only the explicitly selected completed job. No
automatic personal-data, result or useful-cache purge is performed.

Monitoring exposes bounded public labels, reservations and eight recent jobs,
without arguments, logs, source contents or credentials. The widget derives CPU
usage from differences between real cumulative samples.
