// Executes the NAS 2026.9.6 entry resolver, contract helpers and registrar body.
// The surrounding registry/lifecycle is a bounded fake, never a live Gateway.
import assert from "node:assert/strict";
import { readFileSync } from "node:fs";
import { stripTypeScriptTypes } from "node:module";
import { join } from "node:path";
import { pathToFileURL } from "node:url";
import { runInNewContext } from "node:vm";
import { test } from "node:test";
import * as pluginModule from "../index.js";

const manifest = JSON.parse(readFileSync(new URL("../openclaw.plugin.json", import.meta.url), "utf8"));
const OWNER = "agent:coder:source-contract-owner";
const root = process.env.DROVER_TEST_OPENCLAW_NAS_SOURCE;

test("NAS 2026.9.6 actual entry resolver and registrar load the declared V2 tool", async (t) => {
  assert.ok(root, "DROVER_TEST_OPENCLAW_NAS_SOURCE is required; Studio 2026.3.13 is not target evidence");
  const read = (file) => readFileSync(join(root, file), "utf8");
  const pkg = JSON.parse(read("package.json"));
  assert.equal(pkg.version, "2026.9.6");
  assert.equal(pkg.exports["./plugin-sdk/plugin-entry"].default, "./dist/plugin-sdk/plugin-entry.js");
  assert.equal(pkg.exports["./plugin-sdk/tool-plugin"].default, "./dist/plugin-sdk/tool-plugin.js");
  assert.match(read("src/plugin-sdk/plugin-entry.ts"), /export function definePluginEntry/);
  const apiTypes = read("src/plugins/plugin-api.types.ts");
  assert.match(apiTypes, /pluginConfig\?: Record<string, unknown>/);
  assert.match(apiTypes, /OpenClawPluginToolFactory<2>/);
  const toolTypes = read("src/plugins/tool-types.ts");
  assert.match(toolTypes, /sessionKey\?: string/);
  assert.match(toolTypes, /assertInvocationCurrent: \(\) => void/);
  assert.match(toolTypes, /contextVersion: 2/);
  assert.match(read("docs/plugins/manage-plugins.md"), /openclaw plugins install npm-pack:<path\.tgz>/);
  assert.match(read("src/plugins/install-source-plan.ts"), /source: "npm-pack", archivePath: resolveUserPath\(npmPackPath\)/);
  const packageEntries = read("docs/plugins/sdk-entrypoints/package-entries.md");
  assert.match(packageEntries, /runtimeExtensions.*when present/);
  const packageMetadata = JSON.parse(readFileSync(new URL("../package.json", import.meta.url), "utf8"));
  assert.deepEqual(packageMetadata.openclaw.runtimeExtensions, packageMetadata.openclaw.extensions);
  assert.equal(packageMetadata.engines.node, pkg.engines.node);
  assert.equal(manifest.enabledByDefault, false);
  assert.deepEqual(manifest.activation, { onStartup: false });
  assert.deepEqual(manifest.toolMetadata.drover_continuity_owner,
    { optional: true, replaySafe: false, sideEffecting: true });

  // Direct imports are pure source helpers with erased type-only imports.
  const { resolvePluginModuleExport } = await import(pathToFileURL(join(root, "src/plugins/module-export.ts")));
  const contracts = await import(pathToFileURL(join(root, "src/plugins/tool-contracts.ts")));
  const resolved = resolvePluginModuleExport(pluginModule);
  assert.equal(resolved.definition.id, manifest.id);
  assert.equal(resolved.register, pluginModule.register);
  assert.equal(typeof resolved.register, "function");

  // Run precisely the current registrar body, not a hand-copied implementation.
  // Dependencies supplied below are only surrounding registry bookkeeping.
  const source = read("src/plugins/registry-registrars-tools-hooks.ts");
  const start = source.indexOf("  const registerTool = (");
  const end = source.indexOf("  const registerHook = (", start);
  assert.ok(start >= 0 && end > start, "Current NAS registrar source shape changed; review required");
  const errors = [];
  const registry = { tools: [] };
  const registerTool = runInNewContext(stripTypeScriptTypes(source.slice(start, end)) + "\nregisterTool;", {
    ...contracts, registry, pluginsWithChannelRegistrationConflict: new Set(),
    reportRegistrationError: (_record, message) => errors.push(message),
    createRegistration: (record, entry) => ({ pluginId: record.id, ...entry }),
  });
  const record = { id: manifest.id, contracts: manifest.contracts, toolNames: [], origin: "global" };
  resolved.register({ pluginConfig: {}, registerTool: (tool, options) => registerTool(record, tool, options) });
  assert.deepEqual(errors, []);
  assert.equal(registry.tools.length, 1);
  const entry = registry.tools[0];
  assert.equal(entry.contextVersion, 2);
  assert.equal(entry.optional, true);
  assert.equal(entry.names.join(), "drover_continuity_owner");
  assert.equal(entry.factory({ sessionKey: OWNER, assertInvocationCurrent() {} }), null);
  assert.throws(() => entry.factory({ sessionKey: OWNER }), /host invocation authority/);

  // Regression: the 7747188 manifest was rejected by this current-source registrar.
  registerTool({ ...record, contracts: undefined, toolNames: [] }, pluginModule.register, { name: "drover_continuity_owner" });
  assert.match(errors.at(-1), /must declare contracts.tools/);
  assert.equal(registry.tools.length, 1);

  const enabledRecord = { ...record, toolNames: [] };
  resolved.register({ pluginConfig: {
    enabled: true, ownerSessionKey: OWNER, runId: "run_NAS497", authorityScope: "implementation",
    droverOrigin: "https://drover.example", tokenEnvName: "DROVER_SOURCE_TEST_TOKEN",
  }, registerTool: (tool, options) => registerTool(enabledRecord, tool, options) });
  const currentTool = registry.tools[1].factory({ sessionKey: OWNER,
    assertInvocationCurrent() { throw new Error("host invocation retired"); } });
  assert.equal(currentTool.name, "drover_continuity_owner");
  process.env.DROVER_SOURCE_TEST_TOKEN = "synthetic-test-token";
  try {
    await assert.rejects(currentTool.execute("retired", { version: 1, request: { operation: "poll" } }), /host invocation retired/);
  } finally { delete process.env.DROVER_SOURCE_TEST_TOKEN; }
  t.diagnostic("Actual NAS 2026.9.6 source resolver/contract helpers/registrar body executed; registry bookkeeping fake, no Gateway loaded");
});
