/**
 * WhatsApp Web: staying linked, and posting a message or an image to a group.
 *
 * WhatsApp changes its page often and publishes no stable hooks, so every
 * element is looked for by several selectors in turn -- the first one visible
 * wins -- and every failure saves a screenshot, because "could not find the
 * attach button" means nothing without seeing what was on the screen instead.
 *
 * Linking is done once. The browser profile lives on a volume, so a restart
 * finds the session already there.
 */
import fs from "node:fs";
import path from "node:path";

import { config } from "./config.js";
import { log } from "./log.js";
import { status } from "./status.js";

const WA_URL = "https://web.whatsapp.com";

// Logged in: the chat list is on screen.
const CHAT_LIST = ["#pane-side", '[aria-label="Chat list"]', '[data-testid="chat-list"]', 'div[role="grid"]'];
// Not logged in: the QR code or the phone-number screen.
const LOGIN_SCREEN = ['[data-testid="link_device_qr_phone_number_link"]', "canvas[aria-label]", "div[data-ref] canvas"];

// The chat search. Its label and placeholder go away once something is typed
// in it -- which is how the second group of a run lost it -- so it is also
// found by where it is.
const SEARCH_BOX = [
  'input[aria-label="Search or start a new chat"]',
  '#side input[role="textbox"]',
  '#side input[data-tab="3"]',
  'div[contenteditable="true"][data-tab="3"]',
];
const ATTACH_BUTTON = [
  'button[aria-label="Attach"]',
  '[aria-label="Attach"]',
  'span[data-icon="plus-rounded"]',
  'span[data-icon="plus"]',
  'span[data-icon="attach-menu-plus"]',
  '[title="Attach"]',
];
const PHOTO_INPUT = 'input[type="file"][accept*="image"]';
const CAPTION_BOX = [
  'div[aria-label="Add a caption"]',
  'div[aria-placeholder="Add a caption"]',
  'div[contenteditable="true"][data-tab="10"]',
];
const SEND_BUTTON = [
  'div[aria-label="Send"]',
  'button[aria-label="Send"]',
  'span[data-icon="send"]',
  'span[data-icon="wds-ic-send-filled"]',
];
// The message box at the foot of an open chat.
const COMPOSE_BOX = [
  '#main footer div[contenteditable="true"][role="textbox"]',
  '#main footer div[contenteditable="true"]',
  'div[aria-label="Type a message"][contenteditable="true"]',
  'div[aria-placeholder="Type a message"][contenteditable="true"]',
];

/** The first of these selectors that is visible, waiting up to `timeout`. */
async function firstVisible(page, selectors, timeout = 15_000) {
  const deadline = Date.now() + timeout;
  while (Date.now() < deadline) {
    for (const selector of selectors) {
      const el = page.locator(selector).first();
      if (await el.isVisible().catch(() => false)) return el;
    }
    await page.waitForTimeout(400);
  }
  return null;
}

export async function screenshot(page, name) {
  fs.mkdirSync(config.outDir, { recursive: true });
  const file = path.join(config.outDir, `${name}.png`);
  await page.screenshot({ path: file }).catch(() => {});
  return file;
}

/**
 * Close whatever WhatsApp has put over the page. "What's new on WhatsApp Web"
 * came up right after linking and took every click until it was shut. Only a
 * button that dismisses is pressed; anything else gets Escape.
 */
async function dismissDialogs(page) {
  for (let i = 0; i < 5; i += 1) {
    const dialog = page.locator('[role="dialog"][aria-modal="true"]').filter({ visible: true }).first();
    if (!(await dialog.count().catch(() => 0))) return;
    const dismiss = dialog.getByRole("button", { name: /^(continue|ok|got it|close|not now|done)$/i }).first();
    if (await dismiss.count().catch(() => 0)) await dismiss.click().catch(() => {});
    else await page.keyboard.press("Escape");
    await page.waitForTimeout(800);
  }
}

