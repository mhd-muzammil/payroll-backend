import assert from "node:assert/strict";
import test from "node:test";

// Set before the worker's config is read: a dead local address and a made-up
// login -- and fetch itself is replaced below, so nothing here can reach the
// payroll. A key set here, even empty, is one config.js does not take from a .env.
process.env.PAYROLL_API_URL = "http://127.0.0.1:9";
process.env.PAYROLL_USERNAME = "km-worker-test";
process.env.PAYROLL_PASSWORD = "not-a-real-password";
process.env.WANTED_DAYS = "";

/** What was sent, and how the stand-in payroll answers: (url, init) -> {status, body} or {status, raw}. */
let sent = [];
let answer = () => ({ status: 200, body: {} });
const answering = (fn) => {
  sent = [];
  answer = fn;
};

globalThis.fetch = async (url, init = {}) => {
  sent.push({
    url: String(url),
    method: init.method || "GET",
    headers: init.headers || {},
    body: init.body === undefined ? undefined : JSON.parse(init.body),
  });
  const { status, body, raw } = answer(String(url), init);
  return new Response(raw ?? JSON.stringify(body), { status, headers: { "Content-Type": "application/json" } });
};

const { fetchWanted, postResults, signIn } = await import("../src/payrollApi.js");

const results = Array.from({ length: 60 }, (_, i) => ({
  wo: `WO-0123456${String(i).padStart(2, "0")}`,
  status: "not_found",
  fieldez_ticket_id: "",
  resolutions: [],
  error: "",
}));

test("sign-in as the WhatsApp poster does it: POST /api/auth/login/, the access token back", async () => {
  answering(() => ({ status: 200, body: { access: "access-token", refresh: "refresh-token" } }));
  assert.equal(await signIn(), "access-token");
  assert.deepEqual(sent, [
    {
      url: "http://127.0.0.1:9/api/auth/login/",
      method: "POST",
      headers: { Accept: "application/json", "Content-Type": "application/json" },
      body: { username: "km-worker-test", password: "not-a-real-password" },
    },
  ]);
});

test("a refused sign-in names the settings -- never the password, never the answer", async () => {
  for (const status of [400, 401]) {
    answering(() => ({ status, body: { detail: "No active account", echo: "not-a-real-password" } }));
    await assert.rejects(signIn(), {
      message: "payroll: sign-in refused -- check PAYROLL_USERNAME / PAYROLL_PASSWORD",
    });
  }
  answering(() => ({ status: 200, body: { detail: "no token here" } }));
  await assert.rejects(signIn(), { message: "payroll: sign-in gave no token" });
});

test("the wanted list: a GET with the days and the token, the work orders back", async () => {
  answering(() => ({ status: 200, body: { days: 7, work_orders: ["WO-012345678", "WO-012345679-1"] } }));
  assert.deepEqual(await fetchWanted("access-token", 7), ["WO-012345678", "WO-012345679-1"]);
  assert.equal(sent.length, 1);
  assert.equal(sent[0].method, "GET");
  assert.equal(sent[0].url, "http://127.0.0.1:9/api/fieldez_km/wanted/?days=7");
  assert.equal(sent[0].headers.Authorization, "Bearer access-token");
  assert.equal(sent[0].body, undefined);

  // WANTED_DAYS unset: 31.
  answering(() => ({ status: 200, body: { days: 31, work_orders: [] } }));
  assert.deepEqual(await fetchWanted("access-token"), []);
  assert.equal(sent[0].url, "http://127.0.0.1:9/api/fieldez_km/wanted/?days=31");
});

test("the wanted list: a missing endpoint, a refused login, an odd answer", async () => {
  answering(() => ({ status: 404, body: {} }));
  await assert.rejects(fetchWanted("access-token", 7), {
    message: "payroll: /api/fieldez_km/wanted/ is not there -- redeploy the backend",
  });
  answering(() => ({ status: 403, body: {} }));
  await assert.rejects(fetchWanted("access-token", 7), {
    message: "payroll: /api/fieldez_km/wanted/ refused -- PAYROLL_USERNAME must be an HR or admin login",
  });
  answering(() => ({ status: 200, body: { days: 7 } }));
  await assert.rejects(fetchWanted("access-token", 7), {
    message: "payroll: the wanted list came back in a shape this does not know",
  });
});

test("results go 25 to a request, in order, and the counts add up", async () => {
  answering((url, init) => {
    const n = JSON.parse(init.body).results.length;
    return { status: 200, body: { saved: n - 1, rejected: 1 } };
  });
  assert.deepEqual(await postResults("access-token", results), { saved: 57, rejected: 3 });
  assert.deepEqual(sent.map((s) => s.body.results.length), [25, 25, 10]);
  for (const s of sent) {
    assert.equal(s.method, "POST");
    assert.equal(s.url, "http://127.0.0.1:9/api/fieldez_km/work_orders/");
    assert.equal(s.headers.Authorization, "Bearer access-token");
    assert.deepEqual(Object.keys(s.body), ["results"]);
  }
  // None lost, none sent twice.
  assert.deepEqual(sent.flatMap((s) => s.body.results), results);
});

test("nothing to hand over is no request at all", async () => {
  answering(() => ({ status: 500, body: {} }));
  assert.deepEqual(await postResults("access-token", []), { saved: 0, rejected: 0 });
  assert.equal(sent.length, 0);
});

test("a failing request: the status only, never the body -- and it stops there", async () => {
  answering(() => {
    if (sent.length === 1) return { status: 200, body: { saved: 25, rejected: 0 } };
    return { status: 500, body: { error: "Traceback ... SECRET ..." } };
  });
  await assert.rejects(postResults("access-token", results), (err) => {
    assert.equal(err.message, "payroll: POST /api/fieldez_km/work_orders/ answered 500");
    assert.equal(err.status, 500);
    return true;
  });
  assert.equal(sent.length, 2);

  answering(() => ({ status: 404, body: {} }));
  await assert.rejects(postResults("access-token", results), {
    message: "payroll: /api/fieldez_km/work_orders/ is not there -- redeploy the backend",
  });

  // A 200 that is not JSON (a proxy's page): JSON.parse's message would quote it.
  answering(() => ({ status: 200, raw: "<html>proxy error SECRET</html>" }));
  await assert.rejects(fetchWanted("access-token", 7), {
    message: "payroll: GET /api/fieldez_km/wanted/?days=7 answered with something that is not JSON",
  });
});

test("a payroll that cannot be reached says why, in a word", async () => {
  answering(() => {
    throw new TypeError("fetch failed", { cause: Object.assign(new Error("connect ECONNREFUSED"), { code: "ECONNREFUSED" }) });
  });
  await assert.rejects(fetchWanted("access-token", 7), {
    message: "payroll: GET /api/fieldez_km/wanted/?days=7 could not be reached (ECONNREFUSED)",
  });
  answering(() => {
    throw Object.assign(new Error("The operation was aborted due to timeout"), { name: "TimeoutError" });
  });
  await assert.rejects(postResults("access-token", results.slice(0, 1)), {
    message: "payroll: POST /api/fieldez_km/work_orders/ could not be reached (TimeoutError)",
  });
});
