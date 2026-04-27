"""
URL configuration for config project.

The `urlpatterns` list routes URLs to views. For more information please see:
    https://docs.djangoproject.com/en/4.2/topics/http/urls/
Examples:
Function views
    1. Add an import:  from my_app import views
    2. Add a URL to urlpatterns:  path('', views.home, name='home')
Class-based views
    1. Add an import:  from other_app.views import Home
    2. Add a URL to urlpatterns:  path('', Home.as_view(), name='home')
Including another URLconf
    1. Import the include() function: from django.urls import include, path
    2. Add a URL to urlpatterns:  path('blog/', include('blog.urls'))
"""
from django.contrib import admin
from django.urls import include, path
from django.views.generic import RedirectView

from portal.admin_views import reset_user_password_view

admin.site.site_header = "NEO ALARM BILLING SYSTEM"
admin.site.site_title = "NEO ALARM BILLING SYSTEM"
admin.site.index_title = "NEO ALARM BILLING SYSTEM"

urlpatterns = [
    path("", RedirectView.as_view(pattern_name="portal:dashboard", permanent=False)),
    path("app/", include("portal.urls")),
    path("admin-tools/users/<int:user_id>/reset-password/", admin.site.admin_view(reset_user_password_view), name="admin_user_reset_password"),
    path('admin/', admin.site.urls),
]
