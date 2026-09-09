#!/usr/bin/env python3
"""
Debug script to visualize Twitter UI and diagnose selector issues.

This script runs with headless=False so you can see exactly what's happening,
and captures screenshots and HTML at key steps.
"""

import asyncio
import json
import logging
import sys
from pathlib import Path

# Add project root to path
sys.path.insert(0, str(Path(__file__).parent.parent))

from playwright.async_api import async_playwright

from nuzzel.browser_utils import auth

logging.basicConfig(
    level=logging.DEBUG,
    format='%(asctime)s - %(name)s - %(levelname)s - %(message)s'
)
logger = logging.getLogger(__name__)


async def debug_twitter_ui():
    """Debug Twitter UI elements and GraphQL interception"""

    # Load cookies
    cookies_path = Path(__file__).parent.parent / "cookies.json"
    if not cookies_path.exists():
        logger.error(f"Cookies file not found: {cookies_path}")
        return

    with open(cookies_path) as f:
        cookies_data = json.load(f)

    # Create debug output directory
    debug_dir = Path(__file__).parent.parent / "debug_output"
    debug_dir.mkdir(exist_ok=True)

    captured_responses = []
    all_responses = []  # Capture ALL responses for debugging

    async def handle_response(response):
        url = response.url
        all_responses.append(url)

        # Log ALL Twitter API responses
        if "twitter.com" in url or "x.com" in url:
            if "api" in url or "graphql" in url:
                logger.info(f"API Response: {url[:200]}")
                try:
                    data = await response.json()
                    captured_responses.append({"url": url, "data": data})
                except Exception:
                    pass

    playwright = await async_playwright().start()

    try:
        # Launch browser VISIBLE (not headless)
        browser = await playwright.chromium.launch(
            headless=False,
            args=[
                "--disable-blink-features=AutomationControlled",
                "--disable-dev-shm-usage",
            ],
            slow_mo=500,  # Slow down actions so we can see them
        )

        context = await browser.new_context(
            viewport={"width": 1920, "height": 1080},
            locale="en-US",
        )

        page = await context.new_page()
        page.on("response", handle_response)

        await auth.inject_cookies(context, json.dumps(cookies_data))

        # Navigate to Twitter home
        logger.info("Navigating to Twitter home...")
        await page.goto("https://x.com/home", wait_until="domcontentloaded", timeout=60000)
        await asyncio.sleep(5)

        # Dismiss any popups (premium upsell, etc.)
        logger.info("\n=== DISMISSING POPUPS ===")
        try:
            close_selectors = [
                'button[aria-label*="Close"]',
                'button[aria-label*="close"]',
                '[data-testid*="close"]',
                '[data-testid="app-bar-close"]',
                'button:has-text("Not now")',
                'button:has-text("Skip")',
            ]

            for selector in close_selectors:
                try:
                    close_btn = page.locator(selector).first
                    if await close_btn.count() > 0 and await close_btn.is_visible():
                        parent = await close_btn.evaluate_handle("el => el.closest('[role=\"dialog\"], [role=\"alertdialog\"], [data-testid*=\"modal\"], [data-testid*=\"popup\"]')")
                        if parent.as_element():
                            logger.info(f"Found and clicking popup close button: {selector}")
                            await close_btn.click()
                            await asyncio.sleep(1)
                            break
                except Exception as e:
                    logger.debug(f"Close selector {selector} failed: {e}")

            # Try Escape key as fallback
            modal = page.locator('[role="dialog"], [role="alertdialog"]').first
            if await modal.count() > 0 and await modal.is_visible():
                logger.info("Modal still visible, pressing Escape")
                await page.keyboard.press("Escape")
                await asyncio.sleep(1)
        except Exception as e:
            logger.debug(f"Error dismissing popups: {e}")

        # Screenshot 1: Initial page (after popup dismissal)
        await page.screenshot(path=str(debug_dir / "01_initial_page.png"))
        logger.info("Saved screenshot: 01_initial_page.png")

        # Save page HTML
        html = await page.content()
        with open(debug_dir / "01_initial_page.html", "w") as f:
            f.write(html)
        logger.info("Saved HTML: 01_initial_page.html")

        # Debug: Find all tab-like elements
        logger.info("\n=== DEBUGGING TAB ELEMENTS ===")

        tab_selectors = [
            '[role="tab"]',
            '[role="tablist"] > *',
            'a[href="/home"]',
            'nav a',
            '[data-testid*="tab"]',
            '[aria-selected]',
        ]

        for selector in tab_selectors:
            try:
                elements = await page.query_selector_all(selector)
                if elements:
                    logger.info(f"\nFound {len(elements)} elements matching: {selector}")
                    for i, elem in enumerate(elements[:5]):  # Limit to first 5
                        text = await elem.inner_text()
                        tag = await elem.evaluate("el => el.tagName")
                        attrs = await elem.evaluate("""el => {
                            return Array.from(el.attributes).map(a => `${a.name}="${a.value}"`).join(' ');
                        }""")
                        logger.info(f"  [{i}] <{tag} {attrs[:200]}> Text: {text[:50]}")
            except Exception as e:
                logger.debug(f"Selector {selector} failed: {e}")

        # Debug: Find elements containing "Following" text
        logger.info("\n=== FINDING 'FOLLOWING' ELEMENTS ===")
        following_elements = await page.query_selector_all('text="Following"')
        logger.info(f"Found {len(following_elements)} elements with text 'Following'")

        for i, elem in enumerate(following_elements):
            try:
                box = await elem.bounding_box()
                parent = await elem.evaluate("el => el.parentElement?.outerHTML.slice(0, 500)")
                logger.info(f"  [{i}] Box: {box}, Parent: {parent[:200] if parent else 'None'}")
            except Exception as e:
                logger.info(f"  [{i}] Error: {e}")

        # Try clicking on Following
        logger.info("\n=== ATTEMPTING TO CLICK FOLLOWING ===")

        # Try different approaches to find and click Following tab
        following_clicked = False
        click_selectors = [
            # Most specific first
            '[role="tab"][aria-selected="false"]:has-text("Following")',
            '[role="tab"]:has-text("Following")',
            'div[role="presentation"]:has-text("Following")',
            'a:has-text("Following")',
            'span:has-text("Following")',
            'text="Following"',
        ]

        for selector in click_selectors:
            try:
                logger.info(f"Trying selector: {selector}")
                elem = page.locator(selector).first
                if await elem.count() > 0:
                    # Check if visible
                    is_visible = await elem.is_visible()
                    logger.info(f"  Found element, visible: {is_visible}")

                    if is_visible:
                        await elem.click()
                        await asyncio.sleep(2)
                        await page.screenshot(path=str(debug_dir / "02_after_following_click.png"))
                        logger.info("  CLICKED! Saved screenshot: 02_after_following_click.png")
                        following_clicked = True
                        break
            except Exception as e:
                logger.debug(f"  Failed: {e}")

        if not following_clicked:
            logger.warning("Could not click Following tab")

        # Debug: Look for sort/dropdown elements
        logger.info("\n=== FINDING SORT/DROPDOWN ELEMENTS ===")

        dropdown_selectors = [
            '[aria-haspopup="menu"]',
            '[aria-expanded]',
            'button[aria-label*="Sort"]',
            '[data-testid*="sort"]',
            '[data-testid*="dropdown"]',
            'svg[aria-label*="arrow"]',
        ]

        for selector in dropdown_selectors:
            try:
                elements = await page.query_selector_all(selector)
                if elements:
                    logger.info(f"\nFound {len(elements)} elements matching: {selector}")
                    for i, elem in enumerate(elements[:3]):
                        text = await elem.inner_text()
                        attrs = await elem.evaluate("""el => {
                            return Array.from(el.attributes).map(a => `${a.name}="${a.value}"`).join(' ');
                        }""")
                        logger.info(f"  [{i}] Attrs: {attrs[:200]}, Text: {text[:50] if text else 'empty'}")
            except Exception as e:
                logger.debug(f"Selector {selector} failed: {e}")

        # Try to find the dropdown arrow next to Following (from screenshot)
        logger.info("\n=== LOOKING FOR DROPDOWN ARROW NEAR FOLLOWING ===")

        # The dropdown in the screenshot appears as a small arrow/chevron
        arrow_selectors = [
            '[aria-label="Sort"]',
            '[data-testid="sortDropdown"]',
            # Look for svg/icon near Following
            '[role="tab"]:has-text("Following") svg',
            '[role="tab"]:has-text("Following") + button',
            # Generic dropdown triggers
            'button:has(svg[viewBox])',
        ]

        for selector in arrow_selectors:
            try:
                elem = page.locator(selector).first
                if await elem.count() > 0 and await elem.is_visible():
                    logger.info(f"Found dropdown element: {selector}")
                    await elem.click()
                    await asyncio.sleep(2)
                    await page.screenshot(path=str(debug_dir / "03_dropdown_opened.png"))
                    logger.info("Clicked dropdown, saved screenshot: 03_dropdown_opened.png")
                    break
            except Exception as e:
                logger.debug(f"Dropdown selector {selector} failed: {e}")

        # Now scroll and see if we capture responses
        logger.info("\n=== SCROLLING TO CAPTURE RESPONSES ===")
        captured_responses.clear()

        for i in range(5):
            await page.evaluate("window.scrollBy(0, 500)")
            await asyncio.sleep(2)
            logger.info(f"Scroll {i+1}: Captured {len(captured_responses)} responses so far")

        await page.screenshot(path=str(debug_dir / "04_after_scroll.png"))

        # Log captured response URLs
        logger.info("\n=== CAPTURED API RESPONSES ===")
        for resp in captured_responses:
            url = resp["url"]
            if "HomeTimeline" in url:
                logger.info(f"  [HomeTimeline] {url[:100]}")
                # Check if data has tweets
                data = resp["data"]
                logger.info(f"    Data keys: {list(data.keys()) if isinstance(data, dict) else type(data)}")
            elif "Following" in url or "Timeline" in url:
                logger.info(f"  [Timeline] {url[:100]}")

        logger.info(f"\nTotal API responses captured: {len(captured_responses)}")
        logger.info(f"Total all responses: {len(all_responses)}")

        # Save all captured responses
        with open(debug_dir / "captured_responses.json", "w") as f:
            json.dump([{"url": r["url"]} for r in captured_responses], f, indent=2)

        logger.info("\n=== DEBUG COMPLETE ===")
        logger.info(f"Check {debug_dir} for screenshots and HTML")

        # Keep browser open for manual inspection
        input("\nPress Enter to close browser...")

    finally:
        await browser.close()
        await playwright.stop()


if __name__ == "__main__":
    asyncio.run(debug_twitter_ui())
