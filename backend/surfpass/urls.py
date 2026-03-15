from django.contrib import admin
from django.urls import path, include

urlpatterns = [
    path("django-admin/", admin.site.urls),
    path("api/v1/portal/", include("apps.portal.urls")),
    path("api/v1/payments/", include("apps.payments.urls")),
    path("api/v1/sessions/", include("apps.sessions.urls")),
    path("api/v1/devices/", include("apps.devices.urls")),
    path("api/v1/admin/", include("apps.admin_dashboard.urls")),
]