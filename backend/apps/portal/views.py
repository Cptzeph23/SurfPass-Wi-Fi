import logging
from rest_framework.decorators import api_view, permission_classes, throttle_classes
from rest_framework.permissions import AllowAny
from rest_framework.response import Response
from rest_framework.throttling import AnonRateThrottle
from apps.sessions.models import Package, Session
from apps.sessions.service import SessionService

logger = logging.getLogger(__name__)


class PortalThrottle(AnonRateThrottle):
    rate = "30/minute"


def get_client_mac(request):
    return (
        request.GET.get("mac")
        or request.data.get("mac")
        or request.headers.get("X-Mac-Address")
        or request.META.get("HTTP_X_MAC_ADDRESS")
    )


def get_client_ip(request):
    forwarded = request.META.get("HTTP_X_FORWARDED_FOR")
    if forwarded:
        return forwarded.split(",")[0].strip()
    return request.META.get("REMOTE_ADDR")


@api_view(["GET"])
@permission_classes([AllowAny])
@throttle_classes([PortalThrottle])
def check_status(request):
    """
    Called on portal load.
    Returns whether this device has an active session.
    Query param: ?mac=XX:XX:XX:XX:XX:XX
    """
    mac = get_client_mac(request)
    ip = get_client_ip(request)

    if not mac:
        return Response({
            "has_access": False,
            "reason": "mac_not_detected",
            "session": None,
        })

    device = SessionService.get_or_create_device(mac, ip)

    if device.is_blocked:
        return Response({
            "has_access": False,
            "reason": "device_blocked",
            "session": None,
        })

    session = SessionService.check_active_session(mac)

    if session:
        return Response({
            "has_access": True,
            "session": {
                "id": str(session.id),
                "package_name": session.package.name,
                "expiry_time": session.expiry_time.isoformat(),
                "time_remaining_seconds": session.time_remaining_seconds,
                "time_remaining_display": session.time_remaining_display,
            },
        })

    return Response({
        "has_access": False,
        "reason": "no_active_session",
        "mac": mac,
        "session": None,
    })


@api_view(["GET"])
@permission_classes([AllowAny])
def list_packages(request):
    """Return all active packages."""
    packages = Package.objects.filter(is_active=True).order_by(
        "display_order", "price"
    )
    data = []
    for p in packages:
        data.append({
            "id": str(p.id),
            "name": p.name,
            "description": p.description,
            "price": float(p.price),
            "duration_minutes": p.duration_minutes,
            "duration_display": p.duration_display,
            "bandwidth_upload_kbps": p.bandwidth_upload_kbps,
            "bandwidth_download_kbps": p.bandwidth_download_kbps,
        })
    return Response({"packages": data})


@api_view(["POST"])
@permission_classes([AllowAny])
@throttle_classes([PortalThrottle])
def redeem_voucher(request):
    """
    Redeem a voucher code.
    Body: { "code": "ABCD123456", "mac": "XX:XX:XX:XX:XX:XX" }
    """
    code = request.data.get("code", "").strip()
    mac = get_client_mac(request)
    ip = get_client_ip(request)

    if not code:
        return Response(
            {"success": False, "error": "Voucher code is required."},
            status=400,
        )
    if not mac:
        return Response(
            {"success": False, "error": "Device MAC address not detected."},
            status=400,
        )

    try:
        session = SessionService.redeem_voucher(code, mac, ip)
        return Response({
            "success": True,
            "message": "Voucher redeemed. You now have internet access.",
            "session": {
                "id": str(session.id),
                "package_name": session.package.name,
                "expiry_time": session.expiry_time.isoformat(),
                "time_remaining_seconds": session.time_remaining_seconds,
                "time_remaining_display": session.time_remaining_display,
            },
        })
    except ValueError as e:
        return Response({"success": False, "error": str(e)}, status=400)
    except Exception as e:
        logger.exception("Voucher redeem error: %s", e)
        return Response(
            {"success": False, "error": "An unexpected error occurred."},
            status=500,
        )
