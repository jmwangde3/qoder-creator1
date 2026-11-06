"""Registration names must vary across a batch.

Every account used to be "User Dev"; a hundred identical names is a trivial
signal that the accounts were generated.
"""
import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from src.utils import generate_name  # noqa: E402


class NameGenerationTest(unittest.TestCase):
    def test_names_are_varied(self):
        names = {generate_name() for _ in range(200)}
        self.assertGreater(len(names), 150)

    def test_parts_are_letters_and_distinct(self):
        for _ in range(100):
            first, last = generate_name()
            self.assertTrue(first.isalpha(), first)
            self.assertTrue(last.isalpha(), last)
            self.assertNotEqual(first, last)


if __name__ == "__main__":
    unittest.main()