/** The text of the link code WhatsApp shows after Next, "ABCD-EFGH". */
async function readLinkCode(page) {
  for (let i = 0; i < 30; i += 1) {
    const text = await page.evaluate(() => document.body.innerText);
    // The eight characters are drawn one per box, with the dash in a box of its
    // own, so the page text has them split by line breaks; joined back up they
    // are the code. It is the only "XXXX-XXXX" on the screen -- the number
    // being linked has no dash -- and what follows it changes from version to
    // version ("1 Open WhatsApp..." today), so nothing after it is relied on.
    const squeezed = text.replace(/\s+/g, "");
    const match = squeezed.match(/([A-Z0-9]{4})-([A-Z0-9]{4})/);
    if (/enter code/i.test(text) && match) return `${match[1]}-${match[2]}`;
    await page.waitForTimeout(1000);
  }
  return null;
}

// WhatsApp could not open its storage in this browser profile. On Windows that
// was a profile folder whose path was too long; a fresh login cannot work
// until the profile can store again, so this is said rather than retried.
const DATABASE_ERROR = /a database error occurred/i;

async function failIfDatabaseError(page) {
  if (await page.getByText(DATABASE_ERROR).count().catch(() => 0)) {
    await screenshot(page, "database-error");
    throw new Error(
      `whatsapp: "A database error occurred on your browser" -- the profile in ${config.profileDir} ` +
        "cannot store data (on Windows: use a short DATA_DIR path)",
    );
  }
}

/** Ask WhatsApp for a code to link this browser to WA_PHONE's account. */
async function startPhoneLink(page) {
  const link = page.locator('[data-testid="link_device_qr_phone_number_link"]');
  await link.waitFor({ timeout: 60_000 });
  // The page answers the link only once it is ready, and it is ready when the
  // QR code is drawn -- clicked before that, nothing happens.
  await page.locator("canvas").first().waitFor({ state: "visible", timeout: 90_000 }).catch(() => {});
  await failIfDatabaseError(page);
  await page.waitForTimeout(2000);

  const phone = page.locator('input[aria-label^="Type your phone number"], [data-testid="phone-number-input"]').first();
  for (let attempt = 1; ; attempt += 1) {
    const box = await link.boundingBox();
    if (box) await page.mouse.click(box.x + box.width / 2, box.y + box.height / 2);
    try {
      await phone.waitFor({ state: "visible", timeout: 20_000 });
      break;
    } catch (err) {
      if (attempt >= 3) {
        await screenshot(page, "phone-link-failed");
        throw new Error("whatsapp: the phone-number box did not open -- see out/phone-link-failed.png");
      }
    }
  }
  // The box keeps the chosen country's code in front ("+91 "), and a "+" typed
  // after it is dropped: typing the whole number made "+91 91...", which
  // WhatsApp refused. So: whatever code the box insists on, type the rest.
  await phone.click();
  await page.keyboard.press("Control+A");
  await page.keyboard.press("Backspace");
  const kept = (await phone.inputValue()).replace(/\D/g, "");
  const rest = kept && config.waPhone.startsWith(kept) ? config.waPhone.slice(kept.length) : config.waPhone;
  await phone.type(kept ? rest : `+${config.waPhone}`, { delay: 60 });

  const entered = (await phone.inputValue()).replace(/\D/g, "");
  if (entered !== config.waPhone) {
    await screenshot(page, "phone-number-wrong");
    throw new Error(
      `whatsapp: the number box reads ${entered.length} digits, not the ${config.waPhone.length} of WA_PHONE ` +
        "-- see out/phone-number-wrong.png",
    );
  }
  await page.getByRole("button", { name: "Next" }).click();

  const code = await readLinkCode(page);
  await screenshot(page, "link-code");
  return code;
}

/**
 * Open WhatsApp and make sure this browser is linked, linking it if not.
 * Resolves once the chat list is showing.
 */
