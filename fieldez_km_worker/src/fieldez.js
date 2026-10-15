/**
 * FieldEZ, read the way its own pages read it: in a logged-in browser, with
 * the headers its own requests carry.
 *
 * Two reads, and only these two -- FieldEZ has write endpoints right beside
 * them, and nothing here calls anything else or ever logs out:
 *
 *   POST /tlm/v1.0/ticket/summary-list/search   Ticket/Summary's "Enter Ticket No"
 *   GET  /tlm/v1.0/ticket-activity/ticket/{id}  the ticket's Task Details rows
 *
 * The calls are copied from OpenCall's SLA job (fieldezSlaJob.ts:
 * captureAuthHeaders, searchTicket), which reads FieldEZ this way every day --
 * copied, not imported: the two are separate projects. Its own browser
 * profile (config.profileDir), never the OpenCall fieldez-worker's.
 */
import fs from "node:fs";
import { chromium } from "playwright";
import { config } from "./config.js";
import { parseTicketList, pickTicket, resultFor, woKey } from "./resolve.js";

export const onLoginPage = (url) => /\/auth\b|login/i.test(url);

// Set once FieldEZ has turned FIELDEZ_KM_USERNAME / FIELDEZ_KM_PASSWORD away:
// for as long as this process runs the form is not filled again, so a stale
// password is not typed in on every pass -- an account that locks itself after
// repeated failures would be locked, and it may be someone's working login.
let refused = false;
const REFUSED = "FieldEZ refused FIELDEZ_KM_USERNAME / FIELDEZ_KM_PASSWORD -- fix .env and start the worker again";

/** For the tests: as if this process had never been turned away. */
export function forgetRefusal() {
  refused = false;
}

const summaryList = () => `${config.fieldezBaseUrl}/frontend/tlm/summary-list`;

// The headers worth copying off FieldEZ's own requests -- the SLA job's list.
const AUTH_HEADERS = ["authorization", "x-auth-token", "x-access-token", "token", "orgid", "x-org-id", "x-tenant-id"];

/** The worker's own Chrome on its own profile, with whatever login that profile holds. */
export async function openContext() {
  fs.mkdirSync(config.profileDir, { recursive: true });
  const context = await chromium.launchPersistentContext(config.profileDir, {
    channel: config.browserChannel,
    headless: config.headless,
  });
  const page = context.pages()[0] || (await context.newPage());
  return { context, page };
}

/**
 * Logged in to FieldEZ, or an error -- as src/probe.js does it: the login
 * form filled in when FIELDEZ_KM_USERNAME / FIELDEZ_KM_PASSWORD are set (the
 * same fill the OpenCall fieldez-worker uses), else a person logs in in the
 * window. One try for the life of the process: a password FieldEZ refused is
 * not typed again (see `refused`).
 */
export async function ensureLoggedIn(page) {
  await page.goto(summaryList(), { waitUntil: "domcontentloaded", timeout: 60_000 });
  await page.waitForTimeout(3000);
  if (!onLoginPage(page.url())) return;

  if (config.fieldezUsername && config.fieldezPassword) {
    if (refused) throw new Error(REFUSED);
    await page.getByPlaceholder(/user ?name/i).first().fill(config.fieldezUsername);
    await page.locator("input[type=password]").first().fill(config.fieldezPassword);
    await page.getByRole("button", { name: /log ?in/i }).first().click();
    await page.waitForLoadState("networkidle").catch(() => {});
    await page.waitForTimeout(3000);
    if (onLoginPage(page.url())) {
      refused = true;
      throw new Error(REFUSED);
    }
  } else if (config.headless) {
    // Nobody can log in to a window that is not shown.
    throw new Error("not logged in to FieldEZ -- run `npm run once` with HEADLESS=false and log in, or set FIELDEZ_KM_USERNAME / FIELDEZ_KM_PASSWORD");
  } else {
    await page.bringToFront();
    console.log("\n>>> Log in to FieldEZ in the Chrome window that opened. Waiting up to", Math.round(config.loginWaitMs / 60000), "minutes...\n");
    const until = Date.now() + config.loginWaitMs;
    while (onLoginPage(page.url()) && Date.now() < until) await page.waitForTimeout(2000);
  }
  if (onLoginPage(page.url())) throw new Error("not logged in to FieldEZ");
  await page.goto(summaryList(), { waitUntil: "domcontentloaded", timeout: 60_000 });
  await page.waitForTimeout(3000);
}

/**
 * The headers FieldEZ's frontend sends with its API calls, copied off its
 * first one rather than assumed. Cookies come along on their own because
 * page.request shares the browser's, but a bearer token or an org header
 * would not -- and a hand-built request without them gets a 401. {} when its
 * calls carry none: cookies alone, then.
 */
