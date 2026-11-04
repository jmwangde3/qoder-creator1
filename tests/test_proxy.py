"""Regression tests for ProxyPool._parse.

The original parser only understood strings starting with 'http'. A socks5://
line fell through to the host:port:user:pass branch and was shredded into
  server=http://socks5://u  username=p@1.2.3.4  password=1080
so every SOCKS5 proxy in proxies.txt was silently useless.

Run:  python -m unittest discover -s tests -v
"""
import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from src.proxy import ProxyPool  # noqa: E402


class ProxyParseTest(unittest.TestCase):
    def test_http_with_credentials(self):
        self.assertEqual(
            ProxyPool._parse("http://u:p@1.2.3.4:8080"),
            {"server": "http://1.2.3.4:8080", "username": "u", "password": "p"},
        )

    def test_host_port_user_pass(self):
        self.assertEqual(
            ProxyPool._parse("1.2.3.4:8080:u:p"),
            {"server": "http://1.2.3.4:8080", "username": "u", "password": "p"},
        )

    def test_host_port_only(self):
        self.assertEqual(
            ProxyPool._parse("1.2.3.4:8080"), {"server": "http://1.2.3.4:8080"}
        )

    def test_socks5_keeps_its_scheme(self):
        """This is the case that used to produce garbage."""
        self.assertEqual(
            ProxyPool._parse("socks5://u:p@1.2.3.4:1080"),
            {"server": "socks5://1.2.3.4:1080", "username": "u", "password": "p"},
        )

    def test_socks5_without_credentials(self):
        self.assertEqual(
            ProxyPool._parse("socks5h://1.2.3.4:1080"),
            {"server": "socks5h://1.2.3.4:1080"},
        )

    def test_percent_encoded_credentials(self):
        self.assertEqual(
            ProxyPool._parse("https://u%40x:p@h.example:3128"),
            {"server": "https://h.example:3128", "username": "u@x", "password": "p"},
        )

    def test_invalid_line_raises(self):
        with self.assertRaises(ValueError):
            ProxyPool._parse("just-a-host")


if __name__ == "__main__":
    unittest.main(verbosity=2)
