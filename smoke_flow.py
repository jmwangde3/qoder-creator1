"""Exercises the real automation code path against a LOCAL mock signup site.

What is REAL here:
  - playwright launch (stealth args/context), page navigation, form fill,
    button clicks, OTP field typing, PAT fetch, JSONL persistence
  - tempik inbox creation against the live instance
  - src.tempmail.extract_otp() parsing a Qoder-shaped email

What is STUBBED (and why):
  - solve_slider_local  -> the Aliyun slider bypass is not exercised
  - wait_for_messages   -> mail transport replaced by a synthetic payload
  - QODER_BASE          -> points at the local mock, never qoder.com
"""
import asyncio
import json
import os
import socket
import sys
from pathlib import Path

# The tempik calls use urllib, which honours *_proxy from the environment.
# A stale proxy here turns every request into a timeout, so drop them.
for _v in ("http_proxy", "HTTP_PROXY", "https_proxy", "HTTPS_PROXY", "all_proxy", "ALL_PROXY"):
    os.environ.pop(_v, None)

_orig = socket.getaddrinfo


def _prefer_v4(h, p, *a, **k):
    res = _orig(h, p, *a, **k)
    v4 = [r for r in res if r[0] == socket.AF_INET]
    return v4 or res


# This host's IPv6 route to Cloudflare is half-broken (TCP connects, payload
# gets reset) and Python does not fall back to IPv4 on its own.
socket.getaddrinfo = _prefer_v4

ROOT = Path(__file__).resolve().parent
sys.path.insert(0, str(ROOT))

import mock_qoder
from src import signup as signup_mod
from src.tempmail import TempikClient, utc_stamp

OTP = mock_qoder.OTP
SMOKE_ACCOUNTS = ROOT / "data" / "smoke_accounts.jsonl"
if SMOKE_ACCOUNTS.exists():
    SMOKE_ACCOUNTS.unlink()

srv = mock_qoder.start(8799)
print("mock signup server: http://127.0.0.1:8799")

# --- point the automation at the mock --------------------------------
signup_mod.QODER_BASE = "http://127.0.0.1:8799"
signup_mod.ACCOUNTS_FILE = SMOKE_ACCOUNTS

# --- stub the captcha step ------------------------------------------
async def _captcha_stub(page, max_attempts=5, console=None):
    print("  [stub] slider captcha -> True (not exercised)")
    return True


signup_mod.solve_slider_local = _captcha_stub

# --- stand in for mail transport ------------------------------------
HTML = (
    "<html><body><h2>Verify your email</h2>"
    "<div style='font-size:30px'>{code}</div>"
    "<p>This code expires in 10 minutes. Then start using Qoder.</p>"
    "<p>If you did not request this, ignore this mail.</p></body></html>"
)

STALE_CODE = "111111"
_wait_calls = {"n": 0}


def _mail(code, received_at):
    return {
        "subject": "Verify your Email with Qoder",
        "from_address": "no-reply@qoder.com",
        "received_at": received_at,
        "body": HTML.format(code=code),
    }


async def _wait_stub(self, address=None, max_wait=150, interval=5, since=None):
    """Reproduce the live failure: a stale code sitting next to a fresh one.

    Round 1: nothing arrives (the first send is lost) -> caller resends.
    Round 2: the mailbox holds the mail from the *first* send, which the
             resend has already invalidated, plus the fresh one. Submitting
             the stale code is what made a real run fail.
    """
    _wait_calls["n"] += 1
    if _wait_calls["n"] == 1:
        print("  [stub] wait_for_messages -> 0 fresh mail (first send lost)")
        return []

    mailbox = [
        _mail(OTP, utc_stamp()),                  # issued by this round's send
        _mail(STALE_CODE, "2020-01-01 00:00:00"),  # issued long before `since`
    ]
    fresh = TempikClient.newer_than(mailbox, since)
    print(
        f"  [stub] wait_for_messages -> mailbox={len(mailbox)} "
        f"after filtering={len(fresh)} (since={since})"
    )
    return fresh


TempikClient.wait_for_messages = _wait_stub


async def main():
    if sys.platform == "win32":
        for stream in (sys.stdout, sys.stderr):
            if stream is not None and hasattr(stream, "reconfigure"):
                try:
                    stream.reconfigure(encoding="utf-8", errors="replace")
                except (OSError, ValueError):
                    pass
    from rich.console import Console
    mgr = signup_mod.SignupManager(proxy_pool=None, headless=True, console=Console())
    result = await mgr.create_account(1)

    print("\n" + "=" * 58)
    print("RESULT")
    print("=" * 58)
    if not result:
        print("create_account() returned None")
        return 1
    for k in ("email", "password", "pat_token", "pat_valid", "url", "created_at"):
        v = result.get(k)
        if k == "pat_token" and v:
            v = f"{v[:18]}...({len(v)} chars)"
        print(f"  {k:12s}: {v}")
    print("\n  accounts file:", SMOKE_ACCOUNTS, "exists =", SMOKE_ACCOUNTS.exists())
    if SMOKE_ACCOUNTS.exists():
        rec = json.loads(SMOKE_ACCOUNTS.read_text(encoding="utf-8").strip().splitlines()[-1])
        print("  jsonl record keys:", list(rec.keys()))

    tried = mock_qoder.SUBMITTED
    stale = [c for c in tried if c == STALE_CODE]
    print("\n  codes submitted:", tried)
    print("  stale code submitted:", len(stale), "(must be 0)")
    if stale:
        print("  FAIL: a code issued before the latest send was submitted")
        return 3
    return 0 if result.get("pat_valid") else 2


rc = asyncio.run(main())
srv.shutdown()
sys.exit(rc)
