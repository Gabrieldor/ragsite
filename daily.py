"""
RO Daily Tasks — ro.gnjoylatam.com

Options:
  1. Check-in (roulette event button)
  2. Dado do Dia — click "CLIQUE!" on midgardtrail, pausing after the click for your input
  3. Daily Login — click "RECEBER ITEM" on dailylogin
  4. All of the above (in order: 1 → 2 → 3), no pause
  5. All of the above, pausing after dado click (wait for your input to continue)
"""

import io
import json
import asyncio
import sys
from pathlib import Path
from playwright.async_api import async_playwright, TimeoutError as PlaywrightTimeout

# Ensure UTF-8 output on Windows
if sys.stdout.encoding != "utf-8":
    sys.stdout = io.TextIOWrapper(sys.stdout.buffer, encoding="utf-8", errors="replace")
    sys.stderr = io.TextIOWrapper(sys.stderr.buffer, encoding="utf-8", errors="replace")

TIMEOUT = 20_000  # ms

_sys = sys
_BASE = Path(_sys.executable).parent if getattr(_sys, "frozen", False) else Path(__file__).parent
ACCOUNTS_FILE = _BASE / "accounts.json"

URL_MIDGARDTRAIL = "https://ro.gnjoylatam.com/pt/event/anniversary_1st/midgardtrail"
URL_DAILYLOGIN   = "https://ro.gnjoylatam.com/pt/event/anniversary_1st/dailylogin"

GREEN  = "\033[92m"
RED    = "\033[91m"
YELLOW = "\033[93m"
CYAN   = "\033[96m"
RESET  = "\033[0m"


def log(msg: str, color: str = RESET):
    print(f"{color}{msg}{RESET}", flush=True)


def load_config() -> tuple[str, list[dict]]:
    with open(ACCOUNTS_FILE, encoding="utf-8") as f:
        data = json.load(f)
    return data["checkin_url"], data["accounts"]


def ask_option() -> int:
    log("\n=== RO Daily Tasks ===", CYAN)
    log("1. Check-in (roulette event)", YELLOW)
    log("2. Dado do Dia — click 'CLIQUE!' (midgardtrail)", YELLOW)
    log("3. Daily Login — click 'RECEBER ITEM' (dailylogin)", YELLOW)
    log("4. All of the above", YELLOW)
    log("5. All of the above, pausing after dado click (wait for your input to continue)", YELLOW)
    while True:
        choice = input("\nChoose an option (1/2/3/4/5): ").strip()
        if choice in ("1", "2", "3", "4", "5"):
            return int(choice)
        log("Invalid choice. Please enter 1, 2, 3, 4, or 5.", RED)


# ── shared helpers ────────────────────────────────────────────────────────────

async def dismiss_cookie_banner(page):
    try:
        btn = page.locator("button.cookieprivacy_btn__Pqz8U, button:has-text('concordo')").first
        if await btn.is_visible(timeout=3_000):
            await btn.click()
            await page.wait_for_timeout(500)
            log("  Cookie banner dismissed.", YELLOW)
    except PlaywrightTimeout:
        pass


async def needs_login(page) -> bool:
    login_btn = page.locator(
        "button:has-text('Login'), "
        "button:has-text('Sign in'), "
        "button:has-text('Entrar'), "
        "a:has-text('Login'), "
        "a:has-text('Entrar'), "
        "button[aria-label='Login'], "
        "button[aria-label='Entrar']"
    ).first
    try:
        return await login_btn.is_visible(timeout=3_000)
    except PlaywrightTimeout:
        return False


async def do_login(page, account: dict, redirect_url: str):
    email = account["email"]
    log(f"[{email}] Navigating to login page...")
    login_url = f"https://login.gnjoylatam.com?redirectUrl={redirect_url}"
    await page.goto(login_url, wait_until="domcontentloaded", timeout=TIMEOUT)
    await page.wait_for_timeout(2_000)

    log(f"[{email}] Filling credentials...")
    await page.locator("input[name='email'], input[placeholder='E-mail']").first.fill(account["email"])
    await page.locator("input[type='password']").first.fill(account["password"])

    log(
        f"\n{CYAN}[{email}] ACTION REQUIRED:{RESET} "
        f"Please click the Cloudflare checkbox ('Confirme que é humano') "
        f"in the browser window. Waiting...",
    )
    await page.wait_for_function(
        "document.querySelector('input[name=\"cf-turnstile-response\"]')?.value?.length > 0",
        timeout=120_000,
    )
    log(f"[{email}] Turnstile solved! Submitting...", GREEN)

    await page.locator("button:has-text('CONTINUAR'), button[type='submit']").first.click()
    await page.wait_for_url(
        lambda url: "login.gnjoylatam.com" not in url,
        timeout=TIMEOUT,
    )
    log(f"[{email}] Login successful. Redirected to: {page.url}", GREEN)


