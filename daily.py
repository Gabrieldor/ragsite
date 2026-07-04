"""
RO Daily Tasks — ro.gnjoylatam.com

Options:
  1. Check-in (roulette event button)
  2. Dado do Dia — click "CLIQUE!" on midgardtrail, pausing after the click for your input
  3. Jogar Dado — spend accumulated dice on the trail board (loops until dice count is 0)
  4. Roulette Spin — click "GIRAR"
  5. Check-in + Dado do Dia
  6. Check-in + Dado do Dia + Roulette Spin + Jogar Dado
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
    log("3. Jogar Dado — spend dice on the trail board (midgardtrail)", YELLOW)
    log("4. Roulette Spin — click 'GIRAR'", YELLOW)
    log("5. Check-in + Dado do Dia", YELLOW)
    log("6. Check-in + Dado do Dia + Roulette Spin + Jogar Dado", YELLOW)
    while True:
        choice = input("\nChoose an option (1/2/3/4/5/6): ").strip()
        if choice in ("1", "2", "3", "4", "5", "6"):
            return int(choice)
        log("Invalid choice. Please enter 1, 2, 3, 4, 5, or 6.", RED)


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

    if session.get("logged_in") and page.url.rstrip("/") == target_url.rstrip("/"):
        log(f"[{email}] Already on {target_url} — skipping reload.", GREEN)
        return

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
    """Wait for Turnstile to be solved if a widget is present on the page.

    The widget (and its hidden response input) renders asynchronously, so a
    short existence check right after page load can race it and wrongly
    conclude "no turnstile here" — leading to a click that fires while the
    "Verificando segurança para acesso" gate is still pending and silently
    does nothing. Give it a real window to attach before deciding it's absent.
    """
    turnstile_input = page.locator("input[name='cf-turnstile-response']").first
    try:
        await turnstile_input.wait_for(state="attached", timeout=8_000)
    except PlaywrightTimeout:
        return  # No Turnstile on this page, proceed normally

    value = await turnstile_input.get_attribute("value")
    if value:
        return  # Already solved (invisible/auto-pass)

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


# ── shared roulette-page counters ─────────────────────────────────────────────

async def get_presence_days(page) -> int:
    """Read 'Número de dias de presença: X / N' from the roulette page. -1 if unreadable."""
    try:
        text = await page.locator(
            "dl:has(dt:has-text('Número de dias de presença')) dd span"
        ).first.inner_text(timeout=5_000)
        return int(text.strip())
    except (PlaywrightTimeout, ValueError):
        return -1


async def get_dice_count(page) -> int:
    """Read 'Número atual de dados' from the midgardtrail page. -1 if unreadable."""
    try:
        text = await page.locator(
            "div[class*='status_card']:has(div[class*='status_label']:has-text('Número atual de dados')) "
            "div[class*='status_value']"
        ).first.inner_text(timeout=5_000)
        return int(text.strip())
    except (PlaywrightTimeout, ValueError):
        return -1


# ── task 1: check-in ──────────────────────────────────────────────────────────

async def do_checkin(page, account: dict, event_url: str, session: dict) -> bool:
    email = account["email"]
    await ensure_on_page(page, account, event_url, session)

    pre_shot = _BASE / f"pre_checkin_{email.split('@')[0]}.png"
    await page.screenshot(path=str(pre_shot), full_page=True)
    log(f"[{email}] Pre-checkin screenshot: {pre_shot.name}", YELLOW)

    days_before = await get_presence_days(page)
    log(f"[{email}] Número de dias de presença before: {days_before}", CYAN)

    # Once today's check-in is done, the site swaps the button image for
    # btn-complete.webp (no text, just like the other button states) — a
    # completely different image than btn-checkin.webp, so the check-in button
    # lookup below would fail and get misreported as "not found / event ended".
    # Detect this first so we skip cleanly instead of falling through to the
    # manual-click fallback (which would otherwise sit there for ~60s per
    # account for no reason).
    already_registered = await page.locator("img[srcset*='btn-complete.webp']").first.is_visible(timeout=3_000)
    if already_registered:
        log(f"[{email}] Already checked in today (btn-complete image shown).", YELLOW)
        await page.screenshot(path=str(_BASE / f"checkin_{email.split('@')[0]}.png"), full_page=True)
        return True

    log(f"[{email}] Looking for check-in button...")
    # The button has no text (it's a background image) and its CSS-module class hash
    # changes every event build. There are TWO elements sharing the 'checkin_button'
    # class stem: one is the real check-in button (img srcset .../btn-checkin.webp),
    # the other is the "reward history" popup button reusing the same styling (img
    # alt="REWARD HISTORY BUTTON", srcset .../btn-popup-reward.webp) — clicking that
    # one is what produced the misleading "Nenhum histórico de recompensas da
    # roleta" alert. Disambiguate via the actual check-in image, not the class name.
    checkin_btn = page.locator("button:has(img[srcset*='btn-checkin.webp'])").first

    try:
        await checkin_btn.wait_for(state="attached", timeout=TIMEOUT)
    except PlaywrightTimeout:
        log(f"[{email}] Check-in button not found — event may have ended or the page layout changed.", YELLOW)
        await page.screenshot(path=str(_BASE / f"error_checkin_{email.split('@')[0]}.png"), full_page=True)
        return False

    if await checkin_btn.is_disabled():
        log(f"[{email}] Already checked in today (button disabled).", YELLOW)
        await page.screenshot(path=str(_BASE / f"checkin_{email.split('@')[0]}.png"), full_page=True)
        return True

    await checkin_btn.scroll_into_view_if_needed()
    await page.wait_for_timeout(500)
    await solve_turnstile_if_present(page, email, "check-in")

    dialog_state = {"text": None}

    async def on_dialog(dialog):
        dialog_state["text"] = dialog.message
        log(f"[{email}] Alert popup: {dialog.message}", YELLOW)
        await dialog.accept()

    # Registered only around the click itself so unrelated page alerts (if any)
    # aren't mistaken for a check-in rejection.
    page.on("dialog", on_dialog)
    await checkin_btn.click()
    log(f"[{email}] Check-in button clicked!", GREEN)
    await page.wait_for_timeout(3_000)
    page.remove_listener("dialog", on_dialog)

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

    days_after = await get_presence_days(page)
    for _ in range(4):
        if days_before < 0 or days_after == days_before + 1:
            break
        await page.wait_for_timeout(1_500)
        days_after = await get_presence_days(page)

    if days_before >= 0 and days_after == days_before + 1:
        log(f"[{email}] Check-in confirmed — presence days {days_before} -> {days_after}.", GREEN)
        await page.screenshot(path=str(_BASE / f"checkin_{email.split('@')[0]}.png"), full_page=True)
        return True

    # The site appears to run extra bot-detection specifically on this button —
    # an identical, automated click is consistently rejected (silent no-op or a
    # generic alert) even though the other buttons on this site click fine
    # automated. A manual click from here reliably succeeds, so fall back to it
    # instead of trying to force the automated path.
    if dialog_state["text"]:
        log(f"[{email}] Automated click was rejected (alert: '{dialog_state['text']}').", YELLOW)
    else:
        log(f"[{email}] Automated click did not register (presence days still {days_after}).", YELLOW)
    log(
        f"\n{CYAN}[{email}] ACTION REQUIRED:{RESET} "
        f"Please click 'FAZER CHECK-IN' yourself in the browser window. Waiting up to 60s...",
    )
    for _ in range(40):
        await page.wait_for_timeout(1_500)
        days_after = await get_presence_days(page)
        if days_before >= 0 and days_after == days_before + 1:
            break

    log(f"[{email}] Número de dias de presença after: {days_after}", CYAN)
    await page.screenshot(path=str(_BASE / f"checkin_{email.split('@')[0]}.png"), full_page=True)

    if days_before >= 0 and days_after == days_before + 1:
        log(f"[{email}] Check-in confirmed — presence days {days_before} -> {days_after}.", GREEN)
        return True

    log(f"[{email}] Check-in NOT confirmed — presence days {days_before} -> {days_after} (expected {days_before + 1}).", RED)
    return False


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

    already_claimed = await clique_btn.evaluate(
        "el => el.disabled || el.className.includes('disabled') || el.getAttribute('aria-disabled') === 'true'"
    )
    if already_claimed:
        log(f"[{email}] Daily dice already claimed today (button disabled).", YELLOW)
        await page.screenshot(path=str(_BASE / f"dado_{email.split('@')[0]}.png"), full_page=True)
        return True

    await clique_btn.scroll_into_view_if_needed()
    await page.wait_for_timeout(500)
    await solve_turnstile_if_present(page, email, "dado do dia")
    await clique_btn.click()
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


# ── task 3: jogar dado (spend dice on the trail board) ────────────────────────

async def do_jogar_dado(page, account: dict, session: dict) -> bool:
    email = account["email"]
    await ensure_on_page(page, account, URL_MIDGARDTRAIL, session)
    await page.wait_for_timeout(1_500)

    dice_count = await get_dice_count(page)
    log(f"[{email}] Número atual de dados: {dice_count}", CYAN)

    if dice_count <= 0:
        log(f"[{email}] No dice to play (count is {dice_count}) — skipping jogar dado.", YELLOW)
        await page.screenshot(path=str(_BASE / f"jogardado_{email.split('@')[0]}.png"), full_page=True)
        return True

    log(f"[{email}] Looking for 'JOGAR DADO' button...")
    throw_btn = page.locator("button[class*='throwBtn']").first
    try:
        await throw_btn.wait_for(state="visible", timeout=TIMEOUT)
    except PlaywrightTimeout:
        log(f"[{email}] 'JOGAR DADO' button not found — page layout may have changed.", YELLOW)
        await page.screenshot(path=str(_BASE / f"jogardado_{email.split('@')[0]}.png"), full_page=True)
        return False

    await solve_turnstile_if_present(page, email, "jogar dado")

    max_iterations = 60
    for _ in range(max_iterations):
        current = await get_dice_count(page)
        if current <= 0:
            break

        await throw_btn.scroll_into_view_if_needed()
        await page.wait_for_timeout(300)
        await throw_btn.click()
        log(f"[{email}] 'JOGAR DADO' clicked (dice before click: {current})...", GREEN)
        await page.wait_for_timeout(4_000)

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
                if await btn.is_visible(timeout=1_500):
                    await btn.click()
                    log(f"[{email}] Modal dismissed.", GREEN)
                    await page.wait_for_timeout(1_000)
                    break
            except PlaywrightTimeout:
                continue

        # Give the trail-board animation and stats panel time to settle before
        # reading the updated count — reading too soon can catch a stale value
        # and wrongly conclude "unchanged" or "went up" mid-animation.
        await page.wait_for_timeout(2_000)
        new_count = await get_dice_count(page)
        if new_count < current:
            log(f"[{email}] Dice count went down: {current} -> {new_count}.", GREEN)
        elif new_count > current:
            log(f"[{email}] Dice count went up: {current} -> {new_count} (landed on a bonus tile) — continuing.", YELLOW)
            await page.wait_for_timeout(1_000)
        else:
            log(f"[{email}] Dice count unchanged ({current}) — stopping to avoid an infinite loop.", RED)
            break
    else:
        log(f"[{email}] Reached max iterations ({max_iterations}) — stopping.", YELLOW)

    final_count = await get_dice_count(page)
    await page.screenshot(path=str(_BASE / f"jogardado_{email.split('@')[0]}.png"), full_page=True)

    if final_count == 0:
        log(f"[{email}] Jogar dado done — all dice used (count reached 0).", GREEN)
        return True

    log(f"[{email}] Jogar dado stopped with {final_count} dice remaining.", YELLOW)
    return False


# ── task 4: roulette spin ─────────────────────────────────────────────────────

async def do_roulette_spin(page, account: dict, event_url: str, session: dict) -> bool:
    email = account["email"]
    await ensure_on_page(page, account, event_url, session)

    log(f"[{email}] Looking for roulette 'GIRAR' button...")
    girar_btn = page.locator("button[class*='roulette_button']").first
    try:
        await girar_btn.wait_for(state="visible", timeout=TIMEOUT)
    except PlaywrightTimeout:
        log(f"[{email}] 'GIRAR' button not found — page layout may have changed.", YELLOW)
        await page.screenshot(path=str(_BASE / f"roulette_{email.split('@')[0]}.png"), full_page=True)
        return False

    dialog_state = {"text": None}

    async def on_dialog(dialog):
        dialog_state["text"] = dialog.message
        log(f"[{email}] Alert popup: {dialog.message}", YELLOW)
        await dialog.accept()

    page.on("dialog", on_dialog)
    try:
        await girar_btn.scroll_into_view_if_needed()
        await page.wait_for_timeout(500)
        await solve_turnstile_if_present(page, email, "roulette spin")
        await girar_btn.click()
        log(f"[{email}] 'GIRAR' clicked!", GREEN)
        await page.wait_for_timeout(3_000)
    finally:
        page.remove_listener("dialog", on_dialog)

    await page.screenshot(path=str(_BASE / f"roulette_{email.split('@')[0]}.png"), full_page=True)

    if dialog_state["text"]:
        log(f"[{email}] Roulette spin confirmed — alert shown: '{dialog_state['text']}'.", GREEN)
        return True

    log(f"[{email}] No alert appeared after clicking GIRAR — check the screenshot to see what happened.", YELLOW)
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
                elif task == "jogar_dado":
                    results["jogar_dado"] = await do_jogar_dado(page, account, session)
                elif task == "roulette_spin":
                    results["roulette_spin"] = await do_roulette_spin(page, account, checkin_url, session)
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
        3: ["jogar_dado"],
        4: ["roulette_spin"],
        5: ["checkin", "dado_pause"],
        6: ["checkin", "roulette_spin", "dado", "jogar_dado"],
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
