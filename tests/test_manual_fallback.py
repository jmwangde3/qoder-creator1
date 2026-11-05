"""Regression tests for the bounded human slider fallback.

The fallback exists for the case where every automatic attempt failed and a
person happens to be in front of a visible browser. The two properties that
matter are the opposite of each other: it must never hang an unattended batch,
and it must never report success just because the widget briefly vanished
while reloading after a rejected attempt.

Run:  ./venv/Scripts/python.exe -m unittest discover -s tests -v
"""
import sys
import unittest
from pathlib import Path
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from src.captcha import _wait_for_human, solve_slider_local  # noqa: E402


class FakeClock:
    """Monotonic clock that only advances when the page 'waits'."""

    def __init__(self):
        self.t = 100.0

    def __call__(self):
        return self.t

    def advance(self, seconds):
        self.t += seconds


class FakePage:
    def __init__(self, clock):
        self.clock = clock
        self.pauses = []

    async def wait_for_timeout(self, ms):
        self.pauses.append(ms)
        self.clock.advance(ms / 1000.0)


class HumanFallbackTest(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.clock = FakeClock()
        self.page = FakePage(self.clock)
        self.lines = []

    def emit(self, message):
        self.lines.append(message)

    async def run_fallback(self, timeout=3.0, solved=None, verdicts=None, mark=0):
        async def never(_page):
            return False

        return await _wait_for_human(
            self.page,
            timeout,
            self.emit,
            verdicts=verdicts,
            verdict_mark=mark,
            solved_fn=solved or never,
            clock=self.clock,
        )

    async def test_times_out_and_never_reports_success(self):
        result = await self.run_fallback(timeout=3.0)
        self.assertFalse(result)
        self.assertTrue(any("timed out" in line for line in self.lines))
        # 3 seconds at 500ms per poll. No real sleeping happened.
        self.assertEqual(len(self.page.pauses), 6)
        self.assertTrue(all(p == 500 for p in self.page.pauses))

    async def test_t001_is_unambiguous_success(self):
        result = await self.run_fallback(
            timeout=30.0, verdicts=["F001", "T001"], mark=1
        )
        self.assertTrue(result)
        self.assertEqual(self.page.pauses, [])
        self.assertTrue(any("T001" in line for line in self.lines))

    async def test_stale_t001_from_an_earlier_attempt_does_not_count(self):
        # verdict_mark is the length of the sink when the fallback starts, so
        # a T001 produced by the automatic attempts is not reused.
        result = await self.run_fallback(
            timeout=1.0, verdicts=["F001", "T001"], mark=2
        )
        self.assertFalse(result)

    async def test_a_single_vanish_is_not_enough(self):
        sequence = iter([True, False, False, False, False, False, False])

        async def flicker(_page):
            return next(sequence, False)

        result = await self.run_fallback(timeout=3.0, solved=flicker)
        self.assertFalse(result)
        self.assertTrue(any("timed out" in line for line in self.lines))

    async def test_two_consecutive_vanishes_do_count(self):
        sequence = iter([True, False, True, True])

        async def solved_later(_page):
            return next(sequence, False)

        result = await self.run_fallback(timeout=30.0, solved=solved_later)
        self.assertTrue(result)
        self.assertEqual(len(self.page.pauses), 3)
        self.assertTrue(any("widget gone" in line for line in self.lines))

    async def test_zero_timeout_disables_it_without_waiting(self):
        result = await self.run_fallback(timeout=0)
        self.assertFalse(result)
        self.assertEqual(self.page.pauses, [])
        self.assertTrue(any("disabled" in line for line in self.lines))

    async def test_long_window_only_prints_bounded_hints(self):
        await self.run_fallback(timeout=45.0)
        hints = [line for line in self.lines if "s left" in line]
        self.assertEqual(len(hints), 4)  # at 10/20/30/40s
        self.assertEqual(len(self.page.pauses), 90)


class StubSolverPage:
    """A widget that renders but never yields a usable target."""

    def __init__(self):
        self.pauses = []

    def on(self, *args, **kwargs):
        return None

    async def click(self, *args, **kwargs):
        return None

    async def wait_for_selector(self, *args, **kwargs):
        return None

    async def wait_for_function(self, *args, **kwargs):
        return None

    async def wait_for_timeout(self, ms):
        self.pauses.append(ms)

    async def evaluate(self, *args, **kwargs):
        return {"targetX": -1}


class FallbackWiringTest(unittest.IsolatedAsyncioTestCase):
    async def _run(self, manual_fallback):
        page = StubSolverPage()
        seen = {}

        async def fake_wait(pg, timeout, emit, **kwargs):
            seen["timeout"] = timeout
            return True

        with patch("src.captcha._install_verdict_hook", return_value=[]), patch(
            "src.captcha._wait_for_human", fake_wait
        ):
            result = await solve_slider_local(
                page,
                max_attempts=2,
                manual_fallback=manual_fallback,
                manual_timeout=12.0,
            )
        return result, seen

    async def test_enabled_after_every_automatic_attempt_fails(self):
        result, seen = await self._run(manual_fallback=True)
        self.assertTrue(result)
        self.assertEqual(seen["timeout"], 12.0)

    async def test_headless_never_waits_for_a_person(self):
        result, seen = await self._run(manual_fallback=False)
        self.assertFalse(result)
        self.assertEqual(seen, {})


if __name__ == "__main__":
    unittest.main()
