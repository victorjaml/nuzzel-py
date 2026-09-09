"""
Twitter API Client

This module provides a unified interface for Twitter API operations with support
for both live API calls and mock data for testing.
"""

import json
import os
import logging
from datetime import datetime
from pathlib import Path
from typing import Dict, Any, List, Optional
from abc import ABC, abstractmethod

import xdk  # type: ignore[import-untyped]
from xdk.oauth1_auth import OAuth1  # type: ignore[import-untyped]
from pydantic import BaseModel, ConfigDict

from nuzzel.constants import TWEET_FIELDS, TWEET_EXPANSIONS, MEDIA_FIELDS
from nuzzel.utils.rate_limit_utils import sleep_with_jitter

# Configure logging
logger = logging.getLogger(__name__)


class TwitterAPIError(Exception):
    """Twitter API error"""


class XDKResponse(BaseModel):
    """Protocol for XDK API response objects with data, includes, and meta attributes"""
    data: List[Any]
    includes: Optional[Dict[str, Any]] = None
    meta: Optional[Dict[str, Any]] = None
    errors: Optional[List[Any]] = None
    model_config = ConfigDict(populate_by_name=True, extra="allow")




class TwitterClient(ABC):
    """Abstract base class for Twitter API client"""

    def _convert_xdk_response_to_dict(self, response: XDKResponse) -> Dict[str, Any]:
        """
        Convert XDK response object to standardized dictionary format.

        Args:
            response: XDK Pydantic model with data, includes, and meta attributes

        Returns:
            Dictionary with 'data', 'users', 'media', 'referenced_tweets', 'next_token'.
            'users' and 'media' are indexed by their ID for efficient lookups.
        """
        if hasattr(response, "errors"):
            logger.warning("Response errors: %s", response.errors)
        if not hasattr(response, "data"):
            msg = "Response missing data attribute."
            if hasattr(response, "errors"):
                msg += f" Response has errors: {response.errors}"
            raise TwitterAPIError(msg)
        result: Dict[str, Any] = {"data": response.data}
        if hasattr(response, "includes") and response.includes:
            if "users" in response.includes:
                result["users"] = {
                    user.get("id"): user
                    for user in response.includes["users"]
                    if user.get("id")
                }
            if "media" in response.includes:
                result["media"] = {
                    media.get("media_key"): media
                    for media in response.includes["media"]
                    if media.get("media_key")
                }
            if "tweets" in response.includes:
                # Convert referenced tweets list to a map for efficient lookups
                result["referenced_tweets"] = {
                    tweet.get("id"): tweet
                    for tweet in response.includes["tweets"]
                    if tweet.get("id")
                }
        if hasattr(response, "meta") and response.meta and "next_token" in response.meta:
            result["next_token"] = response.meta.get("next_token")
        return result

    @abstractmethod
    async def get_user_timeline(
        self, start_time: datetime, max_pages: int = 3
    ) -> Dict[str, Any]:
        """Get user's timeline tweets within time window"""

    @abstractmethod
    async def get_user_liked_tweets(self, max_results: int = 10) -> Dict[str, Any]:
        """Get user's recently liked tweets"""

    @abstractmethod
    async def get_user_tweets(self, max_results: int = 10) -> Dict[str, Any]:
        """Get user's recent tweets"""

    @abstractmethod
    async def get_user_id(self) -> str:
        """Get authenticated user ID"""

    @abstractmethod
    async def get_owned_lists(self) -> Dict[str, Any]:
        """Get user's owned lists"""

    @abstractmethod
    async def get_list_members(self, list_id: str) -> Dict[str, Any]:
        """Get members of a specific list"""


