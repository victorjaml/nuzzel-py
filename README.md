# Personal Daily Twitter Digest Service

This service helps you escape the exhausting Twitter feed experience where important posts from followed accounts are often hidden. It:

1. Fetches all tweets from your Twitter followers published in a configured time window (default: 24 hours)
2. Aggregates and deduplicates content (especially shared links/articles)
3. Uses an LLM API to generate intelligent insights, summaries, and themes from the period's tweets
4. Emails a digest at your configured interval

For detailed product requirements, see [docs/prd.md](./docs/prd.md). For technical architecture and design details, see [docs/tech_design.md](./docs/tech_design.md).

---

## Prerequisites

Before setting up the service, ensure you have:

- **Python 3.11+** installed
- A **Twitter account**
- A **Mailjet account** (free tier available) for email delivery
- At least one **LLM API key** (choose from):
  - Google Gemini API (recommended)
  - Groq API (backup option)
  - OpenRouter API (backup option)

---

## Setup Instructions

### Step 1: Clone and Install Dependencies

```bash
# Clone the repository
git clone <repository-url>
cd nuzzel-py

# Create and activate virtual environment
python -m venv .venv
source .venv/bin/activate  # On Windows: .venv\Scripts\activate

# Install dependencies
pip install -r requirements.txt

# Install Playwright browsers (required for browser client)
playwright install chromium --with-deps
```

### Step 2: Choose Your Twitter Data Collection Method

The service supports two methods for collecting Twitter data:

#### Option A: Playwright Browser Client

- **Best for**: Users without paid Twitter API access
- **How it works**: Uses headless browser automation to scrape Twitter's web interface
- **Pros**: No API rate limits, no paid subscription required
- **Cons**: More fragile (can break if Twitter changes UI), requires cookie management

#### Option B: Twitter API (XDK) - Paid Account Required

