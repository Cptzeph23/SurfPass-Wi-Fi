"""
SurfPass WiFi - Payment API Views
Handles M-Pesa STK Push initiation and callback processing
"""
import logging
from django.views.decorators.csrf import csrf_exempt
from django.utils.decorators import method_decorator
from rest_framework import status
from rest_framework.decorators import api_view, permission_classes, throttle_classes
from rest_framework.permissions import AllowAny
from rest_framework.response import Response
from rest_framework.throttling import AnonRateThrottle
from apps.payments.models import Payment
from apps.payments.mpesa import mpesa_client, MpesaError
from apps.sessions.models import Package
from apps.sessions.service import SessionService
from apps.sessions.tasks import check_pending_payment

logger = logging.getLogger(__name__)


class PaymentThrottle(AnonRateThrottle):
    rate = "10/minute"


def _get_client_mac(request) -> str | None:
    return (
        request.data.get("mac")
        or request.GET.get("mac")
        or request.headers.get("X-MAC-Address")
    )


def _get_client_ip(request) -> str:
    forwarded = request.META.get("HTTP_X_FORWARDED_FOR")
    if forwarded:
        return forwarded.split(",")[0].strip()
    return request.META.get("REMOTE_ADDR")


@api_view(["POST"])
@permission_classes([AllowAny])
@throttle_classes([PaymentThrottle])
def initiate_payment(request):
    """
    Initiate M-Pesa STK Push for a package purchase.

    Body:
        package_id: UUID of selected package
        phone_number: User's M-Pesa phone number
        mac: Device MAC address

    Returns:
        payment_id: For polling status
        message: User-friendly status message
    """
    package_id = request.data.get("package_id")
    phone_number = request.data.get("phone_number", "").strip()
    mac = _get_client_mac(request)
    ip = _get_client_ip(request)

    # Validate inputs
    errors = {}
    if not package_id:
        errors["package_id"] = "Package selection is required."
    if not phone_number:
        errors["phone_number"] = "Phone number is required."
    if not mac:
        errors["mac"] = "Device not detected. Please reconnect to the network."

    if errors:
        return Response({"success": False, "errors": errors}, status=400)

    # Validate phone number format
    normalized_phone = _normalize_phone(phone_number)
    if not normalized_phone:
        return Response(
            {"success": False, "errors": {"phone_number": "Enter a valid Kenyan phone number."}},
            status=400,
        )

    # Fetch package
    try:
        package = Package.objects.get(id=package_id, is_active=True)
    except Package.DoesNotExist:
        return Response({"success": False, "error": "Selected package not found."}, status=404)

    # Get or create device
    device = SessionService.get_or_create_device(mac, ip)

    if device.is_blocked:
        return Response({"success": False, "error": "This device has been blocked."}, status=403)

    # Create pending payment record
    payment = Payment.objects.create(
        device=device,
        package=package,
        phone_number=normalized_phone,
        amount=package.price,
        method=Payment.Method.MPESA,
        status=Payment.Status.PENDING,
    )

    # Initiate STK Push
    try:
        result = mpesa_client.initiate_stk_push(
            phone_number=normalized_phone,
            amount=int(package.price),
            account_reference=str(payment.id)[:12],
            transaction_desc=f"SurfPass {package.name}",
        )

        # Store M-Pesa IDs
        Payment.objects.filter(pk=payment.pk).update(
            mpesa_checkout_request_id=result["checkout_request_id"],
            mpesa_merchant_request_id=result["merchant_request_id"],
        )

        # Schedule fallback polling (in case callback is delayed/missing)
        check_pending_payment.apply_async(
            args=[str(payment.id)], countdown=35
        )

        logger.info(
            "STK Push sent: payment=%s phone=%s package=%s",
            payment.id, normalized_phone, package.name,
        )

        return Response({
            "success": True,
            "payment_id": str(payment.id),
            "checkout_request_id": result["checkout_request_id"],
            "message": f"A payment request of KES {int(package.price)} has been sent to {phone_number}. Please check your phone and enter your M-Pesa PIN.",
            "amount": float(package.price),
            "package_name": package.name,
        })

    except MpesaError as e:
        payment.mark_failed(str(e))
        logger.error("STK Push failed for payment %s: %s", payment.id, e)
        return Response(
            {"success": False, "error": "Payment initiation failed. Please try again."},
            status=502,
        )
    except Exception as e:
        payment.mark_failed(str(e))
        logger.exception("Unexpected error initiating payment %s: %s", payment.id, e)
        return Response(
            {"success": False, "error": "An unexpected error occurred."},
            status=500,
        )


