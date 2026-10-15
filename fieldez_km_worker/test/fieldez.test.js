import assert from "node:assert/strict";
import test from "node:test";

// Set before the worker's config is read. Every page below is a stand-in that
// answers from the test itself -- no browser, no network -- and even the
// address is a dead local one, so nothing here can reach FieldEZ. A key set
// here, even empty, is one config.js does not take from a .env.
process.env.FIELDEZ_BASE_URL = "http://127.0.0.1:9";
process.env.HEADLESS = "true";
process.env.FIELDEZ_KM_USERNAME = "";
process.env.FIELDEZ_KM_PASSWORD = "";

const { config } = await import("../src/config.js");
const { captureAuthHeaders, ensureLoggedIn, forgetRefusal, readActivities, readWorkOrder, searchTicket } = await import(
  "../src/fieldez.js"
);

// Made-up work orders and places, shaped like the answers measured on 14 Oct 2026.
const searchAnswer = (rows) => ({ id: "c0ffee", message: "Success", data: { ticketList: rows, totalRecords: rows.length } });
const row = (id, ticketNo) => ({ id, bpId: 9, ft_ticket_no: ticketNo, ftTaskName: "WO Closed" });
const SYNC = 1791782875512;
const resolution = {
  fta_name: "Problem Resolution",
  updatedBy: "Test Engineer",
  syncTime: SYNC,
  createTime: SYNC + 1000,
  latitude: 11.1111,
  longitude: 77.7777,
  locationAddress: "1 Test Street, Testpuram",
};

/** A page whose page.request answers from `routes(method, url)` -> {status, json}, and remembers every call. */
function fakePage(routes) {
  const calls = [];
  const reply = async (method, url, options) => {
    calls.push({ method, url, options });
    const { status = 200, json } = routes(method, url, options) || {};
    return {
      status: () => status,
      ok: () => status >= 200 && status < 300,
      json: async () => {
        if (json === undefined) throw new SyntaxError("Unexpected token '<', \"<html>\" is not valid JSON");
        return json;
      },
    };
  };
  return {
    calls,
    request: {
      get: (url, options) => reply("GET", url, options),
      post: (url, options) => reply("POST", url, options),
    },
  };
}

const pathOf = (call) => `${call.method} ${new URL(call.url).pathname}`;

test("the search: exactly the call FieldEZ's search box makes", async () => {
  const page = fakePage(() => ({ json: searchAnswer([row(1000002, "WO-012345678-1"), row(1000001, "WO-012345678")]) }));
  const ticket = await searchTicket(page, { authorization: "Bearer test", orgid: "7" }, "WO-012345678");
  assert.deepEqual(ticket, { id: 1000001, bpId: 9, ticketNo: "WO-012345678" });
  assert.equal(page.calls.length, 1);
  const [call] = page.calls;
  assert.equal(call.method, "POST");
  assert.equal(call.url, "http://127.0.0.1:9/tlm/v1.0/ticket/summary-list/search?page=0&rows=50");
  assert.deepEqual(call.options.data, { searchKey: "Work Order", searchValue: "WO-012345678" });
  assert.deepEqual(call.options.headers, { authorization: "Bearer test", orgid: "7", "Content-Type": "application/json" });
});

test("the search: no exact match is null; no answer is an error, never 'not found'", async () => {
  const neighbour = fakePage(() => ({ json: searchAnswer([row(1000002, "WO-012345678-1")]) }));
  assert.equal(await searchTicket(neighbour, {}, "WO-012345678"), null);
  assert.equal(await searchTicket(fakePage(() => ({ json: searchAnswer([]) })), {}, "WO-012345678"), null);

  await assert.rejects(searchTicket(fakePage(() => ({ status: 500, json: { message: "boom" } })), {}, "WO-012345678"), {
    message: "search answered 500",
  });
  await assert.rejects(searchTicket(fakePage(() => ({ status: 403, json: {} })), {}, "WO-012345678"), {
    message: "search answered 403",
  });
  await assert.rejects(searchTicket(fakePage(() => ({ status: 200 })), {}, "WO-012345678"), {
    message: "search answered with something that is not JSON",
  });
  // A 401 is the login gone, and says so.
  await assert.rejects(searchTicket(fakePage(() => ({ status: 401, json: {} })), {}, "WO-012345678"), (err) => {
    assert.equal(err.loggedOut, true);
    assert.equal(err.message, "FieldEZ answered 401 to the search: not logged in");
    return true;
  });
});

