"""End-to-end smoke test for the OpenCall cases + tracking flow.

Run from payroll_backend/:  python test_cases_flow.py
Uses DRF's APIClient against the real URLconf + a temporary sqlite state.
Safe: creates its own throwaway users/employee and cleans them up.
"""
import os
import django

os.environ.setdefault("DJANGO_SETTINGS_MODULE", "payroll.settings")
os.environ.setdefault("DEBUG", "1")
django.setup()

# The DRF test client talks to host 'testserver'; allow it for this local run.
from django.conf import settings
if "testserver" not in settings.ALLOWED_HOSTS:
    settings.ALLOWED_HOSTS.append("testserver")

from rest_framework.test import APIClient
from authentication.models import User
from employees.models import Employee
from cases.models import Case


def cleanup():
    Case.objects.filter(customer_name="TEST Ravi").delete()
    Employee.objects.filter(employee_name="TEST Suresh").delete()
    User.objects.filter(username__in=["test_opencall_bot", "test_suresh"]).delete()


def main():
    cleanup()
    ok = True

    # --- actors ---
    admin = User.objects.create_user(username="test_opencall_bot", password="x", role="admin")
    eng_user = User.objects.create_user(username="test_suresh", password="x", role="employee")
    engineer = Employee.objects.create(
        user=eng_user, employee_name="TEST Suresh", role="Engineer",
        department="Service", salary=0, branch="Chennai",
    )

    staff = APIClient(); staff.force_authenticate(admin)
    eng = APIClient(); eng.force_authenticate(eng_user)

    # 1. OpenCall creates a case
    r = staff.post("/api/cases/", {
        "customer_name": "TEST Ravi", "customer_phone": "9876543210",
        "title": "AC not cooling", "address": "Anna Nagar, Chennai",
        "latitude": 13.0850, "longitude": 80.2101, "priority": "high",
    }, format="json")
    assert r.status_code == 201, ("create case", r.status_code, r.data)
    case_id = r.data["id"]
    print(f"1. Case created: {r.data['case_number']} (id={case_id})")

    # 2. Assign by NAME (as OpenCall would)
    r = staff.post(f"/api/cases/{case_id}/assign/", {"engineer_name": "TEST Suresh"}, format="json")
    assert r.status_code == 200 and r.data["assigned_to"] == engineer.id, ("assign", r.status_code, r.data)
    print(f"2. Assigned to {r.data['assigned_to_name']}, status={r.data['status']}")

    # 3. Engineer sees only their own case
    r = eng.get("/api/cases/")
    ids = [c["id"] for c in (r.data if isinstance(r.data, list) else r.data.get("results", []))]
    assert case_id in ids, ("engineer list", r.data)
    print(f"3. Engineer sees the case ({len(ids)} case(s))")

    # 4. Engineer drives the lifecycle
    for action, expect in [("start_travel", "on_the_way"), ("reached", "reached"),
                           ("start_work", "working"), ("complete", "completed")]:
        r = eng.post(f"/api/cases/{case_id}/{action}/", {}, format="json")
        assert r.status_code == 200 and r.data["status"] == expect, (action, r.status_code, r.data)
    print("4. Lifecycle ok: on_the_way -> reached -> working -> completed")

    # 5. Engineer sends live pings along a short route
    route = [(13.0850, 80.2101), (13.0862, 80.2135), (13.0879, 80.2160), (13.0895, 80.2181)]
    for lat, lon in route:
        r = eng.post("/api/tracking/ping/", {
            "latitude": lat, "longitude": lon, "accuracy": 12,
            "status": "on_the_way", "case_id": case_id,
        }, format="json")
        assert r.status_code == 201, ("ping", r.status_code, r.data)
    print(f"5. Sent {len(route)} location pings")

    # 6. Staff reads live positions
    r = staff.get("/api/tracking/live/")
    live = [x for x in r.data if x["engineer_id"] == engineer.id]
    assert live and live[0]["active_case_number"], ("live", r.data)
    print(f"6. Live map sees engineer at {live[0]['latitude']:.4f},{live[0]['longitude']:.4f} on {live[0]['active_case_number']}")

    # 7. Staff reads the path + total km
    r = staff.get(f"/api/tracking/path/?case={case_id}")
    assert r.data["count"] == len(route), ("path count", r.data)
    print(f"7. Path: {r.data['count']} points, total {r.data['total_km']} km")

    cleanup()
    print("\nALL GOOD - cases + tracking flow works end to end.")
    return ok


if __name__ == "__main__":
    try:
        main()
    except AssertionError as e:
        print("FAILED -", e)
        cleanup()
        raise
