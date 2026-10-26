// Minimal 2026.9.9 V2 registrar fake. NAS-source.test.js executes actual source
// resolver/registrar code separately. This fake does not load a Gateway.
import assert from "node:assert/strict";
import { readFileSync } from "node:fs";
const manifest = JSON.parse(readFileSync(new URL("../openclaw.plugin.json", import.meta.url), "utf8"));

export function fakeApi(pluginConfig = {}) {
  const registrations = [];
  const services = [];
  const systemEvents = [];
  const wakeRequests = [];
  const subagentWakeRequests = [];
  const subagentWakeResult = { ok: true };
  const api = {
    pluginConfig,
    runtime: { system: {
      enqueueSystemEvent(text, options) { systemEvents.push({ text, options }); return true; },
      requestHeartbeat(options) { wakeRequests.push(options); },
      requestLiveSubagentOwnerWake(options) { subagentWakeRequests.push(options); return subagentWakeResult; },
    } },
    registerService(service) { services.push(service); },
    registerTool(descriptor, options) {
      assert.deepEqual(manifest.contracts.tools, ["drover_continuity_owner"]);
      assert.equal(descriptor.contextVersion, 2);
      assert.equal(typeof descriptor.create, "function");
      assert.deepEqual(options, { name: "drover_continuity_owner", optional: true });
      const factory = (context) => {
        if (!context.assertInvocationCurrent) throw new Error("Version 2 tool factories require host invocation authority");
        return descriptor.create(context);
      };
      registrations.push({ factory, options, descriptor });
    },
  };
  return { api, registrations, services, systemEvents, wakeRequests, subagentWakeRequests };
}
