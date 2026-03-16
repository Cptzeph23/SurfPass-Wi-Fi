import uuid
from django.db import models
from django.utils import timezone


class Device(models.Model):
    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    mac_address = models.CharField(max_length=17, unique=True, db_index=True)
    phone_number = models.CharField(max_length=15, blank=True, null=True)
    hostname = models.CharField(max_length=255, blank=True, null=True)
    ip_address = models.GenericIPAddressField(blank=True, null=True)
    first_seen = models.DateTimeField(default=timezone.now)
    last_seen = models.DateTimeField(default=timezone.now)
    is_blocked = models.BooleanField(default=False)
    block_reason = models.CharField(max_length=255, blank=True, null=True)
    total_sessions = models.PositiveIntegerField(default=0)
    total_spent = models.DecimalField(max_digits=10, decimal_places=2, default=0)

    class Meta:
        app_label = "wifi_devices"
        db_table = "devices"
        ordering = ["-last_seen"]

    def __str__(self):
        return f"{self.mac_address} ({self.phone_number or 'unknown'})"

    def save(self, *args, **kwargs):
        self.mac_address = (
            self.mac_address.upper()
            .replace("-", ":")
            .replace(".", ":")
        )
        super().save(*args, **kwargs)
