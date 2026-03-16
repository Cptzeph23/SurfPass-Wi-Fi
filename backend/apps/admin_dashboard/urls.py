from django.urls import path
from . import views

app_name = "wifi_admin"

urlpatterns = [
    path("overview/", views.dashboard_overview, name="overview"),
    path("sessions/active/", views.active_sessions, name="active_sessions"),
    path("sessions/<str:session_id>/terminate/", views.terminate_session, name="terminate_session"),
    path("devices/", views.device_list, name="device_list"),
    path("devices/<str:mac_address>/block/", views.block_device, name="block_device"),
    path("vouchers/generate/", views.generate_vouchers, name="generate_vouchers"),
    path("revenue/chart/", views.revenue_chart, name="revenue_chart"),
]
