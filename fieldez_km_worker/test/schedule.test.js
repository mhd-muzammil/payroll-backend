import assert from "node:assert/strict";
import path from "node:path";
import test from "node:test";

// The defaults, whatever a .env on this computer says: a key already in the
// environment, even empty, is one config.js does not take from a .env.
const SETTINGS = [
  "PAYROLL_API_URL",
  "PAYROLL_USERNAME",
  "PAYROLL_PASSWORD",
  "WANTED_DAYS",
  "RUN_EVERY_MINUTES",
  "RUN_FROM",
  "RUN_TO",
  "PAUSE_MS",
  "MAX_PER_PASS",
  // What src/probe.js reads, which must read as it did.
  "FIELDEZ_BASE_URL",
  "FIELDEZ_KM_USERNAME",
  "FIELDEZ_KM_PASSWORD",
  "HEADLESS",
  "BROWSER_CHANNEL",
  "DATA_DIR",
  "LOGIN_WAIT_MS",
];
for (const name of SETTINGS) process.env[name] = "";

const { config } = await import("../src/config.js");
const { inWindow, istMinutes, minutesOf } = await import("../src/schedule.js");

test("the worker's defaults", () => {
  assert.equal(config.payrollApiUrl, "https://payrollback.systimus.in");
  assert.equal(config.payrollUsername, "");
  assert.equal(config.payrollPassword, "");
  assert.equal(config.wantedDays, 31);
  assert.equal(config.runEveryMinutes, 30);
  assert.equal(config.runFrom, "08:00");
  assert.equal(config.runTo, "22:30");
  assert.equal(config.pauseMs, 1500);
  assert.equal(config.maxPerPass, 200);
});

test("what the probe reads is as it was", () => {
  const dataDir = process.platform === "win32" ? "C:\\fzkm" : "/data";
  assert.equal(config.fieldezBaseUrl, "https://prod.fsmsupport.com");
  assert.equal(config.fieldezUsername, "");
  assert.equal(config.fieldezPassword, "");
  assert.equal(config.headless, false);
  assert.equal(config.browserChannel, "chrome");
  assert.equal(config.dataDir, dataDir);
  assert.equal(config.profileDir, path.join(dataDir, "fieldez-km-profile"));
  assert.equal(config.outDir, path.join(dataDir, "out"));
  assert.equal(config.loginWaitMs, 600_000);
});

test("settings from the environment; a number that is not one is the default", async () => {
  Object.assign(process.env, {
    PAYROLL_API_URL: "http://127.0.0.1:9//",
    WANTED_DAYS: "60",
    RUN_EVERY_MINUTES: "15",
    RUN_FROM: "09:15",
    RUN_TO: "21:00",
    PAUSE_MS: "0",
    MAX_PER_PASS: "abc",
  });
  // The same file read afresh, with this environment.
  const { config: set } = await import("../src/config.js?settings");
  assert.equal(set.payrollApiUrl, "http://127.0.0.1:9");
  assert.equal(set.wantedDays, 31); // the most the payroll gives
  assert.equal(set.runEveryMinutes, 15);
  assert.equal(set.runFrom, "09:15");
  assert.equal(set.runTo, "21:00");
  assert.equal(set.pauseMs, 0);
  assert.equal(set.maxPerPass, 200);

  Object.assign(process.env, { WANTED_DAYS: "0", RUN_EVERY_MINUTES: "-5", PAUSE_MS: "-1", MAX_PER_PASS: "2.5" });
  const { config: bad } = await import("../src/config.js?bad");
  assert.equal(bad.wantedDays, 31);
  assert.equal(bad.runEveryMinutes, 30);
  assert.equal(bad.pauseMs, 1500);
  assert.equal(bad.maxPerPass, 200);
});

test("RUN_FROM / RUN_TO: HH:MM, and nothing else", () => {
  assert.equal(minutesOf("08:00"), 480);
  assert.equal(minutesOf("22:30"), 1350);
  assert.equal(minutesOf(" 00:00 "), 0);
  assert.equal(minutesOf("23:59"), 1439);
  for (const bad of ["8:00", "24:00", "08:60", "0800", "", "noon", undefined]) {
    assert.throws(() => minutesOf(bad, "RUN_FROM"), /^Error: RUN_FROM must be a time like 08:00/);
  }
});

test("the office's clock, whatever this computer's says", () => {
  assert.equal(istMinutes(new Date("2026-10-14T02:30:00Z")), 480); // 08:00 IST
  assert.equal(istMinutes(new Date("2026-10-14T17:00:00Z")), 1350); // 22:30 IST
  assert.equal(istMinutes(new Date("2026-10-14T18:29:00Z")), 1439); // 23:59 IST
  assert.equal(istMinutes(new Date("2026-10-14T18:30:00Z")), 0); // midnight: 00, never 24
});

test("08:00 up to, not including, 22:30", () => {
  const [from, to] = [minutesOf("08:00"), minutesOf("22:30")];
  assert.equal(inWindow(0, from, to), false);
  assert.equal(inWindow(479, from, to), false);
  assert.equal(inWindow(480, from, to), true);
  assert.equal(inWindow(1349, from, to), true);
  assert.equal(inWindow(1350, from, to), false);
});

test("a window across midnight works, and from == to is the whole day", () => {
  const [from, to] = [minutesOf("22:00"), minutesOf("06:00")];
  assert.equal(inWindow(1320, from, to), true);
  assert.equal(inWindow(30, from, to), true);
  assert.equal(inWindow(359, from, to), true);
  assert.equal(inWindow(360, from, to), false);
  assert.equal(inWindow(720, from, to), false);
  assert.equal(inWindow(0, 600, 600), true);
  assert.equal(inWindow(1439, 600, 600), true);
});
