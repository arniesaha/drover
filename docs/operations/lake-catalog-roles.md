# Catalog group roles

`provision_catalog_roles(spec, prefix=...)` is an explicit admin operation on an
initialized, separate DuckLake catalog database. The tested role model follows
[DuckLake access control](https://ducklake.select/docs/stable/duckdb/guides/access_control).

It creates three NOLOGIN groups, without passwords or control-store grants:

- `<prefix>_reader`: CONNECT, schema USAGE, metadata SELECT.
- `<prefix>_exporter`: CONNECT, schema USAGE, metadata SELECT/INSERT/UPDATE/DELETE
  and sequence USAGE/SELECT. No schema ownership or CREATE permission.
- `<prefix>_admin`: owns the catalog schema, metadata tables and sequences;
  can initialize/evolve the catalog under explicit operator control.

Every group is NOSUPERUSER, NOCREATEDB, NOCREATEROLE, NOREPLICATION and
NOBYPASSRLS. PUBLIC database/schema privileges are revoked in this catalog.
Admin default privileges preserve reader/exporter access to future metadata
objects. Separate login credentials must be granted the appropriate group by
the operator or installer; they are not created or written into config here.

Reader/exporter privilege tests use an initdb-created scratch cluster and fresh
catalog. Reader lake queries succeed, exporter appends succeed, reader metadata
DELETE fails, and both reader/exporter PostgreSQL CREATE TABLE fail. Serving
credential wiring and installer provisioning remain release work.
