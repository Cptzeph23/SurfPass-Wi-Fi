import logging
from datetime import timedelta
from django.utils import timezone
from django.db import transaction
from apps.devices.models import Device
from apps.sessions.models import Session, Package, Voucher

logger = logging.getLogger(__name__)


class SessionService:
    """
    Core access control logic.
    Coordinates between Device, Session, Payment and the MikroTik router.
    """

    # ── Device ────────────────────────────────────────────────────────────────

    @staticmethod
    def get_or_create_device(mac_address, ip_address=None):
        """Register new device or update existing one's last_seen."""
        mac = (
            mac_address.upper()
            .replace("-", ":")
            .replace(".", ":")
        )
        device, _ = Device.objects.get_or_create(
            mac_address=mac,
            defaults={"ip_address": ip_address},
        )
        Device.objects.filter(pk=device.pk).update(
            last_seen=timezone.now(),
            ip_address=ip_address or device.ip_address,
        )
        device.refresh_from_db()
        return device

    # ── Session Checking ──────────────────────────────────────────────────────

    @staticmethod
    def check_active_session(mac_address):
        """
        Return the active Session for this MAC, or None.
        Used by the portal on every page load.
        """
        mac = (
            mac_address.upper()
            .replace("-", ":")
            .replace(".", ":")
        )
        return (
            Session.objects.filter(
                device__mac_address=mac,
                status=Session.Status.ACTIVE,
                expiry_time__gt=timezone.now(),
            )
            .select_related("device", "package")
            .first()
        )

    # ── Session Activation ────────────────────────────────────────────────────

    @classmethod
    @transaction.atomic
    def activate_session(cls, payment):
        """
        Called after a successful payment.
        1. Terminates any existing active session for the device.
        2. Creates a new active Session.
        3. Links the payment to the session.
        4. Updates device stats.
        5. Grants access on the MikroTik router.
        """
        device = payment.device
        package = payment.package
        now = timezone.now()
        expiry = now + timedelta(minutes=package.duration_minutes)

        # Deactivate any existing sessions for this device
        Session.objects.filter(
            device=device,
            status=Session.Status.ACTIVE,
        ).update(
            status=Session.Status.TERMINATED,
            terminated_by="new_session",
        )

        # Create the new session
        session = Session.objects.create(
            device=device,
            package=package,
            start_time=now,
            expiry_time=expiry,
            status=Session.Status.ACTIVE,
            ip_address=device.ip_address,
        )

        # Link payment → session
        payment.session = session
        payment.save(update_fields=["session"])

        # Update device totals
        Device.objects.filter(pk=device.pk).update(
            total_sessions=device.total_sessions + 1,
            total_spent=device.total_spent + payment.amount,
            phone_number=payment.phone_number,
        )

        # Grant router access
        cls._grant_router_access(device.mac_address, session, package)

        logger.info(
            "Session activated: mac=%s package=%s expires=%s",
            device.mac_address,
            package.name,
            expiry,
        )
        return session

    # ── Session Termination ───────────────────────────────────────────────────

    @classmethod
    @transaction.atomic
    def terminate_session(cls, session, reason="admin"):
        """
        Manually terminate a session and revoke router access.
        Used by admin dashboard and the expiry task.
        """
        if session.status != Session.Status.ACTIVE:
            return False

        Session.objects.filter(pk=session.pk).update(
            status=Session.Status.TERMINATED,
            terminated_by=reason,
        )

        cls._revoke_router_access(session.device.mac_address)

        logger.info(
            "Session terminated: id=%s reason=%s mac=%s",
            session.id,
            reason,
            session.device.mac_address,
        )
        return True

    # ── Session Expiry ────────────────────────────────────────────────────────

    @classmethod
    def expire_stale_sessions(cls):
        """
        Find all sessions past expiry_time and expire them.
        Called by Celery beat every 60 seconds.
        Returns the count of sessions expired.
        """
        expired_sessions = Session.objects.filter(
            status=Session.Status.ACTIVE,
            expiry_time__lte=timezone.now(),
        ).select_related("device")

        count = 0
        for session in expired_sessions:
            Session.objects.filter(pk=session.pk).update(
                status=Session.Status.EXPIRED,
                terminated_by="system_timeout",
            )
            cls._revoke_router_access(session.device.mac_address)
            count += 1

        if count:
            logger.info("Auto-expired %d session(s)", count)

        return count

    # ── Voucher Redemption ────────────────────────────────────────────────────

    @classmethod
    @transaction.atomic
    def redeem_voucher(cls, code, mac_address, ip_address=None):
        """
        Redeem a voucher code and activate internet access.
        Raises ValueError with a user-friendly message on failure.
        """
        try:
            voucher = Voucher.objects.select_related("package").get(
                code=code.upper().strip(),
                status=Voucher.Status.ACTIVE,
            )
        except Voucher.DoesNotExist:
            raise ValueError("Invalid or already used voucher code.")

        if voucher.expires_at and voucher.expires_at < timezone.now():
            Voucher.objects.filter(pk=voucher.pk).update(
                status=Voucher.Status.EXPIRED
            )
            raise ValueError("This voucher code has expired.")

        device = cls.get_or_create_device(mac_address, ip_address)

        # Deactivate existing sessions
        Session.objects.filter(
            device=device,
            status=Session.Status.ACTIVE,
        ).update(
            status=Session.Status.TERMINATED,
            terminated_by="voucher_redeem",
        )

        now = timezone.now()
        session = Session.objects.create(
            device=device,
            package=voucher.package,
            start_time=now,
            expiry_time=now + timedelta(minutes=voucher.package.duration_minutes),
            status=Session.Status.ACTIVE,
            ip_address=ip_address,
        )

        Voucher.objects.filter(pk=voucher.pk).update(
            status=Voucher.Status.USED,
            used_by=device,
            used_at=now,
        )

        Device.objects.filter(pk=device.pk).update(
            total_sessions=device.total_sessions + 1,
        )

        cls._grant_router_access(device.mac_address, session, voucher.package)

        logger.info(
            "Voucher redeemed: code=%s mac=%s package=%s",
            code,
            mac_address,
            voucher.package.name,
        )
        return session

    # ── Router Helpers ────────────────────────────────────────────────────────

    @staticmethod
    def _grant_router_access(mac_address, session, package):
        """
        Whitelist MAC on MikroTik.
        Failures are logged but do NOT roll back the session —
        operator can manually grant access if router is unreachable.
        """
        try:
            from apps.sessions.mikrotik import get_mikrotik_client
            with get_mikrotik_client() as mt:
                mt.grant_access(
                    mac_address=mac_address,
                    session_id=str(session.id),
                    comment=(
                        f"{package.name} "
                        f"expires:{session.expiry_time.strftime('%Y-%m-%d %H:%M')}"
                    ),
                )
                if package.bandwidth_upload_kbps > 0 or package.bandwidth_download_kbps > 0:
                    mt.set_bandwidth_limit(
                        mac_address,
                        package.bandwidth_upload_kbps,
                        package.bandwidth_download_kbps,
                    )
        except Exception as e:
            logger.error(
                "Router grant failed for %s — session still created: %s",
                mac_address, e,
            )

    @staticmethod
    def _revoke_router_access(mac_address):
        """
        Remove MAC from MikroTik whitelist.
        Failures are logged but do not affect DB state.
        """
        try:
            from apps.sessions.mikrotik import get_mikrotik_client
            with get_mikrotik_client() as mt:
                mt.revoke_access(mac_address)
                mt.remove_bandwidth_limit(mac_address)
        except Exception as e:
            logger.error(
                "Router revoke failed for %s: %s",
                mac_address, e,
            )
