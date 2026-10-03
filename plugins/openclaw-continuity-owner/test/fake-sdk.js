// Exact minimal shape from OpenClaw 2026.3.13 src/plugins/types.ts:63-89,
// 366-380 and src/plugins/registry.ts:194-218. This does NOT load a Gateway.
import assert from "node:assert/strict";

export function fakeApi(pluginConfig = {}) {
  const registrations = [];
  const api = {
    pluginConfig,
    registerTool(factory, options) {
      assert.equal(typeof factory, "function");
      assert.deepEqual(options, { name: "drover_continuity_owner", optional: true });
      registrations.push({ factory, options });
    },
  };
  return { api, registrations };
}
