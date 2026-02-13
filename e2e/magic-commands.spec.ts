import { test, expect } from "@playwright/test";

/**
 * Magic command UX tests.
 *
 * Tests for:
 * 1. Command autocomplete dropdown functionality and positioning
 * 2. Shell command cwd visibility and changeability
 */

const TEAM = "testteam";

test.describe("Magic command autocomplete", () => {
  test.beforeEach(async ({ page }) => {
    await page.goto(`/${TEAM}/chat`);
    // Wait for chat to load
    await expect(page.locator(".chat-input-box textarea")).toBeVisible({
      timeout: 5_000,
    });
  });

  test("autocomplete dropdown appears when typing magic command", async ({ page }) => {
    const textarea = page.locator(".chat-input-box textarea");

    // Type a slash to trigger command mode
    await textarea.fill("/");

    // Autocomplete dropdown should appear
    const dropdown = page.locator(".command-autocomplete");
    await expect(dropdown).toBeVisible({ timeout: 2_000 });

    // Dropdown should contain command items
    const items = dropdown.locator(".command-autocomplete-item");
    await expect(items.first()).toBeVisible();
  });

  test("autocomplete dropdown is positioned directly above input", async ({ page }) => {
    const textarea = page.locator(".chat-input-box textarea");
    const inputBox = page.locator(".chat-input-box");

    // Type a slash to trigger command mode
    await textarea.fill("/");

    // Wait for dropdown to appear
    const dropdown = page.locator(".command-autocomplete");
    await expect(dropdown).toBeVisible({ timeout: 2_000 });

    // Get bounding boxes
    const inputBoxBounds = await inputBox.boundingBox();
    const dropdownBounds = await dropdown.boundingBox();

    expect(inputBoxBounds).not.toBeNull();
    expect(dropdownBounds).not.toBeNull();

    if (inputBoxBounds && dropdownBounds) {
      // Dropdown should be positioned above the input box
      // The bottom of the dropdown should be within ~10px of the top of the input box
      const dropdownBottom = dropdownBounds.y + dropdownBounds.height;
      const inputTop = inputBoxBounds.y;
      const gap = inputTop - dropdownBottom;

      // Gap should be positive (dropdown is above input) and small (dropdown has 4px margin-bottom + input-box has 8px margin-top = 12px total)
      expect(gap).toBeGreaterThanOrEqual(0);
      expect(gap).toBeLessThanOrEqual(15);

      // Dropdown should be horizontally aligned with input box (allow 1-2px rounding difference)
      expect(Math.abs(dropdownBounds.x - inputBoxBounds.x)).toBeLessThanOrEqual(2);
      expect(Math.abs(dropdownBounds.width - inputBoxBounds.width)).toBeLessThanOrEqual(2);
    }
  });

  test("autocomplete shows available commands when typing /", async ({ page }) => {
    const textarea = page.locator(".chat-input-box textarea");

    await textarea.fill("/");

    const dropdown = page.locator(".command-autocomplete");
    await expect(dropdown).toBeVisible();

    // Should show both /shell and /status commands
    await expect(dropdown.locator(".command-autocomplete-item")).toHaveCount(2);
    await expect(dropdown.locator(".command-name", { hasText: "/shell" })).toBeVisible();
    await expect(dropdown.locator(".command-name", { hasText: "/status" })).toBeVisible();
  });

  test("autocomplete filters commands as user types", async ({ page }) => {
    const textarea = page.locator(".chat-input-box textarea");

    // Type /sh to filter to shell command only
    await textarea.fill("/sh");

    const dropdown = page.locator(".command-autocomplete");
    await expect(dropdown).toBeVisible();

    // Should show only /shell
    await expect(dropdown.locator(".command-autocomplete-item")).toHaveCount(1);
    await expect(dropdown.locator(".command-name", { hasText: "/shell" })).toBeVisible();
  });

  test("autocomplete closes when Escape is pressed", async ({ page }) => {
    const textarea = page.locator(".chat-input-box textarea");

    await textarea.fill("/");

    const dropdown = page.locator(".command-autocomplete");
    await expect(dropdown).toBeVisible();

    // Press Escape to dismiss the autocomplete
    await textarea.press("Escape");

    // Dropdown should be hidden (commandMode set to false unmounts it)
    await expect(dropdown).not.toBeVisible({ timeout: 2000 });
  });

  test("selecting command from autocomplete completes the input", async ({ page }) => {
    const textarea = page.locator(".chat-input-box textarea");

    await textarea.fill("/");

    const dropdown = page.locator(".command-autocomplete");
    await expect(dropdown).toBeVisible();

    // Click on /shell command
    const shellItem = dropdown.locator(".command-autocomplete-item", { has: page.locator(".command-name", { hasText: "/shell" }) });
    await shellItem.click();

    // Input should now have "/shell " (with trailing space)
    await expect(textarea).toHaveValue("/shell ");
  });
});

test.describe("Shell command cwd visibility", () => {
  test.beforeEach(async ({ page }) => {
    await page.goto(`/${TEAM}/chat`);
    await expect(page.locator(".chat-input-box textarea")).toBeVisible({
      timeout: 5_000,
    });
  });

  test("cwd badge is visible when typing shell command", async ({ page }) => {
    const textarea = page.locator(".chat-input-box textarea");

    // Type a shell command
    await textarea.fill("/shell ls");

    // CWD badge should be visible
    const cwdBadge = page.locator(".chat-cwd-badge");
    await expect(cwdBadge).toBeVisible({ timeout: 2_000 });

    // Should show cwd label and input
    await expect(cwdBadge.locator(".chat-cwd-label")).toContainText("cwd:");
    await expect(cwdBadge.locator(".chat-cwd-input")).toBeVisible();
  });

  test("cwd badge is not visible for non-shell commands", async ({ page }) => {
    const textarea = page.locator(".chat-input-box textarea");

    // Type a status command
    await textarea.fill("/status");

    // CWD badge should NOT be visible
    const cwdBadge = page.locator(".chat-cwd-badge");
    await expect(cwdBadge).not.toBeVisible();
  });

  test("cwd input is editable and persists value", async ({ page }) => {
    const textarea = page.locator(".chat-input-box textarea");

    // Type a shell command
    await textarea.fill("/shell pwd");

    const cwdInput = page.locator(".chat-cwd-input");
    await expect(cwdInput).toBeVisible();

    // Default value should be ~ or current directory
    const initialValue = await cwdInput.inputValue();
    expect(initialValue).toBeTruthy();

    // Change cwd value (fill() clears first automatically)
    await cwdInput.fill("/tmp");

    // Value should persist
    await expect(cwdInput).toHaveValue("/tmp");

    // Type more in the command
    await textarea.fill("/shell pwd && ls");

    // CWD should still show /tmp
    await expect(cwdInput).toHaveValue("/tmp");
  });

  test("cwd badge displays prominently in command mode", async ({ page }) => {
    const textarea = page.locator(".chat-input-box textarea");

    await textarea.fill("/shell echo test");

    const cwdBadge = page.locator(".chat-cwd-badge");
    await expect(cwdBadge).toBeVisible();

    // Verify it's properly styled
    const cwdBox = await cwdBadge.boundingBox();
    expect(cwdBox).not.toBeNull();

    if (cwdBox) {
      // Badge should be visible and have reasonable dimensions
      expect(cwdBox.height).toBeGreaterThan(15); // Has padding
      expect(cwdBox.width).toBeGreaterThan(50); // Has label + input
    }
  });
});
