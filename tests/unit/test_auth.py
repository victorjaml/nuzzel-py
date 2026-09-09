"""Unit tests for browser cookie authentication helpers"""

import json
from unittest.mock import AsyncMock

import pytest

from nuzzel.browser_utils.auth import inject_cookies, prepare_cookies


def test_prepare_cookies_mirrors_x_and_twitter_domains():
    cookies = prepare_cookies(
        [
            {
                "name": "auth_token",
                "value": "secret",
                "domain": ".twitter.com",
                "path": "/",
                "secure": True,
                "httpOnly": True,
                "sameSite": "None",
            },
            {
                "name": "ct0",
                "value": "csrf",
                "domain": ".x.com",
                "path": "/",
                "secure": True,
            },
        ]
    )

    by_name = {}
    for cookie in cookies:
        by_name.setdefault(cookie["name"], set()).add(cookie["domain"])

    assert by_name["auth_token"] == {".x.com", ".twitter.com"}
    assert by_name["ct0"] == {".x.com", ".twitter.com"}
    auth_token = next(c for c in cookies if c["name"] == "auth_token")
    assert auth_token["sameSite"] == "None"
    assert auth_token["secure"] is True
    assert "expires" not in auth_token


def test_prepare_cookies_leaves_other_domains_alone():
    cookies = prepare_cookies(
        [
            {
                "name": "guest_id",
                "value": "v1%3A123",
                "domain": ".ads-twitter.com",
                "path": "/",
            }
        ]
    )

    assert len(cookies) == 1
    assert cookies[0]["domain"] == ".ads-twitter.com"


def test_prepare_cookies_skips_cloudflare_bot_cookies():
    cookies = prepare_cookies(
        [
            {"name": "auth_token", "value": "secret", "domain": ".x.com", "path": "/"},
            {"name": "__cf_bm", "value": "bot-mgmt", "domain": ".x.com", "path": "/"},
            {"name": "cf_clearance", "value": "cleared", "domain": ".x.com", "path": "/"},
            {"name": "_cfuvid", "value": "vid", "domain": ".x.com", "path": "/"},
        ]
    )

    names = {cookie["name"] for cookie in cookies}
    assert "auth_token" in names
    assert "__cf_bm" not in names
    assert "cf_clearance" not in names
    assert "_cfuvid" not in names


def test_prepare_cookies_dedupes_existing_both_domains():
    cookies = prepare_cookies(
        [
            {"name": "auth_token", "value": "secret", "domain": ".x.com", "path": "/"},
            {"name": "auth_token", "value": "secret", "domain": ".twitter.com", "path": "/"},
        ]
    )

    pairs = {(c["name"], c["domain"]) for c in cookies}
    assert pairs == {("auth_token", ".x.com"), ("auth_token", ".twitter.com")}


@pytest.mark.asyncio
async def test_inject_cookies_adds_prepared_cookies():
    context = AsyncMock()
    payload = json.dumps(
        [{"name": "auth_token", "value": "secret", "domain": ".twitter.com", "path": "/"}]
    )

    await inject_cookies(context, payload)

    context.add_cookies.assert_called_once()
    added = context.add_cookies.call_args[0][0]
    assert {c["domain"] for c in added} == {".x.com", ".twitter.com"}
