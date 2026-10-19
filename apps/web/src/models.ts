export type Host = { id: string; name: string; status: string };
export type Session = { id: string; hostId: string; hostName: string; harness: string; status: string; awaiting?: string; repo?: string; title: string; activityAt?: string };
export type Snapshot = { hosts: Host[]; sessions: Session[] };
export type Message = { id: string; seq: number; role: string; text: string };
export type ProviderAccount = { provider: string; account: string; remaining: number | null; status: "healthy" | "low" | "unknown" };
const string = (value: unknown) => typeof value === "string" ? value : "";
const array = (value: unknown) => Array.isArray(value) ? value : [];
const record = (value: unknown): Record<string, unknown> => value && typeof value === "object" && !Array.isArray(value) ? value as Record<string, unknown> : {};

/** Maps the tolerant iOS /harness payload without inventing a web projection. */
export function mapSnapshot(payload: unknown): Snapshot {
  const source = record(payload);
  const hosts = array(source.hosts).map(record).filter((item) => string(item.host_id)).map((item) => ({ id: string(item.host_id), name: string(record(item.capabilities).display_name) || string(item.host_id), status: string(item.liveness) || string(item.status) || "offline" }));
  const hostNames = new Map(hosts.map((host) => [host.id, host.name]));
  const sessions = array(source.sessions).map(record).filter((item) => string(item.session_id)).map((item) => ({ id: string(item.session_id), hostId: string(item.host_id), hostName: string(item.host_display_name) || hostNames.get(string(item.host_id)) || string(item.host_id) || "Unknown host", harness: string(item.harness) || "Unknown harness", status: string(item.status) || "unknown", awaiting: string(item.awaiting) || undefined, repo: string(item.repo_name) || undefined, title: string(item.preview) || string(item.recap) || "Untitled session", activityAt: string(item.last_activity) || string(item.updated_at) || string(item.started_at) || undefined }));
  return { hosts, sessions };
}
export function mapMessagePage(payload: unknown): { messages: Message[]; hasOlder: boolean } { const source = record(payload); return { messages: array(source.messages).map(record).filter((item) => string(item.event_id)).map((item) => ({ id: string(item.event_id), seq: Number(item.seq) || 0, role: string(item.role) || "assistant", text: string(item.text) })), hasOlder: source.has_older === true }; }

const ago = (minutes: number) => new Date(Date.now() - minutes * 60_000).toISOString();
export const fixtureSnapshot: Snapshot = mapSnapshot({ hosts: [{ host_id: "studio", liveness: "online", capabilities: { display_name: "Mac Studio" } }, { host_id: "forge", liveness: "online", capabilities: { display_name: "Forge Linux" } }], sessions: [
  ["s1","studio","claude-code","working","input","arniesaha/drover","Review the web client home cards",2], ["s2","forge","codex","working","approval","arniesaha/drover","Confirm the migration boundary",5], ["s3","studio","codex","working","","acme/storefront","Tighten checkout form validation",11], ["s4","forge","claude-code","working","","acme/api","Trace the retry budget regression",18], ["s5","studio","codex","working","","arniesaha/drover","Package the client bundle into the wheel",32], ["s6","forge","codex","completed","","acme/worker","Add structured request logging",65], ["s7","studio","claude-code","completed","","acme/mobile","Audit Dynamic Type layouts",91], ["s8","forge","codex","errored","","acme/infra","Repair the preview deployment",128], ["s9","studio","claude-code","completed","","arniesaha/drover","Document pairing guidance",185], ["s10","forge","codex","working","input","acme/api","Choose a schema migration strategy",244], ["s11","studio","codex","completed","","acme/storefront","Reduce the image payload",330], ["s12","forge","claude-code","terminated","","acme/worker","Investigate flaky integration tests",460]
].map(([session_id,host_id,harness,status,awaiting,repo_name,preview,minutes]) => ({ session_id, host_id, harness, status, awaiting: awaiting || null, repo_name, preview, last_activity: ago(minutes as number) })) });
export const fixtureMessages: Message[] = mapMessagePage({ has_older: true, messages: [{ event_id: "m1", seq: 18, role: "user", text: "Please review the home cards against the iOS fleet view." }, { event_id: "m2", seq: 19, role: "assistant", text: "I’m comparing the card hierarchy, state labels, and host metadata now." }, { event_id: "m3", seq: 20, role: "tool", text: "rg --files apps/web apps/drover" }, { event_id: "m4", seq: 21, role: "assistant", text: "The fleet view now uses the same Tide palette and monospace repository labels." }] }).messages;
export const fixtureAccounts: ProviderAccount[] = [
  { provider: "Claude", account: "studio@acme.test", remaining: 74, status: "healthy" },
  { provider: "OpenAI", account: "forge@acme.test", remaining: 18, status: "low" },
  { provider: "Claude", account: "forge@acme.test", remaining: 61, status: "healthy" },
  { provider: "OpenAI", account: "studio@acme.test", remaining: null, status: "unknown" }
];
