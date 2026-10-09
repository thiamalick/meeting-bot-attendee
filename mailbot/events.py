"""Turns parsed invitations into CalendarEvents of the project's email calendar, and keeps a bot scheduled for each of them."""

import copy
import logging
from dataclasses import dataclass
from datetime import datetime, timedelta

from django.conf import settings
from django.db.models import Q
from django.utils import timezone

from bots.bots_api_utils import BotCreationSource, create_bot, delete_bot
from bots.launch_bot_utils import launch_bot
from bots.models import Bot, BotStates, Calendar, CalendarEvent, CalendarPlatform, Project
from bots.tasks.sync_calendar_task import sync_bots_for_calendar_event

from .ical import InvitationComponent, expand_series, occurrence_uuid
from .models import InboundEmailOutcomes, RecurringSeries

logger = logging.getLogger(__name__)

MAILBOT_CALENDAR_DEDUPLICATION_KEY = "mailbot"


@dataclass
class EventResult:
    outcome: str
    reason: str = ""
    summary: str = ""
    # In the invitation's timezone, for display in the reply
    start: datetime | None = None
    calendar_event: CalendarEvent | None = None
    bot: Bot | None = None
    occurrence_count: int | None = None
    # False when an update didn't change anything that matters (time, link, cancellation), so no reply is needed
    changed: bool = True

    def as_detail(self):
        return {
            "outcome": self.outcome,
            "reason": self.reason,
            "summary": self.summary,
            "start": self.start.isoformat() if self.start else None,
            "calendar_event_id": self.calendar_event.object_id if self.calendar_event else None,
            "bot_id": self.bot.object_id if self.bot else None,
            "occurrence_count": self.occurrence_count,
        }


def get_mailbot_project() -> Project:
    return Project.objects.get(object_id=settings.MAILBOT_PROJECT_ID)


def get_mailbot_calendar(project: Project) -> Calendar:
    calendar, _ = Calendar.objects.get_or_create(
        project=project,
        deduplication_key=MAILBOT_CALENDAR_DEDUPLICATION_KEY,
        defaults={"platform": CalendarPlatform.EMAIL, "client_id": settings.MAILBOT_ADDRESS or "mailbot", "metadata": {"source": "mailbot"}},
    )
    return calendar


def _upsert_event(calendar: Calendar, platform_uuid: str, **fields) -> CalendarEvent:
    event = CalendarEvent.objects.filter(calendar=calendar, platform_uuid=platform_uuid).first()
    if event is None:
        return CalendarEvent.objects.create(calendar=calendar, platform_uuid=platform_uuid, **fields)
    for name, value in fields.items():
        setattr(event, name, value)
    event.save()
    return event


def _event_raw(component: InvitationComponent, requested_by: str, override: bool) -> dict:
    return {
        "source": "email",
        "uid": component.uid,
        "recurrence_id": component.recurrence_id.isoformat() if component.recurrence_id else None,
        "sequence": component.sequence,
        "override": override,
        "organizer": component.organizer,
        "requested_by": requested_by,
    }


def _attendees(emails: list[str]) -> list[dict]:
    return [{"email": email} for email in emails]


def _active_bots(event: CalendarEvent):
    return event.bots.exclude(state__in=BotStates.post_meeting_states())


def sync_bot_for_event(event: CalendarEvent, requested_by: str) -> tuple[str, Bot | None, str]:
    """Creates, updates or deletes the bot of an event so it matches the event. Returns (outcome, bot, reason)."""
    if event.is_deleted:
        # Deletes the bots that haven't joined yet, a bot already in the meeting stays until the meeting ends
        sync_bots_for_calendar_event(event)
        return InboundEmailOutcomes.CANCELLED, None, "The meeting was cancelled, its bot was removed."

    if not event.meeting_url:
        for bot in _active_bots(event).filter(state=BotStates.SCHEDULED):
            delete_bot(bot)
        return InboundEmailOutcomes.NO_MEETING_URL, None, "No Zoom, Google Meet or Microsoft Teams link was found in the invitation."

    now = timezone.now()
    if event.end_time <= now:
        return InboundEmailOutcomes.PAST_MEETING, None, "The meeting is already over."

    active_bot = _active_bots(event).first()
    if active_bot:
        sync_bots_for_calendar_event(event)
        active_bot.refresh_from_db()
        return InboundEmailOutcomes.UPDATED, active_bot, "The bot of this meeting was updated."

    data = copy.deepcopy(settings.MAILBOT_BOT_SETTINGS)
    data.update(
        {
            "bot_name": settings.MAILBOT_BOT_NAME,
            "deduplication_key": f"mailbot-{event.object_id}",
            "metadata": {"source": "mailbot", "calendar_event_id": event.object_id, "requested_by": requested_by},
        }
    )
    if event.start_time > now:
        # join_at and meeting_url are taken from the event
        data["calendar_event_id"] = event.object_id
    else:
        # The meeting is in progress: join right away
        data["meeting_url"] = event.meeting_url

    bot, error = create_bot(data=data, source=BotCreationSource.EMAIL, project=event.calendar.project)
    if error:
        logger.error(f"Mailbot could not create a bot for calendar event {event.object_id}: {error}")
        return InboundEmailOutcomes.BOT_CREATION_FAILED, None, f"The bot could not be created: {error}"

    if bot.calendar_event_id is None:
        Bot.objects.filter(id=bot.id).update(calendar_event=event)
        bot.calendar_event = event
    if bot.state == BotStates.JOINING:
        # Scheduled bots are launched by the scheduler, a bot joining right away has to be launched here (we run in a worker)
        launch_bot(bot)
    return InboundEmailOutcomes.SCHEDULED, bot, "The bot was scheduled." if event.start_time > now else "The meeting is in progress, the bot is joining now."


