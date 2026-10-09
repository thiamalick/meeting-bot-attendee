import hmac

from django.conf import settings
from django.contrib.auth.mixins import LoginRequiredMixin
from django.http import Http404, JsonResponse
from django.shortcuts import redirect
from django.utils.decorators import method_decorator
from django.views import View
from django.views.decorators.csrf import csrf_exempt
from django.views.generic import ListView

from bots.projects_views import ProjectUrlContextMixin, get_project_for_user

from .ingest import ingest_raw_email
from .models import InboundEmail, InboundEmailSources, InboundEmailStates


@method_decorator(csrf_exempt, name="dispatch")
class InboundEmailView(View):
    """
    Lets an MTA push received emails, e.g. a Postfix pipe transport or a Stalwart webhook:
    POST the raw RFC 822 message as the body, with "Authorization: Bearer <MAILBOT_HTTP_INGEST_TOKEN>".
    """

    def post(self, request):
        token = settings.MAILBOT_HTTP_INGEST_TOKEN
        if not settings.MAILBOT_ENABLED or not token:
            raise Http404()

        authorization = request.headers.get("Authorization", "")
        if not hmac.compare_digest(authorization.encode(), f"Bearer {token}".encode()):
            return JsonResponse({"error": "Invalid token"}, status=401)

        if int(request.headers.get("Content-Length") or 0) > settings.MAILBOT_MAX_EMAIL_SIZE_BYTES:
            return JsonResponse({"error": "Email too large"}, status=413)
        raw = request.body
        if not raw:
            return JsonResponse({"error": "Empty body"}, status=400)

        inbound_email, created = ingest_raw_email(raw, InboundEmailSources.HTTP)
        return JsonResponse({"id": inbound_email.object_id, "duplicate": not created}, status=202)


class ProjectMailbotView(LoginRequiredMixin, ProjectUrlContextMixin, ListView):
    template_name = "mailbot/project_mailbot.html"
    context_object_name = "inbound_emails"
    paginate_by = 50

    def get(self, request, *args, **kwargs):
        try:
            self.project = get_project_for_user(user=request.user, project_object_id=kwargs["object_id"])
        except Exception:
            return redirect("/")
        return super().get(request, *args, **kwargs)

    def get_queryset(self):
        return InboundEmail.objects.filter(project=self.project).order_by("-created_at").prefetch_related("calendar_events")

    def get_context_data(self, **kwargs):
        context = super().get_context_data(**kwargs)
        context.update(self.get_project_context(self.kwargs["object_id"], self.project))
        context.update(
            {
                "InboundEmailStates": InboundEmailStates,
                "mailbot_enabled": settings.MAILBOT_ENABLED,
                "mailbot_address": settings.MAILBOT_ADDRESS,
                "mailbot_is_this_project": settings.MAILBOT_PROJECT_ID == self.project.object_id,
                "allowed_sender_domains": settings.MAILBOT_ALLOWED_SENDER_DOMAINS,
                "require_dmarc": settings.MAILBOT_REQUIRE_DMARC,
            }
        )
        return context