export async function captureAuthHeaders(page) {
  let captured = null;

  const onRequest = (request) => {
    if (captured) return;
    if (!/\/(tlm|admin|dashboard)\/v1\.0\//i.test(request.url())) return;
    const headers = request.headers();
    const picked = {};
    for (const name of AUTH_HEADERS) {
      const value = headers[name];
      if (value) picked[name] = value;
    }
    captured = picked;
  };

  page.on("request", onRequest);
  try {
    await page.goto(summaryList(), { waitUntil: "domcontentloaded", timeout: 45_000 });
    await page.waitForTimeout(5000);
  } finally {
    page.off("request", onRequest);
  }
  return captured ?? {};
}

/**
 * The JSON FieldEZ answered, or an error naming the status only -- never the
 * body. A 401 is marked: the login is gone, which is not the work order's doing.
 */
async function answer(res, what) {
  if (res.status() === 401) {
    const err = new Error(`FieldEZ answered 401 to the ${what}: not logged in`);
    err.loggedOut = true;
    throw err;
  }
  if (!res.ok()) throw new Error(`${what} answered ${res.status()}`);
  try {
    return await res.json();
  } catch {
    throw new Error(`${what} answered with something that is not JSON`);
  }
}

/**
 * The work order's ticket, through the search the Ticket/Summary box drives:
 * {id, bpId, ticketNo}, or null when FieldEZ has no ticket with exactly this
 * WO. It finds closed work orders too. Unlike the SLA job's, it throws when
 * FieldEZ does not answer: "not there" and "could not ask" are different answers.
 */
export async function searchTicket(page, headers, wo) {
  // 50 a page, as FieldEZ's own search box asks: the search is a contains-match,
  // and a WO's revisits ("-1", "-2", ...) come back with it.
  const res = await page.request.post(`${config.fieldezBaseUrl}/tlm/v1.0/ticket/summary-list/search?page=0&rows=50`, {
    headers: { ...headers, "Content-Type": "application/json" },
    // The exact body the search box sends, captured rather than reasoned out.
    data: { searchKey: "Work Order", searchValue: wo },
    timeout: 45_000,
  });
  const json = await answer(res, "search");
  // A 200 with no ticket list in it -- a lapsed login's {"message": ..., "data":
  // null} -- is "could not ask", not "not there": read as not_found it would
  // put a false "not in FieldEZ" on the check-in.
  if (!Array.isArray(json?.data?.ticketList) && !Array.isArray(json)) {
    throw new Error("search came back in a shape this does not know");
  }
  const list = parseTicketList(json);
  // Two different tickets for one work order is no answer either: the wrong
  // one would be a wrong place. Said, so the check-in is not called absent.
  const wanted = woKey(wo);
  if (new Set(list.filter((ticket) => woKey(ticket.ticketNo) === wanted).map((ticket) => ticket.id)).size > 1) {
    throw new Error("FieldEZ has more than one ticket for this work order");
  }
  const ticket = pickTicket(list, wo);
  // Not on a page FieldEZ cut short is not "not there": the ticket may be on
  // the next one. Said, so the check-in is not called absent.
  const shown = Array.isArray(json?.data?.ticketList) ? json.data.ticketList.length : 0;
  if (!ticket && Number(json?.data?.totalRecords) > shown) {
    throw new Error("search answer was cut short");
  }
  return ticket;
}

/** One ticket's activity list -- its Task Details rows. Measured as a bare array; a data wrapper is read too. */
export async function readActivities(page, headers, id) {
  const res = await page.request.get(`${config.fieldezBaseUrl}/tlm/v1.0/ticket-activity/ticket/${encodeURIComponent(id)}`, {
    headers,
    timeout: 45_000,
  });
  const json = await answer(res, "activities");
  if (Array.isArray(json)) return json;
  if (Array.isArray(json?.data)) return json.data;
  throw new Error("activities came back in a shape this does not know");
}

/**
 * One work order, search to Task Details: its result for the payroll (see
 * resultFor). Whatever stops the reading is this work order's "failed" --
 * except a lost login, which is thrown: that is the pass's problem, and
 * every work order after it would fail the same way.
 */
export async function readWorkOrder(page, headers, wo) {
  let ticket = null;
  try {
    ticket = await searchTicket(page, headers, wo);
    if (!ticket) return resultFor(wo, null, null, null);
    return resultFor(wo, ticket, await readActivities(page, headers, ticket.id), null);
  } catch (err) {
    if (err.loggedOut) throw err;
    return resultFor(wo, ticket, null, err);
  }
}
