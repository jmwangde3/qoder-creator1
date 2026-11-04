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
from .tempmail import TempikClient, sent_at_stamp
from .proxy import ProxyPool
from .stealth import create_stealth_context, launch_stealth_browser
from .captcha import solve_slider_local, dump_widget_state
from .pat import PATManager

# Qoder's verification screen has no "resend" control. The only way to get a
# fresh code is to hit the back button, re-submit the password and pass the
# slider again. The very first send is sometimes lost entirely, so give the
# mail a limited window before falling back to that.
OTP_WAIT_SECONDS = 60
OTP_ROUNDS = 3  # initial send + 2 re-sends

# How long the OTP screen may take to accept or reject a code. It auto-submits
# once the sixth digit is typed, but the account is only marked verified a
# moment later: a real run asked for a PAT 0.7s after typing, got 401, and the
# welcome mail landed three seconds after that.
OTP_RESULT_SECONDS = 20

# Only a page the flow actually lands on after verifying counts as success.
OTP_SUCCESS_URL_HINTS = ("download", "dashboard", "welcome", "onboarding")

# The session can lag behind the verification by a few seconds, so a 401 from
# the PAT endpoint is retried rather than treated as final.
PAT_ATTEMPTS = 4
PAT_RETRY_SECONDS = 4

# Wording the verification screen uses when the code is wrong. A rejected code
# means the account is not verified, and creating a PAT then answers 401 while
# the run still reports success — so it has to be detected, not ignored.
OTP_ERROR_HINTS = (
    "invalid or incorrect",
    "verification code is invalid",
    "invalid verification code",
    "invalid code",
    "incorrect code",
    "code is incorrect",
    "code is invalid",
    "验证码无效",
    "验证码错误",
    "验证码不正确",
)

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


# One client for the whole process. It owns a keep-alive connection and a
# tempik session, so ten accounts cost one TLS handshake instead of ten — on a
# link that drops a third of its packets, that is the difference between a
# batch that finishes and one that stalls on the first request.
_shared_tempmail: Optional[TempikClient] = None


