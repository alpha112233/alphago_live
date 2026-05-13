# blueprints/broker_metadata.py
"""
Per-broker form fields + signup/setup instructions.

Curated 2026-05-13. Only brokers with verified IPv6 (AAAA on their API host)
are listed — others are dropped because we can't whitelist IPv4 per-customer
on tidi. See hostingsol/docs/DEPLOY_READINESS_AUDIT.md for the full audit.

Three tiers of auth shape across the kept brokers:
  - "Fully automatable" — Kotak Neo, Groww, Dhan, AliceBlue, Indmoney: the
    customer pastes credentials once (TOTP seed, API key+secret, or a static
    access token) and our backend logs in daily with no human click.
  - "Daily OAuth click" — Zerodha, Upstox, Fyers, IIFL, Flattrade: one
    browser redirect each morning; access_token rotates EOD.
  - "Paid subscription" — Zerodha (₹500/mo per Kite Connect app), Groww
    (₹499+tax/mo). Customer-paid, not us.

Each setup-step block has been cross-checked against the broker's CURRENT
developer documentation, not memory, in 2026-05-13.
"""

# Field shape: a list of dicts describing form inputs for that broker.
# Each input:
#   name:     the field name expected by /api/broker/credentials/save
#   label:    human-readable label for the form
#   type:     "text" | "password"  (frontend uses this to decide masking)
#   required: bool
#   help:     optional one-liner shown under the input

_TEXT_KEY = {"name": "api_key", "label": "API Key", "type": "text", "required": True}
_TEXT_SECRET = {"name": "api_secret", "label": "API Secret", "type": "password", "required": True}
_CLIENT_CODE = {"name": "client_code", "label": "Client Code / User ID", "type": "text", "required": True}
_TOTP = {
    "name": "totp_seed", "label": "TOTP Seed (base32, optional)", "type": "password", "required": False,
    "help": "Saves your TOTP secret so we can refresh the broker session daily without you logging in. Find it in your broker app's 2FA setup screen.",
}

