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
        {"name": "client_code", "label": "Dhan Client ID",
         "type": "text", "required": True,
         "help": "The 10-12 digit numeric ID you use to log in at web.dhan.co (e.g., 1100000123). Same value Dhan calls 'Client ID' in API Key mode."},
        {"name": "api_key", "label": "API Key",
         "type": "text", "required": True,
         "help": "The hex 'API Key' shown in Dhan's developer console. ⚠️ Make sure the toggle in the top-right is on 'API Key' — Dhan defaults to 'Access Token' mode."},
        _TEXT_SECRET,
        {"name": "extra.pin", "label": "Trading PIN (4-digit web/app PIN)",
         "type": "password", "required": False,
         "help": "Required for daily auto-login. This is the 4-digit PIN you use to log in to web.dhan.co — NOT the API secret."},
        {"name": "totp_seed", "label": "TOTP Seed (base32)",
         "type": "password", "required": False,
         "help": "Required for daily auto-login. From web.dhan.co → My Profile → 2FA Settings → save the seed during setup."},
    ],
    # dhan_sandbox intentionally removed from the customer-facing list 2026-05-25.
    # The plugin code stays in broker/dhan_sandbox/ for internal testing, but
    # it's no longer surfaced in the Add Broker dropdown.
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
    # 'iifl' alias dropped from the dropdown 2026-05-25 — was a back-compat
    # alias for iiflcapital and confused customers (two IIFLs in the picker).
    # The brlogin "iifl" handler still exists for any pre-existing customer
    # records, but new connections route through 'iiflcapital'.
    "iiflcapital": [_TEXT_KEY, _TEXT_SECRET],  # no TOTP seed — see iiflcapital instructions block: daemon login isn't possible, browser OAuth only
    # IIFL XTS (Symphony) — fully headless: appKey/secretKey for BOTH the
    # Interactive (trading) and Market Data apps → no browser, no OTP, true
    # daily auto-login. The 4 fields map to BROKER_API_KEY/SECRET (interactive)
    # and BROKER_API_KEY_MARKET/SECRET_MARKET (market data) via broker_env_bootstrap.
    "iiflxts": [
        {"name": "api_key", "label": "Interactive App Key", "type": "text", "required": True,
         "help": "From your IIFL XTS Interactive (trading) API app."},
        {"name": "api_secret", "label": "Interactive Secret Key", "type": "password", "required": True,
         "help": "Secret key of the IIFL XTS Interactive app."},
        {"name": "api_key_market", "label": "Market Data App Key", "type": "text", "required": True,
         "help": "From your IIFL XTS Market Data API app (a SEPARATE app from Interactive — needed for quotes/streaming)."},
        {"name": "api_secret_market", "label": "Market Data Secret Key", "type": "password", "required": True,
         "help": "Secret key of the IIFL XTS Market Data app."},
        {"name": "base_url", "label": "XTS API Base URL (optional)", "type": "text", "required": False,
         "help": "Only if IIFL gave you an XTS host other than https://ttblaze.iifl.com (e.g. https://blazemum.indiainfoline.com). Paste the exact Interactive/trading API base URL from your IIFL onboarding. Leave blank for the default."},
        {"name": "base_url_market", "label": "XTS Market Data Base URL (optional)", "type": "text", "required": False,
         "help": "Only if the Market Data host differs from the Interactive host above. Leave blank to reuse the Interactive base URL."},
    ],
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
    # DefinEdge INTEGRATE: api_token + api_secret at save; OTP entered each
    # morning via the /broker/definedge/totp page. No TOTP-seed shortcut at
    # the API auth layer today — the OTP is delivered via email/SMS only.
    "definedge": [_TEXT_KEY, _TEXT_SECRET],
    # Angel One (SmartAPI). Authenticates with client_code + PIN + TOTP each
    # morning via the /broker/angel/totp form. TOTP seed stored for hands-free
    # daily auto-login.
    "angel": [_TEXT_KEY,
        {"name": "client_code", "label": "Angel Client Code (e.g., A123456)",
         "type": "text", "required": True,
         "help": "Your Angel One trading client code — the same one you log in to angelone.in with."},
        {"name": "extra.pin", "label": "MPIN (4-6 digits)",
         "type": "password", "required": True,
         "help": "The MPIN you use on the Angel One app or web login."},
        {"name": "totp_seed", "label": "TOTP Seed (base32)",
         "type": "password", "required": True,
         "help": "Required — Angel SmartAPI's daily auth uses TOTP. From Angel app → Profile → My Account → Settings → External 2FA → 'Can't scan QR' reveals the seed."},
    ],
    # 5paisa OpenAPI. Old-style client_code + PIN + TOTP login (same shape as
    # Angel). 5paisa's developer console issues api_key + api_secret which are
    # also required.
    "fivepaisa": [_TEXT_KEY, _TEXT_SECRET,
        {"name": "client_code", "label": "5paisa Client Code",
         "type": "text", "required": True,
         "help": "The numeric client ID printed on your 5paisa contract notes / login screen."},
        {"name": "extra.pin", "label": "Trading PIN",
         "type": "password", "required": True,
         "help": "The 5paisa web/app login PIN."},
        {"name": "totp_seed", "label": "TOTP Seed (base32)",
         "type": "password", "required": True,
         "help": "Required for daily auto-login. From 5paisa.com → Profile → Security → 2FA reveal."},
    ],
    # Paytm Money. OAuth redirect to login.paytmmoney.com/merchant-login —
    # callback returns ?request_token=... which we exchange for an access
    # token. Plugin shipped upstream, server-side TOTP autologin is NOT
    # available (Paytm's OAuth needs the browser); the totp_seed field is
    # reserved for a future automated path.
    "paytm": [_TEXT_KEY, _TEXT_SECRET,
        {"name": "client_code", "label": "Paytm Money Client ID (optional)",
         "type": "text", "required": False,
         "help": "Surfaced to the dashboard as a friendly identifier; not used by the OAuth flow itself."},
    ],
    # Arihant TradeBridge — TRADING-CRITICAL PORTED. Auth is appId + (refresh
    # token saved via one-time OTP login). api_secret format:
    # `{user_id}:::{refresh_token}` (set by the /broker/arihant/login flow).
    # Plugin design-parity-ready but NOT enabled in hostingsol allowlist
    # until Arihant publishes AAAA (no AAAA as of 2026-05-20).
    "arihant": [
        {"name": "api_key", "label": "Arihant API Key",
         "type": "text", "required": True,
         "help": "From your TradeBridge developer portal → My Apps, copy the value in the 'API Key' column (a short string like 'cqfS9tStGb1YClULn8'). Do NOT copy the 'App Id' column (the UUID) — only the API Key is used by the broker plugin."},
        {"name": "api_secret", "label": "Refresh token (auto-set by Connect button — leave blank)",
         "type": "password", "required": False,
         "help": "DON'T enter manually. After you save the API Key and click 'Connect Arihant Capital', a two-step OTP page opens; once you complete it, this field is auto-populated with your user ID + refresh token. The daily 08:00 IST auto-login uses it to mint a fresh access token."},
        # OPTIONAL trio for hands-free 6-monthly refresh-token renewal
        # (see broker_login_adapters/arihant.py). Filling all three lets
        # the daily auto-login adapter re-mint the refresh token via
        # login + TOTP when the previous one expires, without you
        # redoing the OTP page. Leaving any blank reverts to manual.
        {"name": "client_code", "label": "Arihant User ID (optional — for hands-free renewal)",
         "type": "text", "required": False,
         "help": "Your Arihant Client Code / Trading User ID (e.g. '284300014'). Required only if you also fill the Trading Password and TOTP Seed below. Stored in plaintext (it's not secret on its own)."},
        {"name": "api_key_market", "label": "Arihant Trading Password (optional — for hands-free renewal)",
         "type": "password", "required": False,
         "help": "Your Arihant trading password (the one you use at tradebridge.arihantplus.com). Stored Fernet-encrypted at rest using your per-instance API_KEY_PEPPER. Required only with User ID + TOTP Seed for hands-free 6-monthly refresh-token re-mint."},
        {"name": "totp_seed", "label": "Arihant TOTP Seed (optional — for hands-free renewal)",
         "type": "password", "required": False,
         "help": "Base32 TOTP seed from your TradeBridge portal → Setup TOTP. Stored Fernet-encrypted. With this + User ID + Trading Password set, the daily auto-login cron generates the OTP via pyotp at refresh-token re-mint time instead of waiting for SMS, so you never have to redo the OTP page by hand."},
        {"name": "api_secret_market", "label": "Arihant Market Feed API Key (needed for Sandbox / live quotes)",
         "type": "password", "required": False,
         "help": "Arihant gates market data behind a SEPARATE app. In TradeBridge → My Apps → Add App, pick API Type = 'Market Feed APIs', set Primary Static IP to your dedicated IP shown above in 'IP addresses to whitelist', save, then copy that app's API Key here. The Trading API key does NOT work for quotes. Required only if you want Sandbox/Analyze (paper) mode — live order placement needs no quotes."},
    ],
    # ICICI Direct Breeze API — full port (feat/icici-direct-full-port).
    # Customer pastes app_key + secret_key once; daily session_token is
    # captured automatically via Breeze's OAuth redirect to
    # /broker/icicidirect/callback?apisession=...
    "icicidirect": [
        {"name": "api_key", "label": "Breeze App Key",
         "type": "text", "required": True,
         "help": "From api.icicidirect.com → Developer Console → Apps. The 'App Key' (NOT the API Key Secret)."},
        {"name": "api_secret", "label": "Breeze Secret Key",
         "type": "password", "required": True,
         "help": "From the same Developer Console row as the App Key. Used to sign every request — stable across days."},
        {"name": "totp_seed", "label": "TOTP Seed (base32, optional)",
         "type": "password", "required": False,
         "help": "Stored encrypted for future automated daily login. From ICICI Direct mobile app → Profile → Security → Two-Factor Authentication → TOTP setup."},
    ],
    # HDFC Securities (InvestRight API) — full port (feat/hdfcsec-full-port).
    # Customer pastes Consumer Key + Consumer Secret once; the daily
    # access_token is captured automatically via InvestRight's OAuth
    # redirect to /broker/hdfcsec/callback?request_token=...
    "hdfcsec": [
        {"name": "api_key", "label": "InvestRight Consumer Key",
         "type": "text", "required": True,
         "help": "From developer.hdfcsec.com → Apps → your application. Stable across days."},
        {"name": "api_secret", "label": "InvestRight Consumer Secret",
         "type": "password", "required": True,
         "help": "From the same Apps row as the Consumer Key. Used to sign the daily access_token exchange."},
        {"name": "totp_seed", "label": "TOTP Seed (base32, optional)",
         "type": "password", "required": False,
         "help": "Stored encrypted for future automated daily login. From HDFC Securities mobile app → Profile → Security → TOTP."},
    ],
}

