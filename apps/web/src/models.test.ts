import { describe, expect, it } from "vitest";
import { mapMessagePage, mapSnapshot } from "./models";

describe("iOS wire payload mapping", () => {
  it("keeps host display names and maps useful fallback session fields", () => {
    const fleet = mapSnapshot({ hosts: [{ host_id: "studio", liveness: "online", capabilities: { display_name: "Mac Studio" } }], sessions: [{ session_id: "run-1", host_id: "studio", harness: "codex", status: "working", awaiting: "input", repo_name: "acme/drover", recap: "Use recap when preview is absent", updated_at: "2026-10-19T10:00:00Z" }, { harness: "bad row" }] });
    expect(fleet.hosts).toEqual([{ id: "studio", name: "Mac Studio", status: "online" }]);
    expect(fleet.sessions).toEqual([{ id: "run-1", hostId: "studio", hostName: "Mac Studio", harness: "codex", mode: undefined, status: "working", awaiting: "input", repo: "acme/drover", cwd: undefined, model: undefined, title: "Use recap when preview is absent", activityAt: "2026-10-19T10:00:00Z", startedAt: undefined, updatedAt: "2026-10-19T10:00:00Z", endedAt: undefined }]);
  });
  it("handles a real-shaped bounded transcript page", () => {
    const page = mapMessagePage({ messages: [{ event_id: "evt-4", seq: 4, role: "assistant", type: "assistant_output", text: "A safe response." }, { type: "bad" }], page_min_seq: 4, page_max_seq: 4, max_seq: 9, has_older: true, has_newer: true });
    expect(page).toEqual({ hasOlder: true, messages: [{ id: "evt-4", seq: 4, type: "assistant_output", role: "assistant", text: "A safe response.", timestamp: undefined }] });
  });
});