BROKER_FIELDS: dict[str, list[dict]] = {
    "zerodha": [_TEXT_KEY, _TEXT_SECRET,
        {"name": "client_code", "label": "Zerodha User ID (e.g., ABC123)", "type": "text", "required": False,
         "help": "Required if you provide a TOTP seed — we use it as the login user-id."},
        {"name": "extra.password", "label": "Trading Password", "type": "password", "required": False,
         "help": "Required if you provide a TOTP seed."},
        {"name": "totp_seed", "label": "TOTP Seed (base32, optional)", "type": "password", "required": False,
         "help": "Enable daily auto-login: paste the TOTP secret from kite.zerodha.com → Console → Settings → Account → 2FA. With this + User ID + Password, we log in automatically each morning."}],
    "upstox": [_TEXT_KEY, _TEXT_SECRET,
        {"name": "client_code", "label": "Mobile Number (with country code, e.g. +91...)", "type": "text", "required": False,
         "help": "Required if you provide a TOTP seed."},
        {"name": "extra.password", "label": "Upstox Password", "type": "password", "required": False,
         "help": "Required if you provide a TOTP seed."},
        {"name": "totp_seed", "label": "TOTP Seed (base32, optional)", "type": "password", "required": False,
         "help": "Enables daily auto-login. From Upstox app → Profile → Security → 2FA → 'Can't scan?' shows the seed."}],
    "dhan": [
        {"name": "api_key", "label": "Client ID:::API Key (joined by ':::')",
         "type": "text", "required": True,
         "help": "Combine your Dhan Client ID and API Key with three colons. Example: 1100000123:::abcd1234"},
        _TEXT_SECRET,
        {"name": "extra.pin", "label": "Trading PIN (4-digit web/app PIN)",
         "type": "password", "required": False,
         "help": "Required for daily auto-login. This is the 4-digit PIN you use to log in to web.dhan.co — NOT the API secret."},
        {"name": "totp_seed", "label": "TOTP Seed (base32)",
         "type": "password", "required": False,
         "help": "Required for daily auto-login. From web.dhan.co → My Profile → 2FA Settings → save the seed during setup."},
    ],
    "dhan_sandbox": [
        {"name": "api_key", "label": "Sandbox: Client ID:::API Key", "type": "text", "required": True},
        {"name": "api_secret", "label": "Sandbox API Secret", "type": "password", "required": True},
    ],
    "fyers": [_TEXT_KEY, _TEXT_SECRET,
        {"name": "client_code", "label": "Fyers Client ID (e.g. XK12345)", "type": "text", "required": False,
         "help": "Required if you provide a TOTP seed."},
        {"name": "extra.pin", "label": "Trading PIN (4 digits)", "type": "password", "required": False,
         "help": "Required if you provide a TOTP seed."},
        {"name": "totp_seed", "label": "TOTP Seed (base32, optional)", "type": "password", "required": False,
         "help": "Enables daily auto-login. From Fyers app → Security → 2FA → reveal the secret."}],
    "kotak": [
        _TEXT_KEY,   # UCC
        {"name": "api_secret", "label": "Long-lived Access Token", "type": "password", "required": True,
         "help": "Generated once at the Kotak Neo developer portal — does NOT expire daily."},
        {"name": "client_code", "label": "Mobile Number (with country code)", "type": "text", "required": True},
        {"name": "extra.mpin", "label": "MPIN", "type": "password", "required": True,
         "help": "Your Neo trading MPIN."},
        {"name": "totp_seed", "label": "TOTP Seed (base32)", "type": "password", "required": True,
         "help": "Required — Kotak's daily auth flow uses TOTP, no OAuth redirect."},
    ],
    "iifl": [_TEXT_KEY, _TEXT_SECRET],
    "iiflcapital": [_TEXT_KEY, _TEXT_SECRET],  # no TOTP seed — see iiflcapital instructions block: daemon login isn't possible, browser OAuth only
    "groww": [_TEXT_KEY,
        {"name": "api_secret", "label": "API Secret (approval mode, optional)",
         "type": "password", "required": False,
         "help": "Only needed if you'll paste a fresh approval daily via the Connect button. Daemon auto-login uses the TOTP seed below instead — leave this blank if you want hands-free."},
        {"name": "totp_seed", "label": "TOTP Seed (Base32, required for auto-login)",
         "type": "password", "required": False,
         "help": "From Groww's 'Generate TOTP token' dialog at groww.in/trade-api/api-keys, copy the Base32 secret shown BELOW the QR code — NOT the JWT-style 'TOTP Token' at the top. Required for hands-free daily refresh."}],
    "aliceblue": [
        {"name": "api_key", "label": "App Code", "type": "text", "required": True,
         "help": "Issued in the AliceBlue API portal as 'appcode'."},
        _TEXT_SECRET,
        {"name": "client_code", "label": "AliceBlue Client ID (e.g., AB123456)",
         "type": "text", "required": False,
         "help": "Required for daemon auto-login. Skip if you'll click the daily Connect button manually."},
        {"name": "extra.password", "label": "Trading Password",
         "type": "password", "required": False,
         "help": "The same password you use on ant.aliceblueonline.com. Required for daemon auto-login."},
        _TOTP,
    ],
    "flattrade": [
        {"name": "api_key", "label": "User ID:::API Key (joined by ':::')",
         "type": "text", "required": True,
         "help": "Flattrade requires both your trading user-id and API key joined with three colons."},
        _TEXT_SECRET,
        {"name": "extra.password", "label": "Trading Password",
         "type": "password", "required": False,
         "help": "Same password you use to log in at auth.flattrade.in. Required for daemon auto-login."},
        _TOTP,
    ],
    "indmoney": [
        {"name": "api_secret", "label": "Long-lived Access Token", "type": "password", "required": True,
         "help": "IndMoney issues a static token via their developer portal — no daily refresh."},
    ],
}

# Per-broker setup instructions — markdown rendered in the React frontend.
# {{REDIRECT_URL}} is substituted server-side based on HOST_SERVER + broker name.

