"""The generated password must satisfy Qoder's signup rule every time.

Uniform random selection from the full character pool looks fine but misses a
required class often enough to matter: with 14 draws from 26+26+10+8
characters, ~11% contain no digit and ~18% no special character. The form
answers "The password must be longer than 8 characters and contain numbers,
uppercase letters, lowercase letters, and special characters" and refuses to
continue. Three of five failures in one run came from this.
"""
import string
import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from src.utils import generate_password  # noqa: E402

UPPER = set(string.ascii_uppercase)
LOWER = set(string.ascii_lowercase)
DIGITS = set(string.digits)
SPECIAL = set("!@#$%^&*")


class PasswordPolicyTest(unittest.TestCase):
    def test_every_password_meets_the_rule(self):
        for _ in range(2000):
            pw = generate_password()
            where = f"generated {pw!r}"
            self.assertGreater(len(pw), 8, where)
            self.assertTrue(set(pw) & UPPER, f"no uppercase: {where}")
            self.assertTrue(set(pw) & LOWER, f"no lowercase: {where}")
            self.assertTrue(set(pw) & DIGITS, f"no digit: {where}")
            self.assertTrue(set(pw) & SPECIAL, f"no special character: {where}")

    def test_respects_the_requested_length(self):
        self.assertEqual(len(generate_password(20)), 20)
        self.assertEqual(len(generate_password(9)), 9)

    def test_passwords_are_not_trivially_repeated(self):
        seen = {generate_password() for _ in range(200)}
        self.assertGreater(len(seen), 190)


if __name__ == "__main__":
    unittest.main()
