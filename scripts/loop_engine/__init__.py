"""Phase 0 of the Loop Engine: an instrumented manual loop.

Deliberately outside ``src/drover/server``. Decision D4 of the Phase 0 design:
the driver speaks to Drover over its public harness API, adds no
``pipeline_jobs`` job kind, and does not run inside the server process. That is
what keeps decision D3 -- where control-plane state lives -- genuinely deferred:
a scratch table can be dropped, a job kind cannot.

The deliverable of Phase 0 is evidence, not a schema and not a bug count. A run
that finds nothing but produces trustworthy instrumentation has succeeded.
"""
