from dataclasses import dataclass

from django.conf import settings

from .models import InboundEmailOutcomes


@dataclass
class AuthorizationDecision:
    allowed: bool
    outcome: str | None = None
    reason: str = ""


def authorize_sender(sender: str | None, auth_results: dict | None) -> AuthorizationDecision:
    """
    Deny by default: the sender domain must be explicitly allowed and, unless disabled, its DMARC check must
    have passed on our own MTA. Authorization relies on the authenticated sender rather than on the meeting
    organizer, so that an employee can forward an invitation received from an external partner.
    """
    if not sender:
        return AuthorizationDecision(False, InboundEmailOutcomes.UNAUTHORIZED_SENDER, "The From header is missing or holds several addresses.")

    if settings.MAILBOT_ADDRESS and sender == settings.MAILBOT_ADDRESS:
        return AuthorizationDecision(False, InboundEmailOutcomes.LOOP, "The email was sent by the bot itself.")

    domain = sender.rsplit("@", 1)[1]
    if domain not in settings.MAILBOT_ALLOWED_SENDER_DOMAINS:
        return AuthorizationDecision(False, InboundEmailOutcomes.UNAUTHORIZED_SENDER, f"The sender domain {domain} is not in MAILBOT_ALLOWED_SENDER_DOMAINS.")

    if settings.MAILBOT_REQUIRE_DMARC:
        if not auth_results:
            return AuthorizationDecision(False, InboundEmailOutcomes.UNAUTHENTICATED_SENDER, f"No Authentication-Results header from the trusted server '{settings.MAILBOT_TRUSTED_AUTHSERV_ID}' was found.")
        if auth_results.get("dmarc") != "pass":
            return AuthorizationDecision(False, InboundEmailOutcomes.UNAUTHENTICATED_SENDER, f"The DMARC check did not pass (dmarc={auth_results.get('dmarc')}).")
        if auth_results.get("header_from") and auth_results["header_from"] != domain:
            return AuthorizationDecision(False, InboundEmailOutcomes.UNAUTHENTICATED_SENDER, f"DMARC was evaluated for {auth_results['header_from']}, not for the sender domain {domain}.")

    return AuthorizationDecision(True)
