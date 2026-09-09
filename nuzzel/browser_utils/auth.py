"""Authentication helpers for browser-based Twitter client"""

import asyncio
import json
import logging
import random
from typing import Any, Dict, List, Optional

from playwright.async_api import BrowserContext, Page

logger = logging.getLogger(__name__)

_X_HOSTS = {"x.com", "twitter.com"}
_SAMESITE_MAP = {
    "lax": "Lax",
    "strict": "Strict",
    "none": "None",
    "no_restriction": "None",
}


def _normalize_samesite(value: Any) -> Optional[str]:
    if not value:
        return None
    mapped = _SAMESITE_MAP.get(str(value).strip().lower())
    return mapped


def _domains_for_cookie(domain: str) -> List[str]:
    """Return domains to set the cookie on.

    Cookies copied from DevTools are often scoped to either .twitter.com or
    .x.com. Playwright only sends a cookie to the host you navigate to, so a
    .twitter.com auth_token is silently dropped on https://x.com/home.
    """
    host = domain.lower().lstrip(".")
    if host in _X_HOSTS:
        return [".x.com", ".twitter.com"]
    return [domain]


def prepare_cookies(cookies_data: List[Dict[str, Any]]) -> List[Dict[str, Any]]:
    """Normalize browser cookies into Playwright SetCookieParam dicts.

    Strips expiration (Twitter still accepts a valid auth_token) and mirrors
    x.com/twitter.com cookies onto both domains.
    """
    cookies: List[Dict[str, Any]] = []
    seen = set()
    names = []

    for cookie in cookies_data:
        name = cookie.get("name")
        value = cookie.get("value")
        if not name or value is None:
            continue

        names.append(name)
        cookie_domain = cookie.get("domain") or ".x.com"
        same_site = _normalize_samesite(cookie.get("sameSite"))

        for domain in _domains_for_cookie(cookie_domain):
            key = (name, domain, cookie.get("path", "/"))
            if key in seen:
                continue
            seen.add(key)

            cookie_obj: Dict[str, Any] = {
                "name": name,
                "value": value,
                "domain": domain,
                "path": cookie.get("path", "/"),
            }
            if cookie.get("secure") or same_site == "None":
                cookie_obj["secure"] = True
            if cookie.get("httpOnly"):
                cookie_obj["httpOnly"] = True
            if same_site:
                cookie_obj["sameSite"] = same_site
            cookies.append(cookie_obj)

    if "auth_token" not in names:
        logger.warning("No auth_token cookie found; authentication is likely to fail")
    if "ct0" not in names:
        logger.warning("No ct0 cookie found; X API requests may fail CSRF checks")

    return cookies


async def inject_cookies(context: BrowserContext, cookies_json: str) -> None:
    """
    Inject cookies into browser context from JSON string.

    Args:
        context: Playwright browser context
        cookies_json: JSON string containing cookies array
    """
    try:
        cookies_data = json.loads(cookies_json)
        if not isinstance(cookies_data, list):
            raise ValueError("Cookies must be a JSON array")

        cookies = prepare_cookies(cookies_data)
        if not cookies:
            raise ValueError("Cookies JSON did not contain any usable cookies")

        # Type cast to satisfy mypy - cookie_obj matches SetCookieParam structure
        await context.add_cookies(cookies)  # type: ignore[arg-type]
        cookie_summary = ", ".join(f"{c['name']}@{c['domain']}" for c in cookies)
        logger.info(
            "Injected %d cookies into browser context (stripped expiration, mirrored x.com/twitter.com): %s",
            len(cookies),
            cookie_summary,
        )
    except json.JSONDecodeError as e:
        raise ValueError(f"Invalid JSON in cookies: {e}") from e
    except ValueError:
        raise
    except Exception as e:
        raise ValueError(f"Failed to inject cookies: {e}") from e


async def login_with_credentials(
    page: Page,
    username: str,
    password: str,
) -> None:
    """
    Automate Twitter login flow.

    Args:
        page: Playwright page instance
        username: Twitter username or email
        password: Twitter password
    """
    logger.info("Starting automated login flow")

    # Navigate to login page
    await page.goto("https://x.com/i/flow/login", wait_until="networkidle")
    await _random_delay(2, 4)

    # Enter username
    username_input = page.locator('input[autocomplete="username"]')
    await username_input.wait_for(state="visible", timeout=10000)
    await username_input.fill(username)
    await _random_delay(1, 2)

    # Click next button
    next_button = page.locator('text="Next"').first
    await next_button.click()
    await _random_delay(2, 3)

    # Check if there's an unusual activity challenge (phone/email verification)
    unusual_activity = page.locator('text="Unusual activity"')
    if await unusual_activity.count() > 0:
        logger.warning("Unusual activity challenge detected - manual intervention may be required")
        # Try to find and click "Use phone instead" or similar
        try:
            use_phone = page.locator('text=/use.*phone/i').first
            if await use_phone.count() > 0:
                await use_phone.click()
                await _random_delay(2, 3)
        except Exception:
            pass

    # Enter password
    password_input = page.locator('input[name="password"]')
    await password_input.wait_for(state="visible", timeout=10000)
    await password_input.fill(password)
    await _random_delay(1, 2)

    # Click login button
    login_button = page.locator('button[data-testid="LoginForm_Login_Button"]')
    if await login_button.count() == 0:
        login_button = page.locator('text="Log in"').first
    await login_button.click()
    await _random_delay(3, 5)

    # Handle 2FA if required
    two_factor_input = page.locator('input[name="text"]')
    if await two_factor_input.count() > 0:
        logger.warning("2FA challenge detected - manual intervention required")
        logger.warning("Waiting up to 60 seconds for manual 2FA code entry...")
        # Wait up to 60 seconds for manual entry
        try:
            await two_factor_input.wait_for(state="hidden", timeout=60000)
            logger.info("2FA code entered successfully")
        except Exception as e:
            raise ValueError("2FA code not entered within timeout period") from e

    # Wait for successful login (check for home timeline)
    try:
        await page.wait_for_url("https://x.com/home", timeout=30000)
        logger.info("Login successful - navigated to home")
    except Exception as e:
        # Check if we're logged in by looking for common elements
        home_timeline = page.locator('[data-testid="primaryColumn"]')
        if await home_timeline.count() > 0:
            logger.info("Login successful - home timeline detected")
        else:
            raise ValueError("Login failed - could not verify successful authentication") from e


async def _random_delay(min_seconds: float, max_seconds: float) -> None:
    """Add random delay to appear more human-like"""
    delay = random.uniform(min_seconds, max_seconds)
    await asyncio.sleep(delay)
