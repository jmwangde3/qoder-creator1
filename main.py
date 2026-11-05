"""
Qoder Creator - CLI Entry Point
Automated Qoder account creation.
Flow: tempik (temp mail) -> signup -> captcha -> OTP -> PAT

Usage:
  python main.py              # Interactive menu
  python main.py signup -n 5  # Create 5 accounts
  python main.py signup -n 5 --headed   # Visible browser + human fallback
  python main.py doctor       # Check the environment before a batch
  python main.py view         # View results
"""

import asyncio
import os
import sys

# Force UTF-8 on Windows to avoid UnicodeEncodeError with Rich
if sys.platform == "win32":
    os.environ.setdefault("PYTHONIOENCODING", "utf-8")

from rich import box
from rich.console import Console
from rich.panel import Panel
from rich.progress import Progress, SpinnerColumn, TextColumn, BarColumn
from rich.prompt import IntPrompt, Confirm
from rich.table import Table
from rich.text import Text

from src.config import (
    DATA_DIR, ACCOUNTS_FILE, PROXY_FILE, LOG_FILE, PROXY_MODE, HEADLESS,
    MANUAL_FALLBACK, MANUAL_TIMEOUT, TEMPIK_BASE,
)
from src.utils import setup_logging, load_jsonl, write_log
from src.proxy import ProxyPool
from src.signup import create_accounts, SignupManager

console = Console(force_terminal=True)


def banner():
    """Render professional banner."""
    title = Text("QODER CREATOR", style="bold cyan")
    subtitle = Text("Temp Mail -> Signup -> Captcha -> OTP -> PAT", style="dim")
    note = Text("No Google OAuth. No Qoder Desktop. Just Python.", style="italic bright_black")
    body = Text.assemble(title, "\n", subtitle, "\n\n", note)
    panel = Panel(body, box=box.HEAVY, border_style="cyan")
    console.print(panel)


def check_prerequisites() -> bool:
    """Check prerequisites, return True if ok."""
    if PROXY_MODE != "none" and not PROXY_FILE.exists():
        console.print("[!] [yellow]No proxies.txt found![/] Create one or set proxy mode = \"none\".", style="yellow")
        return False
    return True


async def menu_signup():
    """Interactive signup with rich UI."""
    console.print()
    console.print(Panel("CREATE ACCOUNTS", style="bold cyan", box=box.HEAVY))

    count = IntPrompt.ask("How many accounts?", default=1)
    headless = Confirm.ask("Headless mode?", default=True)

    proxy_pool = None
    if PROXY_FILE.exists():
        proxy_pool = ProxyPool()
        proxy_pool.load(str(PROXY_FILE))
        console.print(f"  [*] Loaded [cyan]{proxy_pool.count}[/] proxies")

    manager = SignupManager(proxy_pool, headless, console=console)

    with Progress(
        SpinnerColumn(),
        TextColumn("[progress.description]{task.description}"),
        BarColumn(),
        TextColumn("[cyan]{task.completed}/{task.total}[/]"),
        console=console,
    ) as progress:
        task = progress.add_task("[cyan]Creating accounts...", total=count)

        results = []
        for i in range(count):
            # One failing account must not end the batch: a DNS hiccup while
            # creating the mailbox killed a 10-account run on #6 before this.
            try:
                result = await manager.create_account(i + 1)
            except Exception as e:
                write_log(f"[{i + 1}] Unhandled signup error: {e}", "ERROR")
                result = None
            if result:
                results.append(result)
            progress.advance(task)

    ok = sum(1 for r in results if r and r.get("pat_valid"))
    console.print()
    if ok == count:
        console.print(Panel(f"[OK] All {count}/{count} accounts created successfully!", style="green"))
    elif ok > 0:
        console.print(Panel(f"[!] {ok}/{count} accounts created ({count - ok} failed)", style="yellow"))
    else:
        console.print(Panel("[FAIL] All accounts failed!", style="red"))