class LiveTwitterClient(TwitterClient):
    """Live Twitter API client using XDK"""

    def __init__(self, api_key: str, api_secret: str, access_token: str, access_token_secret: str):
        # Create OAuth1 auth instance for user context authentication
        oauth1_auth = OAuth1(
            api_key=api_key,
            api_secret=api_secret,
            callback="oob",  # Out-of-band callback for existing tokens
            access_token=access_token,
            access_token_secret=access_token_secret,
        )
        self.client = xdk.Client(
            auth=oauth1_auth,
        )
        self.user_id: Optional[str] = None

    async def get_user_id(self) -> str:
        """Get authenticated user's ID"""
        if self.user_id:
            return self.user_id

        response = self.client.users.get_me(user_fields=["id"])
        if hasattr(response, "data") and response.data:
            self.user_id = response.data["id"]
            return self.user_id or ""
        raise TwitterAPIError("Failed to get user ID: response missing data attribute")

    async def get_user_timeline(
        self, start_time: datetime, max_pages: int = 3
    ) -> Dict[str, Any]:
        """Get user's timeline tweets within time window
        https://docs.x.com/x-api/users/get-timeline
        """
        user_id = await self.get_user_id()
        tweets: List[Dict[str, Any]] = []
        users: Dict[str, Dict[str, Any]] = {}
        media: Dict[str, Dict[str, Any]] = {}
        referenced_tweets: Dict[str, Dict[str, Any]] = {}
        pages_fetched = 0
        pagination_token = None

        while pages_fetched < max_pages:
            # Format start_time as YYYY-MM-DDTHH:mm:ssZ (X API requires seconds precision, no microseconds)
            start_time_str = start_time.strftime("%Y-%m-%dT%H:%M:%SZ")
            response_iter = self.client.users.get_timeline(
                id=user_id,
                start_time=start_time_str,
                max_results=100,
                pagination_token=pagination_token,
                expansions=TWEET_EXPANSIONS,
                tweet_fields=TWEET_FIELDS,
                media_fields=MEDIA_FIELDS,
                user_fields=["username", "public_metrics"],
            )
            # ... (StopIteration logic remains same)

            # Get the first (and typically only) response from the iterator
            try:
                response = next(response_iter)
            except StopIteration:
                break

            response_dict = self._convert_xdk_response_to_dict(response)
            tweets.extend(response_dict["data"])
            users.update(response_dict.get("users", {}))
            media.update(response_dict.get("media", {}))
            referenced_tweets.update(response_dict.get("referenced_tweets", {}))

            # Check for next token
            if "next_token" in response_dict:
                pagination_token = response_dict["next_token"]
                pages_fetched += 1

                # Rate limit protection between pages
                if pages_fetched < max_pages:
                    logger.info(
                        "Fetched page %d, waiting 15 minutes before next page...",
                        pages_fetched
                    )
                    sleep_with_jitter(15 * 60)
            else:
                break

        return {
            "data": tweets,
            "users": users,
            "media": media,
            "referenced_tweets": referenced_tweets,
        }


    async def get_user_liked_tweets(self, max_results: int = 10) -> Dict[str, Any]:
        """Get user's recently liked tweets
        https://docs.x.com/x-api/users/get-liked-posts
        """
        user_id = await self.get_user_id()
        response_iter = self.client.users.get_liked_posts(
            id=user_id,
            max_results=max_results,
            expansions=TWEET_EXPANSIONS,
            tweet_fields=TWEET_FIELDS,
            media_fields=MEDIA_FIELDS,
        )

        # Get the first response from the iterator
        try:
            response = next(response_iter)
            return self._convert_xdk_response_to_dict(response)
        except StopIteration:
            return {
                "data": [],
                "users": {},
                "media": {},
                "referenced_tweets": {},
            }

    async def get_user_tweets(self, max_results: int = 10) -> Dict[str, Any]:
        """Get user's recent tweets
        https://docs.x.com/x-api/users/get-posts
        """
        user_id = await self.get_user_id()
        response_iter = self.client.users.get_posts(
            id=user_id,
            max_results=max_results,
            expansions=TWEET_EXPANSIONS,
            tweet_fields=TWEET_FIELDS,
            media_fields=MEDIA_FIELDS,
        )

        # Get the first response from the iterator
        try:
            response = next(response_iter)
            response_dict = self._convert_xdk_response_to_dict(response)
            return response_dict
        except StopIteration:
            return {
                "data": [],
                "users": {},
                "media": {},
                "referenced_tweets": {},
            }

    async def get_owned_lists(self, max_results: int = 10) -> Dict[str, Any]:
        """Get user's owned lists
        https://docs.x.com/x-api/users/get-owned-lists
        """
        user_id = await self.get_user_id()
        response_iter = self.client.users.get_owned_lists(
            id=user_id, max_results=max_results, list_fields=["id", "name", "member_count"]
        )

        # Get the first response from the iterator
        try:
            response = next(response_iter)
            return self._convert_xdk_response_to_dict(response)
        except StopIteration:
            return {"data": []}

    async def get_list_members(self, list_id: str) -> Dict[str, Any]:
        """Get members of a specific list
        https://docs.x.com/x-api/lists/get-list-members
        """
        members: List[Dict[str, Any]] = []
        users: Dict[str, Dict[str, Any]] = {}
        pages_fetched = 0
        pagination_token = None
        max_pages = 3

        while pages_fetched < max_pages:
            response_iter = self.client.lists.get_members(
                id=list_id,
                max_results=100,
                pagination_token=pagination_token,
                expansions=["affiliation.user_id"],
                user_fields=["id"],
            )

            try:
                response = next(response_iter)
            except StopIteration:
                break

            response_dict = self._convert_xdk_response_to_dict(response)
            members.extend(response_dict.get("data", []))
            users.update(response_dict.get("users", {}))

            # Check for next token
            if "next_token" in response_dict:
                pagination_token = response_dict["next_token"]
                pages_fetched += 1
            else:
                break

        return {"data": members, "users": users}


