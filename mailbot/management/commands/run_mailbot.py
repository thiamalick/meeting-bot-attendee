import logging
import signal
import time

from django.conf import settings
from django.core.management import call_command
from django.core.management.base import BaseCommand, CommandError
from django.db import connection

from mailbot.events import get_mailbot_project
from mailbot.sources.imap import ImapMailSource
from mailbot.tasks import extend_recurring_series

log = logging.getLogger(__name__)

# Recurring series are extended and old raw emails purged on this period
MAINTENANCE_INTERVAL_SECONDS = 3600


class Command(BaseCommand):
    help = "Reads the bot mailbox over IMAP and queues the received emails for processing. Run a single instance."

    _keep_running = True

    def _graceful_exit(self, signum, frame):
        log.info("Received %s, shutting down after current cycle", signum)
        self._keep_running = False

    def _write_heartbeat(self):
        """Rewrite the heartbeat file so a liveness probe can detect a stalled loop."""
        if not settings.MAILBOT_HEARTBEAT_FILE:
            return
        try:
            with open(settings.MAILBOT_HEARTBEAT_FILE, "w") as f:
                f.write(str(time.time()))
        except Exception:
            log.warning("Failed to write mailbot heartbeat file", exc_info=True)

    def _check_configuration(self):
        if not settings.MAILBOT_ENABLED:
            raise CommandError("The mailbot is disabled, set MAILBOT_ENABLED=true.")
        missing = [name for name in ("MAILBOT_ADDRESS", "MAILBOT_PROJECT_ID", "MAILBOT_IMAP_HOST", "MAILBOT_IMAP_USER", "MAILBOT_IMAP_PASSWORD") if not getattr(settings, name)]
        if missing:
            raise CommandError(f"Missing settings: {', '.join(missing)}")
        if not settings.MAILBOT_ALLOWED_SENDER_DOMAINS:
            log.warning("MAILBOT_ALLOWED_SENDER_DOMAINS is empty: every email will be ignored")
        if settings.MAILBOT_REQUIRE_DMARC and not settings.MAILBOT_TRUSTED_AUTHSERV_ID:
            log.warning("MAILBOT_REQUIRE_DMARC is on but MAILBOT_TRUSTED_AUTHSERV_ID is empty: every email will be ignored")
        # Fails early if the project doesn't exist
        get_mailbot_project()

    def handle(self, *args, **options):
        self._check_configuration()
        signal.signal(signal.SIGINT, self._graceful_exit)
        signal.signal(signal.SIGTERM, self._graceful_exit)

        source = ImapMailSource()
        interval = settings.MAILBOT_POLL_INTERVAL_SECONDS
        last_maintenance = 0.0
        log.info("Mailbot started for %s, polling every %ss", settings.MAILBOT_ADDRESS, interval)
        self._write_heartbeat()

        while self._keep_running:
            began = time.monotonic()
            try:
                handled = source.poll_once()
                if handled:
                    log.info("Mailbot handled %d emails", handled)
                if began - last_maintenance >= MAINTENANCE_INTERVAL_SECONDS:
                    extend_recurring_series.delay()
                    call_command("purge_mailbot_raw_emails")
                    last_maintenance = began
                # A failed poll doesn't refresh the heartbeat, so a mailbox that stays unreachable is visible to the liveness probe
                self._write_heartbeat()
            except Exception:
                log.exception("Mailbot cycle failed")
            finally:
                # Close stale connections so the loop never inherits a dead socket
                connection.close()

            remaining_sleep = max(0, interval - (time.monotonic() - began))
            while remaining_sleep > 0 and self._keep_running:
                time.sleep(min(1, remaining_sleep))
                remaining_sleep -= 1

        log.info("Mailbot exited cleanly")
