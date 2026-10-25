// Normal OpenClaw registerTool factory; see README for exact local SDK evidence.
// The symbol below is the public static authoring metadata contract consumed by
// `openclaw plugins build|validate|pack`. Keep it in step with this V2 factory.
import Ajv from "ajv";
import addFormats from "ajv-formats";
import manifest from "./openclaw.plugin.json" with { type: "json" };
import replySchema from "./schemas/reply.json" with { type: "json" };
import requestSchema from "./schemas/request.json" with { type: "json" };

export const TOOL_NAME = "drover_continuity_owner";
const ENDPOINT = "/harness/factory-observer/continuity";
const MAX_REPLY_BYTES = 2 * 1024 * 1024;
// Pydantic's discriminator is an annotation; oneOf still strictly validates.
const ajv = new Ajv({ strict: false, coerceTypes: false, useDefaults: false, removeAdditional: false });
addFormats(ajv);
const validateRequest = ajv.compile(requestSchema);
const validateReply = ajv.compile(replySchema);
const validateConfig = ajv.compile(manifest.configSchema);
const scopes = { implementation: "commit_only", integration: "publish_review", deployment: "explicit_approval_required" };
const actionFields = ["action_id", "event_id", "type", "authority_scope", "authorized"];
const WAKE_SERVICE_ID = "drover-continuity-owner-wake";

function requireValue(condition, message) {
  if (!condition) throw new Error(message);
}

function safeIntegers(value) {
  if (typeof value === "number") requireValue(Number.isSafeInteger(value), "Protocol integer exceeds JavaScript safe range");
  else if (value && typeof value === "object") for (const child of Object.values(value)) safeIntegers(child);
}

function configuration(raw) {
  requireValue(validateConfig(raw), "Invalid trusted Drover plugin configuration");
  const config = {
    mode: "poll_only", timeoutMs: 5000, wakePollIntervalMs: 15000,
    wakeRetryIntervalMs: 60000, ownerLeaseSeconds: 60, ...raw,
  };
  if (config.enabled !== true) return Object.freeze(config);
  requireValue(!config.canarySessionKey || config.canarySessionKey !== config.ownerSessionKey, "Canary and owner session bindings must be distinct");
  requireValue(/^agent:[^:\s]+(?::[^:\s]+)+$/.test(config.ownerSessionKey), "Owner session binding must be a canonical agent session key");
  if (config.canarySessionKey !== undefined) requireValue(/^agent:[^:\s]+(?::[^:\s]+)+$/.test(config.canarySessionKey), "Canary session binding must be a canonical agent session key");
  const origin = new URL(config.droverOrigin);
  const loopback = ["127.0.0.1", "[::1]", "localhost"].includes(origin.hostname);
  requireValue(origin.protocol === "https:" || (origin.protocol === "http:" && loopback), "Drover origin requires HTTPS or loopback HTTP");
  requireValue(!origin.username && !origin.password && !origin.search && !origin.hash && origin.pathname === "/", "Drover origin must contain no credentials, path, query or fragment");
  config.droverOrigin = origin.origin;
  return Object.freeze(config);
}

function waitFor(signal, milliseconds) {
  if (signal.aborted) return Promise.resolve();
  return new Promise((resolve) => {
    const timer = setTimeout(resolve, milliseconds);
    signal.addEventListener("abort", () => { clearTimeout(timer); resolve(); }, { once: true });
  });
}

// A Work-coordinator run is owned by its canonical agent session key; a Factory
// observer run by the bearer principal. Both are exact, trusted configuration.
function ownerIdFor(config, status) {
  return status.owner_handoff?.authorized_owner_id === config.ownerSessionKey
    ? config.ownerSessionKey
    : config.ownerPrincipal;
}

function wakeFingerprint(status) {
  const event = status.next_action?.event_id ?? status.events?.[0]?.event_id;
  return event ? `${status.run_id}:${event}` : null;
}

function ownerAgentId(sessionKey) {
  const parts = sessionKey.split(":");
  requireValue(parts[0] === "agent" && parts[1], "Owner session binding has no agent identity");
  return parts[1];
}

