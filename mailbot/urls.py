from django.urls import path

from . import views

app_name = "mailbot"

urlpatterns = [
    path("inbound", views.InboundEmailView.as_view(), name="inbound"),
    path("projects/<str:object_id>", views.ProjectMailbotView.as_view(), name="project-mailbot"),
]
