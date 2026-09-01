"""The payslip as a real PDF, rendered server-side.

Why this exists rather than the browser doing it: the app is a Capacitor
WebView, and `window.print()` — which is what the Download button called — does
nothing at all in an Android WebView. Nor does `<a download>` with a blob:
Capacitor registers no DownloadListener, so the WebView silently drops it.
What Capacitor DOES do (Bridge.launchIntent) is hand any URL whose host differs
from the app's to the system browser. The API lives on a different host from
the site, so a plain navigation to a PDF endpoint opens Chrome, which downloads
it properly. Hence: a URL that returns application/pdf.

The layout mirrors the on-screen document in Payslip.jsx cell for cell. Two
things there are worth naming because they look like bugs and are not:

  Total(C) and MONTHLY CTC(A+C) both EXCLUDE petrol allowance. It is shown as
  a line and is deliberately not part of either total, nor of the net.

  The three leave-balance columns are literals on screen (12/10/2 CL, zeros for
  SL and EL) — nothing in the database backs them. They are reproduced here as
  the same literals so the PDF and the screen agree; making the PDF invent
  different numbers would be worse than repeating the placeholder.
"""
import io
from decimal import Decimal

from django.utils.html import escape

# "Rs." and not the rupee sign. No font reportlab bundles carries U+20B9, and
# python:slim ships no fonts at all, so the sign renders as a black box -- which
# is what the first draft of this did, on the CTC line and the net pay line of
# somebody's payslip. Registering a TTF is possible but means trusting a font to
# exist inside the image, and a payslip that silently degrades to a box is worse
# than one that says Rs. The screen keeps the sign; only the PDF says Rs.
RUPEE = "Rs."


MONTHS = [
    "January", "February", "March", "April", "May", "June",
    "July", "August", "September", "October", "November", "December",
]


def month_label(month):
    try:
        return MONTHS[int(month) - 1]
    except (TypeError, ValueError, IndexError):
        return str(month)


def period_dates(month, year):
    """The 25th-to-24th cycle, worded exactly as the screen words it."""
    prev_month, prev_year = month - 1, year
    if prev_month == 0:
        prev_month, prev_year = 12, year - 1
    return (
        f"25 {month_label(prev_month)[:3]} {prev_year} to "
        f"24 {month_label(month)[:3]} {year}"
    )


def money(amount):
    """1,234.50 — grouped the Indian way, which is not every three digits."""
    value = Decimal(str(amount or 0)).quantize(Decimal("0.01"))
    negative = value < 0
    whole, _, frac = f"{abs(value):.2f}".partition(".")
    if len(whole) > 3:
        head, tail = whole[:-3], whole[-3:]
        groups = []
        while len(head) > 2:
            groups.insert(0, head[-2:])
            head = head[:-2]
        if head:
            groups.insert(0, head)
        whole = ",".join(groups + [tail])
    return ("-" if negative else "") + f"{whole}.{frac}"


def money_or_dash(amount):
    return money(amount) if Decimal(str(amount or 0)) > 0 else "-"


def slip_date(value):
    return value.strftime("%d-%m-%Y") if value else "\u2014"


def mask_account(number):
    if not number:
        return "N/A"
    text = str(number).strip()
    if len(text) <= 4:
        return text
    return "*" * (len(text) - 4) + text[-4:]


def _onboarding_for(employee):
    """The bank / DOB details, which live on Onboarding rather than Employee.

    Matched the same way EmployeeSerializer matches them: email first because
    it is unique and reliable, then emp_code qualified by branch, because
    emp_code is not unique across branches.
    """
    from onboarding.models import Onboarding

    record = None
    if employee.email:
        record = Onboarding.objects.filter(email_id__iexact=employee.email).first()
    if not record and employee.emp_code:
        qs = Onboarding.objects.filter(employee_id=employee.emp_code)
        if employee.branch:
            qs = qs.filter(work_location__iexact=employee.branch)
        record = qs.first()
    return record


def _leave_grid(cl_value):
    """One of the three CL/SL/EL blocks in the leave matrix."""
    return (
        '<table class="grid">'
        '<tr><td class="g">CL</td><td class="g">SL</td><td class="gl">EL</td></tr>'
        f'<tr><td class="v">{cl_value}</td><td class="v">0.0</td>'
        '<td class="vl">0.0</td></tr>'
        "</table>"
    )


