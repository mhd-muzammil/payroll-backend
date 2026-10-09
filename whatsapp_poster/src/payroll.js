/**
 * Today's attendance image for one branch, made by the payroll site itself.
 *
 * Not redrawn here. The Attendance page already has a "Today (Image)" button
 * that draws exactly this picture for whichever region is selected, and the
 * office knows what it looks like. Pressing that button is the only way the
 * image in the group can never drift from the one on the screen.
 */
import fs from "node:fs";
import path from "node:path";

import { config } from "./config.js";
import { log } from "./log.js";

const NAV_TIMEOUT = 60_000;

async function ensureLoggedIn(page) {
  await page.goto(`${config.payrollUrl}/attendance`, { waitUntil: "networkidle", timeout: NAV_TIMEOUT });
  if (!page.url().includes("/login")) return;

  log("payroll: signing in as", config.payrollUsername);
  await page.fill('input[type="text"], input[name="username"]', config.payrollUsername);
  await page.fill('input[type="password"]', config.payrollPassword);
  await page.click('button[type="submit"]');
  await page.waitForURL((u) => !u.pathname.includes("/login"), { timeout: NAV_TIMEOUT });
  await page.goto(`${config.payrollUrl}/attendance`, { waitUntil: "networkidle", timeout: NAV_TIMEOUT });
  if (page.url().includes("/login")) {
    throw new Error("payroll: sign-in did not stick -- check PAYROLL_USERNAME / PAYROLL_PASSWORD");
  }
}

/**
 * The PNG for one branch, saved under outDir. Returns its path, or null when
 * the branch has nobody marked today -- the page says so in an alert, and
 * there is nothing to send.
 */
export async function captureBranchImage(page, branch, stamp) {
  await ensureLoggedIn(page);

  // The region select is the one that offers the five branches.
  const region = page.locator("select", { has: page.locator('option[value="Kanchipuram"]') }).first();
  await region.waitFor({ state: "visible", timeout: NAV_TIMEOUT });
  await region.selectOption(branch);
  // Let the page settle on the new region before asking it for the picture.
  await page.waitForTimeout(1200);

  // Whichever comes first: the picture, or the page's alert saying there is
  // nothing to draw. Waiting out the download after the alert cost half a
  // minute per empty branch.
  let alertText = null;
  let onDialog;
  const alerted = new Promise((resolve) => {
    onDialog = async (dialog) => {
      alertText = dialog.message();
      await dialog.accept().catch(() => {});
      resolve(null);
    };
  });
  page.on("dialog", onDialog);
  try {
    const download = page.waitForEvent("download", { timeout: 30_000 }).catch(() => null);
    await page.getByRole("button", { name: "Today (Image)" }).click();
    const file = await Promise.race([download, alerted]);

    if (!file) {
      if (alertText && /No attendance marked today/i.test(alertText)) return null;
      throw new Error(`payroll: no image came back for ${branch}${alertText ? ` (${alertText})` : ""}`);
    }

    fs.mkdirSync(config.outDir, { recursive: true });
    const target = path.join(config.outDir, `${stamp}-${branch}.png`);
    await file.saveAs(target);
    return target;
  } finally {
    page.off("dialog", onDialog);
  }
}
