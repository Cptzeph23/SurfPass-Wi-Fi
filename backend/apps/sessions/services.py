"""
SurfPass WiFi - Session Service
Core business logic for access control
"""
import logging
from datetime import timedelta
from django.utils import timezone
from django.db import transaction
from apps.devices.models import Device
from apps.sessions.models import Session, Package
from apps.sessions.mikrotik import get_mikrotik_client, MikroTikError

logger = logging.getLogger(__name__)


class SessionService:
    """Manages WiFi session lifecycle: create, activate, expire."""

    @staticmethod
    def get_or_create_device(mac_address: str, ip_address: str = None) -> Device:
        """Get existing device or register new one."""
        mac = mac_address.upper().replace("-", ":").replace(".", ":")
        device, _ = Device.objects.get_or_create(
            mac_address=mac,
            defaults={"ip_address": ip_address},
        )
        # Update last seen and IP
        Device.objects.filter(pk=device.pk).update(
            last_seen=timezone.now(),
            ip_address=ip_address or device.ip_address,
        )
        return device

    @staticmethod
    def check_active_session(mac_address: str) -> Session | None:
        """Return active session for MAC, or None."""
        mac = mac_address.upper().replace("-", ":").replace(".", ":")
        return (
            Session.objects.filter(
                device__mac_address=mac,
                status=Session.Status.ACTIVE,
                expiry_time__gt=timezone.now(),
            )
            .select_related("device", "package")
            .first()
        )

    @classmethod
    @transaction.atomic
    def activate_session(cls, payment) -> Session:
        """
        Activate internet access after successful payment.
        Creates session, whitelists MAC, sets bandwidth limits.
        """
        device = payment.device
        package = payment.package
        now = timezone.now()
        expiry = now + timedelta(minutes=package.duration_minutes)

        # Deactivate any stale active sessions for this device
        Session.objects.filter(
            device=device, status=Session.Status.ACTIVE
        ).update(status=Session.Status.TERMINATED, terminated_by="new_session")

        # Create new session
        session = Session.objects.create(
            device=device,
            package=package,
            start_time=now,
            expiry_time=expiry,
            status=Session.Status.ACTIVE,
            ip_address=device.ip_address,
        )

        # Link payment to session
        payment.session = session
        payment.save(update_fields=["session"])

        # Update device stats
        Device.objects.filter(pk=device.pk).update(
            total_sessions=device.total_sessions + 1,
            total_spent=device.total_spent + payment.amount,
            phone_number=payment.phone_number,
        )

        # Grant access on router
        cls._grant_router_access(device.mac_address, session, package)

        logger.info(
            "Session activated: device=%s package=%s expires=%s",
            device.mac_address,
            package.name,
            expiry,
        )
        return session

    @staticmethod
    def _grant_router_access(mac_address: str, session: Session, package: Package):
        """Whitelist MAC on MikroTik router."""
        try:
            with get_mikrotik_client() as mt:
                mt.grant_access(
                    mac_address=mac_address,
                    session_id=str(session.id),
                    comment=f"{package.name} expires:{session.expiry_time.isoformat()}",
                )
                # Set bandwidth limits if configured
                if package.bandwidth_upload_kbps > 0 or package.bandwidth_download_kbps > 0:
                    mt.set_bandwidth_limit(
                        mac_address,
                        package.bandwidth_upload_kbps,
                        package.bandwidth_download_kbps,
                    )
        except MikroTikError as e:
            logger.error("Router grant failed for %s: %s", mac_address, e)
            # Don't fail session creation - log for manual resolution
            # In production, implement retry queue via Celery

    @staticmethod
    def _revoke_router_access(mac_address: str):
        """Remove MAC from MikroTik whitelist."""
        try:
            with get_mikrotik_client() as mt:
                mt.revoke_access(mac_address)
                mt.remove_bandwidth_limit(mac_address)
        except MikroTikError as e:
            logger.error("Router revoke failed for %s: %s", mac_address, e)

    @classmethod
    @transaction.atomic
    def terminate_session(cls, session: Session, reason: str = "admin") -> bool:
        """Terminate session and revoke access."""
        if session.status != Session.Status.ACTIVE:
            return False

        session.status = Session.Status.TERMINATED
        session.terminated_by = reason
        session.save(update_fields=["status", "terminated_by", "updated_at"])

        cls._revoke_router_access(session.device.mac_address)
        logger.info("Session terminated: %s reason=%s", session.id, reason)
        return True

    @classmethod
    def expire_stale_sessions(cls) -> int:
        """
        Find and expire all sessions past their expiry time.
        Called by Celery beat every 60 seconds.
        """
        expired = Session.objects.filter(
            status=Session.Status.ACTIVE,
            expiry_time__lte=timezone.now(),
        ).select_related("device")

        count = 0
        for session in expired:
            session.status = Session.Status.EXPIRED
            session.terminated_by = "system_timeout"
            session.save(update_fields=["status", "terminated_by", "updated_at"])
            cls._revoke_router_access(session.device.mac_address)
            count += 1

        if count:
            logger.info("Expired %d sessions", count)
        return count

    @staticmethod
    def redeem_voucher(voucher_code: str, mac_address: str) -> Session:
        """Redeem a voucher code and activate session."""
        from apps.sessions.models import Voucher
        from django.utils import timezone

        try:
            voucher = Voucher.objects.select_related("package").get(
                code=voucher_code.upper().strip(),
                status=Voucher.Status.ACTIVE,
            )
        except Voucher.DoesNotExist:
            raise ValueError("Invalid or already used voucher code.")

        if voucher.expires_at and voucher.expires_at < timezone.now():
            voucher.status = Voucher.Status.EXPIRED
            voucher.save()
            raise ValueError("This voucher has expired.")

        device = SessionService.get_or_create_device(mac_address)

        # Deactivate existing session
        Session.objects.filter(
            device=device, status=Session.Status.ACTIVE
        ).update(status=Session.Status.TERMINATED, terminated_by="voucher_redeem")

        now = timezone.now()
        session = Session.objects.create(
            device=device,
            package=voucher.package,
            start_time=now,
            expiry_time=now + timedelta(minutes=voucher.package.duration_minutes),
            status=Session.Status.ACTIVE,
        )

        voucher.status = Voucher.Status.USED
        voucher.used_by = device
        voucher.used_at = now
        voucher.save()

        SessionService._grant_router_access(device.mac_address, session, voucher.package)
        return session
