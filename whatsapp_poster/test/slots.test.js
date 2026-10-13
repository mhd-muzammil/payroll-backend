import assert from "node:assert/strict";
import test from "node:test";

import { config } from "../src/config.js";
import { slotsOfTheDay } from "../src/slots.js";

test("the office's day, in order", () => {
  // The defaults, as the office set them on 12 Oct 2026 -- with the logout
  // reminder moved from 19:15 to 20:30 on 13 Oct. (A test run has no RUN_TIMES
  // & co in its environment, unless somebody's shell sets them.)
  const day = slotsOfTheDay(config).map((s) => `${s.label} ${s.kind}`);
  assert.deepEqual(day, [
    "09:10 report",
    "09:13 reminder",
    "09:35 report",
    "09:38 reminder",
    "10:00 report",
    "10:05 reminder",
    "10:30 mark",
    "10:30 final",
    "19:00 report",
    "20:30 logout",
    "21:00 final",
  ]);
});

test("on the same minute the marking always runs before the final report", () => {
  const slots = slotsOfTheDay({
    runTimes: [],
    reminderTimes: [],
    // Listed final-first on purpose: the order must not depend on it.
    finalTimes: ["10:30"],
    markTimes: ["10:30"],
    logoutTimes: [],
  });
  assert.deepEqual(slots.map((s) => s.kind), ["mark", "final"]);
});