def build_payslip_html(payslip):
    emp = payslip.employee
    onboarding = _onboarding_for(emp)

    dob = onboarding.dob.strftime("%d-%m-%Y") if onboarding and onboarding.dob else "\u2014"
    bank_name = (onboarding.bank_name if onboarding else None) or "\u2014"
    account = mask_account(onboarding.account_number if onboarding else None)
    work_location = (onboarding.work_location if onboarding else None) or emp.branch or "\u2014"

    d = lambda v: escape(str(v)) if v not in (None, "") else "\u2014"  # noqa: E731

    # Total(C) and the CTC line below it both leave petrol out. See the header.
    employer_total = (
        Decimal(str(payslip.employer_epf or 0))
        + Decimal(str(payslip.employer_esi or 0))
        + Decimal(str(payslip.employer_insurance or 0))
    )
    monthly_ctc = Decimal(str(payslip.gross_earnings or 0)) + employer_total

    casual_pay = Decimal(str(payslip.casual_leave_pay or 0))
    special_pay = Decimal(str(payslip.special_work_pay or 0))

    extra_rows = ""
    if casual_pay > 0:
        days = Decimal(str(payslip.casual_leave_used or 0)).normalize()
        extra_rows += (
            '<tr><td class="lbl">Casual Leave ({d} day{s})</td>'
            '<td class="num">-</td><td class="num pos">+{amount}</td></tr>'
        ).format(d=days, s="" if days == 1 else "s", amount=money(casual_pay))
    if special_pay > 0:
        days = Decimal(str(payslip.special_work_days or 0)).normalize()
        extra_rows += (
            '<tr><td class="lbl">Special Work ({d} day{s})</td>'
            '<td class="num">-</td><td class="num pos">+{amount}</td></tr>'
        ).format(d=days, s="" if days == 1 else "s", amount=money(special_pay))
    if casual_pay > 0 or special_pay > 0:
        total_earnings = Decimal(str(payslip.gross_earnings or 0)) + casual_pay + special_pay
        extra_rows += (
            '<tr class="tot"><td>Total Earnings</td><td class="num"></td>'
            f'<td class="num">{money(total_earnings)}</td></tr>'
        )

    return f"""<!DOCTYPE html>
<html><head><meta charset="utf-8" />
<style>
  @page {{ size: a4 portrait; margin: 12mm 8mm; }}
  body {{ font-family: Helvetica; font-size: 8pt; color: #000; }}
  table {{ border-collapse: collapse; width: 100%; }}
  td, th {{ border: 1px solid #000; padding: 3px 5px; vertical-align: middle; }}
  .no-border td {{ border: none; }}
  .head {{ border: 1px solid #000; text-align: center; padding: 6px; }}
  .co {{ font-size: 14pt; font-weight: bold; color: #1e3a8a; }}
  .co .t {{ color: #db2777; }}
  .addr {{ font-size: 7pt; color: #1e3a8a; }}
  .forwhat {{ font-size: 8.5pt; font-weight: bold; }}
  .period {{ font-size: 7.5pt; color: #4b5563; }}
  .conf {{ font-size: 6.5pt; font-weight: bold; text-align: right; }}
  .lbl {{ font-weight: bold; background-color: #fafafa; }}
  .key {{ font-weight: bold; background-color: #f3f4f6; width: 18%; }}
  .val {{ width: 32%; }}
  .num {{ text-align: right; }}
  .ctr {{ text-align: center; font-weight: bold; }}
  .hdr {{ font-weight: bold; background-color: #f3f4f6; text-align: center; }}
  .tot {{ font-weight: bold; background-color: #f3f4f6; }}
  .red {{ color: #b91c1c; }}
  .pos {{ color: #047857; font-weight: bold; }}
  .grid td {{ border: none; border-right: 1px solid #000; padding: 2px; text-align: center;
              font-size: 7pt; }}
  .grid .gl, .grid .vl {{ border-right: none; }}
  .grid .g, .grid .gl {{ border-bottom: 1px solid #000; font-weight: bold; }}
  .net {{ background-color: #111827; color: #ffffff; font-weight: bold; font-size: 11pt; }}
  .ctc {{ background-color: #fffbeb; color: #92400e; font-weight: bold; }}
  .foot {{ text-align: center; font-size: 6.5pt; font-style: italic; font-weight: bold;
           color: #6b7280; border-top: 2px solid #000; }}
</style></head>
<body>

<table>
  <tr><td class="head">
    <div class="conf">PRIVATE &amp; CONFIDENTIAL</div>
    <div class="co">Renderways <span class="t">Technology</span> Pvt Ltd</div>
    <div class="addr">#25, 1st Floor, Gandhi Street, Mettukuppam, Maduravoyal, Chennai 600 095.</div>
    <div class="forwhat">Pay Slip Cum Leave Card for the month of {month_label(payslip.month)} {payslip.year}</div>
    <div class="period">Calculation Period: {period_dates(payslip.month, payslip.year)}</div>
  </td></tr>
</table>

<table>
  <tr><td class="key">Employee Name</td><td class="val"><b>{escape((emp.employee_name or '').upper()) or '&#8212;'}</b></td>
      <td class="key">Employee Code</td><td class="val">{d(emp.emp_code)}</td></tr>
  <tr><td class="key">DOJ</td><td class="val">{slip_date(emp.date_of_joining)}</td>
      <td class="key">DOB</td><td class="val">{dob}</td></tr>
  <tr><td class="key">Department</td><td class="val">{d(emp.department)}</td>
      <td class="key">Pan No.</td><td class="val">&#8212;</td></tr>
  <tr><td class="key">Designation</td><td class="val">{d(emp.role)}</td>
      <td class="key">Paymode</td><td class="val">Bank Transfer</td></tr>
  <tr><td class="key">Location</td><td class="val">{escape(str(work_location))}</td>
      <td class="key">Bank Name</td><td class="val">{escape(str(bank_name))}</td></tr>
  <tr><td class="key">Region</td><td class="val">{d(emp.branch)}</td>
      <td class="key">Bank Account No</td><td class="val">{escape(account)}</td></tr>
  <tr><td class="key">PF Number</td><td class="val">-</td>
      <td class="key">ESI Number</td><td class="val">-</td></tr>
  <tr><td class="key">UAN Number</td><td class="val">-</td>
      <td class="key">CTC</td><td class="val"><b>{RUPEE}{money(payslip.gross_salary)}</b></td></tr>
</table>

<table>
  <tr>
    <td class="lbl" rowspan="2" style="width:22%; text-align:center;">Leave Days</td>
    <td class="hdr">Ope Bal</td><td class="hdr">Avl Bal</td><td class="hdr">Clo Bal</td>
    <td class="key">Total Days</td><td class="ctr">{payslip.total_days}</td>
  </tr>
  <tr>
    <td style="padding:0">{_leave_grid("12.0")}</td>
    <td style="padding:0">{_leave_grid("10.0")}</td>
    <td style="padding:0">{_leave_grid("2.0")}</td>
    <td class="key red">No of Lop Days</td>
    <td class="ctr red">{Decimal(str(payslip.lop_days or 0)):.2f}</td>
  </tr>
  <tr>
    <td colspan="4" style="border:none"></td>
    <td class="key">No of Days</td><td class="ctr">{Decimal(str(payslip.paid_days or 0)):.2f}</td>
  </tr>
  <tr>
    <td colspan="4" style="border:none"></td>
    <td class="key" style="background-color:#ecfdf5; color:#047857">Special Work</td>
    <td class="ctr" style="color:#047857">{Decimal(str(payslip.special_work_days or 0)):.2f}</td>
  </tr>
</table>

<table>
  <tr><td style="padding:0; width:58%; border:none; vertical-align:top">
    <table>
      <tr><td class="hdr" style="width:40%">Salary/Wages</td>
          <td class="hdr" style="width:30%">Gross Salary</td>
          <td class="hdr">Gross Earnings</td></tr>
      <tr><td class="lbl">Basic</td><td class="num">{money(payslip.gross_basic)}</td><td class="num"><b>{money(payslip.earned_basic)}</b></td></tr>
      <tr><td class="lbl">HRA</td><td class="num">{money(payslip.gross_hra)}</td><td class="num"><b>{money(payslip.earned_hra)}</b></td></tr>
      <tr><td class="lbl">Conveyance</td><td class="num">{money(payslip.gross_conveyance)}</td><td class="num"><b>{money(payslip.earned_conveyance)}</b></td></tr>
      <tr><td class="lbl">Child Edu Allowance</td><td class="num">{money(payslip.gross_child_edu)}</td><td class="num"><b>{money(payslip.earned_child_edu)}</b></td></tr>
      <tr><td class="lbl">Personal Allowance</td><td class="num">{money(payslip.gross_personal_allowance)}</td><td class="num"><b>{money(payslip.earned_personal_allowance)}</b></td></tr>
      <tr><td class="lbl">Incentive</td><td class="num">{money(payslip.gross_incentive)}</td><td class="num"><b>{money(payslip.earned_incentive)}</b></td></tr>
      <tr><td class="lbl">Other Earnings</td><td class="num">{money(payslip.gross_other_earnings)}</td><td class="num"><b>{money(payslip.earned_other_earnings)}</b></td></tr>
      <tr class="tot"><td>Gross Salary / Earnings</td><td class="num">{money(payslip.gross_salary)}</td><td class="num">{money(payslip.gross_earnings)}</td></tr>
      {extra_rows}
    </table>
  </td>
  <td style="padding:0; border:none; vertical-align:top">
    <table>
      <tr><td class="hdr" style="width:60%">Gross Deduction</td><td class="hdr">Amount</td></tr>
      <tr><td class="lbl">EPF (12%)</td><td class="num red"><b>{money(payslip.deduction_epf)}</b></td></tr>
      <tr><td class="lbl">ESI</td><td class="num red">{money_or_dash(payslip.deduction_esi)}</td></tr>
      <tr><td class="lbl">Insurance</td><td class="num red">{money_or_dash(payslip.deduction_insurance)}</td></tr>
      <tr><td class="lbl">Professional Tax</td><td class="num red"><b>{money(payslip.deduction_prof_tax)}</b></td></tr>
      <tr><td class="lbl">LWF</td><td class="num red">{money_or_dash(payslip.deduction_lwf)}</td></tr>
      <tr><td class="lbl">Staff Advance</td><td class="num red">{money_or_dash(payslip.deduction_staff_advance)}</td></tr>
      <tr><td class="lbl">TDS</td><td class="num red">{money_or_dash(payslip.deduction_tds)}</td></tr>
      <tr><td class="lbl">Other Deduction</td><td class="num red">{money_or_dash(payslip.deduction_other)}</td></tr>
      <tr class="tot"><td>Total Deductions</td><td class="num red">{money(payslip.gross_deductions)}</td></tr>
    </table>
  </td></tr>
</table>

<table>
  <tr><td class="hdr" style="width:50%">Benefits (Cost to Company Components)</td>
      <td class="hdr" style="width:25%">Contribution / Allowance</td>
      <td class="hdr" style="width:25%">Total(C)</td></tr>
  <tr><td class="lbl">Employer EPF Contribution</td><td class="num">{money_or_dash(payslip.employer_epf)}</td>
      <td class="num" rowspan="4" style="vertical-align:middle"><b>{money(employer_total)}</b></td></tr>
  <tr><td class="lbl">Employer ESI Contribution</td><td class="num">{money_or_dash(payslip.employer_esi)}</td></tr>
  <tr><td class="lbl">Insurance (Employer Contribution)</td><td class="num">{money_or_dash(payslip.employer_insurance)}</td></tr>
  <tr><td class="lbl">Petrol allowance</td><td class="num">{money_or_dash(payslip.petrol_allowance)}</td></tr>
  <tr class="ctc"><td colspan="2">MONTHLY CTC(A+C)</td><td class="num">{RUPEE}{money(monthly_ctc)}</td></tr>
</table>

<table>
  <tr class="net"><td style="width:70%">NET TAKE HOME SALARY</td>
      <td class="num net">{RUPEE}{money(payslip.net_salary)}</td></tr>
  <tr><td colspan="2" class="foot">** This is a computer generated salary slip, signature is not required **</td></tr>
</table>

</body></html>"""


def payslip_filename(payslip):
    name = "".join(
        ch if ch.isalnum() else "-" for ch in (payslip.employee.employee_name or "payslip")
    ).strip("-")
    return f"Payslip-{name}-{month_label(payslip.month)}-{payslip.year}.pdf"


def render_payslip_pdf(payslip):
    """The payslip as PDF bytes. Raises RuntimeError if the renderer fails."""
    from xhtml2pdf import pisa

    buffer = io.BytesIO()
    result = pisa.CreatePDF(
        src=build_payslip_html(payslip),
        dest=buffer,
        encoding="utf-8",
        # Nothing on this page loads a remote resource, and a payslip endpoint
        # must not be turned into a fetcher by anything that slips into the
        # markup later.
        link_callback=lambda uri, rel: None,
    )
    if result.err:
        raise RuntimeError(f"payslip PDF render failed ({result.err} errors)")
    return buffer.getvalue()
