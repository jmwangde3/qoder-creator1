"""The slider drag must land where the solver says, not 1/4 of the way there.

A run failed 7 of 50 accounts on the captcha, and every one of them failed all
five attempts. The screenshots show the piece sitting at the far left of the
image while the gap is ~165px away, and the code explains it: `displayLeft` is
already in CSS pixels, but the drag multiplied it by (dl / (bgW - 52)) and then
by the *handle* width, so the first move landed far short. The coarse move was
supposed to be corrected by a refine loop reading `#aliyunCaptcha-puzzle.style.left`
— which is empty when the widget positions the piece with a transform, so the
loop believed the piece had never moved and kept pushing right.

These tests pin the arithmetic that replaces it: a straight 1:1 offset, scaled
by the gain measured from a short probe.
"""
import pathlib
import sys
import unittest

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parents[1]))

from src.captcha import _mouse_delta  # noqa: E402


class MouseDeltaTest(unittest.TestCase):
    def test_remaining_distance_is_one_to_one(self):
        # Probe moved the piece to 24; the gap is at 165 -> 141 more pixels.
        self.assertAlmostEqual(_mouse_delta(165.0, 24.0, 1.0), 141.0)

    def test_gain_scales_the_correction(self):
        # Half a pixel of piece per pixel of mouse needs twice the movement.
        self.assertAlmostEqual(_mouse_delta(165.0, 24.0, 0.5), 282.0)

    def test_overshoot_corrects_backwards(self):
        self.assertAlmostEqual(_mouse_delta(100.0, 140.0, 1.0), -40.0)

    def test_zero_gain_cannot_divide_by_zero(self):
        # A dead slider still has to produce a finite number.
        self.assertAlmostEqual(_mouse_delta(100.0, 0.0, 0.0), 500.0)

    def test_already_on_target(self):
        self.assertAlmostEqual(_mouse_delta(50.0, 50.0, 1.0), 0.0)


if __name__ == "__main__":
    unittest.main()
