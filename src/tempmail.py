"""
Qoder Creator - Tempik API Client
Self-hosted temp mail service: https://github.com/hirotomasato/tempik

Endpoints:
  GET  /api/session               → { sessionId }
  POST /api/inboxes                → { address, created_at }
  GET  /api/inboxes/{addr}/messages → [ { subject, body, from_address, received_at } ]
"""

import asyncio
import http.client
import io
import json
import random
import re
import socket
import ssl
import time
import urllib.error
import urllib.request
from typing import List, Optional, Dict, Any

from .config import TEMPIK_BASE, DATA_DIR
from .utils import write_log
from urllib.parse import urlparse

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
#
# 3 was not enough on a cold connection: a measured run lost two accounts in a
# row because the line dropped for 75s and each attempt only covers ~12s. Five
# attempts span roughly that window without slowing healthy requests at all.
_HTTP_RETRIES = 5
_RETRY_BACKOFF = 1.5  # seconds, multiplied by the attempt number

# Per-request socket timeout. Healthy answers take ~0.3s; a dropped reply is
# better detected quickly and retried than waited out.
_REQUEST_TIMEOUT = 12.0

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

# `<style>` contents survive the tag stripper in _plain(), so inline CSS like
# `color:#000000` leaves a bare 000000 in the text. That is a colour, not a
# code: it can win the keyword match, and it can turn "the only 6-digit number
# in the mail" into two numbers so the real code is rejected as ambiguous.
# Hex colours containing a-f cannot match a digits-only regex, but keep them
# listed so the intent is obvious if the extractor ever widens.
_CSS_COLOR_CODES = frozenset(
    {
        "000000", "111111", "222222", "333333", "444444", "555555",
        "666666", "777777", "888888", "999999", "232323", "464646",
        "aaaaaa", "bbbbbb", "cccccc", "dddddd", "eeeeee", "ffffff",
        "f9f8f9",
    }
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


# Resolving costs a round trip to the resolver and sometimes fails outright on
# this network: measured here, five consecutive lookups each burned 11 seconds
# and then failed, before the sixth answered. The answer rarely changes, so it
# is cached for the process *and* on disk — a later run can start without
# asking DNS at all.
_DNS_TTL = 600
_DNS_CACHE: Dict[str, tuple] = {}
_DNS_FILE = DATA_DIR / "dns_cache.json"


def _load_dns_file() -> Dict[str, str]:
    try:
        data = json.loads(_DNS_FILE.read_text(encoding="utf-8"))
        return data if isinstance(data, dict) else {}
    except Exception:
        return {}


def _save_dns_file(host: str, ip: str) -> None:
    try:
        data = _load_dns_file()
        data[host] = ip
        _DNS_FILE.parent.mkdir(parents=True, exist_ok=True)
        _DNS_FILE.write_text(json.dumps(data, indent=1), encoding="utf-8")
    except Exception:
        pass


class _KeepAlive:
    """One HTTPS connection, kept open and rebuilt only when it breaks.

    The uplink to Cloudflare measured 33% packet loss at ~300ms RTT, so a
    fresh TLS handshake — five or six round trips, each able to lose a packet
    — succeeded about half the time, while a request on an already-open socket
    went through 8 out of 8. Polling every 5s used to cost twelve handshakes
    per wait window; this turns them into one.

    IPv4 is forced on purpose: this host's IPv6 route to Cloudflare accepts the
    TCP connection and then resets the payload, and Python does not fall back.
    """

    def __init__(self, host: str, port: int = 443, timeout: float = 20.0,
                 tls: bool = True):
        self.host = host
        self.port = port
        self.timeout = timeout
        self.tls = tls
        self._conn: Optional[http.client.HTTPConnection] = None
        self._ip: Optional[str] = None
        self._ctx = ssl.create_default_context()

    def _resolve(self) -> str:
        if self._ip:
            return self._ip

        now = time.time()
        cached = _DNS_CACHE.get(self.host)
        if cached and cached[1] > now:
            self._ip = cached[0]
            return self._ip

        last: Optional[Exception] = None
        persisted = _load_dns_file().get(self.host)
        rounds = 1 if persisted else 2

        for attempt in range(1, rounds + 1):
            try:
                infos = socket.getaddrinfo(
                    self.host, self.port, socket.AF_INET, socket.SOCK_STREAM
                )
                return self._remember(infos[0][4][0])
            except Exception as e:
                last = e

            # Some resolvers answer AAAA only for a moment; the A-filtered
            # query then fails with "getaddrinfo failed" even though the name
            # does resolve. Ask again unfiltered and take an IPv4 answer if one
            # is present, since this host's IPv6 route to Cloudflare is broken.
            try:
                infos = socket.getaddrinfo(
                    self.host, self.port, socket.AF_UNSPEC, socket.SOCK_STREAM
                )
                v4 = [i for i in infos if i[0] == socket.AF_INET]
                if v4:
                    return self._remember(v4[0][4][0])
            except Exception as e:
                last = e

            write_log(
                f"DNS lookup for {self.host} failed (attempt {attempt}/{rounds}): "
                f"{last}",
                "WARNING",
            )
            time.sleep(0.5 * attempt)

        if persisted:
            write_log(
                f"DNS unavailable; using the last known address for "
                f"{self.host} ({persisted})",
                "WARNING",
            )
            self._ip = persisted
            return persisted

        raise last  # type: ignore[misc]

    def _remember(self, ip: str) -> str:
        self._ip = ip
        _DNS_CACHE[self.host] = (ip, time.time() + _DNS_TTL)
        _save_dns_file(self.host, ip)
        return ip

    def close(self) -> None:
        if self._conn is not None:
            try:
                self._conn.close()
            except Exception:
                pass
        self._conn = None

    def _connect(self) -> None:
        self.close()
        try:
            ip = self._resolve()
            raw = socket.create_connection((ip, self.port), timeout=self.timeout)
        except Exception:
            self._ip = None  # the A record may have moved
            raise

        raw.setsockopt(socket.IPPROTO_TCP, socket.TCP_NODELAY, 1)
        if self.tls:
            try:
                sock = self._ctx.wrap_socket(raw, server_hostname=self.host)
            except Exception:
                raw.close()
                raise
            conn = http.client.HTTPSConnection(
                self.host, self.port, timeout=self.timeout
            )
        else:
            sock = raw
            conn = http.client.HTTPConnection(
                self.host, self.port, timeout=self.timeout
            )

        conn.sock = sock  # request() then skips its own connect()
        self._conn = conn

    def request(self, method: str, path: str, body: Optional[bytes],
                headers: Dict[str, str]) -> tuple:
        if self._conn is None or self._conn.sock is None:
            self._connect()

        assert self._conn is not None
        self._conn.request(method, path, body=body, headers=headers)
        resp = self._conn.getresponse()
        data = resp.read()
        if resp.will_close:
            self.close()
        return resp.status, data


class TempikClient:
    """Tempik disposable email API client."""

    def __init__(self, base_url: str = None):
        self.base_url = (base_url or TEMPIK_BASE).rstrip("/")
        self.session_id: Optional[str] = None
        self._email: Optional[str] = None
        self._domains: List[str] = []

        parsed = urlparse(self.base_url)
        self._prefix = parsed.path.rstrip("/")
        self._http = _KeepAlive(
            parsed.hostname or "",
            port=parsed.port or (80 if parsed.scheme == "http" else 443),
            # A healthy answer arrives in ~0.3s. When the uplink drops the
            # reply, waiting 20s per attempt just delays the retry that would
            # have worked; 12s is already ~40x the normal latency.
            timeout=_REQUEST_TIMEOUT,
            tls=parsed.scheme != "http",
        )

    # ==================== TRANSPORT ====================
    def _request(
        self,
        path: str,
        method: str = "GET",
        body: Optional[Dict[str, Any]] = None,
        with_session: bool = True,
        retries: int = _HTTP_RETRIES,
    ) -> Any:
        """Call the tempik API over a reused connection.

        4xx answers are re-raised immediately (except 429): they mean the
        request itself is wrong, so retrying cannot change the outcome.
        Transport failures drop the socket and retry with jittered backoff —
        on a lossy uplink the retry is usually a different packet draw.
        """
        target = f"{self._prefix}{path}" or "/"
        payload = None if body is None else json.dumps(body).encode()

        headers = {
            "Accept": "application/json",
            "User-Agent": _BROWSER_UA,
            "Accept-Language": "en-US,en;q=0.9",
        }
        if payload is not None:
            headers["Content-Type"] = "application/json"
        if with_session and self.session_id:
            headers["x-session-id"] = self.session_id

        last: Optional[Exception] = None

        for attempt in range(1, retries + 1):
            try:
                status, raw = self._http.request(method, target, payload, headers)
                if 400 <= status < 500 and status != 429:
                    raise urllib.error.HTTPError(
                        f"{self.base_url}{path}",
                        status,
                        f"HTTP {status}",
                        None,
                        io.BytesIO(raw),
                    )
                if status >= 400:
                    raise IOError(f"HTTP {status}: {raw[:120]!r}")
                return json.loads(raw.decode())
            except urllib.error.HTTPError:
                raise
            except Exception as e:
                last = e
                self._http.close()

            if attempt < retries:
                delay = _RETRY_BACKOFF * attempt * random.uniform(0.8, 1.4)
                write_log(
                    f"Tempik {method} {path} attempt {attempt}/{retries} "
                    f"failed: {last}",
                    "WARNING",
                )
                time.sleep(delay)

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
        an unrelated number further down the mail. CSS colour codes are
        skipped: `verification code` followed by a `<style>` block can put
        000000 right where the real code should be.
        """
        for match in re.finditer(
            _CODE_PREFIX + r"[^0-9]{0,60}(\d{4,8})", text, re.IGNORECASE
        ):
            code = match.group(1)
            if code.lower() not in _CSS_COLOR_CODES:
                return code
        return None

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
            found = set(re.findall(r"\b(\d{6})\b", text)) - _CSS_COLOR_CODES
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