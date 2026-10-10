/**
 * Everything the poster is told from outside, in one place.
 *
 * Set in Dokploy's environment for this app. Nothing here has a default that
 * could post somewhere real by accident: the groups default to the five the
 * office named, but TEST_CHAT redirects every post to one chat while it is
 * being tried out, and DRY_RUN sends nothing at all.
 */
import fs from "node:fs";
import path from "node:path";

// A local .env, for running it on a laptop -- typed into the file by whoever
// runs it, so a password never has to pass through a chat. On Dokploy the real
// environment is set in the app and wins over anything here.
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

/** Which group each branch's image goes to. Names must match WhatsApp exactly
 *  (spacing and case are forgiven when the chat is looked up). */
const DEFAULT_GROUPS = {
  Chennai: "RENDERWAYS-CHENNAI-ATTENDANCE",
  Salem: "RENDERWAYS-SALEM-ATTENDANCE",
  Kanchipuram: "RENDERWAYS- KANCHIPURAM-ATTENDANCE",
  Vellore: "RENDERWAYS-VELLORE-ATTENDANCE",
  Hosur: "RENDERWAYS-HOSUR-ATTENDANCE",
};

// WA_GROUPS, not GROUPS: bash keeps a read-only GROUPS of its own and silently
// ignores an assignment to it, so a test run quietly used the defaults.
const parseGroups = () => {
  if (!env.WA_GROUPS) return DEFAULT_GROUPS;
  try {
    const parsed = JSON.parse(env.WA_GROUPS);
    if (parsed && typeof parsed === "object") return parsed;
  } catch {
    // Fall through to the error below -- a typo here must not quietly post
    // every branch to the default groups.
  }
  throw new Error('WA_GROUPS must be JSON, e.g. {"Salem":"RENDERWAYS-SALEM-ATTENDANCE"}');
};

const DATA_DIR = env.DATA_DIR || "/data";

// "09:10, 09:35" -> ["09:10", "09:35"]. A time that is not HH:MM stops the
// poster at start rather than quietly never firing.
const times = (value, fallback) => {
  const list = (value ?? fallback).split(",").map((t) => t.trim()).filter(Boolean);
  for (const t of list) {
    if (!/^([01]\d|2[0-3]):[0-5]\d$/.test(t)) throw new Error(`"${t}" is not a time like 09:10`);
  }
  return list;
};

export const config = {
  // The site, for POST_AS=image (the picture is made by its own button), and
  // the API the text is read from.
  payrollUrl: (env.PAYROLL_URL || "https://payroll.systimus.in").replace(/\/+$/, ""),
  payrollApiUrl: (env.PAYROLL_API_URL || "https://payrollback.systimus.in").replace(/\/+$/, ""),
  payrollUsername: env.PAYROLL_USERNAME || "",
  payrollPassword: env.PAYROLL_PASSWORD || "",

  // The number the poster's WhatsApp account belongs to, digits only with the
  // country code: 91XXXXXXXXXX. Only used to ask WhatsApp for a link code.
  waPhone: (env.WA_PHONE || "").replace(/\D/g, ""),

  groups: parseGroups(),
  // "text": the day's register as a message (the default -- the office asked
  // for text). "image": the Attendance page's "Today (Image)" picture.
  postAs: /^\s*image\s*$/i.test(env.POST_AS || "") ? "image" : "text",
  // IST wall-clock times, HH:MM, comma separated. The office's day:
  //   09:10 report, 09:11 reminder, 09:35 report, 09:37 reminder,
  //   (10:00 the backend marks the no-shows Absent), 10:05 final report,
  //   19:00 report, 21:00 report.
  runTimes: times(env.RUN_TIMES, "09:10,09:35,19:00,21:00"),
  // Who has not logged in yet, named, in each branch's group.
  reminderTimes: times(env.REMINDER_TIMES, "09:11,09:37"),
  // The report after 10am: Absent first, then on leave, then present.
  finalTimes: times(env.FINAL_TIMES, "10:05"),

  // While trying it out: every post goes to this one chat instead of the
  // branch groups ("Message yourself" works -- type your own name).
  testChat: env.TEST_CHAT || "",
  dryRun: /^(1|true|yes)$/i.test(env.DRY_RUN || ""),
  headless: !/^(0|false|no)$/i.test(env.HEADLESS || "true"),

  dataDir: DATA_DIR,
  profileDir: path.join(DATA_DIR, "wa-profile"),
  outDir: path.join(DATA_DIR, "out"),

  // The status page. Off unless a token is set: it can show the WhatsApp
  // screen, which is the QR code before linking and the chats after.
  port: Number(env.PORT || 3000),
  statusToken: env.STATUS_TOKEN || "",
};

export const missingForPosting = () => {
  const missing = [];
  if (!config.payrollUsername) missing.push("PAYROLL_USERNAME");
  if (!config.payrollPassword) missing.push("PAYROLL_PASSWORD");
  return missing;
};
