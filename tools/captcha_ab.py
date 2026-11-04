"""Count slider attempts per account and per solving session.

Usage:
    python tools/captcha_ab.py <label> [start_line]

A "session" starts at `captcha attempt 1/N` and ends at the first solved. An
account can have more than one session: Qoder's OTP screen has no resend, so a
late mail forces the flow to go back and pass the slider again. Counting
sessions is the only fair way to compare two solvers -- otherwise whatever
happens in the mail step shows up as a slider failure.
"""

import re
import sys
import pathlib

LOG = pathlib.Path(__file__).resolve().parents[1] / "data" / "qoder.log"


def parse(start: int = 0):
    lines = LOG.read_text(encoding="utf-8", errors="replace").splitlines()[start:]
    accounts, sessions = [], []
    current = None
    session = None
    for line in lines:
        started = re.search(r"\[(\d+)\] Signup start: (\S+)", line)
        if started:
            current = {
                "idx": int(started.group(1)),
                "email": started.group(2),
                "tries": 0,
                "done": False,
            }
            accounts.append(current)
            continue
        if current is None:
            continue
        attempt = re.search(r"captcha attempt (\d+)/(\d+)", line)
        if attempt:
            number = int(attempt.group(1))
            if number == 1 or session is None or session["closed"]:
                session = {
                    "account": current["idx"],
                    "tries": 0,
                    "ok": None,
                    "verdicts": [],
                    "closed": False,
                }
                sessions.append(session)
            session["tries"] += 1
            current["tries"] += 1
            continue
        if session is None or session["closed"]:
            continue
        for code in re.findall(r"verdict=(\w+)", line):
            session["verdicts"].append(code)
        if "captcha solved" in line or re.search(r"solved!$", line.strip()):
            session["ok"] = True
            session["closed"] = True
        elif "solution rejected" in line or "miss, retrying" in line:
            session["ok"] = False
        if "Signup complete" in line:
            current["done"] = True
    # A session that never closed ended in the account giving up.
    for item in sessions:
        if not item["closed"]:
            item["closed"] = True
    return accounts, sessions


def main() -> int:
    label = sys.argv[1] if len(sys.argv) > 1 else "run"
    start = int(sys.argv[2]) if len(sys.argv) > 2 else 0
    accounts, sessions = parse(start)
    print(f"\n== {label} ==")
    print(f"{'acct':>4} {'session':>7} {'tries':>5}  {'result':<8} verdicts")
    for index, session in enumerate(sessions, 1):
        print(
            f"{session['account']:>4} {index:>7} {session['tries']:>5}  "
            f"{'solved' if session['ok'] else 'FAILED':<8} "
            f"{','.join(session['verdicts']) or '-'}"
        )
    total = len(sessions)
    first = sum(1 for s in sessions if s["ok"] and s["tries"] == 1)
    solved = sum(1 for s in sessions if s["ok"])
    tries = sum(s["tries"] for s in sessions)
    print(
        f"\n   sessions={total} solved={solved} failed={total - solved} "
        f"tries={tries} avg={tries / total:.2f}"
    )
    print(
        f"   first-try rate {first}/{total} = {first / total * 100:.0f}%   "
        f"accounts={len(accounts)} completed={sum(1 for a in accounts if a['done'])}"
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
