import assert from "node:assert/strict";
import { createServer } from "node:http";
import { readFileSync } from "node:fs";
import { test } from "node:test";
import ownerPlugin, { register, TOOL_NAME } from "../index.js";
import { fakeApi } from "./fake-sdk.js";

const OWNER = "agent:coder:subagent:mock-owner";
const PARENT = "agent:main:mock-parent";
const OWNER_PRINCIPAL = "legacy:operator";
const EVENT = "a".repeat(64);
const MODEL_CREDENTIAL = "fixture-model-input";
const action = {
  action_id: "observer-action-" + EVENT, event_id: EVENT, type: "review_worker_result",
  authority_scope: "implementation", authorized: true,
};

function projection(overrides = {}) {
  return {
    version: 1, run_id: "run_PLUGIN497", session_id: "mock-observer", expected_revision: 4,
    authority: "taskflow", objective: "Canary", checkpoint: "Waiting for explicit owner",
    authority_scope: "implementation", scope_limit: "commit_only",
    owner_handoff: { authorized_owner_id: OWNER_PRINCIPAL, state: "owner_lease_required" },
    owner: { id: OWNER_PRINCIPAL, epoch: 1, lease_until: "2099-10-03T00:00:00+00:00", live: true },
    worker_state: "brief_completed", terminal_release: false, next_action: null,
    recovery: "continue_owner", owner_wake: true, inbox_counts: { pending: 1 },
    completion_receipts: { counts: {}, errors: [] },
    events: [], has_more: true, ...overrides,
  };
}

async function fixture(t, handler = (_req, res) => res.end(JSON.stringify({ continuity: projection() }))) {
  const calls = [];
  const server = createServer(async (req, res) => {
    const parts = [];
    for await (const chunk of req) parts.push(chunk);
    const body = parts.length ? JSON.parse(Buffer.concat(parts)) : null;
    calls.push({ method: req.method, url: req.url, authorization: req.headers.authorization, body });
    res.setHeader("Content-Type", "application/json");
    handler(req, res, body);
  });
  await new Promise((resolve) => server.listen(0, "127.0.0.1", resolve));
  t.after(async () => {
    server.closeAllConnections();
    await new Promise((resolve) => server.close(resolve));
    delete process.env.DROVER_PLUGIN_TEST_TOKEN;
  });
  process.env.DROVER_PLUGIN_TEST_TOKEN = "synthetic-canary-token";
  const config = {
    enabled: true, droverOrigin: `http://127.0.0.1:${server.address().port}`,
    tokenEnvName: "DROVER_PLUGIN_TEST_TOKEN", runId: "run_PLUGIN497",
    ownerSessionKey: OWNER, ownerPrincipal: OWNER_PRINCIPAL, canarySessionKey: PARENT, authorityScope: "implementation",
  };
  return { config, calls, server };
}

function tool(config, sessionKey = OWNER) {
  const { api, registrations } = fakeApi(config);
  register(api);
  assert.equal(registrations.length, 1);
  return registrations[0].factory({ sessionKey, assertInvocationCurrent() {} });
}
const call = (operation, fields = {}) => ({ version: 1, request: { operation, ...fields } });

async function eventually(predicate, message = "condition was not reached") {
  for (let index = 0; index < 100; index++) {
    if (predicate()) return;
    await new Promise((resolve) => setTimeout(resolve, 5));
  }
  assert.fail(message);
}

function serviceContext(pluginConfig) {
  const failures = [];
  return {
    failures,
    context: {
      config: { plugins: { entries: { "drover-continuity-owner": { config: pluginConfig } } } },
      logger: { warn(message) { failures.push(message); } },
      serviceHealth: {
        reportFailure(error) { failures.push(error); },
        clearFailure() {},
      },
    },
  };
}

test("exposes static tool authoring metadata required by OpenClaw pack", () => {
  const metadata = ownerPlugin[Symbol.for("openclaw.plugin-sdk.tool-plugin.metadata")];
  assert.deepEqual(metadata, {
    id: "drover-continuity-owner",
    name: "Drover Continuity Owner",
    description: "Fenced continuity owner transport with exact-session wake dispatch; no executor",
    activation: { onStartup: true },
    configSchema: JSON.parse(readFileSync(new URL("../openclaw.plugin.json", import.meta.url), "utf8")).configSchema,
    tools: [{
      name: TOOL_NAME,
      label: "Drover Continuity Owner",
      description: "Explicit Drover continuity calls for the bound owner. Advisory actions only; no executor, merge, deployment or messaging. Ack only after idempotent reconciliation.",
      parameters: JSON.parse(readFileSync(new URL("../schemas/request.json", import.meta.url), "utf8")),
      optional: true,
    }],
  });
});

