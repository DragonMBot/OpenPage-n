from django.contrib import admin
from django.contrib.auth.admin import UserAdmin

from ..knowledge.admin_mixins import StorageCleanupAdminMixin

from .models import PendingRegistration, User


# Пользователи в админке
@admin.register(User)
class CustomUserAdmin(StorageCleanupAdminMixin, UserAdmin):
    ordering = ("email",)
    list_display = (
        "email",
        "name",
        "is_staff",
        "is_active",
        "date_joined",
    )
    list_filter = ("is_active", "is_staff", "is_superuser")
    search_fields = ("email", "name")
    readonly_fields = ("last_login", "date_joined")

    fieldsets = (
        (None, {"fields": ("email", "password")}),
        ("Личные данные", {"fields": ("name",)}),
        (
            "Права доступа",
            {
                "fields": (
                    "is_active",
                    "is_staff",
                    "is_superuser",
                    "groups",
                    "user_permissions",
                )
            },
        ),
        ("Важные даты", {"fields": ("last_login", "date_joined")}),
    )

    add_fieldsets = (
        (
            None,
            {
                "classes": ("wide",),
                "fields": (
                    "email",
                    "name",
                    "password1",
                    "password2",
                    "is_active",
                    "is_staff",
                ),
            },
        ),
    )


# Хеши пароля и кода намеренно не показываются и не редактируются в админке.
@admin.register(PendingRegistration)
class PendingRegistrationAdmin(admin.ModelAdmin):
    list_display = (
        "email",
        "name",
        "sent_at",
        "expires_at",
        "failed_attempts",
    )
    search_fields = ("email", "name")
    ordering = ("-sent_at",)
    date_hierarchy = "sent_at"
    fields = (
        "email",
        "name",
        "sent_at",
        "expires_at",
        "failed_attempts",
    )
    readonly_fields = fields

    def has_add_permission(self, request):
        return False