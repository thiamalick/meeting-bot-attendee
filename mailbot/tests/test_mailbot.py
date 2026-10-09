import uuid
from datetime import timedelta
from pathlib import Path
from string import Template
from unittest.mock import patch
from zoneinfo import ZoneInfo

from django.conf import settings
from django.core import mail
from django.test import TestCase, override_settings
from django.urls import reverse
from django.utils import timezone

from accounts.models import Organization
from bots.models import Bot, BotStates, CalendarEvent, CalendarPlatform, Project
from mailbot.ical import expand_series, parse_invitation
from mailbot.ingest import ingest_raw_email
from mailbot.models import InboundEmail, InboundEmailOutcomes, InboundEmailSources, InboundEmailStates, RecurringSeries
from mailbot.tasks import extend_recurring_series, process_inbound_email

FIXTURES = Path(__file__).parent / "fixtures"
PARIS = ZoneInfo("Europe/Paris")
TRUSTED_AUTH_RESULTS = "mx.client.fr; dkim=pass header.d=client.fr; spf=pass smtp.mailfrom=client.fr; dmarc=pass (p=REJECT) header.from=client.fr"


def utc_ical(dt):
    return dt.astimezone(ZoneInfo("UTC")).strftime("%Y%m%dT%H%M%SZ")


def local_ical(dt):
    return dt.astimezone(PARIS).strftime("%Y%m%dT%H%M%S")


def build_email(fixture, **values):
    defaults = {
        "AUTH_RESULTS": TRUSTED_AUTH_RESULTS,
        "SENDER": "alice@client.fr",
        "MESSAGE_ID": f"{uuid.uuid4()}@mail.gmail.com",
        "METHOD": "REQUEST",
        "STATUS": "CONFIRMED",
        "SEQUENCE": "0",
        "EXTRA_HEADERS": "",
    }
    defaults.update(values)
    if defaults["EXTRA_HEADERS"]:
        defaults["EXTRA_HEADERS"] += "\n"
    return Template((FIXTURES / fixture).read_text()).substitute(defaults).encode("utf-8")


MAILBOT_SETTINGS = {
    "MAILBOT_ENABLED": True,
    "MAILBOT_ADDRESS": "bot@client.fr",
    "MAILBOT_ALLOWED_SENDER_DOMAINS": ["client.fr"],
    "MAILBOT_REQUIRE_DMARC": True,
    "MAILBOT_TRUSTED_AUTHSERV_ID": "mx.client.fr",
    "MAILBOT_SEND_REPLIES": True,
    "MAILBOT_REPLY_LANGUAGE": "fr",
    "MAILBOT_BOT_NAME": "Meeting Bot",
    "MAILBOT_BOT_SETTINGS": {},
    "MAILBOT_RECURRENCE_HORIZON_DAYS": 14,
    "MAILBOT_MAX_EMAILS_PER_SENDER_PER_HOUR": 60,
    "EMAIL_BACKEND": "django.core.mail.backends.locmem.EmailBackend",
}


def in_memory_storages():
    storages = {alias: dict(config) for alias, config in settings.STORAGES.items()}
    for alias in ("recordings", "bot_debug_screenshots", "audio_chunks"):
        storages[alias] = {"BACKEND": "django.core.files.storage.InMemoryStorage"}
    return storages


