/**
 * The attendance list, straight from the payroll API.
 *
 * The same request the Attendance page makes when it opens -- GET
 * /api/attendance/ with no filters, as a login that sees every branch -- so
 * the rows are the ones its "Today (Image)" picture is drawn from. Asked
 * directly rather than through the page: a text message needs the rows, not a
 * browser tab, and one request a run is all it takes.
 */
import { config } from "./config.js";

const TIMEOUT_MS = 90_000;

async function call(path, { method = "GET", token, body } = {}) {
  const res = await fetch(`${config.payrollApiUrl}${path}`, {
    method,
    headers: {
      Accept: "application/json",
      ...(body ? { "Content-Type": "application/json" } : {}),
      ...(token ? { Authorization: `Bearer ${token}` } : {}),
    },
    body: body ? JSON.stringify(body) : undefined,
    signal: AbortSignal.timeout(TIMEOUT_MS),
  });
  if (!res.ok) {
    // The status only. A body here can echo what was sent, password included.
    const err = new Error(`payroll: ${method} ${path} answered ${res.status}`);
    err.status = res.status;
    throw err;
  }
  return res.json();
}

/** A token for the poster's own login; one run signs in once. */
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
 * Who has not logged in yet today, by branch -- the server's list, the same
 * people its 10am job marks Absent. {date, cutoff, skip, branches: {Salem: [names]}};
 * `skip` says why there is nobody to chase (Sunday, or nobody logged in at all).
 */
export async function fetchNotLoggedIn(token) {
  try {
    return await call("/api/attendance/not_logged_in/", { token: token || (await signIn()) });
  } catch (err) {
    if (err.status === 404) {
      throw new Error("payroll: /api/attendance/not_logged_in/ is not there -- redeploy the backend");
    }
    throw err;
  }
}

/** Every attendance row the poster's login can see. */
export async function fetchAttendance(token) {
  const data = await call("/api/attendance/", { token: token || (await signIn()) });
  // The API has answered with a bare list and with {results: [...]}; the page
  // reads both, and so does this.
  if (Array.isArray(data)) return data;
  if (Array.isArray(data?.results)) return data.results;
  if (Array.isArray(data?.data)) return data.data;
  throw new Error("payroll: the attendance list came back in a shape this does not know");
}
