/**
 * What FieldEZ's two answers say about one work order, in the shape the
 * payroll stores it (POST /api/fieldez_km/work_orders/).
 *
 * Pure: no browser and no network, so all of it is tested on its own
 * (test/resolve.test.js). The answers were measured on 14 Oct 2026 with
 * src/probe.js:
 *
 *   POST /tlm/v1.0/ticket/summary-list/search   {data: {ticketList: [{id, bpId, ft_ticket_no, ...}]}}
 *   GET  /tlm/v1.0/ticket-activity/ticket/{id}  [{fta_name, updatedBy, syncTime, createTime,
 *                                                 latitude, longitude, locationAddress, ...}]
 */

// The payroll's own rule (fieldez_km's wo_key): "WO", an optional dash, six
// or more digits, and a revisit's "-1" if there is one.
const WO = /^WO-?(\d{6,})(-\d+)?$/;

// The status whose place the FIELDEZ table uses. Exactly this one:
// "Problem Resolution FCT Validate" is the office's step, with no place, and
// "Problem Resolution (Buffer)" is not it either.
const PROBLEM_RESOLUTION = "problem resolution";

// The most the payroll takes for one work order.
const MAX_RESOLUTIONS = 50;

// A time FieldEZ could have written. Anything outside is a bad number, and a
// year like +287396 would make the payroll turn the whole work order away.
const EARLIEST = Date.UTC(2000, 0, 1);
const LATEST = Date.UTC(2100, 0, 1);

/**
 * The one spelling of a work order both sides compare: uppercase, no
 * whitespace, "WO-<digits><suffix>". "wo036165368" -> "WO-036165368";
 * "WO-035260625-1" stays -- a revisit is a different work order. Anything
 * else (blank, a serial number) is not a work order: null.
 */
export function woKey(text) {
  const match = String(text ?? "").toUpperCase().replace(/\s+/g, "").match(WO);
  return match ? `WO-${match[1]}${match[2] || ""}` : null;
}

// FieldEZ writes "nothing here" as a dash on screen and sometimes as the
// string "null" (the same reading as OpenCall's fieldezSlaParse.ts).
function text(value) {
  if (typeof value !== "string" && typeof value !== "number") return "";
  const out = String(value).trim();
  return out === "-" || out === "—" || out === "null" || out === "undefined" ? "" : out;
}

/** A row id: a whole number above zero, or null. It goes into a URL. */
function rowId(value) {
  const n = typeof value === "number" ? value : typeof value === "string" && /^\d+$/.test(value.trim()) ? Number(value) : NaN;
  return Number.isSafeInteger(n) && n > 0 ? n : null;
}

/**
 * The array of ticket rows, wherever it is. Measured in data.ticketList;
 * found rather than hard-coded, as OpenCall's SLA job does it, so another
 * wrapper in the next FieldEZ release costs nothing.
 */
function findRows(payload) {
  const stack = [payload];
  let depth = 0;
  while (stack.length > 0 && depth < 500) {
    depth += 1;
    const node = stack.shift();
    if (Array.isArray(node)) {
      const rows = node.filter((item) => item && typeof item === "object" && !Array.isArray(item) && "ft_ticket_no" in item);
      if (rows.length > 0) return rows;
      stack.push(...node);
      continue;
    }
    if (node && typeof node === "object") stack.push(...Object.values(node));
  }
  return [];
}

/** The tickets in a search answer: [{id, bpId, ticketNo}]. */
export function parseTicketList(json) {
  const out = [];
  for (const row of findRows(json)) {
    const ticketNo = text(row.ft_ticket_no);
    const id = rowId(row.id);
    if (!ticketNo || id === null) continue;
    out.push({ id, bpId: rowId(row.bpId), ticketNo });
  }
  return out;
}

/**
 * The search's row for this work order, or null. The search is a
 * contains-match -- a WO brings back its "-1" revisit too -- so only an exact
 * wo_key match is this work order, and two different tickets claiming it is
 * no answer at all: a wrong ticket is a wrong place.
 */
