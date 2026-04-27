from django.contrib import messages
from django.contrib.auth.models import User
from django.shortcuts import get_object_or_404, redirect
from django.utils.crypto import get_random_string
from django.utils.html import format_html


def reset_user_password_view(request, user_id):
    user = get_object_or_404(User, pk=user_id)
    alphabet = "ABCDEFGHJKLMNPQRSTUVWXYZabcdefghijkmnopqrstuvwxyz23456789"
    temp_password = get_random_string(10, allowed_chars=alphabet)
    user.set_password(temp_password)
    user.save(update_fields=["password"])
    messages.warning(
        request,
        format_html(
            "Temporary password for <strong>{}</strong> has been reset to <strong>{}</strong>.",
            user.username,
            temp_password,
        ),
    )
    return redirect("admin:auth_user_change", user_id)