test("the activity list: one GET, a bare array or a data wrapper", async () => {
  const list = [resolution];
  const page = fakePage(() => ({ json: list }));
  assert.deepEqual(await readActivities(page, { token: "test" }, 1000001), list);
  assert.equal(page.calls.length, 1);
  assert.equal(page.calls[0].method, "GET");
  assert.equal(page.calls[0].url, "http://127.0.0.1:9/tlm/v1.0/ticket-activity/ticket/1000001");
  assert.deepEqual(page.calls[0].options.headers, { token: "test" });

  assert.deepEqual(await readActivities(fakePage(() => ({ json: { data: list } })), {}, 1000001), list);
  assert.deepEqual(await readActivities(fakePage(() => ({ json: [] })), {}, 1000001), []);
  await assert.rejects(readActivities(fakePage(() => ({ json: { data: null } })), {}, 1000001), {
    message: "activities came back in a shape this does not know",
  });
  await assert.rejects(readActivities(fakePage(() => ({ status: 404, json: {} })), {}, 1000001), {
    message: "activities answered 404",
  });
});

test("one work order, search to Task Details -- and only those two reads", async () => {
  const page = fakePage((method, url) =>
    url.includes("/search") ? { json: searchAnswer([row(1000001, "WO-012345678")]) } : { json: [resolution] },
  );
  assert.deepEqual(await readWorkOrder(page, {}, "WO-012345678"), {
    wo: "WO-012345678",
    status: "found",
    fieldez_ticket_id: "1000001",
    resolutions: [
      { at: "2026-10-12T05:27:55.512Z", user: "Test Engineer", latitude: 11.1111, longitude: 77.7777, address: "1 Test Street, Testpuram" },
    ],
    error: "",
  });
  assert.deepEqual(page.calls.map(pathOf), [
    "POST /tlm/v1.0/ticket/summary-list/search",
    "GET /tlm/v1.0/ticket-activity/ticket/1000001",
  ]);
});

test("not in FieldEZ: the activity list is not asked for", async () => {
  const page = fakePage(() => ({ json: searchAnswer([row(1000002, "WO-012345678-1")]) }));
  assert.deepEqual(await readWorkOrder(page, {}, "WO-012345678"), {
    wo: "WO-012345678",
    status: "not_found",
    fieldez_ticket_id: "",
    resolutions: [],
    error: "",
  });
  assert.deepEqual(page.calls.map(pathOf), ["POST /tlm/v1.0/ticket/summary-list/search"]);
});

test("no Problem Resolution place yet", async () => {
  const page = fakePage((method, url) =>
    url.includes("/search")
      ? { json: searchAnswer([row(1000001, "WO-012345678")]) }
      : { json: [{ ...resolution, fta_name: "Problem Resolution FCT Validate", latitude: 0, longitude: 0 }] },
  );
  const result = await readWorkOrder(page, {}, "WO-012345678");
  assert.equal(result.status, "no_resolution");
  assert.equal(result.fieldez_ticket_id, "1000001");
});