@override_settings(**MAILBOT_SETTINGS)
class MailbotTestCase(TestCase):
    def setUp(self):
        self.storage_override = override_settings(STORAGES=in_memory_storages())
        self.storage_override.enable()
        self.addCleanup(self.storage_override.disable)

        self.organization = Organization.objects.create(name="Client")
        self.project = Project.objects.create(name="Réunions", organization=self.organization)
        project_override = override_settings(MAILBOT_PROJECT_ID=self.project.object_id)
        project_override.enable()
        self.addCleanup(project_override.disable)

        self.start = (timezone.now() + timedelta(days=2)).replace(minute=0, second=0, microsecond=0)
        self.end = self.start + timedelta(hours=1)

    def receive(self, raw, source=InboundEmailSources.IMAP):
        """Ingests an email and runs its processing task synchronously, like the worker would."""
        with patch("mailbot.tasks.process_inbound_email.delay", side_effect=lambda inbound_email_id: process_inbound_email.apply(args=[inbound_email_id])):
            with self.captureOnCommitCallbacks(execute=True):
                inbound_email, created = ingest_raw_email(raw, source)
        inbound_email.refresh_from_db()
        return inbound_email

    def google_invite(self, **values):
        values.setdefault("UID", "7kukuqrfedlm2f9t4vo6s6r6s9@google.com")
        values.setdefault("START_UTC", utc_ical(self.start))
        values.setdefault("END_UTC", utc_ical(self.end))
        return build_email("google_meet_invite.eml", **values)

    def active_bots(self):
        return Bot.objects.filter(project=self.project).exclude(state__in=BotStates.post_meeting_states())


class TestSchedulingFromInvitations(MailbotTestCase):
    def test_google_invite_schedules_bot_and_replies(self):
        inbound_email = self.receive(self.google_invite())

        self.assertEqual(inbound_email.state, InboundEmailStates.PROCESSED)
        self.assertEqual(inbound_email.outcome, InboundEmailOutcomes.SCHEDULED)
        bot = self.active_bots().get()
        self.assertEqual(bot.state, BotStates.SCHEDULED)
        self.assertEqual(bot.join_at, self.start)
        self.assertEqual(bot.meeting_url, "https://meet.google.com/abc-defg-hij")
        self.assertEqual(bot.name, "Meeting Bot")
        self.assertEqual(bot.metadata["requested_by"], "alice@client.fr")
        self.assertEqual(bot.calendar_event.calendar.platform, CalendarPlatform.EMAIL)
        self.assertEqual(inbound_email.details[0]["bot_id"], bot.object_id)

        self.assertEqual(len(mail.outbox), 1)
        reply = mail.outbox[0]
        self.assertEqual(reply.to, ["alice@client.fr"])
        self.assertEqual(reply.from_email, "bot@client.fr")
        self.assertEqual(reply.subject, "Re: Invitation: Point projet")
        self.assertEqual(reply.extra_headers["Auto-Submitted"], "auto-replied")
        self.assertIn("Je rejoindrai la réunion.", reply.body)
        self.assertIn("Point projet", reply.body)

    def test_same_email_delivered_twice_is_processed_once(self):
        raw = self.google_invite(MESSAGE_ID="duplicate@mail.gmail.com")
        first = self.receive(raw)
        second = self.receive(raw)

        self.assertEqual(first.id, second.id)
        self.assertEqual(InboundEmail.objects.count(), 1)
        self.assertEqual(self.active_bots().count(), 1)
        self.assertEqual(len(mail.outbox), 1)

    def test_outlook_teams_invite_with_windows_timezone(self):
        raw = build_email(
            "outlook_teams_invite.eml",
            SENDER="bruno.leroy@client.fr",
            UID="040000008200E00074C5B7101A82E00800000000A1B2C3D4E5F6D901000000000000000010000000",
            START_LOCAL=local_ical(self.start),
            END_LOCAL=local_ical(self.end),
        )
        inbound_email = self.receive(raw)

        self.assertEqual(inbound_email.outcome, InboundEmailOutcomes.SCHEDULED, inbound_email.decision_reason)
        bot = self.active_bots().get()
        # The VTIMEZONE of the invitation, not the server timezone, defines the local time
        self.assertEqual(bot.join_at, self.start)
        self.assertTrue(bot.meeting_url.startswith("https://teams.microsoft.com/l/meetup-join/"))

    def test_zoom_invite_without_zoom_credentials_fails_and_explains(self):
        raw = build_email("zoom_invite.eml", SENDER="chloe.durand@client.fr", UID="zoom-84315220467", START_UTC=utc_ical(self.start), END_UTC=utc_ical(self.end))
        inbound_email = self.receive(raw)

        self.assertEqual(inbound_email.state, InboundEmailStates.FAILED)
        self.assertEqual(inbound_email.outcome, InboundEmailOutcomes.BOT_CREATION_FAILED)
        self.assertIn("Zoom App credentials are required", inbound_email.decision_reason)
        self.assertEqual(self.active_bots().count(), 0)
        self.assertIn("erreur technique", mail.outbox[0].body)

    def test_meeting_in_progress_bot_joins_now(self):
        start = timezone.now() - timedelta(minutes=10)
        with patch("mailbot.events.launch_bot") as launch_bot:
            inbound_email = self.receive(self.google_invite(START_UTC=utc_ical(start), END_UTC=utc_ical(start + timedelta(hours=1))))

        self.assertEqual(inbound_email.outcome, InboundEmailOutcomes.SCHEDULED)
        bot = self.active_bots().get()
        self.assertIsNone(bot.join_at)
        self.assertEqual(bot.state, BotStates.JOINING)
        self.assertIsNotNone(bot.calendar_event)
        launch_bot.assert_called_once()

    def test_past_meeting_is_not_scheduled(self):
        start = timezone.now() - timedelta(hours=3)
        inbound_email = self.receive(self.google_invite(START_UTC=utc_ical(start), END_UTC=utc_ical(start + timedelta(hours=1))))

        self.assertEqual(inbound_email.state, InboundEmailStates.IGNORED)
        self.assertEqual(inbound_email.outcome, InboundEmailOutcomes.PAST_MEETING)
        self.assertEqual(self.active_bots().count(), 0)
        self.assertIn("déjà terminée", mail.outbox[0].body)


