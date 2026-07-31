# OpenCall ↔ Payroll — Cases & Live Tracking Integration

This documents how the **OpenCall system** (separate Next.js/TypeScript project)
talks to the **Payroll app** (this Django project) so that the confirmed workflow
works end to end:

```
OpenCall: case created + engineer assigned
        ↓  (OpenCall calls Payroll API)
Payroll app: the assigned employee sees the case
        ↓  (engineer works, Payroll app sends GPS)
Payroll: /api/tracking/ping stores live location
        ↓  (OpenCall reads Payroll API)
OpenCall: live tracking map + distance (km)
```

**Division of responsibility**
- **OpenCall** owns: creating cases, assigning the engineer, and the tracking
  *view* (map / km) shown to the office.
- **Payroll** owns: the engineer-facing app (the employee sees the case and
  sends their live location from here), plus the data store for cases + pings.

Everything below is served by this Payroll backend. Base URL is the Payroll API
host, e.g. `https://payrollback.systimus.in` (prod) or `http://localhost:8000`
(dev). All endpoints require a JWT `Authorization: Bearer <access>` header.

---

## 1. Authenticate (OpenCall → Payroll)

OpenCall authenticates once as a dedicated **staff service account** (create an
`admin` role user in Payroll for this) and reuses the token.

```
POST /api/auth/login/
{ "username": "opencall-bot", "password": "••••••" }

→ 200 { "access": "<jwt>", "refresh": "<jwt>" }
```

Refresh with `POST /api/auth/refresh/  { "refresh": "<jwt>" }`.

---

## 2. Push a case into Payroll (OpenCall → Payroll)

Create the case, then assign it. (Two calls; assign is separate so reassignment
uses the same endpoint.)

```
POST /api/cases/
{
  "customer_name": "Ravi Kumar",
  "customer_phone": "9876543210",
  "title": "AC not cooling",
  "description": "Split AC, no cooling since morning",
  "address": "12 Anna Nagar, Chennai",
  "latitude": 13.0850,          // optional but needed for the map pin
  "longitude": 80.2101,         // optional
  "priority": "high"            // low | medium | high | urgent
}
→ 201 { "id": 42, "case_number": "OC-000042", "status": "open", ... }
```

```
POST /api/cases/42/assign/
{ "engineer_name": "Suresh" }   // OR "engineer_email": "..." OR "engineer_id": 7
→ 200 { ...case, "status": "assigned", "assigned_to": 7, "assigned_to_name": "Suresh" }
```

> Payroll resolves the engineer by id → email → name (case-insensitive). Use the
> same name/email that exists on the Payroll Employee record.

---

## 3. Engineer side (happens inside Payroll — no OpenCall work)

The assigned engineer logs into the Payroll app and opens **Cases**. They drive
the case forward; each action is a POST (used by the Payroll UI, also callable):

| Action        | Endpoint                          | Sets status  |
|---------------|-----------------------------------|--------------|
| Accept        | `POST /api/cases/{id}/accept/`      | accepted     |
| Start travel  | `POST /api/cases/{id}/start_travel/`| on_the_way   |
| Reached       | `POST /api/cases/{id}/reached/`     | reached      |
| Start work    | `POST /api/cases/{id}/start_work/`  | working      |
| Complete      | `POST /api/cases/{id}/complete/`    | completed    |

While on duty the Payroll app streams location every ~30s (app-open only):

```
POST /api/tracking/ping/
{ "latitude": 13.08, "longitude": 80.21, "accuracy": 12.5,
  "speed": 4.1, "status": "on_the_way", "case_id": 42 }
→ 201 { ...ping }
```

---

## 4. Read live tracking (OpenCall → Payroll)

**Everyone's latest position** (engineers active in the last 10 min):

```
GET /api/tracking/live/
→ 200 [
  {
    "engineer_id": 7, "engineer_name": "Suresh", "branch": "Chennai",
    "latitude": 13.081, "longitude": 80.209, "accuracy": 12.5, "speed": 4.1,
    "status": "on_the_way", "timestamp": "2026-07-31T09:15:22Z",
    "active_case_id": 42, "active_case_number": "OC-000042"
  }
]
```

Poll this every ~30s to animate the OpenCall map.

**One engineer's full trail + total km for a day:**

```
GET /api/tracking/path/?engineer=7&date=2026-07-31
→ 200 { "count": 128, "total_km": 42.6, "points": [ {lat,lon,accuracy,timestamp}, ... ] }
```

**One case's trail** (engineer's path while attending that case):

```
GET /api/tracking/path/?case=42
→ 200 { "count": 40, "total_km": 8.3, "points": [ ... ] }
```

Distance skips readings with accuracy worse than 100 m so GPS noise doesn't
inflate the kilometers. Draw `points` as a polyline; the latest point is the
live position.

---

## Notes / limits
- **App-open tracking only** — the Payroll app sends location while open; it
  stops when the engineer closes it (no background plugin). Accepted trade-off.
- Map tiles on both sides use **OpenStreetMap** (free). No Google billing.
- Cases are **branch-scoped** by the engineer's branch for Payroll staff; the
  OpenCall service account should be an `admin`/`superadmin` to see all branches.