test("a FieldEZ that does not answer is that work order's failed; a lost login is thrown", async () => {
  const broken = fakePage((method, url) =>
    url.includes("/search") ? { json: searchAnswer([row(1000001, "WO-012345678")]) } : { status: 500, json: {} },
  );
  assert.deepEqual(await readWorkOrder(broken, {}, "WO-012345678"), {
    wo: "WO-012345678",
    status: "failed",
    fieldez_ticket_id: "1000001",
    resolutions: [],
    error: "activities answered 500",
  });

  // A request that throws, the way Playwright does on a timeout: the first
  // line only, never the call log with the headers in it.
  const timingOut = {
    request: {
      post: async () => {
        throw new Error("apiRequestContext.post: Timeout 45000ms exceeded.\nCall log:\n  - → POST\n    - authorization: Bearer SECRET-TOKEN");
      },
    },
  };
  const timedOut = await readWorkOrder(timingOut, { authorization: "Bearer SECRET-TOKEN" }, "WO-012345678");
  assert.equal(timedOut.status, "failed");
  assert.equal(timedOut.error, "apiRequestContext.post: Timeout 45000ms exceeded.");
  assert.ok(!JSON.stringify(timedOut).includes("SECRET"));

  const loggedOut = fakePage((method, url) =>
    url.includes("/search") ? { json: searchAnswer([row(1000001, "WO-012345678")]) } : { status: 401, json: {} },
  );
  await assert.rejects(readWorkOrder(loggedOut, {}, "WO-012345678"), (err) => err.loggedOut === true);
});

test("the auth headers: copied off FieldEZ's own first API call, those names only", async () => {
  const listeners = new Map();
  const visited = [];
  const page = {
    on: (event, fn) => listeners.set(event, fn),
    off: (event, fn) => {
      if (listeners.get(event) === fn) listeners.delete(event);
    },
    goto: async (url) => {
      visited.push(url);
      const emit = (requestUrl, headers) => listeners.get("request")?.({ url: () => requestUrl, headers: () => headers });
      emit("http://127.0.0.1:9/frontend/main.js", { authorization: "not an API call" });
      emit("http://127.0.0.1:9/tlm/v1.0/ticket/summary-list?page=0&rows=20", {
        authorization: "Bearer test",
        orgid: "7",
        "x-tenant-id": "",
        cookie: "session=1",
        "user-agent": "Chrome",
        accept: "application/json",
      });
      emit("http://127.0.0.1:9/admin/v1.0/user", { authorization: "Bearer a later one" });
    },
    waitForTimeout: async () => {},
  };
  assert.deepEqual(await captureAuthHeaders(page), { authorization: "Bearer test", orgid: "7" });
  assert.deepEqual(visited, ["http://127.0.0.1:9/frontend/tlm/summary-list"]);
  // It stops listening once the page has loaded.
  assert.equal(listeners.has("request"), false);

  // Pages that call no API: no headers, cookies alone.
  const quiet = { ...page, goto: async () => {} };
  assert.deepEqual(await captureAuthHeaders(quiet), {});
});

/** A page for the login: the summary page when logged in, FieldEZ's login page when not. */
function loginPage({ loggedIn = false, password = "right" } = {}) {
  const did = [];
  let at = "about:blank";
  let typed = "";
  return {
    did,
    url: () => at,
    goto: async (url) => {
      did.push(`goto ${new URL(url).pathname}`);
      at = loggedIn ? url : "http://127.0.0.1:9/frontend/auth/login";
    },
    waitForTimeout: async () => {},
    waitForLoadState: async () => {},
    bringToFront: async () => did.push("front"),
    getByPlaceholder: () => ({ first: () => ({ fill: async (value) => did.push(`username ${value}`) }) }),
    locator: () => ({
      first: () => ({
        fill: async (value) => {
          typed = value;
          did.push("password");
        },
      }),
    }),
    getByRole: () => ({
      first: () => ({
        click: async () => {
          did.push("log in");
          if (typed === password) {
            loggedIn = true;
            at = "http://127.0.0.1:9/frontend/dashboard";
          }
        },
      }),
    }),
  };
}

test("already logged in: the summary page, and nothing else", async () => {
  const page = loginPage({ loggedIn: true });
  await ensureLoggedIn(page);
  assert.deepEqual(page.did, ["goto /frontend/tlm/summary-list"]);
});

