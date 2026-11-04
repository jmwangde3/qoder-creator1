"""
Qoder Creator - Tempik API Client
Self-hosted temp mail service: https://github.com/hirotomasato/tempik

Endpoints:
  GET  /api/session               → { sessionId }
  POST /api/inboxes                → { address, created_at }
  GET  /api/inboxes/{addr}/messages → [ { subject, body, from_address, received_at } ]
"""

import asyncio
import json
import random
import re
import time
import urllib.error
import urllib.request
from typing import List, Optional, Dict, Any

from .config import TEMPIK_BASE
from .utils import write_log

# Browser-like User-Agent to bypass Cloudflare
_BROWSER_UA = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
    "AppleWebKit/537.36 (KHTML, like Gecko) "
    "Chrome/132.0.0.0 Safari/537.36"
)

# /config is not worth failing an account over, but one flaky TLS handshake
# used to poison the domain list for the whole run — retry a couple of times.
_FETCH_RETRIES = 3

# Any single request can hit a TLS handshake timeout on a flaky uplink; one
# lost handshake should not throw away an otherwise good account.
_HTTP_RETRIES = 3
_RETRY_BACKOFF = 1.5  # seconds, multiplied by the attempt number


class TempikClient:
    """Tempik disposable email API client."""

    def __init__(self, base_url: str = None):
        self.base_url = (base_url or TEMPIK_BASE).rstrip("/")
        self.session_id: Optional[str] = None
        self._email: Optional[str] = None
        self._domains: List[str] = []

    # ==================== TRANSPORT ====================
    def _request(
        self,
        path: str,
        method: str = "GET",
        body: Optional[Dict[str, Any]] = None,
        with_session: bool = True,
        retries: int = _HTTP_RETRIES,
    ) -> Any:
        """Call the tempik API, retrying transport-level failures.

        4xx answers are re-raised immediately (except 429): they mean the
        request itself is wrong, so retrying cannot change the outcome.
        """
        url = f"{self.base_url}{path}"
        data = None if body is None else json.dumps(body).encode()
        last: Optional[Exception] = None

        for attempt in range(1, retries + 1):
            req = urllib.request.Request(url, data=data, method=method)
            req.add_header("Accept", "application/json")
            req.add_header("User-Agent", _BROWSER_UA)
            req.add_header("Accept-Language", "en-US,en;q=0.9")
            if data is not None:
                req.add_header("Content-Type", "application/json")
            if with_session and self.session_id:
                req.add_header("x-session-id", self.session_id)

            try:
                with urllib.request.urlopen(req, timeout=20) as resp:
                    return json.loads(resp.read().decode())
            except urllib.error.HTTPError as e:
                if 400 <= e.code < 500 and e.code != 429:
                    raise
                last = e
            except Exception as e:
                last = e

            if attempt < retries:
                write_log(
                    f"Tempik {method} {path} attempt {attempt}/{retries} "
                    f"failed: {last}",
                    "WARNING",
                )
                time.sleep(_RETRY_BACKOFF * attempt)

        raise last  # type: ignore[misc]

    # ==================== CONFIG ====================
    def _fetch_domains(self) -> List[str]:
        """Fetch available domains from /api/config. Cached.

        Returns [] when the endpoint stays unreachable, so create_inbox() can
        omit `domain` and let the server pick its default. Falling back to a
        hardcoded domain was worse: the server rejects unknown domains with
        HTTP 400 and every signup fails.
        """
        if self._domains:
            return self._domains

        try:
            data = self._request(
                "/config",
                with_session=False,
                retries=_FETCH_RETRIES,
            )
            domains = data.get("mailDomains") or []
            if not domains and data.get("mailDomain"):
                domains = [data["mailDomain"]]
            if domains:
                self._domains = domains
                write_log(f"Tempik domains: {self._domains}", "INFO")
                return self._domains
            write_log("Tempik /config had no mailDomains", "WARNING")
        except Exception as e:
            write_log(
                f"Tempik domain fetch gave up ({e}); using server default domain",
                "WARNING",
            )
        return []

    # ==================== SESSION ====================
    def init_session(self) -> str:
        """Create a new session. Returns sessionId."""
        if self.session_id:
            return self.session_id

        data = self._request("/session", with_session=False)
        self.session_id = (
            data.get("sessionId") or data.get("id") or data.get("session_id")
        )
        write_log(f"Tempik session: {self.session_id[:8]}...", "INFO")
        return self.session_id

    # ==================== INBOX ====================
    def create_inbox(self, local_part: str = None, domain: str = None) -> str:
        """Create a new inbox. Returns email address.

        If domain is not specified, picks a random domain from available domains.
        """
        self.init_session()

        # Pick random domain if not specified
        if not domain:
            domains = self._fetch_domains()
            domain = random.choice(domains) if domains else None

        body_data: Dict[str, Any] = {}
        if domain:
            body_data["domain"] = domain
        if local_part:
            body_data["localPart"] = local_part

        data = self._request("/inboxes", method="POST", body=body_data)

        self._email = data.get("address")
        write_log(
            f"Tempik inbox: {self._email} (domain={domain or 'server-default'})",
            "INFO",
        )
        return self._email

    # ==================== MESSAGES ====================
    def get_messages(self, address: str = None) -> List[Dict[str, Any]]:
        """Get all messages for an inbox address."""
        addr = address or self._email
        if not addr:
            raise ValueError("No email address provided")

        data = self._request(f"/inboxes/{addr}/messages")
        return data if isinstance(data, list) else []

    async def wait_for_messages(
        self,
        address: str = None,
        max_wait: int = 150,
        interval: int = 5,
    ) -> List[Dict[str, Any]]:
        """Poll for messages until one arrives or timeout."""
        addr = address or self._email
        t0 = time.time()

        while time.time() - t0 < max_wait:
            try:
                messages = self.get_messages(addr)
                if messages and len(messages) > 0:
                    return messages
            except Exception as e:
                write_log(f"Tempik poll error: {e}", "WARNING")

            await asyncio.sleep(interval)

        return []

    # ==================== OTP ====================
    def extract_otp(self, messages: List[Dict[str, Any]]) -> Optional[str]:
        """Extract verification code from messages.

        Qoder sends HTML email with the code as a standalone 6-digit number
        after "Verify your email" text. We strip HTML tags first, then search
        for the code in the plain text.
        """
        for msg in messages:
            subject = msg.get("subject", "")
            body = msg.get("body", "") or msg.get("text", "") or ""

            # Strip HTML tags to get plain text
            plain = re.sub(r"<[^>]+>", " ", body)
            plain = re.sub(r"\s+", " ", plain).strip()
            content = f"{subject} {plain}"

            write_log(f"OTP plain text: {content[:300]}", "INFO")

            patterns = [
                # Qoder specific: "Verify your email" OR "start using Qoder"
                # followed by a standalone 6-digit code
                r"(?:verify\s+your\s+email|start\s+using\s+Qoder)[\s\S]*?(\d{6})",
                # Generic fallback: standalone 6-digit code
                r"\b(\d{6})\b",
                # Other formats
                r"verification\s*code\s*:?\s*(\d{4,8})",
                r"code\s*:?\s*(\d{4,8})",
                r"OTP\s*:?\s*(\d{4,8})",
                r"passcode\s*:?\s*(\d{4,8})",
            ]

            for pattern in patterns:
                match = re.search(pattern, content, re.IGNORECASE)
                if match:
                    code = match.group(1)
                    write_log(f"OTP extracted: {code}", "SUCCESS")
                    return code

        return None

    async def wait_for_otp(
        self,
        address: str = None,
        max_wait: int = 150,
        interval: int = 5,
    ) -> Optional[str]:
        """Wait for OTP email and extract the code."""
        messages = await self.wait_for_messages(address, max_wait, interval)
        if messages:
            return self.extract_otp(messages)
        return None

    @property
    def email(self) -> Optional[str]:
        return self._email