# Per-broker setup instructions — markdown rendered in the React frontend.
# {{REDIRECT_URL}} is substituted server-side based on HOST_SERVER + broker name.

BROKER_INSTRUCTIONS: dict[str, str] = {
    "zerodha": """\
**Connect Zerodha (Kite Connect)**

**Cost:** ₹500/month per app, paid via your Zerodha account. (₹2,000/month also includes a quote/order subscription.)

1. Log in to **https://kite.zerodha.com** with your trading account.
2. Go to **https://developers.kite.trade** → **My Apps** → **Create new app**.
3. Fill the form:
   - **App name:** anything (e.g., `MyAlgo`).
   - **App type:** **"Connect"**.
   - **Redirect URL:** paste this exact value:
     {{REDIRECT_URL}}
   - **Postback URL:** leave blank.
4. After payment, the app page shows your **API Key** and **API Secret**.
5. Paste both into the form here. **Strongly recommended:** also fill in your **Zerodha User ID**, **Trading Password**, and **TOTP Seed** — this enables daily auto-login so you don't have to log in to Kite each morning.
6. Save → Make Active.

ℹ️ **TOTP seed for auto-login.** Zerodha doesn't expose a programmatic OAuth flow, but the daily access_token can be obtained by automating kite.zerodha.com's normal login + 2FA. We do this with `pyotp` + `curl_cffi` (the same pattern alpha_live's `refresh_upstox_token_via_totp.py` uses). Find the TOTP secret at **kite.zerodha.com → Console → Settings → Account → External 2FA** ("Can't scan QR? Reveal secret").

Without the TOTP seed: you'll click **Connect** each morning at ~06:00 IST when the token rotates, and complete the login + 2FA in your browser.

Official docs: https://kite.trade/docs/connect/v3/
""",
    "upstox": """\
**Connect Upstox**

**Cost:** Free.

1. Go to **https://account.upstox.com/developer/apps** and sign in.
2. **New App** → choose **Algo Trading** (or Live API).
3. Fill the form:
   - **App name:** anything.
   - **Redirect URI:** paste:
     {{REDIRECT_URL}}
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
**Connect Dhan**

**Cost:** Free.

1. Log in to **https://web.dhan.co**.
2. Open **My Profile → Access DhanHQ APIs** (also called "DhanHQ Trading APIs").
3. **⚠️ Toggle the top-right switch from "Access Token" to "API Key" mode.** Dhan defaults to Access Token (long-lived static token), but for daemon auto-login we need API Key mode. The toggle is on the same row as the **API Secret** column header.
4. In API Key mode, click **Generate** to create credentials if you don't have any yet. The page then shows three pieces:
   - **Client ID** — 10-12 digits, the same number you log into web.dhan.co with.
   - **API Key** — hex string, e.g. `08956b3a`.
   - **API Secret** — UUID-style, e.g. `09bcbeea-fea1-49c7-8ba7-cc947805bf0f`.
5. Enable **TOTP** on your account if not already (Profile → Security → 2FA). Save the base32 seed shown during setup.
6. Under **API Settings → Add IP Setting**, add this customer's whitelisted IPv6 (shown at the top of this page). **Mandatory from Jan 2026** for order placement.
7. Paste into the form here:
   - **Dhan Client ID:** the 10-12 digit number from step 4.
   - **API Key:** the hex string from step 4.
   - **API Secret:** the UUID from step 4.
   - **Trading PIN:** your 4-digit web.dhan.co login PIN.
   - **TOTP Seed:** the base32 seed from step 5.
8. Save → Make Active.

ℹ️ Access token is 24h. With the TOTP seed our backend renews automatically; without it the dashboard will return 401 once a day until you re-enter.

Official docs: https://dhanhq.co/docs/v2/authentication/
""",
    "fyers": """\
**Connect Fyers**

**Cost:** Free.

1. Go to **https://myapi.fyers.in** and sign in.
2. **Create App** → **App Type: Web**.
3. Fill the form:
   - **App name:** anything.
   - **Redirect URL:** paste:
     {{REDIRECT_URL}}
4. Save. Fyers shows your **App ID** (paste as API Key) and **App Secret**.
5. Paste both. **Strongly recommended:** also fill in your **Fyers Client ID** (e.g. XK12345), **Trading PIN**, and **TOTP Seed** for daily auto-login.
6. Save → Make Active.

ℹ️ **TOTP seed for auto-login.** Fyers' v3 auth flow exposes a programmatic login: POST `/api/v3/send_login_otp` → POST `/api/v3/verify_otp` with `pyotp`-generated 6-digit code → POST `/api/v3/verify_pin` with trading PIN → SHA-256 checksum exchange for access_token. With (client_id + PIN + TOTP seed) we run this daily without your input. Find the TOTP seed at **Fyers app → Profile → Security → 2FA**.

Without TOTP seed: daily browser login required via the OAuth `auth_code` flow.

Official docs: https://myapi.fyers.in/docs/
""",
    "kotak": """\
**Connect Kotak Securities (Neo)**

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
**Connect IIFL Capital**

**Cost:** Free.

1. Apply at **https://api.iiflcapital.com** → Developer Portal.
2. After approval, IIFL issues an **App Key** and **App Secret**.
3. In your app settings:
   - **Redirect URL:** paste:
     {{REDIRECT_URL}}
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
    "iiflxts": """\
**Connect IIFL XTS (auto-login)**

**Cost:** IIFL XTS API is a paid dealer/API subscription — confirm pricing with
your IIFL relationship manager.

Unlike **IIFL Capital** (browser login every day), **IIFL XTS** (the Symphony
XTS API on `ttblaze.iifl.com`) logs in **headlessly from your App Key + Secret
Key** — ✅ **no browser, no OTP, true daily auto-login.**

IIFL XTS issues **two separate API apps** — you need the keys from BOTH:

1. Ask IIFL (or the XTS dealer portal) to enable **XTS API** on your account.
   You'll get credentials for two apps:
   - **Interactive API** (placing/managing orders) → an **App Key** + **Secret Key**
   - **Market Data API** (quotes / streaming) → a **separate App Key** + **Secret Key**
2. **Whitelisted IP:** if IIFL asks for an IP to whitelist, use the **dedicated
   IPv4** shown in this broker's panel (IIFL XTS / `ttblaze.iifl.com` is IPv4-only —
   request a dedicated IPv4 from the button if you don't see one yet).
3. Enter all four keys above:
   - Interactive App Key + Interactive Secret Key
   - Market Data App Key + Market Data Secret Key
4. Save → Make Active. We log in automatically each day — **no redirect URL and
   no daily Connect click required.**

**If login says "Data Not found":** that's IIFL rejecting the request. Two
causes — (a) the **dedicated IPv4 isn't whitelisted** with IIFL yet (do that
first), or (b) your keys are registered on a **different XTS host** than the
default `ttblaze.iifl.com` — in that case paste your host into the **XTS API
Base URL** field above (then reconnect).

