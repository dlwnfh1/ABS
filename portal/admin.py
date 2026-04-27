from django.contrib import admin, messages
from django.contrib.auth.admin import UserAdmin
from django.contrib.auth.models import User
from django.urls import reverse
from django.utils.crypto import get_random_string
from django.utils.html import format_html, format_html_join


@admin.action(description="Reset selected passwords")
def reset_selected_passwords(modeladmin, request, queryset):
    reset_rows = []
    alphabet = "ABCDEFGHJKLMNPQRSTUVWXYZabcdefghijkmnopqrstuvwxyz23456789"
    for user in queryset.order_by("username"):
        temp_password = get_random_string(10, allowed_chars=alphabet)
        user.set_password(temp_password)
        user.save(update_fields=["password"])
        reset_rows.append((user.username, temp_password))

    if not reset_rows:
        modeladmin.message_user(request, "No users were selected.", level=messages.WARNING)
        return

    reset_html = format_html_join(
        "<br>",
        "{}: <strong>{}</strong>",
        ((username, password) for username, password in reset_rows),
    )
    modeladmin.message_user(
        request,
        format_html(
            "Temporary passwords have been reset for {} user(s):<br>{}",
            len(reset_rows),
            reset_html,
        ),
        level=messages.WARNING,
    )


admin.site.unregister(User)


@admin.register(User)
class ABSUserAdmin(UserAdmin):
    actions = [reset_selected_passwords]
    change_form_template = "admin/auth/user/change_form.html"
    list_display = UserAdmin.list_display + ("reset_password_link",)

    def get_fieldsets(self, request, obj=None):
        fieldsets = list(super().get_fieldsets(request, obj))
        if obj:
            fieldsets.append(("Password Reset", {"fields": ("reset_password_button",)}))
        return fieldsets

    def get_readonly_fields(self, request, obj=None):
        readonly_fields = list(super().get_readonly_fields(request, obj))
        if obj and "reset_password_button" not in readonly_fields:
            readonly_fields.append("reset_password_button")
        return readonly_fields

    @admin.display(description="Reset password")
    def reset_password_button(self, obj):
        if not obj or not obj.pk:
            return "-"
        url = reverse("admin_user_reset_password", args=[obj.pk])
        return format_html(
            '<a class="button" href="{}" style="padding:8px 12px; background:#ba2121; color:#fff; border-radius:4px; text-decoration:none;">Reset Password</a>',
            url,
        )

    @admin.display(description="Reset password")
    def reset_password_link(self, obj):
        return self.reset_password_button(obj)
