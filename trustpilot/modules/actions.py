"""
Playwright-based actions for Trustpilot warming strategy, including account registration, company page opening and reviews reading with interactions.
"""

import asyncio
import random
import urllib.parse
import pyotp

from playwright.async_api import async_playwright, expect
from playwright.async_api import Error as PlaywrightError
from playwright._impl._errors import TargetClosedError
from load_django import *
from pathlib import Path
import dotenv
import os
import requests
import hashlib
import time
from typing import Optional, List
from datetime import datetime
from services.task_logger import TaskLogger
from requests.exceptions import JSONDecodeError, RequestException
from asgiref.sync import sync_to_async
from user.models import UserTaskManager, Review
from clients.multilogin_client import MultiloginClient
from browser.browser_session import BrowserSession, BrowserStartError


class SessionExpiredError(RuntimeError):
    """Raised when a Trustpilot login/signup modal appears mid-scenario, indicating session expiry."""


BASE_DIR = Path(__file__).resolve().parent.parent

dotenv.load_dotenv(os.path.join(BASE_DIR, '.env'))


def _response_debug_message(response, max_body_length: int = 1000):
    content_type = response.headers.get("content-type", "unknown")
    body = response.text.strip()
    if len(body) > max_body_length:
        body = f"{body[:max_body_length]}... [truncated]"
    return f"status={response.status_code}, content_type={content_type}, body={body!r}"


# DEPRECATED: moved to MultiloginClient — kept for backward compatibility during migration
def get_token(task):
    url = "https://api.multilogin.com/user/signin"
    payload = {
        "email": task.profile.user.email,
        "password": hashlib.md5(task.profile.user.password.encode()).hexdigest()
    }
    r = requests.post(url, json=payload)
    if r.status_code != 200:
        logger = TaskLogger(task)
        logger.console(f"Failed to get token for user {task.profile.user.email}. Status: {r.status_code}")
        return None
    return r.json().get("data", {}).get("token")


MAX_START_RETRIES = 3
BASE_RETRY_DELAY = 2


def _extract_port(data):
    return data.get("value") or data.get("data", {}).get("port")


def _is_lock_error(response_text, data=None):
    text = (response_text or "").lower()
    if "lock_profile_error" in text or "can't lock profile" in text:
        return True

    if isinstance(data, dict):
        status = data.get("status") or {}
        message = str(status.get("message", "")).lower()
        error_code = str(status.get("error_code", "")).lower()
        if error_code == "lock_profile_error" or "can't lock profile" in message:
            return True

    return False


def _is_proxy_error(response_text, data=None):
    text = (response_text or "").lower()
    proxy_keywords = ["proxy_error", "proxy_connection_error", "proxy_timeout", "proxy_auth_error"]
    if any(k in text for k in proxy_keywords):
        return True

    if isinstance(data, dict):
        status = data.get("status") or {}
        error_code = str(status.get("error_code", "")).lower()
        if "proxy" in error_code:
            return True

    return False


async def _request_start_profile(url, headers, params):
    return await asyncio.to_thread(
        requests.get,
        url,
        headers=headers,
        params=params,
        verify=False,
        timeout=30,
    )


# DEPRECATED: moved to MultiloginClient.start_profile — kept for backward compatibility during migration
async def start_browser(token, task, cleanup_locked_profile=None, max_retries=MAX_START_RETRIES):
    logger = TaskLogger(task)
    folder_id = task.profile.ml_folder_id
    profile_id = task.profile.ml_profile_id

    url = f"https://launcher.mlx.yt:45001/api/v2/profile/f/{folder_id}/p/{profile_id}/start"
    params = {"automation_type": "playwright"}
    headers = {"Authorization": f"Bearer {token}"}

    last_error_msg = None

    for attempt in range(1, max_retries + 1):
        try:
            res = await _request_start_profile(url, headers, params)
        except RequestException as e:
            last_error_msg = (
                f"Request failed while starting browser for user "
                f"{task.profile.user.email}, attempt {attempt}/{max_retries}: "
                f"{type(e).__name__}: {e}"
            )

            if attempt < max_retries:
                await logger.warning(last_error_msg)
                await asyncio.sleep(min(BASE_RETRY_DELAY * attempt, 10))
                continue

            await logger.error(last_error_msg, exc=e)
            task.status = UserTaskManager.Status.FAILED
            return None

        raw_text = res.text
        data = None

        try:
            data = res.json()
        except JSONDecodeError:
            data = None

        if res.status_code == 200:
            if data is None:
                last_error_msg = (
                    f"JSON decoding failed while starting browser on successful response, "
                    f"attempt {attempt}/{max_retries}. {_response_debug_message(res)}"
                )

                if attempt < max_retries:
                    await logger.warning(last_error_msg)
                    await asyncio.sleep(min(BASE_RETRY_DELAY * attempt, 10))
                    continue

                await logger.error(last_error_msg)
                task.status = UserTaskManager.Status.FAILED
                return None

            port = _extract_port(data)
            if port:
                return port

            last_error_msg = (
                f"Browser started response did not contain a port, "
                f"attempt {attempt}/{max_retries}. Parsed response: {data}"
            )

            if attempt < max_retries:
                await logger.warning(last_error_msg)
                await asyncio.sleep(min(BASE_RETRY_DELAY * attempt, 10))
                continue

            await logger.error(last_error_msg)
            task.status = UserTaskManager.Status.FAILED
            return None

        error_msg = (
            f"API error while starting browser, attempt {attempt}/{max_retries}: "
            f"{_response_debug_message(res)}"
        )
        logger.console(error_msg)

        if _is_proxy_error(raw_text, data):
            proxy_err_msg = f"Critical Proxy Error: {error_msg}. The configured proxy for profile {profile_id} is unreachable or invalid."
            await logger.error(proxy_err_msg)
            # Proxy errors during startup usually require manual intervention or rotation
            task.status = UserTaskManager.Status.FAILED
            return None

        if _is_lock_error(raw_text, data):
            await logger.warning(error_msg)

            if cleanup_locked_profile is not None:
                try:
                    cleaned = await cleanup_locked_profile(token, task)
                    cleanup_msg = (
                        f"Locked profile cleanup result for profile {profile_id}: {cleaned}"
                    )
                    await logger.warning(cleanup_msg)
                except Exception as cleanup_err:
                    cleanup_msg = (
                        f"Cleanup failed for locked profile {profile_id}: "
                        f"{type(cleanup_err).__name__}: {cleanup_err}"
                    )
                    await logger.error(cleanup_msg, exc=cleanup_err)

            if attempt < max_retries:
                await asyncio.sleep(min(BASE_RETRY_DELAY * (2 ** (attempt - 1)), 10))
                continue

            last_error_msg = error_msg
            task.status = UserTaskManager.Status.FAILED
            await logger.error("Profile remained locked after retries.")
            return None

        await logger.error(error_msg)
        task.status = UserTaskManager.Status.FAILED
        return None

    task.status = UserTaskManager.Status.FAILED
    await logger.error(last_error_msg or "Failed to start browser after retries.")
    return None


# DEPRECATED: split into MultiloginClient.stop_profile and BrowserSession.stop — kept for backward compatibility during migration
async def stop_multilogin_profile(playwright, task):
    logger = TaskLogger(task)
    if os.getenv("STOP_BROWSER", "True").lower() == "true":
        if playwright is not None:
            try:
                await playwright.stop()
            except Exception as e:
                await logger.error(f"Failed to stop playwright: {e}", exc=e)
        else:
            logger.console("Playwright instance was not initialized, skipping stop.")

        token = get_token(task)

        if not token:
            await logger.error("Cannot stop Multilogin profile: token is missing")
            return

        url = f"https://launcher.mlx.yt:45001/api/v1/profile/stop/p/{task.profile.ml_profile_id}"

        headers = {
            "Authorization": f"Bearer {token}",
            "Accept": "application/json",
        }

        try:
            response = requests.get(url, headers=headers, verify=False, timeout=30)

            # Attempt to parse JSON response for detailed error codes
            response_data = None
            try:
                response_data = response.json()
            except JSONDecodeError:
                # If not JSON, we'll fall back to checking raw text
                pass

            if response.status_code == 500 and response_data and "profile already stopped" in str(response_data.get("status", {}).get("message", "")).lower():
                # Profile was already stopped, which is the desired state. Log as INFO.
                message = f"Multilogin profile {task.profile.ml_profile_id} was already stopped. Status: {response.status_code}."
                await logger.info(message)
            elif response.status_code not in (200, 204):
                # Other non-success status codes are actual failures
                message = f"Failed to stop Multilogin profile: status={response.status_code}, body={response.text}"
                await logger.error(message)
            else:
                # Successful stop (200 or 204)
                message = f"Stopped Multilogin profile: {task.profile.ml_profile_id}"
                await logger.info(message)

        except RequestException as e: # Catch network-related errors specifically
            message = f"Network error while stopping Multilogin profile {task.profile.ml_profile_id}: {e}"
            await logger.error(message, exc=e)
        except Exception as e: # Catch any other unexpected errors
            message = f"An unexpected error occurred while stopping Multilogin profile {task.profile.ml_profile_id}: {e}"
            await logger.error(message, exc=e)
    else:
        return True


async def _search_tab(context, tabs_to_search: List[str]):
    logger = TaskLogger(task=None)
    target_page = None

    for p in context.pages:
        logger.console(f"Checking tab URL: {p.url}")
        if any(tab in p.url for tab in tabs_to_search):
            logger.console(f"Matched tab found: {p.url}")
            target_page = p
            break

    if not target_page:
        logger.console(f"No matching tab found for: {tabs_to_search}")


async def verify_check(page, task, attempts, delay=30000):
    """Wait for a Cloudflare 'Verifying your connection' challenge to clear.

    Uses wait_for(state='hidden') so we wait for actual resolution, not a fixed sleep.
    """
    logger = TaskLogger(task)
    verify_selector = '//h1[text()="Verifying your connection..."]'
    for verify_attempt in range(attempts):
        try:
            is_visible = await page.locator(verify_selector).is_visible(timeout=3_000)
        except PlaywrightError:
            # Page is navigating away — challenge already resolved.
            break
        if not is_visible:
            break
        await logger.warning(
            f"verify_check:: 'Verifying your connection' detected "
            f"(attempt {verify_attempt + 1}/{attempts}), waiting up to {delay // 1000}s..."
        )
        try:
            await page.locator(verify_selector).wait_for(state="hidden", timeout=delay)
            logger.console("verify_check:: Challenge cleared.")
            break
        except PlaywrightError:
            pass  # Still visible after delay — loop and check again.
        if verify_attempt == attempts - 1:
            err_msg = "verify_check:: 'Verifying your connection' did not resolve after all attempts."
            await logger.error(err_msg)
            if task:
                task.comment = "Verifying your connection issue"
            raise RuntimeError(err_msg)

async def safe_goto(page, url, *, wait_until="domcontentloaded", timeout=60_000, retries=3, logger=None):
    """Navigate to url, retrying on ERR_ABORTED or timeout.

    Uses a 60s default timeout because Trustpilot search URLs are slow under
    Cloudflare bot-detection. Retries cover both race-condition aborts and
    transient slowness.

    SOCKS/SSL proxy errors are NOT retried — the tunnel is dead and no number
    of retries on the same connection will fix it. Raise immediately so the
    task fails fast and the profile is rescheduled.
    """
    for attempt in range(retries):
        try:
            # Let any in-flight navigation settle before starting a new one.
            try:
                await page.wait_for_load_state("domcontentloaded", timeout=10_000)
            except PlaywrightError:
                pass
            await page.goto(url, wait_until=wait_until, timeout=timeout)
            return
        except PlaywrightError as e:
            msg = str(e)
            # Dead proxy tunnel — no point retrying the same broken connection.
            if "ERR_SOCKS_CONNECTION_FAILED" in msg or "ERR_CERT_COMMON_NAME_INVALID" in msg:
                raise
            retryable = "ERR_ABORTED" in msg or "Timeout" in msg
            if retryable and attempt < retries - 1:
                if logger:
                    logger.console(
                        f"safe_goto:: {type(e).__name__} on attempt {attempt + 1}/{retries}, retrying: {url}"
                    )
                await page.wait_for_timeout(2_000 * (attempt + 1))
                continue
            raise