ℹ️ If IIFL tells you the API `source` for your account is not the default
`WebAPI`, let us know — it's a one-line setting.

Official docs: https://ttblaze.iifl.com/doc/interactive/
""",
    "groww": """\
**Connect Groww**

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
**Connect Alice Blue (Ant)**

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
**Connect Flattrade**

**Cost:** Free.

1. Go to **https://authapi.flattrade.in** and create a developer account if you don't have one.
2. The portal issues a **User ID** (your Flattrade trading user-id) and an **API Key**.
3. Set the **Redirect URL** in your Flattrade developer app to:
   {{REDIRECT_URL}}
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
**Connect IndMoney**

**Cost:** Free.

1. Log in to **https://www.indmoney.com** and request API access from Settings → Developer.
2. The portal issues a **long-lived Access Token** — copy it. No api_key/secret pair.
3. Paste the token into **Long-lived Access Token** → Save → Make Active → click **Auto Login** to activate the session.

✅ Simplest setup — paste once, no daily refresh and no TOTP. Auto Login just re-arms the saved token; you only ever re-paste if you rotate the token on IndMoney's console.

Official docs: link from the IndMoney app's Developer screen (no public docs page yet).
""",
    "definedge": """\
**Connect DefinEdge Securities (INTEGRATE)**