@api_view(["GET"])
@permission_classes([AllowAny])
def payment_status(request, payment_id: str):
    """
    Poll payment status. Called by frontend every 5 seconds.

    Returns:
        status: pending | completed | failed | cancelled
        session: Active session data if completed
    """
    mac = _get_client_mac(request)

    try:
        payment = Payment.objects.select_related("device", "package", "session").get(
            id=payment_id
        )
    except Payment.DoesNotExist:
        return Response({"error": "Payment not found."}, status=404)

    # Security: verify payment belongs to requesting device
    if mac and payment.device.mac_address != mac.upper().replace("-", ":"):
        return Response({"error": "Unauthorized."}, status=403)

    response_data = {
        "payment_id": str(payment.id),
        "status": payment.status,
        "amount": float(payment.amount),
        "package_name": payment.package.name,
    }

    if payment.status == Payment.Status.COMPLETED and payment.session:
        session = payment.session
        response_data["session"] = {
            "id": str(session.id),
            "package_name": session.package.name,
            "expiry_time": session.expiry_time.isoformat(),
            "time_remaining_seconds": session.time_remaining_seconds,
            "time_remaining_display": session.time_remaining_display,
        }
    elif payment.status == Payment.Status.FAILED:
        response_data["failure_reason"] = payment.failure_reason or "Payment was not completed."

    return Response(response_data)


@api_view(["POST"])
@permission_classes([AllowAny])
@csrf_exempt
def mpesa_callback(request):
    """
    M-Pesa STK Push callback endpoint.
    Called by Safaricom servers when payment is confirmed or fails.
    This endpoint MUST be publicly accessible via HTTPS.
    """
    logger.info("M-Pesa callback received: %s", request.data)

    try:
        parsed = mpesa_client.parse_callback(request.data)
    except Exception as e:
        logger.error("Failed to parse M-Pesa callback: %s", e)
        return Response({"ResultCode": 0, "ResultDesc": "Accepted"})

    checkout_id = parsed.get("checkout_request_id")
    if not checkout_id:
        return Response({"ResultCode": 0, "ResultDesc": "Accepted"})

    # Find the payment record
    try:
        payment = Payment.objects.select_related("device", "package").get(
            mpesa_checkout_request_id=checkout_id,
            status=Payment.Status.PENDING,
        )
    except Payment.DoesNotExist:
        # Payment already processed (e.g., by polling task)
        logger.warning("Payment not found for checkout_id: %s", checkout_id)
        return Response({"ResultCode": 0, "ResultDesc": "Accepted"})

    if parsed["success"]:
        payment.mark_completed(
            receipt_number=parsed["receipt_number"],
            transaction_date=parsed.get("transaction_date"),
        )
        # Activate internet access
        try:
            SessionService.activate_session(payment)
            logger.info(
                "Payment completed and session activated: %s receipt=%s",
                payment.id, parsed["receipt_number"],
            )
        except Exception as e:
            logger.exception("Session activation failed after payment %s: %s", payment.id, e)
    else:
        result_code = parsed.get("result_code")
        result_desc = parsed.get("result_desc", "Payment failed")
        payment.mark_failed(result_desc, str(result_code))
        logger.info("Payment failed: %s code=%s desc=%s", payment.id, result_code, result_desc)

    # Always return 200 to M-Pesa
    return Response({"ResultCode": 0, "ResultDesc": "Accepted"})


def _normalize_phone(phone: str) -> str | None:
    """Normalize and validate Kenyan phone number."""
    phone = phone.strip().replace(" ", "").replace("-", "").replace("+", "")
    if phone.startswith("0") and len(phone) == 10:
        phone = "254" + phone[1:]
    elif (phone.startswith("7") or phone.startswith("1")) and len(phone) == 9:
        phone = "254" + phone
    elif phone.startswith("254") and len(phone) == 12:
        pass
    else:
        return None

    # Validate is a valid Kenyan mobile number
    valid_prefixes = ("2547", "2541")
    if not any(phone.startswith(p) for p in valid_prefixes):
        return None

    return phone