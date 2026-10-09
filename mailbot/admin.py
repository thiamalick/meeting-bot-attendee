from django.contrib import admin

from .models import InboundEmail, RecurringSeries


@admin.register(InboundEmail)
class InboundEmailAdmin(admin.ModelAdmin):
    list_display = ("object_id", "created_at", "sender", "subject", "state", "outcome", "source")
    list_filter = ("state", "outcome", "source")
    search_fields = ("object_id", "sender", "subject", "message_id")
    ordering = ("-created_at",)
    # The audit trail must not be altered by hand
    readonly_fields = [field.name for field in InboundEmail._meta.fields] + ["calendar_events"]

    def has_add_permission(self, request):
        return False

    def has_change_permission(self, request, obj=None):
        return False


@admin.register(RecurringSeries)
class RecurringSeriesAdmin(admin.ModelAdmin):
    list_display = ("uid", "name", "calendar", "sequence", "is_cancelled", "horizon_end", "requested_by")
    list_filter = ("is_cancelled",)
    search_fields = ("uid", "name", "requested_by")
    readonly_fields = ("created_at", "updated_at")
