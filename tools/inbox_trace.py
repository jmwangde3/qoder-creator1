"""Read a tempik inbox straight from Cloudflare D1 and run the project's own
OTP extractor over it.

Why this exists: when a run gives up at the verification step, the log can say
how many mails arrived but not what was in them. This reads them from the D1
database behind the tempik worker, so it works for any address regardless of
which process created the inbox — no tempik session needed.

Configuration (environment variables):
  QODER_CF_ACCOUNT   Cloudflare account id                        (required)
  QODER_D1_ID        D1 database id the tempik worker is bound to (required)
  WRANGLER_CONFIG    path to wrangler's default.toml               (optional)

The OAuth token is read from the wrangler config, i.e. the same credential
`wrangler whoami` uses. Nothing is written.

Usage:
  python tools/inbox_trace.py someone@example.com [limit]
"""
import json
import os
import socket
import sys
import tomllib
import urllib.request
from pathlib import Path

# This host's IPv6 route to Cloudflare accepts TCP and then resets the payload.
_orig = socket.getaddrinfo
socket.getaddrinfo = lambda h, p, *a, **k: (
    lambda r: ([x for x in r if x[0] == socket.AF_INET] or r)
)(_orig(h, p, *a, **k))

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from src.tempmail import TempikClient  # noqa: E402


def _wrangler_token() -> str:
    candidates = []
    if os.getenv("WRANGLER_CONFIG"):
        candidates.append(Path(os.environ["WRANGLER_CONFIG"]))
    appdata = os.getenv("APPDATA")
    if appdata:
        candidates.append(Path(appdata) / "xdg.config" / ".wrangler" / "config" / "default.toml")
    candidates.append(Path.home() / ".config" / ".wrangler" / "config" / "default.toml")

    for path in candidates:
        if path.exists():
            with open(path, "rb") as f:
                token = tomllib.load(f).get("oauth_token")
            if token:
                return token
    raise SystemExit(
        "No wrangler OAuth token found. Run `wrangler login`, or point "
        "WRANGLER_CONFIG at a default.toml that contains oauth_token."
    )


def query(account: str, database: str, token: str, sql: str, params=None):
    payload = {"sql": sql}
    if params:
        payload["params"] = params
    req = urllib.request.Request(
        f"https://api.cloudflare.com/client/v4/accounts/{account}"
        f"/d1/database/{database}/query",
        data=json.dumps(payload).encode(),
        method="POST",
    )
    req.add_header("Authorization", f"Bearer {token}")
    req.add_header("Content-Type", "application/json")
    with urllib.request.urlopen(req, timeout=30) as resp:
        return json.load(resp)["result"][0]["results"]


def main() -> int:
    if len(sys.argv) < 2:
        print(__doc__.strip().splitlines()[-1])
        return 2

    address = sys.argv[1]
    limit = int(sys.argv[2]) if len(sys.argv) > 2 else 5

    account = os.getenv("QODER_CF_ACCOUNT")
    database = os.getenv("QODER_D1_ID")
    if not account or not database:
        raise SystemExit("Set QODER_CF_ACCOUNT and QODER_D1_ID first.")

    rows = query(
        account,
        database,
        _wrangler_token(),
        "SELECT id, from_address, subject, body, received_at FROM messages "
        "WHERE inbox_address = ? ORDER BY received_at DESC LIMIT ?",
        [address, limit],
    )

    print(f"address : {address}")
    print(f"messages: {len(rows)}")
    if not rows:
        print("\nNothing arrived at this address — Qoder never delivered.")
        return 1

    client = TempikClient()
    for i, msg in enumerate(rows, 1):
        body = msg.get("body") or ""
        print(f"\n--- [{i}] {msg['received_at']} | {msg['from_address']} ---")
        print(f"    subject : {msg['subject']}")
        print(f"    body    : {len(body)} chars")
        print(f"    OTP     : {client.extract_otp([msg]) or '(not recognised)'}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