export async function ensureLinked(page) {
  await page.goto(WA_URL, { waitUntil: "domcontentloaded", timeout: 90_000 });

  // Another tab or browser took the session: take it back.
  const useHere = page.getByRole("button", { name: /use here/i });

  for (let attempt = 0; ; attempt += 1) {
    if (await useHere.isVisible().catch(() => false)) await useHere.click();

    const loggedIn = await firstVisible(page, CHAT_LIST, 45_000);
    if (loggedIn) {
      status.linked = true;
      status.linkCode = null;
      log("whatsapp: linked, chat list is showing");
      await dismissDialogs(page);
      return;
    }

    status.linked = false;
    await failIfDatabaseError(page);
    const onLogin = await firstVisible(page, LOGIN_SCREEN, 30_000);
    if (!onLogin) {
      await screenshot(page, "unknown-screen");
      throw new Error("whatsapp: neither the chat list nor the login screen appeared -- see out/unknown-screen.png");
    }

    if (config.waPhone) {
      const code = await startPhoneLink(page);
      status.linkCode = code;
      log("============================================================");
      log(`whatsapp: NOT LINKED. On the phone for +${config.waPhone}:`);
      log("  WhatsApp > Settings > Linked devices > Link a device >");
      log("  'Link with phone number instead', and enter this code:");
      log(`      ${code || "(could not read it -- open out/link-code.png)"}`);
      log("============================================================");
    } else {
      await screenshot(page, "qr");
      log("whatsapp: NOT LINKED and WA_PHONE is not set. Scan the QR in out/qr.png (or on the status page) from the phone: Linked devices > Link a device.");
    }

    // A code lasts a few minutes; wait for the phone, then ask for a new one.
    const linked = await firstVisible(page, CHAT_LIST, 4 * 60_000);
    if (linked) {
      status.linked = true;
      status.linkCode = null;
      log("whatsapp: linked. The session is saved and will survive restarts.");
      // WhatsApp syncs the chat list for a while after a fresh link.
      await page.waitForTimeout(20_000);
      await dismissDialogs(page);
      return;
    }
    log("whatsapp: the code expired before it was entered -- asking for a new one");
    await page.goto(WA_URL, { waitUntil: "domcontentloaded", timeout: 90_000 });
  }
}

// Spacing and case forgiven, every other character not: the Kanchipuram group
// is "RENDERWAYS- KANCHIPURAM-ATTENDANCE", with a space the others do not have.
const normalise = (s) => String(s || "").replace(/\s+/g, "").toLowerCase();

/** Open the chat with exactly this name. */
async function openChat(page, name) {
  await dismissDialogs(page);
  const search = await firstVisible(page, SEARCH_BOX, 20_000);
  if (!search) throw new Error("whatsapp: the search box was not found");
  await search.click();
  await page.keyboard.press("Control+A");
  await page.keyboard.press("Backspace");
  await page.keyboard.type(name, { delay: 30 });
  await page.waitForTimeout(2500);

  // The result whose title IS the name -- spacing and case forgiven, nothing
  // else. "RENDERWAYS-SALEM" must not open "RENDERWAYS-SALEM OLD".
  const wanted = normalise(name);
  const titles = page.locator("#pane-side span[title], #side span[title]");
  const count = await titles.count();
  for (let i = 0; i < count; i += 1) {
    const el = titles.nth(i);
    const title = await el.getAttribute("title").catch(() => null);
    if (normalise(title) === wanted && (await el.isVisible().catch(() => false))) {
      await el.click();
      await page.waitForTimeout(1500);
      // The open chat's header has its name as a line of its own, beside
      // "click here for group info" or the member list. (Its first [title] is
      // "Profile details", which is what a run read as the chat's name.)
      const lines = (await page.locator("#main header").innerText().catch(() => "")).split("\n");
      if (!lines.some((line) => normalise(line) === wanted)) {
        throw new Error(`whatsapp: opened "${lines[0] || "?"}" instead of "${name}"`);
      }
      return;
    }
  }
  throw new Error(`whatsapp: no chat named "${name}" -- check the name in WA_GROUPS`);
}

