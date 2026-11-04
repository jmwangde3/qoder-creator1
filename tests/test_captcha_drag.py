"""Regression tests for the smooth drag arc.

The widget grades the trajectory, not only the endpoint, so the arc has to be
monotonic, eased and finish exactly on the requested position. These are pure
arithmetic checks; no browser needed.
"""

import pathlib
import sys
import unittest

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parents[1]))

from src.captcha import (  # noqa: E402
    MAIN_DRAG_FRACTION,
    TRIM_STEPS,
    _smooth_move,
    plan_trim,
)


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
        # Eased samples plus one deliberate final step: the widget drops
        # sub-threshold pointer deltas, so the tail cannot be eased to zero.
        self.assertEqual(len(page.mouse.points), 9)
        self.assertAlmostEqual(page.mouse.points[-1], 250.0, delta=0.01)
        self.assertGreaterEqual(
            page.mouse.points[-1] - page.mouse.points[-2], 12.0
        )

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

    async def test_speed_peaks_in_the_middle(self):
        # A hand accelerates from rest, so the first sample must be small and
        # the biggest step must fall near the middle. Ease-out put the peak on
        # the first sample, which is a generator tell.
        page = FakePage()
        await _smooth_move(page, 0.0, 260.0, 50.0, segments=10, gap_ms=0)
        points = page.mouse.points
        first_step = points[0] - 0.0
        steps = [b - a for a, b in zip(points, points[1:])]
        self.assertLess(first_step, max(steps) * 0.5)
        # Half way through the arc, roughly half the eased distance is gone
        # (the last 8% is held back for the un-eased final step).
        self.assertGreater(points[4], 110.0)
        self.assertLess(points[4], 130.0)

    async def test_pauses_once_per_segment(self):
        page = FakePage()
        await _smooth_move(page, 0.0, 90.0, 50.0, segments=6, gap_ms=18)
        self.assertEqual(len(page.pauses), 6)
        for pause in page.pauses:
            self.assertGreaterEqual(pause, 12)
            self.assertLessEqual(pause, 28)


class PlanTrimTest(unittest.TestCase):
    """The correction loop is a pure function, so it can be exercised against
    a model of the widget instead of only against the live one."""

    def test_on_target_stops(self):
        self.assertEqual(plan_trim(200.0, 200.0, 500.0, 2000.0), (500.0, True))
        self.assertEqual(plan_trim(200.0, 200.7, 500.0, 2000.0), (500.0, True))

    def test_shortfall_moves_forward_by_a_fraction(self):
        cursor, done = plan_trim(200.0, 150.0, 500.0, 2000.0)
        self.assertFalse(done)
        # 35% of 50px, not all of it: a short correction on this widget moves
        # the piece ~1.8x the cursor delta, so aiming at the remainder
        # overshoots and the loop oscillates instead of settling.
        self.assertAlmostEqual(cursor - 500.0, 17.5, delta=0.01)

    def test_overshoot_steps_back(self):
        cursor, done = plan_trim(200.0, 215.0, 520.0, 2000.0)
        self.assertFalse(done)
        self.assertLess(cursor, 520.0)

    def test_sub_pixel_step_is_widened_to_the_sampling_threshold(self):
        # 1.7px of error * 0.35 = 0.6px, which the widget would discard.
        cursor, _ = plan_trim(200.0, 198.3, 500.0, 2000.0)
        self.assertGreaterEqual(abs(cursor - 500.0), 1.5)

    def test_cannot_leave_the_viewport(self):
        cursor, _ = plan_trim(300.0, 0.0, 1500.0, 1500.0)
        self.assertEqual(cursor, 1500.0)

    def test_discarded_step_remeasures_from_the_accepted_cursor(self):
        # The cursor moved 10px but the widget dropped the step: the reading
        # still belongs to the cursor from before it. Aiming from the new
        # cursor would skip 10px of the error twice over.
        stuck_cursor, _ = plan_trim(
            200.0, 195.0, 510.0, 2000.0, applied_cursor=500.0, stuck=True
        )
        self.assertAlmostEqual(stuck_cursor - 500.0, 5.0, delta=0.01)
        # And the stuck step is not damped: a widget that discards small
        # deltas will not integrate a creeping approach either.
        self.assertGreater(stuck_cursor - 500.0, 4.6)

    def test_converges_when_the_piece_runs_ahead(self):
        # Two ratios matter. A long main drag delivers ~1.0-1.1x of the cursor
        # travel; a short correction delivers up to ~1.8x (measured trails:
        # a 7.6px trim moved the piece 14.4px, a 27.2px one moved it 48.8px).
        # The loop must be stable against both without knowing which it is in.
        for gain_main in (1.0, 1.06, 1.10):
            for gain_trim in (1.0, 1.4, 1.8):
                for target in (60.0, 140.0, 220.0, 248.0):
                    landed = target * MAIN_DRAG_FRACTION * gain_main
                    cursor = target * MAIN_DRAG_FRACTION
                    applied = cursor
                    for _ in range(TRIM_STEPS):
                        nxt, done = plan_trim(
                            target, landed, cursor, 5000.0, applied_cursor=applied
                        )
                        if done:
                            break
                        step = nxt - cursor
                        previous = landed
                        # The widget drops sub-threshold deltas and quantises
                        # what it accepts to half a pixel.
                        if abs(step) >= 1.5:
                            landed = round((landed + step * gain_trim) * 2.0) / 2.0
                        cursor = nxt
                        # A discarded step leaves `applied` behind; an accepted
                        # one re-anchors it where the reading came from.
                        applied = applied if abs(landed - previous) < 0.4 else cursor
                    self.assertLessEqual(
                        abs(target - landed),
                        2.0,
                        f"main={gain_main} trim={gain_trim} "
                        f"target={target} landed={landed}",
                    )

    def test_discard_does_not_drift_the_cursor_off_the_reading(self):
        # One step in the middle is discarded. The loop must re-measure from
        # the cursor that produced the reading instead of the one that kept
        # moving, and still land on the gap within a dozen rounds.
        for target in (80.0, 160.0, 240.0):
            landed = 0.0
            cursor = 0.0
            applied = 0.0
            for round_no in range(1, TRIM_STEPS + 1):
                nxt, done = plan_trim(
                    target, landed, cursor, 5000.0, applied_cursor=applied
                )
                if done:
                    break
                step = nxt - cursor
                previous = landed
                # The widget honours deltas of ~1.5px and delivers ~1.8x; the
                # fifth step is swallowed whole, as seen once in the live
                # trails.
                if round_no != 5 and abs(step) >= 1.5:
                    landed = round((landed + step * 1.8) * 2.0) / 2.0
                cursor = nxt
                if landed != previous:
                    applied = cursor
            self.assertLessEqual(
                abs(target - landed),
                3.0,
                f"target={target} landed={landed}",
            )


if __name__ == "__main__":
    unittest.main()
