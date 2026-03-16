import logging
from datetime import timedelta
from django.utils import timezone
from django.db.models import Sum, Count, Q
from rest_framework.decorators import api_view, permission_classes
from rest_framework.permissions import IsAdminUser
from rest_framework.response import Response
from apps.devices.models import Device
from apps.sessions.models import Session, Package, Voucher
from apps.payments.models import Payment
from apps.sessions.service import SessionService
import secrets
import string

logger = logging.getLogger(__name__)


@api_view(["GET"])
@permission_classes([IsAdminUser])
def dashboard_overview(request):
    """Revenue, session, device summary for the dashboard."""
    now = timezone.now()
    today_start = now.replace(hour=0, minute=0, second=0, microsecond=0)
    month_start = now.replace(day=1, hour=0, minute=0, second=0, microsecond=0)

    revenue_today = Payment.objects.filter(
        status="completed", completed_at__gte=today_start
    ).aggregate(total=Sum("amount"))["total"] or 0

    revenue_month = Payment.objects.filter(
        status="completed", completed_at__gte=month_start
    ).aggregate(total=Sum("amount"))["total"] or 0

    revenue_all = Payment.objects.filter(
        status="completed"
    ).aggregate(total=Sum("amount"))["total"] or 0

    active_sessions = Session.objects.filter(
        status=Session.Status.ACTIVE,
        expiry_time__gt=now,
    ).count()

    sessions_today = Session.objects.filter(
        start_time__gte=today_start,
        status__in=[
            Session.Status.ACTIVE,
            Session.Status.EXPIRED,
            Session.Status.TERMINATED,
        ],
    ).count()

    payments_today = Payment.objects.filter(
        status="completed",
        completed_at__gte=today_start,
    ).count()

    total_devices = Device.objects.count()
    devices_today = Device.objects.filter(last_seen__gte=today_start).count()

    package_stats = (
        Payment.objects.filter(status="completed")
        .values("package__name")
        .annotate(count=Count("id"), revenue=Sum("amount"))
        .order_by("-count")[:5]
    )

    return Response({
        "revenue": {
            "today": float(revenue_today),
            "this_month": float(revenue_month),
            "all_time": float(revenue_all),
        },
        "sessions": {
            "active_now": active_sessions,
            "today": sessions_today,
        },
        "payments": {
            "completed_today": payments_today,
        },
        "devices": {
            "total": total_devices,
            "online_today": devices_today,
        },
        "package_stats": list(package_stats),
    })


@api_view(["GET"])
@permission_classes([IsAdminUser])
def active_sessions(request):
    """List all currently active sessions."""
    now = timezone.now()
    sessions = Session.objects.filter(
        status=Session.Status.ACTIVE,
        expiry_time__gt=now,
    ).select_related("device", "package").order_by("expiry_time")

    data = []
    for s in sessions:
        data.append({
            "session_id": str(s.id),
            "mac_address": s.device.mac_address,
            "phone_number": s.device.phone_number,
            "ip_address": s.ip_address,
            "package_name": s.package.name,
            "started_at": s.start_time.isoformat(),
            "expires_at": s.expiry_time.isoformat(),
            "time_remaining_seconds": s.time_remaining_seconds,
            "time_remaining_display": s.time_remaining_display,
        })

    return Response({"active_sessions": data, "count": len(data)})


@api_view(["POST"])
@permission_classes([IsAdminUser])
def terminate_session(request, session_id):
    """Manually disconnect a user."""
    try:
        session = Session.objects.select_related("device").get(
            id=session_id,
            status=Session.Status.ACTIVE,
        )
    except Session.DoesNotExist:
        return Response({"error": "Active session not found."}, status=404)

    success = SessionService.terminate_session(session, reason="admin")
    if success:
        return Response({
            "success": True,
            "message": f"Session {session_id} terminated.",
        })
    return Response(
        {"success": False, "error": "Failed to terminate session."},
        status=500,
    )