test("a login set in .env: the form filled once, then the summary page", async () => {
  Object.assign(config, { fieldezUsername: "km-worker", fieldezPassword: "right" });
  try {
    const page = loginPage();
    await ensureLoggedIn(page);
    assert.deepEqual(page.did, [
      "goto /frontend/tlm/summary-list",
      "username km-worker",
      "password",
      "log in",
      "goto /frontend/tlm/summary-list",
    ]);

    // A wrong password is not typed again and again -- not in this pass, and
    // not in any later one while the process runs.
    const refusedMessage = /^FieldEZ refused FIELDEZ_KM_USERNAME \/ FIELDEZ_KM_PASSWORD/;
    const refused = loginPage({ password: "something else" });
    await assert.rejects(ensureLoggedIn(refused), { message: refusedMessage });
    assert.equal(refused.did.filter((step) => step === "log in").length, 1);
    const nextPass = loginPage({ password: "something else" });
    await assert.rejects(ensureLoggedIn(nextPass), { message: refusedMessage });
    assert.deepEqual(nextPass.did, ["goto /frontend/tlm/summary-list"]);
  } finally {
    Object.assign(config, { fieldezUsername: "", fieldezPassword: "" });
    forgetRefusal();
  }
});

test("a 200 with no ticket list is 'could not read', never 'not in FieldEZ'", async () => {
  for (const json of [{ message: "Invalid token", data: null }, {}, { data: {} }]) {
    await assert.rejects(searchTicket(fakePage(() => ({ json })), {}, "WO-012345678"), {
      message: "search came back in a shape this does not know",
    });
    const result = await readWorkOrder(fakePage(() => ({ json })), {}, "WO-012345678");
    assert.equal(result.status, "failed");
    assert.equal(result.error, "search came back in a shape this does not know");
  }
});

test("a page cut short without the ticket: failed, never 'not in FieldEZ'", async () => {
  const revisits = Array.from({ length: 50 }, (_, n) => row(1000100 + n, `WO-012345678-${n + 1}`));
  const cut = fakePage(() => ({ json: { ...searchAnswer(revisits), data: { ticketList: revisits, totalRecords: 51 } } }));
  await assert.rejects(searchTicket(cut, {}, "WO-012345678"), { message: "search answer was cut short" });
  const result = await readWorkOrder(cut, {}, "WO-012345678");
  assert.equal(result.status, "failed");
  assert.equal(result.error, "search answer was cut short");
  // The ticket on the page is the answer, however many more there are.
  const found = [row(1000001, "WO-012345678"), ...revisits.slice(1)];
  const onPage = fakePage(() => ({ json: { ...searchAnswer(found), data: { ticketList: found, totalRecords: 51 } } }));
  assert.deepEqual(await searchTicket(onPage, {}, "WO-012345678"), { id: 1000001, bpId: 9, ticketNo: "WO-012345678" });
  // Everything there was came back: not there is not there.
  assert.equal(await searchTicket(fakePage(() => ({ json: searchAnswer(revisits) })), {}, "WO-012345678"), null);
});

test("two different tickets for one work order: failed, and said so", async () => {
  const twice = fakePage(() => ({ json: searchAnswer([row(1000001, "WO-012345678"), row(1000003, "WO012345678")]) }));
  await assert.rejects(searchTicket(twice, {}, "WO-012345678"), {
    message: "FieldEZ has more than one ticket for this work order",
  });
  const result = await readWorkOrder(twice, {}, "WO-012345678");
  assert.equal(result.status, "failed");
  assert.equal(result.error, "FieldEZ has more than one ticket for this work order");
  // The same ticket listed twice is still that one ticket.
  const same = fakePage(() => ({ json: searchAnswer([row(1000001, "WO-012345678"), row(1000001, "WO-012345678")]) }));
  assert.deepEqual(await searchTicket(same, {}, "WO-012345678"), { id: 1000001, bpId: 9, ticketNo: "WO-012345678" });
});

test("headless with no login set: an error at once, not a wait for nobody", async () => {
  assert.equal(config.headless, true);
  const page = loginPage();
  await assert.rejects(ensureLoggedIn(page), /not logged in to FieldEZ -- run `npm run once` with HEADLESS=false/);
  assert.deepEqual(page.did, ["goto /frontend/tlm/summary-list"]);
});
