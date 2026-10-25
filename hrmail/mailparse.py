"""Reading one mail: who sent it, what it says, what it carries, and whether it
is safe to show and to answer. Pure functions -- no network, no database --
so every rule here is tested on its own. Ported from OpenCall's customer mail
(htmlSanitizer.ts, emailMatcher.ts, composeValidator.ts), which has run on the
region mailboxes for months.
"""
import html as html_lib
import re
from email import policy
from email.parser import BytesParser
from email.utils import getaddresses, parseaddr, parsedate_to_datetime

# ------------------------------------------------------------------- HTML
# Mail bodies are written by anyone in the world and shown to HR with their
# session open. The reading pane puts them in a sandboxed iframe with scripting
# off -- that is what stops execution -- and this pass is the second layer, so a
# mistake in either alone is not enough. It strips, it does not rewrite: tables,
# inline styles, fonts and colours survive, so the mail looks as it was sent.

FORBIDDEN_ELEMENTS = (
    "script", "iframe", "frame", "frameset", "object", "embed", "applet", "link",
    "meta", "base", "form", "input", "button", "textarea", "select", "svg",
)
DANGEROUS_URL = re.compile(r"^\s*(?:javascript|vbscript|data|file)\s*:", re.I)
# A picture written into the mail itself (Outlook does this for pasted images).
# Kept in an <img>, so the mail looks as it was sent; anywhere else a data: URL
# is still removed. SVG is left out: it is a document, not a picture.
IMAGE_DATA_URL = re.compile(r"^\s*data:image/(?:png|jpe?g|gif|webp|bmp);base64,", re.I)
_URL_ATTR = re.compile(
    r"""\s(href|src|xlink:href|action|formaction)\s*=\s*(?:"([^"]*)"|'([^']*)'|([^\s>]+))""", re.I
)
_STYLE_ATTR = re.compile(r"""\sstyle\s*=\s*(?:"([^"]*)"|'([^']*)')""", re.I)
_HANDLER = re.compile(r"""\son[a-z]+\s*=\s*(?:"[^"]*"|'[^']*'|[^\s>]+)""", re.I)


def _strip_element(html, tag):
    html = re.sub(rf"<{tag}\b[\s\S]*?</{tag}\s*>", "", html, flags=re.I)
    return re.sub(rf"</?{tag}\b[^>]*>", "", html, flags=re.I)


def _clean_tag(tag):
    tag = _HANDLER.sub("", tag)
    is_img = tag[:4].lower() == "<img"

    def url(match):
        value = match.group(2) or match.group(3) or match.group(4) or ""
        if is_img and match.group(1).lower() == "src" and IMAGE_DATA_URL.match(value):
            return match.group(0)
        return "" if DANGEROUS_URL.match(value) else match.group(0)

    def css(match):
        value = match.group(1) or match.group(2) or ""
        return "" if re.search(r"expression\s*\(|@import|javascript\s*:", value, re.I) else match.group(0)

    return _STYLE_ATTR.sub(css, _URL_ATTR.sub(url, tag))


def sanitize_html(html):
    out = str(html or "")
    if not out.strip():
        return ""
    # Comments can hide a closing tag and reopen parsing somewhere unexpected.
    out = re.sub(r"<!--[\s\S]*?-->", "", out)
    for tag in FORBIDDEN_ELEMENTS:
        out = _strip_element(out, tag)
    out = re.sub(r"@import[^;]*;?", "", out, flags=re.I)
    out = re.sub(r"expression\s*\([^)]*\)", "", out, flags=re.I)
    return re.sub(r"<[a-z][^>]*>", lambda m: _clean_tag(m.group(0)), out, flags=re.I)