// Compared without spaces or WhatsApp's formatting marks: the box may show
// *bold* as bold, and it keeps lines its own way.
const squeeze = (s) => String(s || "").replace(/[\s*_~]+/g, "");
// The lines that have words on them, the same way. Squeezing alone would pass
// a message whose lines had all run into one.
const wordedLines = (s) =>
  String(s || "")
    .split("\n")
    .map((line) => line.replace(/[*_~]/g, "").replace(/\s+/g, " ").trim())
    .filter(Boolean);
const sameLines = (a, b) => JSON.stringify(wordedLines(a)) === JSON.stringify(wordedLines(b));

/**
 * The newest message in the open chat, as the page shows it -- read off what
 * the page says to a screen reader, which has outlasted its class names: the
 * message-out class and data-id are gone, while "You:" and " Delivered " are
 * what a sent message carries today (with the tick's icon named in an svg
 * <title>, wds-ic-read).
 */
function lastMessage(page) {
  return page.evaluate(() => {
    const rows = document.querySelectorAll('#main [role="row"]');
    const last = rows[rows.length - 1];
    if (!last) return null;
    const meta = last.querySelector('[data-testid="msg-meta"]');
    return {
      id: last.querySelector('[data-testid^="conv-msg-"]')?.getAttribute("data-testid") || null,
      mine: Boolean(last.querySelector('[aria-label="You:"], [data-testid="tail-out"], [data-icon="tail-out"]')),
      text: last.innerText || "",
      // "Pending", "Sent", "Delivered" or "Read".
      status: [...(meta ? meta.querySelectorAll("[aria-label]") : [])]
        .map((el) => el.getAttribute("aria-label").trim())
        .join(" "),
      icons: [...last.querySelectorAll("[data-icon], svg > title")].map(
        (el) => el.getAttribute("data-icon") || el.textContent,
      ),
    };
  });
}

// The clock: written, not yet sent. A tick, one or two: sent.
const isWaiting = (msg) => /pending/i.test(msg.status) || msg.icons.some((name) => /msg-time|clock/i.test(name));
const isTicked = (msg) => /\b(sent|delivered|read)\b/i.test(msg.status);

/**
 * Post one text message to the chat with this name. `mustSay` is a line near
 * the top of it, which the sent message has to show for it to count as sent.
 * Resolves to "sent" once the message is in the chat with a tick, or says what
 * it saw when there was no tick to see.
 */