# Log-in to Google account
async def login_to_google_account(context, page, user_data, multiplier_delay, task):
    logger = TaskLogger(task)
    # Login page if no tabs with google opened
    try:
        await _search_tab(context, ["mail.google.com", "accounts.google.com"])
        await page.goto("https://www.gmail.com/", wait_until="domcontentloaded")
        logger.console("Navigated to Google login page.")
    except PlaywrightError as e:
        await logger.error(f"login_to_google_account:: Failed to navigate to Google: {e}", exc=e)
        return False

    try:
        # Check wether it's a main page
        signin_path = '//span[text()="Sign in"]'
        if await page.locator('//span[text()="Gmail"]').count() > 0:
            async with page.expect_popup() as popup_info:
                await page.locator(signin_path).click()

            google_page = await popup_info.value
            await google_page.wait_for_load_state()
        else:
            google_page = page

        email_path = '//input[@type="email"]'
        await google_page.locator(email_path).wait_for(state='visible')
        await google_page.locator(email_path).type(user_data["email"])

        next_button = '//span[text()="Next"]'
        await google_page.locator(next_button).wait_for(state='visible')
        await google_page.locator(next_button).click()
        await google_page.wait_for_timeout(1500 * multiplier_delay)

        div_password_path = '//div[@id="password"]'
        await google_page.locator(div_password_path).wait_for(state='visible')
        await google_page.wait_for_timeout(1500 * multiplier_delay)

        password_path = '//input[@type="password"]'
        for attempt in range(3):
            password_input = google_page.locator(password_path)
            if await google_page.locator(password_path).count() > 1:
                await logger.warning(f"Multiple password inputs found, attempt {attempt + 1}/3. Retrying...")
                await google_page.wait_for_timeout(2500 * multiplier_delay)
            elif await password_input.count() == 1:
                break

        await google_page.locator(password_path).wait_for(state='visible')
        await expect(password_input).to_be_editable(timeout=30000)
        await google_page.locator(password_path).type(user_data["password"])
        await google_page.locator(next_button).wait_for(state='visible')
        await google_page.locator(next_button).click()
        await google_page.wait_for_timeout(1500 * multiplier_delay)

        await google_page.wait_for_load_state("domcontentloaded")

        # Check 2-FA verification
        fa2_sign = google_page.locator('//strong[text()="Google Authenticator"]')
        try:
            await fa2_sign.wait_for(state="visible", timeout=30000)
            await logger.info("2-FA verification, continue...")

        except PlaywrightError as e:
            await logger.error("2-FA verification method not found, cannot continue login.", exc=e)
            return False

        # Enter 2-FA code
        secret = os.getenv("G_2FA_SECRET")
        totp = pyotp.TOTP(secret)
        try:
            fa_input_path = '//input[@type="tel"]'
            await google_page.locator(fa_input_path).wait_for(state='visible')
            await google_page.locator(fa_input_path).type(totp.now())
        except PlaywrightError as e:
            await logger.error(f"Failed to enter 2-FA code: {e}", exc=e)
            return False

        for attempt in range(3):
            if await google_page.locator(next_button).count() == 0:
                await logger.warning(f"Button 'Next' didn't found after entering 2-FA code, attempt {attempt + 1}/3. Retrying...")
                await google_page.wait_for_timeout(2500 * multiplier_delay)

        await google_page.locator(next_button).wait_for(state='visible')
        await google_page.locator(next_button).click()
        await google_page.wait_for_timeout(1500 * multiplier_delay)
        await google_page.wait_for_load_state("domcontentloaded")

        # Check whether or not 2FA passes by checking existance of next element
        c5_label_path = '//span[@id="c5"]'
        try:
            await google_page.locator(c5_label_path).wait_for(state="visible", timeout=30000)
            logger.console("Logged in to Google account successfully.")
            await google_page.goto("about:blank")

        except PlaywrightError as e:
            await logger.error(f"Failed to log in to Google account, possibly due to 2-FA failure: {e}", exc=e)
            return False

    except PlaywrightError as e:
        await logger.error(f"Failed to log in to Google: {e}", exc=e)
        return False

    return True


# Register account on Trustpilot wich has status "New" on DB
async def account_registraton(page, user_data, task):
    logger = TaskLogger(task)
    # Login page
    try:
        await page.goto("https://www.trustpilot.com/", wait_until="domcontentloaded")
    except PlaywrightError as e:
        await logger.error(f"account_registration:: Failed to navigate to Trustpilot: {e}", exc=e)
        return False

    #await accept_cookies(page)

    # Login button
    await page.locator('//span[text()="Log in"]').wait_for(state='visible')
    await page.locator('//span[text()="Log in"]').click()
    await page.wait_for_timeout(1500)

    # "Continue with Google button"
    iframe = page.locator('iframe[id^="gsi_"]').first
    await iframe.wait_for(state="visible", timeout=15000)

    frame = iframe.content_frame
    print(f"Frame loaded - {frame is not None}: {iframe}")

    google_button = frame.get_by_text(
        "Continue with Google",
        exact=True,
    )

    await google_button.wait_for(state="visible", timeout=15000)

    async with page.expect_popup(timeout=15000) as popup_info:
        await google_button.click()

    google_page = await popup_info.value
    await google_page.wait_for_load_state("domcontentloaded")

    # Click on account with email from DB
    try:
        await google_page.locator(f'[data-identifier="{user_data["email"]}"]').wait_for(state='visible')
        await google_page.locator(f'[data-identifier="{user_data["email"]}"]').click()
    except PlaywrightError as e:
        print(f"Error during account registration, email: {user_data['email']}, didn't found: {e}")
        await google_page.close()
        await logger.error(f"Failed to find Google account: {e}", exc=e)
        return
    await google_page.wait_for_load_state()

    # ⚠️ 2-FA feature handling is needed while google may ask about it, so the rest of funtion may not be working
    # Continue with google account
    try:
        await google_page.wait_for_load_state("domcontentloaded")

        if await google_page.locator('//span[text()="Продовжити"]').count() > 0:
            await google_page.locator('//span[text()="Продовжити"]').click()
        elif await google_page.locator('//span[text()="Continue"]').count() > 0:
            await google_page.locator('//span[text()="Continue"]').click()
    except PlaywrightError as e:
        print(f"Error during account registration, email: {user_data['email']}, couldn't continue: {e}")
        await google_page.close()
        return

    # Continue with terms and conditions
    try:
        await google_page.wait_for_load_state("domcontentloaded")

        if await google_page.locator('//span[text()="Прийняти"]').count() > 0:
            await google_page.locator('//span[text()="Прийняти"]').click()
        elif await google_page.locator('//span[text()="Continue"]').count() > 0:
            await google_page.locator('//span[text()="Continue"]').click()
            print(f"Account with email {user_data['email']} registered successfully")

    except PlaywrightError as e:
        print(f"Error during account registration, couldn't accept terms: {e}")
        await google_page.close()
        return


# Go on Main page "/"
async def go_to_main_page(page, task):
    logger = TaskLogger(task)
    try:
        await page.wait_for_load_state("domcontentloaded")
        await page.wait_for_load_state("networkidle")

        # Check wether the page is "Loading verify check"
        for attempt in range(8):
            verify_check = '//h1[text()="Verifying your connection..."]'

            if await page.locator(verify_check).count() > 0:
                err_msg = f"Encountered 'Verifying your connection' page, attempt {attempt + 1}/8. Waiting for it to resolve..."
                await logger.warning(err_msg)
                await page.wait_for_timeout(15000)
                logger.console("'Verifying your connection' page resolved, continuing...")

                if attempt == 7:
                    error_msg = "go_to_main_page:: Failed to resolve 'Verifying your connection' page after 8 attempts."
                    await logger.error(error_msg)
                    raise RuntimeError(error_msg)
            else:
                logger.console("No 'Verifying your connection' page, proceeding with login...")
                break

        await page.wait_for_load_state("domcontentloaded")
        await page.wait_for_load_state("networkidle")

        for attempt in range(3):
             if await page.locator('//a[@href="/"]').count() == 0:
                err_msg = 'go_to_main_page:: Main page link not found, attempt {attempt + 1}/3. Retrying...'
                await logger.warning(err_msg)
                await page.wait_for_timeout(2500)

                if attempt == 2:
                    error_msg = "go_to_main_page:: Failed to find main page link after 3 attempts."
                    await logger.error(error_msg)
                    raise RuntimeError(error_msg)
             else:
                break

        await page.locator('//a[@href="/"]').wait_for(state='visible')
        await page.locator('//a[@href="/"]').click()

    except PlaywrightError as e:
        await logger.error(f"go_to_main_page:: Failed to navigate to main page Trustpilot: {e}", exc=e)
        return


# DEPRECATED: moved to BrowserSession.start — kept for backward compatibility during migration
async def connect_multilogin(port):
    playwright = await async_playwright().start()
    browser = await playwright.chromium.connect_over_cdp(f"http://127.0.0.1:{port}")
    context = browser.contexts[0]
    page = context.pages[0] if context.pages else await context.new_page()
    return playwright, browser, context, page


async def login_to_site(page, task):
    logger = TaskLogger(task)
    context = page.context

    # Find existing Trustpilot tab
    for p in context.pages:
        if "trustpilot.com" in p.url:
            await p.bring_to_front()
            await p.wait_for_load_state("domcontentloaded")
            return p

    # Open a new tab if none exist or the current one is closed
    if not context.pages or page.is_closed():
        page = await context.new_page()

    try:
        await page.goto("https://www.trustpilot.com/", wait_until="domcontentloaded")
        await page.bring_to_front()

        # Check wether the page is "Loading verify check"
        for attempt in range(8):
            verify_check = '//h1[text()="Verifying your connection..."]'

            if await page.locator(verify_check).count() > 0:
                err_msg = f"login_to_site:: Encountered 'Verifying your connection' page, attempt {attempt + 1}/8. Waiting for it to resolve..."
                await logger.warning(err_msg)
                await page.wait_for_timeout(15000)
                logger.console("'Verifying your connection' page resolved, continuing...")

                if attempt == 7:
                    error_msg = "login_to_site:: Failed to resolve 'Verifying your connection' page after 8 attempts."
                    await logger.error(error_msg)
                    raise RuntimeError(error_msg)
            else:
                logger.console("login_to_site:: No 'Verifying your connection' page, proceeding with login...")
                break

    except PlaywrightError as e:
        err_str = str(e)
        if "net::ERR_SOCKS_CONNECTION_FAILED" in err_str:
            # Distinct message so task_runner can intercept SOCKS failures specifically
            # and attempt a single reconnect without conflating them with SSL errors.
            error_msg = f"SOCKS connection failed: {err_str}. Profile {task.profile.ml_profile_id if task else 'unknown'}."
            await logger.error(error_msg, exc=e)
            raise RuntimeError("SOCKS_PROXY_FAILURE") from e
        elif "ERR_CERT_COMMON_NAME_INVALID" in err_str:
            # Distinct message so task_runner can intercept SSL failures specifically
            # and attempt a single SSL reconnect, mirroring the SOCKS recovery path.
            error_msg = f"SSL connection failed: {err_str}. Profile {task.profile.ml_profile_id if task else 'unknown'}."
            await logger.error(error_msg, exc=e)
            raise RuntimeError("SSL_PROXY_FAILURE") from e
        else:
            await logger.error(f"login_to_site:: Failed to navigate to Trustpilot: {e}", exc=e)

    return page