BROKER_INSTRUCTIONS: dict[str, str] = {
    "zerodha": """\
### Connect Zerodha (Kite Connect)

**Cost:** ₹500/month per app, paid via your Zerodha account. (₹2,000/month also includes a quote/order subscription.)

1. Log in to **https://kite.zerodha.com** with your trading account.
2. Go to **https://developers.kite.trade** → **My Apps** → **Create new app**.
3. Fill the form:
   - **App name:** anything (e.g., `MyAlgo`).
   - **App type:** **"Connect"**.
   - **Redirect URL:** paste this exact value:
     ```
     {{REDIRECT_URL}}
     ```
   - **Postback URL:** leave blank.
4. After payment, the app page shows your **API Key** and **API Secret**.
5. Paste both into the form here. **Strongly recommended:** also fill in your **Zerodha User ID**, **Trading Password**, and **TOTP Seed** — this enables daily auto-login so you don't have to log in to Kite each morning.
6. Save → Make Active.

ℹ️ **TOTP seed for auto-login.** Zerodha doesn't expose a programmatic OAuth flow, but the daily access_token can be obtained by automating kite.zerodha.com's normal login + 2FA. We do this with `pyotp` + `curl_cffi` (the same pattern alpha_live's `refresh_upstox_token_via_totp.py` uses). Find the TOTP secret at **kite.zerodha.com → Console → Settings → Account → External 2FA** ("Can't scan QR? Reveal secret").

Without the TOTP seed: you'll click **Connect** each morning at ~06:00 IST when the token rotates, and complete the login + 2FA in your browser.

Official docs: https://kite.trade/docs/connect/v3/
""",
    "upstox": """\
### Connect Upstox

**Cost:** Free.

1. Go to **https://account.upstox.com/developer/apps** and sign in.
2. **New App** → choose **Algo Trading** (or Live API).
3. Fill the form:
   - **App name:** anything.
   - **Redirect URI:** paste:
     ```
     {{REDIRECT_URL}}
     ```
   - **Static IPs** (separate dialog on the app's detail page): add your dedicated IPv6 shown at the top of this page. This whitelist gates the **order/quote API** (place, modify, cancel orders); the login flow uses a different IP-reputation check on Upstox's edge.
4. Save the Upstox app. The detail page shows your **API Key** and **API Secret**.
5. Back here, paste them in the form. Also fill in your **Mobile Number** (10-digit Indian number, NO country code — `7349290444`, not `+917349290444`), **Password**, and **TOTP Seed**.
6. Save → Make Active.

⚠️ **Known limitation: Upstox auto-login may fail with "1017072 outdated app" on first deploy.** Upstox's edge gates the login endpoint with Cloudflare bot-management on IP reputation. New hosting deployments often start on IPv6 ranges Cloudflare flags as low-reputation, and the request is rejected with the misleading "outdated app" message before it ever reaches Upstox's actual handler. **Workaround:** click **Connect** instead — that drives the OAuth flow through your own browser (whose IP IS trusted), and the resulting access token is stored exactly as if Auto Login had worked. Auto Login will then handle subsequent days. We're working on a dedicated SSH-relay path so Auto Login works from day one.

ℹ️ **Finding the TOTP seed.** The seed lives at **Upstox app → Profile → Security → 2FA → "Can't scan?"** — copy the base32 string it reveals.

Token expiry is daily at **03:30 IST**.

Official docs: https://upstox.com/developer/api-documentation/open-api
""",
    "dhan": """\
### Connect Dhan

**Cost:** Free.

1. Log in to **https://web.dhan.co**.
2. Open **My Profile → Access DhanHQ APIs** (also called "DhanHQ Trading APIs").
3. Click **Generate API Credentials**. The page shows your **Client ID**, **API Key**, and **API Secret**.
4. Enable **TOTP** on your account if not already (Profile → Security → 2FA). Save the base32 seed shown during setup.
5. Under **API Settings** → **Allowed IPs**, add this customer's whitelisted IP (we'll show it on the next screen). **Mandatory from Jan 2026** for order placement.
6. Paste into the form here:
   - **API Key field:** `<your Client ID>:::<your API Key>` (joined with `:::`).
   - **API Secret:** your API Secret.
   - **TOTP Seed:** the base32 seed from step 4.
7. Save → Make Active.

ℹ️ Token is 24h. With the TOTP seed our backend renews automatically; without it the dashboard will return 401 once a day until you re-enter.

Official docs: https://dhanhq.co/docs/v2/authentication/
""",
    "fyers": """\
### Connect Fyers

**Cost:** Free.

1. Go to **https://myapi.fyers.in** and sign in.
2. **Create App** → **App Type: Web**.
3. Fill the form:
   - **App name:** anything.
   - **Redirect URL:** paste:
     ```
     {{REDIRECT_URL}}
     ```
4. Save. Fyers shows your **App ID** (paste as API Key) and **App Secret**.
5. Paste both. **Strongly recommended:** also fill in your **Fyers Client ID** (e.g. XK12345), **Trading PIN**, and **TOTP Seed** for daily auto-login.
6. Save → Make Active.

ℹ️ **TOTP seed for auto-login.** Fyers' v3 auth flow exposes a programmatic login: POST `/api/v3/send_login_otp` → POST `/api/v3/verify_otp` with `pyotp`-generated 6-digit code → POST `/api/v3/verify_pin` with trading PIN → SHA-256 checksum exchange for access_token. With (client_id + PIN + TOTP seed) we run this daily without your input. Find the TOTP seed at **Fyers app → Profile → Security → 2FA**.

Without TOTP seed: daily browser login required via the OAuth `auth_code` flow.

Official docs: https://myapi.fyers.in/docs/
""",
    "kotak": """\
### Connect Kotak Securities (Neo)

**Cost:** Free.

1. Sign up at the **Kotak Neo Trading API portal** (link from https://www.kotaksecurities.com/trading-tools/kotak-neo-trading-api/).
2. Create a new API session. The portal issues a **long-lived Access Token** that does **not** rotate daily — copy it.
3. Note your **UCC** (consumer code shown in your Neo account) and your **registered mobile number** (with country code, e.g. `+919876543210`).
4. Set up TOTP for your account if not already (Neo app → Security → 2FA). Save the base32 seed.
5. Paste into the form here:
   - **API Key:** your UCC.
   - **Long-lived Access Token:** the token from step 2.
   - **Mobile Number:** with country code.
   - **MPIN:** your Neo trading MPIN.
   - **TOTP Seed:** the base32 seed.
6. Save → Make Active.

✅ Best broker for hands-off automation — no daily OAuth click.

Official docs: https://documenter.getpostman.com/view/21534797/UzBnqmpD
""",
    "iiflcapital": """\
### Connect IIFL Capital

**Cost:** Free.

1. Apply at **https://api.iiflcapital.com** → Developer Portal.
2. After approval, IIFL issues an **App Key** and **App Secret**.
3. In your app settings:
   - **Redirect URL:** paste:
     ```
     {{REDIRECT_URL}}
     ```
   - **Whitelisted IPs:** add the IPv6 shown at the top of this page.
4. Paste both keys → Save → Make Active.

⚠️ **Daily browser login required — no daemon auto-login for IIFL.**

Unlike Zerodha / Upstox / Fyers / Kotak / Dhan, IIFL Capital does not
expose a programmable login API. Their documented flow (official Postman
collection + BridgePy SDK) starts with the browser-issued auth code and
treats user authentication as out-of-band. There is no TOTP-based
headless endpoint we can drive from the daemon.

Practical impact: each morning (or whenever the userSession expires)
you click **Connect** on the dashboard → log in to IIFL in the browser
→ IIFL redirects back to us with the auth code → we exchange it for the
session token → trading works for the rest of the day.

If IIFL ships a programmable login endpoint in future we'll build the
adapter and surface an Auto-login button here.

Official docs: https://api.iiflcapital.com/docs
""",
    "iifl": "REDIRECT_TO_IIFLCAPITAL",  # placeholder — kept for VALID_BROKERS back-compat
    "groww": """\
### Connect Groww

**Cost:** ₹499 + tax/month.

1. Open **https://groww.in/trade-api** and request developer access from your trading account.
2. After approval (1-2 days), open **https://groww.in/trade-api/api-keys** → click **"Generate TOTP token"**.
3. The dialog shows two things — copy the right one:
   - **API Key** — at the top of the dialog. Paste into the *API Key* field.
   - **Base32 TOTP secret** — shown BELOW the QR code, looks like
     `JBSWY3DPEHPK3PXP...`. Paste into the *TOTP Seed* field.
   - ⚠️ DO **NOT** copy the long JWT-style "TOTP Token" shown above the QR — that
     is a single-use 6-digit code in disguise, not a seed. Pasting it will fail
     with "TOTP seed is too short / non-Base32 characters."
4. Save → Make Active.

✅ Daemon auto-login: enabled. We mint a fresh access_token daily via `key_type=totp` against `api.groww.in/v1/token/api/access`. No browser, no daily clicks.
⚠️ Groww enforces a static-IP whitelist per API Key. Your dedicated IPv6 (shown at the top of this page) must be added to the API Key's allow-list when you create it.

Official docs: https://groww.in/trade-api/docs/curl
""",
    "aliceblue": """\
### Connect Alice Blue (Ant)

**Cost:** Free.

1. Sign in to **https://ant.aliceblueonline.com/api** with your AliceBlue trading account.
2. Generate API credentials. The page shows your **App Code** and **API Secret**.
3. Set the **Redirect URL** in the AliceBlue API portal to the exact value shown at the top of this page.
4. Enable TOTP-based 2FA on your AliceBlue account (ANT Web → Settings → Security → External 2FA).
   Save the base32 seed when AliceBlue displays the QR code.
5. Paste all four pieces — App Code, API Secret, Client ID, Trading Password — and the TOTP seed into the form.
6. Save → Make Active.

✅ Daemon auto-login: enabled. We log in daily with no human click.
⚠️ AliceBlue's password is the same one you use on ant.aliceblueonline.com. If you change it there you must update it here too.

Official docs: https://ant.aliceblueonline.com/api (login required)
""",
    "flattrade": """\
### Connect Flattrade

**Cost:** Free.

1. Go to **https://authapi.flattrade.in** and create a developer account if you don't have one.
2. The portal issues a **User ID** (your Flattrade trading user-id) and an **API Key**.
3. Set the **Redirect URL** in your Flattrade developer app to:
   ```
   {{REDIRECT_URL}}
   ```
4. Enable **TOTP-based 2FA** in Flattrade (Settings → Security → TOTP). Save
   the Base32 secret when the QR code is displayed.
5. Paste into the form here:
   - **API Key field:** `<your user-id>:::<your API Key>` (joined with `:::`).
   - **API Secret:** the secret shown by Flattrade.
   - **Trading Password:** the same password you use at auth.flattrade.in.
   - **TOTP Seed:** the Base32 secret from step 4.
6. Save → Make Active.

✅ Daemon auto-login: enabled. We drive `/auth/session` + `/ftauth` + `/trade/apitoken` daily with no human click.

Official docs: https://flattrade.in/  /  https://api.flattrade.in/docs
""",
    "indmoney": """\
### Connect IndMoney

**Cost:** Free.

1. Log in to **https://www.indmoney.com** and request API access from Settings → Developer.
2. The portal issues a **long-lived Access Token** — copy it. No api_key/secret pair.
3. Paste the token into **Long-lived Access Token** → Save → Make Active → click **Auto Login** to activate the session.

✅ Simplest setup — paste once, no daily refresh and no TOTP. Auto Login just re-arms the saved token; you only ever re-paste if you rotate the token on IndMoney's console.

Official docs: link from the IndMoney app's Developer screen (no public docs page yet).
""",
}

