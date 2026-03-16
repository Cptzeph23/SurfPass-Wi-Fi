import uuid
from django.db import models
from django.utils import timezone


class Payment(models.Model):

    class Status(models.TextChoices):
        PENDING = "pending", "Pending"
        COMPLETED = "completed", "Completed"
        FAILED = "failed", "Failed"
        CANCELLED = "cancelled", "Cancelled"

    class Method(models.TextChoices):
        MPESA = "mpesa", "M-Pesa"
        VOUCHER = "voucher", "Voucher"
        ADMIN = "admin", "Admin Grant"

    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    device = models.ForeignKey(
        "wifi_devices.Device",
        on_delete=models.CASCADE,
        related_name="payments",
    )
    package = models.ForeignKey(
        "wifi_sessions.Package",
        on_delete=models.PROTECT,
        related_name="payments",
    )
    session = models.OneToOneField(
        "wifi_sessions.Session",
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        related_name="payment",
    )
    phone_number = models.CharField(max_length=15)
    amount = models.DecimalField(max_digits=8, decimal_places=2)
    method = models.CharField(
        max_length=20,
        choices=Method.choices,
        default=Method.MPESA,
    )
    status = models.CharField(
        max_length=20,
        choices=Status.choices,
        default=Status.PENDING,
    )
    mpesa_checkout_request_id = models.CharField(
        max_length=100, blank=True, null=True, unique=True
    )
    mpesa_merchant_request_id = models.CharField(
        max_length=100, blank=True, null=True
    )
    mpesa_receipt_number = models.CharField(
        max_length=50, blank=True, null=True
    )
    mpesa_transaction_date = models.DateTimeField(blank=True, null=True)
    mpesa_result_code = models.CharField(max_length=10, blank=True, null=True)
    mpesa_result_desc = models.TextField(blank=True, null=True)
    failure_reason = models.TextField(blank=True, null=True)
    initiated_at = models.DateTimeField(default=timezone.now)
    completed_at = models.DateTimeField(blank=True, null=True)
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        app_label = "wifi_payments"
        db_table = "payments"
        ordering = ["-created_at"]
        indexes = [
            models.Index(fields=["status", "created_at"]),
            models.Index(fields=["mpesa_checkout_request_id"]),
            models.Index(fields=["phone_number", "status"]),
        ]

    def __str__(self):
        return f"Payment {self.id} - {self.phone_number} KES {self.amount} ({self.status})"

    def mark_completed(self, receipt_number, transaction_date=None):
        self.status = self.Status.COMPLETED
        self.mpesa_receipt_number = receipt_number
        self.mpesa_transaction_date = transaction_date or timezone.now()
        self.completed_at = timezone.now()
        self.save(update_fields=[
            "status",
            "mpesa_receipt_number",
            "mpesa_transaction_date",
            "completed_at",
            "updated_at",
        ])

    def mark_failed(self, reason, result_code=None):
        self.status = self.Status.FAILED
        self.failure_reason = reason
        self.mpesa_result_code = result_code
        self.save(update_fields=[
            "status",
            "failure_reason",
            "mpesa_result_code",
            "updated_at",
        ])
