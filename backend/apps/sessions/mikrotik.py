"""
SurfPass WiFi - MikroTik RouterOS API Client
Controls hotspot access via MAC address whitelisting
"""
import socket
import hashlib
import logging
import binascii
from typing import Optional
from django.conf import settings

logger = logging.getLogger(__name__)


class MikroTikSentence:
    """RouterOS API protocol sentence."""

    def __init__(self, command: str):
        self.words = [command]

    def add_attribute(self, key: str, value: str):
        self.words.append(f"={key}={value}")
        return self

    def add_query(self, key: str, value: str = None):
        if value is not None:
            self.words.append(f"?{key}={value}")
        else:
            self.words.append(f"?{key}")
        return self


class MikroTikAPI:
    """
    MikroTik RouterOS API client.
    Manages hotspot user whitelist for internet access control.
    """

    def __init__(
        self,
        host: str = None,
        port: int = None,
        username: str = None,
        password: str = None,
    ):
        self.host = host or settings.MIKROTIK_HOST
        self.port = port or settings.MIKROTIK_PORT
        self.username = username or settings.MIKROTIK_USER
        self.password = password or settings.MIKROTIK_PASSWORD
        self._socket: Optional[socket.socket] = None

    def connect(self):
        """Establish connection to RouterOS API."""
        try:
            self._socket = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
            self._socket.settimeout(10)
            self._socket.connect((self.host, self.port))
            self._login()
            logger.debug("Connected to MikroTik %s:%s", self.host, self.port)
        except Exception as e:
            logger.error("MikroTik connection failed: %s", e)
            raise MikroTikError(f"Connection failed: {e}") from e

    def disconnect(self):
        if self._socket:
            try:
                self._socket.close()
            except Exception:
                pass
            self._socket = None

    def __enter__(self):
        self.connect()
        return self

    def __exit__(self, *args):
        self.disconnect()

    def _login(self):
        """Authenticate with RouterOS API using MD5 challenge."""
        response = self._talk(["/login"])
        challenge = None
        for item in response:
            if "=ret=" in item:
                challenge = item.split("=ret=")[1]

        if challenge:
            # MD5 challenge-response
            md5 = hashlib.md5()
            md5.update(b"\x00")
            md5.update(self.password.encode("utf-8"))
            md5.update(binascii.unhexlify(challenge))
            self._talk([
                "/login",
                f"=name={self.username}",
                f"=response=00{md5.hexdigest()}",
            ])
        else:
            # Plain text login (API v2+)
            self._talk([
                "/login",
                f"=name={self.username}",
                f"=password={self.password}",
            ])

    def _write_sentence(self, sentence: list):
        for word in sentence:
            encoded = word.encode("utf-8")
            self._write_length(len(encoded))
            self._socket.send(encoded)
        self._write_length(0)  # End of sentence

    def _write_length(self, length: int):
        if length < 0x80:
            self._socket.send(bytes([length]))
        elif length < 0x4000:
            length |= 0x8000
            self._socket.send(bytes([length >> 8, length & 0xFF]))
        elif length < 0x200000:
            length |= 0xC00000
            self._socket.send(bytes([
                (length >> 16) & 0xFF,
                (length >> 8) & 0xFF,
                length & 0xFF,
            ]))
        else:
            self._socket.send(bytes([
                0xF0,
                (length >> 24) & 0xFF,
                (length >> 16) & 0xFF,
                (length >> 8) & 0xFF,
                length & 0xFF,
            ]))

    def _read_length(self) -> int:
        b = ord(self._socket.recv(1))
        if b < 0x80:
            return b
        elif b < 0xC0:
            b2 = ord(self._socket.recv(1))
            return ((b & ~0x80) << 8) | b2
        elif b < 0xE0:
            b2 = ord(self._socket.recv(1))
            b3 = ord(self._socket.recv(1))
            return ((b & ~0xC0) << 16) | (b2 << 8) | b3
        elif b < 0xF0:
            b2 = ord(self._socket.recv(1))
            b3 = ord(self._socket.recv(1))
            b4 = ord(self._socket.recv(1))
            return ((b & ~0xE0) << 24) | (b2 << 16) | (b3 << 8) | b4
        else:
            b2, b3, b4, b5 = [ord(self._socket.recv(1)) for _ in range(4)]
            return (b2 << 24) | (b3 << 16) | (b4 << 8) | b5

    def _read_word(self) -> str:
        length = self._read_length()
        if length == 0:
            return ""
        return self._socket.recv(length).decode("utf-8")

    def _read_sentence(self) -> list:
        words = []
        while True:
            word = self._read_word()
            if not word:
                break
            words.append(word)
        return words

    def _talk(self, sentence: list) -> list:
        self._write_sentence(sentence)
        response = []
        while True:
            s = self._read_sentence()
            if not s:
                continue
            response.extend(s)
            if "!done" in s or "!trap" in s:
                break
        return response

    # ─── Hotspot Access Control ─────────────────────────────────────────────

    def grant_access(self, mac_address: str, session_id: str, comment: str = "") -> bool:
        """Add MAC address to hotspot whitelist (grant internet access)."""
        try:
            mac = mac_address.upper()
            self._talk([
                "/ip/hotspot/user/add",
                f"=mac-address={mac}",
                f"=name={mac}",
                f"=comment=session:{session_id} {comment}",
                "=profile=surfpass-users",
            ])
            logger.info("Granted access for MAC: %s", mac)
            return True
        except Exception as e:
            logger.error("Failed to grant access for %s: %s", mac_address, e)
            return False

    def revoke_access(self, mac_address: str) -> bool:
        """Remove MAC address from hotspot whitelist (revoke internet access)."""
        try:
            mac = mac_address.upper()
            # Find the user entry
            response = self._talk([
                "/ip/hotspot/user/print",
                f"?mac-address={mac}",
                "=.proplist=.id",
            ])
            user_ids = [w.split("=.id=")[1] for w in response if "=.id=" in w]

            for uid in user_ids:
                self._talk(["/ip/hotspot/user/remove", f"=.id={uid}"])

            # Also disconnect active session
            self._disconnect_active_session(mac)

            logger.info("Revoked access for MAC: %s", mac)
            return True
        except Exception as e:
            logger.error("Failed to revoke access for %s: %s", mac_address, e)
            return False

    def _disconnect_active_session(self, mac_address: str):
        """Force disconnect active hotspot session."""
        try:
            response = self._talk([
                "/ip/hotspot/active/print",
                f"?mac-address={mac_address}",
                "=.proplist=.id",
            ])
            session_ids = [w.split("=.id=")[1] for w in response if "=.id=" in w]
            for sid in session_ids:
                self._talk(["/ip/hotspot/active/remove", f"=.id={sid}"])
        except Exception as e:
            logger.warning("Could not disconnect active session for %s: %s", mac_address, e)

    def get_active_sessions(self) -> list:
        """Get all currently active hotspot sessions."""
        try:
            response = self._talk([
                "/ip/hotspot/active/print",
                "=.proplist=mac-address,address,user,uptime,bytes-in,bytes-out",
            ])
            sessions = []
            current = {}
            for word in response:
                if word == "!re":
                    if current:
                        sessions.append(current)
                    current = {}
                elif word.startswith("=") and "=" in word[1:]:
                    key, _, val = word[1:].partition("=")
                    current[key] = val
            if current:
                sessions.append(current)
            return sessions
        except Exception as e:
            logger.error("Failed to get active sessions: %s", e)
            return []

    def set_bandwidth_limit(
        self, mac_address: str, upload_kbps: int, download_kbps: int
    ) -> bool:
        """Set bandwidth limits for a device (requires Queue Tree or Simple Queue)."""
        try:
            mac = mac_address.upper()
            if upload_kbps > 0 and download_kbps > 0:
                self._talk([
                    "/queue/simple/add",
                    f"=name=surfpass-{mac}",
                    f"=target={mac}",
                    f"=max-limit={upload_kbps}k/{download_kbps}k",
                    f"=comment=SurfPass rate limit",
                ])
            return True
        except Exception as e:
            logger.error("Failed to set bandwidth limit: %s", e)
            return False

    def remove_bandwidth_limit(self, mac_address: str) -> bool:
        """Remove bandwidth limit for a device."""
        try:
            mac = mac_address.upper()
            response = self._talk([
                "/queue/simple/print",
                f"?name=surfpass-{mac}",
                "=.proplist=.id",
            ])
            queue_ids = [w.split("=.id=")[1] for w in response if "=.id=" in w]
            for qid in queue_ids:
                self._talk(["/queue/simple/remove", f"=.id={qid}"])
            return True
        except Exception as e:
            logger.error("Failed to remove bandwidth limit: %s", e)
            return False


class MikroTikError(Exception):
    pass


def get_mikrotik_client() -> MikroTikAPI:
    """Factory function to get a configured MikroTik client."""
    return MikroTikAPI()