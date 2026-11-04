"""
Qoder Creator - Signup Manager
Full signup flow: temp mail → form → captcha (local slider) → OTP → account created → PAT.
"""

import asyncio
import random
import time
from typing import Optional, Dict, Any

from playwright.async_api import async_playwright, Page
from rich.console import Console
from rich.text import Text

from .config import QODER_BASE, ACCOUNTS_FILE
from .utils import write_log, generate_password, save_jsonl
from .tempmail import TempikClient
from .proxy import ProxyPool
from .stealth import create_stealth_context, launch_stealth_browser
from .captcha import solve_slider_local, dump_widget_state
from .pat import PATManager

# Qoder's verification screen has no "resend" control. The only way to get a
# fresh code is to hit the back button, re-submit the password and pass the
# slider again. The very first send is sometimes lost entirely, so give the
# mail a limited window before falling back to that.
OTP_WAIT_SECONDS = 45
OTP_ROUNDS = 3  # initial send + 2 re-sends

# The OTP screen's back control is a plain text link, not a <button> — the
# tag differs between locales/releases, so match on text and let Playwright
# pick the innermost matching element.
BACK_SELECTORS = [
    'text=go back',
    'text="Go back"',
    'button:has-text("Go back")',
    'a:has-text("Go back")',
    '[role="button"]:has-text("Go back")',
    'button:has-text("返回")',
    'a:has-text("返回")',
    'text=返回',
]


