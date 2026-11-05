"""Selector fallback against a *different* Aliyun markup revision.

The solver used to address every element by its `aliyunCaptcha-*` ID. These
tests render a widget that has none of those IDs -- only the generic
`.nc-container` / `.slide-verify-slider` shapes -- and check that the
presence/visibility plumbing still finds it. The image-processing pipeline
still requires the canonical IDs; that is a deliberate boundary, not an
oversight (see HANDOFF 0.7).

Run:  ./venv/Scripts/python.exe -m unittest discover -s tests -v
"""
import sys
import unittest
from pathlib import Path

from playwright.async_api import async_playwright

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from src.captcha import (  # noqa: E402
    READY_JS,
    _CLICK_TRIGGER_SELECTORS,
    is_solved,
    widget_state,
)

_PIXEL = (
    "data:image/png;base64,"
    "iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAYAAAAfFcSJAAAADUlEQVR42mP8z8BQDwAEhQGAhKmMIQAAAABJRU5ErkJggg=="
)

GENERIC_MARKUP = f"""
<div class="nc-container" id="foo-captcha">
  <img src="{_PIXEL}" style="width:300px;height:200px">
  <div class="slide-verify">
    <div class="slide-verify-slider" style="width:40px;height:40px"></div>
  </div>
  <div class="captcha-box"><button class="btn">Click to verify</button></div>
</div>
"""

CANONICAL_MARKUP = f"""
<div id="aliyunCaptcha-captcha-wrapper">
  <div id="aliyunCaptcha-captcha-body">
    <img id="aliyunCaptcha-img" src="{_PIXEL}" style="width:300px;height:200px">
    <div id="aliyunCaptcha-sliding-slider" style="width:40px;height:40px"></div>
  </div>
</div>
"""


class SelectorFallbackTest(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        try:
            self.pw = await async_playwright().start()
            self.browser = await self.pw.chromium.launch(headless=True)
        except Exception as exc:  # pragma: no cover - environment dependent
            self.skipTest(f"Chromium unavailable: {exc}")
        self.page = await self.browser.new_page()

    async def asyncTearDown(self):
        await self.browser.close()
        await self.pw.stop()

    async def test_generic_markup_is_still_detected(self):
        await self.page.set_content(GENERIC_MARKUP)
        state = await widget_state(self.page)
        self.assertTrue(state["img"], state)
        self.assertTrue(state["slider"], state)
        self.assertTrue(state["wrapper"], state)
        self.assertTrue(state["anyVisible"], state)
        await self.page.wait_for_function(READY_JS, timeout=2000)
        self.assertFalse(await is_solved(self.page))

    async def test_canonical_markup_is_unchanged(self):
        await self.page.set_content(CANONICAL_MARKUP)
        state = await widget_state(self.page)
        self.assertTrue(state["img"], state)
        self.assertTrue(state["slider"], state)
        self.assertTrue(state["wrapper"], state)
        await self.page.wait_for_function(READY_JS, timeout=2000)
        self.assertFalse(await is_solved(self.page))

    async def test_widget_gone_counts_as_solved(self):
        await self.page.set_content("<p>nothing here</p>")
        self.assertTrue(await is_solved(self.page))
        state = await widget_state(self.page)
        self.assertFalse(state["anyVisible"], state)
        self.assertFalse(state["slider"], state)

    async def test_click_trigger_list_finds_a_generic_button(self):
        clicked = []

        await self.page.expose_function("report_click", lambda: clicked.append(1))
        await self.page.set_content(
            GENERIC_MARKUP.replace(
                '<button class="btn">',
                '<button class="btn" onclick="report_click()">',
            )
        )
        await self.page.click(", ".join(_CLICK_TRIGGER_SELECTORS), timeout=2000)
        self.assertEqual(len(clicked), 1)


if __name__ == "__main__":
    unittest.main()
