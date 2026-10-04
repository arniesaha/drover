# Hub process RSS budget

The server samples its own process RSS every second. The default budget is
4 GiB; warn starts at 80% of that budget, and over starts above the budget.
An over-budget transition logs a warning, repeated at most once per minute
while it persists. This guard is observable alerting; it does not restart the
service or change the control-store readiness verdict.

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
    "rss_bytes": 123456789,
    "budget_bytes": 4294967296,
    "state": "ok",
    "peak_rss_bytes": 123456789,
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
without private job error details. RSS sampling failures return a null RSS,
warn state and `measurement_error=true`, rather than a false ok measurement.

Query children are registered by the disposable query supervisor and reaped
on success or failure. Their RSS is excluded from the server measurement;
their lifetime counts and sampled peak are reported separately. A cached
store-readiness verdict does not freeze the live process memory snapshot.
