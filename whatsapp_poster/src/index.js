/**
 * Each branch's attendance for the day into that branch's WhatsApp group, on
 * the office's day (IST):
 *
 *   09:10  report        09:11  reminder: who has not logged in yet
 *   09:35  report        09:37  reminder
 *   10:05  final report  (the backend marks the no-shows Absent at 10:00)
 *   19:00  report        21:00  report
 *
 *   node src/index.js                          run for ever, on that schedule
 *   node src/index.js --once [--kind=final]    link if needed, post one run now, exit
 *   node src/index.js --capture-only [--kind=reminder]
 *                                              only make the messages (no WhatsApp), exit
 *
 * --kind is report (the default), reminder or final. Everything is read from
 * the payroll API; with POST_AS=image the report is instead the Attendance
 * page's own "Today (Image)" picture, made in a browser tab on the payroll
 * site. It all goes out through a WhatsApp Web tab that stays open between
 * runs, so the session stays warm.
 */
import fs from "node:fs";
import path from "node:path";

import { chromium } from "playwright";

import { buildFinalReport, buildMessage, buildReminder, indianDate, todaysRows } from "./attendanceText.js";
import { config, missingForPosting } from "./config.js";
import { log } from "./log.js";
import { captureBranchImage } from "./payroll.js";
import { fetchAttendance, fetchNotLoggedIn, signIn } from "./payrollApi.js";
import { recordRun, startStatusServer, status } from "./status.js";
import { ensureLinked, sendImage, sendText } from "./whatsapp.js";

const args = process.argv.slice(2);
const ONCE = args.includes("--once");
const CAPTURE_ONLY = args.includes("--capture-only");
const KINDS = ["report", "reminder", "final"];
const KIND = (args.find((a) => a.startsWith("--kind=")) || "--kind=report").slice("--kind=".length);
if (!KINDS.includes(KIND)) {
  console.error(`--kind must be one of ${KINDS.join(", ")}`);
  process.exit(2);
}

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

