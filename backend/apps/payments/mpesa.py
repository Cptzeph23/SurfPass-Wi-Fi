import base64
import logging
import requests
from datetime import datetime
from django.conf import settings
from django.core.cache import cache

logger = logging.getLogger(__name__)

SANDBOX_BASE = "https://sandbox.safaricom.co.ke"
PRODUCTION_BASE = "https://api.safaricom.co.ke"


class MpesaClient:
    """
    Safaricom Daraja API client.
    Handles STK Push initiation and callback parsing.
    """

    def __init__(self):
        self.consumer_key = settings.MPESA_CONSUMER_KEY
        self.consumer_secret = settings.MPESA_CONSUMER_SECRET
        self.shortcode = settings.MPESA_SHORTCODE
        self.passkey = settings.MPESA_PASSKEY
        self.callback_url = settings.MPESA_CALLBACK_URL
        self.env = settings.MPESA_ENV
        self.base_url = (
            PRODUCTION_BASE if self.env == "production" else SANDBOX_BASE
        )

    # ── Auth ──────────────────────────────────────────────────────────────────

    def _get_access_token(self):
        """Fetch OAuth token, cached for 55 minutes."""
        cache_key = "mpesa_access_token"
        token = cache.get(cache_key)
        if token:
            return token

        credentials = base64.b64encode(
            f"{self.consumer_key}:{self.consumer_secret}".encode()
        ).decode()

        response = requests.get(
            f"{self.base_url}/oauth/v1/generate?grant_type=client_credentials",
            headers={"Authorization": f"Basic {credentials}"},
            timeout=30,
        )
        response.raise_for_status()
        token = response.json()["access_token"]
        cache.set(cache_key, token, 3300)
        return token

    def _get_timestamp(self):
        return datetime.now().strftime("%Y%m%d%H%M%S")

    def _generate_password(self, timestamp):
        raw = f"{self.shortcode}{self.passkey}{timestamp}"
        return base64.b64encode(raw.encode()).decode()

    # ── STK Push ──────────────────────────────────────────────────────────────

    def initiate_stk_push(self, phone_number, amount, account_reference, transaction_desc):
        """
        Send STK Push prompt to user's phone.

        Args:
            phone_number:      Format 2547XXXXXXXX
            amount:            Integer KES amount
            account_reference: Max 12 characters
            transaction_desc:  Max 13 characters

        Returns:
            dict with checkout_request_id and merchant_request_id
        """
        phone_number = self.normalize_phone(phone_number)
        timestamp = self._get_timestamp()
        password = self._generate_password(timestamp)
        token = self._get_access_token()

        payload = {
            "BusinessShortCode": self.shortcode,
            "Password": password,
            "Timestamp": timestamp,
            "TransactionType": "CustomerPayBillOnline",
            "Amount": int(amount),
            "PartyA": phone_number,
            "PartyB": self.shortcode,
            "PhoneNumber": phone_number,
            "CallBackURL": self.callback_url,
            "AccountReference": account_reference[:12],
            "TransactionDesc": transaction_desc[:13],
        }

        logger.info(
            "Initiating STK Push: phone=%s amount=%s",
            phone_number, amount,
        )

        response = requests.post(
            f"{self.base_url}/mpesa/stkpush/v1/processrequest",
            json=payload,
            headers={
                "Authorization": f"Bearer {token}",
                "Content-Type": "application/json",
            },
            timeout=30,
        )
        response.raise_for_status()
        data = response.json()

        if data.get("ResponseCode") != "0":
            raise MpesaError(
                f"STK Push failed: {data.get('ResponseDescription', 'Unknown error')}"
            )

        logger.info(
            "STK Push sent: CheckoutRequestID=%s",
            data.get("CheckoutRequestID"),
        )

        return {
            "checkout_request_id": data["CheckoutRequestID"],
            "merchant_request_id": data["MerchantRequestID"],
            "message": data.get("CustomerMessage", "Request submitted"),
        }

    def query_stk_status(self, checkout_request_id):
        """Query the current status of an STK Push transaction."""
        timestamp = self._get_timestamp()
        password = self._generate_password(timestamp)
        token = self._get_access_token()

        payload = {
            "BusinessShortCode": self.shortcode,
            "Password": password,
            "Timestamp": timestamp,
            "CheckoutRequestID": checkout_request_id,
        }

        response = requests.post(
            f"{self.base_url}/mpesa/stkpushquery/v1/query",
            json=payload,
            headers={
                "Authorization": f"Bearer {token}",
                "Content-Type": "application/json",
            },
            timeout=30,
        )
        response.raise_for_status()
        return response.json()

    # ── Callback Parser ───────────────────────────────────────────────────────

    @staticmethod
    def parse_callback(data):
        """
        Parse raw Safaricom STK Push callback payload.
        Returns a normalised dict regardless of success or failure.
        """
        stk = data.get("Body", {}).get("stkCallback", {})

        result_code = stk.get("ResultCode")
        result_desc = stk.get("ResultDesc", "")
        checkout_request_id = stk.get("CheckoutRequestID")
        merchant_request_id = stk.get("MerchantRequestID")

        receipt_number = None
        phone_number = None
        amount = None
        transaction_date = None

        if result_code == 0:
            items = stk.get("CallbackMetadata", {}).get("Item", [])
            item_map = {i["Name"]: i.get("Value") for i in items}
            receipt_number = item_map.get("MpesaReceiptNumber")
            phone_number = str(item_map.get("PhoneNumber", ""))
            amount = item_map.get("Amount")
            transaction_date = item_map.get("TransactionDate")

        return {
            "success": result_code == 0,
            "result_code": result_code,
            "result_desc": result_desc,
            "checkout_request_id": checkout_request_id,
            "merchant_request_id": merchant_request_id,
            "receipt_number": receipt_number,
            "phone_number": phone_number,
            "amount": amount,
            "transaction_date": transaction_date,
        }

    # ── Helpers ───────────────────────────────────────────────────────────────

    @staticmethod
    def normalize_phone(phone):
        """
        Normalize any Kenyan phone format to 2547XXXXXXXX.
        Accepts: 0712345678, 712345678, +254712345678, 254712345678
        """
        phone = (
            phone.strip()
            .replace(" ", "")
            .replace("-", "")
            .replace("+", "")
        )
        if phone.startswith("0") and len(phone) == 10:
            phone = "254" + phone[1:]
        elif (phone.startswith("7") or phone.startswith("1")) and len(phone) == 9:
            phone = "254" + phone
        return phone

    @staticmethod
    def validate_phone(phone):
        """
        Return normalized phone if valid Kenyan mobile number, else None.
        Valid prefixes after 254: 7xx or 1xx
        """
        normalized = MpesaClient.normalize_phone(phone)
        if (
            len(normalized) == 12
            and normalized.startswith("254")
            and (normalized[3] in ("7", "1"))
        ):
            return normalized
        return None


class MpesaError(Exception):
    pass


mpesa_client = MpesaClient()
