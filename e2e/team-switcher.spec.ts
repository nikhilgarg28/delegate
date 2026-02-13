import { test, expect } from "@playwright/test";

/**
 * Tests for the Cmd+K team quick-switcher.
 *
 * These tests verify:
 * - Cmd+K opens the team switcher modal
 * - Team list is searchable
 * - Keyboard navigation (up/down/enter/esc) works
 * - Switching teams updates the URL and UI
 * - Team selector dropdown opens downward on chat page
 */

const TEAM = "testteam";
const TEAM2 = "otherteam";

test.describe("Team Switcher", () => {
  test("Cmd+K opens team switcher modal", async ({ page }) => {
    await page.goto(`/${TEAM}/chat`);

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
    await page.goto(`/${TEAM}/chat`);
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
    await page.goto(`/${TEAM}/chat`);
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
    await page.goto(`/${TEAM}/chat`);
    const modifier = process.platform === "darwin" ? "Meta" : "Control";
    await page.keyboard.press(`${modifier}+KeyK`);

    // First item should be selected by default
    await expect(page.locator(".team-switcher-item.selected").first()).toBeVisible();

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

  test("switching teams updates URL and content", async ({ page }) => {
    await page.goto(`/${TEAM}/chat`);
    const modifier = process.platform === "darwin" ? "Meta" : "Control";
    await page.keyboard.press(`${modifier}+KeyK`);

    // Click on the other team
    await page
      .locator(".team-switcher-item-name", { hasText: "Otherteam" })
      .click();

    // Modal should close
    await expect(page.locator(".team-switcher-backdrop")).not.toBeVisible();

    // URL should update
    await expect(page).toHaveURL(`/${TEAM2}/chat`);

    // Wait for chat to load
    await page.waitForTimeout(500);

    // Open switcher again to verify current team changed
    await page.keyboard.press(`${modifier}+KeyK`);
    const currentTeamItem = page.locator(".team-switcher-item.current");
    await expect(
      currentTeamItem.locator(".team-switcher-item-name")
    ).toContainText("Otherteam");
  });

  test("Escape closes team switcher", async ({ page }) => {
    await page.goto(`/${TEAM}/chat`);
    const modifier = process.platform === "darwin" ? "Meta" : "Control";
    await page.keyboard.press(`${modifier}+KeyK`);

    await expect(page.locator(".team-switcher-backdrop")).toBeVisible();

    // Press Escape
    await page.keyboard.press("Escape");

    // Modal should close
    await expect(page.locator(".team-switcher-backdrop")).not.toBeVisible();
  });

  test("clicking backdrop closes team switcher", async ({ page }) => {
    await page.goto(`/${TEAM}/chat`);
    const modifier = process.platform === "darwin" ? "Meta" : "Control";
    await page.keyboard.press(`${modifier}+KeyK`);

    await expect(page.locator(".team-switcher-backdrop")).toBeVisible();

    // Click backdrop (outside modal)
    await page.locator(".team-switcher-backdrop").click({ position: { x: 10, y: 10 } });

    // Modal should close
    await expect(page.locator(".team-switcher-backdrop")).not.toBeVisible();
  });

  test("team selector works from tasks page", async ({ page }) => {
    await page.goto(`/${TEAM}/tasks`);
    const modifier = process.platform === "darwin" ? "Meta" : "Control";
    await page.keyboard.press(`${modifier}+KeyK`);

    // Modal should open
    await expect(page.locator(".team-switcher-backdrop")).toBeVisible();

    // Switch to other team
    await page
      .locator(".team-switcher-item-name", { hasText: "Otherteam" })
      .click();

    // URL should update with tasks tab preserved
    await expect(page).toHaveURL(`/${TEAM2}/tasks`);
  });

  test("chat page team dropdown opens downward", async ({ page }) => {
    await page.goto(`/${TEAM}/chat`);

    // Wait for page to load
    await expect(page.locator(".chat-team-select")).toBeVisible();

    // Click team selector to open dropdown
    await page.locator(".chat-team-select .csel-trigger").click();

    // Check that dropdown is visible and positioned below the trigger
    const menu = page.locator(".chat-team-select .csel-menu");
    await expect(menu).toBeVisible();

    // Get bounding boxes
    const triggerBox = await page
      .locator(".chat-team-select .csel-trigger")
      .boundingBox();
    const menuBox = await menu.boundingBox();

    // Menu should be below trigger (menu.top > trigger.bottom)
    expect(menuBox!.y).toBeGreaterThan(triggerBox!.y + triggerBox!.height);
  });

  test("switching teams via dropdown works", async ({ page }) => {
    await page.goto(`/${TEAM}/chat`);

    // Click team selector
    await page.locator(".chat-team-select .csel-trigger").click();

    // Click other team
    await page.locator(".csel-option", { hasText: "Otherteam" }).click();

    // URL should update
    await expect(page).toHaveURL(`/${TEAM2}/chat`);
  });
});
