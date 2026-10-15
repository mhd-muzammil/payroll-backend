/**
 * One work order, looked at the way the office looks at it in FieldEZ --
 * Ticket/Summary, "Enter Ticket No", search, open the WO, Task Details -- to
 * learn what FieldEZ gives for its "Problem Resolution" row: a latitude and
 * longitude, an address, or both, and under which names.
 *
 *   node src/probe.js WO-036165368
 *
 * Read-only: it only opens pages, types the WO into the search box and opens
 * the ticket and its Task Details. It presses nothing else, and it never logs
 * out. Its own browser profile (config.profileDir), never the OpenCall
 * fieldez-worker's.
 *
 * What it prints is safe to share: the API paths FieldEZ's pages called, their
 * field names, and the Problem Resolution entry's time, user and location
 * fields. Everything it saw is kept on this computer only, in config.outDir.
 */
import fs from "node:fs";
import path from "node:path";
import { chromium } from "playwright";
import { config } from "./config.js";

const wo = (process.argv[2] || "").trim().toUpperCase();
if (!/^WO-?\d{6,}(-\d+)?$/.test(wo)) {
  console.error("Give a work order, e.g.: node src/probe.js WO-036165368");
  process.exit(2);
}

// Field names worth showing from the Problem Resolution entry: when, who, what
// and where. Nothing else is printed from it.
const SHOWN = /status|name|task|time|date|on$|user|by$|lat|lng|lon|location|address|place|geo|sync|activity/i;

const onLoginPage = (url) => /\/auth\b|login/i.test(url);

async function ensureLoggedIn(page) {
  await page.goto(`${config.fieldezBaseUrl}/frontend/tlm/summary-list`, { waitUntil: "domcontentloaded", timeout: 60_000 });
  await page.waitForTimeout(3000);
  if (!onLoginPage(page.url())) return;

  if (config.fieldezUsername && config.fieldezPassword) {
    // The same form fill the OpenCall fieldez-worker uses.
    await page.getByPlaceholder(/user ?name/i).first().fill(config.fieldezUsername);
    await page.locator("input[type=password]").first().fill(config.fieldezPassword);
    await page.getByRole("button", { name: /log ?in/i }).first().click();
    await page.waitForLoadState("networkidle").catch(() => {});
    await page.waitForTimeout(3000);
  } else {
    console.log("\n>>> Log in to FieldEZ in the Chrome window that opened. Waiting up to", Math.round(config.loginWaitMs / 60000), "minutes...\n");
    const until = Date.now() + config.loginWaitMs;
    while (onLoginPage(page.url()) && Date.now() < until) await page.waitForTimeout(2000);
  }
  if (onLoginPage(page.url())) throw new Error("not logged in to FieldEZ");
  await page.goto(`${config.fieldezBaseUrl}/frontend/tlm/summary-list`, { waitUntil: "domcontentloaded", timeout: 60_000 });
  await page.waitForTimeout(3000);
}

