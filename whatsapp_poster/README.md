# WhatsApp attendance poster

Three times a day (9:15 am, 10:30 am and 9:30 pm IST) it posts each branch's
attendance for the day to the branch's WhatsApp group, as a text message:

```
*Renderways Technology*
Daily Attendance · *Salem branch*
Friday, 09 October 2026
Present 4 · Absent 0 · On leave 0 · 4 records

*Jayakumar S* — Present
General · In 09:08 AM · Out — · Hours —
...

_Generated 9 Oct 2026, 11:09 am_
```

It says what the Attendance page's **Today (Image)** picture says — the same
people, order, times, hours and totals, read from the same list on the payroll
API. (`POST_AS=image` sends that picture instead.)

| Branch      | Group                                  |
|-------------|----------------------------------------|
| Chennai     | RENDERWAYS-CHENNAI-ATTENDANCE          |
| Salem       | RENDERWAYS-SALEM-ATTENDANCE            |
| Kanchipuram | RENDERWAYS- KANCHIPURAM-ATTENDANCE     |
| Vellore     | RENDERWAYS-VELLORE-ATTENDANCE          |
| Hosur       | RENDERWAYS-HOSUR-ATTENDANCE            |

A branch with nobody marked that day is skipped.

> Automating WhatsApp Web is against WhatsApp's terms and the number can be
> banned. Three posts per group a day is low risk, not no risk. Use a
> company number, not somebody's personal one.

## Deploy on Dokploy

1. **Create Application** → Git: this repository (payroll-backend), branch `main`.
2. **Build**: type *Dockerfile*, **Build Path / Context** `whatsapp_poster`,
   Dockerfile `Dockerfile`.
3. **Volume / Mount**: a volume at **`/data`**. Without it the phone has to be
   linked again after every deploy.
4. **Environment**:

   ```
   PAYROLL_API_URL=https://payrollback.systimus.in
   PAYROLL_USERNAME=<an HR or admin login that sees every branch>
   PAYROLL_PASSWORD=<its password>
   WA_PHONE=91XXXXXXXXXX
   RUN_TIMES=09:15,10:30,21:30
   ```

   Optional:

   ```
   TEST_CHAT=<a chat name>   # while trying it out: every post goes here instead
   DRY_RUN=true              # make the messages, send nothing
   POST_AS=image             # the picture instead of text (also needs PAYROLL_URL)
   PAYROLL_URL=https://payroll.systimus.in
   WA_GROUPS={"Salem":"RENDERWAYS-SALEM-ATTENDANCE", ...}   # other group names
   STATUS_TOKEN=<long random string>   # turns on the status page on port 3000
   ```

5. **Deploy**, then open the **Logs**. The first time it prints:

   ```
   whatsapp: NOT LINKED. On the phone for +91XXXXXXXXXX:
     WhatsApp > Settings > Linked devices > Link a device >
     'Link with phone number instead', and enter this code:
         ABCD-EFGH
   ```

   Enter that code on the phone. The code lasts a few minutes; if it runs out
   a new one is printed. Once linked the log says so, and it stays linked.

## Checking on it

- Logs: one line per branch per run — `sent`, `skipped: nobody marked today`,
  or `FAILED -- <why>`. Each message is also kept in `/data/out/` as a `.txt`,
  and a failure saves a screenshot of the WhatsApp screen there.
- With `STATUS_TOKEN` set and port 3000 exposed:
  `https://<host>/?token=<token>` (JSON) and `/screen.png?token=<token>`.

## Running it locally

Put PAYROLL_USERNAME, PAYROLL_PASSWORD and WA_PHONE in a `.env` here (it is
git-ignored), then the commands below. On Windows keep DATA_DIR short, like
`./data`: with the profile under a long folder path WhatsApp Web says *"A
database error occurred on your browser"* and never shows a login.

```
npm install
npm test
DATA_DIR=./data node src/index.js --capture-only     # messages into data/out, nothing sent
BROWSER_CHANNEL=chrome DATA_DIR=./data node src/index.js --once   # link, post once
```
