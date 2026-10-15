/**
 * Everything the FieldEZ KM worker is told from outside, in one place.
 *
 * Its own browser profile and its own data folder, never the OpenCall
 * fieldez-worker's: that worker deletes its profile's lock files every time it
 * starts, so a second browser on the same profile would break it.
 */
import fs from "node:fs";
import path from "node:path";

// A local .env, for running it on a laptop -- typed into the file by whoever
// runs it, so a password never has to pass through a chat. A real environment
// wins over anything here.
try {
  const text = fs.readFileSync(new URL("../.env", import.meta.url), "utf8");
  for (const line of text.split(/\r?\n/)) {
    const match = line.match(/^\s*([A-Z_][A-Z0-9_]*)\s*=\s*(.*?)\s*$/);
    if (match && match[2] !== "" && !(match[1] in process.env)) process.env[match[1]] = match[2];
  }
} catch {
  // No .env: everything comes from the environment.
}

const env = process.env;

// Short on Windows on purpose: Chrome's profile database fails under a long
// path (the WhatsApp poster hit "database error" under the user folder).
const DATA_DIR = env.DATA_DIR || (process.platform === "win32" ? "C:\\fzkm" : "/data");

// A whole number from the environment, or the default when it is not one or
// is below the least allowed.
const whole = (value, fallback, least = 1) => {
  const n = Number(value);
  return value !== undefined && value !== "" && Number.isInteger(n) && n >= least ? n : fallback;
};

export const config = {
  fieldezBaseUrl: (env.FIELDEZ_BASE_URL || "https://prod.fsmsupport.com").replace(/\/+$/, ""),
  // Optional. Left empty, the worker waits for a person to log in in the
  // browser window it opens -- the password is then typed only into FieldEZ.
  fieldezUsername: env.FIELDEZ_KM_USERNAME || "",
  fieldezPassword: env.FIELDEZ_KM_PASSWORD || "",

  headless: /^(1|true|yes)$/i.test(env.HEADLESS || "false"),
  // The installed Chrome by default, so nothing has to be downloaded.
  browserChannel: env.BROWSER_CHANNEL || "chrome",

  dataDir: DATA_DIR,
  profileDir: path.join(DATA_DIR, "fieldez-km-profile"),
  outDir: path.join(DATA_DIR, "out"),

  // How long to wait for someone to log in by hand in the opened window.
  loginWaitMs: Number(env.LOGIN_WAIT_MS || 10 * 60_000),

  // The payroll: which work orders it wants read, and where the answers go.
  // An HR or admin login that sees every branch -- the endpoints are staff-only.
  payrollApiUrl: (env.PAYROLL_API_URL || "https://payrollback.systimus.in").replace(/\/+$/, ""),
  payrollUsername: env.PAYROLL_USERNAME || "",
  payrollPassword: env.PAYROLL_PASSWORD || "",
  // Check-ins of the last this many IST days are asked about (the payroll takes
  // 1 to 31). 31 by default: a check-in older than the payroll's 31 days can no
  // longer be read, so anything shorter leaves days it will never fill.
  wantedDays: Math.min(31, whole(env.WANTED_DAYS, 31)),

  // npm start: a pass every this many minutes, only between these IST times
  // (HH:MM; checked when it starts, so a typo stops it there).
  runEveryMinutes: whole(env.RUN_EVERY_MINUTES, 30),
  runFrom: env.RUN_FROM || "08:00",
  runTo: env.RUN_TO || "22:30",
  // Between two work orders, so FieldEZ sees a steady trickle, not a burst.
  pauseMs: whole(env.PAUSE_MS, 1500, 0),
  // The most work orders one pass reads (the payroll asks for at most 200).
  maxPerPass: whole(env.MAX_PER_PASS, 200),
};
