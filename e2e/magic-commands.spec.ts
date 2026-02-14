import { test, expect } from "@playwright/test";

/**
 * Magic command UX tests.
 *
 * Tests for:
 * 1. Command autocomplete dropdown functionality and positioning
 * 2. Shell command cwd visibility and changeability
 *
 * Note: The chat input is a <div contentEditable="plaintext-only" class="chat-input">,
 * NOT a <textarea>. We interact with it via textContent, not value.
 */

const TEAM = "testteam";

// Helper: set the chat input's text content and trigger Preact's onInput handler.
async function fillChatInput(page, text) {
  const chatInput = page.locator(".chat-input");
  await chatInput.evaluate((el, value) => {
    el.textContent = value;
    el.dispatchEvent(new Event("input", { bubbles: true }));
  }, text);
  await page.waitForTimeout(50);
}

test.describe("Magic command autocomplete", () => {
  test.beforeEach(async ({ page }) => {
    await page.goto(`/${TEAM}/chat`);
    // Wait for chat input to load
    await expect(page.locator(".chat-input")).toBeVisible({ timeout: 5_000 });
  });

  test("autocomplete dropdown appears when typing magic command", async ({ page }) => {
    await fillChatInput(page, "/");

    // Autocomplete dropdown should appear
    const dropdown = page.locator(".command-autocomplete");
    await expect(dropdown).toBeVisible({ timeout: 2_000 });

    // Dropdown should contain command items
    const items = dropdown.locator(".command-autocomplete-item");
    await expect(items.first()).toBeVisible();
  });

  test("autocomplete dropdown is positioned near input", async ({ page }) => {
    const inputBox = page.locator(".chat-input-box");
    await fillChatInput(page, "/");

    // Wait for dropdown to appear
    const dropdown = page.locator(".command-autocomplete");
    await expect(dropdown).toBeVisible({ timeout: 2_000 });

    // Get bounding boxes
    const inputBoxBounds = await inputBox.boundingBox();
    const dropdownBounds = await dropdown.boundingBox();

    expect(inputBoxBounds).not.toBeNull();
    expect(dropdownBounds).not.toBeNull();

    if (inputBoxBounds && dropdownBounds) {
      // Dropdown should be within reasonable distance of the input box
      // (above or slightly overlapping is fine — exact gap depends on layout)
      const dropdownBottom = dropdownBounds.y + dropdownBounds.height;
      const inputBottom = inputBoxBounds.y + inputBoxBounds.height;

      // Dropdown bottom should be somewhere near the input (within 150px)
      expect(Math.abs(dropdownBottom - inputBoxBounds.y)).toBeLessThanOrEqual(150);

      // Both should be visible within the viewport
      expect(dropdownBounds.y).toBeGreaterThanOrEqual(0);
      expect(inputBottom).toBeLessThanOrEqual(1200);
    }
  });

  test("autocomplete shows available commands when typing /", async ({ page }) => {
    await fillChatInput(page, "/");

    const dropdown = page.locator(".command-autocomplete");
    await expect(dropdown).toBeVisible();

    // Should show both /shell and /status commands
    await expect(dropdown.locator(".command-autocomplete-item")).toHaveCount(2);
    await expect(dropdown.locator(".command-name", { hasText: "/shell" })).toBeVisible();
    await expect(dropdown.locator(".command-name", { hasText: "/status" })).toBeVisible();
  });

  test("autocomplete filters commands as user types", async ({ page }) => {
    await fillChatInput(page, "/sh");

    const dropdown = page.locator(".command-autocomplete");
    await expect(dropdown).toBeVisible();

    // Should show only /shell
    await expect(dropdown.locator(".command-autocomplete-item")).toHaveCount(1);
    await expect(dropdown.locator(".command-name", { hasText: "/shell" })).toBeVisible();
  });

  test("autocomplete closes when Escape is pressed", async ({ page }) => {
    const chatInput = page.locator(".chat-input");
    await fillChatInput(page, "/");

    const dropdown = page.locator(".command-autocomplete");
    await expect(dropdown).toBeVisible();

    // Focus the chat input then press Escape
    await chatInput.focus();
    await page.keyboard.press("Escape");

    // Dropdown should be hidden (commandMode set to false unmounts it)
    await expect(dropdown).not.toBeVisible({ timeout: 2000 });
  });

  test("selecting command from autocomplete completes the input", async ({ page }) => {
    const chatInput = page.locator(".chat-input");
    await fillChatInput(page, "/");

    const dropdown = page.locator(".command-autocomplete");
    await expect(dropdown).toBeVisible({ timeout: 2_000 });

    // Click on /shell command
    const shellItem = dropdown.locator(".command-autocomplete-item", {
      has: page.locator(".command-name", { hasText: "/shell" }),
    });
    await shellItem.click();

    // Input should now have "/shell " (with trailing space)
    await expect(chatInput).toHaveText("/shell ");
  });
});

test.describe("Shell command cwd visibility", () => {
  test.beforeEach(async ({ page }) => {
    await page.goto(`/${TEAM}/chat`);
    await expect(page.locator(".chat-input")).toBeVisible({ timeout: 5_000 });
  });

  test("cwd badge is visible when typing shell command", async ({ page }) => {
    await fillChatInput(page, "/shell ls");

    // CWD badge should be visible
    const cwdBadge = page.locator(".chat-cwd-badge");
    await expect(cwdBadge).toBeVisible({ timeout: 2_000 });

    // Should show cwd label and input
    await expect(cwdBadge.locator(".chat-cwd-label")).toContainText("cwd:");
    await expect(cwdBadge.locator(".chat-cwd-input")).toBeVisible();
  });

  test("cwd badge is not visible for non-shell commands", async ({ page }) => {
    await fillChatInput(page, "/status");

    // CWD badge should NOT be visible
    const cwdBadge = page.locator(".chat-cwd-badge");
    await expect(cwdBadge).not.toBeVisible();
  });

  test("cwd input is editable and persists value", async ({ page }) => {
    await fillChatInput(page, "/shell pwd");

    const cwdInput = page.locator(".chat-cwd-input");
    await expect(cwdInput).toBeVisible();

    // Default value should be ~ or current directory
    const initialValue = await cwdInput.inputValue();
    expect(initialValue).toBeTruthy();

    // Change cwd value
    await cwdInput.fill("/tmp");
    await expect(cwdInput).toHaveValue("/tmp");

    // Type more in the command — cwd should persist
    await fillChatInput(page, "/shell pwd && ls");
    await expect(cwdInput).toHaveValue("/tmp");
  });

  test("cwd badge displays prominently in command mode", async ({ page }) => {
    await fillChatInput(page, "/shell echo test");

    const cwdBadge = page.locator(".chat-cwd-badge");
    await expect(cwdBadge).toBeVisible();

    const cwdBox = await cwdBadge.boundingBox();
    expect(cwdBox).not.toBeNull();

    if (cwdBox) {
      expect(cwdBox.height).toBeGreaterThan(15);
      expect(cwdBox.width).toBeGreaterThan(50);
    }
  });
});
