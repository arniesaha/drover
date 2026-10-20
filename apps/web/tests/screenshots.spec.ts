import { expect, test } from "@playwright/test";

const home = "/?fixture=1#/home";
const open = async (page: import("@playwright/test").Page, path: string) => {
  await page.addInitScript(() => { Date.now = () => new Date("2026-10-19T18:00:00.000Z").valueOf(); });
  await page.goto(path);
};
for (const theme of ["dark", "light"] as const) {
  test.describe(`${theme} fixture views`, () => {
    test.use({ colorScheme: theme });
    test("home wide", async ({ page }) => { await page.setViewportSize({ width: 1440, height: 900 }); await open(page, home); await expect(page).toHaveScreenshot(`home-wide-${theme}.png`, { fullPage: true }); });
    test("home filtered narrow", async ({ page }) => { await page.setViewportSize({ width: 390, height: 844 }); await open(page, home); await page.locator(".header-filters").getByRole("button", { name: "Mac Studio", exact: true }).click(); await expect(page).toHaveScreenshot(`home-filtered-narrow-${theme}.png`, { fullPage: true }); });
    test("session detail wide", async ({ page }) => { await page.setViewportSize({ width: 1440, height: 900 }); await open(page, "/?fixture=1#/sessions/s1"); await expect(page).toHaveScreenshot(`session-detail-wide-${theme}.png`, { fullPage: true }); });
    test("new session wide", async ({ page }) => { await page.setViewportSize({ width: 1440, height: 900 }); await open(page, "/?fixture=1#/new-session"); await expect(page.getByRole("heading", { name: "New Session" })).toBeVisible(); await expect(page).toHaveScreenshot(`new-session-wide-${theme}.png`, { fullPage: true }); });
    test("connect narrow", async ({ page }) => { await page.setViewportSize({ width: 390, height: 844 }); await open(page, "/#/connection"); await expect(page).toHaveScreenshot(`connect-narrow-${theme}.png`, { fullPage: true }); });
  });
}