**Cost:** Free. Available to any DefinEdge trading account holder.

1. Log in to **https://signup.definedgesecurities.com/integrate** (the INTEGRATE developer portal) with your DefinEdge trading credentials.
2. Go to **My Apps** → **Create App**. Fill the form (name + description; no redirect URL is needed for this OTP-based flow).
3. Copy the **API Token** (this is your API Key) and **API Secret** shown after app creation. Paste both into the form here.
4. Save → click **Connect**. DefinEdge sends a one-time OTP to your registered email + mobile. Enter it on the next screen.
5. The session is valid until midnight IST. Click **Connect** + enter a fresh OTP each trading day.

ℹ️ **No auto-login today**: DefinEdge's API auth requires an email/SMS OTP each morning — there is no TOTP-seed shortcut at the API layer. Auto-login support is on the roadmap and will be added when DefinEdge exposes a TOTP-based programmatic path.

ℹ️ Native GTT + OCO supported (`/gttplaceorder`, `/ocoplaceorder`).

Official docs: https://signup.definedgesecurities.com/trading-api-docs
""",
    "angel": """\
**Connect Angel One (SmartAPI)**

**Cost:** Free.

1. Log in to **https://smartapi.angelone.in** with your Angel One trading credentials.
2. **My Apps** → **Create New App**. Fill the form:
   - **App Name:** anything (e.g. `MyAlgo`).
   - **App Type:** **"Trading API"**.
   - **Redirect URL:** paste:
     {{REDIRECT_URL}}
