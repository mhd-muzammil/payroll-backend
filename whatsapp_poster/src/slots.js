/**
 * The day's slots, in the order they run.
 *
 * By time; and when two fall on the same minute, in KINDS order -- which puts
 * the 10:30 marking ahead of the 10:30 final report that has to show it.
 */

// The order of kinds sharing a minute.
export const KINDS = ["mark", "final", "report", "reminder", "logout"];

/** [{label: "10:30", kind: "mark", minutes: 630}, ...] from the configured times. */
export function slotsOfTheDay(config) {
  return [
    ...config.runTimes.map((label) => ({ label, kind: "report" })),
    ...config.reminderTimes.map((label) => ({ label, kind: "reminder" })),
    ...config.markTimes.map((label) => ({ label, kind: "mark" })),
    ...config.finalTimes.map((label) => ({ label, kind: "final" })),
    ...config.logoutTimes.map((label) => ({ label, kind: "logout" })),
  ]
    .map((slot) => {
      const [h, m] = slot.label.split(":").map(Number);
      return { ...slot, minutes: h * 60 + m };
    })
    .sort((a, b) => a.minutes - b.minutes || KINDS.indexOf(a.kind) - KINDS.indexOf(b.kind));
}
