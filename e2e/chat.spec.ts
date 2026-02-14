import { test, expect } from "@playwright/test";

/**
 * Chat interaction tests.
 *
 * Seed data includes:
 *   - 5 messages (boss↔manager, manager↔alice)
 *   - 2 system events referencing T0001 and T0002
 *   - Message "Great, also check T0002 status." contains T0002 in body
 *   - Message "Please kick off the project." has task_id=T0001 badge in header
 */

const TEAM = "testteam";

test.describe("Chat interactions", () => {
  test.beforeEach(async ({ page }) => {
    await page.goto("/chat");
    await page.waitForLoadState("domcontentloaded");

    // Wait for the sidebar nav to be visible — confirms the app shell rendered.
    await expect(page.locator(".sb-nav-btn").first()).toBeVisible({ timeout: 10_000 });

    // Wait for seeded messages to load — we need at least 5 chat messages
    // (the seed creates 5 chat messages + 2 events for testteam).
    // Waiting for a specific count avoids the pitfall where only an
    // auto-generated greeting message satisfies a single-element wait.
    await page.waitForFunction(
      () => document.querySelectorAll(".msg").length >= 5,
      { timeout: 15_000 },
    );

    // Small settle time for rendering to complete
    await page.waitForTimeout(500);
  });

  test("task ID link in chat message body opens task panel", async ({ page }) => {
    // The message "Great, also check T0002 status." contains T0002 as a clickable link
    const msg = page.locator(".msg-content", { hasText: "T0002 status" });
    await expect(msg).toBeVisible({ timeout: 10_000 });

    // Click the T0002 link inside the message
    const taskLink = msg.locator("[data-task-id='2']");
    await expect(taskLink).toBeVisible({ timeout: 5_000 });
    await taskLink.click();

    // Task panel should open with T0002
    const panel = page.locator(".task-panel");
    await expect(panel).toBeVisible({ timeout: 5_000 });
    await expect(panel.locator(".task-panel-id")).toContainText("T0002");
  });

  test("system event task link opens task panel", async ({ page }) => {
    // System event: "Alice started working on T0001"
    const event = page.locator(".msg-event", { hasText: "Alice started working on" });
    await expect(event).toBeVisible({ timeout: 10_000 });

    // Click the T0001 link in the event
    const taskLink = event.locator("[data-task-id='1']");
    await expect(taskLink).toBeVisible({ timeout: 5_000 });
    await taskLink.click();

    // Task panel should open with T0001
    const panel = page.locator(".task-panel");
    await expect(panel).toBeVisible({ timeout: 5_000 });
    await expect(panel.locator(".task-panel-id")).toContainText("T0001");
  });

  test("message sender name opens agent panel", async ({ page }) => {
    // Click on "Edison" sender name in a message header
    const sender = page.locator(".msg-sender", { hasText: "Edison" }).first();
    await expect(sender).toBeVisible({ timeout: 10_000 });
    // Use position to avoid the CopyBtn (which appears on hover and has stopPropagation)
    await sender.click({ position: { x: 5, y: 8 } });

    // Agent/diff panel should open for edison — check for "open" class
    // (diff-panel is always in DOM, just off-screen without .open)
    const panel = page.locator(".diff-panel");
    await expect(panel).toHaveClass(/open/, { timeout: 5_000 });
    await expect(panel.locator(".diff-panel-title")).toContainText("Edison");
  });

  test("message header task badge opens task panel", async ({ page }) => {
    // Find a message with a task badge in the header (messages with task_id)
    const badge = page.locator(".msg-task-badge", { hasText: "T0001" }).first();
    await expect(badge).toBeVisible({ timeout: 10_000 });
    // Use position to avoid the CopyBtn (which appears on hover and has stopPropagation)
    await badge.click({ position: { x: 5, y: 8 } });

    // Task panel should open with T0001
    const panel = page.locator(".task-panel");
    await expect(panel).toBeVisible({ timeout: 5_000 });
    await expect(panel.locator(".task-panel-id")).toContainText("T0001");
  });
});
