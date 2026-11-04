# Qoder Creator

> Automated Qoder account factory — temp mail, signup, captcha, OTP, PAT. One command.

<p align="center">
  <br>
  <strong>Temp Mail</strong> → <strong>Signup</strong> → <strong>Captcha</strong> → <strong>OTP</strong> → <strong>PAT</strong>
  <br><br>
  <em>No Google OAuth. No Qoder Desktop. Just Python.</em>
</p>

---

## Features

- **Fully automated** — fill form, solve captcha, read OTP, create PAT, all hands-free
- **Local captcha solver** — slider captcha solved with image processing, no external API needed
- **Rotating proxies** — SOCKS5/HTTP proxy pool with round-robin (`ip:port:user:pass`)
- **Batch creation** — `python main.py signup -n 10` and walk away
- **Append-safe** — every account appended to `accounts.jsonl`, never overwrites
- **Temp mail** — powered by [tempik](https://github.com/hirotomasato/tempik), deployable on Cloudflare Workers

## Quick Start

```bash
# 1. Clone & venv
git clone https://github.com/hirotomasato/qoder-creator.git
cd qoder-creator
python -m venv venv
venv\Scripts\activate       # Windows
# source venv/bin/activate  # Linux / macOS

# 2. Install
pip install -r requirements.txt
playwright install chromium

# 3. Configure
cp config.example.toml config.toml
# Edit config.toml — set proxy mode, API keys, etc.

# 4. Create accounts
python main.py signup -n 3
```

## Configuration

Copy the example and edit it:

```bash
cp config.example.toml config.toml
```

Everything lives in [config.example.toml](config.example.toml):

| Section | Key | What |
|---------|-----|------|
| `api` | `tempmail_base` | Tempik instance URL |
| `signup` | `headless` | `true` = background, `false` = show browser |
| `signup` | `delay` | Seconds between accounts (+ random 0–5s) |
| `proxy` | `mode` | `"none"` / `"file"` / `"env"` |
| `proxy` | `pool_file` | Path to proxy list (default: `proxies.txt`) |

### Proxy format

```
ip:port:user:pass
```

One per line. See [proxies.txt](proxies.txt) for example.

## Usage

```bash
python main.py              # Interactive menu
python main.py signup -n 5  # Create 5 accounts
python main.py view         # Show saved accounts
```

## Project Structure

```
qoder-creator/
├── src/
│   ├── config.py       # Config loader, constants, colors
│   ├── signup.py       # Full signup flow orchestrator
│   ├── captcha.py      # Local slider captcha solver
│   ├── tempmail.py     # Tempik API client (session, inbox, OTP)
│   ├── pat.py          # PAT creation via browser session
│   ├── stealth.py      # Playwright stealth & fingerprint evasion
│   ├── proxy.py        # Rotating proxy pool
│   └── utils.py        # Logging, JSONL save/load, generators
├── tests/              # Unit tests — no network needed
├── tools/
│   └── inbox_trace.py  # Read an inbox straight from D1 for debugging
├── main.py             # CLI entry point
├── mock_qoder.py       # Local fake signup site (port 8799)
├── smoke_flow.py       # Offline end-to-end run against the mock
├── config.toml         # All settings in one file
├── proxies.txt         # Proxy list (ip:port:user:pass)
└── data/
    └── accounts.jsonl  # Output — one JSON object per line
```

## How It Works

1. **Temp mail** — creates inbox via tempik, gets a disposable email
2. **Browser** — Playwright Chromium with stealth fingerprint evasion
3. **Signup** — fills name, email, password on `qoder.com/users/sign-up`
4. **Captcha** — local image processing slides the puzzle piece into place (5 attempts)
5. **OTP** — polls tempik inbox for the verification email, extracts 6-digit code
6. **PAT** — creates a personal access token via browser's authenticated session

Each successful account is appended to `data/accounts.jsonl`:

```json
{"email": "user@example.com", "password": "...", "pat_token": "pt-...", "pat_valid": true, "created_at": "2026-08-16T..."}
```

## Temp Mail

This project uses [**tempik**](https://github.com/hirotomasato/tempik) — deployed on your Cloudflare Workers — a lightweight disposable email API written in Node.js. Deploy on your own Cloudflare Workers — see the [tempik repo](https://github.com/hirotomasato/tempik) for setup instructions.

Set your Cloudflare Workers URL in `config.toml` → `tempmail_base`.

## Claim / Trial

**Claim flow is not included in this project.** Qoder's trial claim requires device-level authentication via the Qoder CLI binary (`qodercli-wake` on Linux) or Qoder Desktop app (Windows). It cannot be done through the web API alone.

If you need claim, you'll need to build it yourself — the signup flow here gives you everything you need to start (email, password, PAT).

## Requirements

- Python 3.11+
- Playwright Chromium
- `rich` — beautiful terminal UI
- Proxy list (optional — set `mode = "none"` in config to skip)

## Development

```bash
python -m unittest discover -s tests   # 29 unit tests, no network
python smoke_flow.py                   # end-to-end against the local mock
```

`smoke_flow.py` starts `mock_qoder.py` on port 8799, stubs the captcha solver,
creates a real tempik inbox and drives the actual signup code — it never
touches qoder.com.

See [HANDOFF.md](HANDOFF.md) for the environment pitfalls on this network, the
invariants to preserve when editing, and the known failure modes.

## License

MIT
