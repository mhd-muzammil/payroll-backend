/** One line per event, with the IST time, so Dokploy's log reads as a diary. */
export const istNow = () =>
  new Date().toLocaleString("en-IN", { timeZone: "Asia/Kolkata", hour12: false });

export const log = (...parts) => console.log(`[${istNow()}]`, ...parts);