async def ensure_on_page(page, account: dict, target_url: str, session: dict):
    """Navigate to target_url and handle login only if not already logged in this session."""
    email = account["email"]
    log(f"[{email}] Navigating to {target_url} ...")
    await page.goto(target_url, wait_until="domcontentloaded", timeout=TIMEOUT)
    await page.wait_for_timeout(3_000)
    await dismiss_cookie_banner(page)

    if session.get("logged_in"):
        log(f"[{email}] Already logged in (session active).", GREEN)
        return

    if await needs_login(page):
        log(f"[{email}] Login required — starting login flow.", YELLOW)
        await do_login(page, account, target_url)
        session["logged_in"] = True
        # After login, go back to the target if redirected elsewhere
        if target_url not in page.url:
            await page.goto(target_url, wait_until="domcontentloaded", timeout=TIMEOUT)
            await page.wait_for_timeout(3_000)
            await dismiss_cookie_banner(page)
    else:
        log(f"[{email}] Already logged in.", GREEN)
        session["logged_in"] = True


# ── shared turnstile helper ───────────────────────────────────────────────────

async def solve_turnstile_if_present(page, email: str, label: str):
    """Wait for Turnstile to be solved if a widget is present on the page."""
    try:
        turnstile_value = await page.locator("input[name='cf-turnstile-response']").first.get_attribute("value", timeout=2_000)
        if not turnstile_value:
            log(
                f"\n{CYAN}[{email}] ACTION REQUIRED:{RESET} "
                f"Cloudflare checkbox detected for {label}. "
                f"Please solve it in the browser window. Waiting...",
            )
            await page.wait_for_function(
                "document.querySelector('input[name=\"cf-turnstile-response\"]')?.value?.length > 0",
                timeout=120_000,
            )
            log(f"[{email}] Turnstile solved!", GREEN)
            await page.wait_for_timeout(500)
    except PlaywrightTimeout:
        pass  # No Turnstile present, proceed normally


# ── task 1: check-in ──────────────────────────────────────────────────────────

async def do_checkin(page, account: dict, event_url: str, session: dict) -> bool:
    email = account["email"]
    await ensure_on_page(page, account, event_url, session)

    pre_shot = _BASE / f"pre_checkin_{email.split('@')[0]}.png"
    await page.screenshot(path=str(pre_shot), full_page=True)
    log(f"[{email}] Pre-checkin screenshot: {pre_shot.name}", YELLOW)

    log(f"[{email}] Looking for check-in button...")
    # Text-based match survives event-to-event CSS module hash changes (e.g. june26roulette
    # -> july26roulette). Old hashed classes kept as a last-resort fallback.
    checkin_btn = page.locator(
        "button:has-text('fazer check-in'), "
        "button[aria-label='Check-in'], "
        "button[aria-label='Fazer check-in'], "
        "button[title='Check-in'], "
        "button.styles_checkin_button__YOuXP"
    ).first

    try:
        await checkin_btn.wait_for(state="attached", timeout=TIMEOUT)
    except PlaywrightTimeout:
        already_done = page.locator(
            "button:has-text('concluído'), "
            "button:has-text('completed'), "
            "button[aria-label='Completed'], "
            "button.styles_complete_button__m12Yr"
        ).first
        try:
            if await already_done.is_visible(timeout=2_000):
                log(f"[{email}] Already checked in today (Completed).", YELLOW)
                await page.screenshot(path=str(_BASE / f"checkin_{email.split('@')[0]}.png"))
                return True
        except PlaywrightTimeout:
            pass
        log(f"[{email}] Check-in button not found — event may have ended or the page layout changed.", YELLOW)
        await page.screenshot(path=str(_BASE / f"error_checkin_{email.split('@')[0]}.png"), full_page=True)
        return False

    await checkin_btn.scroll_into_view_if_needed()
    await page.wait_for_timeout(500)
    await solve_turnstile_if_present(page, email, "check-in")
    await checkin_btn.click(force=True)
    log(f"[{email}] Check-in button clicked!", GREEN)
    await page.wait_for_timeout(3_000)

    for selector in [
        "button:has-text('Confirmar')",
        "button:has-text('Confirm')",
        "button:has-text('OK')",
        "button:has-text('Sim')",
        "button:has-text('ok')",
    ]:
        try:
            confirm_btn = page.locator(selector).first
            if await confirm_btn.is_visible(timeout=2_000):
                await confirm_btn.click()
                log(f"[{email}] Confirmation dialog dismissed.", GREEN)
                await page.wait_for_timeout(2_000)
                break
        except PlaywrightTimeout:
            continue

    try:
        await page.locator(
            "button[aria-label='Completed'], button.styles_complete_button__m12Yr"
        ).first.wait_for(state="visible", timeout=5_000)
        log(f"[{email}] Check-in confirmed — button changed to Completed.", GREEN)
    except PlaywrightTimeout:
        log(f"[{email}] Button did not change to Completed (may still be OK).", YELLOW)

    await page.screenshot(path=str(_BASE / f"checkin_{email.split('@')[0]}.png"), full_page=True)
    return True