def html_to_text(html):
    """Readable text out of HTML, for the list's preview and as a fallback."""
    text = re.sub(r"(?is)<(style|script|head)\b.*?</\1\s*>", " ", str(html or ""))
    text = re.sub(r"(?i)<br\s*/?>|</p\s*>|</div\s*>|</tr\s*>|</li\s*>", "\n", text)
    text = re.sub(r"<[^>]+>", " ", text)
    text = html_lib.unescape(text)
    text = re.sub(r"[ \t\r\f\v]+", " ", text)
    return re.sub(r"\n\s*\n+", "\n\n", text).strip()


def to_preview(text, limit=200):
    return re.sub(r"\s+", " ", str(text or "")).strip()[:limit]


def looks_like_markup(text):
    """A "plain text" part that is really HTML or CSS -- some senders' mailers
    put the page source there -- and so is no use as the mail's text."""
    head = str(text or "")[:400]
    return bool(
        re.match(r"\s*<(?:!doctype|html|head|body|table|div|meta|style)\b", head, re.I)
        or re.search(r"[@.#a-z][^{}\n]{0,80}\{[^{}]*:[^{}]*;", head, re.I)
    )


# ------------------------------------------------------------------- who may be answered

def is_auto_generated(headers):
    """Auto-replies, bulk mail and mailing lists. headers: lower-cased names."""
    def get(name):
        return str(headers.get(name, "") or "").strip().lower()

    auto_submitted = get("auto-submitted")
    if auto_submitted and auto_submitted != "no":
        return True
    if get("x-autoreply") or get("x-autorespond") or get("x-auto-response-suppress"):
        return True
    if get("precedence") in ("bulk", "auto_reply", "junk", "list"):
        return True
    return bool(get("list-id") or get("list-unsubscribe"))


def is_no_reply_sender(email):
    local = str(email or "").split("@")[0]
    return bool(re.search(
        r"(^|[._-])(no[-._]?reply|do[-._]?not[-._]?reply|mailer[-._]?daemon|postmaster|bounce)", local, re.I
    ))


# ------------------------------------------------------------------- addresses typed by HR

MAX_RECIPIENTS = 25
MAX_ATTACHMENT_TOTAL_BYTES = 20 * 1024 * 1024
MAX_SUBJECT_LENGTH = 300
MAX_BODY_LENGTH = 50_000
_EMAIL = re.compile(r"^[^\s@,;<>]+@[^\s@,;<>]+\.[a-z]{2,}$", re.I)


def _split_outside_brackets(raw):
    pieces, current, inside = [], "", False
    for char in raw:
        if char == "<":
            inside = True
        elif char == ">":
            inside = False
        if not inside and char in ",;\n\r":
            pieces.append(current)
            current = ""
            continue
        current += char
    pieces.append(current)
    return pieces


def parse_recipients(raw):
    """A recipient field as people type one: commas, semicolons or new lines
    between, and "Name <addr>" gives just the address. A bare token before an
    angled address is the unquoted half of a name ("Sharma, Bhupesh <...>")."""
    seen, out = set(), []
    tokens = _split_outside_brackets(str(raw or ""))
    for index, token in enumerate(tokens):
        token = token.strip()
        if not token:
            continue
        angled = re.search(r"<([^>]*)>", token)
        if angled:
            value = angled.group(1)
        elif "@" not in token and index + 1 < len(tokens) and re.search(r"<[^>]*>", tokens[index + 1]):
            continue
        else:
            value = token
        cleaned = value.strip().strip("\"'").strip().lower()
        if cleaned and cleaned not in seen:
            seen.add(cleaned)
            out.append(cleaned)
    return out


