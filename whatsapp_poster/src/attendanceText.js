/**
 * Today's attendance for one branch, as a WhatsApp message.
 *
 * It says what the Attendance page's "Today (Image)" picture says -- the same
 * people, in the same order, with the same times, hours and totals -- as text.
 * The office knows that picture, so every helper here is a copy of the page's
 * own (payroll_frontend: Attendance.jsx handleExportTodayImage,
 * Utility/attendanceUtils.js, Utility/attendanceDayImage.js). Change one of
 * those and this has to change with it.
 *
 * One difference, on purpose. A day with no punch -- Absent, Leave -- is
 * stored with its time at midnight, and the picture prints that as "12:00 AM".
 * The register on the page shows a dash for it, and so does this: nobody
 * clocked in at midnight.
 *
 * Read in India time whatever the server's clock is set to. The picture is
 * drawn in an office browser in IST, and the office's rule for hours adds up
 * the minutes as that clock shows them.
 */

const ZONE = "Asia/Kolkata";
// +05:30 all year: India has no daylight saving.
const IST_OFFSET_MS = 330 * 60_000;
const MS_PER_HOUR = 60 * 60_000;
const MAX_WORKDAY_HOURS = 24;

// A day nobody came in has no punch; its midnight is only the date.
const NO_PUNCH_STATUSES = new Set(["Absent", "Leave", "Half Day"]);
const STATUS_DISPLAY = { overTime: "Overtime" };

const CLOCK = new Intl.DateTimeFormat("en-US", {
  hour: "2-digit",
  minute: "2-digit",
  hour12: true,
  timeZone: ZONE,
});
const DAY = new Intl.DateTimeFormat("en-IN", {
  weekday: "long",
  day: "2-digit",
  month: "long",
  year: "numeric",
  timeZone: ZONE,
});
const STAMP = new Intl.DateTimeFormat("en-IN", { dateStyle: "medium", timeStyle: "short", timeZone: ZONE });
const DATE = new Intl.DateTimeFormat("en-CA", { year: "numeric", month: "2-digit", day: "2-digit", timeZone: ZONE });

// Newer ICU puts a narrow no-break space before AM/PM. It is a space.
const plainSpaces = (s) => s.replace(/\s+/g, " ").trim();

/** "2026-10-09": the date in India at this moment. */
export const indianDate = (at = new Date()) => DATE.format(at);

const getDatePart = (value) => (value ? String(value).slice(0, 10) : "");

/** "09:08 AM", or a dash when there is no time. */
export const formatTime = (value) => {
  if (!value) return "—";
  const at = new Date(value);
  if (Number.isNaN(at.getTime())) return "—";
  return plainSpaces(CLOCK.format(at));
};

/**
 * Whether a record holds only a date -- an Absent or Leave day stored at
 * midnight that nobody punched. Not the status alone: somebody marked Absent
 * at 10:30 who logs in later has their Login recorded on that row, and the day
 * stays Absent. (isDayMark in the page's attendanceUtils.js.)
 */
const isDayMark = (record) => {
  if (!record?.intime || !NO_PUNCH_STATUSES.has(record.status)) return false;
  const at = new Date(record.intime);
  if (Number.isNaN(at.getTime())) return false;
  return formatTime(record.intime) === "12:00 AM";
};

/** A clock time for the register: a dash on a day nobody punched. */
export const punchTime = (record, field) => (isDayMark(record) ? "—" : formatTime(record?.[field]));

/**
 * How long the day was, by the office's rule -- or null, when the day cannot
 * be measured. Whole hours between the two hour marks, plus the minutes of
 * BOTH punches: 9:20 am to 9:20 pm is 12h 40m. See hoursBetween in the page's
 * attendanceUtils.js for why; it is the office's decision, not a bug.
 */
export const hoursBetween = (intime, outtime) => {
  if (!intime || !outtime) return null;

  const start = new Date(intime).getTime();
  const end = new Date(outtime).getTime();
  if (Number.isNaN(start) || Number.isNaN(end)) return null;

  const elapsed = (end - start) / MS_PER_HOUR;
  if (!(elapsed > 0 && elapsed < MAX_WORKDAY_HOURS)) return null;

  // Read on India's clock, as the page reads them in an office browser.
  const hourMark = (ms) => Math.floor((ms + IST_OFFSET_MS) / MS_PER_HOUR);
  const minuteOf = (ms) => new Date(ms + IST_OFFSET_MS).getUTCMinutes();
  return hourMark(end) - hourMark(start) + (minuteOf(start) + minuteOf(end)) / 60;
};

/** "4h 36m"; a part that is zero is left out. */
export const formatDuration = (hours) => {
  const value = Number(hours);
  if (!Number.isFinite(value) || value <= 0) return "0h";

  const minutes = Math.round(value * 60);
  const wholeHours = Math.floor(minutes / 60);
  const restMinutes = minutes % 60;

  if (!wholeHours) return `${restMinutes}m`;
  if (!restMinutes) return `${wholeHours}h`;
  return `${wholeHours}h ${restMinutes}m`;
};

