/**
 * The payroll's side of the FIELDEZ table: which work orders it wants read
 * from FieldEZ, and where the answers go.
 *
 *   GET  /api/fieldez_km/wanted/?days=31  {days, work_orders: ["WO-...", ...]}, never-read ones first
 *   POST /api/fieldez_km/work_orders/     {results: [...]} -> {saved, rejected}
 *
 * Signed in the way the WhatsApp poster's payrollApi.js does it, as
 * PAYROLL_USERNAME -- an HR or admin login: these endpoints are staff-only.
 */
import { config } from "./config.js";

const TIMEOUT_MS = 90_000;

// Results per POST. The payroll takes up to 200; 25 keeps every request small.
const BATCH = 25;

async function call(path, { method = "GET", token, body } = {}) {
  let res;
  try {
    res = await fetch(`${config.payrollApiUrl}${path}`, {
      method,
      headers: {
        Accept: "application/json",
        ...(body ? { "Content-Type": "application/json" } : {}),
        ...(token ? { Authorization: `Bearer ${token}` } : {}),
      },
      body: body ? JSON.stringify(body) : undefined,
      signal: AbortSignal.timeout(TIMEOUT_MS),
    });
  } catch (err) {
    // fetch's own words are "fetch failed"; the reason is in its cause.
    throw new Error(`payroll: ${method} ${path} could not be reached (${err.cause?.code || err.name})`);
  }
  if (!res.ok) {
    // The status only. A body here can echo what was sent, password included.
    const err = new Error(`payroll: ${method} ${path} answered ${res.status}`);
    err.status = res.status;
    throw err;
  }
  try {
    return await res.json();
  } catch {
    // JSON.parse's own message quotes the start of the body.
    throw new Error(`payroll: ${method} ${path} answered with something that is not JSON`);
  }
}

/** A token for the worker's own login; one pass signs in once. */
export async function signIn() {
  let access;
  try {
    ({ access } = await call("/api/auth/login/", {
      method: "POST",
      body: { username: config.payrollUsername, password: config.payrollPassword },
    }));
  } catch (err) {
    if (err.status === 400 || err.status === 401) {
      throw new Error("payroll: sign-in refused -- check PAYROLL_USERNAME / PAYROLL_PASSWORD");
    }
    throw err;
  }
  if (!access) throw new Error("payroll: sign-in gave no token");
  return access;
}

/**
 * The work orders the payroll wants read: check-ins of the last `days` IST
 * days whose FieldEZ answer it does not have yet, or has an old one of. At
 * most 200: never read first, then re-reads newest check-in first, then
 * failed reads -- the payroll's order (fieldez_km points.wanted_work_orders).
 */
export async function fetchWanted(token, days = config.wantedDays) {
  let data;
  try {
    data = await call(`/api/fieldez_km/wanted/?days=${days}`, { token: token || (await signIn()) });
  } catch (err) {
    if (err.status === 404) throw new Error("payroll: /api/fieldez_km/wanted/ is not there -- redeploy the backend");
    if (err.status === 403) throw new Error("payroll: /api/fieldez_km/wanted/ refused -- PAYROLL_USERNAME must be an HR or admin login");
    throw err;
  }
  if (!Array.isArray(data?.work_orders)) throw new Error("payroll: the wanted list came back in a shape this does not know");
  return data.work_orders;
}

/** Every result, 25 to a request: {saved, rejected}, added up. Stops at the first request that fails. */
export async function postResults(token, results) {
  const auth = token || (await signIn());
  const total = { saved: 0, rejected: 0 };
  for (let index = 0; index < results.length; index += BATCH) {
    let answer;
    try {
      answer = await call("/api/fieldez_km/work_orders/", {
        method: "POST",
        token: auth,
        body: { results: results.slice(index, index + BATCH) },
      });
    } catch (err) {
      if (err.status === 404) throw new Error("payroll: /api/fieldez_km/work_orders/ is not there -- redeploy the backend");
      throw err;
    }
    total.saved += Number(answer?.saved) || 0;
    total.rejected += Number(answer?.rejected) || 0;
  }
  return total;
}