test("registers exactly one optional normal tool; default inactive and unknown contexts get none", () => {
  const { api, registrations, services } = fakeApi();
  register(api);
  assert.equal(registrations.length, 1);
  assert.equal(services.length, 1);
  assert.equal(registrations[0].factory({ sessionKey: OWNER, assertInvocationCurrent() {} }), null);
  assert.equal(registrations[0].factory({ assertInvocationCurrent() {} }), null);
});

test("owner mode acquires the trusted lease and wakes only the exact bound owner session", async (t) => {
  const live = projection({
    owner: { id: OWNER_PRINCIPAL, epoch: 2, lease_until: "2099-10-03T00:00:00+00:00", live: true },
    events: [{ event_id: EVENT, state: "pending", attempts: 0, next_delivery_at: null,
      report: { source: "drover", source_event_id: "worker-1", subject: "worker", sequence: 1,
        kind: "worker_brief_completed", summary: "Worker completed" } }],
  });
  const stale = projection({ owner: { id: null, epoch: 1, lease_until: null, live: false }, owner_wake: false });
  const f = await fixture(t, (req, res, body) => res.end(JSON.stringify({
    continuity: req.method === "POST" && body.operation === "lease" ? live : stale,
  })));
  const ownerConfig = { ...f.config, mode: "owner", wakePollIntervalMs: 1000 };
  const runtime = fakeApi(f.config); // Registered while poll-only; committed reload config activates it.
  register(runtime.api);
  assert.equal(runtime.services.length, 1);
  const health = serviceContext(ownerConfig);
  runtime.services[0].start(health.context);
  await eventually(() => runtime.wakeRequests.length === 1, "owner wake was not dispatched");
  await runtime.services[0].stop(health.context);
  assert.deepEqual(f.calls.map((request) => request.method), ["GET", "GET", "POST"]);
  assert.equal(f.calls[2].body.operation, "lease");
  assert.equal(f.calls[2].body.owner_id, OWNER_PRINCIPAL);
  assert.equal(runtime.systemEvents.length, 1);
  assert.deepEqual(runtime.systemEvents[0].options, {
    sessionKey: OWNER,
    contextKey: `run_PLUGIN497:${EVENT}`,
  });
  assert.deepEqual(runtime.wakeRequests[0], {
    source: "other", intent: "immediate", reason: "drover-continuity-owner-wake",
    agentId: "coder", sessionKey: OWNER, heartbeat: { target: "last" },
  });
  assert.match(runtime.systemEvents[0].text, /acknowledge only after.*durably verified/);
  assert.equal(health.failures.length, 0);
});

test("a coordinator run authorized to the agent session key is leased and woken as that owner", async (t) => {
  const bound = (extra = {}) => projection({
    owner_handoff: { authorized_owner_id: OWNER, state: "owner_lease_required" },
    ...extra,
  });
  const live = bound({
    owner: { id: OWNER, epoch: 2, lease_until: "2099-10-03T00:00:00+00:00", live: true },
    events: [{ event_id: EVENT, state: "pending", attempts: 0, next_delivery_at: null,
      report: { source: "drover-work-coordinator-idle", source_event_id: "worker-1", subject: "worker", sequence: 1,
        kind: "worker_brief_completed", summary: "idle_undeclared: final output" } },
      { event_id: "b".repeat(64), state: "pending", attempts: 0, next_delivery_at: null,
        report: { source: "drover-work-coordinator", source_event_id: "launch:1", subject: "worker", sequence: 0,
          kind: "worker_started", summary: "Work coordinator launched the bound worker." } }],
  });
  const stale = bound({ owner: { id: null, epoch: 1, lease_until: null, live: false }, owner_wake: false });
  const f = await fixture(t, (req, res, body) => res.end(JSON.stringify({
    continuity: req.method === "POST" && body.operation === "lease" ? live : stale,
  })));
  const ownerConfig = { ...f.config, mode: "owner", wakePollIntervalMs: 1000 };
  const runtime = fakeApi(f.config);
  register(runtime.api);
  const health = serviceContext(ownerConfig);
  runtime.services[0].start(health.context);
  await eventually(() => runtime.wakeRequests.length === 1, "owner wake was not dispatched");
  await runtime.services[0].stop(health.context);
  assert.equal(f.calls[2].body.operation, "lease");
  assert.equal(f.calls[2].body.owner_id, OWNER);
  assert.equal(health.failures.length, 0);
});

