/**
 * Each checked-in work order's "Problem Resolution" place, read from FieldEZ
 * for the KM Report's FIELDEZ table.
 *
 *   node src/index.js          a pass every RUN_EVERY_MINUTES (30), only between
 *                              RUN_FROM and RUN_TO IST (08:00-22:30)
 *   node src/index.js --once   one pass now, then exit
 *
 * A pass: sign in to the payroll, ask which work orders it wants read, open
 * FieldEZ in this worker's own Chrome (logging in if it has to), copy the
 * headers FieldEZ's own pages send, read the work orders one at a time with a
 * pause between, hand the answers to the payroll and close Chrome. What the
 * answers mean for the table is the payroll's business (fieldez_km); this
 * only reads, and it writes nothing to FieldEZ.
 *
 * One line in the log per pass -- counts only, no names or work orders.
 */
import { config } from "./config.js";
import { captureAuthHeaders, ensureLoggedIn, onLoginPage, openContext, readWorkOrder } from "./fieldez.js";
import { fetchWanted, postResults, signIn } from "./payrollApi.js";
import { woKey } from "./resolve.js";
import { inWindow, istMinutes, minutesOf } from "./schedule.js";

const ONCE = process.argv.slice(2).includes("--once");

// This many work orders failing in a row is FieldEZ in trouble, not the work
// orders: the pass stops instead of marking every one after them failed.
// The run itself is still reported, so a work order that can never be read
// cannot hold up the ones behind it pass after pass.
const STOP_AFTER_FAILURES = 5;

/** One line per event, with the IST time, as the WhatsApp poster logs. */
const istNow = () => new Date().toLocaleString("en-IN", { timeZone: "Asia/Kolkata", hour12: false });
const log = (...parts) => console.log(`[${istNow()}]`, ...parts);

// An error's first line only. A Playwright error goes on with its call log,
// and that lists the request's headers -- the FieldEZ login's token included.
const firstLine = (err) => String(err?.message ?? err).split(/\r?\n/)[0];

const sleep = (ms) => new Promise((resolve) => setTimeout(resolve, ms));

// The open browser, so the way out can close it properly.
let openBrowser = null;

/** Read every wanted work order: {results, stopped} -- why it stopped early, or "". */
async function readAll(list) {
  const results = [];
  let stopped = "";
  const { context, page } = await openContext();
  openBrowser = context;
  try {
    await ensureLoggedIn(page);
    const headers = await captureAuthHeaders(page);
    // FieldEZ's page can go to its login only after a moment.
    if (onLoginPage(page.url())) throw new Error("not logged in to FieldEZ");

    let failures = 0;
    for (const [index, wo] of list.entries()) {
      if (index > 0) await sleep(config.pauseMs);
      // A person on this computer can close the window; that is not the
      // work orders' doing either.
      if (page.isClosed()) {
        stopped = "the FieldEZ window was closed";
        break;
      }
      let result;
      try {
        result = await readWorkOrder(page, headers, wo);
      } catch (err) {
        // Only a lost login comes out of readWorkOrder.
        stopped = firstLine(err);
        break;
      }
      if (result.status === "failed" && page.isClosed()) {
        stopped = "the FieldEZ window was closed";
        break;
      }
      results.push(result);
      failures = result.status === "failed" ? failures + 1 : 0;
      if (failures >= STOP_AFTER_FAILURES) {
        stopped = `${failures} work orders in a row could not be read`;
        break;
      }
    }
  } finally {
    openBrowser = null;
    await context.close().catch(() => {});
  }
  return { results, stopped };
}

/** One pass, start to end. Never throws: true when it all went through. */
async function runPass(reason) {
  const started = Date.now();
  const took = () => `${Math.round((Date.now() - started) / 1000)}s`;
  try {
    if (!config.payrollUsername || !config.payrollPassword) {
      throw new Error("PAYROLL_USERNAME / PAYROLL_PASSWORD are not set");
    }
    const token = await signIn();
    const wanted = [...new Set((await fetchWanted(token, config.wantedDays)).map(woKey).filter(Boolean))];
    // Nothing to read, no browser -- except with --once, which is also how a
    // person logs in to FieldEZ the first time.
    if (!wanted.length && !ONCE) {
      log(`pass (${reason}): nothing wanted (${took()})`);
      return true;
    }

    const { results, stopped } = await readAll(wanted.slice(0, config.maxPerPass));
    const count = (status) => results.filter((r) => r.status === status).length;

    // What was read is handed over even when the reading stopped half way.
    let saved = "nothing to save";
    let ok = !stopped;
    if (results.length) {
      try {
        const answer = await postResults(token, results);
        saved = `saved ${answer.saved}, rejected ${answer.rejected}`;
      } catch (err) {
        saved = `NOT saved: ${firstLine(err)}`;
        ok = false;
      }
    }
    log(
      `pass (${reason}): ${wanted.length} wanted, ${results.length} read -- ${count("found")} found, ` +
        `${count("no_resolution")} no Problem Resolution place, ${count("not_found")} not in FieldEZ, ` +
        `${count("failed")} failed; ${saved}${stopped ? `; stopped early: ${stopped}` : ""} (${took()})`,
    );
    return ok;
  } catch (err) {
    log(`pass (${reason}) FAILED: ${firstLine(err)} (${took()})`);
    return false;
  }
}

/** npm start: a pass every runEveryMinutes, inside the IST window, for ever. */
async function schedule() {
  const from = minutesOf(config.runFrom, "RUN_FROM");
  const to = minutesOf(config.runTo, "RUN_TO");
  const every = config.runEveryMinutes * 60_000;
  log(`schedule: a pass every ${config.runEveryMinutes} min, ${config.runFrom}-${config.runTo} IST`);

  let lastStart = 0;
  for (;;) {
    if (inWindow(istMinutes(), from, to) && Date.now() - lastStart >= every) {
      lastStart = Date.now();
      await runPass("scheduled");
    }
    await sleep(30_000);
  }
}

async function shutdown(code) {
  const closing = openBrowser ? openBrowser.close().catch(() => {}) : null;
  openBrowser = null;
  // A profile killed in the middle of a write can lose its login.
  if (closing) await Promise.race([closing, sleep(8000)]);
  process.exit(code);
}

async function main() {
  if (ONCE) {
    process.exitCode = (await runPass("once")) ? 0 : 1;
    return;
  }
  await schedule();
}

for (const signal of ["SIGTERM", "SIGINT"]) {
  process.on(signal, () => {
    log(`${signal}: closing the browser`);
    shutdown(0);
  });
}

main().catch((err) => {
  log("fatal:", firstLine(err));
  shutdown(1);
});
