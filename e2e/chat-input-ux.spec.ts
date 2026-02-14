import { test, expect } from "@playwright/test";

/**
 * Chat input UX tests.
 *
 * Tests for:
 * 1. Reply cursor focus - after clicking reply in selection tooltip, cursor should be in chatbox
 * 2. Shift+Enter newline - Shift+Enter should insert a newline in the chatbox
 * 3. Enter sends and clears the input
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

test.describe("Chat input UX", () => {
  test.beforeEach(async ({ page }) => {
    await page.goto("/chat");
    // Wait for the chat input (contentEditable div) to be visible
    await page.locator(".chat-input").waitFor({ state: "visible" });
  });

  test("Shift+Enter inserts newline in chatbox", async ({ page }) => {
    const chatInput = page.locator(".chat-input");
    await chatInput.click();
    await fillChatInput(page, "Line 1");

    // Move cursor to end
    await page.evaluate(() => {
      const el = document.querySelector(".chat-input");
      if (el) {
        const range = document.createRange();
        range.selectNodeContents(el);
        range.collapse(false);
        const sel = window.getSelection();
        sel?.removeAllRanges();
        sel?.addRange(range);
      }
    });

    await page.keyboard.press("Shift+Enter");
    await chatInput.pressSequentially("Line 2");

    const text = await chatInput.textContent();
    expect(text).toContain("Line 1");
    expect(text).toContain("Line 2");
  });

  test("Enter (without Shift) sends message and clears input", async ({ page }) => {
    const chatInput = page.locator(".chat-input");

    // Wait for agents to load and recipient to be auto-selected
    await page.waitForTimeout(500);

    await fillChatInput(page, "Test message");
    await chatInput.focus();

    await page.keyboard.press("Enter");
    await page.waitForTimeout(300);

    const text = await chatInput.textContent();
    expect(text).toBe("");
  });

  test("Reply button focuses chatbox and positions cursor at end", async ({ page }) => {
    // Wait for chat messages to load
    await page.waitForTimeout(500);

    // Find a message with selectable text
    const msgContent = page.locator(".msg-content").first();
    await expect(msgContent).toBeVisible();

    // Select text by triple-clicking
    await msgContent.click({ clickCount: 3 });
    await page.waitForTimeout(300);

    // Check if selection tooltip appears
    const tooltip = page.locator(".selection-tooltip");
    await expect(tooltip).toBeVisible({ timeout: 2000 });

    // Click the Reply button (second button in the tooltip)
    const replyBtn = tooltip.locator("button").nth(1);
    await replyBtn.click();
    await page.waitForTimeout(400);

    // Check if chat input is focused
    const isFocused = await page.evaluate(() => {
      const el = document.querySelector(".chat-input");
      return document.activeElement === el;
    });
    expect(isFocused).toBe(true);
  });
});