class MockTwitterClient(TwitterClient):
    """Mock Twitter API client for testing"""

    def __init__(self, fixtures_dir: str = "tests/fixtures/twitter_api"):
        self.fixtures_dir = Path(__file__).parent.parent / fixtures_dir
        self.call_count = 0
        self.user_id: Optional[str] = None

    def _load_fixture(self, filename: str) -> Dict[str, Any]:
        """Load JSON fixture file"""
        fixture_path = self.fixtures_dir / filename
        if not fixture_path.exists():
            raise FileNotFoundError(f"Mock fixture not found: {fixture_path}")

        with open(fixture_path, "r", encoding="utf-8") as f:
            return json.load(f)

    def _convert_to_dict_format(self, data: Dict[str, Any]) -> Dict[str, Any]:
        """Convert fixture data from list format to dictionary format"""
        result = {"data": data.get("data", [])}

        # Convert users list to dictionary
        if "users" in data:
            users_list = data["users"]
            if isinstance(users_list, list):
                result["users"] = {
                    user.get("id"): user
                    for user in users_list
                    if user.get("id")
                }
            else:
                result["users"] = users_list
        else:
            result["users"] = {}

        # Convert media list to dictionary
        if "media" in data:
            media_list = data["media"]
            if isinstance(media_list, list):
                result["media"] = {
                    media.get("media_key"): media
                    for media in media_list
                    if media.get("media_key")
                }
            else:
                result["media"] = media_list
        else:
            result["media"] = {}

        # Add referenced_tweets dictionary (empty if not present)
        if "referenced_tweets" in data:
            referenced_list = data["referenced_tweets"]
            if isinstance(referenced_list, list):
                result["referenced_tweets"] = {
                    tweet.get("id"): tweet
                    for tweet in referenced_list
                    if tweet.get("id")
                }
            else:
                result["referenced_tweets"] = referenced_list
        else:
            result["referenced_tweets"] = {}

        return result

    async def get_user_id(self) -> str:
        """Get mock user ID"""
        if self.user_id:
            return self.user_id
        data = self._load_fixture("user_me.json")
        self.user_id = data["data"]["id"]
        return self.user_id or ""

    async def get_user_timeline(
        self, start_time: datetime, max_pages: int = 3
    ) -> Dict[str, Any]:
        """Get mock timeline data"""
        data = self._load_fixture("timeline_single_page.json")
        # Create Pydantic model matching XDK response structure
        mock_response = XDKResponse(**data)
        # Use the same conversion as live client
        return self._convert_xdk_response_to_dict(mock_response)

    async def get_user_liked_tweets(self, max_results: int = 10) -> Dict[str, Any]:
        """Get mock liked tweets"""
        data = self._load_fixture("liked_tweets.json")
        # Create Pydantic model matching XDK response structure
        mock_response = XDKResponse(**data)
        # Use the same conversion as live client
        return self._convert_xdk_response_to_dict(mock_response)

    async def get_user_tweets(self, max_results: int = 10) -> Dict[str, Any]:
        """Get mock user tweets"""
        data = self._load_fixture("user_posts.json")
        # Create Pydantic model matching XDK response structure
        mock_response = XDKResponse(**data)
        # Use the same conversion as live client
        return self._convert_xdk_response_to_dict(mock_response)

    async def get_owned_lists(self) -> Dict[str, Any]:
        """Get mock owned lists"""
        return self._load_fixture("lists.json")

    async def get_list_members(self, list_id: str) -> Dict[str, Any]:
        """Get mock list members"""
        # Return mock data for the requested list
        filename = f"list_members_{list_id}.json"
        try:
            return self._load_fixture(filename)
        except FileNotFoundError:
            # Return empty list if specific fixture doesn't exist
            return {"data": []}