3. After creation the app page shows your **API Key**. Copy it.
4. From Angel One mobile app → **Profile → Settings → External 2FA → 2FA Setup**, complete TOTP enrolment if not already done. On the QR screen, tap **"Can't scan? Enter manually"** to reveal the base32 **TOTP Seed**.
5. Paste into the form here:
   - **API Key:** from step 3.
   - **Angel Client Code:** the trading client code shown on top-right of angelone.in after login (format `A123456`).
   - **MPIN:** the 4–6 digit MPIN you use to log in.
   - **TOTP Seed:** the base32 string from step 4.
6. Save → Make Active.

ℹ️ Angel rotates access tokens daily. With the TOTP seed our backend re-mints the token each morning at 08:00 IST. The connect flow is single-step (no browser redirect) — we POST client_code + MPIN + TOTP straight to SmartAPI.

Official docs: https://smartapi.angelone.in/docs
""",
    "icicidirect": """\
**Connect ICICI Direct (Breeze API)**

**Cost:** Free for retail ICICI Direct customers.

1. Sign in at **https://api.icicidirect.com** with your ICICI Direct credentials. The "Developer Console" link appears in the top bar after login.
2. **Developer Console → Apps → Create New App**. Fill the form:
   - **App Name:** anything (e.g. `MyAlgo`).
   - **Redirect URL:** paste:
     {{REDIRECT_URL}}