class TestUpdatesAndCancellations(MailbotTestCase):
    def test_update_with_higher_sequence_reschedules_bot(self):
        self.receive(self.google_invite())
        new_start = self.start + timedelta(hours=2)
        inbound_email = self.receive(self.google_invite(SEQUENCE="1", START_UTC=utc_ical(new_start), END_UTC=utc_ical(new_start + timedelta(hours=1))))

        self.assertEqual(inbound_email.outcome, InboundEmailOutcomes.UPDATED)
        bot = self.active_bots().get()
        self.assertEqual(bot.join_at, new_start)
        self.assertEqual(len(mail.outbox), 2)
        self.assertIn("modification", mail.outbox[1].body)

    def test_identical_invitation_resent_does_not_reply_again(self):
        self.receive(self.google_invite())
        inbound_email = self.receive(self.google_invite())

        self.assertEqual(inbound_email.outcome, InboundEmailOutcomes.UPDATED)
        self.assertEqual(self.active_bots().count(), 1)
        self.assertEqual(len(mail.outbox), 1)

    def test_out_of_order_older_sequence_is_ignored(self):
        new_start = self.start + timedelta(hours=2)
        self.receive(self.google_invite(SEQUENCE="2", START_UTC=utc_ical(new_start), END_UTC=utc_ical(new_start + timedelta(hours=1))))
        inbound_email = self.receive(self.google_invite(SEQUENCE="1"))

        self.assertEqual(inbound_email.outcome, InboundEmailOutcomes.STALE_UPDATE)
        self.assertEqual(self.active_bots().get().join_at, new_start)

    def test_late_cancellation_of_an_older_version_is_ignored(self):
        self.receive(self.google_invite(SEQUENCE="2"))
        inbound_email = self.receive(self.google_invite(METHOD="CANCEL", STATUS="CANCELLED", SEQUENCE="1"))

        self.assertEqual(inbound_email.outcome, InboundEmailOutcomes.STALE_UPDATE)
        self.assertEqual(self.active_bots().count(), 1)

    def test_cancellation_removes_scheduled_bot(self):
        self.receive(self.google_invite())
        inbound_email = self.receive(self.google_invite(METHOD="CANCEL", STATUS="CANCELLED", SEQUENCE="1"))

        self.assertEqual(inbound_email.outcome, InboundEmailOutcomes.CANCELLED)
        self.assertEqual(self.active_bots().count(), 0)
        self.assertTrue(CalendarEvent.objects.get(platform_uuid="7kukuqrfedlm2f9t4vo6s6r6s9@google.com").is_deleted)
        self.assertIn("Réunion annulée", mail.outbox[1].body)