async function maintainOwnerLease(config, status, signal, assertCurrent) {
  const owner = status.owner;
  const ownerId = ownerIdFor(config, status);
  if (owner.live && owner.id !== ownerId) {
    throw new Error("Drover continuity run has a competing live owner");
  }
  const leaseUntil = owner.lease_until ? Date.parse(owner.lease_until) : 0;
  if (owner.live && owner.id === ownerId && leaseUntil - Date.now() > config.ownerLeaseSeconds * 500) {
    return status;
  }
  const leased = await invoke(config, false, {
    version: 1,
    request: {
      operation: "lease",
      owner_epoch: owner.id === ownerId && owner.epoch > 0 ? owner.epoch : null,
      lease_seconds: config.ownerLeaseSeconds,
    },
  }, signal, assertCurrent);
  return leased.continuity;
}

function committedServiceConfiguration(ctx) {
  return configuration(ctx.config?.plugins?.entries?.[manifest.id]?.config ?? {});
}

function registerWakeService(api) {
  let controller;
  let running;
  let lastWake = { fingerprint: null, at: 0 };
  api.registerService({
    id: WAKE_SERVICE_ID,
    reload: { configPrefixes: ["plugins.entries.drover-continuity-owner.config"] },
    start(ctx) {
      const config = committedServiceConfiguration(ctx);
      if (config.enabled !== true || config.mode !== "owner" || config.authorityScope === "deployment") return;
      controller = new AbortController();
      const signal = controller.signal;
      const assertCurrent = () => requireValue(!signal.aborted, "Drover owner wake service stopped");
      running = (async () => {
        while (!signal.aborted) {
          try {
            const polled = reply(await request(config, "GET", { run_id: config.runId, limit: 1 }, signal, assertCurrent), config);
            const status = await maintainOwnerLease(config, polled.continuity, signal, assertCurrent);
            assertCurrent();
            const fingerprint = status.owner_wake ? wakeFingerprint(status) : null;
            if (fingerprint && (lastWake.fingerprint !== fingerprint || Date.now() - lastWake.at >= config.wakeRetryIntervalMs)) {
              const eventId = fingerprint.slice(fingerprint.lastIndexOf(":") + 1);
              api.runtime.system.enqueueSystemEvent(
                `Drover continuity event ${eventId} is pending for trusted run ${config.runId}. ` +
                "Poll the bound owner tool, consume only with its current fenced epoch, advance only the authorized scoped next action, " +
                "and acknowledge only after that advancement is durably verified. Never infer terminal release.",
                { sessionKey: config.ownerSessionKey, contextKey: fingerprint },
              );
              assertCurrent();
              api.runtime.system.requestHeartbeat({
                source: "other", intent: "immediate", reason: "drover-continuity-owner-wake",
                agentId: ownerAgentId(config.ownerSessionKey), sessionKey: config.ownerSessionKey,
                heartbeat: { target: "last" },
              });
              lastWake = { fingerprint, at: Date.now() };
            } else if (!fingerprint) lastWake = { fingerprint: null, at: 0 };
            ctx.serviceHealth?.clearFailure();
          } catch (error) {
            if (!signal.aborted) {
              ctx.serviceHealth?.reportFailure(error);
              ctx.logger.warn(`continuity wake poll failed: ${error instanceof Error ? error.message : "unknown error"}`);
            }
          }
          await waitFor(signal, config.wakePollIntervalMs);
        }
      })();
    },
    async stop() {
      controller?.abort();
      await running;
    },
  });
}

function checkAction(action, scope) {
  if (action === null || action === undefined) return;
  requireValue(action.action_id === "observer-action-" + action.event_id, "Action identity does not match its event");
  const blocked = scope === "deployment";
  requireValue(action.authority_scope === scope && action.authorized === !blocked && (action.type === "request_explicit_approval") === blocked, "Action exceeds bound authority");
}

