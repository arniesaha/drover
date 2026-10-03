import assert from "node:assert/strict";
import { createServer } from "node:http";
import { test } from "node:test";
import { register, TOOL_NAME } from "../index.js";
import { fakeApi } from "./fake-sdk.js";

const OWNER = "agent:coder:subagent:mock-owner";
const PARENT = "agent:main:mock-parent";
const EVENT = "a".repeat(64);
const action = {
  action_id: "observer-action-" + EVENT, event_id: EVENT, type: "review_worker_result",
  authority_scope: "implementation", authorized: true,
};

function projection(overrides = {}) {
  return {
    version: 1, run_id: "run_PLUGIN497", session_id: "mock-observer", expected_revision: 4,
    authority: "taskflow", objective: "Canary", checkpoint: "Waiting for explicit owner",
    authority_scope: "implementation", scope_limit: "commit_only",
    owner: { id: OWNER, epoch: 1, lease_until: "2099-10-03T00:00:00+00:00", live: true },
    worker_state: "brief_completed", terminal_release: false, next_action: null,
    recovery: "continue_owner", owner_wake: true, inbox_counts: { pending: 1 },
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
    ownerSessionKey: OWNER, canarySessionKey: PARENT, authorityScope: "implementation",
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

test("registers exactly one optional normal tool; default inactive and unknown contexts get none", () => {
  const { api, registrations } = fakeApi();
  register(api);
  assert.equal(registrations.length, 1);
  assert.equal(registrations[0].factory({ sessionKey: OWNER, assertInvocationCurrent() {} }), null);
  assert.equal(registrations[0].factory({ assertInvocationCurrent() {} }), null);
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

test("poll_only and a canary parent cannot mutate even when owner mode exists", async (t) => {
  const f = await fixture(t);
  for (const target of [tool(f.config), tool({ ...f.config, mode: "owner" }, PARENT)]) {
    for (const payload of [call("lease"), call("consume", { owner_epoch: 1 }),
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
  for (const fields of [{ url: "https://attacker.invalid" }, { token: "model-token" },
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
  const config = { enabled: true, runId: "run_PLUGIN497", ownerSessionKey: OWNER,
    authorityScope: "implementation", tokenEnvName: "DROVER_PLUGIN_TEST_TOKEN", droverOrigin: "https://drover.example" };
  for (const origin of ["http://remote.invalid", "https://user:secret@drover.example", "https://drover.example/other",
    "https://drover.example?token=secret", "https://drover.example#fragment", "file:///tmp/state"]) {
    assert.throws(() => tool({ ...config, droverOrigin: origin }), /origin|configuration/);
  }
  assert.throws(() => tool({ ...config, canarySessionKey: OWNER }), /distinct/);
  assert.throws(() => tool({ ...config, token: "secret" }), /configuration/);
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
  assert.deepEqual(f.calls[1].body, { operation: "consume", owner_epoch: 1, run_id: "run_PLUGIN497", owner_id: OWNER });
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
