// Foreground Python integration harness uses this mock SDK with real HTTP.
import { readFileSync } from "node:fs";
import { register } from "../index.js";
import { fakeApi } from "./fake-sdk.js";

const input = JSON.parse(readFileSync(0, "utf8"));
const { api, registrations } = fakeApi(input.config);
register(api);
if (registrations.length !== 1) throw new Error("expected one normal tool registration");
const tool = registrations[0].factory({ sessionKey: input.sessionKey, assertInvocationCurrent() {} });
if (!tool) throw new Error("trusted session has no tool binding");
const output = [];
for (const payload of input.calls) output.push(await tool.execute("mock-normal-tool-call", payload));
console.log(JSON.stringify(output));