async def menu_signup_cli(count: int, headless: bool = None):
    """Non-interactive CLI signup (rich)."""
    if headless is None:
        headless = HEADLESS
    console.print()
    console.print(Panel(f"CREATING [cyan]{count}[/] ACCOUNT(S)", style="bold cyan", box=box.HEAVY))
    if not headless and MANUAL_FALLBACK:
        console.print(
            f"  [yellow]Visible browser:[/] if the slider fails {MANUAL_TIMEOUT:.0f}s "
            f"of human time is offered, then the batch continues automatically."
        )

    proxy_pool = None
    if PROXY_FILE.exists():
        proxy_pool = ProxyPool()
        proxy_pool.load(str(PROXY_FILE))
        console.print(f"  [*] Loaded [cyan]{proxy_pool.count}[/] proxies")

    with Progress(
        SpinnerColumn(),
        TextColumn("[progress.description]{task.description}"),
        BarColumn(),
        TextColumn("[cyan]{task.completed}/{task.total}[/]"),
        console=console,
    ) as progress:
        task = progress.add_task("[cyan]Creating accounts...", total=count)
        manager = SignupManager(proxy_pool, headless=headless, console=console)

        results = []
        for i in range(count):
            # One failing account must not end the batch: a DNS hiccup while
            # creating the mailbox killed a 10-account run on #6 before this.
            try:
                result = await manager.create_account(i + 1)
            except Exception as e:
                write_log(f"[{i + 1}] Unhandled signup error: {e}", "ERROR")
                result = None
            if result:
                results.append(result)
            progress.advance(task)

    ok = sum(1 for r in results if r and r.get("pat_valid"))
    console.print()
    if ok == count:
        console.print(Panel(f"[OK] {ok}/{count} accounts created!", style="green"))
    elif ok > 0:
        console.print(Panel(f"[!] {ok}/{count} accounts created ({count - ok} failed)", style="yellow"))
    else:
        console.print(Panel("[FAIL] All failed!", style="red"))


def menu_view_results():
    """View saved results as rich table."""
    console.print()
    console.print(Panel("RESULTS", style="bold cyan", box=box.HEAVY))

    if not ACCOUNTS_FILE.exists():
        console.print("[yellow]No accounts yet. Run signup first![/]")
        return

    accounts = load_jsonl(ACCOUNTS_FILE)
    if not accounts:
        console.print("[yellow]No accounts yet. Run signup first![/]")
        return

    table = Table(title=f"Accounts ({len(accounts)})", box=box.ROUNDED)
    table.add_column("#", style="dim", width=4)
    table.add_column("Email", style="cyan")
    table.add_column("Password", style="yellow")
    table.add_column("PAT", style="green")
    table.add_column("Status", justify="center")

    for i, a in enumerate(accounts[-20:], 1):
        pat = a.get("pat_token", "")
        pat_short = f"{pat[:25]}..." if len(pat) > 25 else pat
        status = "[green]OK[/]" if a.get("pat_valid") else "[red]FAIL[/]"
        table.add_row(
            str(i),
            a.get("email", "?"),
            a.get("password", "?"),
            pat_short,
            status,
        )

    console.print(table)
    console.print(f"\n[dim]Data file: {ACCOUNTS_FILE}[/]")


def menu_view_log():
    """View last log entries."""
    console.print()
    console.print(Panel("RECENT LOGS", style="bold cyan", box=box.HEAVY))

    if not LOG_FILE.exists():
        console.print("[yellow]No log file yet.[/]")
        return

    lines = LOG_FILE.read_text(encoding="utf-8").splitlines()
    for line in lines[-25:]:
        if "ERROR" in line:
            console.print(f"[red]{line}[/]")
        elif "SUCCESS" in line:
            console.print(f"[green]{line}[/]")
        elif "WARNING" in line:
            console.print(f"[yellow]{line}[/]")
        else:
            console.print(f"[dim]{line}[/]")


async def interactive_menu():
    """Main interactive menu with rich."""
    setup_logging()
    console.clear()
    banner()
    check_prerequisites()

    menu_table = Table(show_header=False, box=box.SIMPLE, padding=(0, 2))
    menu_table.add_column(style="cyan", justify="right")
    menu_table.add_column(style="white")
    menu_table.add_row("[cyan]1.[/]", "Create Accounts")
    menu_table.add_row("[cyan]2.[/]", "View Results")
    menu_table.add_row("[cyan]3.[/]", "View Log")
    menu_table.add_row("[cyan]0.[/]", "Exit")

    menu_panel = Panel(menu_table, title="MENU", border_style="cyan", box=box.HEAVY)

    while True:
        console.print()
        console.print(menu_panel)

        choice = console.input("[cyan]Choose (0-3):[/] ").strip()

        if choice == "0":
            console.print("\n[green]Goodbye![/]")
            break
        elif choice == "1":
            await menu_signup()
        elif choice == "2":
            menu_view_results()
        elif choice == "3":
            menu_view_log()
        else:
            console.print("[red]Invalid choice![/]")


