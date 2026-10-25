"""The first mails copied in kept the sender's "plain text" as their preview,
and some senders put the page's HTML or CSS there -- the inbox showed
'<!DOCTYPE html PUBLIC ...'. Their preview (and that text, where it is such
markup) is worked out again from the HTML the reader sees. Nothing else in a
row is touched. The rules are copied here, not imported, so this migration
means the same thing however mailparse changes later.
"""
import html as html_lib
import re

from django.db import migrations


def _html_to_text(html):
    text = re.sub(r"(?is)<(style|script|head)\b.*?</\1\s*>", " ", str(html or ""))
    text = re.sub(r"(?i)<br\s*/?>|</p\s*>|</div\s*>|</tr\s*>|</li\s*>", "\n", text)
    text = re.sub(r"<[^>]+>", " ", text)
    text = html_lib.unescape(text)
    text = re.sub(r"[ \t\r\f\v]+", " ", text)
    return re.sub(r"\n\s*\n+", "\n\n", text).strip()


def _looks_like_markup(text):
    head = str(text or "")[:400]
    return bool(
        re.match(r"\s*<(?:!doctype|html|head|body|table|div|meta|style)\b", head, re.I)
        or re.search(r"[@.#a-z][^{}\n]{0,80}\{[^{}]*:[^{}]*;", head, re.I)
    )


def readable_previews(apps, schema_editor):
    InboundMail = apps.get_model("hrmail", "InboundMail")
    for mail in InboundMail.objects.exclude(body_html="").iterator():
        html_text = _html_to_text(mail.body_html)
        if not html_text:
            continue
        fields = []
        preview = re.sub(r"\s+", " ", html_text).strip()[:200]
        if preview != mail.preview:
            mail.preview = preview
            fields.append("preview")
        if not mail.body_text or _looks_like_markup(mail.body_text):
            mail.body_text = html_text
            fields.append("body_text")
        if fields:
            mail.save(update_fields=fields)


class Migration(migrations.Migration):

    dependencies = [
        ("hrmail", "0001_initial"),
    ]

    operations = [
        migrations.RunPython(readable_previews, migrations.RunPython.noop),
    ]