@api_view(["GET"])
@permission_classes([IsAdminUser])
def device_list(request):
    """Paginated list of all known devices."""
    page = int(request.GET.get("page", 1))
    per_page = int(request.GET.get("per_page", 50))
    search = request.GET.get("search", "").strip()
    offset = (page - 1) * per_page

    qs = Device.objects.all()
    if search:
        qs = qs.filter(
            Q(mac_address__icontains=search)
            | Q(phone_number__icontains=search)
        )

    total = qs.count()
    devices = qs.order_by("-last_seen")[offset: offset + per_page]

    now = timezone.now()
    data = []
    for d in devices:
        active = Session.objects.filter(
            device=d,
            status=Session.Status.ACTIVE,
            expiry_time__gt=now,
        ).select_related("package").first()

        data.append({
            "id": str(d.id),
            "mac_address": d.mac_address,
            "phone_number": d.phone_number,
            "ip_address": d.ip_address,
            "first_seen": d.first_seen.isoformat(),
            "last_seen": d.last_seen.isoformat(),
            "is_blocked": d.is_blocked,
            "total_sessions": d.total_sessions,
            "total_spent": float(d.total_spent),
            "active_session": {
                "id": str(active.id),
                "package": active.package.name,
                "time_remaining": active.time_remaining_display,
            } if active else None,
        })

    return Response({
        "devices": data,
        "total": total,
        "page": page,
        "per_page": per_page,
    })


@api_view(["POST"])
@permission_classes([IsAdminUser])
def block_device(request, mac_address):
    """Block or unblock a device by MAC address."""
    try:
        device = Device.objects.get(
            mac_address=mac_address.upper()
        )
    except Device.DoesNotExist:
        return Response({"error": "Device not found."}, status=404)

    action = request.data.get("action", "block")

    if action == "block":
        device.is_blocked = True
        device.block_reason = request.data.get("reason", "Blocked by admin")
        device.save(update_fields=["is_blocked", "block_reason"])
        for s in Session.objects.filter(
            device=device, status=Session.Status.ACTIVE
        ):
            SessionService.terminate_session(s, reason="admin_block")
        return Response({"success": True, "message": "Device blocked."})

    device.is_blocked = False
    device.block_reason = None
    device.save(update_fields=["is_blocked", "block_reason"])
    return Response({"success": True, "message": "Device unblocked."})


@api_view(["POST"])
@permission_classes([IsAdminUser])
def generate_vouchers(request):
    """Generate a batch of voucher codes for a given package."""
    package_id = request.data.get("package_id")
    quantity = int(request.data.get("quantity", 1))
    expires_at = request.data.get("expires_at")

    if not 1 <= quantity <= 100:
        return Response(
            {"error": "Quantity must be between 1 and 100."},
            status=400,
        )

    try:
        package = Package.objects.get(id=package_id, is_active=True)
    except Package.DoesNotExist:
        return Response({"error": "Package not found."}, status=404)

    alphabet = string.ascii_uppercase + string.digits
    vouchers = []
    codes = []

    for _ in range(quantity):
        code = "".join(secrets.choice(alphabet) for _ in range(10))
        codes.append(code)
        vouchers.append(Voucher(
            code=code,
            package=package,
            expires_at=expires_at,
            created_by=request.user.username,
        ))

    Voucher.objects.bulk_create(vouchers)

    return Response({
        "success": True,
        "count": quantity,
        "package": package.name,
        "vouchers": codes,
    })


@api_view(["GET"])
@permission_classes([IsAdminUser])
def revenue_chart(request):
    """Daily revenue data for the last N days."""
    days = int(request.GET.get("days", 7))
    now = timezone.now()
    data = []

    for i in range(days - 1, -1, -1):
        day = now - timedelta(days=i)
        day_start = day.replace(hour=0, minute=0, second=0, microsecond=0)
        day_end = day.replace(hour=23, minute=59, second=59, microsecond=999999)

        result = Payment.objects.filter(
            status="completed",
            completed_at__range=(day_start, day_end),
        ).aggregate(revenue=Sum("amount"), transactions=Count("id"))

        data.append({
            "date": day_start.strftime("%Y-%m-%d"),
            "revenue": float(result["revenue"] or 0),
            "transactions": result["transactions"],
        })

    return Response({"revenue_chart": data, "days": days})
