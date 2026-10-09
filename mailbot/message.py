import hashlib
import re
from email import policy
from email.message import EmailMessage
from email.parser import BytesParser
from email.utils import getaddresses

CALENDAR_CONTENT_TYPES = {"text/calendar", "application/ics"}

_DMARC_RE = re.compile(r"\bdmarc\s*=\s*([a-z]+)", re.IGNORECASE)
_DMARC_HEADER_FROM_RE = re.compile(r"\bdmarc\s*=\s*[a-z]+[^;]*?\bheader\.from\s*=\s*([^\s;]+)", re.IGNORECASE)


def parse_message(raw: bytes) -> EmailMessage:
    return BytesParser(policy=policy.default).parsebytes(raw)


def message_id(message: EmailMessage, raw: bytes) -> str:
    value = str(message.get("Message-ID") or "").strip()
    if value:
        return value[:998]
    # Some MTAs accept messages without a Message-ID, the content hash still makes redeliveries idempotent
    return f"<{hashlib.sha256(raw).hexdigest()}@mailbot.invalid>"


def sender_address(message: EmailMessage) -> str | None:
    """The single address in From, lowercased. None when From is missing, malformed or holds several addresses."""
    from_headers = message.get_all("From") or []
    if len(from_headers) != 1:
        return None
    addresses = [address for _, address in getaddresses([str(from_headers[0])]) if address]
    if len(addresses) != 1 or "@" not in addresses[0]:
        return None
    return addresses[0].strip().lower()


def subject(message: EmailMessage) -> str:
    return str(message.get("Subject") or "")[:1024]


def is_auto_submitted(message: EmailMessage) -> bool:
    """RFC 3834: never act on, or reply to, automatically generated messages."""
    auto_submitted = str(message.get("Auto-Submitted") or "no").strip().lower()
    precedence = str(message.get("Precedence") or "").strip().lower()
    return auto_submitted != "no" or precedence in ("bulk", "junk", "list")


def authentication_results(message: EmailMessage, trusted_authserv_id: str) -> dict | None:
    """
    The DMARC verdict from the topmost Authentication-Results header written by our own MTA (RFC 8601).
    The receiving MTA prepends its header, so headers further down could have been forged by the sender.
    """
    if not trusted_authserv_id:
        return None
    for header in message.get_all("Authentication-Results") or []:
        value = " ".join(str(header).split())
        authserv_id = value.split(";", 1)[0].strip().split(" ")[0].lower()
        if authserv_id != trusted_authserv_id:
            continue
        dmarc = _DMARC_RE.search(value)
        header_from = _DMARC_HEADER_FROM_RE.search(value)
        return {
            "authserv_id": authserv_id,
            "dmarc": dmarc.group(1).lower() if dmarc else None,
            "header_from": header_from.group(1).lower() if header_from else None,
        }
    return None


def calendar_payloads(message: EmailMessage) -> list[bytes]:
    """iCalendar payloads found anywhere in the message, including in forwarded messages. Identical payloads are returned once."""
    payloads = []
    for part in message.walk():
        if part.is_multipart():
            continue
        filename = (part.get_filename() or "").lower()
        if part.get_content_type() not in CALENDAR_CONTENT_TYPES and not filename.endswith(".ics"):
            continue
        payload = part.get_payload(decode=True)
        if payload and payload not in payloads:
            payloads.append(payload)
    return payloads


def body_text(message: EmailMessage) -> str:
    """Plain text and HTML bodies concatenated, used as a last resort to find the meeting URL."""
    texts = []
    for part in message.walk():
        if part.is_multipart() or part.get_content_type() not in ("text/plain", "text/html"):
            continue
        if part.get_content_disposition() == "attachment":
            continue
        try:
            texts.append(part.get_content())
        except (LookupError, UnicodeDecodeError):
            payload = part.get_payload(decode=True) or b""
            texts.append(payload.decode("utf-8", errors="replace"))
    return "\n".join(texts)
