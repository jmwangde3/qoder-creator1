"""One failed account must not take the batch down with it.

A 10-account run stopped on #6 with "getaddrinfo failed" while creating the
mailbox: the call sat outside create_account()'s try block and escaped into
the batch loop, which had no guard of its own.
"""
import asyncio
import io
import socket
import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from rich.console import Console  # noqa: E402

import src.tempmail as tempmail_mod  # noqa: E402
from src import signup as signup_mod  # noqa: E402


class _BoomMailbox:
    def create_inbox(self, *a, **k):
        raise socket.gaierror(11001, "getaddrinfo failed")


class MailboxFailureTest(unittest.TestCase):
    def test_create_account_returns_none_instead_of_raising(self):
        original = signup_mod.tempmail_client
        signup_mod.tempmail_client = lambda: _BoomMailbox()
        try:
            mgr = signup_mod.SignupManager(
                None, True, Console(file=io.StringIO(), width=80)
            )
            result = asyncio.run(mgr.create_account(6))
        finally:
            signup_mod.tempmail_client = original
        self.assertIsNone(result)

    def test_batch_keeps_going_after_one_account_raises(self):
        calls = []

        async def flaky(self, idx=0, proxy=None):
            calls.append(idx)
            if idx == 2:
                raise RuntimeError("boom")
            return {"email": f"a{idx}@x", "pat_valid": True}

        original = signup_mod.SignupManager.create_account
        original_randint = signup_mod.random.randint
        signup_mod.SignupManager.create_account = flaky
        signup_mod.random.randint = lambda a, b: 0
        try:
            out = asyncio.run(
                signup_mod.create_accounts(
                    count=3, proxy_pool=None, headless=True, delay=0,
                    console=Console(file=io.StringIO(), width=80),
                )
            )
        finally:
            signup_mod.SignupManager.create_account = original
            signup_mod.random.randint = original_randint

        self.assertEqual(calls, [1, 2, 3])       # all three were attempted
        self.assertEqual(len(out), 2)            # the two good ones survived


class DnsCacheTest(unittest.TestCase):
    def setUp(self):
        import tempfile

        tempmail_mod._DNS_CACHE.clear()
        self._real = socket.getaddrinfo
        self._original_file = tempmail_mod._DNS_FILE
        # Never let a test write into the real data/dns_cache.json.
        tempmail_mod._DNS_FILE = Path(tempfile.mkdtemp()) / "dns_cache.json"

    def tearDown(self):
        socket.getaddrinfo = self._real
        tempmail_mod._DNS_FILE = self._original_file
        tempmail_mod._DNS_CACHE.clear()

    def test_answer_is_reused_by_later_clients(self):
        calls = []

        def fake(host, port, family=0, type=0, *a, **k):
            calls.append(family)
            return [(socket.AF_INET, socket.SOCK_STREAM, 6, "", ("93.184.216.34", port))]

        socket.getaddrinfo = fake
        self.assertEqual(tempmail_mod._KeepAlive("example.test")._resolve(), "93.184.216.34")
        self.assertEqual(tempmail_mod._KeepAlive("example.test")._resolve(), "93.184.216.34")
        self.assertEqual(len(calls), 1)  # second client hit the cache

    def test_falls_back_when_the_filtered_query_fails(self):
        def fake(host, port, family=0, type=0, *a, **k):
            if family == socket.AF_INET:
                raise socket.gaierror(11001, "getaddrinfo failed")
            return [
                (socket.AF_INET6, socket.SOCK_STREAM, 6, "", ("2606:4700::1", port, 0, 0)),
                (socket.AF_INET, socket.SOCK_STREAM, 6, "", ("104.21.82.183", port)),
            ]

        socket.getaddrinfo = fake
        self.assertEqual(tempmail_mod._KeepAlive("example.test")._resolve(), "104.21.82.183")

    def test_uses_last_known_address_when_dns_is_down(self):
        tempmail_mod._DNS_FILE.write_text(
            '{"example.test": "104.21.82.183"}', encoding="utf-8"
        )

        def down(*a, **k):
            raise socket.gaierror(11001, "getaddrinfo failed")

        socket.getaddrinfo = down
        self.assertEqual(
            tempmail_mod._KeepAlive("example.test")._resolve(), "104.21.82.183"
        )


if __name__ == "__main__":
    unittest.main()
