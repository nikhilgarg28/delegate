import { test, expect } from "@playwright/test";

/**
 * Chat input UX tests.
 *
 * Tests for:
 * 1. Reply cursor focus - after clicking reply in selection tooltip, cursor should be in chatbox
 * 2. Shift+Enter newline - Shift+Enter should insert a newline in the chatbox
 * 3. Inline markdown rendering - code blocks, inline code, and lists should render with formatting
 */

const TEAM = "testteam";

// Helper function to set textarea value and trigger Preact's onInput event
async function fillTextarea(page, text) {
  const textarea = page.locator(".chat-input-box textarea");

  // First, clear the textarea and click to focus it
  await textarea.click();
  await textarea.fill("");

  // Type the text character by character to properly trigger all input events
  // This is slower but ensures Preact's onInput is called for every change
  await textarea.pressSequentially(text, { delay: 0 });

  // Give Preact time to update state and re-render
  await page.waitForTimeout(200);
}

test.describe("Chat input UX", () => {
  test.beforeEach(async ({ page }) => {
    await page.goto(`/${TEAM}/chat`);
    // Wait for the chatbox to be visible instead of networkidle
    // (networkidle doesn't work with SSE/polling)
    await page.locator(".chat-input-box textarea").waitFor({ state: "visible" });
  });

  test("Shift+Enter inserts newline in chatbox", async ({ page }) => {
    const textarea = page.locator(".chat-input-box textarea");
    await textarea.click();
    await fillTextarea(page, "Line 1");
    await page.keyboard.press("Shift+Enter");
    await textarea.pressSequentially("Line 2");

    const value = await textarea.inputValue();
    expect(value).toContain("\n");
    expect(value).toBe("Line 1\nLine 2");
  });

  test("Enter (without Shift) sends message and clears textarea", async ({ page }) => {
    const textarea = page.locator(".chat-input-box textarea");
    await fillTextarea(page, "Test message");
    await textarea.focus(); // Ensure textarea has focus for keyboard event

    await page.keyboard.press("Enter");
    await page.waitForTimeout(300);

    const value = await textarea.inputValue();
    expect(value).toBe("");
  });

  test("Inline rendering shows for code blocks", async ({ page }) => {
    await fillTextarea(page, "Here is some code:\n```js\nconst x = 1;\n```");

    const overlay = page.locator(".chat-input-overlay");
    await expect(overlay).toBeVisible();

    const codeBlock = overlay.locator("pre code");
    await expect(codeBlock).toBeVisible();
    await expect(codeBlock).toContainText("const x = 1;");
  });

  test("Inline rendering shows for inline code", async ({ page }) => {
    await fillTextarea(page, "Use `console.log()` to print");

    const overlay = page.locator(".chat-input-overlay");
    await expect(overlay).toBeVisible();

    const inlineCode = overlay.locator("code");
    await expect(inlineCode).toBeVisible();
    await expect(inlineCode).toContainText("console.log()");
  });

  test("Inline rendering shows for bullet lists", async ({ page }) => {
    await fillTextarea(page, "- Item 1\n- Item 2\n- Item 3");

    const overlay = page.locator(".chat-input-overlay");
    await expect(overlay).toBeVisible();

    const listItems = overlay.locator("ul li");
    await expect(listItems).toHaveCount(3);
    await expect(listItems.nth(0)).toContainText("Item 1");
    await expect(listItems.nth(1)).toContainText("Item 2");
    await expect(listItems.nth(2)).toContainText("Item 3");
  });

  test("Inline rendering shows for numbered lists", async ({ page }) => {
    await fillTextarea(page, "1. First\n2. Second\n3. Third");

    const overlay = page.locator(".chat-input-overlay");
    await expect(overlay).toBeVisible();

    const listItems = overlay.locator("ol li");
    await expect(listItems).toHaveCount(3);
    await expect(listItems.nth(0)).toContainText("First");
    await expect(listItems.nth(1)).toContainText("Second");
    await expect(listItems.nth(2)).toContainText("Third");
  });

  test("Inline overlay hidden for plain text", async ({ page }) => {
    await fillTextarea(page, "Just plain text without any markdown");

    const overlay = page.locator(".chat-input-overlay");
    await expect(overlay).not.toBeVisible();
  });

  test("Inline overlay hidden for commands (starting with /)", async ({ page }) => {
    await fillTextarea(page, "/help");

    const overlay = page.locator(".chat-input-overlay");
    await expect(overlay).not.toBeVisible();
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

    // Check if textarea is focused
    const textarea = page.locator(".chat-input-box textarea");
    const isFocused = await page.evaluate(() => {
      const el = document.querySelector(".chat-input-box textarea");
      return document.activeElement === el;
    });
    expect(isFocused).toBe(true);

    // Check if cursor is at the end
    const cursorPos = await textarea.evaluate((el: HTMLTextAreaElement) => el.selectionStart);
    const textLength = (await textarea.inputValue()).length;
    expect(cursorPos).toBe(textLength);
  });

  test("Code blocks with uppercase language identifiers are rendered", async ({ page }) => {
    await fillTextarea(page, "```JavaScript\nconst x = 1;\n```");

    const overlay = page.locator(".chat-input-overlay");
    await expect(overlay).toBeVisible();

    const codeBlock = overlay.locator("pre code");
    await expect(codeBlock).toBeVisible();
    await expect(codeBlock).toHaveClass(/language-JavaScript/);
  });

  test("List items with inline code are rendered correctly", async ({ page }) => {
    await fillTextarea(page, "- Use `console.log()` for debugging\n- Try `npm test` to run tests");

    const overlay = page.locator(".chat-input-overlay");
    await expect(overlay).toBeVisible();

    const listItems = overlay.locator("ul li");
    await expect(listItems).toHaveCount(2);

    // Check that inline code is rendered within list items
    const firstItemCode = listItems.nth(0).locator("code");
    await expect(firstItemCode).toBeVisible();
    await expect(firstItemCode).toContainText("console.log()");

    const secondItemCode = listItems.nth(1).locator("code");
    await expect(secondItemCode).toBeVisible();
    await expect(secondItemCode).toContainText("npm test");
  });

  test("XSS protection - HTML in list items is escaped", async ({ page }) => {
    await fillTextarea(page, "- <script>alert('xss')</script>\n- <img src=x onerror=alert('xss')>");

    const overlay = page.locator(".chat-input-overlay");
    await expect(overlay).toBeVisible();

    // Check that the script tag is escaped (shown as text, not executed)
    const firstItem = overlay.locator("ul li").nth(0);
    const firstItemText = await firstItem.textContent();
    expect(firstItemText).toContain("<script>");
    expect(firstItemText).toContain("</script>");

    // Verify no actual script element exists
    const scriptTags = await page.locator("script").count();
    const overlayScriptTags = await overlay.locator("script").count();
    expect(overlayScriptTags).toBe(0);
  });
});