function reply(raw, config) {
  requireValue(raw && typeof raw === "object" && !Array.isArray(raw) && !Object.hasOwn(raw, "version"), "Invalid Drover boundary envelope");
  const result = { version: 1, delivery: null, event_id: null, ...raw };
  requireValue(validateReply(result), "Invalid Drover continuity reply schema");
  safeIntegers(result);
  const status = result.continuity;
  requireValue(status.run_id === config.runId && status.authority_scope === config.authorityScope, "Reply differs from trusted run/scope");
  requireValue(status.owner_handoff.authorized_owner_id === ownerIdFor(config, status), "Reply differs from trusted authorized owner");
  requireValue(status.scope_limit === scopes[config.authorityScope], "Reply scope limit is inconsistent");
  checkAction(status.next_action, config.authorityScope);
  checkAction(result.delivery, config.authorityScope);
  requireValue(config.authorityScope !== "deployment" || !status.owner_wake, "Deployment approval cannot authorize owner execution");
  return result;
}

async function request(config, method, data, signal, assertCurrent) {
  const token = process.env[config.tokenEnvName];
  requireValue(typeof token === "string" && token.length > 0 && token.length <= 8192 && !/\s/.test(token), "Configured Drover credential environment variable is missing or malformed");
  const url = new URL(ENDPOINT, config.droverOrigin);
  const headers = { Authorization: `Bearer ${token}`, Accept: "application/json" };
  let body;
  if (method === "GET") for (const [key, value] of Object.entries(data)) url.searchParams.set(key, String(value));
  else {
    body = JSON.stringify(data);
    requireValue(Buffer.byteLength(body) <= 16384, "Continuity request exceeds 16 KiB");
    headers["Content-Type"] = "application/json";
  }
  const timeout = AbortSignal.timeout(config.timeoutMs);
  const boundedSignal = signal ? AbortSignal.any([signal, timeout]) : timeout;
  let response;
  // 2026.9.9 V2 host authority must be checked after awaits, immediately before effects.
  assertCurrent();
  try {
    response = await fetch(url, { method, headers, body, signal: boundedSignal, redirect: "error", cache: "no-store" });
  } catch {
    throw new Error("Drover transport failed or was cancelled; poll to reconcile any unknown mutation outcome");
  }
  if (!response.ok) {
    await response.body?.cancel();
    throw new Error(`Drover continuity HTTP ${response.status}; no automatic retry or acknowledgment`);
  }
  if (!(response.headers.get("content-type") ?? "").toLowerCase().startsWith("application/json")) {
    await response.body?.cancel();
    throw new Error("Drover reply must be JSON");
  }
  const chunks = [];
  let bytes = 0;
  try {
    for await (const chunk of response.body) {
      bytes += chunk.length;
      if (bytes > MAX_REPLY_BYTES) {
        await response.body.cancel().catch(() => {});
        throw new Error("oversized reply");
      }
      chunks.push(chunk);
    }
    return JSON.parse(Buffer.concat(chunks).toString("utf8"));
  } catch {
    throw new Error("Drover reply failed, exceeded 2 MiB or was invalid JSON; poll to reconcile unknown outcomes");
  }
}

async function invoke(config, readOnly, payload, signal, assertCurrent) {
  requireValue(validateRequest(payload), "Invalid owner tool request schema");
  safeIntegers(payload);
  const args = payload.request;
  requireValue(!readOnly || args.operation === "poll", "Read-only canary permits poll only");
  const get = (limit) => request(config, "GET", { run_id: config.runId, limit }, signal, assertCurrent);
  let raw;
  let ownerId = config.ownerPrincipal;
  if (args.operation === "poll") raw = await get(args.limit ?? 20);
  else {
    if (args.operation !== "initialize") {
      const preflight = await get(1);
      requireValue(!Object.hasOwn(preflight, "delivery") && !Object.hasOwn(preflight, "event_id"), "Status cannot deliver or acknowledge an event");
      ownerId = ownerIdFor(config, reply(preflight, config).continuity); // Immutable scope check before any mutation.
    }
    const body = args.operation === "initialize"
      ? initializeBody(config)
      : { ...args, run_id: config.runId };
    if (body.owner_epoch === null) delete body.owner_epoch;
    if (args.operation === "report") body.source = "openclaw";
    else if (args.operation !== "initialize") body.owner_id = ownerId;
    raw = await request(config, "POST", body, signal, assertCurrent);
  }
  const result = reply(raw, config);
  const status = result.continuity;
  if (args.operation === "consume") {
    requireValue(Object.hasOwn(raw, "delivery"), "Consume must carry a delivery field");
    requireValue(result.delivery === null || (status.next_action && actionFields.every((key) => result.delivery[key] === status.next_action[key])), "Delivery differs from the durable action");
  } else requireValue(!Object.hasOwn(raw, "delivery"), "Only consume may deliver an owner action");
  if (["lease", "consume", "acknowledge"].includes(args.operation)) {
    requireValue(status.owner.id === ownerIdFor(config, status) && status.owner.live, "Reply no longer carries the trusted live owner");
    if (args.operation !== "lease") requireValue(status.owner.epoch === args.owner_epoch, "Reply no longer carries the requested owner epoch");
  }
  if (args.operation === "report") requireValue(result.event_id !== null, "Report must return its durable identity");
  else requireValue(!Object.hasOwn(raw, "event_id"), "Only report may return an event identity");
  return result;
}

