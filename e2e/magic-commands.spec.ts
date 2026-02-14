import { test, expect } from "@playwright/test";

/**
 * Magic command UX tests.
 *
 * Tests for:
 * 1. Command autocomplete dropdown functionality and positioning
 * 2. Autocomplete dismissal once a command is recognized
 * 3. Shell command cwd visibility and changeability
 * 4. Command hint display for no-arg commands
 *
 * Note: The chat input is a <div contentEditable="plaintext-only" class="chat-input">,
 * NOT a <textarea>. We interact with it via textContent, not value.
 */

const TEAM = "testteam";

// Helper: set the chat input's text content and trigger Preact's onInput handler.
// Uses a double-rAF wait to ensure Preact has processed the state update.
async function fillChatInput(page, text) {
  const chatInput = page.locator(".chat-input");
  await chatInput.evaluate((el, value) => {
    el.textContent = value;
    el.dispatchEvent(new Event("input", { bubbles: true }));
  }, text);
  // Wait for Preact to process the state update and re-render
  await page.waitForFunction(() => new Promise(r => requestAnimationFrame(() => requestAnimationFrame(r))));
}

test.describe("Magic command autocomplete", () => {
  test.beforeEach(async ({ page }) => {
    await page.goto("/chat");
    await expect(page.locator(".chat-input")).toBeVisible({ timeout: 5_000 });
  });

  test("autocomplete dropdown appears when typing /", async ({ page }) => {
    await fillChatInput(page, "/");

    const dropdown = page.locator(".command-autocomplete");
    await expect(dropdown).toBeVisible({ timeout: 3_000 });

    // Should show all commands (shell, status, diff, cost)
    const items = dropdown.locator(".command-autocomplete-item");
    await expect(items).toHaveCount(4);
    await expect(dropdown.locator(".command-name", { hasText: "/shell" })).toBeVisible();
    await expect(dropdown.locator(".command-name", { hasText: "/status" })).toBeVisible();
    await expect(dropdown.locator(".command-name", { hasText: "/diff" })).toBeVisible();
    await expect(dropdown.locator(".command-name", { hasText: "/cost" })).toBeVisible();
  });

  test("autocomplete filters commands as user types", async ({ page }) => {
    await fillChatInput(page, "/sh");

    const dropdown = page.locator(".command-autocomplete");
    await expect(dropdown).toBeVisible();

    await expect(dropdown.locator(".command-autocomplete-item")).toHaveCount(1);
    await expect(dropdown.locator(".command-name", { hasText: "/shell" })).toBeVisible();
  });

  test("autocomplete disappears once command is recognized and space is typed", async ({ page }) => {
    // Type "/shell " — the command is recognized, space starts argument mode
    await fillChatInput(page, "/shell ");

    const dropdown = page.locator(".command-autocomplete");
    await expect(dropdown).not.toBeVisible({ timeout: 2_000 });

    // But command-mode class should still be on the input box
    await expect(page.locator(".chat-input-box")).toHaveClass(/command-mode/);
  });

  test("autocomplete dropdown is anchored above the input box", async ({ page }) => {
    const inputBox = page.locator(".chat-input-box");
    await fillChatInput(page, "/");

    const dropdown = page.locator(".command-autocomplete");
    await expect(dropdown).toBeVisible({ timeout: 2_000 });

    const inputBoxBounds = await inputBox.boundingBox();
    const dropdownBounds = await dropdown.boundingBox();

    expect(inputBoxBounds).not.toBeNull();
    expect(dropdownBounds).not.toBeNull();

    if (inputBoxBounds && dropdownBounds) {
      // Dropdown bottom should be at or near the input box top
      const dropdownBottom = dropdownBounds.y + dropdownBounds.height;
      const gap = inputBoxBounds.y - dropdownBottom;
      // Allow some margin (margin-bottom: 4px in CSS + minor layout variance)
      expect(gap).toBeGreaterThanOrEqual(-5);
      expect(gap).toBeLessThanOrEqual(20);
    }
  });

  test("Escape clears input and exits command mode", async ({ page }) => {
    const chatInput = page.locator(".chat-input");
    await fillChatInput(page, "/sh");

    const dropdown = page.locator(".command-autocomplete");
    await expect(dropdown).toBeVisible();

    await chatInput.focus();
    await page.keyboard.press("Escape");

    // Dropdown gone, input cleared, command-mode class removed
    await expect(dropdown).not.toBeVisible({ timeout: 2_000 });
    await expect(page.locator(".chat-input-box")).not.toHaveClass(/command-mode/);
    const content = await chatInput.evaluate((el) => el.textContent);
    expect(content).toBe("");
  });

  test("selecting command from autocomplete fills input and hides dropdown", async ({ page }) => {
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

    // Dropdown should be gone (we're now in argument mode)
    await expect(dropdown).not.toBeVisible({ timeout: 2_000 });
  });

  test("Tab key selects the highlighted command", async ({ page }) => {
    const chatInput = page.locator(".chat-input");
    await fillChatInput(page, "/sh");
    await chatInput.focus();

    const dropdown = page.locator(".command-autocomplete");
    await expect(dropdown).toBeVisible();

    // Tab should select /shell (the only match)
    await page.keyboard.press("Tab");

    await expect(chatInput).toHaveText("/shell ");
    await expect(dropdown).not.toBeVisible({ timeout: 2_000 });
  });

  test("Enter key selects from autocomplete instead of sending", async ({ page }) => {
    const chatInput = page.locator(".chat-input");
    await fillChatInput(page, "/sh");
    await chatInput.focus();

    const dropdown = page.locator(".command-autocomplete");
    await expect(dropdown).toBeVisible();

    // Enter should select /shell (not send "/sh" as a message)
    await page.keyboard.press("Enter");

    await expect(chatInput).toHaveText("/shell ");
    await expect(dropdown).not.toBeVisible({ timeout: 2_000 });
  });
});