# ================= PREFLIGHT =================
def doctor():
    """Catch the environment traps before a long unattended batch.

    The two that actually bit us: the default `python` on this machine is a
    WorkBuddy 3.13 without playwright, and the temp-mail host needs an apex
    domain (the *. subdomain is NXDOMAIN). Both are cheap to check up front.
    """
    import importlib.util
    import urllib.error
    import urllib.request
    from pathlib import Path

    state = {"ok": 0, "warn": 0, "fail": 0}

    def ok(msg):
        state["ok"] += 1
        console.print(f"  [green]OK[/]   {msg}")

    def warn(msg):
        state["warn"] += 1
        console.print(f"  [yellow]WARN[/] {msg}")

    def fail(msg):
        state["fail"] += 1
        console.print(f"  [red]FAIL[/] {msg}")

    console.print()
    console.print(Panel("ENVIRONMENT CHECK", style="bold cyan"))

    ver = sys.version_info
    if ver >= (3, 10):
        ok(f"Python {ver.major}.{ver.minor}.{ver.micro}")
    else:
        fail(f"Python {ver.major}.{ver.minor}.{ver.micro} — need >= 3.10")

    if importlib.util.find_spec("playwright"):
        ok("playwright installed in this interpreter")
    else:
        fail("playwright missing — use ./venv/Scripts/python.exe (the default python is a different install)")

    try:
        from playwright.sync_api import sync_playwright

        with sync_playwright() as p:
            binary = Path(p.chromium.executable_path)
        if binary.exists():
            ok("Chromium binary present")
        else:
            fail("Chromium binary missing — python -m playwright install chromium")
    except Exception as exc:
        warn(f"could not verify the Chromium binary: {type(exc).__name__}")

    ok(
        f"headless={HEADLESS}, manual_fallback={MANUAL_FALLBACK} "
        f"({MANUAL_TIMEOUT:.0f}s cap)"
    )
    if not HEADLESS and MANUAL_FALLBACK:
        warn("visible browser: a failed slider will pause for up to "
             f"{MANUAL_TIMEOUT:.0f}s per account")

    try:
        probe = TEMPIK_BASE.rstrip("/") + "/api/config"
        with urllib.request.urlopen(probe, timeout=6) as resp:
            ok(f"temp-mail API reachable (HTTP {resp.status})")
    except urllib.error.HTTPError as exc:
        # The host answered; this particular probe endpoint may need a
        # session. That is a different problem from DNS/proxy being down.
        warn(f"temp-mail host answered HTTP {exc.code} on /api/config")
    except Exception as exc:
        warn(f"temp-mail API not reachable from here: {type(exc).__name__} "
             f"(proxy/DNS or worker down)")

    if DATA_DIR.is_dir() and os.access(DATA_DIR, os.W_OK):
        ok(f"data dir writable: {DATA_DIR}")
    else:
        fail(f"data dir not writable: {DATA_DIR}")

    console.print()
    console.print(
        f"  [green]{state['ok']} ok[/]  [yellow]{state['warn']} warn[/]  "
        f"[red]{state['fail']} fail[/]"
    )
    if state["fail"]:
        console.print("  [red]Fix the failures before starting a batch.[/]")
    console.print()
    return 1 if state["fail"] else 0


# ================= CLI =================
def main():
    """CLI entry point."""
    args = sys.argv[1:]

    if not args:
        asyncio.run(interactive_menu())
        return

    setup_logging()

    command = args[0].lower()
    n = 1
    for i, arg in enumerate(args):
        if arg == "-n" and i + 1 < len(args):
            n = int(args[i + 1])
    headed = "--headed" in args

    if command == "signup":
        asyncio.run(menu_signup_cli(n, headless=False if headed else None))
    elif command == "doctor":
        sys.exit(doctor())
    elif command == "view":
        menu_view_results()
    else:
        console.print(f"[red]Unknown command:[/] {command}")
        console.print(Text("Usage: python main.py [signup|view|doctor] [-n N] [--headed]", style="dim"))


if __name__ == "__main__":
    main()