# Register your models here.

from django.contrib import admin

from .models import Organization, User


class UserInline(admin.TabularInline):
    model = User
    extra = 0
    fields = ("email", "first_name", "last_name", "is_active", "is_staff")
    readonly_fields = ("email", "first_name", "last_name", "is_active", "is_staff")
    can_delete = False


@admin.register(Organization)
class OrganizationAdmin(admin.ModelAdmin):
    list_display = ("name", "is_webhooks_enabled", "created_at", "updated_at")
    list_filter = ("is_webhooks_enabled",)
    search_fields = ("name",)
    readonly_fields = ("name", "is_webhooks_enabled", "created_at", "updated_at", "version")
    inlines = [UserInline]

    fieldsets = (
        ("Organization Information", {"fields": ("name", "is_webhooks_enabled")}),
        ("Metadata", {"fields": ("created_at", "updated_at", "version")}),
    )

    def has_add_permission(self, request):
        return False