/** One day's length, or a dash when there is no answer. */
export const workedSpan = (intime, outtime) => {
  const hours = hoursBetween(intime, outtime);
  return hours === null ? "—" : formatDuration(hours);
};

const getStatusDisplay = (status) => STATUS_DISPLAY[status] || status;

/**
 * A row the office marked by hand can arrive with no employee id and the
 * branch "Chennai" whoever it is about; it gets the id and branch of the one
 * employee with that name. A copy of linkOrphanRows in attendanceUtils.js --
 * without it a Salem person's hand-marked day would be posted to Chennai.
 */
export const linkOrphanRows = (records) => {
  const list = Array.isArray(records) ? records : [];
  const owners = new Map();
  for (const record of list) {
    if (record.employee_id == null) continue;
    const name = String(record.employee_name || "").trim().toLowerCase();
    if (!name) continue;
    let known = owners.get(name);
    if (!known) owners.set(name, (known = new Map()));
    if (!known.has(record.employee_id)) known.set(record.employee_id, record);
  }
  if (owners.size === 0) return list;

  return list.map((record) => {
    if (record.employee_id != null) return record;
    const name = String(record.employee_name || "").trim().toLowerCase();
    const known = owners.get(name);
    if (!known || known.size !== 1) return record;
    const owner = [...known.values()][0];
    return {
      ...record,
      employee_id: owner.employee_id,
      branch: owner.branch || record.branch,
      email: record.email || owner.email || null,
    };
  });
};

/** The rows the picture would show for this branch today, in its order. */
export const todaysRows = (records, branch, today = indianDate()) =>
  linkOrphanRows(records)
    .filter((record) => getDatePart(record.intime || record.outtime) === today)
    .filter((record) => (record.branch || "Chennai").toLowerCase() === branch.toLowerCase())
    // "en" rather than the process's own locale: a server with no LANG set
    // sorts every capital before every small letter, and the page does not.
    .sort((a, b) => String(a.employee_name || "").localeCompare(String(b.employee_name || ""), "en"));

// Names come in as typed -- "Lava kumar  V", or with a space at the end, which
// would stop WhatsApp making the name bold.
const tidy = (value) => String(value ?? "").replace(/\s+/g, " ").trim();

/**
 * The message:
 *
 *     *Renderways Technology*
 *     Daily Attendance · *Salem branch*
 *     Friday, 09 October 2026
 *     Present 4 · Absent 0 · On leave 0 · 4 records
 *
 *     *Jayakumar S* — Present
 *     General · In 09:08 AM · Out — · Hours —
 *     ...
 *
 *     _Generated 9 Oct 2026, 11:09 am_
 *
 * A line per fact rather than the picture's table: a table in a chat wraps
 * into nonsense on a phone. The picture's Branch column is left out because
 * every row in a branch's group is that branch.
 */
export const buildMessage = ({ rows, branch, now = new Date() }) => {
  const totals = rows.reduce(
    (acc, record) => {
      if (record.status === "Absent") acc.absent += 1;
      else if (record.status === "Half Day") acc.halfDay += 1;
      else if (record.status === "Leave") acc.leave += 1;
      else acc.present += 1;
      return acc;
    },
    { present: 0, absent: 0, halfDay: 0, leave: 0 },
  );

  const lines = [
    "*Renderways Technology*",
    `Daily Attendance · *${branch} branch*`,
    plainSpaces(DAY.format(now)),
    // Half Day only on a day that has one, as the page's picture says it.
    `Present ${totals.present} · Absent ${totals.absent} · ` +
      (totals.halfDay ? `Half Day ${totals.halfDay} · ` : "") +
      `On leave ${totals.leave} · ` +
      `${rows.length} ${rows.length === 1 ? "record" : "records"}`,
  ];

  for (const record of rows) {
    lines.push(
      "",
      `*${tidy(record.employee_name) || "—"}* — ${tidy(getStatusDisplay(record.status)) || "—"}`,
      [
        tidy(record.department) || "—",
        `In ${punchTime(record, "intime")}`,
        `Out ${punchTime(record, "outtime")}`,
        `Hours ${workedSpan(record.intime, record.outtime)}`,
      ].join(" · "),
    );
  }

  lines.push("", `_Generated ${plainSpaces(STAMP.format(now))}_`);
  return lines.join("\n");
};

const CLOCK_SHORT = new Intl.DateTimeFormat("en-IN", { timeStyle: "short", timeZone: ZONE });

/** "10:30" from the server -> "10:30 am", the way the rest of the message says times. */
const cutoffWords = (cutoff) => {
  const [h, m] = String(cutoff || "10:30").split(":").map(Number);
  const at = new Date(Date.UTC(2000, 0, 1, h, m) - IST_OFFSET_MS);
  return plainSpaces(CLOCK_SHORT.format(at));
};

// "• Karthik S", never "1. Karthik S": WhatsApp's message box turns a line
// that starts "1. " into a numbered list and starts the next line with "2. "
// itself, so the box no longer held what was typed and every reminder with
// two names or more was refused (12 Oct 2026). A bullet is just a character.
const listed = (names) => names.map((name) => `• ${tidy(name) || "—"}`);