def tempmail_client() -> TempikClient:
    global _shared_tempmail
    if _shared_tempmail is None:
        _shared_tempmail = TempikClient()
    return _shared_tempmail


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

        # Step 1: Get temp email. This used to sit outside the try block, so a
        # DNS hiccup while creating the mailbox escaped create_account() and
        # killed the whole batch — a 10-account run died on #6 with nine still
        # to go.
        c.print(f"\n[bold cyan]Account #{idx}[/]")
        c.print("  :mailbox: Getting temp email...")
        tempmail = tempmail_client()
        try:
            email = tempmail.create_inbox()
        except Exception as e:
            c.print("  :x: [red]Could not get a temp mailbox[/]")
            write_log(f"[{idx}] Temp mailbox unavailable: {e}", "ERROR")
            return None

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
                # Submitting this step is what asks Qoder for a code, so the
                # clock starts here: anything received before now was issued
                # for an earlier request and is already dead.
                send_at = sent_at_stamp()
                if not await self._fill_password_step(page, password, idx):
                    c.print("  :x: [red]Password step would not advance![/]")
                    write_log(f"[{idx}] Password step stuck for {email}", "ERROR")
                    await self._snapshot(page, f"{idx}_password")
                    await context.close()
                    await browser.close()
                    return None

                # Step 6: Solve captcha
                c.print("  :jigsaw: Solving captcha...")
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
                    write_log(
                        f"[{idx}] Visible buttons: {await self._page_buttons(page)}",
                        "ERROR",
                    )
                    await self._snapshot(page, f"{idx}_captcha")
                    await context.close()
                    await browser.close()
                    return None

                c.print("  :white_check_mark: [green]Captcha solved![/]")
                await page.wait_for_timeout(2000)

                # Step 7: Wait for the code, going back to re-trigger one when
                # the mail does not turn up — and, when the page rejects a
                # code, retrying with a genuinely new one instead of carrying
                # on as if the account were verified.
                otp = None
                attempted = set()
                accepted = False

                for round_no in range(1, OTP_ROUNDS + 1):
                    c.print("  :inbox_tray: Waiting for OTP email...")
                    messages = await tempmail.wait_for_messages(
                        email,
                        max_wait=OTP_WAIT_SECONDS,
                        interval=5,
                        since=send_at,
                    )
                    otp = tempmail.extract_otp(messages) if messages else None

                    if otp and otp in attempted:
                        write_log(
                            f"[{idx}] Ignoring already-tried code {otp}", "WARNING"
                        )
                        otp = None

                    if otp:
                        attempted.add(otp)
                        c.print(f"  :incoming_envelope: OTP: [bold green]{otp}[/]")
                        outcome = await self._submit_otp(page, otp)
                        if outcome == "rejected":
                            c.print("  :x: [red]Code rejected by the page[/]")
                            write_log(
                                f"[{idx}] OTP {otp} rejected by the verification "
                                f"screen (round {round_no})",
                                "ERROR",
                            )
                            await self._snapshot(page, f"{idx}_otp_rejected")
                            otp = None
                        else:
                            if outcome == "unknown":
                                write_log(
                                    f"[{idx}] OTP outcome unknown — page neither "
                                    f"advanced nor showed an error (url={page.url})",
                                    "WARNING",
                                )
                            accepted = True
                            break

                    if round_no == OTP_ROUNDS:
                        break

                    c.print("  :arrows_counterclockwise: No usable code — going back to resend...")
                    write_log(
                        f"[{idx}] No usable OTP in round {round_no}, "
                        "retrying via back button",
                        "WARNING",
                    )
                    resumed_at = await self._resend_verification(page, password)
                    if not resumed_at:
                        write_log(f"[{idx}] Resend round {round_no} failed", "WARNING")
                        break
                    send_at = resumed_at

                if not accepted:
                    c.print("  :x: [red]No usable OTP — giving up![/]")
                    # Distinguish "Qoder never sent anything" from "sent but the
                    # code was unreadable": one means the account is lost to a
                    # server-side drop, the other is our problem.
                    try:
                        seen = await asyncio.to_thread(tempmail.get_messages, email)
                    except Exception:
                        seen = []
                    subjects = [str(m.get("subject", ""))[:40] for m in seen]
                    write_log(
                        f"[{idx}] OTP failed for {email}; inbox holds "
                        f"{len(seen)} message(s)"
                        + (f": {subjects}" if subjects else " — nothing was sent"),
                        "ERROR",
                    )
                    await self._snapshot(page, f"{idx}_otp")
                    await context.close()
                    await browser.close()
                    return None

                # Step 8: Report where the flow landed.
                current_url = page.url
                if "download" in current_url or "dashboard" in current_url:
                    c.print("  :white_check_mark: [bold green]Account created![/]")
                else:
                    c.print("  :warning: [yellow]Account may be pending...[/]")

                # Step 9: Create PAT. The session is marked verified a moment
                # after the code is accepted, so a 401 here usually means "not
                # yet" rather than "never" — retry it briefly.
                c.print("  :shield: Creating PAT...")
                await page.wait_for_timeout(1200)

                pat_response: Dict[str, Any] = {}
                for attempt in range(1, PAT_ATTEMPTS + 1):
                    pat_response = await PATManager.create(page, "farm")
                    if PATManager.is_valid(pat_response):
                        break
                    if pat_response.get("status") != 401 or attempt == PAT_ATTEMPTS:
                        break
                    write_log(
                        f"[{idx}] PAT answered 401 (attempt {attempt}/"
                        f"{PAT_ATTEMPTS}) — session not verified yet, retrying",
                        "WARNING",
                    )
                    await page.wait_for_timeout(PAT_RETRY_SECONDS * 1000)

                pat_token = PATManager.extract_token(pat_response)
                pat_valid = PATManager.is_valid(pat_response)

                if not pat_valid:
                    status = pat_response.get("status")
                    body = (pat_response.get("body") or "")[:200]
                    c.print(f"  :x: [red]PAT failed: {status}[/]")
                    write_log(
                        f"[{idx}] PAT creation failed for {email}: status={status} "
                        f"body={body}",
                        "ERROR",
                    )
                    await self._snapshot(page, f"{idx}_pat")
                    await context.close()
                    await browser.close()
                    return None

                c.print(f"  :white_check_mark: [green]PAT valid! ({len(pat_token)} chars)[/]")

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

    async def _resend_verification(self, page: Page, password: str) -> Optional[str]:
        """Trigger a fresh verification code.

        The OTP screen offers no resend button, only a back link that
        returns to the password step. Re-submitting the password re-sends
        the code, but it also means passing the slider captcha again — and it
        invalidates whatever code was issued before, which is why the caller
        gets the send timestamp back and ignores older mail.
        """
        send_at = sent_at_stamp()

        back = await self._click(page, BACK_SELECTORS)
        if not back:
            write_log("Back control not found on the OTP screen", "WARNING")
            return None
        write_log(f"Resend: clicked back via {back}", "INFO")

        # Make sure the click really landed on the password step — typing into
        # a page that never navigated back would burn a round for nothing.
        try:
            await page.wait_for_selector("#basic_password", timeout=10000)
        except Exception:
            write_log("Back did not lead to the password step", "WARNING")
            return None

        if not await self._fill_password_step(page, password):
            write_log("Resend: password step would not advance", "WARNING")
            return None

        await self._click(page, [
            "#aliyunCaptcha-captcha-body",
            'button:has-text("Click to verify")',
        ])
        await page.wait_for_timeout(3000)

        if not await solve_slider_local(page, max_attempts=5, console=self.console):
            return None

        write_log(f"Resend: fresh code requested at {send_at}", "INFO")
        return send_at

    async def _submit_otp(self, page: Page, otp: str) -> str:
        """Type the code and report what the page did with it.

        Returns "ok", "rejected" or "unknown". The live screen has no submit
        button — it posts the moment the sixth digit lands — while older
        builds do have one, so a click is attempted only when nothing has
        happened yet.
        """
        c = self.console
        c.print("  :keyboard: Filling OTP...")
        try:
            otp_inputs = await page.query_selector_all("input.ant-otp-input")
            if len(otp_inputs) >= 6:
                await otp_inputs[0].click()
                await page.wait_for_timeout(200)
                await page.keyboard.type(otp, delay=80)
            else:
                all_inputs = await page.query_selector_all('input:not([type="hidden"])')
                if all_inputs:
                    await all_inputs[0].click()
                    await page.keyboard.type(otp, delay=80)
                else:
                    await page.keyboard.type(otp, delay=80)
        except Exception as e:
            write_log(f"OTP typing failed: {e}", "ERROR")
            return "unknown"

        outcome = await self._otp_outcome(page, timeout=2)
        if outcome == "unknown":
            await self._click(page, [
                'button:has-text("Create account")',
                'button:has-text("Verify")',
                'button[type="submit"]',
            ])
            outcome = await self._otp_outcome(page, timeout=OTP_RESULT_SECONDS)
        return outcome

    async def _otp_outcome(self, page: Page, timeout: float = OTP_RESULT_SECONDS) -> str:
        """Watch the verification screen: "ok", "rejected" or "unknown".

        Success means the flow reached a page it only reaches after verifying.
        An earlier version also accepted "the URL is not the signup route",
        which fired on the very first poll of the live page — before the code
        had even been submitted — and sent the run to the PAT endpoint while
        the session was still anonymous.
        """
        start_url = page.url
        deadline = time.time() + timeout

        while True:
            try:
                url = page.url
                if any(hint in url.lower() for hint in OTP_SUCCESS_URL_HINTS):
                    return "ok"

                text = ((await page.inner_text("body")) or "").lower()
                if any(hint in text for hint in OTP_ERROR_HINTS):
                    return "rejected"

                # Moved somewhere else entirely and the code boxes are gone.
                if url != start_url and not await page.query_selector(
                    "input.ant-otp-input"
                ):
                    return "ok"
            except Exception:
                pass

            if time.time() >= deadline:
                return "unknown"
            await page.wait_for_timeout(600)

    async def _fill_password_step(self, page: Page, password: str, idx: int = 0) -> bool:
        """Fill the password field and confirm the step actually advanced.

        Observed failure: the Continue click never registered, the page stayed
        on /users/sign-up with #basic_password still mounted, and everything
        after it (captcha, OTP) waited for elements that could never appear.
        Checking here turns that into one clear error instead of a bogus
        captcha failure 80 seconds later.
        """
        for attempt in (1, 2):
            if not await page.query_selector("#basic_password"):
                return True  # already past this step

            filled = False
            try:
                await page.fill("#basic_password", password)
                filled = True
            except Exception:
                pass
            if not filled:
                try:
                    await page.click("#basic_password", force=True)
                    await page.keyboard.type(password, delay=20)
                except Exception:
                    pass

            clicked = await self._click(page, [
                'button:has-text("Continue")',
                'button:has-text("继续")',
                'button[type="submit"]',
            ])
            await page.wait_for_timeout(3500)

            if not await page.query_selector("#basic_password"):
                return True

            detail = await self._field_error(page, "#basic_password")
            write_log(
                f"[{idx}] Password step stuck after {clicked or 'no click'} "
                f"(attempt {attempt}); buttons={await self._page_buttons(page)}"
                + (f"; form says: {detail}" if detail else ""),
                "WARNING",
            )

            # Last resort: some forms submit on Enter.
            try:
                await page.keyboard.press("Enter")
                await page.wait_for_timeout(2500)
            except Exception:
                pass
            if not await page.query_selector("#basic_password"):
                return True

        return False

    @staticmethod
    async def _field_error(page: Page, selector: str) -> str:
        """The page's own words about a form field.

        Logged when a step refuses to advance: "Continue did nothing" is far
        less useful than the actual rule the field is complaining about.
        """
        try:
            return await page.evaluate(
                """(sel) => {
                    const el = document.querySelector(sel);
                    if (!el) return '';
                    const box = el.closest('form')
                        || (el.parentElement && el.parentElement.parentElement)
                        || el.parentElement;
                    if (!box) return '';
                    return (box.innerText || '').replace(/\\s+/g, ' ').trim().slice(0, 240);
                }""",
                selector,
            )
        except Exception:
            return ""

    @staticmethod
    async def _page_buttons(page: Page) -> list:
        """Visible button labels — logged when a step refuses to advance.

        Disabled controls are marked, because a greyed-out Continue is the
        usual reason a click does nothing.
        """
        try:
            return await page.evaluate(
                """() => Array.from(
                        document.querySelectorAll('button, [role=button], a'))
                    .filter(e => e.offsetParent !== null)
                    .map(e => {
                        const t = (e.innerText || e.getAttribute('aria-label') || '')
                            .trim().replace(/\\s+/g, ' ').slice(0, 40);
                        const off = e.disabled
                            || e.getAttribute('aria-disabled') === 'true'
                            || e.classList.contains('disabled')
                            || e.classList.contains('ant-btn-disabled');
                        return t ? (off ? t + ' [disabled]' : t) : '';
                    })
                    .filter(t => t)
                    .slice(0, 15)"""
            )
        except Exception:
            return []

    @staticmethod
    async def _snapshot(page: Page, tag: str) -> None:
        """Save a screenshot of the stuck page next to the results file."""
        try:
            path = ACCOUNTS_FILE.parent / f"failure_{tag}.png"
            await page.screenshot(path=str(path))
            write_log(f"Screenshot saved: {path}", "INFO")
        except Exception as e:
            write_log(f"Screenshot failed: {e}", "WARNING")

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
        try:
            result = await manager.create_account(i + 1)
        except Exception as e:  # keep going: one bad account is not fatal
            write_log(f"[{i + 1}] Unhandled signup error: {e}", "ERROR")
            result = None
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