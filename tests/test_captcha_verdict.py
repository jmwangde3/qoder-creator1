"""The verify verdict is the only signal that tells a miss from a bot flag.

These tests pin the parsing, because a silent change here turns every
diagnosis back into a guess.
"""

import pathlib
import sys
import unittest

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parents[1]))

from src import captcha  # noqa: E402


class FakeResponse:
    def __init__(self, url, payload, content_type="application/json"):
        self.url = url
        self._payload = payload
        self.headers = {"content-type": content_type}

    async def json(self):
        if isinstance(self._payload, Exception):
            raise self._payload
        return self._payload


class FakePage:
    def __init__(self):
        self.handlers = {}

    def on(self, event, handler):
        self.handlers[event] = handler


class VerdictHookTest(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        captcha._VERDICT_SINKS.clear()
        captcha._VERDICT_HOOKED.clear()

    async def test_reads_nested_result_code(self):
        page = FakePage()
        sink = captcha._install_verdict_hook(page)
        await page.handlers["response"](
            FakeResponse(
                "https://xxx.aliyuncs.com/captcha/-verify.json",
                {"Result": {"VerifyCode": "T001"}},
            )
        )
        self.assertEqual(sink, ["T001"])

    async def test_reads_flat_codes(self):
        for payload in (
            {"result": "F015"},
            {"VerifyCode": "f001"},
            {"Result": "T001"},
        ):
            page = FakePage()
            sink = captcha._install_verdict_hook(page)
            await page.handlers["response"](
                FakeResponse("https://x/-verify", payload)
            )
            self.assertEqual(len(sink), 1, payload)
        # Codes are normalised to upper case.
        self.assertEqual(sink, ["T001"])

    async def test_ignores_unrelated_urls(self):
        page = FakePage()
        sink = captcha._install_verdict_hook(page)
        handler = page.handlers["response"]
        await handler(FakeResponse("https://x/other.json", {"Result": "T001"}))
        await handler(FakeResponse("https://x/static/app.js", {"Result": "T001"}))
        self.assertEqual(sink, [])

    async def test_accepts_a_plain_text_body(self):
        # The endpoint does not always label itself as JSON; refusing to parse
        # it would silently drop every verdict.
        page = FakePage()
        sink = captcha._install_verdict_hook(page)
        await page.handlers["response"](
            FakeResponse(
                "https://x/-verify", {"Result": "F015"}, content_type="text/plain"
            )
        )
        self.assertEqual(sink, ["F015"])

    async def test_survives_a_body_that_cannot_be_parsed(self):
        page = FakePage()
        sink = captcha._install_verdict_hook(page)
        await page.handlers["response"](
            FakeResponse("https://x/-verify", ValueError("not json"))
        )
        self.assertEqual(sink, [])

    async def test_hook_is_installed_once_per_page(self):
        page = FakePage()
        first = captcha._install_verdict_hook(page)
        handler = page.handlers["response"]
        second = captcha._install_verdict_hook(page)
        self.assertIs(first, second)
        self.assertIs(page.handlers["response"], handler)


if __name__ == "__main__":
    unittest.main()
