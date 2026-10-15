import assert from "node:assert/strict";
import test from "node:test";

import { parseTicketList, pickTicket, problemResolutions, resultFor, woKey } from "../src/resolve.js";

// Shaped like FieldEZ's answers as measured on 14 Oct 2026 -- with made-up
// work orders, people and places, not a real customer's.
const searchAnswer = (rows) => ({
  id: "c0ffee",
  timestamp: 1791782880000,
  message: "Success",
  data: { ticketList: rows, totalRecords: rows.length },
});
const row = (id, ticketNo, extra = {}) => ({ id, bpId: 9, ft_ticket_no: ticketNo, ftTaskName: "WO Closed", ...extra });

// 2026-10-12 10:57:55 IST, as FieldEZ writes a status time: epoch milliseconds.
const SYNC = 1791782875512;
const activity = (fta_name, extra = {}) => ({
  fta_name,
  updatedBy: "Test Engineer",
  syncTime: SYNC,
  createTime: SYNC + 1000,
  latitude: 11.1111,
  longitude: 77.7777,
  locationAddress: "1 Test Street, Testpuram",
  ...extra,
});

const TICKET = { id: 1000001, bpId: 9, ticketNo: "WO-012345678" };
const PLACE = {
  at: "2026-10-12T05:27:55.512Z",
  user: "Test Engineer",
  latitude: 11.1111,
  longitude: 77.7777,
  address: "1 Test Street, Testpuram",
};

test("wo_key: one spelling of a work order", () => {
  assert.equal(woKey("wo012345678"), "WO-012345678");
  assert.equal(woKey("WO012345678"), "WO-012345678");
  assert.equal(woKey("WO-012345678"), "WO-012345678");
  // Every whitespace goes, wherever it is.
  assert.equal(woKey(" wo - 0123 45678\t"), "WO-012345678");
  // A revisit keeps its suffix, and is a different work order.
  assert.equal(woKey("WO-012345678-1"), "WO-012345678-1");
  assert.equal(woKey("wo012345678 -12"), "WO-012345678-12");
  assert.notEqual(woKey("WO-012345678-1"), woKey("WO-012345678"));
  assert.equal(woKey("WO-0123456789"), "WO-0123456789");
});

test("wo_key: anything else is not a work order", () => {
  for (const bad of [
    "",
    "   ",
    null,
    undefined,
    "WO-12345", // five digits
    "012345678", // no WO
    "SN5CG1234XYZ", // a serial number
    "WO--012345678",
    "WO-012345678-",
    "XWO-012345678",
    "WO-012345678A",
    "WO_012345678",
    "WO-012345678-1-2",
  ]) {
    assert.equal(woKey(bad), null, `${JSON.stringify(bad)} is not a work order`);
  }
});

test("the search answer's tickets, out of its data wrapper", () => {
  const list = parseTicketList(
    searchAnswer([
      row(1000001, "WO-012345678"),
      row("1000002", "WO-012345678-1"),
      row(1000003, "WO-012345679", { bpId: undefined }),
      row(null, "WO-012345680"), // no id: cannot be opened
      row(1000004, ""), // no work order
      row(1000005, "null"),
      row(-3, "WO-012345681"),
    ]),
  );
  assert.deepEqual(list, [
    { id: 1000001, bpId: 9, ticketNo: "WO-012345678" },
    { id: 1000002, bpId: 9, ticketNo: "WO-012345678-1" },
    { id: 1000003, bpId: null, ticketNo: "WO-012345679" },
  ]);
  // The answer's own id is not a ticket; nothing found is an empty list.
  assert.deepEqual(parseTicketList(searchAnswer([])), []);
  assert.deepEqual(parseTicketList({ id: 5, message: "Success", data: { ticketList: [], totalRecords: 0 } }), []);
  assert.deepEqual(parseTicketList(null), []);
  assert.deepEqual(parseTicketList("<html>"), []);
  // A bare list is read too.
  assert.deepEqual(parseTicketList([row(7, "WO-012345678")]), [{ id: 7, bpId: 9, ticketNo: "WO-012345678" }]);
});

test("the search is a contains-match: only the exact work order is it", () => {
  const list = parseTicketList(
    searchAnswer([row(1000002, "WO-012345678-1"), row(1000001, "WO-012345678"), row(1000009, "WO-0123456789")]),
  );
  assert.equal(pickTicket(list, "WO-012345678").id, 1000001);
  assert.equal(pickTicket(list, "wo 012345678").id, 1000001);
  assert.equal(pickTicket(list, "WO-012345678-1").id, 1000002);
  assert.equal(pickTicket(list, "WO-012345678-2"), null);
  // Only a neighbour came back: FieldEZ has no such work order.
  const neighbours = parseTicketList(searchAnswer([row(1000002, "WO-012345678-1"), row(1000009, "WO-0123456789")]));
  assert.equal(pickTicket(neighbours, "WO-012345678"), null);
  assert.equal(pickTicket([], "WO-012345678"), null);
  assert.equal(pickTicket(null, "WO-012345678"), null);
  assert.equal(pickTicket(list, "not a work order"), null);
});

