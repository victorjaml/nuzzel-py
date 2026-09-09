"""Unit tests for browser-based Twitter client"""

import json
import os
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from nuzzel.browser_twitter_client import BrowserTwitterClient


@pytest.fixture
def mock_cookies_json():
    """Sample cookies JSON for testing"""
    return json.dumps([
        {
            "name": "auth_token",
            "value": "test_token",
            "domain": ".twitter.com",
            "path": "/",
        }
    ])


@pytest.fixture
def sample_graphql_response():
    """Sample GraphQL response from Twitter"""
    return {
        "data": {
            "user": {
                "result": {
                    "timeline_v2": {
                        "timeline": {
                            "instructions": [
                                {
                                    "type": "TimelineAddEntries",
                                    "entries": [
                                        {
                                            "content": {
                                                "entryType": "TimelineTimelineItem",
                                                "itemContent": {
                                                    "tweet_results": {
                                                        "result": {
                                                            "rest_id": "123456789",
                                                            "legacy": {
                                                                "id_str": "123456789",
                                                                "full_text": "Test tweet",
                                                                "created_at": "Wed Oct 10 20:19:24 +0000 2024",
                                                                "favorite_count": 10,
                                                                "retweet_count": 5,
                                                                "reply_count": 2,
                                                                "entities": {
                                                                    "urls": [],
                                                                },
                                                            },
                                                            "core": {
                                                                "user_results": {
                                                                    "result": {
                                                                        "rest_id": "987654321",
                                                                        "legacy": {
                                                                            "screen_name": "testuser",
                                                                            "name": "Test User",
                                                                            "followers_count": 100,
                                                                        },
                                                                    },
                                                                },
                                                            },
                                                        },
                                                    },
                                                },
                                            },
                                        },
                                    ],
                                },
                            ],
                        },
                    },
                },
            },
        },
    }


