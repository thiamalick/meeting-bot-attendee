import secrets
import string

from django.db import models

from bots.models import Calendar, CalendarEvent, Project
from bots.storage import StorageAlias


class InboundEmailSources(models.TextChoices):
    IMAP = "imap"
    HTTP = "http"


class InboundEmailStates(models.IntegerChoices):
    RECEIVED = 1, "Received"
    PROCESSED = 2, "Processed"
    IGNORED = 3, "Ignored"
    FAILED = 4, "Failed"


class InboundEmailOutcomes(models.TextChoices):
    """Machine readable decision taken for an inbound email. The human readable explanation is in InboundEmail.decision_reason."""

    SCHEDULED = "scheduled"
    UPDATED = "updated"
    CANCELLED = "cancelled"
    PARTIAL = "partial"
    NO_INVITATION = "no_invitation"
    NO_MEETING_URL = "no_meeting_url"
    PAST_MEETING = "past_meeting"
    ALL_DAY_EVENT = "all_day_event"
    STALE_UPDATE = "stale_update"
    CALENDAR_REPLY = "calendar_reply"
    BOT_CREATION_FAILED = "bot_creation_failed"
    UNAUTHORIZED_SENDER = "unauthorized_sender"
    UNAUTHENTICATED_SENDER = "unauthenticated_sender"
    AUTO_SUBMITTED = "auto_submitted"
    LOOP = "loop"
    RATE_LIMITED = "rate_limited"
    TOO_LARGE = "too_large"
    ERROR = "error"


def _random_object_id(prefix):
    return prefix + "".join(secrets.choice(string.ascii_letters + string.digits) for _ in range(16))


class InboundEmail(models.Model):
    """An email received at the bot address. It's kept as an audit trail of what was decided and why."""

    OBJECT_ID_PREFIX = "inm_"
    object_id = models.CharField(max_length=32, unique=True, editable=False)

    project = models.ForeignKey(Project, on_delete=models.CASCADE, related_name="inbound_emails")
    source = models.CharField(max_length=16, choices=InboundEmailSources.choices)
    message_id = models.CharField(max_length=998)
    sender = models.CharField(max_length=320, blank=True)
    subject = models.CharField(max_length=1024, blank=True)
    size_bytes = models.IntegerField()
    # The raw RFC 822 message, deleted after MAILBOT_RAW_RETENTION_DAYS
    raw_file = models.FileField(storage=StorageAlias("recordings"), null=True, blank=True)

    state = models.IntegerField(choices=InboundEmailStates.choices, default=InboundEmailStates.RECEIVED)
    outcome = models.CharField(max_length=64, choices=InboundEmailOutcomes.choices, null=True, blank=True)
    decision_reason = models.TextField(blank=True)
    auth_results = models.JSONField(null=True, blank=True)
    # One entry per calendar component handled: event, bot and action taken
    details = models.JSONField(default=list, blank=True)
    calendar_events = models.ManyToManyField(CalendarEvent, blank=True, related_name="inbound_emails")
    reply_sent_at = models.DateTimeField(null=True, blank=True)

    created_at = models.DateTimeField(auto_now_add=True)
    processed_at = models.DateTimeField(null=True, blank=True)

    def save(self, *args, **kwargs):
        if not self.object_id:
            self.object_id = _random_object_id(self.OBJECT_ID_PREFIX)
        super().save(*args, **kwargs)

    def __str__(self):
        return f"{self.object_id} from {self.sender}"

    class Meta:
        constraints = [
            models.UniqueConstraint(fields=["project", "message_id"], name="unique_inbound_email_message_id"),
        ]
        indexes = [
            models.Index(fields=["project", "sender", "created_at"], name="inbound_email_sender_idx"),
        ]


class RecurringSeries(models.Model):
    """
    The master component of a recurring invitation. Occurrences are materialized as CalendarEvents
    over a rolling horizon, so the series is kept to extend that horizon as time passes.
    """

    calendar = models.ForeignKey(Calendar, on_delete=models.CASCADE, related_name="recurring_series")
    uid = models.CharField(max_length=1024)
    sequence = models.IntegerField(default=0)
    # A VCALENDAR holding the master VEVENT and the VTIMEZONEs it references
    ics = models.TextField()
    name = models.CharField(max_length=1024, blank=True)
    # Kept on the series because the link may only be in the email body, which the ics doesn't contain
    meeting_url = models.CharField(max_length=2048, null=True, blank=True)
    attendees = models.JSONField(default=list, blank=True)
    is_cancelled = models.BooleanField(default=False)
    horizon_end = models.DateTimeField(null=True, blank=True)
    requested_by = models.CharField(max_length=320, blank=True)

    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    def __str__(self):
        return f"Recurring series {self.uid}"

    class Meta:
        constraints = [
            models.UniqueConstraint(fields=["calendar", "uid"], name="unique_recurring_series_uid"),
        ]
