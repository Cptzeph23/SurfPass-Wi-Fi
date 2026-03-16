from django.contrib import admin
from django.urls import path, include
from rest_framework_simplejwt.views import (
    TokenObtainPairView,
    TokenRefreshView,
)

urlpatterns = [
    path("django-admin/", admin.site.urls),
    path("api/token/", TokenObtainPairView.as_view(), name="token_obtain"),
    path("api/token/refresh/", TokenRefreshView.as_view(), name="token_refresh"),
    path("api/v1/portal/", include("apps.portal.urls")),
    path("api/v1/payments/", include("apps.payments.urls")),
    path("api/v1/sessions/", include("apps.sessions.urls")),
    path("api/v1/devices/", include("apps.devices.urls")),
    path("api/v1/admin/", include("apps.admin_dashboard.urls")),
]
