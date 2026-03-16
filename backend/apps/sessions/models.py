import uuid
from django.db import models
from django.utils import timezone


class Package(models.Model):
    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    name = models.CharField(max_length=100)
    description = models.TextField(blank=True)
    price = models.DecimalField(max_digits=8, decimal_places=2)
    duration_minutes = models.PositiveIntegerField()
    bandwidth_upload_kbps = models.PositiveIntegerField(default=0)
    bandwidth_download_kbps = models.PositiveIntegerField(default=0)
    is_active = models.BooleanField(default=True)
    display_order = models.PositiveSmallIntegerField(default=0)
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        app_label = "wifi_sessions"
        db_table = "packages"
        ordering = ["display_order", "price"]

    def __str__(self):
        return f"{self.name} - KES {self.price}"

    @property
    def duration_display(self):
        mins = self.duration_minutes
        if mins < 60:
            return f"{mins} Minutes"
        hours = mins // 60
        remaining = mins % 60
        if remaining:
            return f"{hours}h {remaining}m"
        return f"{hours} Hour{'s' if hours > 1 else ''}"


class Session(models.Model):

    class Status(models.TextChoices):
        ACTIVE = "active", "Active"
        EXPIRED = "expired", "Expired"
        TERMINATED = "terminated", "Terminated"
        PENDING = "pending", "Pending"

    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    device = models.ForeignKey(
        "wifi_devices.Device",
        on_delete=models.CASCADE,
        related_name="sessions",
    )
    package = models.ForeignKey(
        Package,
        on_delete=models.PROTECT,
        related_name="sessions",
    )
    start_time = models.DateTimeField(default=timezone.now)
    expiry_time = models.DateTimeField()
    status = models.CharField(
        max_length=20,
        choices=Status.choices,
        default=Status.PENDING,
    )
    ip_address = models.GenericIPAddressField(blank=True, null=True)
    bytes_uploaded = models.BigIntegerField(default=0)
    bytes_downloaded = models.BigIntegerField(default=0)
    terminated_by = models.CharField(max_length=50, blank=True, null=True)
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        app_label = "wifi_sessions"
        db_table = "wifi_sessions"
        ordering = ["-created_at"]
        indexes = [
            models.Index(fields=["status", "expiry_time"]),
            models.Index(fields=["device", "status"]),
        ]

    def __str__(self):
        return f"Session {self.id} - {self.device.mac_address} ({self.status})"

    @property
    def is_active(self):
        return (
            self.status == self.Status.ACTIVE
            and self.expiry_time > timezone.now()
        )

    @property
    def time_remaining_seconds(self):
        if not self.is_active:
            return 0
        delta = self.expiry_time - timezone.now()
        return max(0, int(delta.total_seconds()))

    @property
    def time_remaining_display(self):
        seconds = self.time_remaining_seconds
        if seconds <= 0:
            return "Expired"
        hours = seconds // 3600
        minutes = (seconds % 3600) // 60
        if hours > 0:
            return f"{hours}h {minutes}m"
        return f"{minutes}m"


class Voucher(models.Model):

    class Status(models.TextChoices):
        ACTIVE = "active", "Active"
        USED = "used", "Used"
        EXPIRED = "expired", "Expired"
        REVOKED = "revoked", "Revoked"

    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    code = models.CharField(max_length=20, unique=True, db_index=True)
    package = models.ForeignKey(
        Package,
        on_delete=models.PROTECT,
        related_name="vouchers",
    )
    status = models.CharField(
        max_length=20,
        choices=Status.choices,
        default=Status.ACTIVE,
    )
    used_by = models.ForeignKey(
        "wifi_devices.Device",
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        related_name="redeemed_vouchers",
    )
    used_at = models.DateTimeField(null=True, blank=True)
    expires_at = models.DateTimeField(null=True, blank=True)
    created_by = models.CharField(max_length=100, default="admin")
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        app_label = "wifi_sessions"
        db_table = "vouchers"

    def __str__(self):
        return f"Voucher {self.code} ({self.status})"