async def assert_authenticated(page, logger):
    """Check for an active Trustpilot session before posting a review.

    Two-stage check so slow header hydration on public pages (review listings,
    evaluate form) doesn't cause a false session-expired failure:

    Stage 1 — JS predicate on the current page, 15 s budget.
      'logged_in'  → return immediately.
      'logged_out' → raise SessionExpiredError (definitive: Log-in button seen).
      timeout      → log header diagnostics, go to stage 2.

    Stage 2 — navigate to the Trustpilot homepage and recheck (15 s budget).
      'logged_in'  → log that stage-1 page had a slow header, return OK.
      'logged_out' → raise SessionExpiredError.
      timeout      → log warning and return without raising. Indeterminate state
                     is NOT treated as session expiry — the login-modal check
                     inside post_review() will catch a genuinely dead session.
    """

    # JS predicate: returns null while still loading, 'logged_in' or 'logged_out' when certain.
    _AUTH_STATE_JS = """() => {
        const header = document.querySelector('header');
        if (!header) return null;

        // Positive: any element that Trustpilot only renders for authenticated users.
        if (
            header.querySelector('[data-testid*="avatar"]')           ||
            header.querySelector('[data-testid*="user-menu"]')        ||
            header.querySelector('[data-testid*="consumer-profile"]') ||
            header.querySelector('[data-testid*="header-profile"]')   ||
            header.querySelector('[data-testid*="profile-button"]')   ||
            header.querySelector('img[alt*="profile" i]')             ||
            header.querySelector('img[alt*="avatar"  i]')             ||
            header.querySelector('a[href*="/users/"]')                ||
            header.querySelector('a[href*="/account"]')               ||
            header.querySelector('a[href^="/settings"]')              ||
            Array.from(header.querySelectorAll('[aria-label]')).some(
                el => /account|profile|user menu/i.test(el.getAttribute('aria-label'))
            )
        ) return 'logged_in';

        // Negative: exact 'Log in' text on an interactive element in the header only.
        // Exact match prevents matching page-content CTAs that also say "Log in".
        for (const el of header.querySelectorAll('a, button')) {
            if (el.textContent.trim() === 'Log in') return 'logged_out';
        }

        return null;  // header present but not yet hydrated — keep polling
    }"""

    _HEADER_DIAGNOSTIC_JS = """() => {
        const header = document.querySelector('header');
        if (!header) return {exists: false};
        return {
            exists: true,
            testids: Array.from(header.querySelectorAll('[data-testid]'))
                         .map(el => el.getAttribute('data-testid')).slice(0, 20),
            ariaLabels: Array.from(header.querySelectorAll('[aria-label]'))
                            .map(el => el.getAttribute('aria-label')).slice(0, 10),
            links: Array.from(header.querySelectorAll('a[href]'))
                       .map(el => el.getAttribute('href')).slice(0, 10),
            innerHTMLSnippet: header.innerHTML.slice(0, 600),
        };
    }"""

    async def _run_check(target_page, timeout_ms):
        """Return 'logged_in', 'logged_out', or None (timeout / indeterminate)."""
        try:
            handle = await target_page.wait_for_function(_AUTH_STATE_JS, timeout=timeout_ms)
            return await handle.json_value()
        except PlaywrightError:
            return None

    async def _log_header_diagnostics(target_page, stage_label):
        try:
            diag = await target_page.evaluate(_HEADER_DIAGNOSTIC_JS)
            if not diag.get("exists"):
                await logger.warning(
                    f"assert_authenticated:: [{stage_label}] No <header> element in DOM. "
                    f"URL: {target_page.url}"
                )
            else:
                await logger.warning(
                    f"assert_authenticated:: [{stage_label}] Header present but auth state "
                    f"indeterminate after timeout. "
                    f"data-testids={diag['testids']}, "
                    f"aria-labels={diag['ariaLabels']}, "
                    f"header links={diag['links']}, "
                    f"innerHTML[:600]={diag['innerHTMLSnippet']!r}. "
                    f"URL: {target_page.url}"
                )
        except Exception as diag_err:
            await logger.warning(
                f"assert_authenticated:: [{stage_label}] Diagnostic evaluation failed: {diag_err}. "
                f"URL: {target_page.url}"
            )

    # ── Stage 1: check the current page ──────────────────────────────────────
    stage1_url = page.url
    state = await _run_check(page, timeout_ms=15_000)

    if state == "logged_in":
        logger.console(f"assert_authenticated:: Session verified (stage 1). URL: {stage1_url}")
        return

    if state == "logged_out":
        raise SessionExpiredError(
            f"assert_authenticated:: No active session — 'Log in' button found in header "
            f"(stage 1). URL: {stage1_url}"
        )

    # Stage 1 timed out — collect diagnostics before trying a second page.
    await _log_header_diagnostics(page, "stage-1")
    await logger.warning(
        f"assert_authenticated:: Stage-1 check indeterminate on {stage1_url} — "
        f"navigating to Trustpilot homepage for a second check."
    )

    # ── Stage 2: homepage has a more predictable header hydration path ────────
    try:
        await page.goto("https://www.trustpilot.com/", wait_until="domcontentloaded", timeout=20_000)
    except PlaywrightError as nav_err:
        await logger.warning(
            f"assert_authenticated:: Homepage navigation failed ({nav_err}). "
            f"Auth state unconfirmed — proceeding; login-modal check will catch a dead session."
        )
        return

    state2 = await _run_check(page, timeout_ms=15_000)

    if state2 == "logged_in":
        await logger.warning(
            f"assert_authenticated:: Session confirmed on homepage (stage-1 header was slow "
            f"to hydrate on {stage1_url}). Continuing."
        )
        return

    if state2 == "logged_out":
        raise SessionExpiredError(
            f"assert_authenticated:: No active session — 'Log in' button found on homepage "
            f"(stage 2). Original URL was {stage1_url}"
        )

    # Both stages indeterminate — log diagnostics but do NOT raise.
    await _log_header_diagnostics(page, "stage-2")
    await logger.warning(
        f"assert_authenticated:: Auth state could not be determined on homepage either. "
        f"Proceeding without confirmation — login-modal check inside post_review() "
        f"will catch a genuinely expired session."
    )


def print_with_timestamp(message):
    timestamp = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    print(f"[{timestamp}] {message}")


async def _resolve_search_input(page, logger):
    """Return a single-element locator for the visible search input.

    Waits up to 8 s for duplicate inputs (desktop + mobile) to collapse to
    exactly one visible element before any action is taken.
    """
    try:
        await page.wait_for_function(
            """() => {
                const inputs = Array.from(document.querySelectorAll('input[name="query"]'));
                return inputs.filter(el =>
                    window.getComputedStyle(el).display !== 'none' &&
                    window.getComputedStyle(el).visibility !== 'hidden' &&
                    el.offsetParent !== null
                ).length === 1;
            }""",
            timeout=8000,
        )
    except Exception:
        pass  # best-effort; selection below still works

    all_inputs = page.locator('input[name="query"]')
    total = await all_inputs.count()
    visible = all_inputs.filter(visible=True)
    visible_count = await visible.count()
    logger.console(
        f"Search inputs: {total} total, {visible_count} visible; selecting first visible."
    )
    # .first ensures strict-mode is never violated regardless of visible_count
    return visible.first


async def _validate_company_page(page, logger, company_query) -> bool:
    """Return True if the current page appears to be the intended company.

    Uses URL substring match first, then the visible page title as a fallback.
    """
    current_url = page.url.lower()
    # Normalise by stripping spaces and dashes so "Blue Tomato" matches "www.blue-tomato.com"
    query_norm = company_query.lower().replace(" ", "").replace("-", "")
    url_norm = current_url.replace("-", "")

    if query_norm in url_norm:
        logger.console(f"open_company_page:: Company page validated: URL matches '{company_query}'.")
        return True

    try:
        title_el = page.locator('#business-unit-title h1').first
        if await title_el.count() > 0:
            title_text = (await title_el.inner_text()).lower().replace(" ", "").replace("-", "")
            if query_norm in title_text or title_text in query_norm:
                logger.console("open_company_page:: Company page validated via page title.")
                return True
    except PlaywrightError:
        pass

    logger.console(
        f"open_company_page:: Company page mismatch — URL: {page.url}, expected: '{company_query}'."
    )
    return False


def _query_variants(company_query):
    """Generate alternative search strings to try when the literal query yields no card.

    Strips common TLDs / www prefix and turns dots/dashes into spaces so the
    Trustpilot search engine sees a more human-readable form.
    """
    variants = [company_query]
    lower = company_query.lower()
    for suffix in (".com", ".net", ".org", ".io", ".co.uk", ".co", ".de", ".uk", ".eu"):
        if lower.endswith(suffix):
            variants.append(company_query[: -len(suffix)])
            break
    if lower.startswith("www."):
        variants.append(company_query[4:])
    if "." in company_query or "-" in company_query:
        variants.append(company_query.replace(".", " ").replace("-", " ").strip())
    seen, out = set(), []
    for v in variants:
        if v and v not in seen:
            seen.add(v)
            out.append(v)
    return out


def _matches_query(card_text, query):
    """Bidirectional normalized substring match between a card and a query."""
    a = card_text.lower().replace(" ", "").strip()
    b = query.lower().replace(" ", "").strip()
    if not a or not b:
        return False
    return a in b or b in a


async def dismiss_cookie_banner(page, logger=None):
    """Click the cookie consent accept button if the Trustpilot/OneTrust banner is present.

    Uses a short timeout so this is a cheap no-op when no banner exists.
    Returns True if a button was clicked, False otherwise.
    """
    selectors = [
        'button#onetrust-accept-btn-handler',
        'button[data-test="cookie-banner-accept"]',
        '#onetrust-banner-sdk button.onetrust-accept-btn-handler',
        'button:has-text("Accept all")',
        'button:has-text("Accept All")',
        'button:has-text("I accept")',
        'button:has-text("Allow all")',
        'button:has-text("Agree")',
    ]
    for sel in selectors:
        try:
            btn = page.locator(sel).first
            if await btn.is_visible(timeout=2_000):
                await btn.click(timeout=5_000)
                if logger:
                    logger.console(f"dismiss_cookie_banner:: Dismissed via '{sel}'.")
                await page.wait_for_timeout(600)
                return True
        except PlaywrightError:
            continue
    return False


async def _find_and_open_matching_card(page, logger, company_query, task):
    """Scan cards on the current search-results page and click the one matching company_query.

    Tries multiple card selectors and bidirectional text matching so a query like
    'signalrgb.com' will match a card labelled 'SignalRGB'.
    Returns True on a validated match, False if no card matched on this page.
    Raises RuntimeError only on a paradox (text matched but URL diverged after click).
    """
    await dismiss_cookie_banner(page, logger)

    card_selectors = [
        'div[class="styles_content__Z7nNB"]',
        'div[class*="styles_card"] a[href*="/review/"]',
        'a[name="business-unit-card"]',
        'a[href*="/review/"]',
    ]

    for sel in card_selectors:
        cards = page.locator(sel)
        count = await cards.count()
        if count == 0:
            continue
        logger.console(f"_open_from_search_results:: {count} cards via '{sel}'.")

        matched_idx = None
        for i in range(min(count, 10)):
            try:
                card_text = (await cards.nth(i).inner_text()).strip()
            except PlaywrightError:
                continue
            if _matches_query(card_text, company_query):
                matched_idx = i
                logger.console(
                    f"_open_from_search_results:: Card #{i} matches '{company_query}': "
                    f"'{card_text[:60]}'"
                )
                break

        if matched_idx is None:
            logger.console(
                f"_open_from_search_results:: {count} cards via '{sel}' but none matched '{company_query}'."
            )
            continue

        try:
            async with page.expect_navigation(wait_until="domcontentloaded", timeout=30_000):
                await cards.nth(matched_idx).click()
            await verify_check(page, task, 8)
        except PlaywrightError as _ce:
            logger.console(f"_open_from_search_results:: Card click/navigation failed ({_ce}); trying next selector.")
            continue

        if await _validate_company_page(page, logger, company_query):
            logger.console(f"_open_from_search_results:: Search result card validated for '{company_query}'.")
            return True

        raise RuntimeError(
            f"_open_from_search_results:: Card text matched '{company_query}' but landed on "
            f"{page.url} — refusing to continue on wrong company."
        )

    return False


async def _open_from_search_results(page, logger, company_query, multiplier_delay, task):
    """Robustly open the company card matching company_query from a search results page.

    Strategy:
      1. Multi-selector + bidirectional text-matching scan of cards on the current page (3 tries).
      2. Query-variant fallback (strips '.com', 'www.', etc.) via fresh search-URL navigation.
    Raises RuntimeError with diagnostics only when every selector and every variant is exhausted.
    """
    # Phase 1: scan whatever the current results page already shows
    for attempt in range(3):
        if await _find_and_open_matching_card(page, logger, company_query, task):
            return
        logger.console(
            f"_open_from_search_results:: No matching card yet (attempt {attempt + 1}/3); waiting..."
        )
        await page.wait_for_timeout(2_000 * multiplier_delay)
        await verify_check(page, task, 8)

    # Phase 2: try the literal query as a direct search URL (handles stale results page)
    search_url = f"https://www.trustpilot.com/search?query={urllib.parse.quote_plus(company_query)}"
    logger.console(f"_open_from_search_results:: Navigating directly to {search_url}")
    await safe_goto(page, search_url, logger=logger)
    await dismiss_cookie_banner(page, logger)
    await page.wait_for_timeout(int(1_500 * multiplier_delay))
    if await _find_and_open_matching_card(page, logger, company_query, task):
        return

    # Phase 3: query variants
    variants = _query_variants(company_query)
    for variant in variants:
        if variant == company_query:
            continue
        variant_url = f"https://www.trustpilot.com/search?query={urllib.parse.quote_plus(variant)}"
        logger.console(f"_open_from_search_results:: Fallback query '{variant}' at {variant_url}")
        await safe_goto(page, variant_url, logger=logger)
        await dismiss_cookie_banner(page, logger)
        await page.wait_for_timeout(int(1_500 * multiplier_delay))
        if await _find_and_open_matching_card(page, logger, company_query, task):
            logger.console(f"_open_from_search_results:: Fallback query '{variant}' succeeded.")
            return
        logger.console(f"_open_from_search_results:: Fallback query '{variant}' did not yield a match.")

    await logger.warning(
        f"No exact company match found for '{company_query}' after exhausting all "
        f"card selectors and query variants. Skipping. Final URL: {page.url}"
    )
    logger.console(f"_open_from_search_results:: Skipping unmatched query '{company_query}' — scenario continues.")


