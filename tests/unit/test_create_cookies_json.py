"""Unit tests for DevTools cookie TSV parsing"""

import importlib.util
from pathlib import Path


def _load_script():
    path = Path(__file__).resolve().parents[2] / "scripts" / "create_cookies_json.py"
    spec = importlib.util.spec_from_file_location("create_cookies_json", path)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def test_parse_chrome_devtools_tsv_with_size_column():
    script = _load_script()
    text = (
        "Name\tValue\tDomain\tPath\tExpires / Max-Age\tSize\tHttpOnly\tSecure\tSameSite\n"
        "auth_token\tabc123\t.x.com\t/\t2027-01-10T03:31:22.101Z\t42\t✓\t✓\tNone\n"
        "ct0\tcsrf\t.x.com\t/\tSession\t12\t\t✓\tLax\n"
    )

    cookies = script.parse_cookies_from_text(text)
    by_name = {cookie["name"]: cookie for cookie in cookies}

    assert by_name["auth_token"]["httpOnly"] is True
    assert by_name["auth_token"]["secure"] is True
    assert by_name["auth_token"]["sameSite"] == "None"
    assert by_name["ct0"].get("httpOnly") is not True
    assert by_name["ct0"]["secure"] is True
    assert by_name["ct0"]["sameSite"] == "Lax"


def test_browser_client_login_url_detection():
    from nuzzel.browser_twitter_client import BrowserTwitterClient

    client = BrowserTwitterClient(cookies_json="[]")
    assert client._url_looks_like_login("https://x.com/i/flow/login")
    assert client._url_looks_like_login("https://x.com/login")
    assert not client._url_looks_like_login("https://x.com/home")
    assert not client._url_looks_like_login("https://x.com/")
