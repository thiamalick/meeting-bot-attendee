import logging

from django.conf import settings
from django.core.files.base import ContentFile
from django.core.files.storage import storages
from django.db import IntegrityError, transaction
from django.utils import timezone

from .events import get_mailbot_project
from .message import message_id, parse_message, sender_address, subject
from .models import InboundEmail, InboundEmailOutcomes, InboundEmailStates

logger = logging.getLogger(__name__)


def ingest_raw_email(raw: bytes, source: str) -> tuple[InboundEmail, bool]:
    """
    Records a received email and queues its processing. Every source (IMAP, HTTP...) goes through here.
    Returns (inbound_email, created). Receiving the same message twice is a no-op, so sources can safely redeliver.
    """
    from .tasks import process_inbound_email

    project = get_mailbot_project()
    message = parse_message(raw)
    msg_id = message_id(message, raw)

    existing = InboundEmail.objects.filter(project=project, message_id=msg_id).first()
    if existing:
        return existing, False

    inbound_email = InboundEmail(
        project=project,
        source=source,
        message_id=msg_id,
        sender=sender_address(message) or "",
        subject=subject(message),
        size_bytes=len(raw),
    )

    if len(raw) > settings.MAILBOT_MAX_EMAIL_SIZE_BYTES:
        # Recorded for the audit trail, but the content is neither stored nor processed
        inbound_email.state = InboundEmailStates.IGNORED
        inbound_email.outcome = InboundEmailOutcomes.TOO_LARGE
        inbound_email.decision_reason = f"The email is larger than {settings.MAILBOT_MAX_EMAIL_SIZE_BYTES} bytes."
        inbound_email.processed_at = timezone.now()

    try:
        with transaction.atomic():
            inbound_email.save()
            if inbound_email.state == InboundEmailStates.RECEIVED:
                # StorageAlias only delegates delete() and url(), so the file is written through the storage itself
                inbound_email.raw_file.name = storages["recordings"].save(f"mailbot/{inbound_email.object_id}.eml", ContentFile(raw))
                inbound_email.save(update_fields=["raw_file"])
                transaction.on_commit(lambda: process_inbound_email.delay(inbound_email.id))
    except IntegrityError:
        # The same message was ingested concurrently
        return InboundEmail.objects.get(project=project, message_id=msg_id), False

    logger.info(f"Mailbot received {inbound_email.object_id} from {inbound_email.sender} via {source}")
    return inbound_email, True
