import math
import openpyxl
import re
from datetime import datetime, time, date as dt_date
from django.db import transaction
from django.utils import timezone
from django.utils.timezone import make_aware
from rest_framework import viewsets
from rest_framework.permissions import IsAuthenticated
from rest_framework.decorators import action
from rest_framework.response import Response
from .models import Attendance, LeaveRequest
from .serializer import AttendanceSerializer, LeaveRequestSerializer
from employees.models import Employee
from authentication.models import User

def haversine_distance(lat1, lon1, lat2, lon2):
    # approximate radius of earth in km
    R = 6371.0
    dlat = math.radians(lat2 - lat1)
    dlon = math.radians(lon2 - lon1)
    a = math.sin(dlat / 2) * math.sin(dlat / 2) + math.cos(math.radians(lat1)) \
        * math.cos(math.radians(lat2)) * math.sin(dlon / 2) * math.sin(dlon / 2)
    c = 2 * math.atan2(math.sqrt(a), math.sqrt(1 - a))
    return R * c * 1000  # returns distance in meters

class AttendanceViewSet(viewsets.ModelViewSet):
    serializer_class = AttendanceSerializer
    permission_classes = [IsAuthenticated]

    @action(detail=False, methods=["post"])
    def import_sheet(self, request):
        user = request.user
        role = "superadmin" if user.is_superuser else getattr(user, 'role', 'employee')
        if role not in ["superadmin", "admin"]:
            return Response({"detail": "Permission denied."}, status=403)

        file_obj = request.FILES.get('file')
        if not file_obj:
            return Response({"detail": "No file uploaded."}, status=400)

        try:
            wb = openpyxl.load_workbook(file_obj, data_only=True)
            sheet = wb.active
            sheet_values = list(sheet.iter_rows(values_only=True))
        except Exception as e:
            return Response({"detail": f"Failed to parse Excel file: {str(e)}"}, status=400)

        num_rows = len(sheet_values)
        num_cols = len(sheet_values[0]) if num_rows > 0 else 0

        def normalize_val(val):
            if val is None:
                return ""
            return " ".join(str(val).split()).strip().lower()

        # 1. Scan first 5 rows to locate "Year" or "Month"
        year = None
        month = None
        for r in range(1, min(6, num_rows + 1)):
            for c in range(1, num_cols + 1):
                val = sheet_values[r-1][c-1]
                val_norm = normalize_val(val)
                if "year" in val_norm:
                    if ":" in val_norm:
                        try:
                            year = int(val_norm.split(":")[1].strip())
                        except ValueError:
                            pass
                    if not year and c < num_cols:
                        right_val = sheet_values[r-1][c]
                        if right_val:
                            try:
                                year = int(str(right_val).strip())
                            except ValueError:
                                pass
                if "month" in val_norm:
                    if ":" in val_norm:
                        try:
                            month = int(val_norm.split(":")[1].strip())
                        except ValueError:
                            pass
                    if not month and c < num_cols:
                        right_val = sheet_values[r-1][c]
                        if right_val:
                            try:
                                month = int(str(right_val).strip())
                            except ValueError:
                                pass

        if not year:
            year = datetime.now().year
        if not month:
            month = datetime.now().month

        # Helper to parse dates in header
        def parse_header_date(val, default_year):
            if not val:
                return None
            if isinstance(val, (datetime, dt_date)):
                if hasattr(val, 'date'):
                    return val.date()
                return val
            val_str = normalize_val(val)
            # Try to search for DD-MMM pattern anywhere in the normalized string
            # e.g. "25-mar", "25-march", "wed 25-mar"
            match = re.search(r'\b(\d{1,2})[-/]([A-Za-z]+)\b', val_str)
            if match:
                day = int(match.group(1))
                month_name = match.group(2)
                try:
                    dt = datetime.strptime(f"{day}-{month_name[:3]}-{default_year}", "%d-%b-%Y")
                    return dt.date()
                except ValueError:
                    pass
            # Pattern: DD-MM-YYYY or YYYY-MM-DD anywhere
            match_numeric = re.search(r'\b(\d{1,2})[-/](\d{1,2})[-/](\d{2,4})\b', val_str)
            if match_numeric:
                part1 = match_numeric.group(1)
                part2 = match_numeric.group(2)
                part3 = match_numeric.group(3)
                try:
                    dt = datetime.strptime(f"{part1}-{part2}-{part3}", "%d-%m-%Y")
                    return dt.date()
                except ValueError:
                    pass
                try:
                    dt = datetime.strptime(f"{part1}-{part2}-{part3}", "%m-%d-%Y")
                    return dt.date()
                except ValueError:
                    pass
            return None

        # Helper to parse time string/floats
        def parse_excel_time(val):
            if val is None:
                return None
            if isinstance(val, time):
                return val
            if isinstance(val, datetime):
                return val.time()
            
            val_str = str(val).strip()
            if val_str == "" or val_str == "00:00" or val_str == "00:00:00":
                return None
                
            for fmt in ("%I:%M %p", "%I:%M%p", "%H:%M", "%H:%M:%S", "%I:%M:%S %p", "%I:%M:%S%p"):
                try:
                    return datetime.strptime(val_str, fmt).time()
                except ValueError:
                    pass
                    
            try:
                val_float = float(val)
                total_seconds = int(round(val_float * 86400))
                hour = (total_seconds // 3600) % 24
                minute = (total_seconds % 3600) // 60
                second = total_seconds % 60
                return time(hour, minute, second)
            except ValueError:
                pass
                
            return None

        # 2. Find header row containing Employee Code and Name
        header_row_idx = None
        emp_code_col_idx = None
        emp_name_col_idx = None

        # Scan rows from 1 to 20 to find the header row containing both employee code and name
        for r in range(1, min(21, num_rows + 1)):
            temp_code_col = None
            temp_name_col = None
            for c in range(1, num_cols + 1):
                val = sheet_values[r-1][c-1]
                val_norm = normalize_val(val)
                
                is_code_match = ("emp" in val_norm and "code" in val_norm) or (val_norm == "code") or ("employee" in val_norm and "code" in val_norm)
                is_name_match = ("employee" in val_norm and "name" in val_norm) or ("emp" in val_norm and "name" in val_norm) or (val_norm == "name")
                
                if is_code_match:
                    temp_code_col = c
                elif is_name_match:
                    temp_name_col = c
            
            if temp_code_col and temp_name_col:
                header_row_idx = r
                emp_code_col_idx = temp_code_col
                emp_name_col_idx = temp_name_col
                break

        if not header_row_idx:
            # Fallback: find them independently
            for r in range(1, min(21, num_rows + 1)):
                for c in range(1, num_cols + 1):
                    val = sheet_values[r-1][c-1]
                    val_norm = normalize_val(val)
                    if (not emp_code_col_idx) and (("emp" in val_norm and "code" in val_norm) or (val_norm == "code") or ("employee" in val_norm and "code" in val_norm)):
                        emp_code_col_idx = c
                        header_row_idx = r
                    elif (not emp_name_col_idx) and (("employee" in val_norm and "name" in val_norm) or ("emp" in val_norm and "name" in val_norm) or (val_norm == "name")):
                        emp_name_col_idx = c
                        if not header_row_idx:
                            header_row_idx = r

        if not header_row_idx or not emp_name_col_idx or not emp_code_col_idx:
            # Print debug info to console for troubleshooting
            print("--- Excel Import Error: Could not identify header row ---")
            for r in range(1, min(15, num_rows + 1)):
                row_vals = [sheet_values[r-1][c-1] for c in range(1, min(10, num_cols + 1))]
                print(f"Row {r}: {row_vals}")
            return Response({"detail": "Could not identify header row with 'Emp Code' and 'Employee Name'."}, status=400)

        # 3. Find type/status column index
        type_col_idx = None
        for r in range(header_row_idx + 1, min(header_row_idx + 12, num_rows + 1)):
            for c in range(1, num_cols + 1):
                val = sheet_values[r-1][c-1]
                val_norm = normalize_val(val)
                if val_norm in ["in time", "out time", "actual hrs", "present day", "present days"]:
                    type_col_idx = c
                    break
            if type_col_idx:
                break

        if not type_col_idx:
            type_col_idx = emp_name_col_idx + 1

        # 4. Map date columns and determine header bounds
        date_cols = {}
        dates_on_header_row = 0
        dates_on_below_row = 0
        
        for c in range(1, num_cols + 1):
            if c in [emp_code_col_idx, emp_name_col_idx, type_col_idx]:
                continue
            
            val_header = sheet_values[header_row_idx - 1][c - 1]
            val_below = sheet_values[header_row_idx][c - 1] if header_row_idx < num_rows else None
            
            parsed_header = parse_header_date(val_header, year)
            parsed_below = parse_header_date(val_below, year)
            
            if parsed_header:
                dates_on_header_row += 1
            if parsed_below:
                dates_on_below_row += 1
                
            parsed_date = parsed_header or parsed_below
            if parsed_date:
                date_cols[c] = parsed_date

        if not date_cols:
            # Print debug info to console for troubleshooting
            print("--- Excel Import Error: No valid date columns found ---")
            print(f"Header Row Index: {header_row_idx}")
            for r in range(1, min(15, num_rows + 1)):
                row_vals = [sheet_values[r-1][c-1] for c in range(1, min(10, num_cols + 1))]
                print(f"Row {r}: {row_vals}")
            return Response({"detail": "No valid date columns found in the header row."}, status=400)

        # If more dates were parsed from the row below the main header row,
        # the header block extends to header_row_idx + 1.
        if dates_on_below_row > dates_on_header_row:
            start_data_row = header_row_idx + 2
        else:
            start_data_row = header_row_idx + 1

        # 5. Parse employee rows
        employee_data = {}
        current_emp_code = None
        current_emp_name = None

        for r in range(start_data_row, num_rows + 1):
            emp_code_val = sheet_values[r-1][emp_code_col_idx - 1]
            emp_name_val = sheet_values[r-1][emp_name_col_idx - 1]

            if emp_code_val is not None:
                current_emp_code = str(emp_code_val).strip()
                if current_emp_code.endswith(".0"):
                    current_emp_code = current_emp_code[:-2]
            if emp_name_val is not None:
                current_emp_name = str(emp_name_val).strip()

            if not current_emp_name:
                continue

            row_type = normalize_val(sheet_values[r-1][type_col_idx - 1])
            if not row_type:
                continue

            emp_key = current_emp_code or current_emp_name
            if emp_key not in employee_data:
                employee_data[emp_key] = {
                    "code": current_emp_code,
                    "name": current_emp_name,
                    "dates": {}
                }

            dates_dict = employee_data[emp_key]["dates"]
            for c, date_obj in date_cols.items():
                if date_obj not in dates_dict:
                    dates_dict[date_obj] = {"in": None, "out": None, "present": 0.0}

                cell_val = sheet_values[r-1][c - 1]
                if "in time" in row_type:
                    dates_dict[date_obj]["in"] = cell_val
                elif "out time" in row_type:
                    dates_dict[date_obj]["out"] = cell_val
                elif "present" in row_type:
                    try:
                        dates_dict[date_obj]["present"] = float(cell_val) if cell_val is not None else 0.0
                    except ValueError:
                        dates_dict[date_obj]["present"] = 0.0

        # 6. Database Transaction for bulk creation/updates
        employees_created = 0
        records_saved = 0

        try:
            with transaction.atomic():
                # Pre-fetch employees and user information to avoid repeated queries
                all_employees = list(Employee.objects.all().select_related("user"))
                emp_by_id = {e.id: e for e in all_employees}
                emp_by_name = {e.employee_name.lower().strip(): e for e in all_employees}

                # Pre-fetch existing usernames to check uniqueness locally
                existing_usernames = set(User.objects.values_list("username", flat=True))

                # Pre-fetch existing attendance records within the imported date range
                all_dates = list(date_cols.values())
                attendance_map = {}
                if all_dates:
                    min_date = min(all_dates)
                    max_date = max(all_dates)
                    start_datetime = make_aware(datetime.combine(min_date, time.min))
                    end_datetime = make_aware(datetime.combine(max_date, time.max))
                    existing_attendance = Attendance.objects.filter(
                        intime__range=(start_datetime, end_datetime)
                    )
                    for att in existing_attendance:
                        if att.employee_id and att.intime:
                            attendance_map[(att.employee_id, att.intime.date())] = att

                records_to_create = []
                records_to_update = []

                for emp_key, emp_info in employee_data.items():
                    code = emp_info["code"]
                    name = emp_info["name"]
                    dates = emp_info["dates"]

                    employee = None
                    if code:
                        try:
                            emp_id = int(code)
                            employee = emp_by_id.get(emp_id)
                        except ValueError:
                            pass
                    
                    if not employee and name:
                        employee = emp_by_name.get(name.lower().strip())

                    if not employee:
                        # 1. Create User account first
                        username = f"{name.lower().replace(' ', '')}_{code}" if code else name.lower().replace(' ', '')
                        base_username = username
                        counter = 1
                        while username in existing_usernames:
                            username = f"{base_username}_{counter}"
                            counter += 1

                        existing_usernames.add(username)

                        import secrets
                        clean_first_name = re.sub(r'[^a-zA-Z]', '', name.split()[0]).capitalize() if name else "User"
                        temp_pwd = f"{clean_first_name}@{secrets.randbelow(9000) + 1000}"

                        user_obj = User.objects.create_user(
                            username=username,
                            email=f"{username}@company.com",
                            first_name=name,
                            role='employee',
                            plain_password=temp_pwd
                        )
                        user_obj.set_password(temp_pwd)
                        user_obj.save()

                        # 2. Create Employee profile linked to the User
                        create_kwargs = {
                            "user": user_obj,
                            "employee_name": name,
                            "role": "Staff",
                            "department": "General",
                            "salary": 0.00,
                            "branch": "Chennai",
                            "status": "active"
                        }
                        if code:
                            try:
                                emp_id = int(code)
                                if emp_id not in emp_by_id:
                                    create_kwargs["id"] = emp_id
                            except ValueError:
                                pass
                        employee = Employee.objects.create(**create_kwargs)
                        emp_by_id[employee.id] = employee
                        emp_by_name[employee.employee_name.lower().strip()] = employee
                        employees_created += 1
                    else:
                        # If employee exists but doesn't have a linked user account, create and link one
                        if not employee.user:
                            username = f"{name.lower().replace(' ', '')}_{code}" if code else name.lower().replace(' ', '')
                            base_username = username
                            counter = 1
                            while username in existing_usernames:
                                username = f"{base_username}_{counter}"
                                counter += 1

                            existing_usernames.add(username)

                            import secrets
                            clean_first_name = re.sub(r'[^a-zA-Z]', '', name.split()[0]).capitalize() if name else "User"
                            temp_pwd = f"{clean_first_name}@{secrets.randbelow(9000) + 1000}"

                            user_obj = User.objects.create_user(
                                username=username,
                                email=f"{username}@company.com",
                                first_name=name,
                                role='employee',
                                plain_password=temp_pwd
                            )
                            user_obj.set_password(temp_pwd)
                            user_obj.save()

                            employee.user = user_obj
                            employee.save()

                    # Create/Update attendance records
                    for date_obj, vals in dates.items():
                        in_val = vals["in"]
                        out_val = vals["out"]
                        present_val = vals["present"]

                        in_time = parse_excel_time(in_val)
                        out_time = parse_excel_time(out_val)

                        if in_time is not None or present_val > 0.0:
                            status = "Present"
                            in_time_actual = in_time or time(9, 0)
                            out_time_actual = out_time or time(18, 0)
                            
                            in_datetime = make_aware(datetime.combine(date_obj, in_time_actual))
                            out_datetime = make_aware(datetime.combine(date_obj, out_time_actual)) if out_time else None
                        else:
                            status = "Absent"
                            in_datetime = make_aware(datetime.combine(date_obj, time.min))
                            out_datetime = None

                        attendance_fields = {
                            "employee": employee,
                            "employee_name": employee.employee_name,
                            "role": employee.role,
                            "department": employee.department,
                            "salary": employee.salary,
                            "intime": in_datetime,
                            "outtime": out_datetime,
                            "status": status
                        }

                        existing = attendance_map.get((employee.id, date_obj))
                        if existing:
                            for k, v in attendance_fields.items():
                                setattr(existing, k, v)
                            records_to_update.append(existing)
                        else:
                            records_to_create.append(Attendance(**attendance_fields))
                        records_saved += 1

                # Execute bulk operations
                if records_to_create:
                    Attendance.objects.bulk_create(records_to_create)
                if records_to_update:
                    Attendance.objects.bulk_update(
                        records_to_update,
                        fields=["employee_name", "role", "department", "salary", "intime", "outtime", "status"]
                    )

        except Exception as e:
            return Response({"detail": f"Database transaction failed: {str(e)}"}, status=400)

        return Response({
            "message": f"Successfully processed {len(employee_data)} employees.",
            "employees_created": employees_created,
            "records_saved": records_saved
        }, status=200)

    def get_queryset(self):
        user = self.request.user
        queryset = Attendance.objects.select_related("employee").all().order_by("-id")
        role = "superadmin" if user.is_superuser else user.role
        if role == "employee":
            return queryset.filter(employee__user=user)
        return queryset

    @action(detail=False, methods=["post"])
    def check_in(self, request):
        user = request.user
        try:
            employee = user.employee_profile
        except AttributeError:
            return Response({"detail": "Employee profile not found for this user."}, status=400)

        # Fetch dynamically configured work location per user
        allowed_lat = employee.work_lat
        allowed_lon = employee.work_lon

        if allowed_lat is None or allowed_lon is None:
             return Response({"detail": "Allowed location not set for this employee. Please contact HR."}, status=400)

        user_lat = request.data.get("latitude")
        user_lon = request.data.get("longitude")

        if user_lat is None or user_lon is None:
            return Response({"detail": "Latitude and Longitude are required."}, status=400)

        try:
            distance = haversine_distance(float(user_lat), float(user_lon), float(allowed_lat), float(allowed_lon))
        except ValueError:
            return Response({"detail": "Invalid coordinates provided."}, status=400)

        if distance > 50:
            return Response({
                "detail": f"You are too far from the office ({round(distance)}m). Must be within 50m."
            }, status=403)

        # Check if already checked in today
        today = timezone.now().date()
        existing = Attendance.objects.filter(employee=employee, intime__date=today).first()
        
        if existing:
             return Response({"detail": "Already checked in for today."}, status=400)

        # Create record
        attendance = Attendance.objects.create(
            employee=employee,
            employee_name=employee.employee_name,
            role=employee.role,
            department=employee.department,
            salary=employee.salary,
            intime=timezone.now(),
            status="Present"
        )
        serializer = self.get_serializer(attendance)
        return Response(serializer.data, status=201)

    @action(detail=False, methods=["post"])
    def check_out(self, request):
        user = request.user
        try:
            employee = user.employee_profile
        except AttributeError:
            return Response({"detail": "Employee profile not found for this user."}, status=400)

        allowed_lat = employee.work_lat
        allowed_lon = employee.work_lon

        if allowed_lat is None or allowed_lon is None:
             return Response({"detail": "Allowed location not set for this employee. Please contact HR."}, status=400)

        user_lat = request.data.get("latitude")
        user_lon = request.data.get("longitude")

        if user_lat is None or user_lon is None:
            return Response({"detail": "Latitude and Longitude are required."}, status=400)

        try:
            distance = haversine_distance(float(user_lat), float(user_lon), float(allowed_lat), float(allowed_lon))
        except ValueError:
            return Response({"detail": "Invalid coordinates provided."}, status=400)

        if distance > 50:
            return Response({
                "detail": f"You are too far from the office ({round(distance)}m). Must be within 50m to clock out."
            }, status=403)

        today = timezone.now().date()
        existing = Attendance.objects.filter(employee=employee, intime__date=today).first()
        
        if not existing:
             return Response({"detail": "No clock-in found for today. Cannot clock out."}, status=400)

        if existing.outtime:
             return Response({"detail": "Already clocked out for today."}, status=400)

        existing.outtime = timezone.now()
        existing.save()
        serializer = self.get_serializer(existing)
        return Response(serializer.data, status=200)


class LeaveRequestViewSet(viewsets.ModelViewSet):
    serializer_class = LeaveRequestSerializer
    permission_classes = [IsAuthenticated]

    def get_queryset(self):
        user = self.request.user
        queryset = LeaveRequest.objects.select_related("employee").all().order_by("-applied_on")
        role = "superadmin" if user.is_superuser else user.role
        if role == "employee":
            return queryset.filter(employee__user=user)
        return queryset

    def perform_create(self, serializer):
        user = self.request.user
        employee = getattr(user, "employee_profile", None)
        if employee:
            serializer.save(employee=employee)
        else:
            serializer.save()

    @action(detail=True, methods=["post"])
    def approve(self, request, pk=None):
        user = request.user
        role = "superadmin" if user.is_superuser else user.role
        if role not in ["superadmin", "admin"]:
            return Response({"detail": "Permission denied."}, status=403)
        leave = self.get_object()
        leave.status = "Approved"
        leave.save()
        return Response({"status": "Approved"})

    @action(detail=True, methods=["post"])
    def reject(self, request, pk=None):
        user = request.user
        role = "superadmin" if user.is_superuser else user.role
        if role not in ["superadmin", "admin"]:
            return Response({"detail": "Permission denied."}, status=403)
        leave = self.get_object()
        leave.status = "Rejected"
        leave.save()
        return Response({"status": "Rejected"})

    
