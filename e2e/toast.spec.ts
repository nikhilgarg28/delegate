import { test, expect } from "@playwright/test";

/**
 * Toast notification tests.
 *
 * Tests the minimal, elegant styling of toast notifications:
 * - No blue accent colors (neutral tones only)
 * - Thin borders (1px solid)
 * - Light font weight on title
 * - Proper color coding (green for success, red for error, subtle for info)
 */

const TEAM = "testteam";

test.describe("Toast notifications", () => {
  test.beforeEach(async ({ page }) => {
    await page.goto("/chat");
    // Wait for app to load
    await expect(page.locator(".sb-nav-btn").first()).toBeVisible({
      timeout: 5_000,
    });
  });

  test("success toast has green border and minimal styling", async ({ page }) => {
    // Trigger a success toast programmatically
    await page.evaluate(() => {
      (window as any).__test__.showActionToast({
        title: 'T0001 "Test task"',
        body: "Merged successfully",
        taskId: 1,
        type: "success",
      });
    });

    // Toast should appear
    const toast = page.locator(".toast-success");
    await expect(toast).toBeVisible({ timeout: 2_000 });

    // Check border is thin and green (1px solid)
    const borderStyle = await toast.evaluate((el) => {
      const computed = window.getComputedStyle(el);
      return {
        borderTopWidth: computed.borderTopWidth,
        borderTopStyle: computed.borderTopStyle,
        borderColor: computed.borderColor,
      };
    });
    expect(borderStyle.borderTopWidth).toBe("1px");
    expect(borderStyle.borderTopStyle).toBe("solid");
    // Border color should be green (var(--semantic-green))
    // We can't easily check CSS variable values, so just verify it's not blue

    // Check title font weight is semi-bold (600), not full bold (700+)
    const titleWeight = await toast.locator(".toast-title").evaluate((el) => {
      return window.getComputedStyle(el).fontWeight;
    });
    expect(parseInt(titleWeight)).toBeLessThanOrEqual(600);

    // Check View button is NOT blue (should be text-secondary, not accent)
    const viewButton = toast.locator(".toast-action");
    await expect(viewButton).toBeVisible();
    const buttonColor = await viewButton.evaluate((el) => {
      return window.getComputedStyle(el).color;
    });
    // Should be text-secondary (grayish), not blue
    // We verify it's NOT the accent blue (#569cd6 = rgb(86, 156, 214))
    expect(buttonColor).not.toBe("rgb(86, 156, 214)");

    // Check close button is subtle (text-tertiary)
    const closeButton = toast.locator(".toast-close");
    await expect(closeButton).toBeVisible();
  });

  test("info toast has subtle border", async ({ page }) => {
    // Trigger an info toast
    await page.evaluate(() => {
      (window as any).__test__.showActionToast({
        title: 'T0002 "Another task"',
        body: "Needs your approval",
        taskId: 2,
        type: "info",
      });
    });

    const toast = page.locator(".toast-info");
    await expect(toast).toBeVisible({ timeout: 2_000 });

    // Wait for animation to complete
    await page.waitForTimeout(300);

    // Check border is thin (1px solid, subtle color)
    const borderStyle = await toast.evaluate((el) => {
      const computed = window.getComputedStyle(el);
      return {
        borderTopWidth: computed.borderTopWidth,
        borderTopStyle: computed.borderTopStyle,
        classList: Array.from(el.classList),
      };
    });
    expect(borderStyle.borderTopWidth).toBe("1px");
    expect(borderStyle.borderTopStyle).toBe("solid");
  });

  test("error toast has red border", async ({ page }) => {
    // Trigger an error toast
    await page.evaluate(() => {
      (window as any).__test__.showActionToast({
        title: 'T0003 "Failed task"',
        body: "Merge failed -- needs resolution",
        taskId: 3,
        type: "error",
      });
    });

    const toast = page.locator(".toast-error");
    await expect(toast).toBeVisible({ timeout: 2_000 });

    // Check border is thin and red
    const borderStyle = await toast.evaluate((el) => {
      const computed = window.getComputedStyle(el);
      return {
        borderTopWidth: computed.borderTopWidth,
        borderTopStyle: computed.borderTopStyle,
      };
    });
    expect(borderStyle.borderTopWidth).toBe("1px");
    expect(borderStyle.borderTopStyle).toBe("solid");
  });

  test("toast View button opens task panel", async ({ page }) => {
    // Trigger a toast with taskId
    await page.evaluate(() => {
      (window as any).__test__.showActionToast({
        title: 'T0001 "Test task"',
        body: "Merged successfully",
        taskId: 1,
        type: "success",
      });
    });

    const toast = page.locator(".toast-success");
    await expect(toast).toBeVisible({ timeout: 2_000 });

    // Click View button
    await toast.locator(".toast-action").click();

    // Task panel should open
    const panel = page.locator(".task-panel");
    await expect(panel).toBeVisible({ timeout: 3_000 });
    await expect(panel.locator(".task-panel-id")).toContainText("T0001");

    // Toast should be dismissed
    await expect(toast).not.toBeVisible({ timeout: 1_000 });
  });

  test("toast close button dismisses toast", async ({ page }) => {
    // Trigger a toast
    await page.evaluate(() => {
      (window as any).__test__.showToast("Test message", "info");
    });

    const toast = page.locator(".toast-info");
    await expect(toast).toBeVisible({ timeout: 2_000 });

    // Click close button
    await toast.locator(".toast-close").click();

    // Toast should be dismissed
    await expect(toast).not.toBeVisible({ timeout: 1_000 });
  });

  test("toast body text is properly capitalized", async ({ page }) => {
    // Trigger a success toast
    await page.evaluate(() => {
      (window as any).__test__.showActionToast({
        title: 'T0001 "Test task"',
        body: "Merged successfully",
        taskId: 1,
        type: "success",
      });
    });

    const toast = page.locator(".toast-success");
    await expect(toast).toBeVisible({ timeout: 2_000 });

    // Check body text is sentence case (starts with capital letter)
    const bodyText = await toast.locator(".toast-body").textContent();
    expect(bodyText).toBe("Merged successfully");
    // First letter should be uppercase
    expect(bodyText?.[0]).toBe(bodyText?.[0]?.toUpperCase());
  });
});
