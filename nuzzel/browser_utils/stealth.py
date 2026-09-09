"""Stealth measures to avoid bot detection"""

import random
import sys
from typing import Optional

from playwright.async_api import BrowserContext, Page
from playwright_stealth import Stealth


def _navigator_platform() -> str:
    if sys.platform.startswith("linux"):
        return "Linux x86_64"
    if sys.platform == "darwin":
        return "MacIntel"
    return "Win32"


async def apply_stealth_measures(context: BrowserContext) -> None:
    """
    Apply stealth measures to browser context to avoid detection.

    Args:
        context: Playwright browser context
    """
    await Stealth(
        navigator_platform_override=_navigator_platform(),
        navigator_languages_override=("en-US", "en"),
    ).apply_stealth_async(context)


async def human_like_scroll(page: Page, distance: Optional[int] = None) -> None:
    """
    Perform human-like scrolling with random variations.

    Args:
        page: Playwright page instance
        distance: Optional scroll distance, otherwise random
    """
    if distance is None:
        distance = random.randint(300, 800)

    # Scroll with easing (simulate human behavior)
    steps = random.randint(3, 6)
    step_distance = distance // steps

    for i in range(steps):
        scroll_amount = step_distance * (i + 1)
        await page.evaluate(f"window.scrollBy(0, {scroll_amount})")
        await page.wait_for_timeout(random.randint(100, 300))


async def random_mouse_movement(page: Page) -> None:
    """
    Perform random mouse movements to appear more human.

    Args:
        page: Playwright page instance
    """
    try:
        # Move mouse to random position
        x = random.randint(100, 800)
        y = random.randint(100, 600)
        await page.mouse.move(x, y)
        await page.wait_for_timeout(random.randint(50, 200))
    except Exception:
        # Ignore errors - mouse movement is optional
        pass
