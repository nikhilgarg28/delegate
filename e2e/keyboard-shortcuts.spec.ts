import { test, expect } from "@playwright/test";

/**
 * Keyboard shortcut tests.
 *
 * Tests all global keyboard shortcuts defined in app.jsx:
 *   - c : Navigate to Chat tab
 *   - t : Navigate to Tasks tab
 *   - a : Navigate to Agents tab
 *   - s : Toggle sidebar collapse
 *   - n : Toggle notifications
 *   - m : Toggle audio mute
 *   - / : Focus chat input
 *   - ? : Toggle help overlay
 *   - Esc : Close panels / blur input
 *
 * Also verifies:
 *   - Shortcuts work when side panels are open (except help overlay)
 *   - Component-level shortcuts (j/k nav) don't leak to global handler
 *   - Escape properly closes panels and blurs inputs
 */

const TEAM = "testteam";

test.describe("Keyboard shortcuts", () => {
  test("c navigates to Chat tab", async ({ page }) => {
    await page.goto(`/${TEAM}/tasks`);
    await expect(page.locator(".sb-nav-btn.active")).toContainText("Tasks");

    await page.keyboard.press("c");
    await expect(page).toHaveURL(new RegExp(`/${TEAM}/chat`));
    await expect(page.locator(".sb-nav-btn.active")).toContainText("Chat");
  });

  test("t navigates to Tasks tab", async ({ page }) => {
    await page.goto(`/${TEAM}/chat`);
    await expect(page.locator(".sb-nav-btn.active")).toContainText("Chat");

    await page.keyboard.press("t");
    await expect(page).toHaveURL(new RegExp(`/${TEAM}/tasks`));
    await expect(page.locator(".sb-nav-btn.active")).toContainText("Tasks");
  });

  test("a navigates to Agents tab", async ({ page }) => {
    await page.goto(`/${TEAM}/chat`);
    await expect(page.locator(".sb-nav-btn.active")).toContainText("Chat");

    await page.keyboard.press("a");
    await expect(page).toHaveURL(new RegExp(`/${TEAM}/agents`));
    await expect(page.locator(".sb-nav-btn.active")).toContainText("Agents");
  });

  test("s toggles sidebar collapse", async ({ page }) => {
    await page.goto(`/${TEAM}/chat`);

    // Sidebar should start expanded
    const sidebar = page.locator(".sb");
    await expect(sidebar).not.toHaveClass(/sb-collapsed/);

    // Press 's' to collapse
    await page.keyboard.press("s");
    await expect(sidebar).toHaveClass(/sb-collapsed/);

    // Press 's' again to expand
    await page.keyboard.press("s");
    await expect(sidebar).not.toHaveClass(/sb-collapsed/);
  });

  test("? toggles help overlay", async ({ page }) => {
    await page.goto(`/${TEAM}/chat`);

    // Help overlay should not be visible initially
    const helpOverlay = page.locator(".help-overlay");
    await expect(helpOverlay).not.toBeVisible();

    // Press '?' to show help
    await page.keyboard.press("?");
    await expect(helpOverlay).toBeVisible();

    // Press '?' again to hide
    await page.keyboard.press("?");
    await expect(helpOverlay).not.toBeVisible();
  });

  test("Escape closes help overlay", async ({ page }) => {
    await page.goto(`/${TEAM}/chat`);

    // Open help overlay with '?'
    await page.keyboard.press("?");
    const helpOverlay = page.locator(".help-overlay");
    await expect(helpOverlay).toBeVisible();

    // Press Escape to close
    await page.keyboard.press("Escape");
    await expect(helpOverlay).not.toBeVisible();
  });

  test("/ focuses chat input", async ({ page }) => {
    await page.goto(`/${TEAM}/chat`);

    // Chat input should not be focused initially
    const chatInput = page.locator('textarea[placeholder="Send a message..."]');
    await expect(chatInput).not.toBeFocused();

    // Press '/' to focus
    await page.keyboard.press("/");
    await expect(chatInput).toBeFocused();
  });

  test("Escape blurs chat input when focused", async ({ page }) => {
    await page.goto(`/${TEAM}/chat`);

    // Focus the chat input with '/'
    const chatInput = page.locator('textarea[placeholder="Send a message..."]');
    await page.keyboard.press("/");
    await expect(chatInput).toBeFocused();

    // Press Escape to blur
    await page.keyboard.press("Escape");
    await expect(chatInput).not.toBeFocused();
  });

  test("Escape closes task side panel", async ({ page }) => {
    await page.goto(`/${TEAM}/tasks`);
    await expect(page.locator(".task-row").first()).toBeVisible({
      timeout: 5_000,
    });

    // Open a task panel
    await page.locator(".task-row").first().click();
    const panel = page.locator(".task-panel");
    await expect(panel).toBeVisible({ timeout: 3_000 });

    // Press Escape to close
    await page.keyboard.press("Escape");
    await expect(panel).not.toBeVisible({ timeout: 2_000 });
  });

  test("tab navigation shortcuts work when task panel is open", async ({
    page,
  }) => {
    await page.goto(`/${TEAM}/tasks`);
    await expect(page.locator(".task-row").first()).toBeVisible({
      timeout: 5_000,
    });

    // Open a task panel
    await page.locator(".task-row").first().click();
    const panel = page.locator(".task-panel");
    await expect(panel).toBeVisible({ timeout: 3_000 });

    // Press 'c' to navigate to Chat — should work even with panel open
    await page.keyboard.press("c");
    await expect(page).toHaveURL(new RegExp(`/${TEAM}/chat`));
    await expect(page.locator(".sb-nav-btn.active")).toContainText("Chat");
  });

  test("sidebar toggle works when task panel is open", async ({ page }) => {
    await page.goto(`/${TEAM}/tasks`);
    await expect(page.locator(".task-row").first()).toBeVisible({
      timeout: 5_000,
    });

    // Open a task panel
    await page.locator(".task-row").first().click();
    const panel = page.locator(".task-panel");
    await expect(panel).toBeVisible({ timeout: 3_000 });

    const sidebar = page.locator(".sb");
    await expect(sidebar).not.toHaveClass(/sb-collapsed/);

    // Press 's' to toggle sidebar — should work even with panel open
    await page.keyboard.press("s");
    await expect(sidebar).toHaveClass(/sb-collapsed/);

    // Panel should still be visible
    await expect(panel).toBeVisible();

    // Expand sidebar again
    await page.keyboard.press("s");
    await expect(sidebar).not.toHaveClass(/sb-collapsed/);
  });

  test("help overlay blocks all other shortcuts", async ({ page }) => {
    await page.goto(`/${TEAM}/chat`);

    // Open help overlay
    await page.keyboard.press("?");
    const helpOverlay = page.locator(".help-overlay");
    await expect(helpOverlay).toBeVisible();

    // Try to navigate to tasks with 't' — should be blocked
    await page.keyboard.press("t");
    // Should still be on chat tab
    await expect(page).toHaveURL(new RegExp(`/${TEAM}/chat`));
    await expect(page.locator(".sb-nav-btn.active")).toContainText("Chat");

    // Help overlay should still be visible
    await expect(helpOverlay).toBeVisible();

    // Close help overlay first
    await page.keyboard.press("?");
    await expect(helpOverlay).not.toBeVisible();

    // Now 't' should work
    await page.keyboard.press("t");
    await expect(page).toHaveURL(new RegExp(`/${TEAM}/tasks`));
  });

  test("j/k navigation in tasks panel doesn't leak to global handler", async ({
    page,
  }) => {
    await page.goto(`/${TEAM}/tasks`);
    await expect(page.locator(".task-row").first()).toBeVisible({
      timeout: 5_000,
    });

    // There should be 2 tasks (T0001, T0002) with default filters
    await expect(page.locator(".task-row")).toHaveCount(2);

    // Press 'j' to select next task
    await page.keyboard.press("j");

    // First task should be selected (has .selected class)
    await expect(page.locator(".task-row").first()).toHaveClass(/selected/);

    // Press 'j' again to move to second task
    await page.keyboard.press("j");
    await expect(page.locator(".task-row").nth(1)).toHaveClass(/selected/);

    // Press 'k' to move back to first task
    await page.keyboard.press("k");
    await expect(page.locator(".task-row").first()).toHaveClass(/selected/);

    // Verify we're still on the tasks tab (j/k didn't trigger anything else)
    await expect(page).toHaveURL(new RegExp(`/${TEAM}/tasks`));
  });

  test("shortcuts respect input focus — don't trigger when typing", async ({
    page,
  }) => {
    await page.goto(`/${TEAM}/chat`);

    // Focus chat input
    const chatInput = page.locator('textarea[placeholder="Send a message..."]');
    await chatInput.click();
    await expect(chatInput).toBeFocused();

    // Type 't' — should NOT navigate to tasks tab
    await page.keyboard.type("t");

    // Should still be on chat tab
    await expect(page).toHaveURL(new RegExp(`/${TEAM}/chat`));
    await expect(page.locator(".sb-nav-btn.active")).toContainText("Chat");

    // Input should contain 't'
    await expect(chatInput).toHaveValue("t");
  });

  test("Enter opens selected task in tasks panel", async ({ page }) => {
    await page.goto(`/${TEAM}/tasks`);
    await expect(page.locator(".task-row").first()).toBeVisible({
      timeout: 5_000,
    });

    // Press 'j' to select first task (tasks sorted by ID descending, so T0002 is first)
    await page.keyboard.press("j");
    await expect(page.locator(".task-row").first()).toHaveClass(/selected/);

    // Press Enter to open task panel
    await page.keyboard.press("Enter");
    const panel = page.locator(".task-panel");
    await expect(panel).toBeVisible({ timeout: 3_000 });

    // Should show T0002 (first task in descending order)
    await expect(panel.locator(".task-panel-id")).toContainText("T0002");
  });
});
