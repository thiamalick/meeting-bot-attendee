import imaplib
import logging
import ssl

from django.conf import settings

from ..ingest import ingest_raw_email
from ..models import InboundEmailSources

logger = logging.getLogger(__name__)

# Messages handled per poll, so that a large backlog doesn't hold the IMAP connection for too long
BATCH_SIZE = 50


def _quote(mailbox: str) -> str:
    return '"' + mailbox.replace("\\", "\\\\").replace('"', '\\"') + '"'


class ImapMailSource:
    """
    Reads the bot mailbox over IMAP. Every message left in the inbox is unprocessed: once recorded it's moved
    to the processed folder. Recording is idempotent, so a crash between recording and moving is harmless.
    """

    def _connect(self) -> imaplib.IMAP4:
        host, port, security = settings.MAILBOT_IMAP_HOST, settings.MAILBOT_IMAP_PORT, settings.MAILBOT_IMAP_SECURITY
        if security == "ssl":
            connection = imaplib.IMAP4_SSL(host, port, ssl_context=ssl.create_default_context(), timeout=60)
        else:
            connection = imaplib.IMAP4(host, port, timeout=60)
            if security == "starttls":
                connection.starttls(ssl_context=ssl.create_default_context())
            elif security != "none":
                raise ValueError(f"Unsupported MAILBOT_IMAP_SECURITY: {security}")
        connection.login(settings.MAILBOT_IMAP_USER, settings.MAILBOT_IMAP_PASSWORD)
        return connection

    def _supports_move(self, connection: imaplib.IMAP4) -> bool:
        # Servers often advertise more capabilities once logged in than in their greeting
        status, data = connection.capability()
        return status == "OK" and b"MOVE" in (data[0] or b"").upper().split()

    def _move(self, connection: imaplib.IMAP4, uid: bytes, folder: str, supports_move: bool):
        if supports_move:
            status, _ = connection.uid("MOVE", uid, _quote(folder))
            if status == "OK":
                return
        status, _ = connection.uid("COPY", uid, _quote(folder))
        if status != "OK":
            raise RuntimeError(f"Could not copy message {uid!r} to {folder}")
        connection.uid("STORE", uid, "+FLAGS", "(\\Deleted)")
        connection.expunge()

    def poll_once(self) -> int:
        """Records the messages waiting in the inbox. Returns how many were handled."""
        connection = self._connect()
        try:
            # Fails harmlessly when the folder already exists
            connection.create(_quote(settings.MAILBOT_IMAP_PROCESSED_FOLDER))
            status, _ = connection.select(_quote(settings.MAILBOT_IMAP_FOLDER))
            if status != "OK":
                raise RuntimeError(f"Could not open the IMAP folder {settings.MAILBOT_IMAP_FOLDER}")

            status, data = connection.uid("SEARCH", None, "ALL")
            uids = data[0].split()[:BATCH_SIZE] if status == "OK" and data and data[0] else []
            supports_move = self._supports_move(connection) if uids else False
            for uid in uids:
                status, fetched = connection.uid("FETCH", uid, "(BODY.PEEK[])")
                raw = next((part[1] for part in fetched or [] if isinstance(part, tuple)), None)
                if status != "OK" or raw is None:
                    logger.warning(f"Mailbot could not fetch IMAP message {uid!r}, it will be retried")
                    continue
                ingest_raw_email(raw, InboundEmailSources.IMAP)
                self._move(connection, uid, settings.MAILBOT_IMAP_PROCESSED_FOLDER, supports_move)
            return len(uids)
        finally:
            try:
                connection.logout()
            except (imaplib.IMAP4.error, OSError):
                pass