export async function sendText(page, chatName, text, mustSay) {
  const label = chatName.replace(/[^\w-]+/g, "_");
  try {
    await openChat(page, chatName);
    await dismissDialogs(page);

    const box = await firstVisible(page, COMPOSE_BOX, 15_000);
    if (!box) throw new Error("whatsapp: the message box was not found");
    // The box names its chat -- "Type a message to group RENDERWAYS-..." -- so
    // that is checked too, before a word is typed into it.
    const boxLabel = (await box.getAttribute("aria-label").catch(() => null)) || "";
    if (/ to /i.test(boxLabel) && !normalise(boxLabel).endsWith(normalise(chatName))) {
      throw new Error(`whatsapp: the message box says "${boxLabel}", not "${chatName}" -- nothing sent`);
    }
    await box.click();
    // A draft left in the box would go out in front of this.
    await page.keyboard.press("Control+A");
    await page.keyboard.press("Backspace");

    const before = await lastMessage(page).catch(() => null);

    // A line at a time, with Shift+Enter between: Enter on its own sends.
    const lines = text.split("\n");
    for (let i = 0; i < lines.length; i += 1) {
      if (lines[i]) await page.keyboard.insertText(lines[i]);
      if (i < lines.length - 1) await page.keyboard.press("Shift+Enter");
    }
    await page.waitForTimeout(500);

    // What is in the box is what goes, so it is checked while it can still be
    // taken back.
    if (!sameLines(await box.innerText().catch(() => ""), text)) {
      await page.keyboard.press("Control+A");
      await page.keyboard.press("Backspace");
      throw new Error("whatsapp: the message box did not hold the message as typed -- nothing sent");
    }

    const send = await firstVisible(page, SEND_BUTTON, 5_000);
    if (send) await send.click();
    else await page.keyboard.press("Enter");

    // Gone from the box...
    const emptied = Date.now() + 15_000;
    while (squeeze(await box.innerText().catch(() => "")) && Date.now() < emptied) {
      await page.waitForTimeout(500);
    }
    if (squeeze(await box.innerText().catch(() => ""))) {
      throw new Error("whatsapp: the message is still in the box -- it was not sent");
    }

    // ...and in the chat as the newest message, ours, saying what was sent,
    // past the clock icon. "Newest" means it is not the message that was last
    // before sending -- which on a second run the same day says the same first
    // lines -- by its id where the page gives one.
    const isNew = (msg) => (msg.id && before?.id ? msg.id !== before.id : msg.text !== before?.text);
    const isOurs = (msg) =>
      msg && msg.mine && isNew(msg) && squeeze(msg.text).includes(squeeze(mustSay));
    let last = null;
    const shown = Date.now() + 60_000;
    while (Date.now() < shown) {
      last = await lastMessage(page).catch(() => null);
      if (isOurs(last) && !isWaiting(last)) break;
      await page.waitForTimeout(1000);
    }
    // A moment more for the tick, which can come just after.
    const ticked = Date.now() + 10_000;
    while (isOurs(last) && !isWaiting(last) && !isTicked(last) && Date.now() < ticked) {
      await page.waitForTimeout(1000);
      last = (await lastMessage(page).catch(() => null)) || last;
    }
    await screenshot(page, `sent-${label}`);

    if (!isOurs(last)) {
      throw new Error("whatsapp: the box emptied but the message did not show up in the chat");
    }
    if (isWaiting(last)) {
      throw new Error("whatsapp: the message is still waiting to go (clock icon) after a minute");
    }
    // In the chat with no clock and no tick: sent as far as the page shows.
    // What it did show is given, so that a page that has changed again says so.
    return isTicked(last)
      ? `sent (${last.status.toLowerCase()})`
      : `sent (no tick seen; status "${last.status}", icons: ${last.icons.join(",") || "none"})`;
  } catch (err) {
    const shot = await screenshot(page, `failed-${label}`);
    err.message += ` (screen: ${shot})`;
    await page.keyboard.press("Escape").catch(() => {});
    throw err;
  }
}

/** Post one image, with a caption, to the chat with this name. */
export async function sendImage(page, chatName, file, caption) {
  try {
    await openChat(page, chatName);

    const attach = await firstVisible(page, ATTACH_BUTTON, 15_000);
    if (!attach) throw new Error("whatsapp: the attach button was not found");
    await attach.click();
    await page.waitForTimeout(800);

    const input = page.locator(PHOTO_INPUT).first();
    await input.waitFor({ state: "attached", timeout: 15_000 });
    await input.setInputFiles(file);

    const captionBox = await firstVisible(page, CAPTION_BOX, 20_000);
    if (captionBox && caption) {
      await captionBox.click();
      await page.keyboard.type(caption, { delay: 15 });
    }

    const send = await firstVisible(page, SEND_BUTTON, 15_000);
    if (send) await send.click();
    else await page.keyboard.press("Enter");

    // The preview closes once the message is on its way.
    await page.waitForTimeout(4000);
    if (await firstVisible(page, CAPTION_BOX, 1500)) {
      throw new Error("whatsapp: the image preview is still open -- it was not sent");
    }
    await screenshot(page, `sent-${chatName.replace(/[^\w-]+/g, "_")}`);
  } catch (err) {
    const shot = await screenshot(page, `failed-${chatName.replace(/[^\w-]+/g, "_")}`);
    err.message += ` (screen: ${shot})`;
    // Leave the page in a known state for the next group.
    await page.keyboard.press("Escape").catch(() => {});
    throw err;
  }
}