test("two different tickets claiming one work order is no answer; one ticket twice is", () => {
  const twice = [TICKET, { ...TICKET, ticketNo: "WO012345678" }];
  assert.equal(pickTicket(twice, "WO-012345678"), TICKET);
  const two = [TICKET, { ...TICKET, id: 1000002 }];
  assert.equal(pickTicket(two, "WO-012345678"), null);
});

test("Problem Resolution: its place, its time, who, and the address", () => {
  const list = problemResolutions([
    activity("Call Logged", { latitude: 0, longitude: 0, syncTime: SYNC - 86_400_000 }),
    activity("Engineer Reached", { syncTime: SYNC - 3_600_000 }),
    activity("Problem Resolution"),
  ]);
  assert.deepEqual(list, [PLACE]);
  // syncTime is the time FieldEZ's own screen shows for the status.
  assert.equal(new Date(list[0].at).toLocaleString("en-IN", { timeZone: "Asia/Kolkata", hour12: false }), "12/10/2026, 10:57:55");
});

test("only exactly Problem Resolution -- not FCT Validate, not Buffer", () => {
  const list = problemResolutions([
    activity("Problem Resolution FCT Validate", { latitude: 0, longitude: 0, updatedBy: "Office Desk" }),
    activity("Problem Resolution FCT Validate"), // even if it had a place
    activity("Problem Resolution (Buffer)"),
    activity("Pre Problem Resolution"),
    activity("Problem  Resolution"),
    activity("ProblemResolution"),
    activity(null),
    activity(42),
    // Case and the spaces around it do not matter.
    activity("  problem RESOLUTION  ", { syncTime: SYNC + 60_000 }),
  ]);
  assert.equal(list.length, 1);
  assert.equal(list[0].at, new Date(SYNC + 60_000).toISOString());
});

test("0, 0 is FieldEZ's no-location; so is one 0, or a place that is missing or impossible", () => {
  const nowhere = problemResolutions(
    [
      { latitude: 0, longitude: 0 },
      { latitude: 0 },
      { longitude: 0 },
      { latitude: null },
      { longitude: undefined },
      { latitude: "" },
      { latitude: "abc" },
      { latitude: true },
      { latitude: NaN },
      { longitude: Infinity },
      { latitude: 90.0001 },
      { latitude: -91 },
      { longitude: 180.5 },
      { longitude: -181 },
    ].map((place) => activity("Problem Resolution", place)),
  );
  assert.deepEqual(nowhere, []);
  // A number written as text is still the number; the edges of the range are places.
  const edge = problemResolutions([activity("Problem Resolution", { latitude: " -90 ", longitude: "180" })]);
  assert.deepEqual([edge[0].latitude, edge[0].longitude], [-90, 180]);
});

test("the time: syncTime, else createTime; seconds read as seconds; no time, left out", () => {
  const at = (extra) => problemResolutions([activity("Problem Resolution", extra)]).map((r) => r.at);
  assert.deepEqual(at({ syncTime: null }), [new Date(SYNC + 1000).toISOString()]);
  assert.deepEqual(at({ syncTime: 0 }), [new Date(SYNC + 1000).toISOString()]);
  assert.deepEqual(at({ syncTime: undefined }), [new Date(SYNC + 1000).toISOString()]);
  assert.deepEqual(at({ syncTime: String(SYNC) }), ["2026-10-12T05:27:55.512Z"]);
  assert.deepEqual(at({ syncTime: Math.floor(SYNC / 1000) }), ["2026-10-12T05:27:55.000Z"]);
  assert.deepEqual(at({ syncTime: null, createTime: null }), []);
  assert.deepEqual(at({ syncTime: -5, createTime: "soon" }), []);
  // A number no clock wrote would be a year the payroll cannot read.
  assert.deepEqual(at({ syncTime: 1e20, createTime: 9e15 }), []);
  assert.deepEqual(at({ syncTime: "2026-10-12 10:57:55", createTime: undefined }), []);
});

test("who and where: FieldEZ's blanks are blanks", () => {
  const [blank] = problemResolutions([activity("Problem Resolution", { updatedBy: null, locationAddress: "null" })]);
  assert.equal(blank.user, "");
  assert.equal(blank.address, "");
  const [dash] = problemResolutions([activity("Problem Resolution", { updatedBy: "  -  ", locationAddress: { street: "x" } })]);
  assert.equal(dash.user, "");
  assert.equal(dash.address, "");
  const [trimmed] = problemResolutions([activity("Problem Resolution", { updatedBy: "  Test Engineer ", locationAddress: " 1 Test Street " })]);
  assert.equal(trimmed.user, "Test Engineer");
  assert.equal(trimmed.address, "1 Test Street");
});

test("several: oldest first, and past 50 the latest are kept", () => {
  // Newest first, the way a list can come.
  const many = Array.from({ length: 60 }, (_, i) => activity("Problem Resolution", { syncTime: SYNC - i * 60_000 }));
  const list = problemResolutions(many);
  assert.equal(list.length, 50);
  assert.equal(list[0].at, new Date(SYNC - 49 * 60_000).toISOString());
  assert.equal(list[49].at, new Date(SYNC).toISOString());
  assert.ok(list.every((r, i) => i === 0 || list[i - 1].at <= r.at));
});

