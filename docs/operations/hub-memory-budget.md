# Hub process memory budget

The server samples its own process memory every second. The default budget is
4 GiB; warn starts at 80% of that budget, and over starts above the budget.
An over-budget transition logs a warning, repeated at most once per minute
while it persists. This guard is observable alerting; it does not restart the
service, refuse query admission, or change the control-store readiness
verdict (`/readyz` stays 200 while `memory.state` is `over`).

What is measured depends on the platform, and `memory.measurement` says which:

- **Linux: `rss`.** Resident set size.
- **macOS: `phys_footprint`** (`proc_pid_rusage`, the number Activity Monitor
  and `footprint` report). On macOS, RSS also counts pages the allocator has
  freed but keeps resident ("Malloc Small (empty)" in `vmmap`), so RSS
  never comes back down after a peak. On 2026-10-06 the v2 hub showed RSS
  4.54 GB against a footprint of 266 MB, and the rolled-back legacy hub
  showed RSS 7.6 GB against 923 MB. Both read `over` on RSS. RSS is still
  reported as `rss_bytes` for diagnosis. `lifetime_peak_footprint_bytes` is
  the kernel's own high-water mark, so a startup spike between two samples
  still shows.

Each `startup phase … completed` log line and the `analytical Parquet views
ready` line end with the same reading (`phys_footprint=… lifetime_peak=…
rss=…` on macOS). The first line whose `lifetime_peak` jumps names the phase
that set the peak.

```toml
[memory]
rss_budget_bytes = 4294967296
sample_interval_seconds = 1.0
warn_fraction = 0.8
```

`/readyz` and `drover_data_quality` include the same process memory block:

```json
{
  "memory": {
    "measurement": "phys_footprint",
    "measured_bytes": 266000000,
    "peak_measured_bytes": 301000000,
    "lifetime_peak_footprint_bytes": 3400000000,
    "rss_bytes": 4540000000,
    "budget_bytes": 4294967296,
    "state": "ok",
    "peak_rss_bytes": 4547936256,
    "measurement_error": false,
    "sample_interval_seconds": 1.0,
    "query_children": {
      "active": 0,
      "total": 4,
      "completed": 4,
      "peak_rss_bytes": 234567890,
      "rss_ceiling_bytes": 2147483648
    }
  }
}
```

The derived-memory vector/job/embedding fields already in readiness remain in
this block. Anonymous readiness callers receive process counts/measurements,
without private job error details. Sampling failures return a null
`measured_bytes`, warn state and `measurement_error=true`, rather than a false
ok measurement. `state` compares `measured_bytes` with `budget_bytes`; the
config key is still `rss_budget_bytes`. `lifetime_peak_footprint_bytes` is
present only on macOS.

Query children are registered by the disposable query supervisor and reaped
on success or failure. Their RSS is excluded from the server measurement;
their lifetime counts and sampled peak are reported separately. A cached
store-readiness verdict does not freeze the live process memory snapshot.