- **Best for**: Users with paid Twitter API subscriptions
- **How it works**: Uses official Twitter API v2 via XDK (Twitter's Python SDK)
- **Pros**: More reliable, official API, better data quality (includes context annotations)
- **Cons**: Requires paid API subscription. The free tier (100 posts/month) is insufficient for daily digests. You need a paid Twitter API subscription.

The browser client requires Twitter session cookies for authentication.

1. **Open browser DevTools**:
   - Navigate to `https://x.com` while logged in
   - Press `F12` to open DevTools
   - Go to **Application** → **Storage** → **Cookies** → `https://x.com`
   - Select all cookies (Ctrl+A / Cmd+A)
   - Copy (Ctrl+C / Cmd+C) - this copies in tab-separated format

2. **Run the cookie creation script**:
   ```bash
   python scripts/create_cookies_json.py
   ```
   - Paste the copied cookies
   - Press `Enter` twice or `Ctrl+D` (Windows: `Ctrl+Z`) when done
   - This creates `cookies.json` in the project root

3. **Copy `cookies.json` into `TWITTER_SESSION_COOKIES`**:
   - Locally: paste the JSON into `.env` as `TWITTER_SESSION_COOKIES=...` (no extra quotes around the array)
   - GitHub Actions: paste the same JSON into the `TWITTER_SESSION_COOKIES` repository secret. Creating `cookies.json` alone does **not** update CI.

**Note**: Session cookies can be revoked by X well before they expire, especially when GitHub Actions uses them from a new IP. Refresh them if authentication starts failing.

### Step 3: Create a `.env` file in the project root

You will fill in the values based on the keys you obtain in the steps to follow

```bash
# Twitter Client Configuration
# Select from 'browser' or 'xdk' depending what you chose for Step 2 above (Option A or B)
TWITTER_CLIENT_TYPE=browser

# Browser Client Authentication (choose one method)
# Option 1: cookies.json in the project root (local runs)
# Option 2: TWITTER_SESSION_COOKIES env var / GitHub secret (required for CI)
TWITTER_SESSION_COOKIES=[{"name":"auth_token","value":"...","domain":".x.com",...}]

# Used to construct profile/likes URLs in addition to auth if using with password
TWITTER_USERNAME=your_twitter_username
# Optional: Username/password fallback (not recommended, may require 2FA)
# TWITTER_PASSWORD=your_password

# Mailjet (required)
MAILJET_API_KEY=your_mailjet_public_key
MAILJET_API_SECRET=your_mailjet_secret_key

# LLM APIs (at least one required)
GEMINI_API_KEY=your_gemini_key
# GROQ_API_KEY=your_groq_key
# OPEN_ROUTER_API_KEY=your_openrouter_key

# Twitter API Credentials (required for XDK client)
TWITTER_API_KEY=your_api_key
TWITTER_API_SECRET=your_api_secret
TWITTER_ACCESS_TOKEN=your_access_token
TWITTER_ACCESS_TOKEN_SECRET=your_access_token_secret
# Optional - after setting up your Twitter developer account as documented below, you can issue a curl command to fetch your user id and store it here if you'd like. This helps for browser mode since it removes dependency of Twitter API completely
# TWITTER_USER_ID=your_twitter_id

# Email (required)
# Using the same email for both sender and recipient may cause emails to be flagged as spam
RECIPIENT_EMAIL=your-email@example.com
SENDER_EMAIL=noreply@yourdomain.com

# Optional: For debugging
# Run in visual mode
# BROWSER_HEADLESS=false
# Delay between browser actions in ms (defaults to 500 in visual mode, 0 when headless)
# BROWSER_SLOW_MO_MS=0
# Set to true for testing with mock data
# USE_MOCK=false
```

### Step 4: Get API Keys

#### Mailjet API Keys

1. Sign up at [mailjet.com](https://www.mailjet.com) (free tier: 6,000 emails/month)
2. Go to **Account Settings** → **API Keys**
3. Copy your **API Key** and **Secret Key**

#### Google Gemini API Key

1. Go to [Google AI Studio](https://aistudio.google.com)
2. Sign in with your Google account
3. Click **Get API Key** → **Create API Key**
4. Copy the API key

#### Groq API Key (Optional Backup)

1. Sign up at [console.groq.com](https://console.groq.com)
2. Go to **API Keys** section
3. Create a new API key and copy it

#### OpenRouter API Key (Optional Backup)

1. Sign up at [openrouter.ai](https://openrouter.ai)
2. Go to **Keys** section
3. Create a new API key and copy it

#### Twitter API

This is needed regardless of whether you use Option A or B from Step 2 because you'll need to fetch your Twitter user id either way.

1. **Apply for Twitter API Access**:
   - Go to [developer.twitter.com](https://developer.twitter.com)
   - Sign up for a developer account
   - Apply for API access (may require paid subscription)

2. **Create an App**:
   - Go to **Developer Portal** → **Projects & Apps** → **Create App**
   - Note your **API Key** and **API Secret**

3. **Generate Access Tokens**:
   - In your app settings, go to **Keys and Tokens**
   - Generate **Access Token and Secret** (User authentication tokens)
   - Copy all four values:
     - API Key
     - API Secret
     - Access Token
     - Access Token Secret


## Step 3: Configure User Preferences

Edit `nuzzel/constants.py` to customize your digest:

```python
# Interest categories for tweet categorization
INTERESTS = [
    "philosophy",
    "religion",
    "music",
    "artificial intelligence",
    "cultural trends",
    "relationships",
    "art",
    # Add your own categories here
]

# Default time window for digest (in days)
DEFAULT_TIME_WINDOW_DAYS = 1  # Change to 2, 3, etc. for longer windows
```

---

## Step 4: Test Locally

### Test with Mock Data (Recommended First)

```bash
# Set USE_MOCK=true in .env file or explicitly use it when running the app
USE_MOCK=true python -m nuzzel.main
```

This runs the service with mock Twitter data and doesn't send real emails, perfect for testing the pipeline.

### Test with Real Data

```bash
# Set USE_MOCK=false in .env file or explicitly use it when running the app
USE_MOCK=false python -m nuzzel.main
```

**For Browser Client**: If you want to see what the browser is doing, set `BROWSER_HEADLESS=false` as well.

### Visualize digest with fake data

```bash
python tests/templates/visualize_template.py
```

Open generated `email_preview.html` file in browser

---

## Step 5: Deploy to GitHub Actions

### 5.1: Configure GitHub Secrets

1. Go to your GitHub repository
2. Navigate to **Settings** → **Secrets and variables** → **Actions**
3. Click **New repository secret** and add all the keys and values from your `.env` file. Note: don't wrap JSON in extra quotes. Add `[...]` and not `'[...]'`

### 5.2: Configure GitHub Variables

Go to **Settings** → **Secrets and variables** → **Actions** → **Variables** tab:

- `TWITTER_CLIENT_TYPE` = `browser` or `xdk` (default: `browser` if not set)

### 5.3: Configure Schedule

Edit `.github/workflows/daily_digest.yml` to customize when the digest runs:

```yaml
on:
  schedule:
    - cron: '0 13 */2 * *'  # Runs every other day at 1:00 PM UTC (5:00 AM Los Angeles time)
```
**Note**: GitHub Actions uses UTC time. Convert your local time to UTC.

### 5.4: Push and Test

1. **Commit and push** your code:
   ```bash
   git add .
   git commit -m "Configure GitHub Actions"
   git push
   ```

2. **Test the workflow**:
   - Go to **Actions** tab in GitHub
   - Select **Twitter Digest** workflow
   - Click **Run workflow** → **Run workflow** (manual trigger)

3. **Monitor execution**:
   - Click on the workflow run to see logs
   - Check your email for the digest

## License

This project is licensed under the **GNU General Public License v3.0 (GPL-3.0)**.

**Key Points:**
- ✅ **Free and open source** - You can freely use, fork, modify, and contribute to this repository
- ✅ **Copyleft** - Any derivative works must also be open source under GPL-3.0
- ⚠️ **No liability** - The authors assume no liability for any consequences of using this software
- ⚠️ **No warranty** - This software is provided "AS IS" without warranty of any kind

See [LICENSE](./LICENSE) for full terms.
