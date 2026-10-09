import logging
from datetime import timedelta

from django.conf import settings
from django.core.files.storage import storages
from django.utils import timezone

from .authorization import authorize_sender
from .events import EventResult, get_mailbot_calendar, handle_component
from .ical import parse_invitation
from .message import authentication_results, body_text, calendar_payloads, is_auto_submitted, message_id, parse_message
from .models import InboundEmail, InboundEmailOutcomes, InboundEmailStates
from .replies import send_reply, should_reply

logger = logging.getLogger(__name__)

# iTIP methods that schedule or cancel meetings. REPLY, COUNTER... are answers from attendees and are ignored.
SCHEDULING_METHODS = {"REQUEST", "PUBLISH", "ADD", "CANCEL"}
SUCCESS_OUTCOMES = {InboundEmailOutcomes.SCHEDULED, InboundEmailOutcomes.UPDATED, InboundEmailOutcomes.CANCELLED}


def _finish(inbound_email: InboundEmail, state, outcome, reason, results: list[EventResult] = ()):
    inbound_email.state = state
    inbound_email.outcome = outcome
    inbound_email.decision_reason = reason
    inbound_email.details = [result.as_detail() for result in results]
    inbound_email.processed_at = timezone.now()
    inbound_email.save()
    events = [result.calendar_event for result in results if result.calendar_event]
    if events:
        inbound_email.calendar_events.add(*events)
    logger.info(f"Mailbot processed {inbound_email.object_id} from {inbound_email.sender}: {outcome} ({reason})")


def _reply_once(inbound_email: InboundEmail, original_message_id: str, results: list[EventResult], outcome: str):
    # Saved right away so that a retry of the task after a later failure doesn't send the reply twice
    if inbound_email.reply_sent_at:
        return
    send_reply(inbound_email, original_message_id, results, outcome)
    inbound_email.reply_sent_at = timezone.now()
    inbound_email.save(update_fields=["reply_sent_at"])


def _email_outcome(results: list[EventResult]) -> tuple[int, str, str]:
    successes = [result for result in results if result.outcome in SUCCESS_OUTCOMES]
    reason = " ".join(dict.fromkeys(result.reason for result in results if result.reason))
    if successes and len(successes) < len(results):
        return InboundEmailStates.PROCESSED, InboundEmailOutcomes.PARTIAL, reason
    if successes:
        return InboundEmailStates.PROCESSED, results[0].outcome, reason
    if any(result.outcome == InboundEmailOutcomes.BOT_CREATION_FAILED for result in results):
        return InboundEmailStates.FAILED, InboundEmailOutcomes.BOT_CREATION_FAILED, reason
    return InboundEmailStates.IGNORED, results[0].outcome, reason


def process_inbound_email(inbound_email: InboundEmail):
    """Decides what to do with a received email and does it. Safe to run again on the same email."""
    with storages["recordings"].open(inbound_email.raw_file.name, "rb") as raw_file:
        raw = raw_file.read()
    message = parse_message(raw)
    original_message_id = message_id(message, raw)

    # Never act on automatic messages (out of office, delivery reports...), and never answer them to avoid mail loops
    if is_auto_submitted(message):
        return _finish(inbound_email, InboundEmailStates.IGNORED, InboundEmailOutcomes.AUTO_SUBMITTED, "The email was generated automatically.")

    inbound_email.auth_results = authentication_results(message, settings.MAILBOT_TRUSTED_AUTHSERV_ID)
    decision = authorize_sender(inbound_email.sender or None, inbound_email.auth_results)
    if not decision.allowed:
        # No reply: answering unauthorized or spoofed senders would turn the bot into a backscatter source
        return _finish(inbound_email, InboundEmailStates.IGNORED, decision.outcome, decision.reason)

    recent_emails = InboundEmail.objects.filter(project=inbound_email.project, sender=inbound_email.sender, created_at__gte=timezone.now() - timedelta(hours=1)).exclude(id=inbound_email.id)
    if recent_emails.count() >= settings.MAILBOT_MAX_EMAILS_PER_SENDER_PER_HOUR:
        return _finish(inbound_email, InboundEmailStates.IGNORED, InboundEmailOutcomes.RATE_LIMITED, f"The sender sent more than {settings.MAILBOT_MAX_EMAILS_PER_SENDER_PER_HOUR} emails in the last hour.")

    fallback_text = body_text(message)
    components = [component for payload in calendar_payloads(message) for component in parse_invitation(payload, fallback_text)]
    scheduling_components = [component for component in components if component.method in SCHEDULING_METHODS]

    if components and not scheduling_components:
        return _finish(inbound_email, InboundEmailStates.IGNORED, InboundEmailOutcomes.CALENDAR_REPLY, "The email is an answer to an invitation, not an invitation.")

    if not scheduling_components:
        if should_reply([], InboundEmailOutcomes.NO_INVITATION):
            _reply_once(inbound_email, original_message_id, [], InboundEmailOutcomes.NO_INVITATION)
        return _finish(inbound_email, InboundEmailStates.IGNORED, InboundEmailOutcomes.NO_INVITATION, "The email doesn't contain a meeting invitation.")

    calendar = get_mailbot_calendar(inbound_email.project)
    results = [handle_component(calendar, component, inbound_email.sender) for component in scheduling_components]
    state, outcome, reason = _email_outcome(results)

    if should_reply(results, outcome):
        _reply_once(inbound_email, original_message_id, results, outcome)

    return _finish(inbound_email, state, outcome, reason, results)
