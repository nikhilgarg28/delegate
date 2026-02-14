import { test, expect } from "@playwright/test";

/**
 * Tests for the Cmd+K team quick-switcher.
 *
 * These tests verify:
 * - Cmd+K opens the team switcher modal
 * - Team list is searchable
 * - Keyboard navigation (up/down/enter/esc) works
 * - Switching teams updates the UI
 * - PillSelect team dropdown on chat page works
 *
 * Note: URLs are flat (e.g. /chat, /tasks) — team is tracked in JS state, not URL.
 */

const TEAM = "testteam";
const TEAM2 = "otherteam";

/** Navigate and wait for the page + keyboard handlers to be ready. */
async function gotoReady(page: any, path: string) {
  await page.goto(path);
  await page.waitForLoadState("domcontentloaded");
  // Ensure useEffect keyboard handlers are registered (SSE keeps connections
  // open so networkidle would time out)
  await page.waitForTimeout(500);
}

test.describe("Team Switcher", () => {
  test("Cmd+K opens team switcher modal", async ({ page }) => {
    await gotoReady(page, "/chat");

    // Modal should not be visible initially
    await expect(page.locator(".team-switcher-backdrop")).not.toBeVisible();

    // Press Cmd+K (or Ctrl+K on non-Mac)
    const modifier = process.platform === "darwin" ? "Meta" : "Control";
    await page.keyboard.press(`${modifier}+KeyK`);

    // Modal should appear
    await expect(page.locator(".team-switcher-backdrop")).toBeVisible();
    await expect(page.locator(".team-switcher-modal")).toBeVisible();
    await expect(page.locator(".team-switcher-input")).toBeFocused();
  });

  test("team list shows both teams", async ({ page }) => {
    await gotoReady(page, "/chat");
    const modifier = process.platform === "darwin" ? "Meta" : "Control";
    await page.keyboard.press(`${modifier}+KeyK`);

    // Both teams should be visible
    await expect(page.locator(".team-switcher-item")).toHaveCount(2);
    await expect(
      page.locator(".team-switcher-item-name", { hasText: "Testteam" })
    ).toBeVisible();
    await expect(
      page.locator(".team-switcher-item-name", { hasText: "Otherteam" })
    ).toBeVisible();

    // Current team (testteam) should have checkmark
    const currentTeamItem = page.locator(".team-switcher-item.current");
    await expect(currentTeamItem).toBeVisible();
    await expect(currentTeamItem.locator(".team-switcher-item-name")).toContainText(
      "Testteam"
    );
  });

  test("search filters team list", async ({ page }) => {
    await gotoReady(page, "/chat");
    const modifier = process.platform === "darwin" ? "Meta" : "Control";
    await page.keyboard.press(`${modifier}+KeyK`);

    // Type search query
    await page.locator(".team-switcher-input").fill("other");

    // Only one team should match
    await expect(page.locator(".team-switcher-item")).toHaveCount(1);
    await expect(
      page.locator(".team-switcher-item-name", { hasText: "Otherteam" })
    ).toBeVisible();
    await expect(
      page.locator(".team-switcher-item-name", { hasText: "Testteam" })
    ).not.toBeVisible();
  });

  test("keyboard navigation (down/up/enter)", async ({ page }) => {
    await gotoReady(page, "/chat");
    const modifier = process.platform === "darwin" ? "Meta" : "Control";
    await page.keyboard.press(`${modifier}+KeyK`);

    // First item should be selected by default
    await expect(page.locator(".team-switcher-item.selected").first()).toBeVisible();
    // Allow time for keyboard event listeners to register in effects
    await page.waitForTimeout(200);

    // Press down arrow to select second item
    await page.keyboard.press("ArrowDown");
    const selectedItems = page.locator(".team-switcher-item.selected");
    await expect(selectedItems).toHaveCount(1);

    // Press up arrow to go back to first item
    await page.keyboard.press("ArrowUp");
    await expect(selectedItems.first()).toBeVisible();

    // Press Enter to switch to selected team
    await page.keyboard.press("Enter");

    // Modal should close
    await expect(page.locator(".team-switcher-backdrop")).not.toBeVisible();
  });

  test("switching teams updates UI", async ({ page }) => {
    await gotoReady(page, "/chat");
    const modifier = process.platform === "darwin" ? "Meta" : "Control";
    await page.keyboard.press(`${modifier}+KeyK`);

    // Click on the other team
    await page
      .locator(".team-switcher-item-name", { hasText: "Otherteam" })
      .click();

    // Modal should close
    await expect(page.locator(".team-switcher-backdrop")).not.toBeVisible();

    // Wait for team switch to take effect
    await page.waitForTimeout(500);

    // Open switcher again to verify current team changed
    await page.keyboard.press(`${modifier}+KeyK`);
    const currentTeamItem = page.locator(".team-switcher-item.current");
    await expect(
      currentTeamItem.locator(".team-switcher-item-name")
    ).toContainText("Otherteam");
  });

  test("Escape closes team switcher", async ({ page }) => {
    await gotoReady(page, "/chat");
    const modifier = process.platform === "darwin" ? "Meta" : "Control";
    await page.keyboard.press(`${modifier}+KeyK`);

    await expect(page.locator(".team-switcher-backdrop")).toBeVisible();

    // Press Escape
    await page.keyboard.press("Escape");

    // Modal should close
    await expect(page.locator(".team-switcher-backdrop")).not.toBeVisible();
  });

  test("clicking backdrop closes team switcher", async ({ page }) => {
    await gotoReady(page, "/chat");
    const modifier = process.platform === "darwin" ? "Meta" : "Control";
    await page.keyboard.press(`${modifier}+KeyK`);

    await expect(page.locator(".team-switcher-backdrop")).toBeVisible();

    // Click backdrop (outside modal)
    await page.locator(".team-switcher-backdrop").click({ position: { x: 10, y: 10 } });

    // Modal should close
    await expect(page.locator(".team-switcher-backdrop")).not.toBeVisible();
  });

  test("team selector works from tasks page", async ({ page }) => {
    await gotoReady(page, "/tasks");
    const modifier = process.platform === "darwin" ? "Meta" : "Control";
    await page.keyboard.press(`${modifier}+KeyK`);

    // Modal should open
    await expect(page.locator(".team-switcher-backdrop")).toBeVisible();

    // Switch to other team
    await page
      .locator(".team-switcher-item-name", { hasText: "Otherteam" })
      .click();

    // Modal should close
    await expect(page.locator(".team-switcher-backdrop")).not.toBeVisible();

    // Wait for team switch
    await page.waitForTimeout(500);

    // Verify via re-opening switcher that team changed
    await page.keyboard.press(`${modifier}+KeyK`);
    const currentTeamItem = page.locator(".team-switcher-item.current");
    await expect(
      currentTeamItem.locator(".team-switcher-item-name")
    ).toContainText("Otherteam");
  });

  test("chat page PillSelect team dropdown works", async ({ page }) => {
    await gotoReady(page, "/chat");

    // Wait for page to load — the Team PillSelect should be visible in chat filters
    const teamPill = page.locator(".pill-select").first();
    await expect(teamPill).toBeVisible({ timeout: 5_000 });

    // Click the PillSelect value to open dropdown
    await teamPill.locator(".pill-select-value").click();

    // Dropdown should appear
    const dropdown = page.locator(".fb-dropdown");
    await expect(dropdown).toBeVisible({ timeout: 2_000 });
  });

  test("switching teams via PillSelect dropdown works", async ({ page }) => {
    await gotoReady(page, "/chat");

    // Wait for page to load
    const teamPill = page.locator(".pill-select").first();
    await expect(teamPill).toBeVisible({ timeout: 5_000 });

    // Click team selector
    await teamPill.locator(".pill-select-value").click();
    const dropdown = page.locator(".fb-dropdown");
    await expect(dropdown).toBeVisible({ timeout: 2_000 });

    // Click other team
    await dropdown.locator(".fb-dropdown-item", { hasText: "Otherteam" }).click();

    // Wait for team switch
    await page.waitForTimeout(500);

    // Verify by opening Cmd+K switcher
    const modifier = process.platform === "darwin" ? "Meta" : "Control";
    await page.keyboard.press(`${modifier}+KeyK`);
    const currentTeamItem = page.locator(".team-switcher-item.current");
    await expect(
      currentTeamItem.locator(".team-switcher-item-name")
    ).toContainText("Otherteam");
  });
});
