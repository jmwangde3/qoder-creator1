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

# When every read in a wait window failed on the network, the inbox is not
# empty — it is unreadable. Reporting "no mail" in that state makes the caller
# re-send the code, which invalidates one that may already have been delivered.
# Give an unreachable inbox one extra window before saying anything.
_UNREADABLE_GRACE = 60

# Our clock and Cloudflare's are both NTP-synced but not identical; ignore a
# few seconds of skew when comparing a local send time to a server receipt
# time, so a fresh mail can never be filtered out by a fast local clock.
_CLOCK_SKEW = 5

# Text that introduces a verification code. Qoder's mail is localised, so the
# English-only phrases the original regex looked for never show up — match the
# Chinese wording too.
_CODE_PREFIX = (
    r"(?:验证码|校验码|验证代码|动态码|动态密码|"
    r"verification\s*code|verify\s*code|security\s*code|login\s*code|"
    r"one[-\s]?time\s*(?:code|password)|passcode|otp)"
)

# A mail only counts as an OTP candidate when the sender or the text says so.
# Anything else must not donate a random 6-digit number as the code.
_OTP_SENDER_HINTS = ("qoder.com",)
_OTP_TEXT_HINTS = (
    "qoder", "验证码", "verification code", "verify your email",
    "otp", "passcode", "one-time",
)


# ==================== HELPERS ====================
def utc_stamp(offset: float = 0.0) -> str:
    """Timestamp in the format tempik stores in messages.received_at."""
    return time.strftime("%Y-%m-%d %H:%M:%S", time.gmtime(time.time() + offset))


def sent_at_stamp() -> str:
    """Stamp to pass as `since` when a code request is about to be triggered.

    Skew-tolerant: a mail that left the server a moment before our own clock
    reported the send must still count as fresh.
    """
    return utc_stamp(-_CLOCK_SKEW)


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

    @staticmethod
    def _received_at(msg: Dict[str, Any]) -> str:
        return str(msg.get("received_at") or "").strip()

    @classmethod
    def newer_than(cls, messages: List[Dict[str, Any]], since: Optional[str]) -> List[Dict[str, Any]]:
        """Keep only mails received at or after `since`.

        Every request for a verification code invalidates the previous one, so
        a mail that predates the latest request carries a dead code. Submitting
        it produces "Verification code is invalid" and burns the whole round —
        observed on a real run where the first code was read after the resend.

        Mails with no usable timestamp are kept: they cannot be proven stale,
        and dropping them would break a backend that omits the field.
        """
        if not since:
            return list(messages)

        fresh = []
        for msg in messages:
            ts = cls._received_at(msg)
            if not ts or ts >= since:
                fresh.append(msg)
        return fresh

    async def wait_for_messages(
        self,
        address: str = None,
        max_wait: int = 150,
        interval: int = 5,
        since: Optional[str] = None,
    ) -> List[Dict[str, Any]]:
        """Poll until a mail newer than `since` arrives, or the window expires.

        Returns the fresh mails, newest first. An empty list means "the inbox
        really is empty", not "the network failed" — a window in which every
        read failed is extended once, because the caller reacts to "no mail" by
        triggering a resend that invalidates a code which may already be here.
        """
        addr = address or self._email
        deadline = time.time() + max_wait
        ok_reads = 0
        extended = False
        ignored = 0

        while True:
            if time.time() >= deadline:
                if ok_reads == 0 and not extended:
                    extended = True
                    deadline = time.time() + min(max_wait, _UNREADABLE_GRACE)
                    write_log(
                        "Tempik inbox never became readable — extending the "
                        f"wait by {min(max_wait, _UNREADABLE_GRACE)}s instead of "
                        "reporting an empty inbox",
                        "WARNING",
                    )
                    continue
                break

            try:
                messages = self.get_messages(addr)
                ok_reads += 1
                fresh = self.newer_than(messages, since)
                ignored = len(messages) - len(fresh)
                if fresh:
                    return sorted(
                        fresh, key=self._received_at, reverse=True
                    )
            except Exception as e:
                write_log(f"Tempik poll error: {e}", "WARNING")

            await asyncio.sleep(interval)

        if ignored:
            write_log(
                f"{ignored} mail(s) in {addr} were older than since={since} and "
                "ignored — correct after a resend, but persistent occurrences "
                "point at a local clock that is ahead of the server",
                "WARNING",
            )
        return []

    # ==================== OTP ====================
    @staticmethod
    def _plain(msg: Dict[str, Any]) -> str:
        """Subject + body with tags and runs of whitespace collapsed."""
        body = msg.get("body", "") or msg.get("text", "") or ""
        subject = msg.get("subject", "") or ""
        plain = re.sub(r"<[^>]+>", " ", body)
        plain = re.sub(r"\s+", " ", plain).strip()
        return f"{subject} {plain}".strip()

    @staticmethod
    def _looks_like_otp(msg: Dict[str, Any], text: str) -> bool:
        sender = (msg.get("from_address") or "").lower()
        if any(h in sender for h in _OTP_SENDER_HINTS):
            return True
        low = text.lower()
        return any(h in low for h in _OTP_TEXT_HINTS)

    @staticmethod
    def _code_after_keyword(text: str) -> Optional[str]:
        """Dig out a code that directly follows a verification-code phrase.

        Bounded to 60 non-digit characters so the match cannot jump across
        an unrelated number further down the mail.
        """
        match = re.search(_CODE_PREFIX + r"[^0-9]{0,60}(\d{4,8})", text, re.IGNORECASE)
        return match.group(1) if match else None

    def extract_otp(self, messages: List[Dict[str, Any]]) -> Optional[str]:
        """Extract the verification code, preferring mails that look like OTPs.

        The old version returned the first bare 6-digit number it could find in
        any message, so an invoice or a tracking number arriving before the
        real mail could win. Tiers, best first:

          1. code right after a verification-code phrase, in an OTP-looking mail
          2. the only bare 6-digit number in an OTP-looking mail
          3. code after such a phrase in any other mail

        Returns None instead of guessing when the evidence is ambiguous. There
        is deliberately no "just take the only number in the inbox" fallback:
        a lone marketing mail would then donate its order number as the code.
        """
        if not messages:
            return None

        texts = [(m, self._plain(m)) for m in messages]
        priority = [(m, t) for m, t in texts if self._looks_like_otp(m, t)]
        rest = [(m, t) for m, t in texts if not self._looks_like_otp(m, t)]

        for _, text in priority:
            code = self._code_after_keyword(text)
            if code:
                write_log(f"OTP (keyword): {code}", "SUCCESS")
                return code

        for _, text in priority:
            found = set(re.findall(r"\b(\d{6})\b", text))
            if len(found) == 1:
                code = found.pop()
                write_log(f"OTP (single number): {code}", "SUCCESS")
                return code

        for _, text in rest:
            code = self._code_after_keyword(text)
            if code:
                write_log(f"OTP (fallback, keyword): {code}", "SUCCESS")
                return code

        write_log("OTP not found — evidence ambiguous, not guessing", "WARNING")
        return None

    async def wait_for_otp(
        self,
        address: str = None,
        max_wait: int = 150,
        interval: int = 5,
        since: Optional[str] = None,
    ) -> Optional[str]:
        """Wait for OTP email and extract the code."""
        messages = await self.wait_for_messages(address, max_wait, interval, since)
        if messages:
            return self.extract_otp(messages)
        return None

    @property
    def email(self) -> Optional[str]:
        return self._email