class TestBrowserTwitterClient:
    """Test suite for BrowserTwitterClient"""

    @pytest.mark.asyncio
    async def test_cookie_authentication(self, mock_cookies_json):
        """Test cookie-based authentication"""
        with patch("nuzzel.browser_twitter_client.async_playwright") as mock_playwright_func:
            mock_browser = AsyncMock()
            mock_context = AsyncMock()
            mock_page = AsyncMock()

            mock_playwright_instance = AsyncMock()
            mock_playwright_instance.chromium.launch = AsyncMock(return_value=mock_browser)
            mock_playwright_func.return_value.start = AsyncMock(return_value=mock_playwright_instance)
            mock_browser.new_context = AsyncMock(return_value=mock_context)
            mock_context.new_page = AsyncMock(return_value=mock_page)
            mock_page.goto = AsyncMock()
            mock_page.evaluate = AsyncMock(return_value="123456789")
            mock_page.on = MagicMock()
            mock_context.add_cookies = AsyncMock()

            client = BrowserTwitterClient(
                cookies_json=mock_cookies_json,
                headless=True,
            )

            await client._ensure_initialized()

            assert client._initialized is True
            mock_context.add_cookies.assert_called_once()
            mock_page.goto.assert_called()

    @pytest.mark.asyncio
    async def test_login_authentication(self):
        """Test login authentication"""
        with patch("nuzzel.browser_twitter_client.async_playwright") as mock_playwright_func, \
             patch("nuzzel.browser_utils.auth.login_with_credentials") as mock_login:

            mock_browser = AsyncMock()
            mock_context = AsyncMock()
            mock_page = AsyncMock()

            mock_playwright_instance = AsyncMock()
            mock_playwright_instance.chromium.launch = AsyncMock(return_value=mock_browser)
            mock_playwright_func.return_value.start = AsyncMock(return_value=mock_playwright_instance)
            mock_browser.new_context = AsyncMock(return_value=mock_context)
            mock_context.new_page = AsyncMock(return_value=mock_page)
            mock_page.goto = AsyncMock()
            mock_page.evaluate = AsyncMock(return_value="123456789")
            mock_page.on = MagicMock()
            mock_login.return_value = None

            client = BrowserTwitterClient(
                username="testuser",
                password="testpass",
                headless=True,
            )

            await client._ensure_initialized()

            assert client._initialized is True
            mock_login.assert_called_once()

    @pytest.mark.asyncio
    async def test_get_user_id_from_env_var(self, mock_cookies_json):
        """Test get_user_id method when TWITTER_USER_ID env var is set"""
        with patch.dict(os.environ, {"TWITTER_USER_ID": "999888777"}):
            client = BrowserTwitterClient(
                cookies_json=mock_cookies_json,
                headless=True,
            )

            # Should return env var value without initializing browser
            user_id = await client.get_user_id()
            assert user_id == "999888777"
            assert client._initialized is False  # Should not initialize browser

    def test_get_user_id_from_api(self, mock_cookies_json):
        """Test get_user_id method from API when env var is not set"""
        with patch("nuzzel.browser_twitter_client.xdk") as mock_xdk, \
             patch("nuzzel.browser_twitter_client.OAuth1") as mock_oauth1, \
             patch.dict(os.environ, {
                 "TWITTER_API_KEY": "test_api_key",
                 "TWITTER_API_SECRET": "test_api_secret",
                 "TWITTER_ACCESS_TOKEN": "test_access_token",
                 "TWITTER_ACCESS_TOKEN_SECRET": "test_access_token_secret",
             }):

            # Mock XDK client and response
            mock_client = MagicMock()
            mock_response = MagicMock()
            mock_response.data = {"id": "123456789"}
            mock_client.users.get_me.return_value = mock_response
            mock_xdk.Client.return_value = mock_client

            client = BrowserTwitterClient(
                cookies_json=mock_cookies_json,
                headless=True,
            )

            # Test API method directly (now synchronous)
            user_id = client._get_user_id_from_api()
            assert user_id == "123456789"

            # Verify XDK client was created and API call was made
            mock_oauth1.assert_called_once()
            mock_xdk.Client.assert_called_once()
            mock_client.users.get_me.assert_called_once_with(user_fields=["id"])

    @pytest.mark.asyncio
    async def test_response_interception(self, mock_cookies_json, sample_graphql_response):
        """Test response interception captures GraphQL responses"""
        with patch("nuzzel.browser_twitter_client.async_playwright") as mock_playwright_func:
            mock_browser = AsyncMock()
            mock_context = AsyncMock()
            mock_page = AsyncMock()
            mock_response = AsyncMock()
            mock_response.url = "https://api.twitter.com/2/HomeTimeline"
            mock_response.json = AsyncMock(return_value=sample_graphql_response)

            mock_playwright_instance = AsyncMock()
            mock_playwright_instance.chromium.launch = AsyncMock(return_value=mock_browser)
            mock_playwright_func.return_value.start = AsyncMock(return_value=mock_playwright_instance)
            mock_browser.new_context = AsyncMock(return_value=mock_context)
            mock_context.new_page = AsyncMock(return_value=mock_page)
            mock_page.goto = AsyncMock()
            mock_page.evaluate = AsyncMock(return_value="123456789")

            # Track response handlers
            response_handlers = []
            def capture_handler(event, handler):
                if event == "response":
                    response_handlers.append(handler)
            mock_page.on = MagicMock(side_effect=capture_handler)
            mock_context.add_cookies = AsyncMock()

            client = BrowserTwitterClient(
                cookies_json=mock_cookies_json,
                headless=True,
            )

            await client._ensure_initialized()

            # Simulate response event using captured handler
            if response_handlers:
                await response_handlers[0](mock_response)

            assert len(client._captured_responses) > 0
            assert "HomeTimeline" in client._captured_responses[0]["url"]

    @pytest.mark.asyncio
    async def test_scroll_and_collect(self, mock_cookies_json):
        """Test scroll and collect functionality"""
        with patch("nuzzel.browser_twitter_client.async_playwright") as mock_playwright_func, \
             patch("nuzzel.browser_utils.stealth.human_like_scroll") as mock_scroll, \
             patch("nuzzel.browser_utils.stealth.random_mouse_movement") as mock_mouse:

            mock_browser = AsyncMock()
            mock_context = AsyncMock()
            mock_page = AsyncMock()

            mock_playwright_instance = AsyncMock()
            mock_playwright_instance.chromium.launch = AsyncMock(return_value=mock_browser)
            mock_playwright_func.return_value.start = AsyncMock(return_value=mock_playwright_instance)
            mock_browser.new_context = AsyncMock(return_value=mock_context)
            mock_context.new_page = AsyncMock(return_value=mock_page)
            mock_page.goto = AsyncMock()
            mock_page.evaluate = AsyncMock(return_value=1000)
            mock_page.wait_for_timeout = AsyncMock()
            mock_page.on = MagicMock()
            mock_context.add_cookies = AsyncMock()
            mock_scroll.return_value = None
            mock_mouse.return_value = None

            client = BrowserTwitterClient(
                cookies_json=mock_cookies_json,
                headless=True,
            )

            await client._ensure_initialized()

            # Test scroll and collect
            collected = await client._scroll_and_collect(max_items=10, scroll_timeout=5)

            assert isinstance(collected, list)
            mock_scroll.assert_called()

    @pytest.mark.asyncio
    async def test_missing_auth_raises_error(self):
        """Test that missing authentication credentials raises ValueError"""
        with patch("nuzzel.browser_twitter_client.async_playwright") as mock_playwright_func:
            mock_browser = AsyncMock()
            mock_context = AsyncMock()
            mock_page = AsyncMock()

            mock_playwright_instance = AsyncMock()
            mock_playwright_instance.chromium.launch = AsyncMock(return_value=mock_browser)
            mock_playwright_func.return_value.start = AsyncMock(return_value=mock_playwright_instance)
            mock_browser.new_context = AsyncMock(return_value=mock_context)
            mock_context.new_page = AsyncMock(return_value=mock_page)
            mock_page.on = MagicMock()

            client = BrowserTwitterClient(
                cookies_json=None,
                username=None,
                password=None,
            )
            # This will raise during initialization when checking auth
            with pytest.raises(ValueError, match="Either cookies_json or"):
                await client._ensure_initialized()

    @pytest.mark.asyncio
    async def test_close_cleanup(self, mock_cookies_json):
        """Test that close() properly cleans up resources"""
        with patch("nuzzel.browser_twitter_client.async_playwright") as mock_playwright_func:
            mock_browser = AsyncMock()
            mock_context = AsyncMock()
            mock_page = AsyncMock()

            mock_playwright_instance = AsyncMock()
            mock_playwright_instance.chromium.launch = AsyncMock(return_value=mock_browser)
            mock_playwright_instance.stop = AsyncMock()
            mock_playwright_func.return_value.start = AsyncMock(return_value=mock_playwright_instance)
            mock_browser.new_context = AsyncMock(return_value=mock_context)
            mock_browser.close = AsyncMock()
            mock_context.new_page = AsyncMock(return_value=mock_page)
            mock_page.goto = AsyncMock()
            mock_page.evaluate = AsyncMock(return_value="123456789")
            mock_page.on = MagicMock()
            mock_context.add_cookies = AsyncMock()

            client = BrowserTwitterClient(
                cookies_json=mock_cookies_json,
                headless=True,
            )

            await client._ensure_initialized()
            await client.close()

            mock_browser.close.assert_called_once()
            mock_playwright_instance.stop.assert_called_once()
            assert client._initialized is False

    @pytest.mark.asyncio
    async def test_profile_url_handle_prefers_session_over_stale_username(self):
        """Likes/profile URLs should use the logged-in handle, not TWITTER_USERNAME."""
        client = BrowserTwitterClient(username="_oldhandle", cookies_json="[]")
        profile_link = AsyncMock()
        profile_link.count = AsyncMock(return_value=1)
        profile_link.get_attribute = AsyncMock(return_value="/currenthandle")

        locator = MagicMock()
        locator.first = profile_link
        client.page = MagicMock()
        client.page.locator = MagicMock(return_value=locator)

        handle = await client._profile_url_handle()
        assert handle == "currenthandle"

    @pytest.mark.asyncio
    async def test_profile_url_handle_falls_back_to_username(self):
        client = BrowserTwitterClient(username="@envuser", cookies_json="[]")
        missing_link = AsyncMock()
        missing_link.count = AsyncMock(return_value=0)
        locator = MagicMock()
        locator.first = missing_link
        client.page = MagicMock()
        client.page.locator = MagicMock(return_value=locator)

        handle = await client._profile_url_handle()
        assert handle == "envuser"