test("not an activity list is no Problem Resolution", () => {
  for (const notAList of [null, undefined, {}, { data: [activity("Problem Resolution")] }, "Problem Resolution"]) {
    assert.deepEqual(problemResolutions(notAList), []);
  }
  assert.deepEqual(problemResolutions([null, "x", 3, activity("Problem Resolution")]), [PLACE]);
});

test("found: every Problem Resolution with a place", () => {
  assert.deepEqual(resultFor("wo012345678", TICKET, [activity("Problem Resolution")], null), {
    wo: "WO-012345678",
    status: "found",
    fieldez_ticket_id: "1000001",
    resolutions: [PLACE],
    error: "",
  });
});

test("no_resolution: none at all, or none with a place", () => {
  const expected = { wo: "WO-012345678", status: "no_resolution", fieldez_ticket_id: "1000001", resolutions: [], error: "" };
  assert.deepEqual(resultFor("WO-012345678", TICKET, [], null), expected);
  assert.deepEqual(
    resultFor(
      "WO-012345678",
      TICKET,
      [activity("Problem Resolution FCT Validate", { latitude: 0, longitude: 0 }), activity("Problem Resolution", { latitude: 0, longitude: 0 })],
      null,
    ),
    expected,
  );
});

test("not_found: no ticket with exactly this work order", () => {
  assert.deepEqual(resultFor("WO-012345678", null, null, null), {
    wo: "WO-012345678",
    status: "not_found",
    fieldez_ticket_id: "",
    resolutions: [],
    error: "",
  });
});

test("failed: why, in one short line -- never Playwright's call log", () => {
  const timeout = new Error(
    "apiRequestContext.get: Timeout 45000ms exceeded.\nCall log:\n  - → GET https://fieldez.example/tlm/v1.0/ticket-activity/ticket/1000001\n" +
      "    - authorization: Bearer SECRET-TOKEN",
  );
  const failed = resultFor("WO-012345678", TICKET, null, timeout);
  assert.deepEqual(failed, {
    wo: "WO-012345678",
    status: "failed",
    fieldez_ticket_id: "1000001",
    resolutions: [],
    error: "apiRequestContext.get: Timeout 45000ms exceeded.",
  });
  assert.ok(!JSON.stringify(failed).includes("SECRET"));
  // Before the ticket was known there is no ticket id.
  assert.equal(resultFor("WO-012345678", null, null, new Error("search answered 500")).fieldez_ticket_id, "");
  // The payroll's column holds 200.
  assert.equal(resultFor("WO-012345678", null, null, new Error("x".repeat(500))).error.length, 200);
  assert.equal(resultFor("WO-012345678", null, null, "search answered 502").error, "search answered 502");
  assert.equal(resultFor("WO-012345678", null, null, new Error("")).error, "could not be read");
  // No activity list is a failure, never "no Problem Resolution".
  for (const notAList of [undefined, null, { data: [] }]) {
    assert.deepEqual(resultFor("WO-012345678", TICKET, notAList, null), {
      wo: "WO-012345678",
      status: "failed",
      fieldez_ticket_id: "1000001",
      resolutions: [],
      error: "no activity list for the ticket",
    });
  }
});

test("what goes to the payroll passes the checks it makes", () => {
  const results = [
    resultFor("WO-012345678", TICKET, [activity("Problem Resolution"), activity("Problem Resolution", { syncTime: SYNC + 5000 })], null),
    resultFor("WO-012345678-1", TICKET, [], null),
    resultFor("WO-012345679", null, null, null),
    resultFor("WO-012345680", TICKET, null, new Error("activities answered 500")),
  ];
  assert.deepEqual(results.map((r) => r.status), ["found", "no_resolution", "not_found", "failed"]);
  for (const r of results) {
    assert.deepEqual(Object.keys(r).sort(), ["error", "fieldez_ticket_id", "resolutions", "status", "wo"]);
    assert.equal(woKey(r.wo), r.wo);
    assert.equal(typeof r.fieldez_ticket_id, "string");
    assert.ok(r.fieldez_ticket_id.length <= 32);
    assert.ok(r.error.length <= 200);
    assert.ok(r.resolutions.length <= 50);
    for (const place of r.resolutions) {
      assert.deepEqual(Object.keys(place).sort(), ["address", "at", "latitude", "longitude", "user"]);
      // Aware ISO-8601, as the payroll's parser wants it.
      assert.match(place.at, /^\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}\.\d{3}Z$/);
      assert.ok(Number.isFinite(place.latitude) && place.latitude !== 0 && Math.abs(place.latitude) <= 90);
      assert.ok(Number.isFinite(place.longitude) && place.longitude !== 0 && Math.abs(place.longitude) <= 180);
      assert.equal(typeof place.user, "string");
      assert.equal(typeof place.address, "string");
    }
  }
  // And it survives the trip as JSON unchanged.
  assert.deepEqual(JSON.parse(JSON.stringify(results)), results);
});
