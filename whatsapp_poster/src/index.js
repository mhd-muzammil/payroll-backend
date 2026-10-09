/**
 * At each of RUN_TIMES (9:15, 10:30 and 21:30 IST by default), each branch's
 * attendance for the day into that branch's WhatsApp group -- as a text
 * message, or with POST_AS=image as the Attendance page's picture.
 *
 *   node src/index.js                 run for ever, posting at RUN_TIMES (IST)
 *   node src/index.js --once          link if needed, post once now, exit
 *   node src/index.js --capture-only  only make the messages (no WhatsApp), exit
 *
 * The text is read from the payroll API. The picture is made in a browser tab
 * on the payroll site, by the Attendance page's own "Today (Image)" button.
 * Either goes out through a WhatsApp Web tab that stays open between runs, so
 * the session stays warm.
 */
import fs from "node:fs";
import path from "node:path";

import { chromium } from "playwright";

import { buildMessage, indianDate, todaysRows } from "./attendanceText.js";
import { config, missingForPosting } from "./config.js";
import { log } from "./log.js";
import { captureBranchImage } from "./payroll.js";
import { fetchAttendance } from "./payrollApi.js";
import { recordRun, startStatusServer, status } from "./status.js";
import { ensureLinked, sendImage, sendText } from "./whatsapp.js";

const args = new Set(process.argv.slice(2));
const ONCE = args.has("--once");
const CAPTURE_ONLY = args.has("--capture-only");

// A desktop Chrome, not "HeadlessChrome": WhatsApp Web turns the second away.
const USER_AGENT =
  "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/140.0.0.0 Safari/537.36";

const CONTEXT_OPTIONS = {
  viewport: { width: 1280, height: 900 },
  userAgent: USER_AGENT,
  // English, so the labels the selectors look for are the ones on the page.
  locale: "en-US",
  // "Today" is the office's today, wherever the server is.
  timezoneId: "Asia/Kolkata",
  acceptDownloads: true,
};

const launchOptions = () => ({
  headless: config.headless,
  ...(process.env.BROWSER_CHANNEL ? { channel: process.env.BROWSER_CHANNEL } : {}),
});

/** IST parts of a moment: date "2026-10-09", time "10:30", minutes since midnight. */
function ist(date = new Date()) {
  const parts = Object.fromEntries(
    new Intl.DateTimeFormat("en-GB", {
      timeZone: "Asia/Kolkata",
      year: "numeric", month: "2-digit", day: "2-digit",
      hour: "2-digit", minute: "2-digit", hourCycle: "h23",
    })
      .formatToParts(date)
      .map((p) => [p.type, p.value]),
  );
  return {
    date: `${parts.year}-${parts.month}-${parts.day}`,
    time: `${parts.hour}:${parts.minute}`,
    minutes: Number(parts.hour) * 60 + Number(parts.minute),
    pretty: new Intl.DateTimeFormat("en-IN", {
      timeZone: "Asia/Kolkata", day: "2-digit", month: "short", year: "numeric",
      hour: "numeric", minute: "2-digit", hour12: true,
    }).format(date),
  };
}

const made = (file) => `made ${path.basename(file)}${config.dryRun ? " (DRY_RUN, not sent)" : ""}`;

/** One branch's day as a message: written to out/, and sent unless not posting. */
async function postText(waPage, branch, target, records, at, stamp) {
  const rows = todaysRows(records, branch, indianDate(at));
  if (!rows.length) return "skipped: nobody marked today";

  const text = buildMessage({ rows, branch, now: at });
  const file = path.join(config.outDir, `${stamp}-${branch}.txt`);
  fs.writeFileSync(file, text);
  if (!waPage || config.dryRun) return made(file);

  const sent = await sendText(waPage, target, text, `Daily Attendance · ${branch} branch`);
  return `${sent} to "${target}"`;
}

/** One branch's day as the Attendance page's picture. */
async function postImage(payrollPage, waPage, branch, target, stamp, now) {
  const file = await captureBranchImage(payrollPage, branch, stamp);
  if (!file) return "skipped: nobody marked today";
  if (!waPage || config.dryRun) return made(file);

  await sendImage(waPage, target, file, `${branch} attendance — ${now.pretty}`);
  return `sent to "${target}"`;
}