async def _stale_session_reset(page, logger, company_query, stale_url, multiplier_delay, task):
    """Navigate away from a stale inherited tab and open the correct company page.

    Called when open_company_page detects the active tab URL does not match the
    current task's target. Navigates to a fresh search URL, dismisses any cookie
    banner, waits for the React SPA to render cards, then delegates to
    _open_from_search_results. The brief wait prevents Phase 1 from running
    before cards hydrate and making a redundant second navigation trip.
    """
    search_url = f"https://www.trustpilot.com/search?query={urllib.parse.quote_plus(company_query)}"
    await logger.warning(
        f"Stale tab detected — current URL: '{stale_url}', target: '{company_query}'. "
        f"Resetting to search: {search_url}"
    )
    await safe_goto(page, search_url, logger=logger)
    await dismiss_cookie_banner(page, logger)
    await page.wait_for_timeout(int(1_500 * multiplier_delay))
    await _open_from_search_results(page, logger, company_query, multiplier_delay, task)


async def open_company_page(
        page,
        company_query,
        search_with_filters: Optional[bool] = False,
        company_reviews_from: Optional[int] = 1,
        company_reviews_to: Optional[int] = 99999,
        company_rating_from: Optional[int] = 1,
        company_rating_to: Optional[int] = 5,
        multiplier_delay: Optional[float] = 1.0,
        maximum_review_pages: Optional[int] = 5,
        restricted_queries: Optional[list] = None,
        task=None
    ):
    logger = TaskLogger(task)
    DEBUG = os.environ.get("DEBUG", "False").lower() == "true"
    DEBUG_DELAY = int(os.environ.get("DEBUG_DELAY", 1000))

    # Page-state detection: choose the right branch before touching any search input.
    # Critical: an existing /review/ or /evaluate/ page may be stale state from a previous
    # task (Multilogin reuses tabs across sessions). Validate against the current target
    # before accepting any URL as "already on the right page".
    current_url = page.url
    if "/review/" in current_url:
        if await _validate_company_page(page, logger, company_query):
            logger.console(f"open_company_page:: Already on correct review page ({current_url}); no search needed.")
            return
        await _stale_session_reset(page, logger, company_query, current_url, multiplier_delay, task)
        return
    if "/evaluate/" in current_url:
        if await _validate_company_page(page, logger, company_query):
            logger.console(f"open_company_page:: Already on correct evaluate page ({current_url}); no search needed.")
            return
        await _stale_session_reset(page, logger, company_query, current_url, multiplier_delay, task)
        return
    if "/search" in current_url:
        logger.console(f"open_company_page:: Already on search results page ({current_url}); using results directly.")
        await _open_from_search_results(page, logger, company_query, multiplier_delay, task)
        return
    if "trustpilot.com" not in current_url:
        logger.console(f"open_company_page:: Unexpected URL ({current_url}); proceeding with search anyway.")
    else:
        logger.console(f"open_company_page:: Homepage detected; using search input for: {company_query}")

    # If company filters do not specified, use default search query method. Otherwise, see all search result page and open first result
    if not search_with_filters:
        try:
            for attempt in range(3):
                # 1. Check wether the search input is accessible
                query_filled = False
                try:
                    await page.wait_for_load_state("domcontentloaded")
                    logger.console(f"doomcontentloaded state loaded for company search, attempt {attempt + 1}/3.")

                except PlaywrightError as e:
                    await logger.warning(f"Page load state error during company search, attempt {attempt + 1}/3: {e}")
                    await page.wait_for_timeout(2500 * multiplier_delay)
                    continue

                # 2. Resolve search input to a single, stable element
                query_input = await _resolve_search_input(page, logger)
                await verify_check(page, task, 8)

                if await query_input.count() == 0:
                    await logger.warning(f"Query input not found, attempt {attempt + 1}/3. Retrying...")
                    await page.wait_for_timeout(2500 * multiplier_delay)
                    continue

                elif await query_input.count() > 0:
                    logger.console("Query input found, proceeding with search...")
                    await query_input.wait_for(state='visible')

                    try:
                        for query_attempt in range(3):
                            await query_input.fill(company_query)
                            logger.console(f"Filled query input with: {company_query}, waiting...")
                            await page.wait_for_timeout(DEBUG_DELAY if DEBUG else random.randint(2000, 5000) * multiplier_delay)

                            input_val = await query_input.input_value()
                            logger.console(f'query_input == "{input_val}" == "{company_query}"')

                            if input_val != company_query:
                                await page.wait_for_timeout(DEBUG_DELAY if DEBUG else random.randint(2000, 5000) * multiplier_delay)
                                logger.console(f'query_input == "{input_val}" != "{company_query}"')
                                await logger.warning(f"open_company_page:: Query input do not match with query request! Attempt {query_attempt + 1}/3. Retrying...")
                                continue
                            elif input_val == company_query:
                                query_filled = True
                                break
                            elif query_attempt == 2:
                                err_msg = f"Failed to fill intoquery input after 3 attempts."
                                await logger.error(err_msg)
                                raise RuntimeError(f"{err_msg}")

                        # Check wether here are suggestions, if not it possibly will be verify check

                    except PlaywrightError as e:
                        err_msg = f"Failed to fill into query input: {e}"
                        await logger.error(err_msg, exc=e)
                        raise RuntimeError(f"{err_msg}")

                elif await query_input.count() == 0 and attempt == 2:
                    await logger.error(f'Query input was not found, exiting!')
                    raise RuntimeError(f'Query input was not found, exiting!')

                if query_filled:
                    break

            # Choose suggestions, first match
            for attempt in range(3):
                suggestion_input = 'a[name="search-suggestion"]'

                # On the first attempt, wait for the autocomplete dropdown to
                # actually appear in the DOM before checking its count.
                # Trustpilot is a heavy SPA — domcontentloaded fires before the
                # autocomplete JS attaches, so a count-check immediately after
                # fill() often falsely returns 0 during normal hydration.
                if attempt == 0:
                    try:
                        await page.wait_for_selector(suggestion_input, timeout=5_000)
                    except PlaywrightError:
                        pass  # Not visible yet; fallback logic below handles it.

                if await page.locator(suggestion_input).count() == 0:
                    await logger.warning(f"Search suggestions not found, attempt {attempt + 1}/3. Retrying...")
                    await page.wait_for_timeout(2500 * multiplier_delay)

                    await verify_check(page, task, 8)

                    if attempt == 2:
                        await logger.warning(
                            f"Search suggestions absent after 3 attempts — trying 'Show all results' for: {company_query}"
                        )
                        all_results = '//span[text()="Show all results"]'
                        try:
                            await page.locator(all_results).wait_for(state='visible', timeout=15_000)
                            await page.locator(all_results).click()
                            await page.wait_for_load_state("domcontentloaded")
                        except PlaywrightError as _show_all_err:
                            # Button didn't render (search may not have fired, or
                            # the DOM structure changed). Fall back to a direct
                            # search URL so _open_from_search_results can handle
                            # card selection without raising RuntimeError here.
                            await logger.warning(
                                f"'Show all results' not available ({_show_all_err}); "
                                f"falling back to direct search URL for: {company_query}"
                            )
                            fallback_url = f"https://www.trustpilot.com/search?query={urllib.parse.quote_plus(company_query)}"
                            await safe_goto(page, fallback_url, logger=logger)
                            await _open_from_search_results(page, logger, company_query, multiplier_delay, task)
                            return

                        first_match = 'div[class="styles_content__Z7nNB"]'
                        for first_match_attempt in range(3):
                            if await page.locator(first_match).count() == 0:
                                await logger.warning(
                                    f"First match card not found on 'Show all results' page "
                                    f"(attempt {first_match_attempt + 1}/3)."
                                )
                                await page.wait_for_timeout(int(3_000 * multiplier_delay))
                                await verify_check(page, task, 8)
                                if first_match_attempt == 2:
                                    await logger.warning(
                                        f"No card found on 'Show all results' page; falling back to search URL."
                                    )
                                    fallback_url = f"https://www.trustpilot.com/search?query={urllib.parse.quote_plus(company_query)}"
                                    await safe_goto(page, fallback_url, logger=logger)
                                    await _open_from_search_results(page, logger, company_query, multiplier_delay, task)
                                    return
                            else:
                                await page.locator(first_match).first.click()
                                await page.wait_for_load_state("domcontentloaded")
                                if await _validate_company_page(page, logger, company_query):
                                    await logger.info(
                                        f"Opened company page via 'Show all results': {company_query}"
                                    )
                                else:
                                    fallback_url = f"https://www.trustpilot.com/search?query={urllib.parse.quote_plus(company_query)}"
                                    await safe_goto(page, fallback_url, logger=logger)
                                    await _open_from_search_results(page, logger, company_query, multiplier_delay, task)
                                return

                    continue

                else:
                    # Autocomplete suggestions visible — find the best match for company_query
                    suggestions = page.locator(suggestion_input)
                    suggestion_count = await suggestions.count()
                    matched_idx = None
                    query_norm = company_query.lower().replace(" ", "")
                    for i in range(suggestion_count):
                        try:
                            text = (await suggestions.nth(i).inner_text()).strip()
                            text_norm = text.lower().replace(" ", "")
                            if query_norm in text_norm or text_norm in query_norm:
                                matched_idx = i
                                logger.console(
                                    f"Search suggestion match found: '{text}' for query '{company_query}'."
                                )
                                break
                        except PlaywrightError:
                            continue

                    if matched_idx is None:
                        logger.console(
                            f"No safe suggestion match for '{company_query}'; falling back to Enter key."
                        )
                        try:
                            async with page.expect_navigation(wait_until="domcontentloaded", timeout=30_000):
                                await query_input.press('Enter')
                        except PlaywrightError:
                            await page.wait_for_load_state("domcontentloaded")
                    else:
                        try:
                            async with page.expect_navigation(wait_until="domcontentloaded", timeout=30_000):
                                await suggestions.nth(matched_idx).click()
                        except PlaywrightError as _click_err:
                            logger.console(
                                f"Suggestion click/navigation blocked ({_click_err}); pressing Enter as fallback."
                            )
                            try:
                                async with page.expect_navigation(wait_until="domcontentloaded", timeout=30_000):
                                    await query_input.press('Enter')
                            except PlaywrightError:
                                await page.wait_for_load_state("domcontentloaded")

                    await verify_check(page, task, 8)

                    # Validate the opened page matches the intended company
                    if not await _validate_company_page(page, logger, company_query):
                        search_url = f"https://www.trustpilot.com/search?query={urllib.parse.quote_plus(company_query)}"
                        logger.console(
                            f"open_company_page:: Mismatch; navigating to correct search: {search_url}"
                        )
                        await safe_goto(page, search_url, logger=logger)
                        await _open_from_search_results(page, logger, company_query, multiplier_delay, task)

                    logger.console(f"Opened company page for query: {company_query} 🏢")
                    break

        except PlaywrightError as e:
            err_msg = f"open_company_page:: Failed to search for company {company_query} on Trustpilot: {e}"
            await logger.error(err_msg, exc=e)
            raise RuntimeError(f"{err_msg}")

    else:
        try:
            query_input = await _resolve_search_input(page, logger)
            await query_input.wait_for(state='visible')
            await page.wait_for_load_state("domcontentloaded")
            await query_input.type(company_query)
            await page.wait_for_timeout(DEBUG_DELAY if DEBUG else random.randint(2000, 4000) * multiplier_delay)

            # See all results
            await page.locator('//button[@name="Autocomplete Submit"]').wait_for(state='visible')
            await page.locator('//button[@name="Autocomplete Submit"]').click()
            await page.wait_for_load_state("domcontentloaded")
            logger.console(f"Opened company page for query: {company_query} 🏢")

            # Scan reviews to get first matching company with specified filters
            companies = page.locator('div[class="CDS_Card_card__146e7a styles_card__WMwue"]')
            companies_count = await companies.count()

            if companies_count == 0:
                await logger.warning(f"No companies found for query: {company_query} with applied filters, skipping... 🏃🏿‍➡️")
                return

            # Iterate pages
            for p in range(maximum_review_pages):

                # Iterate reviews per page
                for _ in range(companies_count):
                    company = companies.nth(_)

                    # Get company name
                    name = await company.locator('p[class="CDS_Typography_appearance-default__da5bc0 CDS_Typography_prettyStyle__da5bc0 CDS_Typography_heading-s__da5bc0"]').inner_text()
                    logger.console(f"Checking Company \"{name}\" for query {company_query} with applied filters... 🔍")

                    # Get company rating
                    rating_value = await company.locator('span[class="CDS_Typography_appearance-subtle__da5bc0 CDS_Typography_prettyStyle__da5bc0 CDS_Typography_body-m__da5bc0 CDS_Typography_weight-heavy__da5bc0 styles_trustScore__mmIUZ"]').inner_text()
                    logger.console(f"Company \"{name}\" has rating {rating_value} ⭐") if DEBUG else None

                    # Get reviews count
                    reviews_count = await company.locator('span[class="styles_reviewCount__iwsDS styles_underline__3kG9x"]').inner_text()
                    reviews_count = reviews_count.split()[0].replace(",", "")
                    logger.console(f"Company \"{name}\" has {reviews_count} reviews 📝") if DEBUG else None

                    if restricted_queries and name in restricted_queries:
                        logger.console(f"Company \"{name}\" is in restricted queries list, skipping... 🚫") if DEBUG else None
                        await logger.warning(f"Company \"{name}\" is in restricted queries list, skipping... 🚫")
                        continue

                    if rating_value is not None and reviews_count is not None:
                        if (company_reviews_from <= int(reviews_count) <= company_reviews_to):
                            if (company_rating_from <= float(rating_value) <= company_rating_to):
                                await company.locator('a').click()
                                await page.wait_for_load_state("domcontentloaded")
                                await page.wait_for_timeout(DEBUG_DELAY if DEBUG else random.randint(2000, 4000) * multiplier_delay)
                                logger.console(f"Opened company page for query: {company_query} 🏢 with filters match ✅")
                                return
                            else:
                                logger.console(f"Company \"{name}\"::{rating_value} doesn't match rating filter {company_rating_from}-{company_rating_to}, skipping... 🏃🏿‍➡️") if DEBUG else None
                        else:
                            logger.console(f"Company \"{name}\"::{reviews_count} doesn't match reviews count filter {company_reviews_from}-{company_reviews_to}, skipping... 🏃🏿‍➡️") if DEBUG else None
                    else:
                        logger.console(f"Could not get rating or reviews count for Company \"{name}\", skipping... 🏃🏿‍➡️") if DEBUG else None
                pagination = page.locator('//nav[@aria-label="Pagination"]')
                next_page = pagination.locator('//a[@rel="next" and @href]')

                if await next_page.count() > 0:
                    await next_page.click()
                    logger.console("Navigated to the next review page. ⏭️") if DEBUG else None
                    await page.wait_for_load_state("domcontentloaded")
                else:
                    logger.console("No more review pages available, stopping pagination. 🏁 ") if DEBUG else None
                    break

        except PlaywrightError as e:
            err_msg = f"open_company_page:: Failed to search for company {company_query} on Trustpilot: {e}"
            await logger.error(err_msg, exc=e)
            raise RuntimeError(f"{err_msg}")