# ── task 2: dado do dia ───────────────────────────────────────────────────────

async def do_dado(page, account: dict, session: dict, pause: bool = False) -> bool:
    email = account["email"]
    await ensure_on_page(page, account, URL_MIDGARDTRAIL, session)

    log(f"[{email}] Looking for daily dice button (dado do dia)...")

    # Text-based match survives event-to-event CSS module hash changes; old hashed
    # class kept as a last-resort fallback for events where the button has no text.
    clique_btn = page.locator(
        "button:has-text('clique'), "
        "button.styles_daily_dice_btn__Rl1_o"
    ).first

    try:
        await clique_btn.wait_for(state="visible", timeout=TIMEOUT)
    except PlaywrightTimeout:
        log(f"[{email}] Daily dice button not found — event may have ended or the page layout changed.", YELLOW)
        await page.screenshot(path=str(_BASE / f"dado_{email.split('@')[0]}.png"), full_page=True)
        return False

    if await clique_btn.is_disabled():
        log(f"[{email}] Daily dice already claimed today (button disabled).", YELLOW)
        await page.screenshot(path=str(_BASE / f"dado_{email.split('@')[0]}.png"), full_page=True)
        return True

    await clique_btn.scroll_into_view_if_needed()
    await page.wait_for_timeout(500)
    await solve_turnstile_if_present(page, email, "dado do dia")
    await clique_btn.click(force=True)
    log(f"[{email}] Daily dice button clicked!", GREEN)
    await page.wait_for_timeout(3_000)

    if pause:
        log(f"[{email}] Paused — do what you need in the browser, then press ENTER to continue...", CYAN)
        input()

    # Dismiss any confirmation/reward modal
    for selector in [
        "button:has-text('Confirmar')",
        "button:has-text('Confirm')",
        "button:has-text('OK')",
        "button:has-text('ok')",
        "button:has-text('Fechar')",
        "button:has-text('Close')",
    ]:
        try:
            btn = page.locator(selector).first
            if await btn.is_visible(timeout=2_000):
                await btn.click()
                log(f"[{email}] Modal dismissed.", GREEN)
                await page.wait_for_timeout(1_000)
                break
        except PlaywrightTimeout:
            continue

    await page.screenshot(path=str(_BASE / f"dado_{email.split('@')[0]}.png"), full_page=True)
    log(f"[{email}] Dado do dia done.", GREEN)
    return True


# ── task 3: daily login ───────────────────────────────────────────────────────