def check_compose(to, cc, subject, body, attachment_bytes=0):
    """(error or None, to list, cc list). The only checks between a typed
    address and a mail leaving under the company's name, so they are strict."""
    to_list, cc_list = parse_recipients(to), parse_recipients(cc)
    if not to_list:
        return "Add at least one address in To.", to_list, cc_list
    bad = [address for address in to_list + cc_list if not _EMAIL.match(address)]
    if bad:
        return f"Not a valid email address: {', '.join(bad[:3])}", to_list, cc_list
    if len(to_list) + len(cc_list) > MAX_RECIPIENTS:
        return f"At most {MAX_RECIPIENTS} people in one mail.", to_list, cc_list
    if not str(subject or "").strip():
        return "Add a subject.", to_list, cc_list
    if len(str(subject)) > MAX_SUBJECT_LENGTH:
        return f"The subject can be at most {MAX_SUBJECT_LENGTH} characters.", to_list, cc_list
    if not str(body or "").strip():
        return "The mail is empty.", to_list, cc_list
    if len(str(body)) > MAX_BODY_LENGTH:
        return "The mail is too long.", to_list, cc_list
    if attachment_bytes > MAX_ATTACHMENT_TOTAL_BYTES:
        return "Attachments can be at most 20 MB together.", to_list, cc_list
    return None, to_list, cc_list


# ------------------------------------------------------------------- the message itself

def _header(message, name):
    try:
        value = message.get(name)
        return str(value) if value is not None else ""
    except Exception:  # noqa: BLE001 - a broken header must not cost the mail
        return ""


def _addresses(message, name):
    pairs = getaddresses([_header(message, name)])
    return ", ".join(
        f"{display} <{address}>" if display else address for display, address in pairs if address
    )


def _content(part):
    try:
        return part.get_content()
    except Exception:  # noqa: BLE001 - unknown charset, broken encoding
        payload = part.get_payload(decode=True) or b""
        return payload.decode("utf-8", "replace")


def parse_message(raw):
    """Everything the mailbox needs from one raw message."""
    message = BytesParser(policy=policy.default).parsebytes(raw)
    headers = {}
    for name in message.keys():
        headers.setdefault(name.lower(), _header(message, name))

    from_name, from_email = parseaddr(_header(message, "From"))
    try:
        sent_at = parsedate_to_datetime(_header(message, "Date")) if _header(message, "Date") else None
    except (TypeError, ValueError, IndexError):
        sent_at = None

    text_part = message.get_body(preferencelist=("plain",))
    html_part = message.get_body(preferencelist=("html",))
    body_text = _content(text_part) if text_part is not None else ""
    body_html = _content(html_part) if html_part is not None else ""
    # What the mail reads as, from what the reader actually sees: its HTML.
    html_text = html_to_text(body_html) if body_html else ""
    if html_text and (not body_text or looks_like_markup(body_text)):
        body_text = html_text

    attachments = []
    for part in message.walk():
        if part.is_multipart() or part is text_part or part is html_part:
            continue
        disposition = part.get_content_disposition()
        filename = part.get_filename() or ""
        content_id = _header(part, "Content-ID").strip().strip("<>")
        if disposition != "attachment" and not filename and not content_id:
            continue  # an alternative body we did not choose
        data = part.get_payload(decode=True) or b""
        is_inline = bool(content_id) and part.get_content_maintype() == "image" and disposition != "attachment"
        attachments.append({
            "filename": (filename or f"attachment-{len(attachments) + 1}")[:255],
            "content_type": part.get_content_type()[:120],
            "content_id": content_id[:255],
            "is_inline": is_inline,
            "data": data,
        })

    return {
        "headers": headers,
        "message_id": _header(message, "Message-ID").strip()[:255],
        "references": _header(message, "References").strip(),
        "in_reply_to": _header(message, "In-Reply-To").strip()[:255],
        "from_email": (from_email or "").strip().lower()[:254],
        "from_name": (from_name or "").strip()[:200],
        "to_text": _addresses(message, "To"),
        "cc_text": _addresses(message, "Cc"),
        "subject": _header(message, "Subject").strip()[:500],
        "sent_at": sent_at,
        "body_text": body_text,
        "preview": to_preview(html_text or body_text),
        "body_html": sanitize_html(body_html),
        "attachments": attachments,
    }