def _debug_artifact_path(prefix: str, ext: str) -> Path:
    debug_dir = BASE_DIR / "debug"
    debug_dir.mkdir(parents=True, exist_ok=True)
    stamp = datetime.now().strftime("%Y%m%d_%H%M%S")
    return debug_dir / f"{prefix}_{stamp}.{ext}"

async def log_playwright_error(page, error, step: str, **context):
    logger = TaskLogger(task=None)
    logger.console(f"❌ Step failed: {step}")
    logger.console(f"❌ Error type: {type(error).__name__}")
    logger.console(f"❌ Error: {error}")

    if context:
        logger.console(f"📌 Context: {context}")

    try:
        logger.console(f"🌐 URL: {page.url}")
        logger.console(f"📄 Page closed: {page.is_closed()}")
        logger.console(f"📝 Title: {await page.title() if not page.is_closed() else 'N/A'}")

        if not page.is_closed():
            screenshot_path = _debug_artifact_path("playwright_error", "png")
            html_path = _debug_artifact_path("playwright_error", "html")

            await page.screenshot(path=str(screenshot_path), full_page=True)
            html = await page.content()
            html_path.write_text(html, encoding="utf-8")

            logger.console(f"📸 Screenshot saved: {screenshot_path}")
            logger.console(f"🧾 HTML saved: {html_path}")
    except Exception as debug_error:
        logger.console(f"⚠️ Failed to collect debug artifacts: {debug_error}")


async def read_company_reviews(
        page,
        method: Optional[str] = "see_all",
        interactions_count_limit: Optional[int] = 0,
        interactions_count_current: Optional[int] = 0,
        maximum_interactions: Optional[int] = 1,
        reviews_from: Optional[int] = 3,
        reviews_to: Optional[int] = 5,
        multiplier_delay: Optional[float] = 1.0,
        task=None
    ):
    logger = TaskLogger(task)

    if page.is_closed():
        await logger.warning("read_company_reviews:: page is already closed at entry.")
        raise TargetClosedError("Page closed before read_company_reviews started")

    # The function purpose to read some reviews on company page with human-like delays via function reading_delay_ms() and make "Useful" interactions on some of them based on provided limits, to simulate user behavior of reading and interacting with reviews.

    # For development / debug purposes
    DEBUG = os.environ.get("DEBUG", "False").lower() == "true"
    DEBUG_DELAY = int(os.environ.get("DEBUG_DELAY", 1000))

    useful_indexes = None
    maked_interactions = 0
    useful_clicks = 0
    actual_reviews_count = random.randint(reviews_from, reviews_to)
    reviews_count = range(actual_reviews_count)

    if interactions_count_limit != 0:
        # Globaly how much interactions left
        useful_clicks = min((interactions_count_limit - interactions_count_current), maximum_interactions, actual_reviews_count)
        # Randomize on which reviews we will click "Useful", if interactions limit is less than reviews count
        useful_indexes = set(random.sample(range(actual_reviews_count), k=useful_clicks))

    try:
        # for randomizaation purposes there are 2 methods:
        # -- "Based on these reviews" slider
        # -- "See all .." button
        logger.console(f"Userful interactions reviews indexes for this session: {useful_indexes}")
        if method == "slider":
            try:
                await page.wait_for_load_state("domcontentloaded")
                section = page.locator('//h3[text()="Reviews shaping this summary"]')
                await section.wait_for(state="visible", timeout=15_000)
                await section.evaluate("el => el.scrollIntoView({behavior: 'smooth', block: 'center'})")
            except TargetClosedError:
                await logger.warning("read_company_reviews:: page closed while loading slider section.")
                raise
            except PlaywrightError as e:
                await logger.error(f"Failed to find or scroll to 'Reviews shaping this summary' section: {e}", exc=e)
                return maked_interactions

            for _ in reviews_count:
                slider_reviews_path = page.locator('article[class="styles_carouselReviewCard__YIr6O styles_reviewCard__meSdm"]')
                review = page.locator(slider_reviews_path).nth(_)
                await review.wait_for(state="visible", timeout=15_000)

                await review.evaluate("el => el.scrollIntoView({behavior: 'smooth', block: 'center'})")

                logger.console(f"Review #{_} is scrolled into view, waiting before interaction... ⏳")

                await page.wait_for_timeout(DEBUG_DELAY if DEBUG else random.randint(2000, 4000)*multiplier_delay)
                await page.locator(slider_reviews_path).nth(_).click()
                logger.console(f"Opened review #{_} dialog, waiting before interaction... ⏳")

                await page.wait_for_timeout(DEBUG_DELAY if DEBUG else random.randint(25_000, 50_000)*multiplier_delay)

                # Userful / Like iteraction
                if useful_indexes is not None and _ in useful_indexes:
                    await page.locator("//div[@role='dialog']//button[@name='find-useful']").click()
                    await page.wait_for_timeout(DEBUG_DELAY if DEBUG else random.randint(1_000, 2_500)*multiplier_delay)
                    maked_interactions += 1
                    logger.console(f"Clicked 'Useful' on slider review #{_} 👍")

                for attempt in range(3):
                    if await page.locator('button[aria-label="Close"]').count() == 0:
                        await logger.warning(f"Close button not found for review #{_} dialog, attempt {attempt + 1}/3. Retrying...")
                        await page.wait_for_timeout(1500 * multiplier_delay)

                        if attempt == 2:
                            await logger.warning(f"Close button not found for review #{_} dialog after 3 attempts, skipping close... 🚫")

                            return maked_interactions

                        continue

                    else:
                         break

                await page.locator('button[aria-label="Close"]').click()
                logger.console(f"Closed review #{_} dialog ❌")

                await page.wait_for_timeout(DEBUG_DELAY if DEBUG else random.randint(5_000, 15_000)*multiplier_delay)

        elif method == "see_all":
            try:
                await page.wait_for_load_state("domcontentloaded", timeout=15_000)
                btn = page.locator('button[name="See all reviews"]')
                await btn.wait_for(state="visible", timeout=15_000)
                await btn.click()
            except TargetClosedError:
                await logger.warning("read_company_reviews:: page closed while waiting for 'See all reviews' button.")
                raise
            except PlaywrightError as e:
                await logger.warning(
                    f"read_company_reviews:: 'See all reviews' button not ready, skipping query: {e}"
                )
                return maked_interactions
            logger.console("Clicked 'See all reviews' button, waiting for reviews to load")

            for _ in reviews_count:
                await page.locator('div[data-testid="service-review-card-v2"]').nth(_).scroll_into_view_if_needed()
                logger.console(f"Review #{_} is scrolled into view, reading... ⏳")

                await page.wait_for_timeout(DEBUG_DELAY if DEBUG else random.randint(25_000, 50_000)*multiplier_delay)

                # Userful / Like iteraction
                if useful_indexes is not None and _ in useful_indexes and interactions_count_current < interactions_count_limit:
                    await page.locator('//div[@data-testid="service-review-card-v2"]//button[@name="find-useful"]').nth(_).click()
                    await page.wait_for_timeout(DEBUG_DELAY if DEBUG else random.randint(2_000, 5_000)*multiplier_delay)
                    maked_interactions += 1
                    logger.console(f"Clicked 'Useful' on see all review #{_} 👍")

    except TargetClosedError:
        await logger.warning("read_company_reviews:: page closed unexpectedly during review reading.")
        raise
    except PlaywrightError as e:
        await log_playwright_error(
                page,
                e,
                "read_company_reviews",
                method=method,
                reviews_from=reviews_from,
                reviews_to=reviews_to,
                multiplier_delay=multiplier_delay,
            )
        await logger.error(f"read_company_reviews:: Error during reading company reviews: {e}", exc=e)
        return maked_interactions

    return maked_interactions


def reading_delay_ms(
    text: str,
    wpm: int = 220,                 # Expected reading speed in words per minute.
                                    # Lower value = longer delay, higher value = shorter delay.
                                    # 220 is a reasonable conservative default for normal text.

    min_ms: int = 5_000,            # Minimum delay in milliseconds.
                                    # Protects very short texts from disappearing too quickly.

    max_ms: int = 60_000,           # Maximum delay in milliseconds.
                                    # Prevents very long texts from creating an excessively long wait.

    buffer_ms: int = 1_500,         # Extra fixed time added on top of calculated reading time.
                                    # Useful for initial orientation: user notices the text,
                                    # focuses on it, and starts reading.

    comprehension_factor: float = 1.1,  # Multiplier applied to the pure reading-time estimate.
                                        # Adds a small safety margin for pauses, punctuation,
                                        # rereading, or slightly slower reading in real UI usage.
) -> int:
    words = len(text.split())
    read_ms = (words / wpm) * 60_000
    total_ms = buffer_ms + (read_ms * comprehension_factor)
    return int(min(max_ms, max(min_ms, round(total_ms))))