async def do_dailylogin(page, account: dict, session: dict) -> bool:
    email = account["email"]
    await ensure_on_page(page, account, URL_DAILYLOGIN, session)

    # Page content loads lazily — wait for reward buttons to appear before searching
    log(f"[{email}] Waiting for daily login rewards to render...")
    try:
        await page.locator("button.styles_reward_btn__y7X7y").first.wait_for(state="attached", timeout=TIMEOUT)
    except PlaywrightTimeout:
        log(f"[{email}] Daily login reward buttons never appeared — event may have ended.", YELLOW)
        await page.screenshot(path=str(_BASE / f"dailylogin_{email.split('@')[0]}.png"), full_page=True)
        return False

    log(f"[{email}] Looking for 'RECEBER ITEM' button...")

    receber_btn = page.locator("button.styles_reward_btn__y7X7y:not([disabled])").first

    try:
        await receber_btn.wait_for(state="visible", timeout=TIMEOUT)
        btn_text = (await receber_btn.inner_text()).strip()
        if btn_text != "RECEBER ITEM":
            log(f"[{email}] No 'RECEBER ITEM' button — first available button says '{btn_text}'. Already claimed?", YELLOW)
            await page.screenshot(path=str(_BASE / f"dailylogin_{email.split('@')[0]}.png"), full_page=True)
            return True
    except PlaywrightTimeout:
        log(f"[{email}] 'RECEBER ITEM' button not found — may already be claimed today.", YELLOW)
        await page.screenshot(path=str(_BASE / f"dailylogin_{email.split('@')[0]}.png"), full_page=True)
        return False

    await receber_btn.scroll_into_view_if_needed()
    await page.wait_for_timeout(500)
    await solve_turnstile_if_present(page, email, "daily login")
    await receber_btn.click(force=True)
    log(f"[{email}] 'RECEBER ITEM' clicked!", GREEN)
    await page.wait_for_timeout(3_000)

    # Dismiss any confirmation/reward modal
    for selector in [
        "button:has-text('Confirmar')",
        "button:has-text('Confirm')",
        "button:has-text('OK')",
        "button:has-text('ok')",
        "button:has-text('Fechar')",
        "button:has-text('Close')",
    ]:
        try:
            btn = page.locator(selector).first
            if await btn.is_visible(timeout=2_000):
                await btn.click()
                log(f"[{email}] Modal dismissed.", GREEN)
                await page.wait_for_timeout(1_000)
                break
        except PlaywrightTimeout:
            continue

    await page.screenshot(path=str(_BASE / f"dailylogin_{email.split('@')[0]}.png"), full_page=True)
    log(f"[{email}] Daily login done.", GREEN)
    return True


# ── per-account runner ────────────────────────────────────────────────────────

async def process_account(account: dict, playwright, tasks: list[str], checkin_url: str) -> dict:
    email = account["email"]
    browser = await playwright.chromium.launch(
        headless=False,
        channel="chrome",
        args=["--disable-blink-features=AutomationControlled"],
    )
    context = await browser.new_context()
    page = await context.new_page()

    session = {"logged_in": False}
    results = {}
    try:
        for task in tasks:
            try:
                if task == "checkin":
                    results["checkin"] = await do_checkin(page, account, checkin_url, session)
                elif task == "dado":
                    results["dado"] = await do_dado(page, account, session)
                elif task == "dado_pause":
                    results["dado"] = await do_dado(page, account, session, pause=True)
                elif task == "dailylogin":
                    results["dailylogin"] = await do_dailylogin(page, account, session)
            except PlaywrightTimeout as e:
                log(f"[{email}] TIMEOUT on {task}: {e}", RED)
                results[task] = False
            except Exception as e:
                log(f"[{email}] ERROR on {task}: {e}", RED)
                results[task] = False
    finally:
        await context.close()
        await browser.close()

    return results


# ── main ──────────────────────────────────────────────────────────────────────

async def main():
    checkin_url, accounts = load_config()
    option = ask_option()

    task_map = {
        1: ["checkin"],
        2: ["dado_pause"],
        3: ["dailylogin"],
        4: ["checkin", "dado", "dailylogin"],
        5: ["checkin", "dado_pause", "dailylogin"],
    }
    tasks = task_map[option]

    log(f"\nRunning task(s): {', '.join(tasks)} for {len(accounts)} account(s).", YELLOW)
    log("Note: A browser window will open for each account. Solve the Turnstile when prompted.", CYAN)

    all_results: dict[str, dict] = {}
    async with async_playwright() as playwright:
        for account in accounts:
            all_results[account["email"]] = await process_account(
                account, playwright, tasks, checkin_url
            )

    log("\n--- Summary ---", YELLOW)
    for email, res in all_results.items():
        parts = ", ".join(
            f"{t}: {'OK' if ok else 'FAIL'}" for t, ok in res.items()
        )
        color = GREEN if all(res.values()) else RED
        log(f"  {email}: {parts}", color)


if __name__ == "__main__":
    try:
        asyncio.run(main())
    except FileNotFoundError as e:
        log(f"\nERROR: {e}", RED)
        log("Make sure accounts.json is in the same folder as this program.", YELLOW)
    except json.JSONDecodeError as e:
        log(f"\nERROR: accounts.json has invalid JSON — {e}", RED)
        log("Check for missing commas, quotes, or brackets in accounts.json.", YELLOW)
    except Exception as e:
        log(f"\nUnexpected error: {e}", RED)
    finally:
        if getattr(_sys, "frozen", False):
            input("\nPress ENTER to close...")
