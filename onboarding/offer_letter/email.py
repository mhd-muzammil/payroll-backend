"""The mail an offer letter goes out with: HR's own mail, word for word (the
one HR sent from hr@renderways.in, "Offer Letter", 17 June 2026), with the
person's name and the role filled in and the letter attached.

Only two things change from one candidate to the next: the name after
"Dear" -- HR's mail used the short name ("Vaseem") where the letter has the
full one, so it is asked for -- and the role. The rest, signature, address,
products line and disclaimer included, is HR's text as it was.
"""
from html import escape

SUBJECT = "Offer Letter"
PINK = "#E4007C"
NAVY = "#1F3864"
SERIF = "Cambria,Georgia,'Times New Roman',serif"

# Products, as HR's signature colours them: every other one in the brand pink.
PRODUCTS = (
    ("Desktops", False), ("Laptops", True), ("Servers", False), ("Storages", True),
    ("Firewall", False), ("Workstations", True), ("Plotters", False), ("Printers", True),
    ("Software", False), ("Networking", False), ("Chip Level Services", True), ("AMC’s", False),
)

DISCLAIMER = (
    "“Renderways Technology Pvt Ltd, accepts no liability for the content of this email, or for the "
    "consequences of any actions taken on the basis of the information. The recipient acknowledges that "
    "electronic mails are susceptible to alterations and cannot be guaranteed to be secure or error free, as "
    "information could be intercepted, corrupted, lost, destroyed, arrive late or incomplete or contain "
    "viruses. Renderways therefore will not be liable for any errors, omissions, viruses, damages in the "
    "contents of this message and attachments therewith which arise as result of this transmission. This "
    "e-mail and any attachments therewith are for the sole use of the intended recipient (s) and may contain "
    "confidential and privileged information. If you are not the intended recipient, please notify the sender "
    "immediately and destroy all copies of this message and any attachments therewith. Any unauthorized use, "
    "disclosure or copying of this message and the attachments thereto (in whole or part), in any manner is "
    "strictly prohibited and may constitute unlawful act.”"
)

SIGNATURE_LINES = (
    "Best Regards,",
    "Karthikeyan D",
    "Human Resources(HR)",
)
PHONES = "+91 9087019170 | +91 95430 95480"
ADDRESS_LINES = ("#25, 1st Floor, Gandhi Street,", "Mettukuppam, Maduravoyal,", "Chennai 600 095.")


def offer_mail(dear, designation):
    """(subject, html, text) of the mail, for this name and role."""
    dear_html, designation_html = escape(dear), escape(designation)
    serif_line = f'<p style="margin:0 0 8px;font-family:{SERIF};font-size:15px;color:#000000">'
    products = " | ".join(
        f'<span style="color:{PINK}">{escape(name)}</span>' if pink else escape(name)
        for name, pink in PRODUCTS
    )
    html = f"""<div style="font-family:Arial,Helvetica,sans-serif;font-size:14px;line-height:1.5;color:#222222">
<p style="margin:0">Dear <b style="font-family:{SERIF};font-size:16px">{dear_html}</b>,</p>
<p style="margin:0">We are pleased to offer you the position of <b style="font-family:{SERIF};font-size:16px;color:{NAVY}">{designation_html}</b> at&nbsp;<b>Renderways Technology Private Limited</b>.</p>
<br><br>
<p style="margin:0 0 12px">We feel confident that you will contribute your skill and experience to the growth of our organization.</p>
<p style="margin:0 0 12px">Please confirm your acceptance of this offer letter by replying to this mail.</p>
<p style="margin:0">We look forward to welcoming you on board</p>
<br><br>
{"".join(f"{serif_line}{escape(line)}</p>" for line in SIGNATURE_LINES)}
{serif_line}&#128242; {PHONES}</p>
{serif_line}&#128231; <a href="mailto:hr@renderways.in" style="color:#1155CC">hr@renderways.in</a> | <a href="http://www.renderways.in" style="color:#1155CC">www.renderways.in</a></p>
<br>
{serif_line}<b>Renderways <span style="color:{PINK}">Technology</span> Pvt Ltd</b></p>
{"".join(f"{serif_line}{escape(line)}</p>" for line in ADDRESS_LINES)}
<br>
<p style="margin:0 0 8px;font-family:{SERIF};font-size:15px;font-weight:bold;color:#000000">Products: {products}</p>
<br>
<p style="margin:0 0 8px"><b><u>DISCLAIMER:</u></b></p>
<p style="margin:0">{escape(DISCLAIMER)}</p>
</div>"""

    text = "\n".join([
        f"Dear {dear},",
        f"We are pleased to offer you the position of {designation} at Renderways Technology Private Limited.",
        "",
        "We feel confident that you will contribute your skill and experience to the growth of our organization.",
        "",
        "Please confirm your acceptance of this offer letter by replying to this mail.",
        "",
        "We look forward to welcoming you on board",
        "",
        *SIGNATURE_LINES,
        PHONES,
        "hr@renderways.in | www.renderways.in",
        "",
        "Renderways Technology Pvt Ltd",
        *ADDRESS_LINES,
        "",
        "Products: " + " | ".join(name for name, _ in PRODUCTS),
        "",
        "DISCLAIMER:",
        DISCLAIMER,
    ])
    return SUBJECT, html, text