async def read_company_longer_reviews(
        page,
        longer_review_length: Optional[int] = 100,
        maximum_longer_reviews: Optional[int] = 4,
        maximum_review_pages: Optional[int] = 4,
        multiplier_delay: Optional[float] = 1.0,
        reviews_duration_limit: Optional[int] = 5 * 60_000,
        task=None
    ):
    logger = TaskLogger(task)
    # The function purpose to search and filter long reviws on company page, to simulate user behavior of reading reviews using
    # human-like delays which calculates via function reading_delay_ms() based on review text length.

    maximum_review_pages = maximum_review_pages if maximum_review_pages is not None else 4
    maximum_longer_reviews = maximum_longer_reviews if maximum_longer_reviews is not None else 4
    reviews_duration_limit = reviews_duration_limit if reviews_duration_limit is not None else 5 * 60_000

    # For development / debug purposes
    DEBUG = os.environ.get("DEBUG", "False").lower() == "true"
    DEBUG_DELAY = int(os.environ.get("DEBUG_DELAY", 1_000))

    start_time = time.perf_counter()
    reviews_path = 'div[class="styles_cardWrapper__g8amG styles_show__Z8n7u"] p'

    try:
        await page.locator('button[name="See all reviews"]').click()
        await page.wait_for_timeout(DEBUG_DELAY if DEBUG else random.randint(500, 2_500)*multiplier_delay)

        longer_reviews_count = 0

        # Review pages
        for p in range(maximum_review_pages):
            reviews = page.locator(reviews_path)
            await reviews.first.wait_for(state="visible", timeout=15_000)
            reviews_count = await reviews.count()

            logger.console(f"Page {p+1}: Found {reviews_count} reviews, checking for longer reviews... 🔍")

            for _ in range(reviews_count):
                # Calculate average time spent per page and check if enough time remains
                elapsed_time = time.perf_counter() - start_time
                avg_time_per_page = elapsed_time / (p + 1)
                remaining_time = reviews_duration_limit - elapsed_time

                if avg_time_per_page > remaining_time:
                    logger.console(f"Not enough time for next page. Avg: {avg_time_per_page:.0f}s, Remaining: {remaining_time:.0f}s. Breaking cycle. 🏁")
                    break
                # Check global limit
                if time.perf_counter() - start_time > reviews_duration_limit:
                    logger.console(f"Reached reviews duration limit of {reviews_duration_limit} ms, stopping interactions. 🏁")
                    break

                review_text = await reviews.nth(_).inner_text()
                logger.console(f"Current review #{_} contains text 🗒️ {' '.join(review_text.split()[:4])} ... ")

                # Scroll into review
                review = reviews.nth(_)
                await review.scroll_into_view_if_needed()
                await page.wait_for_timeout(DEBUG_DELAY if DEBUG else random.randint(500, 2_500)*multiplier_delay)
                logger.console(f"Review #{_} is scrolled into view, waiting before interaction... ⏳")

                # Check review length
                review_text = await review.inner_text()
                if len(review_text.split()) < longer_review_length:
                    # await page.wait_for_timeout(DEBUG_DELAY if DEBUG else random.randint(300, 700)*multiplier_delay)
                    await page.wait_for_timeout(100)
                    logger.console(f"Review #{_} is too short ({len(review_text.split())} words), skipping... 🏃🏿‍➡️")
                    continue

                # If review is longer than specified, delay based on review length
                delay = DEBUG_DELAY if DEBUG else reading_delay_ms(review_text) * multiplier_delay
                logger.console(f"Review #{_} has {len(review_text.split())} words, waiting for {delay} ms ⏳ to simulate reading... 👀")

                await page.wait_for_timeout(delay)
                longer_reviews_count += 1

                # Stop if we have already interacted with enough longer reviews
                if longer_reviews_count > maximum_longer_reviews:
                    logger.console(f"Reached maximum longer reviews limit of {maximum_longer_reviews}, stopping interactions. 🏁")
                    return time.perf_counter() - start_time

            # Pagination, verify if there is a next page and click on it, otherwise stop
            pagination = page.locator('//nav[@aria-label="Pagination"]')
            next_page = pagination.locator('//a[@rel="next" and @href]')

            if await next_page.count() > 0:
                first_review = reviews.first
                old_text = await first_review.inner_text()

                logger.console("Navigating to the next review page. ⏭️")
                await next_page.click()
                await page.wait_for_timeout(DEBUG_DELAY if DEBUG else random.randint(500, 2_500)*multiplier_delay)
                await page.wait_for_function(
                    """
                    previousText => {
                        const el = document.querySelector('div[class="styles_cardWrapper__g8amG styles_show__Z8n7u"] p');
                        return el && el.innerText !== previousText;
                    }
                    """,
                    arg=old_text,
                    timeout=10_000,
                )
                new_text = await first_review.inner_text()
                logger.console("Next review page loaded successfully. ✅")

            else:
                logger.console("No more review pages available, stopping pagination. 🏁 ")
                break

        logger.console(f"Finished reading reviews. Total longer reviews read: {longer_reviews_count} 📚")
    except PlaywrightError as e:
        await log_playwright_error(
                        page,
                        e,
                        "read_company_longer_reviews",
                        multiplier_delay=multiplier_delay,
                    )
        await logger.error(f"read_company_longer_reviews:: Failed to read company reviews: {e}", exc=e)

    return time.perf_counter() - start_time


# Get token, start browser and connect to it, return page object for further actions
# DEPRECATED: moved to BrowserSession — kept for backward compatibility during migration
async def prepare_playwright(task):
    logger = TaskLogger(task)
    token = get_token(task)
    if not token:
        await logger.error("Failed to get token. Returning failure.")
        return None, None, None, None

    port = await start_browser(token, task)
    if not port:
        await logger.error("Failed to start browser. Returning failure.")
        return None, None, None, None

    try:
        playwright, browser, context, page = await connect_multilogin(port)
        return playwright, browser, context, page
    except PlaywrightError as e:
        await logger.error(f"Failed to connect to browser: {e}", exc=e)
        return None, None, None, None


async def _verify_review_submission(page, logger, pre_submit_url: str) -> bool:
    """Return True only when there is an unambiguous signal that the review was published.

    Polls up to 8 times with 2.5 s gaps (20 s total budget). Accepted signals (any one is enough):

    Strong (immediate True):
    - page URL changed from the write-review URL,
    - a success/thank-you element is present in the DOM,
    - both the submit button AND the review textarea are gone (form fully removed),
    - submit button is absent from DOM entirely (form replaced after AJAX success).

    Processing (True — server accepted, DOM update still in flight):
    - submit button is present but disabled (Trustpilot disables it on successful AJAX
      submission while it transitions the UI; combined with URL still matching we treat
      this as confirmed to avoid false negatives on slow proxies).

    If none of these fire within the budget, returns False.
    """
    _success_selectors = [
        '[data-testid="review-submitted"]',
        '[class*="reviewSubmitted"]',
        '[class*="successState"]',
        '[class*="success-state"]',
        '[role="status"]',
        '[role="alert"]',
        '//h1[contains(translate(., "ABCDEFGHIJKLMNOPQRSTUVWXYZ", "abcdefghijklmnopqrstuvwxyz"), "thank")]',
        '//h2[contains(translate(., "ABCDEFGHIJKLMNOPQRSTUVWXYZ", "abcdefghijklmnopqrstuvwxyz"), "thank")]',
        '//h1[contains(translate(., "ABCDEFGHIJKLMNOPQRSTUVWXYZ", "abcdefghijklmnopqrstuvwxyz"), "submitted")]',
        '//h2[contains(translate(., "ABCDEFGHIJKLMNOPQRSTUVWXYZ", "abcdefghijklmnopqrstuvwxyz"), "submitted")]',
        '//p[contains(translate(., "ABCDEFGHIJKLMNOPQRSTUVWXYZ", "abcdefghijklmnopqrstuvwxyz"), "review has been")]',
        '//p[contains(translate(., "ABCDEFGHIJKLMNOPQRSTUVWXYZ", "abcdefghijklmnopqrstuvwxyz"), "thanks for your review")]',
        '//div[contains(translate(., "ABCDEFGHIJKLMNOPQRSTUVWXYZ", "abcdefghijklmnopqrstuvwxyz"), "your review is")]',
    ]

    _submit_locator = page.locator('//button[@name="submit-review"]')
    _textarea_locator = page.locator('//textarea[@aria-describedby="review-text-helper-text"]')
    _submit_disabled_locator = page.locator('//button[@name="submit-review" and @disabled]')

    for attempt in range(8):
        await page.wait_for_timeout(2_500)

        current_url = page.url
        if current_url != pre_submit_url:
            logger.console(f"Review submission confirmed: URL changed to {current_url}.")
            return True

        for sel in _success_selectors:
            if await page.locator(sel).count() > 0:
                logger.console(f"Review submission confirmed: success element found ({sel}).")
                return True

        submit_count = await _submit_locator.count()
        form_gone = await _textarea_locator.count() == 0

        if submit_count == 0 and form_gone:
            logger.console("Review submission confirmed: form fully removed from DOM.")
            return True

        if submit_count == 0 and not form_gone:
            logger.console("Review submission confirmed: submit button removed (AJAX success, UI transitioning).")
            return True

        if await _submit_disabled_locator.count() > 0:
            logger.console(
                f"Review submission confirmed: submit button disabled after click "
                f"(server accepted, UI still transitioning). Check {attempt + 1}/8."
            )
            return True

        logger.console(
            f"Submission not yet confirmed (check {attempt + 1}/8, "
            f"submit_in_dom={submit_count > 0}, form_gone={form_gone}); re-checking..."
        )

    logger.console("Review submission NOT confirmed — no success signal detected after 20 s.")
    return False


async def _dismiss_overlays(page, logger):
    """Dismiss modal/drawer overlays that block pointer events.

    Tries Escape key first (skipped on /evaluate/ — Escape cancels the review form there),
    then looks for explicit close buttons.
    Returns True when no blocking overlay is detected afterwards.
    """
    if "/evaluate/" not in page.url:
        await page.keyboard.press('Escape')
        await page.wait_for_timeout(600)

    for close_sel in [
        'button[aria-label="Close"]',
        'button[aria-label="close"]',
        '[data-testid="modal-close"]',
        '[role="dialog"] button[aria-label="Close"]',
        '[role="dialog"] button[aria-label="close"]',
        '[class*="filter"] button[class*="close"]',
        '[class*="filter"] button[aria-label="Close"]',
        '[class*="Modal"] button[class*="close"]',
        '[class*="Drawer"] button[class*="close"]',
    ]:
        btn = page.locator(close_sel)
        if await btn.count() > 0:
            try:
                await btn.first.click()
                await page.wait_for_timeout(400)
                logger.console(f"Overlay dismissed via: {close_sel}")
                break
            except PlaywrightError:
                pass

    overlay = page.locator('[class*="CDS_Modal_overlay"], [class*="overlay__"]')
    still_present = await overlay.count() > 0
    if still_present:
        logger.console("Overlay still detected after dismiss attempts.")
    return not still_present