/** Keep the message in out/, and send it unless this run only makes them. */
async function deliver(waPage, target, text, file, mustSay) {
  fs.writeFileSync(file, text);
  if (!waPage || config.dryRun) return made(file);
  const sent = await sendText(waPage, target, text, mustSay);
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

/** What each kind of run posts for one branch. */
const POSTERS = {
  async report({ payrollPage, waPage, branch, target, data, at, stamp, now }) {
    if (config.postAs === "image") return postImage(payrollPage, waPage, branch, target, stamp, now);
    const rows = todaysRows(data.records, branch, indianDate(at));
    if (!rows.length) return "skipped: nobody marked today";
    return deliver(
      waPage, target, buildMessage({ rows, branch, now: at }),
      path.join(config.outDir, `${stamp}-${branch}.txt`), `Daily Attendance · ${branch} branch`,
    );
  },

  async reminder({ waPage, branch, target, data, at, stamp }) {
    const { pending } = data;
    if (pending.skip) return `skipped: ${pending.skip}`;
    const names = pending.branches?.[branch] || [];
    // Everybody has logged in -- or, after 10am, been marked Absent.
    if (!names.length) return "skipped: nobody left to remind";
    return deliver(
      waPage, target, buildReminder({ branch, names, cutoff: pending.cutoff, now: at }),
      path.join(config.outDir, `${stamp}-${branch}-reminder.txt`), `Login reminder · ${branch} branch`,
    );
  },

  async final({ waPage, branch, target, data, at, stamp }) {
    const rows = todaysRows(data.records, branch, indianDate(at));
    // Anybody still not logged in AND not marked: the 10am job did not run.
    const notMarked = data.pending && !data.pending.skip ? data.pending.branches?.[branch] || [] : [];
    if (!rows.length && !notMarked.length) return "skipped: nobody marked today";
    return deliver(
      waPage, target, buildFinalReport({ rows, branch, notMarked, now: at }),
      path.join(config.outDir, `${stamp}-${branch}-final.txt`), `Final Attendance · ${branch} branch`,
    );
  },
};

/** One read of the payroll for every branch, so the five messages describe
 *  the same moment. */
async function readPayroll(kind) {
  if (kind === "report" && config.postAs === "image") return {};
  const token = await signIn();
  if (kind === "reminder") return { pending: await fetchNotLoggedIn(token) };

  const records = await fetchAttendance(token);
  if (kind === "report") return { records };

  // The final report still goes without the list; it only loses the line
  // that would show the 10am job had not run.
  let pending = null;
  try {
    pending = await fetchNotLoggedIn(token);
  } catch (err) {
    log(`  (no not-logged-in list for the final report: ${err.message})`);
  }
  return { records, pending };
}

/** One kind of post, every branch, each to its own group. */
async function runPosts(payrollPage, waPage, kind, reason) {
  const now = ist();
  const stamp = `${now.date}_${now.time.replace(":", "")}`;
  log(`run (${reason}): ${kind}, ${Object.keys(config.groups).length} branches`);
  fs.mkdirSync(config.outDir, { recursive: true });

  if (waPage) {
    // A session can drop between runs -- a logout on the phone, an expired link.
    await ensureLinked(waPage);
  }

  let data = null;
  let readError = null;
  try {
    data = await readPayroll(kind);
  } catch (err) {
    readError = err;
  }
  const at = new Date();

  for (const [branch, group] of Object.entries(config.groups)) {
    const target = config.testChat || group;
    const entry = { at: new Date().toISOString(), reason, kind, branch, group: target, result: "" };
    try {
      if (readError) throw readError;
      entry.result = await POSTERS[kind]({ payrollPage, waPage, branch, target, data, at, stamp, now });
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

/** Which slots have already run today -- kept on the volume, so a restart at
 *  9:12 does not post the 9:10 report a second time. */
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
  const recent = [...done].sort().slice(-60);
  fs.writeFileSync(DONE_FILE(), JSON.stringify(recent));
}

// How late a slot may still run: a restart or a slow minute must not lose it,
// but a container started in the afternoon must not post the morning's.
const CATCH_UP_MINUTES = 20;

function slotsOfTheDay() {
  return [
    ...config.runTimes.map((label) => ({ label, kind: "report" })),
    ...config.reminderTimes.map((label) => ({ label, kind: "reminder" })),
    ...config.finalTimes.map((label) => ({ label, kind: "final" })),
  ]
    .map((slot) => {
      const [h, m] = slot.label.split(":").map(Number);
      return { ...slot, minutes: h * 60 + m };
    })
    .sort((a, b) => a.minutes - b.minutes);
}

async function schedule(payrollPage, waPage) {
  const done = loadDone();
  const slots = slotsOfTheDay();
  log(`schedule (IST, every day): ${slots.map((s) => `${s.label} ${s.kind}`).join(", ")}`);

  let busy = false;
  const tick = async () => {
    if (busy) return;
    const now = ist();
    for (const slot of slots) {
      const key = `${now.date} ${slot.label} ${slot.kind}`;
      const late = now.minutes - slot.minutes;
      if (late < 0 || late > CATCH_UP_MINUTES || done.has(key)) continue;
      busy = true;
      // Marked first: a run that dies half way is not repeated into the groups
      // that already got their message. The log says what failed.
      done.add(key);
      saveDone(done);
      try {
        await runPosts(payrollPage, waPage, slot.kind, `scheduled ${slot.label}`);
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
    if (KIND !== "report" || config.postAs === "text") {
      await runPosts(null, null, KIND, "capture-only");
      return;
    }
    const browser = await chromium.launch(launchOptions());
    const page = await (await browser.newContext(CONTEXT_OPTIONS)).newPage();
    await runPosts(page, null, KIND, "capture-only");
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
    await runPosts(payrollPage, waPage, KIND, "once");
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