3. After saving the app, the page shows **App Key** and **Secret Key** (Breeze calls them "API Key" and "API Secret Key"). Copy both.
4. (Optional) From the ICICI Direct mobile app → **Profile → Security → Two-Factor Authentication**, enable TOTP and save the base32 seed.
5. Paste into the form here:
   - **Breeze App Key:** from step 3.
   - **Breeze Secret Key:** from step 3.
   - **TOTP Seed:** optional, from step 4 — stored encrypted for a future automated daily login. Today the daily click is still needed.
6. Save → click **Connect**. We redirect you to Breeze's login page; after you complete 2FA, Breeze sends a `?apisession=...` callback to AlphaGo which validates the session against `/customerdetails` and stores the daily token (valid until ~22:00 IST that night).

ℹ️ Each Breeze app needs **one redirect URL** registered against it. If you change the redirect URL after creating the app you'll need to either edit the app or create a new one — Breeze rejects the OAuth flow if the callback URL doesn't match the app's registered value exactly (including the `/broker/icicidirect/callback` path).

ℹ️ Equity + F&O + GTT all supported via REST. WS live ticks need `breeze-connect` (already bundled).

Official docs: https://api.icicidirect.com/breezeapi/documents/
""",
    "fivepaisa": """\
**Connect 5paisa OpenAPI**

**Cost:** Free for 5paisa account holders.

1. Open **https://www.5paisa.com/developerapi** and sign in with your 5paisa trading credentials.
2. **Create App** → fill the form (any app name; no redirect URL is needed for this PIN-based flow).
3. The app page shows **API Key** (sometimes labelled "User Key") and **API Secret** ("User Secret"). Copy both.
4. From the 5paisa mobile app → **Profile → Security → 2FA**, enable TOTP and save the base32 seed during setup.
5. Paste into the form here:
   - **API Key** + **API Secret:** from step 3.
   - **5paisa Client Code:** the numeric client ID printed on your 5paisa contract notes / login screen.
   - **Trading PIN:** the same PIN you use on 5paisa.com.
   - **TOTP Seed:** base32 from step 4.
6. Save → Make Active.

ℹ️ Token rotates daily. With the TOTP seed we re-login automatically at 08:00 IST each trading day.

Official docs: https://www.5paisa.com/developerapi/overview
""",
    "paytm": """\
**Connect Paytm Money**

**Cost:** Free for Paytm Money customers.

1. Open **https://developer.paytmmoney.com** and sign in with your Paytm Money credentials.
2. **Apps → Create App**. Fill the form:
   - **App Name:** anything.
   - **Redirect URL:** paste:
     {{REDIRECT_URL}}
