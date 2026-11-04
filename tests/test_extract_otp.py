"""Regression tests for TempikClient.extract_otp.

The fixture in fixtures/qoder_otp_cn.json is a real Qoder verification mail
(Chinese template). Its actual code is 507487. Keep it verbatim — the point
is that the extractor works against the mail Qoder really sends, not against
a hand-written approximation.

Run:  python -m unittest discover -s tests -v
"""
import json
import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from src.tempmail import TempikClient  # noqa: E402

FIXTURE = Path(__file__).parent / "fixtures" / "qoder_otp_cn.json"
QLD_CODE = "507487"


def real_qoder_mail():
    with open(FIXTURE, encoding="utf-8") as f:
        return json.load(f)


def mail(frm, subject, body):
    return {"from_address": frm, "subject": subject, "body": body}


class ExtractOtpTest(unittest.TestCase):
    def setUp(self):
        self.c = TempikClient("https://example.invalid/api")

    def test_real_qoder_mail(self):
        """The actual Chinese template must yield its code."""
        self.assertEqual(self.c.extract_otp([real_qoder_mail()]), QLD_CODE)

    def test_real_mail_wins_over_unrelated_mail_first(self):
        """A stray invoice must not shadow the real code."""
        noise = mail("billing@shop.com", "Your invoice", "Order 202611 total 899.00")
        self.assertEqual(
            self.c.extract_otp([noise, real_qoder_mail()]), QLD_CODE
        )

    def test_real_mail_wins_over_ambiguous_mail(self):
        noise = mail("promo@shop.com", "Promo", "coupon 445566 and 778899")
        self.assertEqual(self.c.extract_otp([noise, real_qoder_mail()]), QLD_CODE)

    def test_newer_welcome_mail_does_not_shadow_the_code(self):
        """Qoder's welcome mail arrives after the OTP and is also from
        no-reply@qoder.com — it must not win just by being newer."""
        welcome = mail(
            "no-reply@qoder.com",
            "Welcome to Qoder! Here's Your Free Pro Trial & 300 Credits",
            "<html><body><p>Hi, welcome. A 2-week Pro trial and 300 Credits "
            "await you.</p></body></html>",
        )
        # newest first, which is the order tempik returns
        self.assertEqual(
            self.c.extract_otp([welcome, real_qoder_mail()]), QLD_CODE
        )

    def test_no_guess_from_invoice(self):
        """A lone order number is not a verification code."""
        noise = mail("billing@shop.com", "Your invoice", "Order 202611 total 899.00")
        self.assertIsNone(self.c.extract_otp([noise]))

    def test_no_guess_from_ambiguous_numbers(self):
        noise = mail("promo@shop.com", "Promo", "coupon 445566 and 778899")
        self.assertIsNone(self.c.extract_otp([noise]))

    def test_no_guess_when_chinese_text_merely_says_verify(self):
        noise = mail("news@shop.com", "促销", "订单号 202611，验证一下")
        self.assertIsNone(self.c.extract_otp([noise]))

    def test_empty_input(self):
        self.assertIsNone(self.c.extract_otp([]))

    def test_english_template_still_works(self):
        msg = mail(
            "no-reply@qoder.com",
            "Verify your email",
            "<html><body><p>Your verification code is</p><b>482913</b></body></html>",
        )
        self.assertEqual(self.c.extract_otp([msg]), "482913")


if __name__ == "__main__":
    unittest.main(verbosity=2)
