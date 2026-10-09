import uuid
from datetime import timedelta

import icalendar
from django.conf import settings
from django.core.mail import EmailMultiAlternatives
from django.core.management.base import BaseCommand
from django.utils import timezone


class Command(BaseCommand):
    help = "Sends a meeting invitation to the bot address through the configured SMTP server, to check a deployment end to end."

    def add_arguments(self, parser):
        parser.add_argument("--from", dest="sender", required=True, help="Organizer address, its domain must be in MAILBOT_ALLOWED_SENDER_DOMAINS")
        parser.add_argument("--meeting-url", required=True, help="Zoom, Google Meet or Microsoft Teams link")
        parser.add_argument("--in-minutes", type=int, default=10, help="Start of the meeting, from now")
        parser.add_argument("--duration-minutes", type=int, default=30)
        parser.add_argument("--uid", help="Reuse the UID of a previous invitation to update it")
        parser.add_argument("--sequence", type=int, default=0, help="Increase it when updating an invitation")
        parser.add_argument("--cancel", action="store_true", help="Send a cancellation instead of an invitation")

    def handle(self, *args, **options):
        start = (timezone.now() + timedelta(minutes=options["in_minutes"])).replace(second=0, microsecond=0)
        uid = options["uid"] or f"{uuid.uuid4()}@mailbot-test"
        method = "CANCEL" if options["cancel"] else "REQUEST"

        calendar = icalendar.Calendar()
        calendar.add("PRODID", "-//Attendee//Mailbot test//EN")
        calendar.add("VERSION", "2.0")
        calendar.add("METHOD", method)
        event = icalendar.Event()
        event.add("UID", uid)
        event.add("SEQUENCE", options["sequence"])
        event.add("DTSTAMP", timezone.now())
        event.add("DTSTART", start)
        event.add("DTEND", start + timedelta(minutes=options["duration_minutes"]))
        event.add("SUMMARY", "Mailbot test meeting")
        event.add("LOCATION", options["meeting_url"])
        event.add("ORGANIZER", f"mailto:{options['sender']}")
        event.add("ATTENDEE", f"mailto:{settings.MAILBOT_ADDRESS}")
        event.add("STATUS", "CANCELLED" if options["cancel"] else "CONFIRMED")
        calendar.add_component(event)

        message = EmailMultiAlternatives(
            subject=f"{'Cancelled' if options['cancel'] else 'Invitation'}: Mailbot test meeting",
            body=f"Test meeting at {start.isoformat()}\n{options['meeting_url']}\n",
            from_email=options["sender"],
            to=[settings.MAILBOT_ADDRESS],
        )
        message.attach_alternative(calendar.to_ical().decode("utf-8"), f"text/calendar; method={method}")
        message.send(fail_silently=False)
        self.stdout.write(f"Sent {method} for {uid} (sequence {options['sequence']}) starting at {start.isoformat()} to {settings.MAILBOT_ADDRESS}")