test("wake retries are bounded and durable acknowledgement suppresses replay", async (t) => {
  let polls = 0;
  const pending = projection({ events: [{ event_id: EVENT, state: "pending", attempts: 0, next_delivery_at: null,
    report: { source: "drover", source_event_id: "worker-1", subject: "worker", sequence: 1,
      kind: "worker_brief_completed", summary: "Worker completed" } }] });
  const acknowledged = projection({ owner_wake: false, inbox_counts: { acknowledged: 1 }, events: [], has_more: false });
  const f = await fixture(t, (_req, res) => {
    polls++;
    res.end(JSON.stringify({ continuity: polls >= 2 ? acknowledged : pending }));
  });
  const runtime = fakeApi({ ...f.config, mode: "owner", wakePollIntervalMs: 10, wakeRetryIntervalMs: 1000 });
  register(runtime.api);
  const health = serviceContext(runtime.api.pluginConfig);
  runtime.services[0].start(health.context);
  await eventually(() => polls >= 3);
  await runtime.services[0].stop(health.context);
  assert.equal(runtime.wakeRequests.length, 1);
  assert.equal(runtime.systemEvents.length, 1);
  assert.equal(health.failures.length, 0);
});

test("poll-only and deployment bindings retain an inert reload-safe service", async (t) => {
  const f = await fixture(t);
  for (const config of [f.config, { ...f.config, mode: "owner", authorityScope: "deployment" }]) {
    const runtime = fakeApi(config);
    register(runtime.api);
    assert.equal(runtime.services.length, 1);
    const health = serviceContext(runtime.api.pluginConfig);
    runtime.services[0].start(health.context);
    await runtime.services[0].stop(health.context);
    assert.equal(runtime.wakeRequests.length, 0);
  }
  assert.equal(f.calls.length, 0);
});

test("service reconnect replays an unacknowledged wake but not an acknowledged event", async (t) => {
  let acknowledged = false;
  let requests = 0;
  const pending = projection({ events: [{ event_id: EVENT, state: "pending", attempts: 0, next_delivery_at: null,
    report: { source: "drover", source_event_id: "worker-1", subject: "worker", sequence: 1,
      kind: "worker_brief_completed", summary: "Worker completed" } }] });
  const done = projection({ owner_wake: false, inbox_counts: { acknowledged: 1 }, events: [], has_more: false });
  const f = await fixture(t, (_req, res) => { requests++; res.end(JSON.stringify({ continuity: acknowledged ? done : pending })); });
  const start = async () => {
    const runtime = fakeApi({ ...f.config, mode: "owner", wakePollIntervalMs: 1000 });
    register(runtime.api);
    const health = serviceContext(runtime.api.pluginConfig);
    runtime.services[0].start(health.context);
    await eventually(() => requests > 0);
    return { runtime, health };
  };
  const first = await start();
  await eventually(() => first.runtime.wakeRequests.length === 1);
  await first.runtime.services[0].stop(first.health.context);
  requests = 0;
  const replay = await start();
  await eventually(() => replay.runtime.wakeRequests.length === 1);
  await replay.runtime.services[0].stop(replay.health.context);
  acknowledged = true;
  requests = 0;
  const final = await start();
  await final.runtime.services[0].stop(final.health.context);
  assert.equal(final.runtime.wakeRequests.length, 0);
});