async def _reach_review_form(page, logger, multiplier_delay):
    """Ensure the browser is showing an open review form.

    Checks current page state first, then tries multiple CTAs in priority order,
    then falls back to direct /evaluate/<domain> navigation.
    Raises RuntimeError (with current URL in the message) if the form cannot be
    reached after all attempts.
    """
    star_selector = '//input[@name="star-selector"]'
    form_selector = '//textarea[@aria-describedby="review-text-helper-text"]'
    evaluate_heading = '//h2[contains(., "How would you rate")]'
    star_animation = '[data-testid="star-selector-animation"]'

    async def _form_open():
        for sel in (star_selector, form_selector, evaluate_heading, star_animation):
            try:
                if await page.locator(sel).count() > 0:
                    return True
            except PlaywrightError:
                pass
        return False

    async def _on_404():
        try:
            return await page.locator('//h1[contains(., "Whoops")]').count() > 0
        except PlaywrightError:
            return False

    async def _wait_for_form_or_evaluate(timeout_ms=5_000):
        """Poll until form appears or URL becomes /evaluate/. Returns 'form', 'evaluate', or 'none'."""
        deadline = time.time() + timeout_ms / 1000
        while time.time() < deadline:
            if await _form_open():
                return "form"
            if "/evaluate/" in page.url:
                return "evaluate"
            await page.wait_for_timeout(300)
        if await _form_open():
            return "form"
        if "/evaluate/" in page.url:
            return "evaluate"
        return "none"

    # Already on the form?
    if await _form_open():
        logger.console("post_review:: Review form already open.")
        return

    # Already on /evaluate/ — dismiss cookie banner, then wait for the form to render
    if "/evaluate/" in page.url:
        logger.console(f"post_review:: Already on evaluate page ({page.url}); waiting for form.")
        await dismiss_cookie_banner(page, logger)
        await page.wait_for_load_state("domcontentloaded")
        await page.wait_for_timeout(int(2_000 * multiplier_delay))
        if await _form_open():
            logger.console("post_review:: Review form found on existing evaluate page.")
            return
        if await _on_404():
            logger.console(f"post_review:: /evaluate/ URL is a 404 page ({page.url}); will attempt URL reconstruction.")
        # Fall through to CTA loop then fallback

    # CTA candidates — target the interactive parent element, not the hidden srOnly span
    cta_candidates = [
        ('//button[.//span[text()="Write a review"]]', 'button wrapping srOnly span'),
        ('//a[.//span[text()="Write a review"]]',      'anchor wrapping srOnly span'),
        ('//a[contains(@href, "/evaluate/")]',          'evaluate href link'),
        ('[data-testid="write-review-button"]',         'data-testid button'),
        ('[data-business-unit-action="write-review"]',  'data-business-unit-action'),
        ('//button[contains(., "Write a review")]',     'button text'),
        ('//a[contains(., "Write a review")]',          'anchor text'),
    ]

    cta_clicked = False
    url_before_cta = page.url

    await dismiss_cookie_banner(page, logger)

    for sel, label in cta_candidates:
        locator = page.locator(sel)
        count = await locator.count()
        if count == 0:
            continue

        # Log element details and viewport state
        try:
            tag = await locator.first.evaluate("el => el.tagName.toLowerCase()")
            cls = await locator.first.evaluate("el => el.className")
            logger.console(f"post_review:: CTA '{label}': <{tag} class='{cls}'> ({count} match(es)).")
            in_viewport = await locator.first.evaluate(
                "el => { const r = el.getBoundingClientRect();"
                " return r.top >= 0 && r.left >= 0"
                " && r.bottom <= window.innerHeight && r.right <= window.innerWidth; }"
            )
            logger.console(f"post_review:: CTA '{label}' in viewport: {in_viewport}.")
            if not in_viewport:
                await locator.first.scroll_into_view_if_needed()
                logger.console(f"post_review:: Scrolled CTA '{label}' into view.")
        except PlaywrightError:
            logger.console(f"post_review:: Trying CTA '{label}' ({count} match(es)).")

        # Dismiss any blocking overlay before the first click attempt
        await _dismiss_overlays(page, logger)

        for attempt in range(3):
            try:
                await locator.first.click(timeout=5_000)
                await page.wait_for_load_state("domcontentloaded")
                logger.console(f"post_review:: CTA '{label}' clicked normally.")
                cta_clicked = True
                break
            except PlaywrightError as _ce:
                logger.console(
                    f"post_review:: CTA click blocked (attempt {attempt + 1}/3): {_ce}; dismissing overlays."
                )
                await _dismiss_overlays(page, logger)
                await page.wait_for_timeout(500)
        else:
            # All pointer-based clicks failed — JS fallback
            try:
                await locator.first.evaluate("el => el.click()")
                await page.wait_for_load_state("domcontentloaded")
                logger.console(f"post_review:: CTA '{label}' clicked via JS fallback.")
                cta_clicked = True
            except PlaywrightError:
                continue  # JS also failed; try next CTA

        url_after = page.url
        logger.console(
            f"post_review:: URL after CTA click: {url_after} "
            f"(changed: {url_after != url_before_cta})."
        )

        # Wait for form to appear or for /evaluate/ redirect
        result = await _wait_for_form_or_evaluate(int(5_000 * multiplier_delay))
        logger.console(
            f"post_review:: Post-click detection result: '{result}'. "
            f"Current URL: {page.url}."
        )

        if result == "evaluate":
            logger.console(f"post_review:: Navigated to /evaluate/ ({page.url}); waiting for form.")
            await page.wait_for_load_state("domcontentloaded")
            await page.wait_for_timeout(int(2_000 * multiplier_delay))
            if await _form_open():
                logger.console(f"post_review:: Review form opened on evaluate page via CTA '{label}'.")
                return

        if result == "form" or await _form_open():
            logger.console(f"post_review:: Review form opened via CTA '{label}'.")
            return

    # No CTA worked — try direct /evaluate/<domain> navigation
    current_url = page.url
    domain = None
    if "/review/" in current_url:
        domain = current_url.split("/review/")[1].split("?")[0].split("/")[0]
    elif "/evaluate/" in current_url and not await _on_404():
        # On /evaluate/ but form never appeared — full reload may fix transient JS failure
        logger.console(f"post_review:: On /evaluate/ but form missing; reloading {current_url}")
        await safe_goto(page, current_url, logger=logger)
        await dismiss_cookie_banner(page, logger)
        await page.wait_for_timeout(int(2_000 * multiplier_delay))
        if await _form_open():
            logger.console("post_review:: Review form reached after evaluate page reload.")
            return

    if domain:
        eval_url = f"https://www.trustpilot.com/evaluate/{domain}"
        logger.console(f"post_review:: All CTAs failed; navigating directly to {eval_url}")
        await safe_goto(page, eval_url, logger=logger)
        await dismiss_cookie_banner(page, logger)
        await page.wait_for_timeout(int(2_000 * multiplier_delay))
        if await _form_open():
            logger.console("post_review:: Review form reached via direct /evaluate/ navigation.")
            return
        # Domain has no TLD (e.g. "similarweb") → /evaluate/similarweb is a 404.
        # Retry with .com appended.
        if "." not in domain:
            eval_url_com = f"https://www.trustpilot.com/evaluate/{domain}.com"
            logger.console(f"post_review:: 404 suspected (no TLD); retrying with .com: {eval_url_com}")
            await safe_goto(page, eval_url_com, logger=logger)
            await dismiss_cookie_banner(page, logger)
            await page.wait_for_timeout(int(2_000 * multiplier_delay))
            if await _form_open():
                logger.console("post_review:: Review form reached via /evaluate/{domain}.com navigation.")
                return

    form_present = await _form_open()
    raise RuntimeError(
        f"post_review:: Could not reach the review form. "
        f"URL: {page.url}; cta_clicked={cta_clicked}; "
        f"form_container_present={form_present}; "
        f"expected_path=/evaluate/, actual_url={page.url}; "
        f"ctas_tried={[label for _, label in cta_candidates]}"
    )


async def _resolve_star_control(page, logger, rating, multiplier_delay, timeout_ms=20_000):
    """Locate a clickable rating control for `rating` stars across Trustpilot UI variants.

    Polls the main frame and every sub-iframe for several possible star widgets.
    Returns a single-element locator. Raises RuntimeError with detailed diagnostics
    on timeout. Each iteration also re-checks for blocking overlays and dismisses them.
    """
    logger.console(f"post_review:: Looking for star control (rating={rating}) on URL: {page.url}")

    # Each entry: (selector, use_nth_indexing).
    # use_nth=True means the selector matches all 5 stars; we pick the (rating-1)th.
    # use_nth=False means the selector is value-specific and already targets this rating.
    star_locators = [
        ('//input[@name="star-selector"]', True),
        (f'//input[@name="star-selector" and @value="{rating}"]', False),
        (f'//label[@for="star-rating-{rating}"]', False),
        ('//label[contains(@class, "star") and @for]', True),
        (f'//button[@name="star-rating" and @value="{rating}"]', False),
        (f'//button[contains(@aria-label, "{rating} star")]', False),
        (f'//button[contains(@aria-label, "{rating} stars")]', False),
        (f'//input[@name="star-rating" and @value="{rating}"]', False),
        (f'//input[@type="radio" and @value="{rating}" and contains(@id, "star")]', False),
        (f'//input[@type="radio" and @value="{rating}"]', False),
        (f'[data-rating-input="{rating}"]', False),
        # '[data-testid*="star"]' removed — too broad; matches rating-display elements on /review/ pages
        # '[aria-label*="{rating} star"]' removed — same issue
    ]

    deadline = time.time() + timeout_ms / 1000
    last_diag = {}

    async def _try_scope(scope, scope_label):
        for selector, use_nth in star_locators:
            try:
                base = scope.locator(selector)
                count = await base.count()
                last_diag[f"{scope_label}::{selector}"] = count
                if count == 0:
                    continue
                target = base.nth(rating - 1) if use_nth else base.first
                if await target.count() > 0:
                    logger.console(
                        f"post_review:: Star control resolved in {scope_label} via "
                        f"'{selector}' (count={count})."
                    )
                    return target
            except PlaywrightError:
                continue
        return None

    while time.time() < deadline:
        # Always check for / dismiss blocking overlays before each scan
        if await page.locator('[class*="CDS_Modal_overlay"], [class*="overlay__"]').count() > 0:
            logger.console("post_review:: Overlay detected during star lookup; dismissing.")
            await _dismiss_overlays(page, logger)

        # 1. Main frame
        result = await _try_scope(page, "main")
        if result is not None:
            return result

        # 2. Sub-iframes (review form sometimes renders inside an embedded frame)
        for frame in page.frames:
            if frame == page.main_frame:
                continue
            result = await _try_scope(frame, f"iframe({frame.url[:40]})")
            if result is not None:
                return result

        await page.wait_for_timeout(int(500 * multiplier_delay))

    # Timeout — gather rich diagnostics for the error message
    form_container_present = False
    for sel in (
        '//textarea[@aria-describedby="review-text-helper-text"]',
        '[data-testid="review-form"]',
        'form[name="review-form"]',
    ):
        try:
            if await page.locator(sel).count() > 0:
                form_container_present = True
                break
        except PlaywrightError:
            continue
    iframe_count = max(0, len(page.frames) - 1)
    modal_present = (
        await page.locator('[class*="CDS_Modal_overlay"], [class*="overlay__"]').count() > 0
    )
    try:
        title = await page.title()
    except Exception:
        title = "<unknown>"

    raise RuntimeError(
        f"post_review:: Star control not found after {timeout_ms}ms. "
        f"URL: {page.url}; Title: '{title}'; "
        f"form_container_present={form_container_present}; "
        f"iframe_count={iframe_count}; modal_present={modal_present}; "
        f"selectors_checked={last_diag}"
    )


