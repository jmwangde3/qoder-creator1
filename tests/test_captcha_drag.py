"""Regression tests for the smooth drag arc.

The widget grades the trajectory, not only the endpoint, so the arc has to be
monotonic, eased and finish exactly on the requested position. These are pure
arithmetic checks; no browser needed.
"""

import pathlib
import sys
import unittest

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parents[1]))

from src.captcha import _smooth_move  # noqa: E402


class FakeMouse:
    def __init__(self):
        self.points = []

    async def move(self, x, _y, steps=1):
        self.points.append(float(x))


class FakePage:
    def __init__(self):
        self.mouse = FakeMouse()
        self.pauses = []

    async def wait_for_timeout(self, milliseconds):
        self.pauses.append(milliseconds)


class SmoothMoveTest(unittest.IsolatedAsyncioTestCase):
    async def test_ends_exactly_on_target(self):
        page = FakePage()
        await _smooth_move(page, 100.0, 250.0, 50.0, segments=8, gap_ms=0)
        self.assertEqual(len(page.mouse.points), 8)
        self.assertAlmostEqual(page.mouse.points[-1], 250.0, delta=0.01)

    async def test_forward_arc_is_monotonic(self):
        page = FakePage()
        await _smooth_move(page, 120.0, 380.0, 50.0, segments=10, gap_ms=0)
        for previous, current in zip(page.mouse.points, page.mouse.points[1:]):
            self.assertLess(previous, current)

    async def test_backward_arc_is_monotonic(self):
        page = FakePage()
        await _smooth_move(page, 380.0, 120.0, 50.0, segments=10, gap_ms=0)
        for previous, current in zip(page.mouse.points, page.mouse.points[1:]):
            self.assertGreater(previous, current)

    async def test_ease_out_covers_most_ground_early(self):
        # A human decelerates: over half the segments the cursor should have
        # covered well over half the distance.
        page = FakePage()
        await _smooth_move(page, 0.0, 260.0, 50.0, segments=10, gap_ms=0)
        halfway = page.mouse.points[4]
        self.assertGreater(halfway, 130.0)

    async def test_pauses_once_per_segment(self):
        page = FakePage()
        await _smooth_move(page, 0.0, 90.0, 50.0, segments=6, gap_ms=18)
        self.assertEqual(page.pauses, [18] * 6)


if __name__ == "__main__":
    unittest.main()