/** Every branch's attendance for the day, each to its own group. */
async function runPosts(payrollPage, waPage, reason) {
  const now = ist();
  const stamp = `${now.date}_${now.time.replace(":", "")}`;
  log(`run (${reason}): ${Object.keys(config.groups).length} branches, as ${config.postAs}`);
  fs.mkdirSync(config.outDir, { recursive: true });

  if (waPage) {
    // A session can drop between runs -- a logout on the phone, an expired link.
    await ensureLinked(waPage);
  }

  // One read for every branch, so the five messages describe the same moment.
  let records = null;
  let readError = null;
  if (config.postAs === "text") {
    try {
      records = await fetchAttendance();
    } catch (err) {
      readError = err;
    }
  }
  const readAt = new Date();

  for (const [branch, group] of Object.entries(config.groups)) {
    const target = config.testChat || group;
    const entry = { at: new Date().toISOString(), reason, branch, group: target, result: "" };
    try {
      if (readError) throw readError;
      entry.result =
        config.postAs === "image"
          ? await postImage(payrollPage, waPage, branch, target, stamp, now)
          : await postText(waPage, branch, target, records, readAt, stamp);
      log(`  ${branch}: ${entry.result}`);
    } catch (err) {
      entry.result = `FAILED: ${err.message}`;
      status.lastError = `${new Date().toISOString()} ${branch}: ${err.message}`;
      log(`  ${branch}: FAILED -- ${err.message}`);
    }
    recordRun(entry);
  }
}

// ---------------------------------------------------------------- scheduling

const DONE_FILE = () => path.join(config.dataDir, "done.json");

/** Which date+time slots have already run -- kept on the volume, so a restart
 *  at 10:31 does not post the 10:30 images a second time. */
function loadDone() {
  try {
    return new Set(JSON.parse(fs.readFileSync(DONE_FILE(), "utf8")));
  } catch {
    return new Set();
  }
}
function saveDone(done) {
  fs.mkdirSync(config.dataDir, { recursive: true });
  // Only the last few days matter.
  const recent = [...done].sort().slice(-40);
  fs.writeFileSync(DONE_FILE(), JSON.stringify(recent));
}

// How late a slot may still run: a restart or a slow minute must not lose it,
// but a container started in the afternoon must not post the morning's.
const CATCH_UP_MINUTES = 20;

async function schedule(payrollPage, waPage) {
  const done = loadDone();
  const slots = config.runTimes.map((t) => {
    const [h, m] = t.split(":").map(Number);
    return { label: t, minutes: h * 60 + m };
  });
  log(`schedule: ${config.runTimes.join(" and ")} IST, every day`);

  let busy = false;
  const tick = async () => {
    if (busy) return;
    const now = ist();
    for (const slot of slots) {
      const key = `${now.date} ${slot.label}`;
      const late = now.minutes - slot.minutes;
      if (late < 0 || late > CATCH_UP_MINUTES || done.has(key)) continue;
      busy = true;
      // Marked first: a run that dies half way is not repeated into the groups
      // that already got their image. The log says what failed.
      done.add(key);
      saveDone(done);
      try {
        await runPosts(payrollPage, waPage, `scheduled ${slot.label}`);
      } catch (err) {
        status.lastError = `${new Date().toISOString()} ${err.message}`;
        log("run failed:", err.message);
      } finally {
        busy = false;
      }
    }
  };
  await tick();
  setInterval(tick, 20_000);
}

// ---------------------------------------------------------------------- main

// The open browser, so the way out can close it properly. A profile killed in
// the middle of a write can come back as WhatsApp's "database error", and that
// means linking the phone again.
let openContext = null;

async function shutdown(code) {
  const closing = openContext ? openContext.close().catch(() => {}) : null;
  openContext = null;
  // Not for ever: docker stop allows ten seconds before it kills.
  if (closing) await Promise.race([closing, new Promise((resolve) => setTimeout(resolve, 8000))]);
  process.exit(code);
}

async function main() {
  fs.mkdirSync(config.outDir, { recursive: true });
  const missing = missingForPosting();
  if (missing.length) log(`WARNING: ${missing.join(", ")} not set -- the attendance cannot be read`);

  if (CAPTURE_ONLY) {
    if (config.postAs === "text") {
      await runPosts(null, null, "capture-only");
      return;
    }
    const browser = await chromium.launch(launchOptions());
    const page = await (await browser.newContext(CONTEXT_OPTIONS)).newPage();
    await runPosts(page, null, "capture-only");
    await browser.close();
    return;
  }

  const context = await chromium.launchPersistentContext(config.profileDir, {
    ...launchOptions(),
    ...CONTEXT_OPTIONS,
  });
  openContext = context;
  const waPage = context.pages()[0] || (await context.newPage());
  // Only the picture needs the payroll site open; the text comes from its API.
  const payrollPage = config.postAs === "image" ? await context.newPage() : null;
  startStatusServer(() => waPage);

  await ensureLinked(waPage);

  if (ONCE) {
    await runPosts(payrollPage, waPage, "once");
    // Closing the browser on a message still on its way would lose it.
    await waPage.waitForTimeout(5000);
    openContext = null;
    await context.close();
    return;
  }
  await schedule(payrollPage, waPage);
}

for (const signal of ["SIGTERM", "SIGINT"]) {
  process.on(signal, () => {
    log(`${signal}: closing the browser`);
    shutdown(0);
  });
}

main().catch((err) => {
  log("fatal:", err.stack || err.message);
  shutdown(1);
});