class TestRecurringMeetings(MailbotTestCase):
    UID = "4o3m2ldk1q8vb7r1a0s9t8u7v6@google.com"

    def recurring_invite(self, components, method="REQUEST"):
        return build_email("recurring_daily_invite.eml", METHOD=method, COMPONENTS=components)

    def master(self, sequence=0, exdate=None):
        lines = [
            "BEGIN:VEVENT",
            f"DTSTART;TZID=Europe/Paris:{local_ical(self.start)}",
            f"DTEND;TZID=Europe/Paris:{local_ical(self.end)}",
            "RRULE:FREQ=DAILY;COUNT=5",
        ]
        if exdate:
            lines.append(f"EXDATE;TZID=Europe/Paris:{local_ical(exdate)}")
        lines += [f"UID:{self.UID}", f"SEQUENCE:{sequence}", "SUMMARY:Daily équipe", "X-GOOGLE-CONFERENCE:https://meet.google.com/xyz-abcd-efg", "END:VEVENT"]
        return "\n".join(lines)

    def override(self, original_start, new_start, sequence=0, status="CONFIRMED"):
        return "\n".join(
            [
                "BEGIN:VEVENT",
                f"DTSTART;TZID=Europe/Paris:{local_ical(new_start)}",
                f"DTEND;TZID=Europe/Paris:{local_ical(new_start + timedelta(hours=1))}",
                f"RECURRENCE-ID;TZID=Europe/Paris:{local_ical(original_start)}",
                f"UID:{self.UID}",
                f"SEQUENCE:{sequence}",
                f"STATUS:{status}",
                "SUMMARY:Daily équipe",
                "X-GOOGLE-CONFERENCE:https://meet.google.com/xyz-abcd-efg",
                "END:VEVENT",
            ]
        )

    def scheduled_join_times(self):
        return sorted(bot.join_at for bot in self.active_bots())

    def test_series_with_exception_and_excluded_date(self):
        day = timedelta(days=1)
        moved = self.start + 2 * day + timedelta(hours=1)
        inbound_email = self.receive(self.recurring_invite(self.master(exdate=self.start + day) + "\n" + self.override(self.start + 2 * day, moved)))

        self.assertEqual(inbound_email.state, InboundEmailStates.PROCESSED, inbound_email.decision_reason)
        # Day 2 is excluded, day 3 is moved one hour later
        self.assertEqual(self.scheduled_join_times(), [self.start, moved, self.start + 3 * day, self.start + 4 * day])
        self.assertEqual(RecurringSeries.objects.get().uid, self.UID)
        self.assertIn("récurrente", mail.outbox[0].body)

    def test_cancelling_one_occurrence_keeps_the_others(self):
        day = timedelta(days=1)
        self.receive(self.recurring_invite(self.master()))
        self.assertEqual(self.active_bots().count(), 5)

        inbound_email = self.receive(self.recurring_invite(self.override(self.start + 3 * day, self.start + 3 * day, sequence=1, status="CANCELLED"), method="CANCEL"))

        self.assertEqual(inbound_email.outcome, InboundEmailOutcomes.CANCELLED)
        self.assertNotIn(self.start + 3 * day, self.scheduled_join_times())
        self.assertEqual(self.active_bots().count(), 4)

    def test_cancelling_the_series_removes_every_bot(self):
        self.receive(self.recurring_invite(self.master()))
        inbound_email = self.receive(self.recurring_invite(self.master(sequence=1), method="CANCEL"))

        self.assertEqual(inbound_email.outcome, InboundEmailOutcomes.CANCELLED)
        self.assertEqual(self.active_bots().count(), 0)
        self.assertTrue(RecurringSeries.objects.get().is_cancelled)

    def test_occurrences_beyond_the_horizon_are_added_later(self):
        with override_settings(MAILBOT_RECURRENCE_HORIZON_DAYS=3):
            self.receive(self.recurring_invite(self.master()))
            self.assertEqual(self.active_bots().count(), 2)

            two_days_later = timezone.now() + timedelta(days=2)
            with patch("django.utils.timezone.now", return_value=two_days_later):
                extend_recurring_series()

        self.assertEqual(self.active_bots().count(), 4)


