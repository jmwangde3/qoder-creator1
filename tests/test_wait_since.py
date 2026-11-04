"""A mail issued before the latest code request carries a dead code.

Observed on a real run: the first verification mail was read only after the
automation had already re-sent, and the code from that older mail was typed
in and rejected. `newer_than()` is what keeps that mail out of the candidate
set, so it is pinned here.
"""
import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from src.tempmail import TempikClient  # noqa: E402


def mail(received_at, code="111111", sender="no-reply@qoder.com"):
    return {
        "from_address": sender,
        "subject": "Verify your Email with Qoder",
        "received_at": received_at,
        "body": f"<div style='font-size:30px'>{code}</div>",
    }


class NewerThanTest(unittest.TestCase):
    def setUp(self):
        self.c = TempikClient("https://example.invalid/api")

    def test_drops_the_mail_from_before_the_resend(self):
        stale = mail("2026-11-04 11:41:11", "635088")
        fresh = mail("2026-11-04 11:42:57", "486519")

        kept = TempikClient.newer_than([stale, fresh], "2026-11-04 11:42:50")

        self.assertEqual([m["received_at"] for m in kept], ["2026-11-04 11:42:57"])
        self.assertEqual(self.c.extract_otp(kept), "486519")

    def test_keeps_a_mail_received_exactly_at_the_boundary(self):
        msg = mail("2026-11-04 11:42:50")
        kept = TempikClient.newer_than([msg], "2026-11-04 11:42:50")
        self.assertEqual(len(kept), 1)

    def test_without_since_everything_is_kept(self):
        msgs = [mail("2020-01-01 00:00:00"), mail("2026-11-04 11:42:57")]
        self.assertEqual(len(TempikClient.newer_than(msgs, None)), 2)

    def test_mail_without_a_timestamp_is_kept(self):
        # Cannot be proven stale, and dropping it would break a backend that
        # does not expose received_at at all.
        msg = {"from_address": "no-reply@qoder.com", "subject": "Verify",
               "body": "<div>123456</div>"}
        kept = TempikClient.newer_than([msg], "2026-11-04 11:42:50")
        self.assertEqual(len(kept), 1)

    def test_stale_only_leaves_nothing_to_submit(self):
        stale = mail("2026-11-04 11:41:11", "635088")
        kept = TempikClient.newer_than([stale], "2026-11-04 11:42:50")
        self.assertEqual(kept, [])
        self.assertIsNone(self.c.extract_otp(kept))


if __name__ == "__main__":
    unittest.main()
