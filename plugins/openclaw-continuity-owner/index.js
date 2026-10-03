// Normal OpenClaw registerTool factory; see README for exact local SDK evidence.
// No SDK runtime import is needed for this supported plain ESM plugin shape.
import { readFileSync } from "node:fs";
import Ajv from "ajv";
import addFormats from "ajv-formats";

export const TOOL_NAME = "drover_continuity_owner";
const ENDPOINT = "/harness/factory-observer/continuity";
const MAX_REPLY_BYTES = 2 * 1024 * 1024;
const schema = (path) => JSON.parse(readFileSync(new URL(path, import.meta.url), "utf8"));
const requestSchema = schema("./schemas/request.json");
const replySchema = schema("./schemas/reply.json");
const manifest = schema("./openclaw.plugin.json");
// Pydantic's discriminator is an annotation; oneOf still strictly validates.
const ajv = new Ajv({ strict: false, coerceTypes: false, useDefaults: false, removeAdditional: false });
addFormats(ajv);
const validateRequest = ajv.compile(requestSchema);
const validateReply = ajv.compile(replySchema);
const validateConfig = ajv.compile(manifest.configSchema);
const scopes = { implementation: "commit_only", integration: "publish_review", deployment: "explicit_approval_required" };
const actionFields = ["action_id", "event_id", "type", "authority_scope", "authorized"];

function requireValue(condition, message) {
  if (!condition) throw new Error(message);
}

function safeIntegers(value) {
  if (typeof value === "number") requireValue(Number.isSafeInteger(value), "Protocol integer exceeds JavaScript safe range");
  else if (value && typeof value === "object") for (const child of Object.values(value)) safeIntegers(child);
}

function configuration(raw) {
  requireValue(validateConfig(raw), "Invalid trusted Drover plugin configuration");
  const config = { mode: "poll_only", timeoutMs: 5000, ...raw };
  if (config.enabled !== true) return Object.freeze(config);
  requireValue(!config.canarySessionKey || config.canarySessionKey !== config.ownerSessionKey, "Canary and owner session bindings must be distinct");
  const origin = new URL(config.droverOrigin);
  const loopback = ["127.0.0.1", "[::1]", "localhost"].includes(origin.hostname);
  requireValue(origin.protocol === "https:" || (origin.protocol === "http:" && loopback), "Drover origin requires HTTPS or loopback HTTP");
  requireValue(!origin.username && !origin.password && !origin.search && !origin.hash && origin.pathname === "/", "Drover origin must contain no credentials, path, query or fragment");
  config.droverOrigin = origin.origin;
  return Object.freeze(config);
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
  requireValue(status.scope_limit === scopes[config.authorityScope], "Reply scope limit is inconsistent");
  checkAction(status.next_action, config.authorityScope);
  checkAction(result.delivery, config.authorityScope);
  requireValue(config.authorityScope !== "deployment" || !status.owner_wake, "Deployment approval cannot authorize owner execution");
  return result;
}

async function request(config, method, data, signal) {
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

async function invoke(config, readOnly, payload, signal) {
  requireValue(validateRequest(payload), "Invalid owner tool request schema");
  safeIntegers(payload);
  const args = payload.request;
  requireValue(!readOnly || args.operation === "poll", "Read-only canary permits poll only");
  const get = (limit) => request(config, "GET", { run_id: config.runId, limit }, signal);
  let raw;
  if (args.operation === "poll") raw = await get(args.limit ?? 20);
  else {
    const preflight = await get(1);
    requireValue(!Object.hasOwn(preflight, "delivery") && !Object.hasOwn(preflight, "event_id"), "Status cannot deliver or acknowledge an event");
    reply(preflight, config); // Immutable scope check before any mutation.
    const body = { ...args, run_id: config.runId };
    if (body.owner_epoch === null) delete body.owner_epoch;
    if (args.operation === "report") body.source = "openclaw";
    else body.owner_id = config.ownerSessionKey;
    raw = await request(config, "POST", body, signal);
  }
  const result = reply(raw, config);
  const status = result.continuity;
  if (args.operation === "consume") {
    requireValue(Object.hasOwn(raw, "delivery"), "Consume must carry a delivery field");
    requireValue(result.delivery === null || (status.next_action && actionFields.every((key) => result.delivery[key] === status.next_action[key])), "Delivery differs from the durable action");
  } else requireValue(!Object.hasOwn(raw, "delivery"), "Only consume may deliver an owner action");
  if (["lease", "consume", "acknowledge"].includes(args.operation)) {
    requireValue(status.owner.id === config.ownerSessionKey && status.owner.live, "Reply no longer carries the trusted live owner");
    if (args.operation !== "lease") requireValue(status.owner.epoch === args.owner_epoch, "Reply no longer carries the requested owner epoch");
  }
  if (args.operation === "report") requireValue(result.event_id !== null, "Report must return its durable identity");
  else requireValue(!Object.hasOwn(raw, "event_id"), "Only report may return an event identity");
  return result;
}

// SDK: OpenClawPluginApi.registerTool(factory, {name, optional}); factory(ctx)
// returns AnyAgentTool or null. Context.sessionKey is trusted host input.
export default function register(api) {
  const config = configuration(api.pluginConfig ?? {});
  api.registerTool((context) => {
    if (config.enabled !== true || !context?.sessionKey) return null;
    const owner = context.sessionKey === config.ownerSessionKey;
    const canary = context.sessionKey === config.canarySessionKey;
    if (!owner && !canary) return null;
    const readOnly = config.mode === "poll_only" || !owner;
    return {
      name: TOOL_NAME,
      label: "Drover Continuity Owner",
      description: readOnly
        ? "Read-only canary: poll the bound Drover observer run. Poll does not consume or acknowledge."
        : "Explicit Drover continuity calls for the bound owner. Advisory actions only; no executor, merge, deployment or messaging. Ack only after idempotent reconciliation.",
      parameters: structuredClone(requestSchema),
      async execute(_toolCallId, payload, signal) {
        const details = await invoke(config, readOnly, payload, signal);
        return { content: [{ type: "text", text: JSON.stringify(details) }], details };
      },
    };
  }, { name: TOOL_NAME, optional: true });
}