test.describe("Command hints", () => {
  test.beforeEach(async ({ page }) => {
    await page.goto("/chat");
    await expect(page.locator(".chat-input")).toBeVisible({ timeout: 5_000 });
  });

  test("shows hint for no-arg commands like /status", async ({ page }) => {
    // Type /status first (autocomplete visible), then add space to enter argument mode
    await fillChatInput(page, "/status");
    await expect(page.locator(".command-autocomplete")).toBeVisible({ timeout: 2_000 });

    // Now add the space — transitions to argument mode
    await fillChatInput(page, "/status ");

    // Autocomplete should not be visible (command is recognized)
    await expect(page.locator(".command-autocomplete")).not.toBeVisible({ timeout: 2_000 });

    // Hint should be visible
    const hint = page.locator(".command-hint");
    await expect(hint).toBeVisible({ timeout: 3_000 });
    await expect(hint).toContainText("press Enter to run");
  });

  test("no hint shown when still typing command name", async ({ page }) => {
    await fillChatInput(page, "/sta");

    // Should show autocomplete, not hint
    await expect(page.locator(".command-autocomplete")).toBeVisible();
    await expect(page.locator(".command-hint")).not.toBeVisible();
  });
});

test.describe("Shell command cwd visibility", () => {
  test.beforeEach(async ({ page }) => {
    await page.goto("/chat");
    await expect(page.locator(".chat-input")).toBeVisible({ timeout: 5_000 });
  });

  test("cwd badge is visible when typing shell command args", async ({ page }) => {
    await fillChatInput(page, "/shell ls");

    const cwdBadge = page.locator(".chat-cwd-badge");
    await expect(cwdBadge).toBeVisible({ timeout: 2_000 });
    await expect(cwdBadge.locator(".chat-cwd-label")).toContainText("cwd:");
    await expect(cwdBadge.locator(".chat-cwd-input")).toBeVisible();
  });

  test("cwd badge is not visible for non-shell commands", async ({ page }) => {
    await fillChatInput(page, "/status ");

    const cwdBadge = page.locator(".chat-cwd-badge");
    await expect(cwdBadge).not.toBeVisible();
  });

  test("cwd input is editable and persists across command edits", async ({ page }) => {
    const chatInput = page.locator(".chat-input");

    // Use real keyboard input (not fillChatInput) to avoid Preact signal sync issues
    await chatInput.click();
    await page.keyboard.type("/shell pwd", { delay: 10 });

    const cwdInput = page.locator(".chat-cwd-input");
    await expect(cwdInput).toBeVisible({ timeout: 3_000 });

    const initialValue = await cwdInput.inputValue();
    expect(initialValue).toBeTruthy();

    // Change CWD
    await cwdInput.fill("/tmp");
    await expect(cwdInput).toHaveValue("/tmp");

    // Go back to chat input and modify the command
    await chatInput.click();
    await page.keyboard.press("End");
    await page.keyboard.type(" && ls", { delay: 10 });

    // CWD should persist
    await expect(cwdInput).toHaveValue("/tmp");
  });
});
