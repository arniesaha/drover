import { describe, expect, it } from "vitest";
import { ScreenHeader, StatusChip, statusLabels } from "./components";

describe("shared design-system components", () => {
  it("exposes only the canonical status labels", () => {
    expect(statusLabels).toEqual(["Needs input", "Needs approval", "Running", "Finished", "Failed", "Terminated", "Unknown"]);
    const chip = StatusChip({ status: "Needs approval" });
    expect(chip.props.class).toContain("needs-approval");
    expect(chip.props.children).toContain("Needs approval");
  });
  it("always renders a header title", () => {
    const header = ScreenHeader({ title: "Inbox", eyebrow: "ATTENTION" });
    const top = header.props.children[0];
    expect(top.props.children[0].props.children[1].type).toBe("h1");
    expect(top.props.children[0].props.children[1].props.children).toBe("Inbox");
  });
});
