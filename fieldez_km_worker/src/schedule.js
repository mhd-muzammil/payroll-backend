/**
 * When `npm start` may run a pass: only between RUN_FROM and RUN_TO on the
 * office's clock (IST), whatever the computer's own clock is set to.
 *
 * Apart from index.js so it is tested on its own, as the WhatsApp poster's
 * slots.js is: index.js runs the moment it is imported.
 */

/** "08:00" -> 480. Anything that is not HH:MM stops the worker at start, rather than quietly never running. */
export function minutesOf(hhmm, name = "time") {
  const match = /^([01]\d|2[0-3]):([0-5]\d)$/.exec(String(hhmm ?? "").trim());
  if (!match) throw new Error(`${name} must be a time like 08:00, not "${hhmm}"`);
  return Number(match[1]) * 60 + Number(match[2]);
}

/** Minutes since midnight in India. */
export function istMinutes(date = new Date()) {
  const parts = Object.fromEntries(
    new Intl.DateTimeFormat("en-GB", { timeZone: "Asia/Kolkata", hour: "2-digit", minute: "2-digit", hourCycle: "h23" })
      .formatToParts(date)
      .map((p) => [p.type, p.value]),
  );
  return Number(parts.hour) * 60 + Number(parts.minute);
}

/**
 * Inside [from, to): 08:00 up to, not including, 22:30. A window across
 * midnight (22:00-06:00) works too, and from == to is the whole day.
 */
export function inWindow(minutes, from, to) {
  if (from === to) return true;
  return from < to ? minutes >= from && minutes < to : minutes >= from || minutes < to;
}
