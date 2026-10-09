import logging

from django.conf import settings
from django.core.mail import EmailMessage
from django.template.loader import render_to_string

from .events import EventResult
from .models import InboundEmail, InboundEmailOutcomes

logger = logging.getLogger(__name__)

# Outcomes worth telling the requester about. UPDATED is only reported when something actually changed.
NOTIFIED_OUTCOMES = {
    InboundEmailOutcomes.SCHEDULED,
    InboundEmailOutcomes.UPDATED,
    InboundEmailOutcomes.CANCELLED,
    InboundEmailOutcomes.NO_MEETING_URL,
    InboundEmailOutcomes.PAST_MEETING,
    InboundEmailOutcomes.ALL_DAY_EVENT,
    InboundEmailOutcomes.BOT_CREATION_FAILED,
}

MESSAGES = {
    "fr": {
        InboundEmailOutcomes.SCHEDULED: "Je rejoindrai la réunion.",
        InboundEmailOutcomes.UPDATED: "C'est noté, j'ai pris en compte la modification.",
        InboundEmailOutcomes.CANCELLED: "Réunion annulée : je ne la rejoindrai pas.",
        InboundEmailOutcomes.NO_MEETING_URL: "Je n'ai trouvé aucun lien Zoom, Google Meet ou Microsoft Teams dans l'invitation, je ne peux pas rejoindre cette réunion.",
        InboundEmailOutcomes.PAST_MEETING: "Cette réunion est déjà terminée.",
        InboundEmailOutcomes.ALL_DAY_EVENT: "C'est un événement sur la journée entière, sans heure de début : je ne peux pas le rejoindre.",
        InboundEmailOutcomes.BOT_CREATION_FAILED: "Je n'ai pas pu me planifier pour cette réunion à cause d'une erreur technique. L'administrateur a été informé.",
        InboundEmailOutcomes.NO_INVITATION: "Je n'ai pas trouvé d'invitation de réunion dans votre message.",
        "recurring": "Réunion récurrente : je rejoindrai chacune des {count} prochaines occurrences, les suivantes seront ajoutées automatiquement.",
        "how_to": "Pour que je rejoigne une réunion, ajoutez {address} comme invité dans votre agenda, ou transférez-moi l'invitation.",
        "untitled": "(sans titre)",
        "at": "à",
        "weekdays": ["lundi", "mardi", "mercredi", "jeudi", "vendredi", "samedi", "dimanche"],
        "months": ["janvier", "février", "mars", "avril", "mai", "juin", "juillet", "août", "septembre", "octobre", "novembre", "décembre"],
    },
    "en": {
        InboundEmailOutcomes.SCHEDULED: "I will join the meeting.",
        InboundEmailOutcomes.UPDATED: "Got it, I have taken the change into account.",
        InboundEmailOutcomes.CANCELLED: "Meeting cancelled: I won't join it.",
        InboundEmailOutcomes.NO_MEETING_URL: "I couldn't find any Zoom, Google Meet or Microsoft Teams link in the invitation, so I can't join this meeting.",
        InboundEmailOutcomes.PAST_MEETING: "This meeting is already over.",
        InboundEmailOutcomes.ALL_DAY_EVENT: "This is an all-day event without a start time, so I can't join it.",
        InboundEmailOutcomes.BOT_CREATION_FAILED: "I couldn't schedule myself for this meeting because of a technical error. The administrator has been notified.",
        InboundEmailOutcomes.NO_INVITATION: "I couldn't find a meeting invitation in your message.",
        "recurring": "Recurring meeting: I will join each of the next {count} occurrences, later ones will be added automatically.",
        "how_to": "To have me join a meeting, add {address} as a guest in your calendar, or forward me the invitation.",
        "untitled": "(untitled)",
        "at": "at",
        "weekdays": ["Monday", "Tuesday", "Wednesday", "Thursday", "Friday", "Saturday", "Sunday"],
        "months": ["January", "February", "March", "April", "May", "June", "July", "August", "September", "October", "November", "December"],
    },
}


def _messages():
    return MESSAGES.get(settings.MAILBOT_REPLY_LANGUAGE, MESSAGES["en"])


def _format_start(start, messages) -> str:
    """Formatted in the invitation's own timezone, without depending on the server locale."""
    if start is None:
        return ""
    offset = start.strftime("%z")
    offset = f"UTC{offset[:3]}:{offset[3:]}" if offset else "UTC"
    return f"{messages['weekdays'][start.weekday()]} {start.day} {messages['months'][start.month - 1]} {start.year} {messages['at']} {start:%H:%M} ({offset})"


def should_reply(results: list[EventResult], email_outcome: str) -> bool:
    if not settings.MAILBOT_SEND_REPLIES:
        return False
    if email_outcome == InboundEmailOutcomes.NO_INVITATION:
        return True
    return any(result.outcome in NOTIFIED_OUTCOMES and result.changed for result in results)


def send_reply(inbound_email: InboundEmail, original_message_id: str, results: list[EventResult], email_outcome: str):
    messages = _messages()
    items = []
    for result in results:
        if result.outcome not in NOTIFIED_OUTCOMES:
            continue
        text = messages[result.outcome]
        if result.occurrence_count and result.outcome in (InboundEmailOutcomes.SCHEDULED, InboundEmailOutcomes.UPDATED):
            text = messages["recurring"].format(count=result.occurrence_count)
        items.append({"summary": result.summary or messages["untitled"], "start": _format_start(result.start, messages), "text": text})

    body = render_to_string(
        f"mailbot/reply_{settings.MAILBOT_REPLY_LANGUAGE if settings.MAILBOT_REPLY_LANGUAGE in MESSAGES else 'en'}.txt",
        {
            "items": items,
            "general": messages[InboundEmailOutcomes.NO_INVITATION] if email_outcome == InboundEmailOutcomes.NO_INVITATION else None,
            "how_to": messages["how_to"].format(address=settings.MAILBOT_ADDRESS),
            "bot_name": settings.MAILBOT_BOT_NAME,
        },
    )
    subject = inbound_email.subject or ""
    if not subject.lower().startswith("re:"):
        subject = f"Re: {subject}".strip()

    reply = EmailMessage(
        subject=subject,
        body=body,
        from_email=settings.MAILBOT_ADDRESS or settings.DEFAULT_FROM_EMAIL,
        to=[inbound_email.sender],
        # RFC 3834 headers so that the requester's auto-responders don't answer back
        headers={"Auto-Submitted": "auto-replied", "In-Reply-To": original_message_id, "References": original_message_id},
    )
    reply.send(fail_silently=False)