def _events_of_uid(calendar: Calendar, uid: str):
    return CalendarEvent.objects.filter(calendar=calendar).filter(Q(platform_uuid=uid) | Q(platform_uuid__startswith=f"{uid}::"))


def _cancel_uid(calendar: Calendar, component: InvitationComponent, requested_by: str) -> EventResult:
    # A cancellation delivered after a newer invitation must not cancel it
    series = RecurringSeries.objects.filter(calendar=calendar, uid=component.uid).first()
    single_event = CalendarEvent.objects.filter(calendar=calendar, platform_uuid=component.uid).first()
    if (series and series.sequence > component.sequence) or (single_event and _is_stale(single_event.raw, component)):
        return EventResult(InboundEmailOutcomes.STALE_UPDATE, "A more recent version of this invitation was already processed.", summary=component.summary)

    events = list(_events_of_uid(calendar, component.uid).filter(is_deleted=False))
    RecurringSeries.objects.filter(calendar=calendar, uid=component.uid).update(is_cancelled=True, sequence=component.sequence)
    for event in events:
        event.is_deleted = True
        event.save()
        sync_bot_for_event(event, requested_by)
    reason = "The meeting was cancelled, its bot was removed." if events else "The cancelled meeting was not scheduled."
    return EventResult(InboundEmailOutcomes.CANCELLED, reason, summary=component.summary, start=component.start, calendar_event=events[0] if events else None)


def _is_stale(raw: dict | None, component: InvitationComponent) -> bool:
    return bool(raw) and raw.get("sequence", 0) > component.sequence


def _has_changed(existing: CalendarEvent | None, start_time, meeting_url, is_deleted) -> bool:
    return existing is None or existing.start_time != start_time or existing.meeting_url != meeting_url or existing.is_deleted != is_deleted


def _handle_single_event(calendar: Calendar, component: InvitationComponent, requested_by: str) -> EventResult:
    existing = CalendarEvent.objects.filter(calendar=calendar, platform_uuid=component.uid).first()
    if existing and _is_stale(existing.raw, component):
        return EventResult(InboundEmailOutcomes.STALE_UPDATE, "A more recent version of this invitation was already processed.", summary=component.summary, calendar_event=existing)

    changed = _has_changed(existing, component.start, component.meeting_url, False)
    event = _upsert_event(
        calendar,
        component.uid,
        start_time=component.start,
        end_time=component.end,
        name=component.summary or None,
        meeting_url=component.meeting_url,
        attendees=_attendees(component.attendees),
        ical_uid=component.uid,
        is_deleted=False,
        raw=_event_raw(component, requested_by, override=False),
    )
    outcome, bot, reason = sync_bot_for_event(event, requested_by)
    return EventResult(outcome, reason, summary=component.summary, start=component.start, calendar_event=event, bot=bot, changed=changed)


def _handle_occurrence_override(calendar: Calendar, component: InvitationComponent, requested_by: str) -> EventResult:
    """A single occurrence of a series that was moved, edited or cancelled (RECURRENCE-ID)."""
    platform_uuid = occurrence_uuid(component.uid, component.recurrence_id)
    existing = CalendarEvent.objects.filter(calendar=calendar, platform_uuid=platform_uuid).first()
    if existing and _is_stale(existing.raw, component):
        return EventResult(InboundEmailOutcomes.STALE_UPDATE, "A more recent version of this occurrence was already processed.", summary=component.summary, calendar_event=existing)

    series = RecurringSeries.objects.filter(calendar=calendar, uid=component.uid).first()
    meeting_url = component.meeting_url or (series.meeting_url if series else None)
    start_time = component.start or component.recurrence_id
    changed = _has_changed(existing, start_time, meeting_url, component.is_cancelled)
    # The override flag stops the series expansion from overwriting this occurrence later
    event = _upsert_event(
        calendar,
        platform_uuid,
        start_time=start_time,
        end_time=component.end or component.recurrence_id,
        name=component.summary or (series.name if series else None) or None,
        meeting_url=meeting_url,
        attendees=_attendees(component.attendees),
        ical_uid=component.uid,
        is_deleted=component.is_cancelled,
        raw=_event_raw(component, requested_by, override=True),
    )
    outcome, bot, reason = sync_bot_for_event(event, requested_by)
    return EventResult(outcome, reason, summary=event.name or "", start=start_time, calendar_event=event, bot=bot, changed=changed)