function initializeBody(config) {
  requireValue(typeof config.observerSessionId === "string" && config.observerSessionId.length > 0, "Initialize requires a trusted observer session binding");
  requireValue(typeof config.objective === "string" && config.objective.trim().length > 0, "Initialize requires a trusted objective");
  requireValue(typeof config.checkpoint === "string" && config.checkpoint.trim().length > 0, "Initialize requires a trusted checkpoint");
  const body = { operation: "initialize", session_id: config.observerSessionId, objective: config.objective, checkpoint: config.checkpoint, authority_scope: config.authorityScope };
  if (config.handoffTokenEnvName !== undefined) {
    const handoffToken = process.env[config.handoffTokenEnvName];
    requireValue(typeof handoffToken === "string" && handoffToken.length > 0 && handoffToken.length <= 512 && !/\s/.test(handoffToken), "Configured owner handoff token is missing or malformed");
    body.handoff_token = handoffToken;
  }
  return body;
}

// SDK 2026.9.9: V2 descriptor uses trusted sessionKey and live invocation guard.
export function register(api) {
  const config = configuration(api.pluginConfig ?? {});
  registerWakeService(api);
  api.registerTool({ contextVersion: 2, create(context) {
    if (config.enabled !== true || !context?.sessionKey) return null;
    const owner = context.sessionKey === config.ownerSessionKey;
    const canary = context.sessionKey === config.canarySessionKey;
    if (!owner && !canary) return null;
    requireValue(typeof context.assertInvocationCurrent === "function", "Host invocation authority is required");
    const assertCurrent = context.assertInvocationCurrent;
    const readOnly = config.mode === "poll_only" || !owner;
    return {
      name: TOOL_NAME,
      label: "Drover Continuity Owner",
      description: readOnly
        ? "Read-only canary: poll the bound Drover observer run. Poll does not consume or acknowledge."
        : "Explicit Drover continuity calls for the bound owner. Advisory actions only; no executor, merge, deployment or messaging. Ack only after idempotent reconciliation.",
      parameters: structuredClone(requestSchema),
      async execute(_toolCallId, payload, signal) {
        const details = await invoke(config, readOnly, payload, signal, assertCurrent);
        return { content: [{ type: "text", text: JSON.stringify(details) }], details };
      },
    };
  } }, { name: TOOL_NAME, optional: true });
}

const definition = { id: manifest.id, name: manifest.name, version: manifest.version,
  description: manifest.description, register };

// `defineToolPlugin` attaches this exact global symbol. This plugin needs the
// V2 factory API for per-session authority, so it retains its explicit
// registration while exposing equivalent static metadata for artifact authoring.
Object.defineProperty(definition, Symbol.for("openclaw.plugin-sdk.tool-plugin.metadata"), {
  value: Object.freeze({
    id: manifest.id,
    name: manifest.name,
    description: manifest.description,
    activation: structuredClone(manifest.activation),
    configSchema: structuredClone(manifest.configSchema),
    tools: Object.freeze([Object.freeze({
      name: TOOL_NAME,
      label: "Drover Continuity Owner",
      description: "Explicit Drover continuity calls for the bound owner. Advisory actions only; no executor, merge, deployment or messaging. Ack only after idempotent reconciliation.",
      parameters: structuredClone(requestSchema),
      optional: true,
    })]),
  }),
  enumerable: false,
});

export default definition;
