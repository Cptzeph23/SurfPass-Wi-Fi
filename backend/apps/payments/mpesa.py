"""
SurfPass WiFi - M-Pesa Daraja API Integration
Handles STK Push and payment verification
"""
import base64
import logging
import requests
from datetime import datetime
from django.conf import settings
from django.core.cache import cache

logger = logging.getLogger(__name__)

MPESA_SANDBOX_BASE = "https://sandbox.safaricom.co.ke"
MPESA_PRODUCTION_BASE = "https://api.safaricom.co.ke"


class MpesaClient:
    """M-Pesa Daraja API client for STK Push payments."""

    def __init__(self):
        self.consumer_key = settings.MPESA_CONSUMER_KEY
        self.consumer_secret = settings.MPESA_CONSUMER_SECRET
        self.shortcode = settings.MPESA_SHORTCODE
        self.passkey = settings.MPESA_PASSKEY
        self.callback_url = settings.MPESA_CALLBACK_URL
        self.env = settings.MPESA_ENV
        self.base_url = (
            MPESA_PRODUCTION_BASE if self.env == "production" else MPESA_SANDBOX_BASE
        )

    def _get_access_token(self) -> str:
        """Get or refresh OAuth access token (cached for 55 minutes)."""
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
        data = response.json()
        token = data["access_token"]
        cache.set(cache_key, token, 3300)  # Cache for 55 minutes
        return token

    def _generate_password(self, timestamp: str) -> str:
        """Generate STK Push password."""
        raw = f"{self.shortcode}{self.passkey}{timestamp}"
        return base64.b64encode(raw.encode()).decode()

    def _get_timestamp(self) -> str:
        return datetime.now().strftime("%Y%m%d%H%M%S")

    def initiate_stk_push(
        self,
        phone_number: str,
        amount: int,
        account_reference: str,
        transaction_desc: str,
    ) -> dict:
        """
        Initiate M-Pesa STK Push to user's phone.

        Args:
            phone_number: Format 2547XXXXXXXX
            amount: Amount in KES (integer)
            account_reference: Payment reference (e.g., payment ID)
            transaction_desc: Description shown on M-Pesa prompt

        Returns:
            dict with CheckoutRequestID and MerchantRequestID on success
        """
        phone_number = self._normalize_phone(phone_number)
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
            "AccountReference": account_reference[:12],  # Max 12 chars
            "TransactionDesc": transaction_desc[:13],
        }

        logger.info("Initiating STK Push: phone=%s amount=%s", phone_number, amount)

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
            "STK Push initiated: CheckoutRequestID=%s",
            data.get("CheckoutRequestID"),
        )
        return {
            "checkout_request_id": data["CheckoutRequestID"],
            "merchant_request_id": data["MerchantRequestID"],
            "response_description": data.get("CustomerMessage", "Request submitted"),
        }

    def query_stk_status(self, checkout_request_id: str) -> dict:
        """Query the status of an STK Push transaction."""
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

    @staticmethod
    def _normalize_phone(phone: str) -> str:
        """Normalize phone to 2547XXXXXXXX format."""
        phone = phone.strip().replace(" ", "").replace("-", "").replace("+", "")
        if phone.startswith("0"):
            phone = "254" + phone[1:]
        elif phone.startswith("7") or phone.startswith("1"):
            phone = "254" + phone
        return phone

    @staticmethod
    def parse_callback(data: dict) -> dict:
        """
        Parse M-Pesa STK Push callback payload.

        Returns normalized dict with payment result.
        """
        body = data.get("Body", {})
        stk_callback = body.get("stkCallback", {})

        result_code = stk_callback.get("ResultCode")
        result_desc = stk_callback.get("ResultDesc", "")
        checkout_request_id = stk_callback.get("CheckoutRequestID")
        merchant_request_id = stk_callback.get("MerchantRequestID")

        receipt_number = None
        phone_number = None
        amount = None
        transaction_date = None

        if result_code == 0:
            items = stk_callback.get("CallbackMetadata", {}).get("Item", [])
            item_map = {item["Name"]: item.get("Value") for item in items}
            receipt_number = item_map.get("MpesaReceiptNumber")
            phone_number = str(item_map.get("PhoneNumber", ""))
            amount = item_map.get("Amount")
            transaction_date = item_map.get("TransactionDate")

        return {
            "result_code": result_code,
            "result_desc": result_desc,
            "checkout_request_id": checkout_request_id,
            "merchant_request_id": merchant_request_id,
            "receipt_number": receipt_number,
            "phone_number": phone_number,
            "amount": amount,
            "transaction_date": transaction_date,
            "success": result_code == 0,
        }


class MpesaError(Exception):
    pass


# Singleton instance
mpesa_client = MpesaClient()