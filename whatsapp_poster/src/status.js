/**
 * What the poster is doing, as a tiny page.
 *
 * Off unless STATUS_TOKEN is set, and every request needs ?token=... : the
 * screen image is the WhatsApp QR code before linking and the chat list after,
 * neither of which belongs on an open URL.
 */
import fs from "node:fs";
import http from "node:http";

import { config } from "./config.js";
import { log } from "./log.js";

export const status = {
  startedAt: new Date().toISOString(),
  linked: false,
  linkCode: null,
  runs: [], // newest first, last 20
  lastError: null,
};

export const recordRun = (entry) => {
  status.runs.unshift(entry);
  status.runs.length = Math.min(status.runs.length, 20);
};

export function startStatusServer(getPage) {
  if (!config.statusToken) {
    log("status page: off (set STATUS_TOKEN to turn it on)");
    return;
  }
  http
    .createServer(async (req, res) => {
      const url = new URL(req.url, "http://x");
      if (url.searchParams.get("token") !== config.statusToken) {
        res.writeHead(403).end("forbidden");
        return;
      }
      if (url.pathname === "/screen.png") {
        const page = getPage();
        if (!page) {
          res.writeHead(503).end("no page yet");
          return;
        }
        const png = await page.screenshot().catch(() => null);
        if (!png) {
          res.writeHead(500).end("could not take a screenshot");
          return;
        }
        res.writeHead(200, { "Content-Type": "image/png", "Cache-Control": "no-store" }).end(png);
        return;
      }
      if (url.pathname.startsWith("/out/")) {
        const name = url.pathname.slice(5).replace(/[^\w.-]/g, "");
        const file = `${config.outDir}/${name}`;
        if (!fs.existsSync(file)) {
          res.writeHead(404).end("no such file");
          return;
        }
        res.writeHead(200, { "Content-Type": "image/png" }).end(fs.readFileSync(file));
        return;
      }
      res.writeHead(200, { "Content-Type": "application/json" }).end(JSON.stringify(status, null, 2));
    })
    .listen(config.port, () => log(`status page: http://<host>:${config.port}/?token=...  (and /screen.png)`));
}
