/**
 * Playwright global setup — runs once before all tests.
 *
 * The temp dir and port are already created/set by playwright.config.ts.
 * This script only seeds test data into that directory.
 */

import { execSync } from "child_process";
import * as path from "path";

async function globalSetup() {
  const tmpDir = process.env.DELEGATE_E2E_HOME;
  if (!tmpDir) {
    throw new Error("DELEGATE_E2E_HOME not set — config should have set it");
  }

  const seedScript = path.resolve(__dirname, "seed.py");
  const projectRoot = path.resolve(__dirname, "..");
  const venvPython = path.join(projectRoot, ".venv", "bin", "python");

  try {
    execSync(`${venvPython} ${seedScript} ${tmpDir}`, {
      cwd: projectRoot,
      stdio: "pipe",
      env: { ...process.env, PYTHONPATH: projectRoot },
    });
  } catch (err: any) {
    console.error("Seed script failed:");
    console.error(err.stderr?.toString() || err.message);
    throw err;
  }

  console.log(
    `\n  E2E setup: home=${tmpDir}, port=${process.env.DELEGATE_E2E_PORT}\n`
  );
}

export default globalSetup;
