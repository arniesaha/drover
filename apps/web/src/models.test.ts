import { describe, expect, it } from "vitest";
import { mapAnalytics, mapHistory, mapInsights, mapMessagePage, mapProviderAccounts, mapSnapshot } from "./models";

describe("iOS wire payload mapping", () => {
  it("keeps host display names and maps useful fallback session fields", () => {
    const fleet = mapSnapshot({ hosts: [{ host_id: "studio", liveness: "online", capabilities: { display_name: "Mac Studio" } }], sessions: [{ session_id: "run-1", host_id: "studio", harness: "codex", status: "working", awaiting: "input", repo_name: "acme/drover", recap: "Use recap when preview is absent", updated_at: "2026-10-19T10:00:00Z" }, { harness: "bad row" }] });
    expect(fleet.hosts).toEqual([{ id: "studio", name: "Mac Studio", status: "online", harnesses: [] }]);
    expect(fleet.sessions).toEqual([{ id: "run-1", hostId: "studio", hostName: "Mac Studio", harness: "codex", mode: undefined, status: "working", awaiting: "input", repo: "acme/drover", cwd: undefined, model: undefined, title: "Use recap when preview is absent", activityAt: "2026-10-19T10:00:00Z", startedAt: undefined, updatedAt: "2026-10-19T10:00:00Z", endedAt: undefined }]);
  });
  it("handles a real-shaped bounded transcript page", () => {
    const page = mapMessagePage({ messages: [{ event_id: "evt-4", seq: 4, role: "assistant", type: "assistant_output", text: "A safe response." }, { type: "bad" }], page_min_seq: 4, page_max_seq: 4, max_seq: 9, has_older: true, has_newer: true });
    expect(page).toEqual({ hasOlder: true, messages: [{ id: "evt-4", seq: 4, type: "assistant_output", role: "assistant", text: "A safe response.", timestamp: undefined, payload: {} }] });
  });
  it("retains unknown quota instead of treating it as empty capacity", () => {
    expect(mapProviderAccounts({ provider_capacity: { status: "ok", data: [{ snapshot_id: "quota-1", provider: "openai", account_label: "team@example.test", host_id: "studio", status: "stale", observed_at: "2026-10-19T10:00:00Z", windows: [{ kind: "five_hour", used_percent: 92 }, { kind: "seven_day" }] }] } })).toEqual([{ id: "quota-1", provider: "openai", account: "team@example.test", plan: undefined, hostId: "studio", status: "stale", observedAt: "2026-10-19T10:00:00Z", source: undefined, errorCategory: undefined, windows: [{ kind: "five_hour", usedPercent: 92, resetsAt: undefined }, { kind: "seven_day", usedPercent: undefined, resetsAt: undefined }] }]);
  });
  it("maps the iOS insights and history endpoints without inventing values", () => {
    expect(mapInsights({ findings: [{ finding_id: "f-1", title: "Unsafe retry", severity: "high", confidence: "likely", state: "open", target_id: "acme/api", last_seen_at: "2026-10-19T10:00:00Z" }] })).toEqual([{ id: "f-1", title: "Unsafe retry", severity: "high", confidence: "likely", state: "open", target: "acme/api", host: undefined, harness: undefined, lastSeenAt: "2026-10-19T10:00:00Z" }]);
    expect(mapHistory({ items: [{ id: "h-1", title: "Session", harness: "codex", host: { name: "Studio" }, repo: "acme/app", state: "finished", tokens: { input: 8, output: 2 } }], has_more: true, next_cursor: "next" })).toEqual({ items: [{ id: "h-1", title: "Session", harness: "codex", model: undefined, host: "Studio", repo: "acme/app", state: "finished", status: undefined, lastActivity: undefined, summary: undefined, tokens: 10 }], hasMore: true, nextCursor: "next" });
  });
  it("maps the analytics envelope and keeps zero totals", () => {
    expect(mapAnalytics({ activity: { observed_at: "2026-10-19T10:00:00Z", data: { totals: { session_count: 0, total_tokens: 12, cost_usd: 1.25, total_latency_ms: 500 }, coverage: { status: "partial" }, projects: [{ project_key: "acme/app", session_count: 2, total_tokens: 12, cost_usd: 1.25 }] } } })).toEqual({ totals: { sessions: 0, tokens: 12, cost: 1.25, latencyMs: 500 }, coverage: "partial", observedAt: "2026-10-19T10:00:00Z", projects: [{ label: "acme/app", sessions: 2, tokens: 12, cost: 1.25 }], harnesses: [], hosts: [], models: [] });
  });
});