def materialize_series(series: RecurringSeries) -> list[EventResult]:
    """Creates the occurrences of a series over the rolling horizon and removes the ones that no longer exist."""
    now = timezone.now()
    window_end = now + timedelta(days=settings.MAILBOT_RECURRENCE_HORIZON_DAYS)
    occurrences = expand_series(series.ics, now, window_end)

    results = []
    expected_uuids = set()
    for original_start, start, end in occurrences:
        platform_uuid = occurrence_uuid(series.uid, original_start)
        expected_uuids.add(platform_uuid)
        existing = CalendarEvent.objects.filter(calendar=series.calendar, platform_uuid=platform_uuid).first()
        if existing and existing.raw.get("override"):
            continue
        event = _upsert_event(
            series.calendar,
            platform_uuid,
            start_time=start,
            end_time=end,
            name=series.name or None,
            meeting_url=series.meeting_url,
            attendees=series.attendees,
            ical_uid=series.uid,
            is_deleted=False,
            raw={"source": "email", "uid": series.uid, "recurrence_id": original_start.isoformat(), "sequence": series.sequence, "override": False, "requested_by": series.requested_by},
        )
        outcome, bot, reason = sync_bot_for_event(event, series.requested_by)
        results.append(EventResult(outcome, reason, summary=series.name, start=start, calendar_event=event, bot=bot))

    # Occurrences removed from the series (EXDATE added, series shortened...) lose their bot
    for event in _events_of_uid(series.calendar, series.uid).filter(is_deleted=False, start_time__gte=now).exclude(platform_uuid__in=expected_uuids):
        if event.raw.get("override"):
            continue
        event.is_deleted = True
        event.save()
        sync_bot_for_event(event, series.requested_by)

    series.horizon_end = window_end
    series.save()
    return results


def _handle_recurring_master(calendar: Calendar, component: InvitationComponent, requested_by: str) -> EventResult:
    series = RecurringSeries.objects.filter(calendar=calendar, uid=component.uid).first()
    if series and series.sequence > component.sequence:
        return EventResult(InboundEmailOutcomes.STALE_UPDATE, "A more recent version of this series was already processed.", summary=component.summary)

    changed = series is None or series.is_cancelled or series.ics != component.series_ics or series.meeting_url != component.meeting_url
    series, _ = RecurringSeries.objects.update_or_create(
        calendar=calendar,
        uid=component.uid,
        defaults={
            "sequence": component.sequence,
            "ics": component.series_ics,
            "name": component.summary,
            "meeting_url": component.meeting_url,
            "attendees": _attendees(component.attendees),
            "is_cancelled": False,
            "requested_by": requested_by,
        },
    )
    if not component.meeting_url:
        materialize_series(series)
        return EventResult(InboundEmailOutcomes.NO_MEETING_URL, "No Zoom, Google Meet or Microsoft Teams link was found in the invitation.", summary=component.summary, start=component.start, changed=changed)

    results = materialize_series(series)
    scheduled = [result for result in results if result.outcome in (InboundEmailOutcomes.SCHEDULED, InboundEmailOutcomes.UPDATED)]
    if not results:
        return EventResult(InboundEmailOutcomes.PAST_MEETING, f"The series has no occurrence in the next {settings.MAILBOT_RECURRENCE_HORIZON_DAYS} days.", summary=component.summary, start=component.start, occurrence_count=0)
    if not scheduled:
        return EventResult(results[0].outcome, results[0].reason, summary=component.summary, start=results[0].start, occurrence_count=0)

    outcome = InboundEmailOutcomes.SCHEDULED if any(result.outcome == InboundEmailOutcomes.SCHEDULED for result in scheduled) else InboundEmailOutcomes.UPDATED
    reason = f"Recurring meeting: a bot is scheduled for each of the {len(scheduled)} occurrences in the next {settings.MAILBOT_RECURRENCE_HORIZON_DAYS} days, later ones are added automatically."
    return EventResult(outcome, reason, summary=component.summary, start=scheduled[0].start, calendar_event=scheduled[0].calendar_event, bot=scheduled[0].bot, occurrence_count=len(scheduled), changed=changed)


def handle_component(calendar: Calendar, component: InvitationComponent, requested_by: str) -> EventResult:
    if component.recurrence_id is None and component.is_cancelled:
        return _cancel_uid(calendar, component, requested_by)
    if component.recurrence_id is not None:
        return _handle_occurrence_override(calendar, component, requested_by)
    if component.start is None or component.is_all_day:
        return EventResult(InboundEmailOutcomes.ALL_DAY_EVENT, "All-day events have no start time to join at.", summary=component.summary)
    if component.is_recurring_master:
        return _handle_recurring_master(calendar, component, requested_by)
    return _handle_single_event(calendar, component, requested_by)