test("service stop during lease acquisition cannot emit a stale wake", async (t) => {
  let releaseLease;
  const live = projection({
    owner: { id: OWNER_PRINCIPAL, epoch: 2, lease_until: "2099-10-03T00:00:00+00:00", live: true },
    events: [{ event_id: EVENT, state: "pending", attempts: 0, next_delivery_at: null,
      report: { source: "drover", source_event_id: "worker-1", subject: "worker", sequence: 1,
        kind: "worker_brief_completed", summary: "Worker completed" } }],
  });
  const stale = projection({ owner: { id: null, epoch: 1, lease_until: null, live: false }, owner_wake: false });
  const f = await fixture(t, (req, res, body) => {
    if (req.method === "POST" && body.operation === "lease") releaseLease = () => res.end(JSON.stringify({ continuity: live }));
    else res.end(JSON.stringify({ continuity: stale }));
  });
  const runtime = fakeApi({ ...f.config, mode: "owner", wakePollIntervalMs: 1000 });
  register(runtime.api);
  const health = serviceContext(runtime.api.pluginConfig);
  runtime.services[0].start(health.context);
  await eventually(() => typeof releaseLease === "function");
  const stopped = runtime.services[0].stop(health.context);
  releaseLease();
  await stopped;
  assert.equal(runtime.systemEvents.length, 0);
  assert.equal(runtime.wakeRequests.length, 0);
});

test("a competing live owner is fenced without a wake or lease mutation", async (t) => {
  const f = await fixture(t, (_req, res) => res.end(JSON.stringify({ continuity: projection({
    owner: { id: "credential:other", epoch: 9, lease_until: "2099-10-03T00:00:00+00:00", live: true },
  }) })));
  const runtime = fakeApi({ ...f.config, mode: "owner", wakePollIntervalMs: 1000 });
  register(runtime.api);
  const health = serviceContext(runtime.api.pluginConfig);
  runtime.services[0].start(health.context);
  await eventually(() => health.failures.length > 0);
  await runtime.services[0].stop(health.context);
  assert.deepEqual(f.calls.map((request) => request.method), ["GET"]);
  assert.equal(runtime.wakeRequests.length, 0);
  assert.match(String(health.failures[0]), /competing live owner/);
});

test("only the configured owner and canary sessions receive the one tool", async (t) => {
  const f = await fixture(t);
  const ownerTool = tool(f.config);
  assert.equal(ownerTool.name, TOOL_NAME);
  assert.equal(tool(f.config, "agent:unbound:session"), null);
  const { api, registrations } = fakeApi(f.config);
  register(api);
  assert.equal(registrations[0].factory({ assertInvocationCurrent() {} }), null);
});

test("parent and owner polls retain pending state; never POST or auto-ack", async (t) => {
  const f = await fixture(t);
  for (const session of [PARENT, OWNER]) {
    const result = await tool(f.config, session).execute("normal-canary", call("poll", { limit: 1 }));
    assert.deepEqual(JSON.parse(result.content[0].text), result.details);
    assert.equal(result.details.continuity.inbox_counts.pending, 1);
    assert.equal(result.details.delivery, null);
  }
  assert.equal(f.calls.length, 2);
  for (const req of f.calls) {
    assert.equal(req.method, "GET");
    assert.equal(req.url, "/harness/factory-observer/continuity?run_id=run_PLUGIN497&limit=1");
    assert.equal(req.authorization, "Bearer synthetic-canary-token");
    assert.equal(req.body, null);
  }
});

test("trusted initialization preserves integration and deployment scope", async (t) => {
  for (const [authorityScope, scopeLimit] of [["integration", "publish_review"], ["deployment", "explicit_approval_required"]]) {
    const f = await fixture(t, (_req, res) => res.end(JSON.stringify({ continuity: projection({ authority_scope: authorityScope, scope_limit: scopeLimit, owner_wake: authorityScope !== "deployment", recovery: authorityScope === "deployment" ? "approval_required" : "continue_owner" }) })));
    const config = {
      ...f.config, mode: "owner", authorityScope,
      observerSessionId: "trusted-observer", objective: "Trusted initialization", checkpoint: "Pending",
    };
    await tool(config).execute("initialize", call("initialize"));
    assert.equal(f.calls.length, 1);
    assert.deepEqual(f.calls[0].body, {
      operation: "initialize", session_id: "trusted-observer",
      objective: "Trusted initialization", checkpoint: "Pending", authority_scope: authorityScope,
    });
  }
});