3. After saving, the app page shows **API Key** and **API Secret**. Copy both.
4. Paste into the form here:
   - **API Key:** from step 3.
   - **API Secret:** from step 3.
   - **Paytm Client ID** (optional): your Paytm Money client number — shown for your reference; not used by the OAuth flow.
5. Save → click **Connect**. We redirect you to `login.paytmmoney.com/merchant-login`; after you authenticate, Paytm sends a `?request_token=...` callback to AlphaGo which exchanges it for a daily access token.

ℹ️ **No daemon auto-login today:** Paytm's flow requires the browser-driven OAuth step every morning. Auto-login support is on the roadmap pending a programmatic path from Paytm.

Official docs: https://developer.paytmmoney.com/docs/
""",
    "arihant": """\
**Connect Arihant Capital**

Free for Arihant trading account holders. Four steps. ~3 minutes.

**1. Get your API Key**
Log in at https://tradebridge.arihantplus.com/ → **Apps** → if you don't
already have one, click **+ New App** (any name works — e.g. `MyAlgo`).
The page lists two columns side by side: **App Id** (a UUID) and
**API Key** (a short string like `cqfS9tStGb1YClULn8`). **Copy the API
Key — not the App Id.** That's the only value the broker plugin needs.

**2. Whitelist your dedicated IPv4 at Arihant**
Arihant's API only accepts traffic from IPs you've pre-authorized.
Whitelist the IPv4 shown above (under "Whitelist this IPv4") in your
Arihant account's allowed-IPs setting. Orders fail with `IP_NOT_ALLOWED`
until this is done.

**3. Paste & Save**
Paste your API Key into the **Arihant API Key** field below. Leave the
**Refresh token** field empty — it's filled in automatically in step 4.
Click **Save**.

**4. Connect (one-time OTP)**
Click **Connect Arihant Capital**. A two-step page opens:
- Enter your **User ID** (your Arihant Client Code, registered email, or
  registered mobile number — any of the three works) and **Trading
  Password**. Arihant sends an OTP to your registered mobile/email.
- Enter the OTP. We complete the login and store your refresh token.

**5. (Optional) Enable hands-free renewal**
By default you'll need to redo step 4 once every ~6 months when Arihant
rotates your refresh token. To skip even that manual step:
- At https://tradebridge.arihantplus.com/ → **Setup TOTP**, enable TOTP
  2FA and copy the base32 seed (and save the QR for your authenticator
  app).
- Back here, fill in the three optional fields below the Refresh token:
  **User ID** (your Arihant Client Code), **Trading Password**, **TOTP
  Seed**. Save.
- From then on, when the stored refresh token expires the daily 08:00
  IST auto-login will use these three values + pyotp to log in and mint
  a fresh refresh token automatically. No more manual OTP.
- All three are Fernet-encrypted at rest with your per-instance pepper.
  Leave any blank → you keep the default manual-every-6-months behavior.

**6. (Optional) Market data — only for Sandbox / live quotes**
Arihant gates market data behind a SEPARATE app — the Trading API Key from
step 1 does NOT return quotes. You only need this if you want Sandbox
(paper) mode or live price/P&L on holdings; live order placement does not
need it.
- At https://tradebridge.arihantplus.com/ → **Apps → + New App**, set
  **API Type = Market Feed APIs**, and set its **Primary Static IP** to the
  same dedicated IPv4 you whitelisted in step 2.
- Copy that app's **API Key** and paste it into the **Arihant Market Feed
  API Key** field below. Save.

You're connected. From the next day onward the 08:00 IST auto-login
mints a fresh access token from your refresh token — no further OTP
needed. If you ever see a "Session Expired" error (Arihant rotates
refresh tokens roughly every 6 months, or when you change your trading
password), just redo step 4.

Official docs: https://tradebridge.arihantplus.com/docs
""",
}

# Fallback instructions for brokers that don't have a custom entry yet.
DEFAULT_INSTRUCTIONS = """\
**Connect {{BROKER}}**

This broker is supported but doesn't have detailed setup instructions in our
docs yet. General steps:

1. Sign up for the broker's developer / API program.
2. Create a new API app.
3. **Redirect URL** — use:
   {{REDIRECT_URL}}
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
