# FieldEZ KM worker

Reads, for each work order an engineer checked in to, where FieldEZ says the
engineer was at **"Problem Resolution"** — the place on the Problem Resolution
row of the ticket's Task Details — and hands it to the payroll. The payroll
uses those places for the **FIELDEZ** table in OpenCall's KM Report (ROUTE KM
with the Problem Resolution place instead of the app's check-in GPS).

It runs on the office PC, in its own Chrome window, logged in to FieldEZ.

## What it does

Every 30 minutes, between 08:00 and 22:30 (India time), a *pass*:

1. signs in to the payroll and asks which work orders it wants read — the
   check-ins of the last 7 days whose FieldEZ answer it does not have yet, or
   has an old one of;
2. opens Chrome on FieldEZ (logging in if it has to);
3. for each work order, one at a time with a 1.5 s pause between: searches it
   as Ticket/Summary's *Enter Ticket No* box does, and reads its ticket's Task
   Details rows;
4. closes Chrome and hands the answers to the payroll.

For each work order the payroll is told one of:

| Answer          | Means |
|-----------------|-------|
| `found`         | the Problem Resolution place(s): time, who, latitude, longitude, address |
| `no_resolution` | the ticket has no Problem Resolution with a place (yet) — asked again later |
| `not_found`     | FieldEZ has no ticket with exactly this work order — asked again later |
| `failed`        | FieldEZ could not be read for it — tried again, at most 3 times in all |

Only the status called exactly *Problem Resolution* counts. *Problem Resolution
FCT Validate* is the office's step and has no place; it is never used. A place
of 0, 0 is FieldEZ's "no location" and is never used either.

**It only reads.** It opens FieldEZ's Ticket/Summary page (to log in, and to
copy the headers FieldEZ's own requests carry); after that the only things it
asks FieldEZ are the ticket search and a ticket's activity list. Apart from
the login form, it never presses anything; it never writes anything, and
never logs out. When nothing is wanted, a pass of `npm start` does not open Chrome at all
(`npm run once` always does, so that a person can log in).

**It never touches the OpenCall fieldez-worker.** That worker runs on the
server with its own Chrome profile and its own login (`FIELDEZ_USERNAME` /
`FIELDEZ_PASSWORD`). This one keeps its login in its own profile folder,
`C:\fzkm\fieldez-km-profile`, and reads only `FIELDEZ_KM_USERNAME` /
`FIELDEZ_KM_PASSWORD` — never the other worker's settings, profile or login.

**Use a FieldEZ login that is not the OpenCall fieldez-worker's** (not the
`FIELDEZ_USERNAME` set on the server) — and best not a person's everyday
login either. If FieldEZ allows one session per login, signing in here with
the same one could sign that worker out, and its report and SLA syncs would
stop.

## Running it on the office PC

Once:

1. Install Node.js 20 or newer. Google Chrome must be installed (it uses the
   installed Chrome; nothing else is downloaded).
2. In this folder:

   ```
   npm install
   ```

3. Make a file called `.env` in this folder (it stays on this PC — it is
   git-ignored) with an HR or admin payroll login that sees every branch:

   ```
   PAYROLL_USERNAME=<the login>
   PAYROLL_PASSWORD=<its password>
   ```

   Optionally a FieldEZ login, so it can log itself in again when FieldEZ logs
   it out. Without these, a person logs in in the Chrome window instead:

   ```
   FIELDEZ_KM_USERNAME=<a FieldEZ login>
   FIELDEZ_KM_PASSWORD=<its password>
   ```

4. Run one pass by hand, and log in to FieldEZ in the Chrome window that
   opens (it waits up to 10 minutes for that):

   ```
   npm run once
   ```

   It ends with one line like
   `pass (once): 12 wanted, 12 read -- 9 found, 1 no Problem Resolution place, 1 not in FieldEZ, 1 failed; saved 12, rejected 0 (41s)`.
   FieldEZ remembers the login in the profile folder, so this is needed only
   the first time, or after FieldEZ has logged it out.

Then leave it running:

```
npm start
```

It writes one line per pass, counts only. A pass that could not run says
`FAILED` and why (for example `sign-in refused -- check PAYROLL_USERNAME /
PAYROLL_PASSWORD`, or `not logged in to FieldEZ`); the next pass tries again.
Ctrl+C stops it and closes Chrome.

`npm run probe -- WO-xxxxxxxxx` (the read-only look at one work order) uses the
same profile, so the login is shared — but Chrome opens one profile only once:
run it while `npm start` is between passes, or stop `npm start` first.

## Settings

All optional, in `.env` or the environment:

| Setting | Default | |
|---------|---------|-|
| `PAYROLL_API_URL` | `https://payrollback.systimus.in` | the payroll |
| `WANTED_DAYS` | `31` | check-ins of the last this many days (1 to 31); the payroll keeps a day older than 31 days without its unread check-ins |
| `RUN_EVERY_MINUTES` | `30` | a pass this often (`npm start`) |
| `RUN_FROM`, `RUN_TO` | `08:00`, `22:30` | only between these times, India time, HH:MM |
| `PAUSE_MS` | `1500` | the pause between two work orders |
| `MAX_PER_PASS` | `200` | the most work orders one pass reads |
| `HEADLESS` | `false` | `true` hides the Chrome window — only with a FieldEZ login set above, since nobody can log in to a hidden window |
| `LOGIN_WAIT_MS` | `600000` | how long it waits for a person to log in (10 minutes) |
| `DATA_DIR` | `C:\fzkm` | where the profile is kept; keep it a short path (Chrome's profile fails under a long one) |
| `BROWSER_CHANNEL` | `chrome` | which installed browser |
| `FIELDEZ_BASE_URL` | `https://prod.fsmsupport.com` | FieldEZ |

## Checking the code

```
npm test
```

runs the tests in `test/`. They use made-up work orders and stand-ins for
FieldEZ and the payroll: nothing is opened, and nothing is sent anywhere.