class TestSecurity(MailbotTestCase):
    def test_sender_from_unknown_domain_is_ignored_silently(self):
        inbound_email = self.receive(self.google_invite(SENDER="mallory@evil.example", AUTH_RESULTS="mx.client.fr; dmarc=pass header.from=evil.example"))

        self.assertEqual(inbound_email.state, InboundEmailStates.IGNORED)
        self.assertEqual(inbound_email.outcome, InboundEmailOutcomes.UNAUTHORIZED_SENDER)
        self.assertEqual(self.active_bots().count(), 0)
        self.assertEqual(len(mail.outbox), 0)

    def test_spoofed_sender_failing_dmarc_is_ignored(self):
        inbound_email = self.receive(self.google_invite(AUTH_RESULTS="mx.client.fr; spf=fail smtp.mailfrom=evil.example; dmarc=fail (p=REJECT) header.from=client.fr"))

        self.assertEqual(inbound_email.outcome, InboundEmailOutcomes.UNAUTHENTICATED_SENDER)
        self.assertEqual(self.active_bots().count(), 0)
        self.assertEqual(len(mail.outbox), 0)

    def test_authentication_results_from_another_server_are_not_trusted(self):
        inbound_email = self.receive(self.google_invite(AUTH_RESULTS="mx.evil.example; dmarc=pass header.from=client.fr"))

        self.assertEqual(inbound_email.outcome, InboundEmailOutcomes.UNAUTHENTICATED_SENDER)
        self.assertEqual(self.active_bots().count(), 0)

    @override_settings(MAILBOT_REQUIRE_DMARC=False)
    def test_dmarc_check_can_be_disabled(self):
        inbound_email = self.receive(self.google_invite(AUTH_RESULTS="mx.other.example; none"))

        self.assertEqual(inbound_email.outcome, InboundEmailOutcomes.SCHEDULED)

    def test_auto_reply_is_ignored_without_answer(self):
        inbound_email = self.receive(self.google_invite(EXTRA_HEADERS="Auto-Submitted: auto-replied"))

        self.assertEqual(inbound_email.outcome, InboundEmailOutcomes.AUTO_SUBMITTED)
        self.assertEqual(len(mail.outbox), 0)

    @override_settings(MAILBOT_MAX_EMAILS_PER_SENDER_PER_HOUR=1)
    def test_sender_rate_limit(self):
        self.receive(self.google_invite())
        inbound_email = self.receive(self.google_invite(UID="another-meeting@google.com"))

        self.assertEqual(inbound_email.outcome, InboundEmailOutcomes.RATE_LIMITED)
        self.assertEqual(self.active_bots().count(), 1)

    @override_settings(MAILBOT_MAX_EMAIL_SIZE_BYTES=100)
    def test_too_large_email_is_recorded_but_not_stored(self):
        inbound_email = self.receive(self.google_invite())

        self.assertEqual(inbound_email.outcome, InboundEmailOutcomes.TOO_LARGE)
        self.assertFalse(inbound_email.raw_file)

    def test_email_without_invitation_gets_help(self):
        raw = ("Authentication-Results: " + TRUSTED_AUTH_RESULTS + "\nFrom: alice@client.fr\nTo: bot@client.fr\nSubject: Salut\nMessage-ID: <plain@client.fr>\n\nPeux-tu venir à la réunion ? https://meet.google.com/abc-defg-hij\n").encode()
        inbound_email = self.receive(raw)

        self.assertEqual(inbound_email.outcome, InboundEmailOutcomes.NO_INVITATION)
        self.assertEqual(self.active_bots().count(), 0)
        self.assertIn("ajoutez bot@client.fr comme invité", mail.outbox[0].body)