class SignupManager:
    """Handle Qoder signup + PAT creation."""

    def __init__(
        self,
        proxy_pool: ProxyPool = None,
        headless: bool = True,
        console: Console = None,
    ):
        self.proxy_pool = proxy_pool
        self.headless = headless
        self.console = console or Console()

    async def create_account(
        self,
        idx: int = 0,
        proxy: Dict[str, str] = None,
    ) -> Optional[Dict[str, Any]]:
        """
        Full signup flow for one account.
        Returns: {email, password, pat, ...} or None on failure.
        """
        if proxy is None and self.proxy_pool:
            proxy = self.proxy_pool.next()

        c = self.console

        # Step 1: Get temp email
        c.print(f"\n[bold cyan]Account #{idx}[/]")
        c.print("  :mailbox: Getting temp email...")
        tempmail = TempikClient()
        email = tempmail.create_inbox()
        password = generate_password()

        c.print(f"  :envelope: Email: [cyan]{email}[/]")
        c.print(f"  :key: Password: [yellow]{password}[/]")
        write_log(f"[{idx}] Signup start: {email}", "INFO")

        try:
            async with async_playwright() as p:
                # Step 2: Launch browser
                c.print("  :rocket: Launching browser...")
                browser = await launch_stealth_browser(p, proxy, self.headless)
                context = await create_stealth_context(browser, proxy)
                page = await context.new_page()

                # Step 3: Open signup page
                c.print("  :globe_with_meridians: Opening signup page...")
                await page.goto(
                    f"{QODER_BASE}/users/sign-up",
                    wait_until="domcontentloaded",
                    timeout=60000,
                )
                await page.wait_for_timeout(5000)

                # Step 4: Fill form (name + email)
                c.print("  :pencil: Filling signup form...")
                try:
                    await page.fill("#basic_firstName", "User")
                    await page.fill("#basic_lastName", "Dev")
                    await page.fill("#basic_email", email)
                except Exception as e:
                    write_log(f"[{idx}] Form fill error: {e}", "ERROR")
                    c.print(f"  :x: [red]Form fill failed: {e}[/]")
                    await context.close()
                    await browser.close()
                    return None

                # Checkbox
                cb = await page.query_selector("input[type=checkbox]")
                if cb:
                    try:
                        await cb.check(force=True)
                    except Exception:
                        pass

                # Click Continue
                await self._click(page, ['button:has-text("Continue")'])
                await page.wait_for_timeout(4000)

                # Step 5: Fill password
                c.print("  :lock: Filling password...")
                pw = await page.query_selector("#basic_password")
                if pw:
                    try:
                        await pw.click(force=True)
                        await page.keyboard.type(password, delay=20)
                    except Exception:
                        pass
                await self._click(page, ['button:has-text("Continue")'])
                await page.wait_for_timeout(4000)

                # Step 6: Solve captcha
                c.print("  :puzzle_piece: Solving captcha...")
                await self._click(page, [
                    "#aliyunCaptcha-captcha-body",
                    "#aliyunCaptcha-captcha-left",
                    'button:has-text("Click to verify")',
                    'text=Click to verify',
                    'button:has-text("点击验证")',
                    'button:has-text("点击此处验证")',
                ])
                await page.wait_for_timeout(3000)

                solved = await solve_slider_local(page, max_attempts=5, console=c)
                if not solved:
                    c.print("  :x: [red]Captcha failed![/]")
                    write_log(f"[{idx}] Captcha failed for {email}", "ERROR")
                    write_log(
                        f"[{idx}] Captcha state: {await dump_widget_state(page)}",
                        "ERROR",
                    )
                    await context.close()
                    await browser.close()
                    return None

                c.print("  :white_check_mark: [green]Captcha solved![/]")
                await page.wait_for_timeout(2000)

                # Step 7: Wait for OTP, going back to re-trigger a fresh code
                # when the mail does not turn up in time.
                otp = None
                for round_no in range(1, OTP_ROUNDS + 1):
                    c.print("  :inbox_tray: Waiting for OTP email...")
                    messages = await tempmail.wait_for_messages(
                        email, max_wait=OTP_WAIT_SECONDS, interval=5
                    )
                    otp = tempmail.extract_otp(messages) if messages else None
                    if otp:
                        break
                    if round_no == OTP_ROUNDS:
                        break

                    c.print("  :arrows_counterclockwise: No code yet — going back to resend...")
                    write_log(
                        f"[{idx}] No OTP in round {round_no}, retrying via back button",
                        "WARNING",
                    )
                    if not await self._resend_verification(page, password):
                        write_log(f"[{idx}] Resend round {round_no} failed", "WARNING")
                        break

                if not otp:
                    c.print("  :x: [red]No OTP received — giving up![/]")
                    write_log(f"[{idx}] OTP timeout for {email}", "ERROR")
                    await context.close()
                    await browser.close()
                    return None

                c.print(f"  :incoming_envelope: OTP: [bold green]{otp}[/]")

                # Step 8: Fill OTP
                c.print("  :keyboard: Filling OTP...")
                otp_inputs = await page.query_selector_all('input.ant-otp-input')
                if len(otp_inputs) >= 6:
                    await otp_inputs[0].click()
                    await page.wait_for_timeout(200)
                    await page.keyboard.type(otp, delay=80)
                    await page.wait_for_timeout(1500)
                else:
                    all_inputs = await page.query_selector_all('input:not([type="hidden"])')
                    if all_inputs:
                        await all_inputs[0].click()
                        await page.keyboard.type(otp, delay=80)
                    else:
                        await page.keyboard.type(otp, delay=80)

                await self._click(page, [
                    'button:has-text("Create account")',
                    'button:has-text("Verify")',
                    'button[type="submit"]',
                ])
                await page.wait_for_timeout(8000)

                # Step 9: Check result
                current_url = page.url
                if "download" in current_url or "dashboard" in current_url:
                    c.print("  :white_check_mark: [bold green]Account created![/]")
                else:
                    c.print("  :warning: [yellow]Account may be pending...[/]")

                # Step 10: Create PAT
                c.print("  :shield: Creating PAT...")
                pat_response = await PATManager.create(page, "farm")
                pat_token = PATManager.extract_token(pat_response)
                pat_valid = PATManager.is_valid(pat_response)

                if pat_valid:
                    c.print(f"  :white_check_mark: [green]PAT valid! ({len(pat_token)} chars)[/]")
                else:
                    c.print(f"  :x: [yellow]PAT invalid: {pat_response.get('status')}[/]")

                await context.close()
                await browser.close()

                # Step 11: Build result
                result = {
                    "email": email,
                    "password": password,
                    "pat_token": pat_token,
                    "pat_valid": pat_valid,
                    "pat_response": pat_response,
                    "url": current_url,
                    "created_at": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
                }

                save_jsonl(ACCOUNTS_FILE, result)
                write_log(f"[{idx}] Signup complete: {email} (PAT valid={pat_valid})", "SUCCESS")

                return result

        except Exception as e:
            write_log(f"[{idx}] Signup error: {e}", "ERROR")
            c.print(f"  :x: [red]Error: {e}[/]")
            import traceback
            traceback.print_exc()
            return None

    async def _resend_verification(self, page: Page, password: str) -> bool:
        """Trigger a fresh verification code.

        The OTP screen offers no resend button, only a back link that
        returns to the password step. Re-submitting the password re-sends
        the code, but it also means passing the slider captcha again.
        """
        back = await self._click(page, BACK_SELECTORS)
        if not back:
            write_log("Back control not found on the OTP screen", "WARNING")
            return False
        write_log(f"Resend: clicked back via {back}", "INFO")

        # Make sure the click really landed on the password step — typing into
        # a page that never navigated back would burn a round for nothing.
        try:
            await page.wait_for_selector("#basic_password", timeout=10000)
        except Exception:
            write_log("Back did not lead to the password step", "WARNING")
            return False

        pw = await page.query_selector("#basic_password")
        if pw:
            try:
                await pw.click(force=True)
                await page.keyboard.type(password, delay=20)
            except Exception:
                pass

        await self._click(page, ['button:has-text("Continue")'])
        await page.wait_for_timeout(4000)

        await self._click(page, [
            "#aliyunCaptcha-captcha-body",
            'button:has-text("Click to verify")',
        ])
        await page.wait_for_timeout(3000)

        return await solve_slider_local(page, max_attempts=5, console=self.console)

    @staticmethod
    async def _click(page: Page, selectors: list) -> Optional[str]:
        """Click the first *visible* match for each selector, in order.

        query_selector() only ever returns the first node in DOM order, so a
        wizard that keeps previous steps around (hidden) would make the click
        stick to an invisible button and silently time out.
        """
        for sel in selectors:
            try:
                elements = await page.query_selector_all(sel)
            except Exception:
                continue
            for el in elements:
                try:
                    if await el.is_visible():
                        await el.click()
                        return sel
                except Exception:
                    continue
        return None


async def create_accounts(
    count: int = 1,
    proxy_pool: ProxyPool = None,
    headless: bool = True,
    delay: int = 5,
    console: Console = None,
) -> list:
    """Create multiple accounts (legacy — use SignupManager directly)."""
    manager = SignupManager(proxy_pool, headless, console)
    results = []

    for i in range(count):
        result = await manager.create_account(i + 1)
        if result:
            results.append(result)

        if i < count - 1:
            wait = delay + random.randint(0, 5)
            c = console or Console()
            c.print(f"  :hourglass: Waiting [yellow]{wait}s[/] before next account...")
            await asyncio.sleep(wait)

    ok = [r for r in results if r and r.get("pat_valid")]
    c = console or Console()
    c.print()
    if ok:
        c.print(f"[green]:tada: Done: {len(ok)}/{count} accounts with valid PAT[/]")
    else:
        c.print(f"[red]:x: All {count} accounts failed[/]")
    c.print(f"[dim]Saved to: {ACCOUNTS_FILE}[/]")

    return results