export function pickTicket(list, wo) {
  const wanted = woKey(wo);
  if (!wanted || !Array.isArray(list)) return null;
  const hits = list.filter((ticket) => ticket && woKey(ticket.ticketNo) === wanted);
  if (!hits.length) return null;
  return hits.every((ticket) => ticket.id === hits[0].id) ? hits[0] : null;
}

/** A latitude or longitude FieldEZ really recorded: finite, in range, and not 0 -- its "no location". */
function coordinate(value, limit) {
  const n = typeof value === "number" ? value : typeof value === "string" && value.trim() !== "" ? Number(value) : NaN;
  return Number.isFinite(n) && n !== 0 && Math.abs(n) <= limit ? n : null;
}

/**
 * An epoch time as ISO-8601 UTC. FieldEZ sends milliseconds; a number too
 * small to be milliseconds since 1973 is read as seconds, never as 1970.
 */
function instant(value) {
  const raw = typeof value === "number" ? value : typeof value === "string" && /^\d+$/.test(value.trim()) ? Number(value) : NaN;
  if (!Number.isFinite(raw) || raw <= 0) return null;
  const ms = raw < 100_000_000_000 ? raw * 1000 : raw;
  return ms >= EARLIEST && ms < LATEST ? new Date(ms).toISOString() : null;
}

/**
 * Every "Problem Resolution" with a place, oldest first:
 * [{at, user, latitude, longitude, address}]. The status name is matched
 * whole, ignoring case and the spaces around it. `at` is syncTime -- the time
 * FieldEZ's own screen shows -- or createTime, a second later, when it is
 * missing. An entry without a place or a time is left out.
 */
export function problemResolutions(activities) {
  if (!Array.isArray(activities)) return [];
  const out = [];
  for (const entry of activities) {
    if (!entry || typeof entry !== "object") continue;
    if (typeof entry.fta_name !== "string" || entry.fta_name.trim().toLowerCase() !== PROBLEM_RESOLUTION) continue;
    const latitude = coordinate(entry.latitude, 90);
    const longitude = coordinate(entry.longitude, 180);
    const at = instant(entry.syncTime) ?? instant(entry.createTime);
    if (latitude === null || longitude === null || !at) continue;
    out.push({ at, user: text(entry.updatedBy), latitude, longitude, address: text(entry.locationAddress) });
  }
  // Same-format ISO strings sort as text. Past the payroll's limit, the latest are kept.
  out.sort((a, b) => (a.at < b.at ? -1 : a.at > b.at ? 1 : 0));
  return out.slice(-MAX_RESOLUTIONS);
}

// What went wrong, in one short line: a Playwright error's first line, never
// its call log -- that lists the request's headers, the login's token included.
function errorText(error) {
  const message = error instanceof Error ? error.message : String(error);
  return message.split(/\r?\n/)[0].trim().slice(0, 200) || "could not be read";
}

/**
 * One work order's answer for the payroll: {wo, status, fieldez_ticket_id,
 * resolutions, error}, status one of
 *
 *   found          resolutions holds every Problem Resolution with a place
 *   no_resolution  the ticket has none with a place (yet)
 *   not_found      the search has no ticket with exactly this WO
 *   failed         FieldEZ could not be read for it; error says why
 *
 * `ticket` is pickTicket's answer, `activities` the ticket's activity list,
 * `error` whatever stopped the reading (or null).
 */
export function resultFor(wo, ticket, activities, error) {
  const result = {
    wo: woKey(wo) || String(wo ?? ""),
    status: "failed",
    fieldez_ticket_id: ticket && ticket.id != null ? String(ticket.id) : "",
    resolutions: [],
    error: "",
  };
  if (error) return { ...result, error: errorText(error) };
  if (!ticket) return { ...result, status: "not_found" };
  if (!Array.isArray(activities)) return { ...result, error: "no activity list for the ticket" };
  const resolutions = problemResolutions(activities);
  return { ...result, status: resolutions.length ? "found" : "no_resolution", resolutions };
}