test("poll_only and a canary parent cannot mutate even when owner mode exists", async (t) => {
  const f = await fixture(t);
  for (const target of [tool(f.config), tool({ ...f.config, mode: "owner" }, PARENT)]) {
  for (const payload of [call("initialize"), call("lease"), call("consume", { owner_epoch: 1 }),
      call("acknowledge", { owner_epoch: 1, event_id: EVENT, checkpoint: "Not authorized" }),
      call("report", { source_event_id: "id", subject: "ci", sequence: 1, kind: "ci_green", summary: "Green" })]) {
      await assert.rejects(target.execute("canary", payload), /Read-only canary/);
    }
  }
  assert.equal(f.calls.length, 0);
});

test("model cannot choose URL, credentials, run, owner, scope, messaging or executor", async (t) => {
  const f = await fixture(t);
  const target = tool({ ...f.config, mode: "owner" });
  for (const fields of [{ url: "https://attacker.invalid" }, { token: MODEL_CREDENTIAL },
    { run_id: "run_OTHER" }, { owner_id: "impostor" }, { authority_scope: "deployment" },
    { command: "anything" }, { approve: true }]) {
    await assert.rejects(target.execute("bad", call("poll", fields)), /request schema/);
  }
  for (const op of ["merge", "deploy", "sessions_send", "execute", "restart"])
    await assert.rejects(target.execute("bad", call(op)), /request schema/);
  for (const epoch of ["1", true, Number.MAX_SAFE_INTEGER + 1])
    await assert.rejects(target.execute("bad", call("consume", { owner_epoch: epoch })), /schema|safe range/);
  assert.equal(f.calls.length, 0);
});

test("trusted configuration rejects unsafe origins, unknown keys and token arguments", () => {
  const config = { enabled: true, runId: "run_PLUGIN497", ownerSessionKey: OWNER, ownerPrincipal: OWNER_PRINCIPAL,
    authorityScope: "implementation", tokenEnvName: "DROVER_PLUGIN_TEST_TOKEN", droverOrigin: "https://drover.example" };
  for (const origin of ["http://remote.invalid", "https://user:secret@drover.example", "https://drover.example/other",
    "https://drover.example?token=secret", "https://drover.example#fragment", "file:///tmp/state"]) {
    assert.throws(() => tool({ ...config, droverOrigin: origin }), /origin|configuration/);
  }
  assert.throws(() => tool({ ...config, canarySessionKey: OWNER }), /distinct/);
  for (const sessionKey of ["agent:coder", "agent::owner", "agent:coder:", "coder:owner:session"])
    assert.throws(() => tool({ ...config, ownerSessionKey: sessionKey }), /configuration|canonical/);
  assert.throws(() => tool({ ...config, token: MODEL_CREDENTIAL }), /configuration/);
  assert.throws(() => tool({ ...config, tokenEnvName: "bad variable" }), /configuration/);
});

test("missing credential, HTTP authorization refusal and redirects are not bypassed or retried", async (t) => {
  const f = await fixture(t, (_req, res) => { res.statusCode = 401; res.end('{"error":"contains synthetic-canary-token"}'); });
  delete process.env.DROVER_PLUGIN_TEST_TOKEN;
  await assert.rejects(tool(f.config).execute("poll", call("poll")), /credential environment variable/);
  assert.equal(f.calls.length, 0);
  process.env.DROVER_PLUGIN_TEST_TOKEN = "synthetic-canary-token";
  await assert.rejects(tool(f.config).execute("poll", call("poll")), (error) => {
    assert.match(error.message, /HTTP 401/);
    assert.ok(!error.message.includes("synthetic-canary-token"));
    return true;
  });
  assert.equal(f.calls.length, 1);
});

test("redirect cannot forward bearer credential to another origin", async (t) => {
  const sink = await fixture(t);
  const f = await fixture(t, (_req, res) => { res.statusCode = 302; res.setHeader("Location", sink.config.droverOrigin); res.end(); });
  await assert.rejects(tool(f.config).execute("poll", call("poll")), /transport failed/);
  assert.equal(sink.calls.length, 0);
  assert.equal(f.calls.length, 1);
});

test("scope mismatch is detected during GET before a POST", async (t) => {
  const f = await fixture(t, (_req, res) => res.end(JSON.stringify({ continuity: projection({ authority_scope: "integration", scope_limit: "publish_review" }) })));
  await assert.rejects(tool({ ...f.config, mode: "owner" }).execute("ack", call("acknowledge", {
    owner_epoch: 1, event_id: EVENT, checkpoint: "Wrong scope" })), /trusted run\/scope/);
  assert.deepEqual(f.calls.map((req) => req.method), ["GET"]);
});