async def post_review(
        page,
        review_text,
        review_title,
        rating,
        target_company: Optional[str] = None,
        multiplier_delay: Optional[float] = 1.0,
        task=None,
        skip_preflight: bool = False,
    ):
    logger = TaskLogger(task)

    async def _assert_no_login_modal():
        """Raise immediately if a Trustpilot login/signup modal has taken over the page.

        The modal overlays the evaluate form and leaves /evaluate/ in the URL, so
        _assert_evaluate_url does not catch it. A submit-button click against a
        modal-blocked page produces a misleading 30 s Playwright timeout.
        """
        _auth_selectors = [
            '//h2[contains(., "Log in or sign up")]',
            '//button[contains(., "Continue with Google")]',
            '//button[contains(., "Continue with email")]',
            '//button[contains(., "Sign in with Apple")]',
            '//button[contains(., "Continue with Facebook")]',
        ]
        for sel in _auth_selectors:
            if await page.locator(sel).count() > 0:
                raise SessionExpiredError(
                    f"post_review:: Session expired — Trustpilot login modal detected "
                    f"before submit (matched: '{sel}'). URL: {page.url}"
                )

    def _assert_evaluate_url(step: str, expected_url: str | None = None):
        """Raise immediately if the page has drifted away from the /evaluate/ path.

        First call (expected_url=None): confirms _reach_review_form landed on any
        /evaluate/ URL — no name comparison since the pre-flight already validated
        the company (display names like 'NOIR & PEARL' don't reliably match canonical
        slugs like 'noirandpearl.co.uk').

        Subsequent calls (expected_url set): exact-URL drift guard to catch a
        concurrent task navigating this tab between form steps.
        """
        url = page.url
        if "/evaluate/" not in url:
            raise RuntimeError(
                f"post_review:: URL drift at '{step}' — "
                f"expected /evaluate/..., got {url}"
            )
        if expected_url and url != expected_url:
            raise RuntimeError(
                f"post_review:: URL drift at '{step}' — "
                f"expected {expected_url}, got {url}"
            )

    try:
        # Pre-flight: verify we're on the right company page before opening the form.
        # Skipped when the caller already validated the canonical URL (e.g. SitePage.post_review).
        if skip_preflight:
            logger.console(
                f"post_review:: Pre-flight skipped (canonical URL). URL: {page.url}"
            )
        elif target_company and not await _validate_company_page(page, logger, target_company):
            raise RuntimeError(
                f"post_review:: Page does not match target company '{target_company}' before form entry. "
                f"URL: {page.url}."
            )
        else:
            logger.console(
                f"post_review:: Pre-flight OK. URL: {page.url}. Title: {await page.title()}"
            )

        # Reach the review form — handles all page states, CTAs, and fallbacks.
        await _reach_review_form(page, logger, multiplier_delay)

        # Confirm _reach_review_form landed on an /evaluate/ URL.
        # No name comparison here — display names like 'NOIR & PEARL' don't map
        # reliably to canonical slugs like 'noirandpearl.co.uk'. The pre-flight
        # already validated the company; we only need the page type check.
        logger.console(f"post_review:: URL after _reach_review_form: {page.url}")
        _assert_evaluate_url("after _reach_review_form")
        _evaluate_url = page.url  # canonical slug captured for subsequent drift guards

        # Set rating — discover the star control across Trustpilot UI variants.
        # dismiss_cookie_banner handles the cookie consent dialog on evaluate pages
        # (Escape is skipped there by _dismiss_overlays to avoid cancelling the form).
        await dismiss_cookie_banner(page, logger)
        await _dismiss_overlays(page, logger)
        logger.console(f"post_review:: URL before star resolution: {page.url}")
        _assert_evaluate_url("before star resolution", _evaluate_url)

        star = await _resolve_star_control(page, logger, rating, multiplier_delay)

        star_clicked = False
        for star_attempt in range(3):
            try:
                await star.click(timeout=5_000)
                star_clicked = True
                logger.console(f"post_review:: Star {rating} clicked normally. URL: {page.url}")
                break
            except PlaywrightError as _se:
                logger.console(
                    f"post_review:: Star click blocked (attempt {star_attempt + 1}/3): {_se}; "
                    "dismissing overlays and re-resolving."
                )
                await _dismiss_overlays(page, logger)
                await page.wait_for_timeout(500)
                star = await _resolve_star_control(page, logger, rating, multiplier_delay)

        if not star_clicked:
            try:
                await star.evaluate("el => el.click()")
                star_clicked = True
                logger.console(f"post_review:: Star {rating} clicked via JS fallback.")
            except PlaywrightError as _se2:
                raise RuntimeError(
                    f"post_review:: Star click failed after all retries ({_se2}). URL: {page.url}"
                )

        await page.wait_for_timeout(2_000 * multiplier_delay)

        # Guard before textarea: drift here means a concurrent task navigated the tab.
        logger.console(f"post_review:: URL before textarea: {page.url}")
        _assert_evaluate_url("before textarea", _evaluate_url)

        # Textarea — try multiple locator strategies before giving up.
        textarea_candidates = [
            page.locator('//textarea[@aria-describedby="review-text-helper-text"]'),
            page.locator('textarea[name="review-text"]'),
            page.locator('[data-testid="review-text-input"]'),
            page.locator('textarea[placeholder*="review" i]'),
            page.locator('form[name="review-form"] textarea'),
            page.locator('form textarea'),
        ]
        review_textarea = None
        for loc in textarea_candidates:
            try:
                if await loc.count() > 0:
                    await loc.first.wait_for(state='visible', timeout=5_000)
                    review_textarea = loc.first
                    logger.console(f"post_review:: Textarea located. URL: {page.url}")
                    break
            except PlaywrightError:
                continue

        if review_textarea is None:
            try:
                page_title = await page.title()
                controls = await page.evaluate(
                    "() => Array.from(document.querySelectorAll('input,textarea,select'))"
                    ".map(e => e.tagName+'[type='+e.type+'][name='+e.name"
                    "+'][placeholder='+e.placeholder+']').join('; ')"
                )
            except Exception:
                page_title, controls = "<unknown>", "<unavailable>"
            raise RuntimeError(
                f"post_review:: Review textarea not found after all fallbacks. "
                f"URL: {page.url}; title: '{page_title}'; "
                f"available_controls: {controls[:600]}"
            )

        logger.console("post_review:: Filling review text.")
        await review_textarea.fill(review_text)
        logger.console(f"post_review:: Review text filled ({len(review_text)} chars). URL: {page.url}")
        await page.wait_for_timeout(2_000 * multiplier_delay)

        # Fill review title
        title_input = page.locator('//input[@name="review-title"]')
        await title_input.fill(review_title)
        await page.wait_for_timeout(2_000 * multiplier_delay)

        # Date of experience
        current_date = datetime.now().strftime("%Y-%m-%d")
        await page.locator('//input[@name="date-of-experience"]').fill(current_date)
        await page.wait_for_timeout(2_000 * multiplier_delay)

        # Agree terms
        await page.locator('//input[@data-testid="incentivised-checkbox"]').check()
        await page.wait_for_timeout(2_000 * multiplier_delay)

        # Guard before submit.
        logger.console(f"post_review:: URL before submit: {page.url}")
        _assert_evaluate_url("before submit", _evaluate_url)

        await _assert_no_login_modal()
        pre_submit_url = page.url
        submit_btn = page.locator('//button[@name="submit-review"]')

        # Confirm the button is in the DOM before any interaction — fast fail with a
        # clear message rather than a 30 s Playwright timeout on a missing element.
        try:
            await submit_btn.wait_for(state='attached', timeout=10_000)
        except PlaywrightError:
            raise RuntimeError(
                f"post_review:: Submit button not found in DOM after 10 s. URL: {page.url}"
            )

        # Bring the button into the viewport — long forms can leave it below the fold.
        try:
            await submit_btn.scroll_into_view_if_needed(timeout=5_000)
        except PlaywrightError:
            logger.console("post_review:: Could not scroll submit button into view — proceeding.")

        _vis = await submit_btn.is_visible()
        _ena = await submit_btn.is_enabled()
        logger.console(
            f"post_review:: Submit button found — visible={_vis}, enabled={_ena}. URL: {page.url}"
        )

        submit_clicked = False
        for _attempt in range(3):
            try:
                await submit_btn.click(timeout=12_000)
                submit_clicked = True
                logger.console(f"post_review:: Submit clicked (attempt {_attempt + 1}/3).")
                break
            except PlaywrightError as _se:
                logger.console(
                    f"post_review:: Submit click blocked (attempt {_attempt + 1}/3): {_se}"
                )
                # Re-check session — a modal could have appeared during the wait.
                await _assert_no_login_modal()
                await _dismiss_overlays(page, logger)
                try:
                    await submit_btn.scroll_into_view_if_needed(timeout=3_000)
                except PlaywrightError:
                    pass
                await page.wait_for_timeout(1_500)

        if not submit_clicked:
            try:
                _vis = await submit_btn.is_visible()
                _ena = await submit_btn.is_enabled()
                _in_vp = await submit_btn.evaluate(
                    "el => { const r = el.getBoundingClientRect(); "
                    "return r.top >= 0 && r.left >= 0 "
                    "&& r.bottom <= window.innerHeight && r.right <= window.innerWidth; }"
                )
            except Exception:
                _vis, _ena, _in_vp = "?", "?", "?"
            raise RuntimeError(
                f"post_review:: Submit button click failed after 3 attempts — "
                f"visible={_vis}, enabled={_ena}, in_viewport={_in_vp}. URL: {page.url}"
            )

        # Short fixed pause so the SPA has a moment to react before the first poll.
        # wait_for_load_state("domcontentloaded") is a no-op here — Trustpilot submits
        # via fetch(), not a form POST, so no navigation event is ever fired.
        await page.wait_for_timeout(1_500)

        # Strict confirmation — only count as success when proven.
        confirmed = await _verify_review_submission(page, logger, pre_submit_url)
        if confirmed:
            logger.console("Review submitted and confirmed.")
            task.profile.reviews_count += 1
            post_submit_url = page.url if page.url != pre_submit_url else None
            await sync_to_async(Review.objects.create)(
                profile=task.profile,
                task=task,
                company=target_company or "",
                review_title=review_title,
                review_text=review_text,
                rating=rating,
                review_link=post_submit_url,
                status=Review.Status.SUCCESSFUL,
            )
            return True
        else:
            await logger.error("post_review:: Submit clicked but submission could not be confirmed.")
            return False
    except PlaywrightError as e:
        await logger.error(f"post_review:: Failed to post review: {e}", exc=e)
        return False


async def get_reviews(
        page, rate: Optional[int] = 4,
        count: Optional[int] = 5,
        min_review_length: Optional[int] = 100,
        max_review_length: Optional[int] = 250,
        maximum_review_pages: Optional[int] = 5,
        multiplier_delay: Optional[float] = 1.0,
        task=None
    ):
    logger = TaskLogger(task)
    DEBUG_DELAY = int(os.environ.get("DEBUG_DELAY", 1_000))
    DEBUG = os.environ.get("DEBUG", "False").lower() == "true"
    # Use filters to get reviews with specified rating
    try:
        # Enable filters (button may be absent on some page states — skip gracefully)
        p_more_filters = '//button[@name="filter"]'
        if await page.locator(p_more_filters).count() > 0:
            await page.locator(p_more_filters).scroll_into_view_if_needed()
            await page.wait_for_timeout(1_500)
            await page.locator(p_more_filters).click()
            await page.wait_for_timeout(2_500)
        else:
            logger.console("Filter button not found; collecting unfiltered reviews.")

        # Get company details name / url
        title = await page.locator('#business-unit-title h1 span').first.text_content()
        href = await page.locator('a[data-visit-website-button-link="true"]').nth(1).get_attribute("href")
        reviews = []

        rate_filter = page.locator(f'//div[@data-rating-filter-button-toggle="true"]//input[@value="{rate}"]')
        if await rate_filter.count() > 0 and await rate_filter.is_enabled():
            await rate_filter.click()
            await page.locator('//button[@data-show-reviews-button="true"]').click()
            await page.wait_for_timeout(int(2_500 * multiplier_delay))
        else:
            logger.console(f"Rating {rate} filter not found or disabled, clicking reset filters.")
            reset_btn = page.locator('//button[@name="reset-filters"]')
            if await reset_btn.count() > 0 and await reset_btn.is_enabled():
                await reset_btn.click()
                await page.wait_for_timeout(int(2_500 * multiplier_delay))

                logger.console(f"Company details collected: name - {title}, url - {href}")
                return title, href.split("?")[0], reviews

        # Get reviews
        for _ in range(maximum_review_pages):
            logger.console(f"Collecting reviews with rating {rate}⭐ from page {_+1}... 🔍")
            # Gather reviews with specified rating and length filters, 20 per page
            p_reviews = '//p[@data-service-review-text-typography="true"]'
            reviews_locator = page.locator(p_reviews)
            await reviews_locator.first.wait_for(state="visible", timeout=20_000)
            count_on_page = await reviews_locator.count()

            for i in range(count_on_page):
                logger.console(f"Checking review #{i+1} on page {_+1} for rating {rate}... 🔍")
                review = await page.locator(p_reviews).nth(i).text_content()
                if len(review.split()) < min_review_length or len(review.split()) > max_review_length:
                    logger.console(f"Review #{i+1} :: {review[:50]}... with rating {rate}⭐ is skipped due to length ({len(review.split())} words). 🏃🏿‍➡️")
                    continue
                reviews.append(review)
                logger.console(f"Got review #{i+1} with rating {rate} ✅ : {review[:100]}...")

                if len(reviews) == count:
                    logger.console(f"Got required {count} reviews with rating {rate}⭐, stopping. 🏁")
                    logger.console(f"Reviews: {reviews}")
                    return title, href.split("?")[0], reviews

            first_review = page.locator(p_reviews).first
            old_text = await first_review.inner_text()

            # Pagination, verify if there is a next page and click on it, otherwise stop
            pagination = page.locator('//nav[@aria-label="Pagination"]')
            next_page = pagination.locator('//a[@rel="next" and @href]')

            if await next_page.count() > 0:
                logger.console("Navigating to the next review page. ⏭️")
                try:
                    await next_page.click()
                    await page.wait_for_timeout(DEBUG_DELAY if DEBUG else random.randint(500, 2_500)*multiplier_delay)
                    await page.wait_for_function(
                        """
                        previousText => {
                            const el = document.querySelector('div[class="styles_cardWrapper__g8amG styles_show__Z8n7u"] p');
                            return el && el.innerText !== previousText;
                        }
                        """,
                        arg=old_text,
                        timeout=10_000,
                    )
                    new_text = await first_review.inner_text()
                    logger.console("Next review page loaded successfully. ✅")

                except PlaywrightError as e:
                    await logger.error(f"get_reviews:: Failed to navigate to the next review page: {e}", exc=e)

            else:
                logger.console("No more review pages available, stopping pagination. 🏁 ")
                break

        return title, href.split("?")[0], reviews

    except PlaywrightError as e:
        await logger.error(f"get_reviews:: Failed to apply review filters: {e}", exc=e)
        return None, None, []


async def generate_review_text_openai(
        prompts,
        company_details: Optional[list] = None,
        reviews=None,
        prompt_parameters = None,
        review_length: Optional[int] = 200,
        task=None
    ):
    logger = TaskLogger(task)
    from openai import OpenAI
    import json

    effective_prompts = prompts if prompts else [
        "Write a genuine, natural customer review based on the provided company information and example reviews."
    ]

    client = OpenAI(api_key=os.environ.get("OPENAI_API_KEY"))

    final_prompt = f"""Generate a realistic customer review based on the provided details and examples.

        Requirements:
        - Return a JSON object only.
        - Keys must be exactly: "title", "text"
        - "title": catchy, natural, maximum 5 words
        - Review body: {review_length} words
        - "text": plain text review body only, realistic tone, human-like, no markdown
        - No extra keys
        - No intro, no outro, no commentary, no notes, no labels outside JSON \n"""

    if prompt_parameters:
        final_prompt += f"\nAdditional parameters: {json.dumps(prompt_parameters)}\n\n"

    if company_details:
        final_prompt += "\n".join(str(item) for item in company_details) + "\n\n" + random.choice(effective_prompts) + "\n\n"
    else:
        final_prompt += random.choice(effective_prompts) + "\n\n" + "\n".join(str(item) for item in reviews)

    try:
        response = client.chat.completions.create(
            model="gpt-5.4-mini",
            messages=[
                {
                    "role": "system",
                    "content": "You generate realistic customer reviews and must return valid JSON only.",
                },
                {
                    "role": "user",
                    "content": final_prompt
                },
            ],
            max_completion_tokens=1000,
            temperature=0.7,
            response_format={"type": "json_object"},

        )

        data = json.loads(response.choices[0].message.content)
        title = data["title"].strip()
        text = data["text"].strip()

        logger.console(f"Generated title 🎉: {title}")
        logger.console(f"Generated review text 🎉: {text}")
        return title, text

    except Exception as e:
        await logger.error(f"generate_review_text_openai:: Failed to generate review text with OpenAI: {e}", exc=e)


async def main():
    token = get_token()
    if not token:
        raise ValueError("Failed to get Multilogin token")

    port = start_browser(token)
    if not port:
        raise ValueError("Failed to get browser port from Multilogin")

    #user = await sync_to_async(User.objects.order_by("date_created").first)()

    async with async_playwright() as p:
        browser = await p.chromium.connect_over_cdp(f"http://127.0.0.1:{port}")
        context = browser.contexts[0]
        page = context.pages[0] if context.pages else await context.new_page()

        # ⚠️ 2-FA feature handling is needed while google may ask about it, so the rest of funtion may not be working
        # await account_registraton(page, user_data={"email": "ivanperpletkin@gmail.com"})


if __name__ == "__main__":
    asyncio.run(main())