/** Every JSON answer FieldEZ's own pages get, kept for the report. */
function captureJson(page, store) {
  page.on("response", async (res) => {
    const url = res.url();
    if (!/\/v1\.0\//.test(url)) return;
    const type = res.headers()["content-type"] || "";
    if (!type.includes("json")) return;
    try {
      store.push({ method: res.request().method(), url, status: res.status(), json: await res.json() });
    } catch {
      // A body that went away with a navigation.
    }
  });
}

/** The visible element with exactly this text that sits furthest left -- the side menu, not a heading. */
async function clickLeftmost(page, text) {
  const matches = page.getByText(text, { exact: true });
  const count = await matches.count();
  let best = null;
  for (let i = 0; i < count; i += 1) {
    const box = await matches.nth(i).boundingBox();
    if (box && (!best || box.x < best.box.x)) best = { i, box };
  }
  if (!best) throw new Error(`no "${text}" on the page`);
  await matches.nth(best.i).click();
}

function shapeOf(value) {
  if (Array.isArray(value)) return value.length && value[0] && typeof value[0] === "object" ? `[${value.length} x {${Object.keys(value[0]).join(", ")}}]` : `[${value.length}]`;
  if (value && typeof value === "object") return `{${Object.keys(value).join(", ")}}`;
  return typeof value;
}

/** Where "Problem Resolution" appears in a JSON answer: the path, and the object holding it. */
function findResolution(node, at = "$", hits = []) {
  if (Array.isArray(node)) node.forEach((v, i) => findResolution(v, `${at}[${i}]`, hits));
  else if (node && typeof node === "object") {
    const own = Object.entries(node).some(([, v]) => typeof v === "string" && /problem\s*resolution/i.test(v));
    if (own) hits.push({ at, entry: node });
    for (const [k, v] of Object.entries(node)) if (v && typeof v === "object") findResolution(v, `${at}.${k}`, hits);
  }
  return hits;
}

const short = (v) => {
  const text = typeof v === "string" ? v : JSON.stringify(v);
  return text && text.length > 160 ? `${text.slice(0, 160)}…` : text;
};

async function main() {
  fs.mkdirSync(config.outDir, { recursive: true });
  const context = await chromium.launchPersistentContext(config.profileDir, {
    channel: config.browserChannel,
    headless: config.headless,
    // Full screen and in front, so the window to log in to is not missed.
    viewport: null,
    args: ["--start-maximized"],
  });
  const page = context.pages()[0] || (await context.newPage());
  await page.bringToFront();
  const seen = [];
  captureJson(page, seen);
  // A WO link that opens a new tab is watched too.
  context.on("page", (opened) => captureJson(opened, seen));
  try {
    await ensureLoggedIn(page);

    // Ticket/Summary: the WO into "Enter Ticket No", then search.
    const box = page.getByPlaceholder(/Enter Ticket No/i).first();
    await box.waitFor({ timeout: 30_000 });
    await box.fill(wo);
    await box.press("Enter");
    const result = page.getByText(wo, { exact: true });
    try {
      await result.first().waitFor({ timeout: 10_000 });
    } catch {
      // Enter did not search: the search button beside the box.
      await box.locator("xpath=following::button[1]").click();
      await result.first().waitFor({ timeout: 30_000 });
    }

    // Open the WO: Ticket/Details. The search's own answer names the ticket
    // (id, bpId), so its page is opened at the address the WO link leads to;
    // failing that, the link itself is clicked, in this tab or a new one.
    await page.waitForTimeout(2000);
    const row = ticketRow(seen, wo);
    let ticketPage = page;
    if (row) {
      await page.goto(`${config.fieldezBaseUrl}/frontend/tlm/ticket-view/${row.bpId}/${row.id}/summary`, {
        waitUntil: "domcontentloaded",
        timeout: 60_000,
      });
    } else {
      const link = page.locator("a", { hasText: wo }).first();
      const target = (await link.count()) ? link : result.first();
      const opened = context.waitForEvent("page", { timeout: 30_000 }).catch(() => null);
      await target.click();
      const sameTab = page.waitForURL(/ticket-view\/\d+\/\d+/, { timeout: 30_000 }).then(() => page).catch(() => null);
      ticketPage = (await Promise.race([sameTab, opened])) || (await sameTab) || (await opened);
      if (!ticketPage) throw new Error("the WO did not open");
      await ticketPage.waitForURL(/ticket-view\/\d+\/\d+/, { timeout: 30_000 });
    }
    await ticketPage.bringToFront();
    await ticketPage.waitForTimeout(3000);

    // Task Details: the status rows with their location.
    await clickLeftmost(ticketPage, "Task Details");
    await ticketPage.waitForLoadState("networkidle").catch(() => {});
    await ticketPage.waitForTimeout(4000);

    // The Problem Resolution row as the page shows it, and any link in it.
    const rows = await ticketPage.evaluate(() =>
      [...document.querySelectorAll("tr, [role=row], li, .row, div")]
        .filter((el) => /problem\s*resolution/i.test(el.innerText || "") && (el.innerText || "").length < 600)
        .slice(-3)
        .map((el) => ({
          text: (el.innerText || "").replace(/\s+/g, " ").trim(),
          links: [...el.querySelectorAll("a[href]")].map((a) => a.href),
          data: [...el.querySelectorAll("*")]
            .flatMap((n) => [...n.attributes].filter((a) => /lat|lng|lon|geo|coord/i.test(a.name + a.value)).map((a) => `${a.name}=${a.value}`))
            .slice(0, 10),
        })),
    );

    report(rows, ticketPage.url());
  } catch (err) {
    console.error("probe stopped:", err.message);
    report([], page.url());
    process.exitCode = 1;
  } finally {
    await context.close();
  }

  // What was seen, whether or not it got to the end.
  function report(rows, at) {
    const stamp = new Date().toISOString().replace(/[:.]/g, "-");
    const file = path.join(config.outDir, `probe-${wo}-${stamp}.json`);
    fs.writeFileSync(file, JSON.stringify({ wo, url: at, rows, seen }, null, 2));

    console.log("\n================ FIELDEZ PROBE:", wo, "================");
    console.log("Last page:", at.replace(config.fieldezBaseUrl, ""));
    console.log("\nAPI calls FieldEZ's pages made (path, status, shape):");
    for (const r of seen) {
      const u = new URL(r.url);
      console.log(` ${r.method} ${u.pathname} -> ${r.status}  ${shapeOf(r.json?.data ?? r.json)}`);
    }
    console.log("\nWhere 'Problem Resolution' is in those answers:");
    let any = false;
    for (const r of seen) {
      for (const hit of findResolution(r.json)) {
        any = true;
        console.log(` ${new URL(r.url).pathname} at ${hit.at}`);
        console.log(`   fields: ${Object.keys(hit.entry).join(", ")}`);
        for (const [k, v] of Object.entries(hit.entry)) {
          if (SHOWN.test(k) && v !== null && v !== undefined) console.log(`   ${k} = ${short(v)}`);
        }
      }
    }
    if (!any) console.log(" (not in any JSON answer)");
    console.log("\nThe Problem Resolution row on the page:");
    for (const row of rows) {
      console.log(` text : ${short(row.text)}`);
      if (row.links.length) console.log(` links: ${row.links.join(" | ")}`);
      if (row.data.length) console.log(` attrs: ${row.data.join(" | ")}`);
    }
    if (!rows.length) console.log(" (none read)");
    console.log("\nEverything seen is kept on this computer only:", file);
    console.log("================ END ================\n");
  }
}

/** The search's own answer for this WO: {id, bpId}, or null. Exact digits, not FieldEZ's contains-match. */
function ticketRow(seen, wanted) {
  const digits = wanted.replace(/\D/g, "");
  const walk = (node) => {
    if (Array.isArray(node)) {
      for (const v of node) {
        const hit = walk(v);
        if (hit) return hit;
      }
    } else if (node && typeof node === "object") {
      const no = node.ft_ticket_no ?? node.ticketNo ?? node.ticket_no;
      if (typeof no === "string" && no.replace(/\D/g, "") === digits && node.id != null && node.bpId != null) {
        return { id: node.id, bpId: node.bpId };
      }
      for (const v of Object.values(node)) {
        if (v && typeof v === "object") {
          const hit = walk(v);
          if (hit) return hit;
        }
      }
    }
    return null;
  };
  for (const r of [...seen].reverse()) {
    if (!/summary-list/.test(r.url)) continue;
    const hit = walk(r.json);
    if (hit) return hit;
  }
  return null;
}

main().catch((err) => {
  console.error("probe failed:", err.message);
  process.exit(1);
});
