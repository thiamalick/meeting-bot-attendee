import logging
from datetime import timedelta

from celery import shared_task
from django.conf import settings
from django.utils import timezone

from .events import materialize_series
from .models import InboundEmail, InboundEmailOutcomes, InboundEmailStates, RecurringSeries
from .pipeline import process_inbound_email as run_pipeline

logger = logging.getLogger(__name__)


@shared_task(bind=True, max_retries=3)
def process_inbound_email(self, inbound_email_id):
    inbound_email = InboundEmail.objects.get(id=inbound_email_id)
    if inbound_email.state != InboundEmailStates.RECEIVED:
        return

    try:
        run_pipeline(inbound_email)
    except Exception as e:
        if self.request.retries < self.max_retries:
            raise self.retry(exc=e, countdown=30 * 2**self.request.retries)
        logger.exception(f"Mailbot failed to process {inbound_email.object_id}")
        InboundEmail.objects.filter(id=inbound_email.id).update(
            state=InboundEmailStates.FAILED,
            outcome=InboundEmailOutcomes.ERROR,
            decision_reason=f"Processing failed after {self.max_retries} retries: {e}"[:2000],
            processed_at=timezone.now(),
        )


@shared_task
def extend_recurring_series():
    """Schedules bots for the occurrences of recurring meetings that entered the rolling horizon."""
    # Re-expanding a day before the horizon ends leaves margin if the mailbot service is down for a while
    threshold = timezone.now() + timedelta(days=settings.MAILBOT_RECURRENCE_HORIZON_DAYS - 1)
    for series in RecurringSeries.objects.filter(is_cancelled=False, horizon_end__lt=threshold):
        try:
            materialize_series(series)
        except Exception:
            logger.exception(f"Mailbot failed to extend recurring series {series.id}")
