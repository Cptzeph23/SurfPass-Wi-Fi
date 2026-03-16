import socket
import hashlib
import logging
import binascii
from django.conf import settings

logger = logging.getLogger(__name__)


class MikroTikAPI:
    """
    MikroTik RouterOS API client.
    Manages hotspot MAC whitelist for internet access control.
    """

    def __init__(self):
        self.host = settings.MIKROTIK_HOST
        self.port = settings.MIKROTIK_PORT
        self.username = settings.MIKROTIK_USER
        self.password = settings.MIKROTIK_PASSWORD
        self._socket = None

    # ── Connection ────────────────────────────────────────────────────────────

    def connect(self):
        try:
            self._socket = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
            self._socket.settimeout(10)
            self._socket.connect((self.host, self.port))
            self._login()
            logger.info("Connected to MikroTik %s:%s", self.host, self.port)
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

    # ── Protocol ──────────────────────────────────────────────────────────────

    def _login(self):
        response = self._talk(["/login"])
        challenge = None
        for item in response:
            if "=ret=" in item:
                challenge = item.split("=ret=")[1]

        if challenge:
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
            self._talk([
                "/login",
                f"=name={self.username}",
                f"=password={self.password}",
            ])

    def _write_length(self, length):
        if length < 0x80:
            self._socket.send(bytes([length]))
        elif length < 0x4000:
            length |= 0x8000
            self._socket.send(bytes([length >> 8, length & 0xFF]))
        else:
            length |= 0xC00000
            self._socket.send(bytes([
                (length >> 16) & 0xFF,
                (length >> 8) & 0xFF,
                length & 0xFF,
            ]))

    def _read_length(self):
        b = ord(self._socket.recv(1))
        if b < 0x80:
            return b
        elif b < 0xC0:
            b2 = ord(self._socket.recv(1))
            return ((b & ~0x80) << 8) | b2
        else:
            b2 = ord(self._socket.recv(1))
            b3 = ord(self._socket.recv(1))
            return ((b & ~0xC0) << 16) | (b2 << 8) | b3

    def _read_word(self):
        length = self._read_length()
        if length == 0:
            return ""
        return self._socket.recv(length).decode("utf-8")

    def _read_sentence(self):
        words = []
        while True:
            word = self._read_word()
            if not word:
                break
            words.append(word)
        return words

    def _write_sentence(self, sentence):
        for word in sentence:
            encoded = word.encode("utf-8")
            self._write_length(len(encoded))
            self._socket.send(encoded)
        self._write_length(0)

    def _talk(self, sentence):
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

    # ── Hotspot Control ───────────────────────────────────────────────────────

    def grant_access(self, mac_address, session_id, comment=""):
        """Add MAC to hotspot whitelist — grants internet access."""
        try:
            mac = mac_address.upper()
            self._talk([
                "/ip/hotspot/user/add",
                f"=mac-address={mac}",
                f"=name={mac}",
                f"=profile=surfpass-users",
                f"=comment=session:{session_id} {comment}",
            ])
            logger.info("Granted access: %s", mac)
            return True
        except Exception as e:
            logger.error("Grant access failed for %s: %s", mac_address, e)
            return False

    def revoke_access(self, mac_address):
        """Remove MAC from hotspot whitelist — revokes internet access."""
        try:
            mac = mac_address.upper()
            response = self._talk([
                "/ip/hotspot/user/print",
                f"?mac-address={mac}",
                "=.proplist=.id",
            ])
            user_ids = [
                w.split("=.id=")[1] for w in response if "=.id=" in w
            ]
            for uid in user_ids:
                self._talk(["/ip/hotspot/user/remove", f"=.id={uid}"])

            self._disconnect_active_session(mac)
            logger.info("Revoked access: %s", mac)
            return True
        except Exception as e:
            logger.error("Revoke access failed for %s: %s", mac_address, e)
            return False

    def _disconnect_active_session(self, mac_address):
        """Force disconnect any live hotspot session for this MAC."""
        try:
            response = self._talk([
                "/ip/hotspot/active/print",
                f"?mac-address={mac_address}",
                "=.proplist=.id",
            ])
            session_ids = [
                w.split("=.id=")[1] for w in response if "=.id=" in w
            ]
            for sid in session_ids:
                self._talk(["/ip/hotspot/active/remove", f"=.id={sid}"])
        except Exception as e:
            logger.warning(
                "Could not disconnect active session for %s: %s",
                mac_address, e,
            )

    def get_active_sessions(self):
        """Return list of all currently active hotspot sessions."""
        try:
            response = self._talk([
                "/ip/hotspot/active/print",
                "=.proplist=mac-address,address,uptime,bytes-in,bytes-out",
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

    def set_bandwidth_limit(self, mac_address, upload_kbps, download_kbps):
        """Create a Simple Queue to throttle this device."""
        try:
            if upload_kbps > 0 or download_kbps > 0:
                mac = mac_address.upper()
                self._talk([
                    "/queue/simple/add",
                    f"=name=surfpass-{mac}",
                    f"=target={mac}",
                    f"=max-limit={upload_kbps}k/{download_kbps}k",
                    "=comment=SurfPass bandwidth limit",
                ])
            return True
        except Exception as e:
            logger.error("Set bandwidth limit failed: %s", e)
            return False

    def remove_bandwidth_limit(self, mac_address):
        """Remove Simple Queue for this device."""
        try:
            mac = mac_address.upper()
            response = self._talk([
                "/queue/simple/print",
                f"?name=surfpass-{mac}",
                "=.proplist=.id",
            ])
            queue_ids = [
                w.split("=.id=")[1] for w in response if "=.id=" in w
            ]
            for qid in queue_ids:
                self._talk(["/queue/simple/remove", f"=.id={qid}"])
            return True
        except Exception as e:
            logger.error("Remove bandwidth limit failed: %s", e)
            return False


class MikroTikError(Exception):
    pass


def get_mikrotik_client():
    return MikroTikAPI()