/**
 * The login reminder (9:13, 9:38, 10:05): who in this branch has not logged in yet.
 *
 *     *Login reminder* · *Salem branch*
 *     Monday, 12 October 2026 · 9:13 am
 *
 *     Not logged in yet (2), please log in now:
 *     • Karthik S
 *     • Priya R
 *
 *     No login by 10:30 am = Absent.
 *
 * English, as the office chose. The names are the server's list -- the same
 * people it marks Absent at 10:30 if they still have not logged in.
 */
export const buildReminder = ({ branch, names, cutoff, now = new Date() }) =>
  [
    `*Login reminder* · *${branch} branch*`,
    `${plainSpaces(DAY.format(now))} · ${plainSpaces(CLOCK_SHORT.format(now))}`,
    "",
    `Not logged in yet (${names.length}), please log in now:`,
    ...listed(names),
    "",
    `No login by ${cutoffWords(cutoff)} = Absent.`,
  ].join("\n");

/**
 * The final report -- at 10:30, right after the no-shows are marked Absent,
 * and again at 9pm: the
 * count, then who is Absent, on a Half Day, on leave and present, in that
 * order. The Half Day line and list appear only on a day that has one.
 *
 * `notMarked` is anybody the server still lists as not logged in. After 10:30
 * it should be nobody -- they were marked Absent -- so a name there means the
 * marking did not happen, and the message says so rather than counting them
 * Absent when the register does not.
 */
export const buildFinalReport = ({ rows, branch, notMarked = [], now = new Date() }) => {
  const absent = rows.filter((r) => r.status === "Absent");
  // Half Day, marked by the office: half a day unpaid, and not a no-show.
  const halfDay = rows.filter((r) => r.status === "Half Day");
  const leave = rows.filter((r) => r.status === "Leave");
  const present = rows.filter((r) => !NO_PUNCH_STATUSES.has(r.status));

  const counts = [
    `Present ${present.length}`,
    `Absent ${absent.length}`,
    ...(halfDay.length ? [`Half Day ${halfDay.length}`] : []),
    `On leave ${leave.length}`,
    ...(notMarked.length ? [`Not marked ${notMarked.length}`] : []),
    `Total ${rows.length + notMarked.length}`,
  ];
  const lines = [
    "*Renderways Technology*",
    `Final Attendance · *${branch} branch*`,
    plainSpaces(DAY.format(now)),
    counts.join(" · "),
  ];

  // A Login after 10:30 is on the Absent row, and the day stays Absent.
  const lateLogin = (r) => (isDayMark(r) || !r.intime ? "" : ` — logged in ${formatTime(r.intime)}`);
  if (absent.length) {
    lines.push("", `*Absent (${absent.length})*`);
    absent.forEach((r) => lines.push(`• ${tidy(r.employee_name) || "—"}${lateLogin(r)}`));
  }
  if (notMarked.length) {
    lines.push("", `*Not logged in, not marked Absent yet (${notMarked.length})*`, ...listed(notMarked));
  }
  if (halfDay.length) {
    lines.push("", `*Half Day (${halfDay.length})*`);
    halfDay.forEach((r) => lines.push(`• ${tidy(r.employee_name) || "—"}${lateLogin(r)}`));
  }
  if (leave.length) {
    lines.push("", `*On leave (${leave.length})*`, ...listed(leave.map((r) => r.employee_name)));
  }
  if (present.length) {
    lines.push("", `*Present (${present.length})*`);
    for (const r of present) {
      const status = tidy(getStatusDisplay(r.status));
      const out = r.outtime ? ` · Out ${formatTime(r.outtime)}` : "";
      lines.push(
        `*${tidy(r.employee_name) || "—"}* — In ${formatTime(r.intime)}${out}` +
          (status && status !== "Present" ? ` · ${status}` : ""),
      );
    }
  }

  lines.push("", `_Generated ${plainSpaces(STAMP.format(now))}_`);
  return lines.join("\n");
};

/**
 * Of a branch's rows for today, who logged in and has not logged out: a real
 * Login (not a day-mark) with no logout. A Login after 10:30 counts too -- the
 * day is Absent, but they are out working, and Logout is what closes their
 * duty and their km.
 */
export const notLoggedOut = (rows) =>
  rows.filter((r) => r.intime && !isDayMark(r) && !r.outtime).map((r) => r.employee_name);

/**
 * The 7:15pm reminder: who in this branch logged in and has not logged out.
 *
 *     *Logout reminder* · *Chennai branch*
 *     Monday, 12 October 2026 · 7:15 pm
 *
 *     Not logged out yet (2), please log out:
 *     • Karthik S
 *     • Priya R
 */
export const buildLogoutReminder = ({ branch, names, now = new Date() }) =>
  [
    `*Logout reminder* · *${branch} branch*`,
    `${plainSpaces(DAY.format(now))} · ${plainSpaces(CLOCK_SHORT.format(now))}`,
    "",
    `Not logged out yet (${names.length}), please log out:`,
    ...listed(names),
  ].join("\n");
