import logging
import re
from dataclasses import dataclass, field
from datetime import date, datetime, timedelta
from datetime import timezone as python_timezone
from zoneinfo import ZoneInfo

import icalendar
from dateutil.rrule import rrulestr
from django.conf import settings

from bots.tasks.sync_calendar_task import extract_meeting_url_from_text

logger = logging.getLogger(__name__)

DEFAULT_EVENT_DURATION = timedelta(hours=1)

# Where calendar clients put the meeting link, most specific first
MEETING_URL_PROPERTIES = [
    "X-MICROSOFT-SKYPETEAMSMEETINGURL",
    "X-MICROSOFT-ONLINEMEETINGCONFLINK",
    "X-GOOGLE-CONFERENCE",
    "LOCATION",
    "URL",
    "DESCRIPTION",
    "X-ALT-DESC",
]

_UNTIL_RE = re.compile(r"UNTIL=(\d{8})(T\d{6})?(Z?)")


@dataclass
class InvitationComponent:
    """A VEVENT of an invitation, normalized. Datetimes are timezone aware."""

    uid: str
    method: str
    sequence: int
    recurrence_id: datetime | None
    is_cancelled: bool
    is_all_day: bool
    start: datetime | None
    end: datetime | None
    summary: str
    organizer: str | None
    attendees: list[str] = field(default_factory=list)
    meeting_url: str | None = None
    rrule: str | None = None
    # For recurring masters: a VCALENDAR with this VEVENT and the VTIMEZONEs, used to expand occurrences later
    series_ics: str | None = None

    @property
    def is_recurring_master(self):
        return self.rrule is not None and self.recurrence_id is None


def occurrence_uuid(uid: str, original_start: datetime) -> str:
    """platform_uuid of one occurrence of a recurring series, keyed by its original start (RECURRENCE-ID)."""
    return f"{uid}::{original_start.astimezone(python_timezone.utc):%Y%m%dT%H%M%SZ}"


def _default_timezone():
    return ZoneInfo(settings.TIME_ZONE)


def _as_aware_datetime(value):
    """Returns (datetime, is_all_day). Floating times are interpreted in the server timezone."""
    if isinstance(value, datetime):
        if value.tzinfo is None:
            value = value.replace(tzinfo=_default_timezone())
        return value, False
    if isinstance(value, date):
        return datetime(value.year, value.month, value.day, tzinfo=_default_timezone()), True
    return None, False


def _email_from_cal_address(value) -> str | None:
    if not value:
        return None
    address = str(value)
    if address.lower().startswith("mailto:"):
        address = address[len("mailto:") :]
    return address.strip().lower() or None


def _meeting_url(component, fallback_text: str) -> str | None:
    for name in MEETING_URL_PROPERTIES:
        values = component.get(name)
        if values is None:
            continue
        for value in values if isinstance(values, list) else [values]:
            url = extract_meeting_url_from_text(str(value))
            if url:
                return url
    return extract_meeting_url_from_text(fallback_text)


def _series_ics(calendar, component) -> str:
    series = icalendar.Calendar()
    series.add("PRODID", "-//Attendee//Mailbot//EN")
    series.add("VERSION", "2.0")
    for timezone_component in calendar.walk("VTIMEZONE"):
        series.add_component(timezone_component)
    series.add_component(component)
    return series.to_ical().decode("utf-8")


def parse_invitation(payload: bytes, fallback_text: str = "") -> list[InvitationComponent]:
    """Normalized VEVENTs of an iCalendar payload. Unparsable payloads yield an empty list."""
    try:
        calendar = icalendar.Calendar.from_ical(payload)
    except ValueError:
        logger.warning("Ignoring an iCalendar payload that could not be parsed", exc_info=True)
        return []

    method = str(calendar.get("METHOD") or "PUBLISH").upper()
    components = []
    for component in calendar.walk("VEVENT"):
        uid = str(component.get("UID") or "").strip()
        if not uid:
            continue

        start, is_all_day = _as_aware_datetime(component.decoded("DTSTART")) if component.get("DTSTART") else (None, False)
        end = None
        if component.get("DTEND"):
            end, _ = _as_aware_datetime(component.decoded("DTEND"))
        elif component.get("DURATION") and start:
            end = start + component.decoded("DURATION")
        elif start:
            end = start + DEFAULT_EVENT_DURATION

        recurrence_id = None
        if component.get("RECURRENCE-ID"):
            recurrence_id, _ = _as_aware_datetime(component.decoded("RECURRENCE-ID"))

        rrule = component.get("RRULE")
        attendees = component.get("ATTENDEE") or []
        if not isinstance(attendees, list):
            attendees = [attendees]

        parsed = InvitationComponent(
            uid=uid,
            method=method,
            sequence=int(component.get("SEQUENCE") or 0),
            recurrence_id=recurrence_id,
            is_cancelled=method == "CANCEL" or str(component.get("STATUS") or "").upper() == "CANCELLED",
            is_all_day=is_all_day,
            start=start,
            end=end,
            summary=str(component.get("SUMMARY") or "")[:1024],
            organizer=_email_from_cal_address(component.get("ORGANIZER")),
            attendees=[address for address in (_email_from_cal_address(attendee) for attendee in attendees) if address],
            meeting_url=_meeting_url(component, fallback_text),
            rrule=rrule.to_ical().decode("utf-8") if rrule else None,
        )
        if parsed.is_recurring_master:
            parsed.series_ics = _series_ics(calendar, component)
        components.append(parsed)

    # Masters first, so their occurrences exist before the exceptions that override them
    components.sort(key=lambda c: c.recurrence_id is not None)
    return components


def _normalize_until(rrule: str, dtstart: datetime) -> str:
    """dateutil requires UNTIL in UTC when DTSTART is timezone aware, calendar clients don't always comply."""

    def replace(match):
        day, time_part, is_utc = match.groups()
        if is_utc:
            return match.group(0)
        if not time_part:
            local_until = datetime.strptime(day + "T235959", "%Y%m%dT%H%M%S").replace(tzinfo=dtstart.tzinfo)
        else:
            local_until = datetime.strptime(day + time_part, "%Y%m%dT%H%M%S").replace(tzinfo=dtstart.tzinfo)
        return f"UNTIL={local_until.astimezone(python_timezone.utc):%Y%m%dT%H%M%SZ}"

    return _UNTIL_RE.sub(replace, rrule)


def expand_series(series_ics: str, window_start: datetime, window_end: datetime) -> list[tuple[datetime, datetime, datetime]]:
    """(original_start, start, end) of the occurrences of a recurring series overlapping [window_start, window_end)."""
    calendar = icalendar.Calendar.from_ical(series_ics)
    master = calendar.walk("VEVENT")[0]
    dtstart, _ = _as_aware_datetime(master.decoded("DTSTART"))
    if master.get("DTEND"):
        duration = _as_aware_datetime(master.decoded("DTEND"))[0] - dtstart
    elif master.get("DURATION"):
        duration = master.decoded("DURATION")
    else:
        duration = DEFAULT_EVENT_DURATION

    rule_set = rrulestr(_normalize_until(master.get("RRULE").to_ical().decode("utf-8"), dtstart), dtstart=dtstart, forceset=True)
    for property_name, add in (("EXDATE", rule_set.exdate), ("RDATE", rule_set.rdate)):
        values = master.get(property_name) or []
        for value in values if isinstance(values, list) else [values]:
            for dt in value.dts:
                aware, _ = _as_aware_datetime(dt.dt)
                if aware:
                    add(aware)

    return [(occurrence, occurrence, occurrence + duration) for occurrence in rule_set.between(window_start - duration, window_end, inc=True)]
