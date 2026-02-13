import { defineConfig, devices } from "@playwright/test";
import * as fs from "fs";
import * as os from "os";
import * as path from "path";

/**
 * Playwright config for Delegate E2E tests.
 *
 * We create the temp dir and pick a port HERE (at config-eval time)
 * because globalSetup runs AFTER the config is evaluated, so env
 * vars set there can't influence webServer/baseURL.
 *
 * globalSetup only seeds data into the already-created temp dir.
 */

// Create (or reuse) a temp directory for this test run
const tmpDir =
  process.env.DELEGATE_E2E_HOME ||
  fs.mkdtempSync(path.join(os.tmpdir(), "delegate-e2e-"));

// Use a fixed high port unlikely to collide (avoids async port-finding)
const port = Number(process.env.DELEGATE_E2E_PORT) || 13548;
const baseURL = `http://127.0.0.1:${port}`;

// Make these available to globalSetup, globalTeardown, and tests
process.env.DELEGATE_E2E_HOME = tmpDir;
process.env.DELEGATE_E2E_PORT = String(port);
process.env.DELEGATE_E2E_BASE_URL = baseURL;

export default defineConfig({
  testDir: "./e2e",
  globalSetup: "./e2e/global-setup.ts",
  globalTeardown: "./e2e/global-teardown.ts",

  fullyParallel: true,
  forbidOnly: !!process.env.CI,
  retries: process.env.CI ? 2 : 0,
  workers: process.env.CI ? 1 : undefined,
  reporter: "html",

  use: {
    baseURL,
    trace: "on-first-retry",
  },

  projects: [
    {
      name: "chromium",
      use: { ...devices["Desktop Chrome"] },
    },
  ],

  webServer: {
    command: `.venv/bin/python -m uvicorn delegate.web:create_app --factory --host 127.0.0.1 --port ${port}`,
    port,
    reuseExistingServer: !process.env.CI,
    env: {
      ...process.env,
      DELEGATE_HOME: tmpDir,
    },
    timeout: 15_000,
  },
});
