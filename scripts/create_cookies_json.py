#!/usr/bin/env python3
"""
Script to create cookies.json from cookies copied from browser dev tools.

Usage:
    python scripts/create_cookies_json.py

The script will prompt you to paste the cookies from the browser dev tools
Application tab. Paste the cookies (tab-separated format) and press Ctrl+D
(or Ctrl+Z on Windows) when done.
"""

import json
import sys
from datetime import datetime
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple


def _is_truthy_flag(value: str) -> bool:
    lowered = value.strip().lower()
    return "✓" in value or "✔" in value or lowered in ("true", "yes", "checked")


def _parse_cookie_flags(columns: List[str]) -> Tuple[bool, bool, Optional[str]]:
    """Parse HttpOnly, Secure, and SameSite from DevTools columns after Path/Expires.

    Chrome copies Size (numeric) before the flags. Older copies may omit Size.
    Empty flag cells mean false, not "missing column".
    """
    cols = [col.strip() for col in columns]
    if cols and cols[0].isdigit():
        cols = cols[1:]

    http_only = _is_truthy_flag(cols[0]) if len(cols) >= 1 else False
    secure = _is_truthy_flag(cols[1]) if len(cols) >= 2 else False
    same_site = None
    if len(cols) >= 3 and cols[2]:
        lowered = cols[2].lower()
        if lowered in ("lax", "strict", "none"):
            same_site = "None" if lowered == "none" else cols[2].capitalize()
    return http_only, secure, same_site


def parse_cookies_from_text(text: str) -> List[Dict[str, Any]]:
    """
    Parse cookies from tab-separated text format (from browser dev tools).

    Expected format (tab-separated):
    Name    Value    Domain    Path    Expires    Size    HttpOnly    Secure    SameSite    ...

    Args:
        text: Tab-separated cookie data from browser dev tools

    Returns:
        List of cookie dictionaries in Playwright format
    """
    cookies = []
    lines = text.strip().split('\n')

    # Skip header line if present
    if lines and ('Name' in lines[0] or 'Cookie' in lines[0]):
        lines = lines[1:]

    for line in lines:
        if not line.strip():
            continue

        parts = line.split('\t')
        if len(parts) < 4:
            # Try splitting by multiple spaces as fallback
            parts = [p for p in line.split('  ') if p.strip()]

        if len(parts) < 4:
            print(f"Warning: Skipping malformed line: {line[:50]}...")
            continue

        name = parts[0].strip()
        value = parts[1].strip()
        domain = parts[2].strip()
        path = parts[3].strip() if len(parts) > 3 else '/'

        # Parse expires date if present
        expires = None
        if len(parts) > 4 and parts[4].strip():
            expires_str = parts[4].strip()
            try:
                # Try parsing ISO format: 2026-01-10T03:31:22.101Z
                if 'T' in expires_str:
                    expires_dt = datetime.fromisoformat(expires_str.replace('Z', '+00:00'))
                    expires = int(expires_dt.timestamp())
                # Try parsing as epoch timestamp
                elif expires_str.isdigit():
                    expires = int(expires_str)
            except (ValueError, AttributeError):
                pass

        # Chrome DevTools columns after Path/Expires: Size, HttpOnly, Secure, SameSite, ...
        http_only, secure, same_site = _parse_cookie_flags(parts[5:])

        cookie: Dict[str, Any] = {
            "name": name,
            "value": value,
            "domain": domain,
            "path": path,
        }

        if expires:
            cookie["expires"] = expires

        if http_only:
            cookie["httpOnly"] = True

        if secure:
            cookie["secure"] = True

        if same_site:
            cookie["sameSite"] = same_site

        cookies.append(cookie)

    return cookies


def main():
    """Main function to create cookies.json file."""
    print("=" * 60)
    print("Twitter Cookies JSON Creator")
    print("=" * 60)
    print()
    print("Paste your cookies from the browser dev tools Application tab.")
    print("(Tab-separated format)")
    print()
    print("Press Ctrl+D (or Ctrl+Z on Windows) when done, or enter a blank line.")
    print()

    # Read cookies from stdin
    lines = []
    try:
        while True:
            line = input()
            if not line.strip():
                break
            lines.append(line)
    except EOFError:
        pass

    if not lines:
        print("Error: No cookies provided.")
        sys.exit(1)

    text = '\n'.join(lines)

    try:
        cookies = parse_cookies_from_text(text)

        if not cookies:
            print("Error: No valid cookies found.")
            sys.exit(1)

        # Create cookies.json in project root
        project_root = Path(__file__).parent.parent
        cookies_file = project_root / "cookies.json"

        # Write cookies as one line so the file can be pasted into GitHub secrets
        with open(cookies_file, 'w', encoding='utf-8') as f:
            json.dump(cookies, f, separators=(',', ':'))

        print(f"\n✓ Successfully created cookies.json with {len(cookies)} cookies")
        print(f"  Location: {cookies_file}")
        print()
        print("Cookies created:")
        for cookie in cookies:
            print(f"  - {cookie['name']} ({cookie['domain']})")
        print()
        print("IMPORTANT: GitHub Actions does not read cookies.json.")
        print("Copy the file contents into the TWITTER_SESSION_COOKIES")
        print("repository secret (Settings → Secrets → Actions).")
        print("Do not wrap the JSON in extra quotes.")

    except Exception as e:
        print(f"\nError: {e}")
        sys.exit(1)


if __name__ == "__main__":
    main()