test("live V2 host authority is required and checked again after scope preflight", async (t) => {
  let current = true;
  let checks = 0;
  const f = await fixture(t, (_request, res) => {
    current = false; // Authority revoked while GET was awaited.
    res.end(JSON.stringify({ continuity: projection() }));
  });
  const { api, registrations } = fakeApi({ ...f.config, mode: "owner" });
  register(api);
  assert.throws(() => registrations[0].factory({ sessionKey: OWNER }), /host invocation authority/);
  const target = registrations[0].factory({ sessionKey: OWNER, assertInvocationCurrent() {
    checks++;
    if (!current) throw new Error("Host authority retired");
  } });
  await assert.rejects(target.execute("consume", call("consume", { owner_epoch: 1 })), /Host authority retired/);
  assert.equal(checks, 2);
  assert.equal(f.calls.length, 1);
  assert.equal(f.calls[0].method, "GET");
});

test("explicit consume has trusted binding and leaves action until a separate ack", async (t) => {
  const f = await fixture(t, (req, res) => res.end(JSON.stringify(req.method === "POST"
    ? { continuity: projection({ next_action: action, inbox_counts: { delivered: 1 } }), delivery: action }
    : { continuity: projection() })));
  const result = await tool({ ...f.config, mode: "owner" }).execute("consume", call("consume", { owner_epoch: 1 }));
  assert.equal(result.details.delivery.type, "review_worker_result");
  assert.deepEqual(f.calls[1].body, { operation: "consume", owner_epoch: 1, run_id: "run_PLUGIN497", owner_id: OWNER_PRINCIPAL });
  assert.deepEqual(f.calls.map((req) => req.method), ["GET", "POST"]);
  assert.ok(!f.calls.some((req) => req.body?.operation === "acknowledge"));
});

test("lost consume reply keeps mock durable intent and triggers no follow-up", async (t) => {
  let outstanding = null;
  const f = await fixture(t, (req, res) => {
    if (req.method === "POST") { outstanding = action; res.destroy(); }
    else res.end(JSON.stringify({ continuity: projection({ next_action: outstanding }) }));
  });
  const target = tool({ ...f.config, mode: "owner" });
  await assert.rejects(target.execute("consume", call("consume", { owner_epoch: 1 })), /transport failed/);
  assert.equal(f.calls.length, 2);
  const recovered = await target.execute("recover-poll", call("poll"));
  assert.deepEqual(recovered.details.continuity.next_action, action);
  assert.equal(f.calls.length, 3);
  assert.ok(!f.calls.some((req) => req.body?.operation === "acknowledge"));
});

test("deployment approval and action vocabulary are validated, never executed", async (t) => {
  const f = await fixture(t, (_req, res) => res.end(JSON.stringify({ continuity: projection({ next_action: { ...action, type: "merge" } }) })));
  await assert.rejects(tool(f.config).execute("poll", call("poll")), /reply schema/);
  assert.equal(f.calls.length, 1);
});

test("deployment poll exposes only blocked approval and rejects authorized escalation", async (t) => {
  let authorized = false;
  const f = await fixture(t, (_req, res) => res.end(JSON.stringify({ continuity: projection({
    authority_scope: "deployment", scope_limit: "explicit_approval_required", owner_wake: false,
    next_action: { ...action, type: "request_explicit_approval", authority_scope: "deployment", authorized },
  }) })));
  const target = tool({ ...f.config, authorityScope: "deployment" });
  const result = await target.execute("poll", call("poll"));
  assert.equal(result.details.continuity.next_action.authorized, false);
  authorized = true;
  await assert.rejects(target.execute("escalation", call("poll")), /bound authority/);
  assert.ok(f.calls.every((request) => request.method === "GET"));
});

test("cancellation stops the read without retry or mutation", async (t) => {
  const f = await fixture(t);
  const controller = new AbortController();
  controller.abort();
  await assert.rejects(tool(f.config).execute("cancelled", call("poll"), controller.signal), /cancelled/);
  assert.equal(f.calls.length, 0);
});
