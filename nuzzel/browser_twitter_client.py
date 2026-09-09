"""
Browser-based Twitter Client

This module provides a headless browser implementation of the Twitter client
using Playwright. It scrapes Twitter's web interface by intercepting GraphQL
responses and extracting data.
"""

import asyncio
import logging
import os
import random
import time
from pathlib import Path
from datetime import datetime, timedelta, timezone
from typing import Dict, Any, List, Optional

import xdk  # type: ignore[import-untyped]
from xdk.oauth1_auth import OAuth1  # type: ignore[import-untyped]
from playwright.async_api import async_playwright, Browser, BrowserContext, Page, Response

from nuzzel.browser_utils import auth, extractors, stealth
from nuzzel.twitter_client import TwitterClient, TwitterAPIError

logger = logging.getLogger(__name__)


class BrowserTwitterClient(TwitterClient):
    """Browser-based Twitter client using Playwright"""

    _LOGGED_IN_SELECTORS = [
        '[data-testid="primaryColumn"]',
        '[data-testid="AppTabBar_Home_Link"]',
        '[data-testid="AppTabBar_Profile_Link"]',
        '[data-testid="SideNav_AccountSwitcher_Button"]',
        '[data-testid="AppTabBar_Notifications_Link"]',
        'a[href="/home"]',
    ]

    def __init__(
        self,
        cookies_json: Optional[str] = None,
        username: Optional[str] = None,
        password: Optional[str] = None,
        headless: bool = True,
    ):
        """
        Initialize browser-based Twitter client.

        Authentication method is auto-detected:
        - If cookies_json is provided, uses cookie authentication
        - If username and password are provided, uses login authentication
        - If both are provided, cookies take precedence

        Args:
            cookies_json: JSON string of cookies (for cookie auth)
            username: Twitter username/email (for login auth)
            password: Twitter password (for login auth)
            headless: Run browser in headless mode (can be overridden by BROWSER_HEADLESS=false env var)
        """
        self.cookies_json = cookies_json
        self.username = username
        self.password = password
        # Allow environment variable to override headless mode for debugging
        env_headless = os.getenv("BROWSER_HEADLESS", "").lower()
        if env_headless == "false":
            self.headless = False
            logger.info("Running in headed mode (BROWSER_HEADLESS=false)")
        else:
            self.headless = headless

        self.playwright: Optional[Any] = None  # type: ignore[assignment]
        self.browser: Optional[Browser] = None
        self.context: Optional[BrowserContext] = None
        self.page: Optional[Page] = None

        # Data collection
        self._captured_responses: List[Dict[str, Any]] = []
        self._user_id: Optional[str] = None

        # Initialize browser (lazy initialization)
        self._initialized = False

    async def _ensure_initialized(self) -> None:
        """Ensure browser is initialized and authenticated"""
        if self._initialized:
            return

        logger.info("Initializing browser client")
        self.playwright = await async_playwright().start()  # type: ignore[assignment]

        # Launch browser with stealth measures
        assert self.playwright is not None
        launch_kwargs = {
            "headless": self.headless,
            "args": [
                "--disable-blink-features=AutomationControlled",
                "--disable-dev-shm-usage",
                "--no-first-run",
                "--no-default-browser-check",
            ],
            "ignore_default_args": ["--enable-automation"],
        }
        # Match debug script's slow_mo when headed to make behavior consistent
        if not self.headless:
            launch_kwargs["slow_mo"] = 500

        self.browser = await self.playwright.chromium.launch(**launch_kwargs)  # type: ignore[arg-type]

        # Use Playwright's default UA so it matches the bundled Chromium version.
        # A stale hardcoded UA (e.g. Chrome/120) is a strong bot signal on X.
        self.context = await self.browser.new_context(
            viewport={"width": 1920, "height": 1080},
            locale="en-US",
            color_scheme="light",
        )

        await stealth.apply_stealth_measures(self.context)

        self.page = await self.context.new_page()

        # Set up response interception
        assert self.page is not None
        await self._setup_response_interception()

        # Authenticate - try cookies first, fall back to username/password if provided and cookies fail
        cookie_auth_failed = False

        if self.cookies_json:
            # Cookie authentication (preferred if both are provided)
            logger.info("Attempting cookie authentication")
            try:
                await auth.inject_cookies(self.context, self.cookies_json)
                is_logged_in = await self._authenticate_with_cookies()
                if not is_logged_in:
                    cookie_auth_failed = True
                else:
                    logger.info("Cookie authentication successful")
            except Exception as e:
                logger.warning("Cookie authentication error: %s", e)
                cookie_auth_failed = True

        # Fall back to username/password if cookies failed or weren't provided
        if cookie_auth_failed or (not self.cookies_json and self.username and self.password):
            if self.username and self.password:
                logger.info("Using username/password authentication")
                await auth.login_with_credentials(
                    self.page, self.username, self.password
                )
            elif cookie_auth_failed:
                raise ValueError(
                    "Cookie authentication failed and no username/password provided for fallback"
                )
        elif not self.cookies_json and not (self.username and self.password):
            raise ValueError(
                "Either cookies_json or (username and password) must be provided for authentication"
            )

        # Get user ID (will be lazy-loaded when needed)
        # Don't fetch it here to avoid blocking initialization

        self._initialized = True
        logger.info("Browser client initialized successfully")

    def _page_url(self) -> str:
        assert self.page is not None
        return str(self.page.url or "")

    def _url_looks_like_login(self, url: str) -> bool:
        lowered = url.lower()
        return (
            "/i/flow/login" in lowered
            or "/i/flow/signup" in lowered
            or lowered.rstrip("/").endswith("/login")
            or "/login?" in lowered
        )

    async def _goto_home(self) -> None:
        assert self.page is not None
        try:
            await self.page.goto(
                "https://x.com/home",
                wait_until="domcontentloaded",
                timeout=60000,
            )
        except Exception as e:
            logger.warning("Home navigation timeout/error: %s, continuing anyway", e)

    async def _verify_logged_in(self, timeout_ms: int = 20000) -> bool:
        """Return True if the X web app looks authenticated."""
        assert self.page is not None
        combined = ", ".join(self._LOGGED_IN_SELECTORS)
        try:
            await self.page.wait_for_selector(combined, timeout=timeout_ms, state="attached")
            logger.info(
                "Cookie authentication appears successful (logged-in elements found) URL=%s",
                self._page_url(),
            )
            return True
        except Exception as e:
            logger.warning("Could not find logged-in elements after cookie injection: %s", e)

        current_url = self._page_url()
        logger.warning("Auth check URL after wait: %s", current_url)
        if self._url_looks_like_login(current_url):
            logger.warning("Cookie authentication failed - redirected to login page")
            return False

        try:
            title = await self.page.title()
            body_text = (await self.page.inner_text("body"))[:500].replace("\n", " ")
            logger.warning("Auth check page title=%r body_preview=%r", title, body_text)
        except Exception as diag_error:
            logger.debug("Failed to collect auth page diagnostics: %s", diag_error)
        return False

    async def _save_auth_failure_debug(self) -> None:
        assert self.page is not None
        debug_dir = Path("debug_output")
        debug_dir.mkdir(exist_ok=True)
        screenshot = debug_dir / "auth_check_failed.png"
        html_path = debug_dir / "auth_check_failed.html"
        try:
            await self.page.screenshot(path=str(screenshot), full_page=True)
            html_path.write_text(await self.page.content(), encoding="utf-8")
            logger.info("Saved auth failure screenshot to %s and HTML to %s", screenshot, html_path)
        except Exception as e:
            logger.warning("Failed to save auth debug artifacts: %s", e)

    async def _authenticate_with_cookies(self) -> bool:
        """Navigate to home and confirm cookie auth, retrying once on failure."""
        assert self.page is not None
        await self._goto_home()
        await asyncio.sleep(1)
        await self._dismiss_popups()

        if await self._verify_logged_in():
            return True

        logger.warning("Auth check failed, retrying home navigation once")
        try:
            await self.page.reload(wait_until="domcontentloaded", timeout=60000)
        except Exception as e:
            logger.warning("Reload after failed auth check errored: %s", e)
            await self._goto_home()
        await asyncio.sleep(2)
        await self._dismiss_popups()

        if await self._verify_logged_in():
            return True

        await self._save_auth_failure_debug()
        return False

    async def _setup_response_interception(self) -> None:
        """Set up response interception for Twitter GraphQL endpoints"""
        assert self.page is not None

        async def handle_response(response: Response) -> None:
            url = response.url

            # Log all GraphQL/API requests for debugging
            if "graphql" in url.lower() or "api" in url.lower():
                logger.debug("API request: %s", url[:150])

            # Intercept Twitter's GraphQL endpoints
            # Include variations for x.com and twitter.com
            timeline_endpoints = [
                "HomeTimeline",
                "HomeLatestTimeline",  # Alternative endpoint name
                "Following",  # Some requests use this
                "ForYou",  # For You tab endpoint
                "UserTweets",
                "Likes",
                "UserByScreenName",
                "ListsManagementPageTimeline",
                "ListMembers",
                "TweetDetail",
            ]

            if any(endpoint in url for endpoint in timeline_endpoints):
                try:
                    data = await response.json()
                    self._captured_responses.append({"url": url, "data": data})
                    logger.info("Captured GraphQL response: %s", url.split('/')[-1].split('?')[0])
                except Exception as e:
                    logger.debug("Failed to parse response from %s: %s", url, e)

        self.page.on("response", handle_response)

    def _get_user_id_from_api(self) -> str:
        """Get user ID from Twitter API using XDK (like LiveTwitterClient)"""
        try:
            # Read OAuth1 credentials from environment variables (same as LiveTwitterClient)
            api_key = os.getenv("TWITTER_API_KEY")
            api_secret = os.getenv("TWITTER_API_SECRET")
            access_token = os.getenv("TWITTER_ACCESS_TOKEN")
            access_token_secret = os.getenv("TWITTER_ACCESS_TOKEN_SECRET")

            if not api_key or not api_secret or not access_token or not access_token_secret:
                raise TwitterAPIError(
                    "TWITTER_API_KEY, TWITTER_API_SECRET, TWITTER_ACCESS_TOKEN, and "
                    "TWITTER_ACCESS_TOKEN_SECRET environment variables required for getting user ID"
                )

            logger.info("Fetching user ID from Twitter API using XDK")

            # Create OAuth1 auth instance (same as LiveTwitterClient)
            oauth1_auth = OAuth1(
                api_key=api_key,
                api_secret=api_secret,
                callback="oob",  # Out-of-band callback for existing tokens
                access_token=access_token,
                access_token_secret=access_token_secret,
            )

            # Create XDK client
            client = xdk.Client(auth=oauth1_auth)

            # Get user ID (same as LiveTwitterClient)
            response = client.users.get_me(user_fields=["id"])
            if hasattr(response, "data") and response.data:
                user_id = response.data["id"]
                logger.info("Successfully fetched user ID from API: %s", user_id)
                return user_id

            raise TwitterAPIError("Failed to get user ID: response missing data attribute")
        except TwitterAPIError:
            raise
        except Exception as e:
            raise TwitterAPIError(f"Failed to get user ID from API: {e}") from e

    async def _scroll_and_collect(
        self, max_items: int = 300, scroll_timeout: int = 30, stop_date_str: Optional[str] = None
    ) -> List[Dict[str, Any]]:
        """
        Scroll page and collect data from intercepted responses.

        Args:
            max_items: Maximum number of items to collect
            scroll_timeout: Maximum seconds to scroll (with jitter)
            stop_date_str: Optional ISO date string - stop scrolling if we reach a tweet older than this

        Returns:
            List of collected items
        """
        assert self.page is not None
        collected: List[Dict[str, Any]] = []
        last_height = 0
        no_change_count = 0
        start_time = time.time()

        # Debug: Log current URL and check for tweets on page
        current_url = self.page.url
        logger.debug("Current URL before scrolling: %s", current_url)

        # Check if any tweets are visible on the page
        try:
            tweet_selectors = [
                '[data-testid="tweet"]',
                '[data-testid="tweetText"]',
                'article[role="article"]',
                '[data-testid="cellInnerDiv"]',
            ]
            for selector in tweet_selectors:
                tweet_count = await self.page.locator(selector).count()
                if tweet_count > 0:
                    logger.debug("Found %s elements matching %s", tweet_count, selector)
                    break
            else:
                logger.warning("No tweet elements found on page before scrolling")
        except Exception as e:
            logger.debug("Error checking for tweets: %s", e)

        # Log any responses already captured before scrolling
        logger.debug("Responses captured before scrolling: %s", len(self._captured_responses))

        # Add jitter to timeout (10% variation)
        jitter = random.uniform(0.9, 1.1)
        actual_timeout = scroll_timeout * jitter
        logger.info("Scrolling with timeout of %.1f seconds (base: %ss)", actual_timeout, scroll_timeout)

        scroll_iteration = 0
        while len(collected) < max_items and (time.time() - start_time) < actual_timeout:
            scroll_iteration += 1

            # Perform human-like scroll
            await stealth.human_like_scroll(self.page)
            await self._random_delay(2, 4)

            # Check if we've reached the end
            new_height = await self.page.evaluate("document.body.scrollHeight")
            if new_height == last_height:
                no_change_count += 1
                logger.debug("Scroll %s: Height unchanged (%s), no_change_count=%s",
                    scroll_iteration, new_height, no_change_count)
                if no_change_count >= 3:
                    logger.info("Reached end of timeline")
                    break
            else:
                no_change_count = 0
                logger.debug("Scroll %s: Height changed %s -> %s", scroll_iteration, last_height, new_height)
                last_height = new_height

            # Collect from intercepted responses (responses accumulate, don't clear them)
            # Count only new responses since last iteration
            new_responses = len(self._captured_responses) - len(collected)
            for response in self._captured_responses[len(collected):]:
                collected.append(response)

            if new_responses > 0 or scroll_iteration <= 3:
                logger.debug("Scroll %s: Captured %s new responses, total collected: %s",
                    scroll_iteration, new_responses, len(collected))

            # Check if we've reached a tweet older than stop_date
            if stop_date_str:
                # Extract tweets from current batch to check dates (check most recent first)
                for response in collected:
                    if "HomeTimeline" in response.get("url", ""):
                        try:
                            tweets = extractors.extract_tweets_from_graphql_response(response["data"])
                            for tweet in tweets:
                                tweet_date = tweet.get("created_at")
                                if tweet_date and tweet_date < stop_date_str:
                                    logger.info("Reached tweet older than 2 days: %s (stop date: %s)",
                                        tweet_date, stop_date_str)
                                    return collected
                        except Exception as e:
                            logger.debug("Error extracting tweets for date check: %s", e)
                            continue

            # Random mouse movement
            if random.random() < 0.3:  # 30% chance
                await stealth.random_mouse_movement(self.page)

        elapsed = time.time() - start_time
        logger.info("Finished scrolling after %.1f seconds, collected %s responses", elapsed, len(collected))
        return collected[:max_items]

    async def _random_delay(self, min_seconds: float, max_seconds: float) -> None:
        """Add random delay to appear more human-like"""
        delay = random.uniform(min_seconds, max_seconds)
        await asyncio.sleep(delay)

    async def _dismiss_popups(self) -> None:
        """Detect and dismiss any popups/modals that might block interaction.

        This handles premium upsell popups, cookie banners, and other modals
        that Twitter might show.
        """
        assert self.page is not None
        try:
            # Wait a moment for popups to appear
            await self._random_delay(0.5, 1.0)

            # Look for common close button patterns
            close_selectors = [
                # X/Close buttons
                'button[aria-label*="Close"]',
                'button[aria-label*="close"]',
                '[data-testid*="close"]',
                '[data-testid*="Close"]',
                '[aria-label="Close"]',
                # Premium upsell specific
                '[data-testid="app-bar-close"]',
                'button:has(svg[aria-label*="Close"])',
                'button:has(svg[aria-label*="close"])',
                # Generic close icons
                'svg[aria-label*="Close"]',
                'svg[aria-label*="close"]',
                # "Not now" or "Skip" buttons
                'button:has-text("Not now")',
                'button:has-text("Skip")',
                'button:has-text("Maybe later")',
                'button:has-text("No thanks")',
                'button:has-text("Dismiss")',
            ]

            popup_dismissed = False

            # Try to find and click close buttons
            for selector in close_selectors:
                try:
                    close_btn = self.page.locator(selector).first
                    if await close_btn.count() > 0 and await close_btn.is_visible():
                        # Check if it's in a modal/popup context (not just any close button)
                        # Look for modal/popup containers
                        has_modal_parent = await close_btn.evaluate("""el => {
                            const parent = el.closest('[role=\"dialog\"], [role=\"alertdialog\"], [data-testid*=\"modal\"], [data-testid*=\"popup\"], [class*=\"modal\"], [class*=\"overlay\"]');
                            return parent !== null;
                        }""")

                        if has_modal_parent:
                            logger.info("Dismissing popup using selector: %s", selector)
                            await close_btn.click()
                            await self._random_delay(0.5, 1.0)
                            popup_dismissed = True
                            break
                except Exception as e:
                    logger.debug("Close selector %s failed: %s", selector, e)
                    continue

            # If no close button found, try pressing Escape key
            if not popup_dismissed:
                # Check if there's a modal/dialog visible
                modal_selectors = [
                    '[role="dialog"]',
                    '[role="alertdialog"]',
                    '[data-testid*="modal"]',
                    '[data-testid*="popup"]',
                ]

                for selector in modal_selectors:
                    try:
                        modal = self.page.locator(selector).first
                        if await modal.count() > 0 and await modal.is_visible():
                            logger.info("Modal detected, pressing Escape to dismiss")
                            await self.page.keyboard.press("Escape")
                            await self._random_delay(0.5, 1.0)
                            popup_dismissed = True
                            break
                    except Exception:
                        continue

        except Exception as e:
            logger.debug("Error dismissing popups: %s", e)
            # Don't fail the whole operation if popup dismissal fails

    async def _switch_to_following_tab(self) -> bool:
        """Switch to the 'Following' tab on the home timeline.

        Uses the same selector approach as debug_twitter_ui.py which has been
        verified to work with the current Twitter UI.

        Returns:
            True if successfully switched (or already on Following), False otherwise
        """
        assert self.page is not None
        try:
            # Wait for page to fully load
            await self._random_delay(1, 2)

            # Selectors that work (from debug_twitter_ui.py)
            # Order matters - most specific first
            following_selectors = [
                # Most specific: unselected tab with Following text
                '[role="tab"][aria-selected="false"]:has-text("Following")',
                # Tab with Following text (may already be selected)
                '[role="tab"]:has-text("Following")',
                # Presentation wrapper
                'div[role="presentation"]:has-text("Following")',
                # Link-based
                'a:has-text("Following")',
                # Span with text
                'span:has-text("Following")',
                # Direct text match
                'text="Following"',
            ]

            for selector in following_selectors:
                try:
                    logger.debug("Trying Following tab selector: %s", selector)
                    element = self.page.locator(selector).first
                    if await element.count() > 0:
                        is_visible = await element.is_visible()
                        logger.debug("  Found element, visible: %s", is_visible)

                        if not is_visible:
                            continue

                        # Check if it's already selected (only for tab elements)
                        is_selected = await element.get_attribute("aria-selected")
                        if is_selected == "true":
                            logger.info("Already on Following tab")
                            return True

                        # Click the tab
                        await element.click()
                        await asyncio.sleep(2)  # Match debug script timing
                        logger.info("Switched to Following tab using selector: %s", selector)
                        return True
                except Exception as e:
                    logger.debug("Selector %s failed: %s", selector, e)
                    continue

            logger.warning("Could not find Following tab, continuing with default view")
            # Save debug screenshot to help diagnose selector issues
            try:
                debug_dir = Path("debug_output")
                debug_dir.mkdir(exist_ok=True)
                screenshot_path = debug_dir / "following_tab_not_found.png"
                await self.page.screenshot(path=str(screenshot_path))
                logger.info("Saved debug screenshot to %s", screenshot_path)
            except Exception as e:
                logger.debug("Could not save debug screenshot: %s", e)
            return False
        except Exception as e:
            logger.warning("Failed to switch to Following tab: %s, continuing anyway", e)
            return False

    async def _select_recent_sort(self) -> None:
        """Select 'Recent' in the sort dropdown.

        Note: In the current Twitter UI, the sort option is accessed via a dropdown
        arrow (SVG) inside the "Following" tab.

        Uses the same approach as debug_twitter_ui.py which has been verified to work.
        """
        assert self.page is not None
        try:
            # The dropdown arrow is an SVG inside the Following tab (from debug_twitter_ui.py)
            # Try the most direct selector first
            dropdown_selectors = [
                '[role="tab"]:has-text("Following") svg',
                '[role="tab"]:has-text("Following") + button',
                '[aria-label="Sort"]',
                '[data-testid="sortDropdown"]',
            ]

            dropdown_clicked = False
            for selector in dropdown_selectors:
                try:
                    elem = self.page.locator(selector).first
                    if await elem.count() > 0 and await elem.is_visible():
                        logger.debug("Found dropdown element: %s", selector)
                        await elem.click()
                        await asyncio.sleep(2)  # Match debug script timing
                        dropdown_clicked = True
                        logger.info("Clicked sort dropdown")
                        break
                except Exception as e:
                    logger.debug("Dropdown selector %s failed: %s", selector, e)
                    continue

            if not dropdown_clicked:
                logger.warning("Could not find Sort dropdown, assuming Recent is already selected or not available")
                return

            # Look for "Recent" option in the dropdown menu
            recent_selectors = [
                'text="Recent"',
                '[role="menuitem"]:has-text("Recent")',
                '[role="option"]:has-text("Recent")',
            ]

            for selector in recent_selectors:
                try:
                    recent_option = self.page.locator(selector).first
                    if await recent_option.count() > 0 and await recent_option.is_visible():
                        await recent_option.click()
                        await asyncio.sleep(2)
                        logger.info("Selected Recent sort option")
                        return
                except Exception as e:
                    logger.debug("Recent option selector %s failed: %s", selector, e)
                    continue

            # Close dropdown if Recent wasn't found
            await self.page.keyboard.press("Escape")
            logger.warning("Could not find Recent option in dropdown")

        except Exception as e:
            logger.warning("Failed to select Recent sort: %s, continuing anyway", e)

    async def get_user_id_async(self) -> str:
        """Async version for use in async pipelines"""
        return await self.get_user_id()

    async def get_user_id(self) -> str:
        """Get authenticated user ID

        First checks TWITTER_USER_ID environment variable.
        If not set, uses XDK to make an API request (like LiveTwitterClient).
        """
        # Method 1: Check environment variable first
        env_user_id = os.getenv("TWITTER_USER_ID")
        if env_user_id:
            # Only log once to avoid clutter
            if not hasattr(self, '_user_id_logged'):
                logger.debug("Using user ID from TWITTER_USER_ID environment variable: %s", env_user_id)
                self._user_id_logged = True
            return env_user_id.strip()

        # Method 2: Get from API using XDK (like LiveTwitterClient)
        # No need to initialize browser - XDK works independently
        if not self._user_id:
            self._user_id = self._get_user_id_from_api()

        return self._user_id


    async def get_user_timeline(
        self, start_time: datetime, max_pages: int = 3
    ) -> Dict[str, Any]:
        """
        Get user's timeline tweets within time window.

        Args:
            start_time: Start time for tweets
            max_pages: Maximum pages to fetch (not used in browser client)

        Returns:
            Dictionary with 'data', 'users', 'media', 'referenced_tweets'
        """
        return await self._get_user_timeline_async(start_time)

    async def get_user_timeline_async(
        self, start_time: datetime
    ) -> Dict[str, Any]:
        """Async version for use in async pipelines"""
        return await self._get_user_timeline_async(start_time)

    async def _get_user_timeline_async(
        self, start_time: datetime
    ) -> Dict[str, Any]:
        """Async implementation of get_user_timeline"""
        await self._ensure_initialized()
        assert self.page is not None

        logger.info("Fetching user timeline")

        # Navigate to home timeline
        # Use x.com (Twitter rebranded) and domcontentloaded for faster loading
        try:
            await self.page.goto("https://x.com/home",
                                wait_until="domcontentloaded",
                                timeout=60000)  # 60 second timeout
        except Exception as e:
            logger.warning("Timeline navigation timeout/error: %s, continuing anyway", e)

        # Log current URL after navigation (might redirect)
        current_url = self.page.url
        logger.debug("After navigation, current URL: %s", current_url)

        # Wait longer for page to fully load (matching debug script's 5 second wait)
        await asyncio.sleep(5)

        # Dismiss any popups that might have appeared (premium upsell, etc.)
        await self._dismiss_popups()

        # Log how many responses we captured during initial page load (For You tab)
        initial_responses = len(self._captured_responses)
        logger.debug("Captured %s responses during initial page load", initial_responses)

        # Switch to "Following" tab - this will trigger HomeLatestTimeline response
        # We do NOT clear responses yet - if tab switch fails, we'll use For You as fallback
        tab_switched = await self._switch_to_following_tab()

        # Wait for tab content to load and any API responses
        await asyncio.sleep(2)

        # If we successfully switched tabs, clear the old "For You" responses
        # so we only use "Following" timeline data
        if tab_switched:
            # Keep only responses captured after the tab switch
            # (responses captured during/after the click)
            if len(self._captured_responses) > initial_responses:
                # New responses came in after tab switch - use only those
                self._captured_responses = self._captured_responses[initial_responses:]
                logger.debug("Using %s responses from Following tab", len(self._captured_responses))
            else:
                # No new responses yet, don't clear - they may come during scrolling
                logger.debug("No new responses after tab switch yet, keeping all for now")
        else:
            # Tab switch failed - log warning but keep initial responses as fallback
            logger.warning("Using For You timeline as fallback since Following tab switch failed")

        # Dismiss popups again in case clicking the tab triggered one
        await self._dismiss_popups()

        # Ensure "Recent" is selected in sort dropdown
        await self._select_recent_sort()

        # Dismiss popups one more time before scrolling (in case sort dropdown triggered one)
        await self._dismiss_popups()

        # Calculate stop time (2 days before now)
        stop_date = datetime.now(timezone.utc) - timedelta(days=2)
        stop_date_str = stop_date.strftime("%Y-%m-%dT%H:%M:%SZ")

        # Scroll and collect (5 minutes with jitter, or until 2 days old)
        responses = await self._scroll_and_collect(
            max_items=300,
            scroll_timeout=10 * 60,  # 10 minutes in seconds
            stop_date_str=stop_date_str
        )

        # Extract tweets and users from responses
        all_tweets = []
        all_users = {}

        if not responses:
            logger.warning(
                "No GraphQL responses captured during scrolling. "
                "This may indicate authentication failed (expired cookies) or "
                "Twitter's page structure has changed. Try regenerating cookies.json."
            )
            # Save debug screenshot to help diagnose the issue
            try:
                debug_dir = Path("debug_output")
                debug_dir.mkdir(exist_ok=True)
                screenshot_path = debug_dir / "failed_timeline_fetch.png"
                await self.page.screenshot(path=str(screenshot_path))
                logger.info("Saved debug screenshot to %s", screenshot_path)
            except Exception as e:
                logger.debug("Could not save debug screenshot: %s", e)

        for response in responses:
            url = response.get("url", "")
            # Match various timeline endpoints
            if any(endpoint in url for endpoint in ["HomeTimeline", "HomeLatestTimeline", "Following"]):
                tweets = extractors.extract_tweets_from_graphql_response(response["data"])
                users = extractors.extract_users_from_graphql_response(response["data"])

                all_tweets.extend(tweets)
                all_users.update(users)

        # Filter by start_time
        start_time_str = start_time.strftime("%Y-%m-%dT%H:%M:%SZ")
        filtered_tweets = [
            tweet
            for tweet in all_tweets
            if tweet.get("created_at") and tweet["created_at"] >= start_time_str
        ]

        logger.info("Collected %s tweets from timeline", len(filtered_tweets))

        return {
            "data": filtered_tweets,
            "users": all_users,
            "media": {},  # Media extraction would require additional parsing
            "referenced_tweets": {},
        }

    async def get_user_liked_tweets(self, max_results: int = 10) -> Dict[str, Any]:
        """Get user's recently liked tweets"""
        return await self._get_user_liked_tweets_async(max_results)

    async def get_user_liked_tweets_async(self, max_results: int = 10) -> Dict[str, Any]:
        """Async version for use in async pipelines"""
        return await self._get_user_liked_tweets_async(max_results)

    async def _get_user_liked_tweets_async(self, max_results: int = 10) -> Dict[str, Any]:
        """Async implementation of get_user_liked_tweets"""
        await self._ensure_initialized()
        assert self.page is not None

        logger.info("Fetching %s liked tweets", max_results)

        # Clear previous captures BEFORE navigation to catch initial load responses
        self._captured_responses.clear()

        # Use username for URL if available, otherwise numeric ID
        handle = self.username or await self.get_user_id()
        # Ensure handle doesn't have @ prefix for URL
        url_handle = handle.lstrip('@')

        likes_url = f"https://x.com/{url_handle}/likes"
        logger.info("Navigating to likes page: %s", likes_url)
        await self.page.goto(likes_url,
                            wait_until="domcontentloaded",
                            timeout=60000)
        await self._random_delay(3, 5)

        # Debug: Capture page state after navigation
        debug_dir = Path("debug_output")
        debug_dir.mkdir(exist_ok=True)

        # Wait a bit more for content to load
        await asyncio.sleep(2)

        # Take screenshot
        screenshot_path = debug_dir / "likes_page_initial.png"
        await self.page.screenshot(path=str(screenshot_path), full_page=True)
        logger.info("Saved initial likes page screenshot to %s", screenshot_path)

        # Log page state
        current_url = self.page.url
        page_title = await self.page.title()
        logger.info("Likes page - URL: %s, Title: %s", current_url, page_title)

        # Check for common error/empty state messages
        try:
            page_text = await self.page.inner_text("body")
            logger.debug("Page body text (first 1000 chars): %s", page_text[:1000])

            # Check for specific error/empty states
            error_indicators = [
                "Something went wrong",
                "Try again",
                "This page doesn't exist",
                "You don't have any likes yet",
                "These likes are private",
                "Sign in",
                "Log in",
                "Rate limit",
                "temporarily restricted",
            ]
            for indicator in error_indicators:
                if indicator.lower() in page_text.lower():
                    logger.warning("Found potential error indicator on page: '%s'", indicator)
        except Exception as e:
            logger.warning("Error reading page text: %s", e)

        # Log initial responses captured
        logger.info("Responses captured after navigation: %s", len(self._captured_responses))
        for i, resp in enumerate(self._captured_responses):
            resp_url = resp.get("url", "")
            logger.info("  Response %s: %s", i + 1, resp_url.split('/')[-1].split('?')[0] if resp_url else "unknown")

        # Scroll to collect liked tweets - use a larger max_items for responses
        # since many responses might not be "Likes"
        responses = await self._scroll_and_collect(max_items=max(100, max_results * 2))

        # Debug: Capture state after scrolling
        screenshot_path = debug_dir / "likes_page_after_scroll.png"
        await self.page.screenshot(path=str(screenshot_path), full_page=True)
        logger.info("Saved post-scroll likes page screenshot to %s", screenshot_path)

        # Log all response URLs found
        logger.info("Total responses collected after scrolling: %s", len(responses))
        response_types = {}
        for resp in responses:
            resp_url = resp.get("url", "")
            endpoint = resp_url.split('/')[-1].split('?')[0] if resp_url else "unknown"
            response_types[endpoint] = response_types.get(endpoint, 0) + 1
        logger.info("Response types: %s", response_types)

        # Check if we found any Likes responses
        likes_responses = [r for r in responses if "Likes" in r.get("url", "")]
        logger.info("Found %s responses containing 'Likes' in URL", len(likes_responses))

        all_tweets = []
        all_users = {}

        for response in responses:
            if "Likes" in response["url"]:
                tweets = extractors.extract_tweets_from_graphql_response(response["data"])
                users = extractors.extract_users_from_graphql_response(response["data"])

                all_tweets.extend(tweets)
                all_users.update(users)

        # Limit to max_results
        limited_tweets = all_tweets[:max_results]

        logger.info("Collected %s liked tweets", len(limited_tweets))

        # Additional debugging if no tweets found
        if len(limited_tweets) == 0:
            logger.warning("No liked tweets collected!")
            logger.warning("  - Total responses: %s", len(responses))
            logger.warning("  - Responses with 'Likes' in URL: %s", len([r for r in responses if "Likes" in r.get("url", "")]))

            # Save final diagnostic screenshot
            debug_dir = Path("debug_output")
            debug_dir.mkdir(exist_ok=True)
            diagnostic_path = debug_dir / "likes_no_tweets_diagnostic.png"
            await self.page.screenshot(path=str(diagnostic_path), full_page=True)
            logger.info("Saved diagnostic screenshot to %s", diagnostic_path)

            # Try to get more info about what's on the page
            try:
                # Check for tweet containers
                tweet_count = await self.page.locator('[data-testid="tweet"]').count()
                logger.info("  - Tweet elements found on page: %s", tweet_count)

                # Get visible text that might explain the empty state
                main_content = await self.page.locator('[data-testid="primaryColumn"], main, [role="main"]').first
                if await main_content.count() > 0:
                    content_text = await main_content.inner_text()
                    logger.info("  - Main content preview: %s", content_text[:500] if content_text else "empty")
            except Exception as e:
                logger.warning("Error during diagnostic check: %s", e)

        return {
            "data": limited_tweets,
            "users": all_users,
            "media": {},
            "referenced_tweets": {},
        }

    async def get_user_tweets(self, max_results: int = 10) -> Dict[str, Any]:
        """Get user's recent tweets"""
        return await self._get_user_tweets_async(max_results)

    async def get_user_tweets_async(self, max_results: int = 10) -> Dict[str, Any]:
        """Async version for use in async pipelines"""
        return await self._get_user_tweets_async(max_results)

    async def _get_user_tweets_async(self, max_results: int = 10) -> Dict[str, Any]:
        """Async implementation of get_user_tweets"""
        await self._ensure_initialized()
        assert self.page is not None

        logger.info("Fetching %s user tweets", max_results)

        # Clear previous captures BEFORE navigation to catch initial load responses
        self._captured_responses.clear()

        # Use username for URL if available, otherwise numeric ID
        handle = self.username or await self.get_user_id()
        # Ensure handle doesn't have @ prefix for URL
        url_handle = handle.lstrip('@')

        user_url = f"https://x.com/{url_handle}"
        logger.info("Navigating to user profile page: %s", user_url)
        await self.page.goto(user_url,
                            wait_until="domcontentloaded",
                            timeout=60000)
        await self._random_delay(3, 5)

        # Debug: Capture page state after navigation
        debug_dir = Path("debug_output")
        debug_dir.mkdir(exist_ok=True)

        # Wait a bit more for content to load
        await asyncio.sleep(2)

        # Take screenshot
        screenshot_path = debug_dir / "user_tweets_page_initial.png"
        await self.page.screenshot(path=str(screenshot_path), full_page=True)
        logger.info("Saved initial user tweets page screenshot to %s", screenshot_path)

        # Log page state
        current_url = self.page.url
        page_title = await self.page.title()
        logger.info("User tweets page - URL: %s, Title: %s", current_url, page_title)

        # Check for common error/empty state messages
        try:
            page_text = await self.page.inner_text("body")
            logger.debug("Page body text (first 1000 chars): %s", page_text[:1000])

            # Check for specific error/empty states
            error_indicators = [
                "Something went wrong",
                "Try again",
                "This page doesn't exist",
                "doesn't have any posts",
                "hasn't posted anything yet",
                "This account doesn't exist",
                "Sign in",
                "Log in",
                "Rate limit",
                "temporarily restricted",
                "This account's Tweets are protected",
            ]
            for indicator in error_indicators:
                if indicator.lower() in page_text.lower():
                    logger.warning("Found potential error indicator on page: '%s'", indicator)
        except Exception as e:
            logger.warning("Error reading page text: %s", e)

        # Log initial responses captured
        logger.info("Responses captured after navigation: %s", len(self._captured_responses))
        for i, resp in enumerate(self._captured_responses):
            resp_url = resp.get("url", "")
            logger.info("  Response %s: %s", i + 1, resp_url.split('/')[-1].split('?')[0] if resp_url else "unknown")

        # Scroll to collect user tweets - use a larger max_items for responses
        # since many responses might not be "UserTweets"
        responses = await self._scroll_and_collect(max_items=max(100, max_results * 2))

        # Debug: Capture state after scrolling
        screenshot_path = debug_dir / "user_tweets_page_after_scroll.png"
        await self.page.screenshot(path=str(screenshot_path), full_page=True)
        logger.info("Saved post-scroll user tweets page screenshot to %s", screenshot_path)

        # Log all response URLs found
        logger.info("Total responses collected after scrolling: %s", len(responses))
        response_types = {}
        for resp in responses:
            resp_url = resp.get("url", "")
            endpoint = resp_url.split('/')[-1].split('?')[0] if resp_url else "unknown"
            response_types[endpoint] = response_types.get(endpoint, 0) + 1
        logger.info("Response types: %s", response_types)

        # Check if we found any UserTweets responses
        user_tweets_responses = [r for r in responses if "UserTweets" in r.get("url", "")]
        logger.info("Found %s responses containing 'UserTweets' in URL", len(user_tweets_responses))

        all_tweets = []
        all_users = {}

        for response in responses:
            if "UserTweets" in response["url"]:
                tweets = extractors.extract_tweets_from_graphql_response(response["data"])
                users = extractors.extract_users_from_graphql_response(response["data"])

                all_tweets.extend(tweets)
                all_users.update(users)

        # Limit to max_results
        limited_tweets = all_tweets[:max_results]

        logger.info("Collected %s user tweets", len(limited_tweets))

        # Additional debugging if no tweets found
        if len(limited_tweets) == 0:
            logger.warning("No user tweets collected!")
            logger.warning("  - Total responses: %s", len(responses))
            logger.warning("  - Responses with 'UserTweets' in URL: %s", len([r for r in responses if "UserTweets" in r.get("url", "")]))

            # Save final diagnostic screenshot
            debug_dir = Path("debug_output")
            debug_dir.mkdir(exist_ok=True)
            diagnostic_path = debug_dir / "user_tweets_no_tweets_diagnostic.png"
            await self.page.screenshot(path=str(diagnostic_path), full_page=True)
            logger.info("Saved diagnostic screenshot to %s", diagnostic_path)

            # Try to get more info about what's on the page
            try:
                # Check for tweet containers
                tweet_count = await self.page.locator('[data-testid="tweet"]').count()
                logger.info("  - Tweet elements found on page: %s", tweet_count)

                # Get visible text that might explain the empty state
                main_content = await self.page.locator('[data-testid="primaryColumn"], main, [role="main"]').first
                if await main_content.count() > 0:
                    content_text = await main_content.inner_text()
                    logger.info("  - Main content preview: %s", content_text[:500] if content_text else "empty")
            except Exception as e:
                logger.warning("Error during diagnostic check: %s", e)

        return {
            "data": limited_tweets,
            "users": all_users,
            "media": {},
            "referenced_tweets": {},
        }

    async def get_owned_lists(self) -> Dict[str, Any]:
        """Get user's owned lists"""
        return await self._get_owned_lists_async()

    async def get_owned_lists_async(self) -> Dict[str, Any]:
        """Async version for use in async pipelines"""
        return await self._get_owned_lists_async()

    async def _get_owned_lists_async(self) -> Dict[str, Any]:
        """Async implementation of get_owned_lists"""
        await self._ensure_initialized()
        assert self.page is not None

        logger.info("Fetching owned lists")

        # Clear previous captures BEFORE navigation
        self._captured_responses.clear()

        # Use username for URL if available, otherwise fallback to generic lists URL
        if self.username:
            url_handle = self.username.lstrip('@')
            lists_url = f"https://x.com/{url_handle}/lists"
        else:
            lists_url = "https://x.com/i/lists"

        await self.page.goto(lists_url,
                            wait_until="domcontentloaded",
                            timeout=60000)
        await self._random_delay(3, 5)

        # Wait for lists to load
        await self._random_delay(2, 3)

        # Extract lists from responses
        lists = []
        for response in self._captured_responses:
            if "ListsManagementPageTimeline" in response["url"]:
                data = response["data"]
                # Parse lists from GraphQL response
                instructions = (
                    data.get("data", {})
                    .get("user", {})
                    .get("result", {})
                    .get("timeline_v2", {})
                    .get("timeline", {})
                    .get("instructions", [])
                )

                for instruction in instructions:
                    if instruction.get("type") == "TimelineAddEntries":
                        entries = instruction.get("entries", [])
                        for entry in entries:
                            content = entry.get("content", {})
                            if content.get("entryType") == "TimelineTimelineList":
                                list_item = content.get("itemContent", {}).get("list", {})
                                if list_item:
                                    lists.append({
                                        "id": list_item.get("id_str") or list_item.get("rest_id"),
                                        "name": list_item.get("name"),
                                        "member_count": list_item.get("member_count", 0),
                                    })

        # Fallback: try DOM scraping if GraphQL didn't work
        if not lists:
            lists = await self._scrape_lists_from_dom()

        logger.info("Collected %s lists", len(lists))

        return {"data": lists}

    async def _scrape_lists_from_dom(self) -> List[Dict[str, Any]]:
        """Fallback: scrape lists from DOM"""
        assert self.page is not None
        lists = []
        try:
            list_elements = await self.page.query_selector_all('[data-testid="list"]')
            for element in list_elements:
                try:
                    name_elem = await element.query_selector('[data-testid="listName"]')
                    name = await name_elem.inner_text() if name_elem else "Unknown"

                    # Try to extract list ID from link
                    link_elem = await element.query_selector("a")
                    href = await link_elem.get_attribute("href") if link_elem else ""
                    list_id = href.split("/")[-1] if href else None

                    if list_id:
                        lists.append({"id": list_id, "name": name, "member_count": 0})
                except Exception:
                    continue
        except Exception as e:
            logger.warning("Failed to scrape lists from DOM: %s", e)

        return lists

    async def get_list_members(self, list_id: str) -> Dict[str, Any]:
        """Get members of a specific list"""
        return await self._get_list_members_async(list_id)

    async def get_list_members_async(self, list_id: str) -> Dict[str, Any]:
        """Async version for use in async pipelines"""
        return await self._get_list_members_async(list_id)

    async def _get_list_members_async(self, list_id: str) -> Dict[str, Any]:
        """Async implementation of get_list_members"""
        await self._ensure_initialized()
        assert self.page is not None

        logger.info("Fetching members for list %s", list_id)

        # Clear previous captures BEFORE navigation to catch initial load responses
        self._captured_responses.clear()

        await self.page.goto(f"https://x.com/i/lists/{list_id}/members",
                            wait_until="domcontentloaded",
                            timeout=60000)
        await self._random_delay(3, 5)

        # Scroll to collect members
        responses = await self._scroll_and_collect(max_items=100)

        members = []
        users = {}

        for response in responses:
            if "ListMembers" in response["url"]:
                data = response["data"]
                # Parse members from GraphQL response
                instructions = (
                    data.get("data", {})
                    .get("list", {})
                    .get("members_timeline", {})
                    .get("timeline", {})
                    .get("instructions", [])
                )

                for instruction in instructions:
                    if instruction.get("type") == "TimelineAddEntries":
                        entries = instruction.get("entries", [])
                        for entry in entries:
                            content = entry.get("content", {})
                            item_content = content.get("itemContent", {})
                            user_result = item_content.get("user_results", {}).get("result", {})

                            if user_result:
                                user = extractors.map_user_to_standard_format(user_result)
                                if user and user.get("id"):
                                    members.append({"user_id": user["id"]})
                                    users[user["id"]] = user

        # Fallback: try DOM scraping
        if not members:
            members, users = await self._scrape_list_members_from_dom()

        logger.info("Collected %s list members", len(members))

        return {"data": members, "users": users}

    async def _scrape_list_members_from_dom(self) -> tuple[List[Dict[str, Any]], Dict[str, Dict[str, Any]]]:
        """Fallback: scrape list members from DOM"""
        assert self.page is not None
        members = []
        users = {}

        try:
            # Scroll to load members
            await self._scroll_and_collect(max_items=100)

            user_elements = await self.page.query_selector_all('[data-testid="UserCell"]')
            for element in user_elements:
                try:
                    # Extract user info
                    username_elem = await element.query_selector('[data-testid="UserName"]')
                    if username_elem:
                        username = await username_elem.inner_text()
                        # Extract user ID from link
                        link_elem = await element.query_selector("a")
                        href = await link_elem.get_attribute("href") if link_elem else ""
                        user_id = href.split("/")[-1] if href else None

                        if user_id:
                            members.append({"user_id": user_id})
                            users[user_id] = {
                                "id": user_id,
                                "username": username,
                                "name": username,
                            }
                except Exception:
                    continue
        except Exception as e:
            logger.warning("Failed to scrape list members from DOM: %s", e)

        return members, users

    async def close(self) -> None:
        """Close browser and cleanup resources"""
        if self.browser:
            await self.browser.close()
        if self.playwright:
            await self.playwright.stop()
        self._initialized = False

    def __del__(self):
        """Cleanup on deletion"""
        if self._initialized:
            try:
                # Try to close, but don't create new event loop if one exists
                try:
                    # If we're in an async context, can't use asyncio.run()
                    # Just skip cleanup in this case (GC will handle it)
                    asyncio.get_running_loop()
                except RuntimeError:
                    # No event loop running, safe to create one
                    try:
                        asyncio.run(self.close())
                    except Exception:
                        pass
            except Exception:
                pass
