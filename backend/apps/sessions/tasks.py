import logging
from celery import shared_task
from django.utils import timezone

logger = logging.getLogger(__name__)


@shared_task(name="apps.sessions.tasks.expire_sessions", bind=True, max_retries=3)
def expire_sessions(self):
    """
    Expire all sessions past their expiry_time.
    Runs every 60 seconds via Celery Beat.
    """
    try:
        from apps.sessions.service import SessionService
        count = SessionService.expire_stale_sessions()
        return {
            "expired": count,
            "timestamp": timezone.now().isoformat(),
        }
    except Exception as exc:
        logger.error("expire_sessions task failed: %s", exc)
        raise self.retry(exc=exc, countdown=30)


@shared_task(
    name="apps.payments.tasks.check_pending_payment",
    bind=True,
    max_retries=8,
)
def check_pending_payment(self, payment_id):
    """
    Fallback polling task — queries M-Pesa for payment status
    if the Safaricom callback was not received within 35 seconds.
    """
    try:
        from apps.payments.models import Payment
        from apps.payments.mpesa import mpesa_client
        from apps.sessions.service import SessionService

        try:
            payment = Payment.objects.select_related(
                "device", "package"
            ).get(id=payment_id, status="pending")
        except Payment.DoesNotExist:
            return {"status": "already_processed"}

        if not payment.mpesa_checkout_request_id:
            return {"status": "no_checkout_id"}

        result = mpesa_client.query_stk_status(
            payment.mpesa_checkout_request_id
        )
        result_code = result.get("ResultCode")

        if str(result_code) == "0":
            receipt = result.get("MpesaReceiptNumber", "QUERIED")
            payment.mark_completed(receipt)
            SessionService.activate_session(payment)
            logger.info(
                "Payment confirmed via polling: %s receipt=%s",
                payment_id, receipt,
            )
            return {"status": "completed", "payment_id": payment_id}

        elif str(result_code) in ("1032", "1"):
            payment.mark_failed("Cancelled by user", str(result_code))
            return {"status": "cancelled"}

        else:
            # Still pending — retry
            raise self.retry(countdown=15)

    except Exception as exc:
        logger.error(
            "check_pending_payment failed for %s: %s",
            payment_id, exc,
        )
        raise self.retry(exc=exc, countdown=30)
