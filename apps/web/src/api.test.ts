import { describe, expect, it, vi } from "vitest";
import { HubApi, redeemPairing } from "./api";

const response = (body: unknown, status = 200) => new Response(JSON.stringify(body), { status, headers: { "Content-Type": "application/json" } });
const call = (body: unknown, status?: number) => {
  const fetcher = vi.fn<typeof fetch>().mockResolvedValue(response(body, status));
  return { api: new HubApi({ hub: "https://hub.example.test", token: "device" }, fetcher), fetcher };
};

describe("hub action request mapping", () => {
  it("maps catalog, folder browse, creation, and launch requests", async () => {
    const { api, fetcher } = call({ models: [{ id: "gpt-5", display_name: "GPT-5", is_default: true, reasoning: { supported: ["low", "high"], default: "high" } }] });
    await expect(api.catalog("mac mini", "codex")).resolves.toEqual([{ id: "gpt-5", name: "GPT-5", isDefault: true, efforts: ["low", "high"], defaultEffort: "high" }]);
    expect(fetcher.mock.calls[0][0]).toBe("https://hub.example.test/harness/hosts/mac%20mini/model-catalog?harness=codex&refresh=0");
    fetcher.mockResolvedValueOnce(response({ path: "/work", entries: [{ name: "repo", path: "/work/repo", is_dir: true, is_git_repo: true }] }));
    await expect(api.folders("mac", "/work")).resolves.toEqual({ path: "/work", parent: undefined, entries: [{ name: "repo", path: "/work/repo", isDir: true, isGitRepo: true }] });
    fetcher.mockResolvedValueOnce(response({ path: "/work/new" })); await api.createFolder("mac", "/work", "new");
    fetcher.mockResolvedValueOnce(response({ session_id: "s-new" })); await api.launch("mac", { harness: "codex", mode: "structured", prompt: "hello", cwd: "/work/new", model: "gpt-5", thinking_effort: "high" });
    expect(fetcher.mock.calls.slice(-2).map(([url, init]) => [url, init?.method, init?.body])).toEqual([
      ["https://hub.example.test/harness/hosts/mac/fs/create", "POST", '{"path":"/work","name":"new"}'],
      ["https://hub.example.test/harness/hosts/mac/sessions", "POST", '{"harness":"codex","mode":"structured","prompt":"hello","cwd":"/work/new","model":"gpt-5","thinking_effort":"high"}']
    ]);
  });
  it("maps transcript and all session controls", async () => {
    const { api, fetcher } = call({ messages: [{ event_id: "e1", seq: 1, type: "assistant_output", role: "assistant", text: "hello", payload: {} }] });
    await expect(api.messages("session one", 9)).resolves.toMatchObject({ messages: [{ id: "e1", seq: 1 }] });
    for (const body of [{ turn_id: "t1" }, {}, {}, {}]) fetcher.mockResolvedValueOnce(response(body));
    await api.turn("session one", "next"); await api.permission("session one", "permission-1", "allow", "looks good"); await api.interrupt("session one"); await api.terminate("session one");
    expect(fetcher.mock.calls.map(([url, init]) => [url, init?.method, init?.body])).toEqual([
      ["https://hub.example.test/harness/sessions/session%20one/messages?after_seq=9", undefined, undefined],
      ["https://hub.example.test/harness/sessions/session%20one/turns", "POST", '{"text":"next"}'],
      ["https://hub.example.test/harness/sessions/session%20one/permission", "POST", '{"request_id":"permission-1","decision":"allow","note":"looks good"}'],
      ["https://hub.example.test/harness/sessions/session%20one/interrupt", "POST", "{}"],
      ["https://hub.example.test/harness/sessions/session%20one/terminate", "POST", "{}"]
    ]);
  });
  it("surfaces credential and hub error states", async () => {
    const expired = call({ error: "gone" }, 401); await expect(expired.api.terminate("s1")).rejects.toThrow("Pair again");
    const denied = call({ error: "outside_allowed_folders" }, 403); await expect(denied.api.createFolder("h", "/", "x")).rejects.toThrow("outside_allowed_folders");
  });
  it("redeems pairing on the hub origin and explains expired codes", async () => {
    const fetcher = vi.fn<typeof fetch>().mockResolvedValue(response({ token: "device-token", scope: "device" }));
    await expect(redeemPairing("https://hub.example.test", "ABCD-EFGH", "Web – Chrome", fetcher)).resolves.toEqual({ hub: "https://hub.example.test", token: "device-token" });
    expect(fetcher).toHaveBeenCalledWith("https://hub.example.test/auth/pair", expect.objectContaining({ method: "POST", body: '{"code":"ABCD-EFGH","device_name":"Web – Chrome"}' }));
    await expect(redeemPairing("https://hub.example.test", "old-code", "Web", vi.fn<typeof fetch>().mockResolvedValue(response({}, 410)))).rejects.toThrow("expired or was already used");
  });
});
