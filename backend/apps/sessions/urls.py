from django.urls import path
from rest_framework.decorators import api_view, permission_classes
from rest_framework.permissions import AllowAny
from rest_framework.response import Response
from .models import Session

app_name = "wifi_sessions"


@api_view(["GET"])
@permission_classes([AllowAny])
def session_detail(request, session_id):
    try:
        session = Session.objects.select_related(
            "device", "package"
        ).get(id=session_id)
    except Session.DoesNotExist:
        return Response({"error": "Session not found."}, status=404)

    return Response({
        "id": str(session.id),
        "status": session.status,
        "package_name": session.package.name,
        "mac_address": session.device.mac_address,
        "start_time": session.start_time.isoformat(),
        "expiry_time": session.expiry_time.isoformat(),
        "time_remaining_seconds": session.time_remaining_seconds,
        "time_remaining_display": session.time_remaining_display,
        "is_active": session.is_active,
    })


urlpatterns = [
    path("<str:session_id>/", session_detail, name="detail"),
]
