from django.contrib import admin
from django.urls import path, include
from django.http import FileResponse, Http404
from rest_framework_simplejwt.views import TokenObtainPairView, TokenRefreshView
import os

BASE_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
FRONTEND_DIR = os.path.join(os.path.dirname(BASE_DIR), 'frontend')


def serve_portal(request):
    f = os.path.join(FRONTEND_DIR, 'captive_portal', 'index.html')
    if os.path.exists(f):
        return FileResponse(open(f, 'rb'), content_type='text/html')
    raise Http404


def serve_admin(request):
    f = os.path.join(FRONTEND_DIR, 'admin', 'index.html')
    if os.path.exists(f):
        return FileResponse(open(f, 'rb'), content_type='text/html')
    raise Http404


urlpatterns = [
    path("", serve_portal),
    path("admin-panel/", serve_admin),
    path("django-admin/", admin.site.urls),
    path("api/token/", TokenObtainPairView.as_view(), name="token_obtain"),
    path("api/token/refresh/", TokenRefreshView.as_view(), name="token_refresh"),
    path("api/v1/portal/", include("apps.portal.urls")),
    path("api/v1/payments/", include("apps.payments.urls")),
    path("api/v1/sessions/", include("apps.sessions.urls")),
    path("api/v1/devices/", include("apps.devices.urls")),
    path("api/v1/admin/", include("apps.admin_dashboard.urls")),
]