# Fallback instructions for brokers that don't have a custom entry yet.
DEFAULT_INSTRUCTIONS = """\
### Connect {{BROKER}}

This broker is supported but doesn't have detailed setup instructions in our
docs yet. General steps:

1. Sign up for the broker's developer / API program.
2. Create a new API app.
3. **Redirect URL** — use:
   ```
   {{REDIRECT_URL}}
   ```
4. Copy the API Key and API Secret (and Client Code / TOTP if asked).
5. Paste below → Save → Make Active.

If you need help, reach out to support — we'll write proper instructions for {{BROKER}}.
"""


def get_fields(broker: str) -> list[dict]:
    """Return the form-field metadata for a broker. Empty list if unknown."""
    return BROKER_FIELDS.get((broker or "").lower(), [])


def get_instructions(broker: str, redirect_url: str = "") -> str:
    """Return rendered markdown instructions for `broker`.

    Substitutes {{REDIRECT_URL}} and {{BROKER}} placeholders. If we don't
    have custom instructions for the broker, falls back to DEFAULT_INSTRUCTIONS.
    """
    broker = (broker or "").lower()
    template = BROKER_INSTRUCTIONS.get(broker, DEFAULT_INSTRUCTIONS)
    # iifl maps to iiflcapital — historic upstream alias.
    if template == "REDIRECT_TO_IIFLCAPITAL":
        template = BROKER_INSTRUCTIONS["iiflcapital"]
    return template.replace("{{REDIRECT_URL}}", redirect_url or "<your-redirect-url>").replace("{{BROKER}}", broker)