def create_twitter_client() -> TwitterClient:
    """
    Factory function to create appropriate Twitter client based on environment.

    Returns:
        TwitterClient instance
    """
    use_mock = os.getenv("USE_MOCK", "false").lower() == "true"
    twitter_client_type = os.getenv("TWITTER_CLIENT_TYPE", "browser").lower()  # browser or xdk

    if use_mock:
        logger.info("Using mock Twitter API client")
        return MockTwitterClient()
    elif twitter_client_type == "browser":
        # Prevent circular import
        from nuzzel.browser_twitter_client import BrowserTwitterClient

        cookies_json = os.getenv("TWITTER_SESSION_COOKIES")

        # Strip whitespace and validate cookies_json is not empty and is valid JSON
        if cookies_json:
            cookies_json = cookies_json.strip()
            if not cookies_json:
                cookies_json = None
                logger.warning(
                    "TWITTER_SESSION_COOKIES is empty. Falling back to cookies.json or username/password."
                )
            else:
                # Validate that cookies_json is valid JSON
                try:
                    json.loads(cookies_json)
                except json.JSONDecodeError as e:
                    cookies_json = None
                    logger.warning(
                        "TWITTER_SESSION_COOKIES contains invalid JSON: %s. Falling back to cookies.json or username/password.",
                        e,
                    )

        if not cookies_json:
            cookies_path = Path("cookies.json")
            if cookies_path.is_file() and cookies_path.stat().st_size > 0:
                try:
                    cookies_json = cookies_path.read_text(encoding="utf-8").strip()
                    json.loads(cookies_json)
                    logger.info("Loaded cookies from cookies.json")
                except json.JSONDecodeError as e:
                    cookies_json = None
                    logger.warning("cookies.json contains invalid JSON: %s", e)

        username = os.getenv("TWITTER_USERNAME")
        password = os.getenv("TWITTER_PASSWORD")
        headless = os.getenv("BROWSER_HEADLESS", "true").lower() == "true"

        # Auto-detect auth method based on provided parameters
        if cookies_json:
            logger.info("Using browser Twitter client with cookie authentication")
        elif username and password:
            logger.info("Using browser Twitter client with login authentication")
        else:
            raise ValueError(
                "TWITTER_SESSION_COOKIES or TWITTER_USERNAME and TWITTER_PASSWORD must be set"
            )

        return BrowserTwitterClient(
            cookies_json=cookies_json,
            username=username,
            password=password,
            headless=headless,
        )
    elif twitter_client_type == "xdk":
        api_key = os.getenv("TWITTER_API_KEY")
        api_secret = os.getenv("TWITTER_API_SECRET")
        access_token = os.getenv("TWITTER_ACCESS_TOKEN")
        access_token_secret = os.getenv("TWITTER_ACCESS_TOKEN_SECRET")
        if not api_key or not api_secret or not access_token or not access_token_secret:
            raise ValueError(
                "TWITTER_API_KEY, TWITTER_API_SECRET, TWITTER_ACCESS_TOKEN, and TWITTER_ACCESS_TOKEN_SECRET must be set"
            )
        logger.info("Using XDK Twitter API client")
        return LiveTwitterClient(api_key, api_secret, access_token, access_token_secret)
    else:
        raise ValueError(f"Invalid Twitter client type: {twitter_client_type}")
