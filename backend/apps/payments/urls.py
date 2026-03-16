from django.urls import path
from . import views

app_name = "wifi_payments"

urlpatterns = [
    path("initiate/", views.initiate_payment, name="initiate"),
    path("status/<str:payment_id>/", views.payment_status, name="status"),
    path("mpesa/callback/", views.mpesa_callback, name="mpesa_callback"),
]
