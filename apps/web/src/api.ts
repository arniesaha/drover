import { mapFolderListing, mapMessagePage, mapModelCatalog, type Folder, type Message, type ModelOption } from "./models";

export type Connection = { hub: string; token: string };
type Fetch = typeof fetch;
export async function redeemPairing(hub: string, code: string, deviceName: string, fetcher: Fetch = fetch): Promise<Connection> {
  let response: Response;
  try { response = await fetcher(`${hub}/auth/pair`, { method: "POST", credentials: "omit", headers: { "Content-Type": "application/json" }, body: JSON.stringify({ code, device_name: deviceName }) }); }
  catch { throw new Error("Unable to pair with that hub. Check its address and try again."); }
  const body = await response.json().catch(() => ({})) as { token?: unknown; scope?: unknown; error?: unknown };
  if (!response.ok) {
    if (response.status === 410) throw new Error("That pairing code has expired or was already used. Generate a new code.");
    throw new Error(typeof body.error === "string" ? body.error : "Pairing was not accepted. Check the code and try a new one.");
  }
  if (body.scope !== "device" || typeof body.token !== "string" || !body.token) throw new Error("This code did not return a device credential. Use a device pairing code from the hub.");
  return { hub, token: body.token };
}
export class HubApi {
  constructor(private readonly connection: Connection, private readonly fetcher: Fetch = fetch) {}
  private async request<T>(path: string, init?: RequestInit, map?: (payload: unknown) => T): Promise<T> {
    let response: Response;
    try { response = await this.fetcher(`${this.connection.hub}${path}`, { ...init, headers: { Authorization: `Bearer ${this.connection.token}`, "Content-Type": "application/json", ...init?.headers }, credentials: "omit" }); }
    catch { throw new Error("Unable to reach this hub. Check the connection and try again."); }
    const payload = await response.json().catch(() => ({}));
    if (!response.ok) {
      const detail = typeof payload === "object" && payload ? (payload as { error?: unknown }).error : undefined;
      if (response.status === 401) throw new Error("This browser’s device credential is no longer valid. Pair again to continue.");
      throw new Error(typeof detail === "string" ? detail : `The hub could not complete this action (${response.status}).`);
    }
    return map ? map(payload) : payload as T;
  }
  get<T>(path: string, map?: (payload: unknown) => T) { return this.request(path, undefined, map); }
  post<T>(path: string, body: unknown = {}, map?: (payload: unknown) => T) { return this.request(path, { method: "POST", body: JSON.stringify(body) }, map); }
  catalog(hostID: string, harness: string) { return this.get(`/harness/hosts/${encodeURIComponent(hostID)}/model-catalog?harness=${encodeURIComponent(harness)}&refresh=0`, mapModelCatalog); }
  folders(hostID: string, path = "") { return this.get(`/harness/hosts/${encodeURIComponent(hostID)}/fs/list?path=${encodeURIComponent(path)}&limit=100`, mapFolderListing); }
  createFolder(hostID: string, path: string, name: string) { return this.post<{ path: string }>(`/harness/hosts/${encodeURIComponent(hostID)}/fs/create`, { path, name }); }
  launch(hostID: string, body: { harness: string; mode: string; prompt?: string; cwd?: string; model?: string; thinking_effort?: string }) { return this.post<{ session_id: string }>(`/harness/hosts/${encodeURIComponent(hostID)}/sessions`, body); }
  messages(sessionID: string, after?: number) { return this.get(`/harness/sessions/${encodeURIComponent(sessionID)}/messages?${after === undefined ? "limit=50" : `after_seq=${after}`}`, mapMessagePage); }
  olderMessages(sessionID: string, before: number) { return this.get(`/harness/sessions/${encodeURIComponent(sessionID)}/messages?before_seq=${before}&limit=50`, mapMessagePage); }
  turn(sessionID: string, text: string) { return this.post<{ turn_id: string }>(`/harness/sessions/${encodeURIComponent(sessionID)}/turns`, { text }); }
  permission(sessionID: string, requestID: string, decision: "allow" | "deny", note?: string) { return this.post(`/harness/sessions/${encodeURIComponent(sessionID)}/permission`, { request_id: requestID, decision, ...(note ? { note } : {}) }); }
  interrupt(sessionID: string) { return this.post(`/harness/sessions/${encodeURIComponent(sessionID)}/interrupt`); }
  terminate(sessionID: string) { return this.post(`/harness/sessions/${encodeURIComponent(sessionID)}/terminate`); }
}
export const mergeMessages = (existing: Message[], additions: Message[]) => [...existing, ...additions].filter((row, index, all) => all.findIndex((candidate) => candidate.id === row.id) === index).sort((a, b) => a.seq - b.seq);
export type { Folder, ModelOption };
