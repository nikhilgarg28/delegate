import { test, expect } from "@playwright/test";

/**
 * Basic smoke tests for the Delegate UI.
 *
 * These tests run against a pre-seeded Delegate instance with:
 *   - Team: "testteam" (boss: "testboss", manager: "edison", agents: alice, bob)
 *   - 3 tasks: T0001 (todo), T0002 (in_progress, has attachment), T0003 (done)
 *   - 5 chat messages + 2 system events
 *
 * Default task filters hide "done" and "cancelled", so only T0001 and T0002
 * are visible in the task list by default.
 */

const TEAM = "testteam";

test.describe("Smoke tests", () => {
  test("app loads and shows chat with messages", async ({ page }) => {
    await page.goto("/chat");

    // Sidebar should be visible with nav buttons
    await expect(page.locator(".sb-nav-btn").first()).toBeVisible();

    // Chat tab should be active
    await expect(page.locator(".sb-nav-btn.active")).toContainText("Chat");

    // Should see at least one chat message from the seeded data
    await expect(page.locator(".msg, .msg-event").first()).toBeVisible({
      timeout: 5_000,
    });
  });

  test("tab switching works (Chat → Tasks → Agents)", async ({ page }) => {
    await page.goto("/chat");

    // Wait for initial load
    await expect(page.locator(".sb-nav-btn.active")).toContainText("Chat");

    // Switch to Tasks tab
    await page.locator(".sb-nav-btn", { hasText: "Tasks" }).click();
    await expect(page).toHaveURL(/\/tasks/);

    // Should see task rows (default filters hide "done" — only T0001, T0002)
    await expect(page.locator(".task-row").first()).toBeVisible({
      timeout: 5_000,
    });
    await expect(page.locator(".task-row")).toHaveCount(2);

    // Switch to Agents tab
    await page.locator(".sb-nav-btn", { hasText: "Agents" }).click();
    await expect(page).toHaveURL(/\/agents/);

    // Should see agent cards (edison, alice, bob)
    await expect(page.locator(".agent-card-rich").first()).toBeVisible({
      timeout: 5_000,
    });

    // Switch back to Chat
    await page.locator(".sb-nav-btn", { hasText: "Chat" }).click();
    await expect(page).toHaveURL(/\/chat/);
  });

  test("clicking a task opens the side panel", async ({ page }) => {
    await page.goto("/tasks");

    // Wait for task list to load
    await expect(page.locator(".task-row").first()).toBeVisible({
      timeout: 5_000,
    });

    // Click on T0002 (the in_progress task with an attachment)
    await page.locator(".task-row", { hasText: "Implement design system" }).click();

    // Task side panel should open
    const panel = page.locator(".task-panel");
    await expect(panel).toBeVisible({ timeout: 3_000 });

    // Panel header should show the task ID and title
    await expect(panel.locator(".task-panel-id")).toContainText("T0002");
    await expect(panel.locator(".task-panel-title")).toContainText(
      "Implement design system"
    );

    // The Overview tab should show the description
    await expect(panel.locator(".task-panel-desc")).toContainText(
      "Build the design system"
    );

    // Should show the attachment
    await expect(panel.locator(".task-attachment-name")).toContainText(
      "design-brief.md"
    );

    // Close via Escape — panel is removed from DOM entirely
    await page.keyboard.press("Escape");
    await expect(panel).not.toBeVisible({ timeout: 2_000 });
  });
});