class TestHttpIngestion(MailbotTestCase):
    @override_settings(MAILBOT_HTTP_INGEST_TOKEN="s3cr3t")
    def test_post_raw_email(self):
        with patch("mailbot.tasks.process_inbound_email.delay") as delay:
            with self.captureOnCommitCallbacks(execute=True):
                response = self.client.post(reverse("mailbot:inbound"), data=self.google_invite(), content_type="message/rfc822", HTTP_AUTHORIZATION="Bearer s3cr3t")

        self.assertEqual(response.status_code, 202)
        inbound_email = InboundEmail.objects.get(object_id=response.json()["id"])
        self.assertEqual(inbound_email.source, InboundEmailSources.HTTP)
        delay.assert_called_once_with(inbound_email.id)

    @override_settings(MAILBOT_HTTP_INGEST_TOKEN="s3cr3t")
    def test_wrong_token_is_rejected(self):
        response = self.client.post(reverse("mailbot:inbound"), data=self.google_invite(), content_type="message/rfc822", HTTP_AUTHORIZATION="Bearer wrong")

        self.assertEqual(response.status_code, 401)
        self.assertEqual(InboundEmail.objects.count(), 0)

    def test_endpoint_disabled_without_token(self):
        response = self.client.post(reverse("mailbot:inbound"), data=self.google_invite(), content_type="message/rfc822")

        self.assertEqual(response.status_code, 404)


class TestIcalParsing(TestCase):
    def test_meeting_url_found_in_location(self):
        payload = b"BEGIN:VCALENDAR\r\nMETHOD:REQUEST\r\nBEGIN:VEVENT\r\nUID:x\r\nDTSTART:20300101T100000Z\r\nDTEND:20300101T110000Z\r\nLOCATION:https://us05web.zoom.us/j/84315220467?pwd=abc\r\nEND:VEVENT\r\nEND:VCALENDAR\r\n"
        [component] = parse_invitation(payload)

        self.assertEqual(component.meeting_url, "https://us05web.zoom.us/j/84315220467?pwd=abc")

    def test_meeting_url_falls_back_to_email_body(self):
        payload = b"BEGIN:VCALENDAR\r\nMETHOD:REQUEST\r\nBEGIN:VEVENT\r\nUID:x\r\nDTSTART:20300101T100000Z\r\nEND:VEVENT\r\nEND:VCALENDAR\r\n"
        [component] = parse_invitation(payload, "Lien : https://meet.google.com/abc-defg-hij")

        self.assertEqual(component.meeting_url, "https://meet.google.com/abc-defg-hij")
        # No DTEND nor DURATION: one hour by default
        self.assertEqual(component.end - component.start, timedelta(hours=1))

    def test_unparsable_payload_is_ignored(self):
        self.assertEqual(parse_invitation(b"not a calendar"), [])

    def test_all_day_event_is_flagged(self):
        payload = b"BEGIN:VCALENDAR\r\nMETHOD:REQUEST\r\nBEGIN:VEVENT\r\nUID:x\r\nDTSTART;VALUE=DATE:20300101\r\nEND:VEVENT\r\nEND:VCALENDAR\r\n"
        [component] = parse_invitation(payload)

        self.assertTrue(component.is_all_day)

    def test_expansion_keeps_local_time_across_daylight_saving_change(self):
        series = "BEGIN:VCALENDAR\r\nBEGIN:VEVENT\r\nUID:x\r\nDTSTART;TZID=Europe/Paris:20261023T140000\r\nDTEND;TZID=Europe/Paris:20261023T150000\r\nRRULE:FREQ=DAILY;UNTIL=20261027\r\nEND:VEVENT\r\nEND:VCALENDAR\r\n"
        occurrences = expand_series(series, timezone.datetime(2026, 10, 1, tzinfo=ZoneInfo("UTC")), timezone.datetime(2026, 11, 1, tzinfo=ZoneInfo("UTC")))

        starts = [start.astimezone(PARIS).strftime("%d %H:%M") for _, start, _ in occurrences]
        self.assertEqual(starts, ["23 14:00", "24 14:00", "25 14:00", "26 14:00", "27 14:00"])
