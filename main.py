"""
Qoder Creator - CLI Entry Point
Automated Qoder account creation.
Flow: tempik (temp mail) -> signup -> captcha -> OTP -> PAT

Usage:
  python main.py              # Interactive menu
  python main.py signup -n 5  # Create 5 accounts
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


async def menu_signup_cli(count: int):
    """Non-interactive CLI signup (rich)."""
    console.print()
    console.print(Panel(f"CREATING [cyan]{count}[/] ACCOUNT(S)", style="bold cyan", box=box.HEAVY))

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
        manager = SignupManager(proxy_pool, headless=HEADLESS, console=console)

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

    if command == "signup":
        asyncio.run(menu_signup_cli(n))
    elif command == "view":
        menu_view_results()
    else:
        console.print(f"[red]Unknown command:[/] {command}")
        console.print(Text("Usage: python main.py [signup|view] [-n N]", style="dim"))


if __name__ == "__main__":
    main()