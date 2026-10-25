"""The HR mailbox inside Payroll: the mail that arrives at it, the mail HR sends
from it, and the mail Payroll sends on its own.

The mailbox is the one Payroll already sends from (settings.EMAIL_HOST_USER,
hr@renderways.in), read over IMAP with the same password. Mail is copied in
from the day the section is first opened, a week back -- never the years
already in the mailbox -- and nothing here ever deletes anything on the
server.
"""
from django.conf import settings
from django.db import models


class MailboxState(models.Model):
    """How far the mailbox has been read, and whether the last read worked."""

    email = models.EmailField(unique=True)
    # Nothing older than this is copied in. Stamped on first sight.
    ingest_from = models.DateTimeField()
    uid_validity = models.BigIntegerField(null=True, blank=True)
    last_polled_at = models.DateTimeField(null=True, blank=True)
    last_error = models.CharField(max_length=500, blank=True, default="")
    sent_folder = models.CharField(max_length=200, blank=True, default="")

    def __str__(self):
        return self.email


class InboundMail(models.Model):
    """One mail that arrived at the HR mailbox. Its HTML is kept already
    stripped of anything that runs (see mailparse.sanitize_html)."""

    STATUS_CHOICES = (
        ("NEW", "Inbox"),
        ("REVIEWED", "Reviewed"),
        ("IGNORED", "Ignored"),
    )

    mailbox = models.EmailField(db_index=True)
    uid_validity = models.BigIntegerField()
    uid = models.BigIntegerField()
    message_id = models.CharField(max_length=255, blank=True, default="", db_index=True)
    in_reply_to = models.CharField(max_length=255, blank=True, default="")
    references = models.TextField(blank=True, default="")
    from_email = models.CharField(max_length=254, blank=True, default="")
    from_name = models.CharField(max_length=200, blank=True, default="")
    to_text = models.TextField(blank=True, default="")
    cc_text = models.TextField(blank=True, default="")
    subject = models.CharField(max_length=500, blank=True, default="")
    received_at = models.DateTimeField(db_index=True)
    body_text = models.TextField(blank=True, default="")
    body_html = models.TextField(blank=True, default="")
    preview = models.CharField(max_length=300, blank=True, default="")
    # Machine-made mail (auto-replies, mailing lists, no-reply senders) is shown
    # but can never be answered: that is how auto-reply loops start.
    is_auto = models.BooleanField(default=False)
    status = models.CharField(max_length=10, choices=STATUS_CHOICES, default="NEW", db_index=True)
    status_by = models.ForeignKey(
        settings.AUTH_USER_MODEL, null=True, blank=True, on_delete=models.SET_NULL, related_name="+"
    )
    status_at = models.DateTimeField(null=True, blank=True)
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        ordering = ["-received_at", "-id"]
        constraints = [
            models.UniqueConstraint(fields=["mailbox", "uid_validity", "uid"], name="hrmail_one_copy_per_uid"),
        ]

    def __str__(self):
        return f"{self.from_email}: {self.subject}"


class InboundAttachment(models.Model):
    mail = models.ForeignKey(InboundMail, on_delete=models.CASCADE, related_name="attachments")
    filename = models.CharField(max_length=255)
    content_type = models.CharField(max_length=120, blank=True, default="application/octet-stream")
    size = models.PositiveIntegerField(default=0)
    content_id = models.CharField(max_length=255, blank=True, default="")
    # A picture shown inside the mail's own HTML (a signature logo), not a file.
    is_inline = models.BooleanField(default=False)
    file = models.FileField(upload_to="hrmail/attachments/")

    def __str__(self):
        return self.filename


class OutboundMail(models.Model):
    """Mail HR sent from this section: a reply, or a new mail. Who pressed Send
    is the point -- a copy also goes into the mailbox's own Sent folder, but
    only this says which of the team sent it."""

    KIND_CHOICES = (("REPLY", "Reply"), ("COMPOSE", "New mail"), ("OFFER", "Offer letter"))
    STATUS_CHOICES = (("SENT", "Sent"), ("FAILED", "Failed"))

    kind = models.CharField(max_length=10, choices=KIND_CHOICES)
    in_reply_to = models.ForeignKey(
        InboundMail, null=True, blank=True, on_delete=models.SET_NULL, related_name="replies"
    )
    from_email = models.EmailField()
    to_text = models.TextField()
    cc_text = models.TextField(blank=True, default="")
    subject = models.CharField(max_length=500)
    body = models.TextField()
    attachment_names = models.JSONField(default=list, blank=True)
    status = models.CharField(max_length=10, choices=STATUS_CHOICES)
    error = models.CharField(max_length=500, blank=True, default="")
    message_id = models.CharField(max_length=255, blank=True, default="")
    filed_in_sent = models.BooleanField(default=False)
    sent_by = models.ForeignKey(
        settings.AUTH_USER_MODEL, null=True, blank=True, on_delete=models.SET_NULL, related_name="+"
    )
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        ordering = ["-created_at", "-id"]

    def __str__(self):
        return f"{self.kind} to {self.to_text}: {self.subject}"


class SystemMail(models.Model):
    """A mail Payroll sent, kept as it went so it can be opened again: who it
    went to, what it said, what was attached. What is stored is safe to show:
    a signing code is never written down here, in the subject or the text."""

    KIND_CHOICES = (
        ("appointment_link", "Appointment order link"),
        ("appointment_code", "Appointment order signing code"),
        ("appointment_signed", "Signed appointment order"),
        ("payslip", "Payslip"),
        ("offer_letter", "Offer letter"),
    )
    STATUS_CHOICES = (("SENT", "Sent"), ("FAILED", "Failed"))

    kind = models.CharField(max_length=30, choices=KIND_CHOICES)
    to_text = models.TextField()
    cc_text = models.TextField(blank=True, default="")
    subject = models.CharField(max_length=500)
    about = models.CharField(max_length=200, blank=True, default="")
    # The mail itself. Empty for mails recorded before it was kept.
    body_text = models.TextField(blank=True, default="")
    body_html = models.TextField(blank=True, default="")
    status = models.CharField(max_length=10, choices=STATUS_CHOICES)
    error = models.CharField(max_length=500, blank=True, default="")
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        ordering = ["-created_at", "-id"]

    def __str__(self):
        return f"{self.kind} to {self.to_text}"


class SystemMailAttachment(models.Model):
    """A file a Payroll mail carried (an offer letter, a signed order), kept
    so the mail can be opened again with it."""

    mail = models.ForeignKey(SystemMail, on_delete=models.CASCADE, related_name="attachments")
    filename = models.CharField(max_length=255)
    content_type = models.CharField(max_length=120, blank=True, default="application/octet-stream")
    size = models.PositiveIntegerField(default=0)
    file = models.FileField(upload_to="hrmail/system/")

    def __str__(self):
        return self.filename
