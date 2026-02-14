import { test, expect } from "@playwright/test";

/**
 * Task side panel tests.
 *
 * Seed data includes:
 *   - T0002: in_progress, has attachment (design-brief.md), description
 *     contains file path "teams/testteam/shared/specs/design-brief.md"
 *     and task reference "T0001"
 *   - design-brief.md contains "# Design Brief" and "Requirement 1"
 */

const TEAM = "testteam";

/** Helper: open T0002 task panel from the tasks tab */
async function openT0002(page) {
    await page.goto("/tasks");
    await expect(page.locator(".task-row").first()).toBeVisible({ timeout: 5_000 });
    await page.locator(".task-row", { hasText: "Implement design system" }).click();
    const panel = page.locator(".task-panel");
    await expect(panel).toBeVisible({ timeout: 3_000 });
    return panel;
}

test.describe("Task side panel", () => {
    test("panel shows all tabs", async ({ page }) => {
        const panel = await openT0002(page);

        // Verify all four tabs are present
        await expect(panel.locator(".task-panel-tab", { hasText: "Overview" })).toBeVisible();
        await expect(panel.locator(".task-panel-tab", { hasText: "Changes" })).toBeVisible();
        await expect(panel.locator(".task-panel-tab", { hasText: "Merge Preview" })).toBeVisible();
        await expect(panel.locator(".task-panel-tab", { hasText: "Activity" })).toBeVisible();
    });

    test("attachment click opens file panel (stacking)", async ({ page }) => {
        const panel = await openT0002(page);

        // Click on the design-brief.md attachment
        const attachment = panel.locator(".task-attachment-name", { hasText: "design-brief.md" });
        await expect(attachment).toBeVisible();
        await attachment.click();

        // File panel should open (diff-panel gains "open" class)
        const filePanel = page.locator(".diff-panel");
        await expect(filePanel).toHaveClass(/open/, { timeout: 5_000 });

        // Should show "Back to T0002..." in the back bar
        const backBar = filePanel.locator(".panel-back-bar");
        await expect(backBar).toBeVisible({ timeout: 5_000 });
        await expect(backBar).toContainText("T0002");
    });

    test("file panel renders content (not stuck on Loading)", async ({ page }) => {
        const panel = await openT0002(page);

        // Click attachment to open file panel
        await panel.locator(".task-attachment-name", { hasText: "design-brief.md" }).click();

        const filePanel = page.locator(".diff-panel");
        await expect(filePanel).toBeVisible({ timeout: 3_000 });

        // File content should render — NOT "Loading file..."
        // The markdown file contains "# Design Brief" which renders as heading text
        await expect(filePanel.locator(".diff-panel-body")).not.toContainText("Loading file...", {
            timeout: 5_000,
        });
        await expect(filePanel.locator(".diff-panel-body")).toContainText("Design Brief", {
            timeout: 5_000,
        });
    });

    test("back navigation pops panel stack", async ({ page }) => {
        const panel = await openT0002(page);

        // Stack a file panel on top
        await panel.locator(".task-attachment-name", { hasText: "design-brief.md" }).click();
        const filePanel = page.locator(".diff-panel");
        await expect(filePanel).toHaveClass(/open/, { timeout: 3_000 });

        // Click the back bar
        await filePanel.locator(".panel-back-bar").click();

        // File panel should lose the "open" class (it stays in DOM but slides away)
        await expect(filePanel).not.toHaveClass(/open/, { timeout: 2_000 });
        // Task panel should still be visible
        await expect(page.locator(".task-panel")).toBeVisible();
        await expect(page.locator(".task-panel-id")).toContainText("T0002");
    });

    test("clicking backdrop closes entire stack", async ({ page }) => {
        const panel = await openT0002(page);

        // Stack a file panel on top
        await panel.locator(".task-attachment-name", { hasText: "design-brief.md" }).click();
        const filePanel = page.locator(".diff-panel");
        // Wait for panel to slide in (webkit can be slower with transitions)
        await expect(filePanel).toBeVisible({ timeout: 5_000 });
        await expect(filePanel).toHaveClass(/open/, { timeout: 5_000 });

        // Click the backdrop (covers the area behind the panel)
        const backdrop = page.locator(".diff-backdrop.open");
        await backdrop.click({ position: { x: 10, y: 10 }, force: true });

        // Both panels should close — diff-panel loses "open" class, task-panel unmounts
        await expect(filePanel).not.toHaveClass(/open/, { timeout: 2_000 });
        await expect(page.locator(".task-panel")).not.toBeVisible({ timeout: 2_000 });
    });

    test("file path link in task description is clickable", async ({ page }) => {
        const panel = await openT0002(page);

        // The description contains "teams/testteam/shared/specs/design-brief.md"
        // which should be linkified with data-file-path attribute
        const fileLink = panel.locator("[data-file-path]", { hasText: "design-brief" });
        await expect(fileLink).toBeVisible();
        await fileLink.click();

        // File panel should open
        const filePanel = page.locator(".diff-panel");
        await expect(filePanel).toBeVisible({ timeout: 3_000 });

        // Should render file content
        await expect(filePanel.locator(".diff-panel-body")).toContainText("Design Brief", {
            timeout: 5_000,
        });
    });
});
