import logging
from datetime import timedelta

from django.conf import settings
from django.core.management.base import BaseCommand
from django.utils import timezone

from mailbot.models import InboundEmail

logger = logging.getLogger(__name__)


class Command(BaseCommand):
    help = "Deletes the raw content of received emails older than MAILBOT_RAW_RETENTION_DAYS. The audit trail (sender, decision...) is kept."

    def handle(self, *args, **options):
        cutoff = timezone.now() - timedelta(days=settings.MAILBOT_RAW_RETENTION_DAYS)
        expired = InboundEmail.objects.filter(created_at__lt=cutoff).exclude(raw_file="").exclude(raw_file__isnull=True)
        count = 0
        for inbound_email in expired.iterator():
            inbound_email.raw_file.delete(save=False)
            InboundEmail.objects.filter(id=inbound_email.id).update(raw_file=None)
            count += 1
        logger.info(f"Purged the raw content of {count} emails older than {settings.MAILBOT_RAW_RETENTION_DAYS} days")
