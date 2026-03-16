from django.urls import path
from . import views

app_name = "wifi_portal"

urlpatterns = [
    path("status/", views.check_status, name="check_status"),
    path("packages/", views.list_packages, name="list_packages"),
    path("voucher/redeem/", views.redeem_voucher, name="redeem_voucher"),
]
