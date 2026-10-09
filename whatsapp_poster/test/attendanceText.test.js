import assert from "node:assert/strict";
import test from "node:test";

import { buildMessage, formatTime, hoursBetween, todaysRows, workedSpan } from "../src/attendanceText.js";

// The API writes times in India time, "+05:30".
const at = (hhmm, day = "2026-10-09") => `${day}T${hhmm}:00+05:30`;

test("hours follow the office rule: whole hours, plus the minutes of both punches", () => {
  assert.equal(workedSpan(at("09:20"), at("21:20")), "12h 40m");
  assert.equal(workedSpan(at("09:07"), at("21:25")), "12h 32m");
});

test("the same moments written in UTC read the same", () => {
  // 09:20 IST is 03:50 UTC; 09:08 IST is 03:38 UTC.
  assert.equal(workedSpan("2026-10-09T03:50:00Z", "2026-10-09T15:50:00Z"), "12h 40m");
  assert.equal(formatTime("2026-10-09T03:38:00Z"), "09:08 AM");
});

test("a day that cannot be measured is a dash, never 0h", () => {
  assert.equal(workedSpan(at("09:00"), null), "—");
  assert.equal(workedSpan(at("18:00"), at("09:00")), "—");
  assert.equal(hoursBetween(at("09:00"), at("09:00", "2026-10-10")), null);
});

test("today's rows: this branch, this day, in the page's order", () => {
  const records = [
    { id: 1, employee_id: 1, employee_name: "Vijayananth M", branch: "Hosur", status: "Present", intime: at("10:42") },
    { id: 2, employee_id: 2, employee_name: "vignesh raja", branch: "Hosur", status: "Present", intime: at("09:06") },
    { id: 3, employee_id: 3, employee_name: "Jaikumar J", branch: "Hosur", status: "Present", intime: at("09:03", "2026-10-08") },
    { id: 4, employee_id: 4, employee_name: "Agalya A", branch: "Vellore", status: "Present", intime: at("09:12") },
  ];
  // Small letters are not sorted after capitals.
  assert.deepEqual(todaysRows(records, "Hosur", "2026-10-09").map((r) => r.id), [2, 1]);
});

test("a hand-marked row with no employee is posted to that person's branch", () => {
  const records = [
    { id: 1, employee_id: 7, employee_name: "Perumal V", branch: "Salem", status: "Present", intime: at("09:00", "2026-10-08") },
    { id: 2, employee_id: null, employee_name: "Perumal V", branch: "Chennai", status: "Absent", intime: at("00:00") },
  ];
  assert.deepEqual(todaysRows(records, "Salem", "2026-10-09").map((r) => r.id), [2]);
  assert.deepEqual(todaysRows(records, "Chennai", "2026-10-09"), []);
});

test("the message", () => {
  const rows = [
    { employee_name: "Lava kumar  V ", department: "General", status: "Present", intime: at("09:20"), outtime: at("21:20") },
    { employee_name: "Perumal V", department: "", status: "Absent", intime: at("00:00"), outtime: null },
    { employee_name: "Srinivasan", department: "Backend", status: "Leave", intime: at("00:00"), outtime: null },
  ];
  const now = new Date("2026-10-09T05:39:00Z"); // 11:09 am in India

  assert.equal(
    buildMessage({ rows, branch: "Salem", now }),
    [
      "*Renderways Technology*",
      "Daily Attendance · *Salem branch*",
      "Friday, 09 October 2026",
      "Present 1 · Absent 1 · On leave 1 · 3 records",
      "",
      "*Lava kumar V* — Present",
      "General · In 09:20 AM · Out 09:20 PM · Hours 12h 40m",
      "",
      // Midnight is only the date of a day with no punch: a dash, not 12:00 AM.
      "*Perumal V* — Absent",
      "— · In — · Out — · Hours —",
      "",
      "*Srinivasan* — Leave",
      "Backend · In — · Out — · Hours —",
      "",
      "_Generated 9 Oct 2026, 11:09 am_",
    ].join("\n"),
  );
});

test("one person is one record", () => {
  const rows = [{ employee_name: "Agalya A", department: "General", status: "Present", intime: at("09:12") }];
  const text = buildMessage({ rows, branch: "Vellore", now: new Date("2026-10-09T05:39:00Z") });
  assert.match(text, /Present 1 · Absent 0 · On leave 0 · 1 record\n/);
});
