--
-- PostgreSQL database dump
--


-- Dumped from database version 17.11 (Homebrew)
-- Dumped by pg_dump version 17.11 (Homebrew)

SET statement_timeout = 0;
SET lock_timeout = 0;
SET idle_in_transaction_session_timeout = 0;
SET transaction_timeout = 0;
SET client_encoding = 'UTF8';
SET standard_conforming_strings = on;
SELECT pg_catalog.set_config('search_path', '', false);
SET check_function_bodies = false;
SET xmloption = content;
SET client_min_messages = warning;
SET row_security = off;

--
-- Data for Name: control_schema_migrations; Type: TABLE DATA; Schema: drover_control; Owner: -
--

COPY drover_control.control_schema_migrations (version, applied_at) FROM stdin;
1	2026-09-21 17:42:48.930699-07
2	2026-09-21 17:42:48.930699-07
3	2026-09-21 17:42:48.930699-07
4	2026-10-01 02:13:55.065406-07
5	2026-10-01 22:32:06.980955-07
6	2026-10-01 22:32:06.980955-07
7	2026-10-02 07:18:14.578477-07
8	2026-10-02 07:29:57.213426-07
9	2026-10-03 15:56:47.094096-07
10	2026-10-03 15:56:47.094096-07
11	2026-10-04 15:59:30.125302-07
\.


--
-- Data for Name: control_store_initialization; Type: TABLE DATA; Schema: drover_control; Owner: -
--

COPY drover_control.control_store_initialization (singleton, state, mode, source_fingerprint, source_timezone, verified_at, details_json, updated_at) FROM stdin;
t	ready	import	4e5b0ee279fb3046d008a4cda2d8900e11fab5a41e42cd52bed43d833583ab58	America/Los_Angeles	2026-09-21 17:42:57.08419-07	{"ok":true,"recovery":"forward-recovery-only-after-writes","relationships":{"events_missing_session":0,"sessions_missing_host":0},"tables":{"advisory_findings":{"actual":78,"expected":78,"match":true},"advisory_occurrences":{"actual":6794,"expected":6794,"match":true},"control_credentials":{"actual":13,"expected":13,"match":true},"control_server_identity":{"actual":2,"expected":2,"match":true},"harness_events":{"actual":78596,"expected":78596,"match":true},"harness_hosts":{"actual":4,"expected":4,"match":true},"harness_sessions":{"actual":212,"expected":212,"match":true},"live_recap_jobs":{"actual":147,"expected":147,"match":true},"live_session_recaps":{"actual":147,"expected":147,"match":true},"native_usage_partition_totals":{"actual":598,"expected":598,"match":true},"native_usage_partition_watermarks":{"actual":227,"expected":227,"match":true},"session_usage":{"actual":800,"expected":800,"match":true},"session_usage_sources":{"actual":1328,"expected":1328,"match":true}}}	2026-09-21 17:42:57.08419-07
\.


--
-- PostgreSQL database dump complete
--
