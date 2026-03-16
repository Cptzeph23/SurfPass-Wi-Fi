import logging
from django.views.decorators.csrf import csrf_exempt
from rest_framework.decorators import api_view, permission_classes, throttle_classes
from rest_framework.permissions import AllowAny
from rest_framework.response import Response
from rest_framework.throttling import AnonRateThrottle
from apps.payments.models import Payment
from apps.payments.mpesa import mpesa_client, MpesaClient, MpesaError
from apps.sessions.models import Package
from apps.sessions.service import SessionService
from apps.sessions.tasks import check_pending_payment

logger = logging.getLogger(__name__)


class PaymentThrottle(AnonRateThrottle):
    rate = "10/minute"


def get_client_mac(request):
    return (
        request.data.get("mac")
        or request.GET.get("mac")
        or request.headers.get("X-Mac-Address")
        or request.META.get("HTTP_X_MAC_ADDRESS")
    )


def get_client_ip(request):
    forwarded = request.META.get("HTTP_X_FORWARDED_FOR")
    if forwarded:
        return forwarded.split(",")[0].strip()
    return request.META.get("REMOTE_ADDR")


@api_view(["POST"])
@permission_classes([AllowAny])
@throttle_classes([PaymentThrottle])
def initiate_payment(request):
    """
    Start an M-Pesa STK Push for a package purchase.

    Body:
        package_id   : UUID of selected package
        phone_number : Kenyan mobile number (any format)
        mac          : Device MAC address
    """
    package_id = request.data.get("package_id")
    phone_number = request.data.get("phone_number", "").strip()
    mac = get_client_mac(request)
    ip = get_client_ip(request)

    # ── Validate ──────────────────────────────────────────────────────────────
    errors = {}
    if not package_id:
        errors["package_id"] = "Please select a package."
    if not phone_number:
        errors["phone_number"] = "Phone number is required."
    if not mac:
        errors["mac"] = "Device not detected. Reconnect to the network."
    if errors:
        return Response({"success": False, "errors": errors}, status=400)

    normalized_phone = MpesaClient.validate_phone(phone_number)
    if not normalized_phone:
        return Response(
            {
                "success": False,
                "errors": {
                    "phone_number": "Enter a valid Kenyan mobile number."
                },
            },
            status=400,
        )

    # ── Fetch package ─────────────────────────────────────────────────────────
    try:
        package = Package.objects.get(id=package_id, is_active=True)
    except Package.DoesNotExist:
        return Response(
            {"success": False, "error": "Selected package not found."},
            status=404,
        )

    # ── Get / create device ───────────────────────────────────────────────────
    device = SessionService.get_or_create_device(mac, ip)

    if device.is_blocked:
        return Response(
            {"success": False, "error": "This device has been blocked."},
            status=403,
        )

    # ── Create pending payment record ─────────────────────────────────────────
    payment = Payment.objects.create(
        device=device,
        package=package,
        phone_number=normalized_phone,
        amount=package.price,
        method=Payment.Method.MPESA,
        status=Payment.Status.PENDING,
    )

    # ── Initiate STK Push ─────────────────────────────────────────────────────
    try:
        result = mpesa_client.initiate_stk_push(
            phone_number=normalized_phone,
            amount=int(package.price),
            account_reference=str(payment.id)[:12],
            transaction_desc=f"SurfPass WiFi",
        )

        Payment.objects.filter(pk=payment.pk).update(
            mpesa_checkout_request_id=result["checkout_request_id"],
            mpesa_merchant_request_id=result["merchant_request_id"],
        )

        # Schedule fallback polling in case callback is delayed
        check_pending_payment.apply_async(
            args=[str(payment.id)],
            countdown=35,
        )

        logger.info(
            "STK Push initiated: payment=%s phone=%s package=%s",
            payment.id, normalized_phone, package.name,
        )

        return Response({
            "success": True,
            "payment_id": str(payment.id),
            "checkout_request_id": result["checkout_request_id"],
            "message": (
                f"A payment request of KES {int(package.price)} "
                f"has been sent to {phone_number}. "
                f"Enter your M-Pesa PIN to confirm."
            ),
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
        logger.exception("Unexpected payment error %s: %s", payment.id, e)
        return Response(
            {"success": False, "error": "An unexpected error occurred."},
            status=500,
        )


@api_view(["GET"])
@permission_classes([AllowAny])
def payment_status(request, payment_id):
    """
    Poll payment status. Frontend calls this every 5 seconds.
    Returns current status and session details if payment completed.
    """
    mac = get_client_mac(request)

    try:
        payment = Payment.objects.select_related(
            "device", "package", "session"
        ).get(id=payment_id)
    except Payment.DoesNotExist:
        return Response({"error": "Payment not found."}, status=404)

    if mac:
        device_mac = (
            mac.upper().replace("-", ":").replace(".", ":")
        )
        if payment.device.mac_address != device_mac:
            return Response({"error": "Unauthorized."}, status=403)

    data = {
        "payment_id": str(payment.id),
        "status": payment.status,
        "amount": float(payment.amount),
        "package_name": payment.package.name,
    }

    if payment.status == Payment.Status.COMPLETED and payment.session:
        s = payment.session
        data["session"] = {
            "id": str(s.id),
            "package_name": s.package.name,
            "expiry_time": s.expiry_time.isoformat(),
            "time_remaining_seconds": s.time_remaining_seconds,
            "time_remaining_display": s.time_remaining_display,
        }
        data["receipt"] = payment.mpesa_receipt_number

    elif payment.status == Payment.Status.FAILED:
        data["failure_reason"] = (
            payment.failure_reason or "Payment was not completed."
        )

    return Response(data)


@csrf_exempt
@api_view(["POST"])
@permission_classes([AllowAny])
def mpesa_callback(request):
    """
    Safaricom STK Push callback.
    Called by Safaricom servers after user confirms or cancels payment.
    Must be publicly accessible via HTTPS.
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

    try:
        payment = Payment.objects.select_related(
            "device", "package"
        ).get(
            mpesa_checkout_request_id=checkout_id,
            status=Payment.Status.PENDING,
        )
    except Payment.DoesNotExist:
        logger.warning(
            "Callback received for unknown/processed checkout: %s",
            checkout_id,
        )
        return Response({"ResultCode": 0, "ResultDesc": "Accepted"})

    if parsed["success"]:
        payment.mark_completed(
            receipt_number=parsed["receipt_number"],
            transaction_date=parsed.get("transaction_date"),
        )
        try:
            SessionService.activate_session(payment)
            logger.info(
                "Session activated via callback: payment=%s receipt=%s",
                payment.id, parsed["receipt_number"],
            )
        except Exception as e:
            logger.exception(
                "Session activation failed after payment %s: %s",
                payment.id, e,
            )
    else:
        payment.mark_failed(
            parsed.get("result_desc", "Payment failed"),
            str(parsed.get("result_code")),
        )
        logger.info(
            "Payment failed via callback: payment=%s code=%s",
            payment.id, parsed.get("result_code"),
        )

    return Response({"ResultCode": 0, "ResultDesc": "Accepted"})
