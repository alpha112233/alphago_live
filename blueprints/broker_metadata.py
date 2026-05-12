# blueprints/broker_metadata.py
"""
Per-broker form fields + signup/setup instructions.

Used by the multi-broker management endpoints (broker_credentials.py) to
tell the React frontend which fields each broker needs and to render
inline instructions. Each entry below MUST be kept in sync with the
broker plugin's actual auth requirements in broker/<name>/api/auth_api.py.

This is an alphago_live fork addition (upstream OpenAlgo is single-broker
per instance so it doesn't need this metadata).
"""

# Field shape: a list of dicts describing form inputs for that broker.
# Each input:
#   name:     the field name expected by /api/broker/credentials/save
#   label:    human-readable label for the form
#   type:     "text" | "password"  (frontend uses this to decide masking)
#   required: bool
#   help:     optional one-liner shown under the input
#
# Common fields:
#   api_key, api_secret             — OAuth-style broker registrations
#   api_key_market, api_secret_market — separate market-data app (XTS brokers)
#   client_code                     — client/user code (Angel, Kotak, 5paisa)
#   totp_seed                       — base32 TOTP seed for auto-login

_TEXT_KEY = {"name": "api_key", "label": "API Key", "type": "text", "required": True}
_TEXT_SECRET = {"name": "api_secret", "label": "API Secret", "type": "password", "required": True}
_CLIENT_CODE = {"name": "client_code", "label": "Client Code / User ID", "type": "text", "required": True}
_TOTP = {
    "name": "totp_seed", "label": "TOTP Seed (base32)", "type": "password", "required": False,
    "help": "Optional. Used for daily auto-login. Find it in your broker app's 2FA setup screen.",
}

BROKER_FIELDS: dict[str, list[dict]] = {
    "zerodha": [_TEXT_KEY, _TEXT_SECRET],
    "upstox": [_TEXT_KEY, _TEXT_SECRET, _TOTP],
    "dhan": [
        {"name": "api_key", "label": "Access Token", "type": "password", "required": True,
         "help": "Dhan uses a long-lived access token (no separate secret)."},
    ],
    "dhan_sandbox": [
        {"name": "api_key", "label": "Sandbox Access Token", "type": "password", "required": True},
    ],
    "fyers": [_TEXT_KEY, _TEXT_SECRET],
    "kotak": [
        _TEXT_KEY, _TEXT_SECRET, _CLIENT_CODE,
        {"name": "extra.mpin", "label": "MPIN", "type": "password", "required": True},
    ],
    "icicidirect": [_TEXT_KEY, _TEXT_SECRET],
    "iifl": [_TEXT_KEY, _TEXT_SECRET, _TOTP],
    "flattrade": [
        {"name": "api_key", "label": "Client ID + API Key (format: CLIENT_ID:::API_KEY)",
         "type": "text", "required": True,
         "help": "Flattrade's BROKER_API_KEY combines two values with ':::' separator."},
        _TEXT_SECRET,
    ],
    "shoonya": [_TEXT_KEY, _TEXT_SECRET, _CLIENT_CODE, _TOTP],
    "groww": [_TEXT_KEY, _TOTP],
    "pocketful": [_TEXT_KEY, _TEXT_SECRET],
    "zebu": [_TEXT_KEY, _TEXT_SECRET, _CLIENT_CODE, _TOTP],
    "tradejini": [_TEXT_KEY, _TEXT_SECRET, _CLIENT_CODE, _TOTP],
    "firstock": [_TEXT_KEY, _TEXT_SECRET, _CLIENT_CODE, _TOTP],
    "aliceblue": [_TEXT_KEY, _TEXT_SECRET, _CLIENT_CODE, _TOTP],
    "angel": [_TEXT_KEY, _TEXT_SECRET, _CLIENT_CODE, _TOTP],
    "compositedge": [
        _TEXT_KEY, _TEXT_SECRET,
        {"name": "api_key_market", "label": "Market Data API Key", "type": "text", "required": False},
        {"name": "api_secret_market", "label": "Market Data API Secret", "type": "password", "required": False},
    ],
    "definedge": [_TEXT_KEY, _TEXT_SECRET, _TOTP],
    "indmoney": [_TEXT_KEY, _TEXT_SECRET],
    "ibulls": [_TEXT_KEY, _TEXT_SECRET, _CLIENT_CODE],
    "paytm": [_TEXT_KEY, _TEXT_SECRET],
    "wisdom": [_TEXT_KEY, _TEXT_SECRET, _CLIENT_CODE, _TOTP],
    "fivepaisaxts": [
        {"name": "api_key", "label": "API Key (format: USER_KEY:::USER_ID:::CLIENT_ID)",
         "type": "text", "required": True,
         "help": "5paisa XTS expects three values joined by ':::'."},
        _TEXT_SECRET,
    ],
}

# Per-broker setup instructions — markdown rendered in the React frontend.
# {{REDIRECT_URL}} is substituted server-side based on HOST_SERVER + broker name.

BROKER_INSTRUCTIONS: dict[str, str] = {
    "zerodha": """\
### Connect Zerodha

You need a **Kite Connect** developer subscription. ₹2,000/month + ₹500/app one-time.
(Different from your Kite trading account.)

1. Go to https://kite.trade and sign in.
2. **Create new app** → type **"Connect"**.
3. **Redirect URL** — paste this exact value:
   ```
   {{REDIRECT_URL}}
   ```
4. Submit. Kite shows your **API key** and **API secret**.
5. Paste them in the form here → Save → Make Active → click **Connect** on the dashboard.

*Auto-login is NOT available for Zerodha — Kite Connect requires daily 1-click login at kite.zerodha.com.*
""",
    "upstox": """\
### Connect Upstox

1. Go to https://account.upstox.com/developer/apps
2. **Create new app**.
3. **Redirect URI** — paste:
   ```
   {{REDIRECT_URL}}
   ```
4. After approval, copy the **API Key** and **API Secret**.
5. Optional — **TOTP seed**: find your TOTP secret in Upstox app's 2FA settings
   (Settings → Security → 2FA → "Show secret"). Paste in the TOTP field below to enable auto-login.

*With TOTP seed, daily relogin happens automatically. Without it, you log in manually each morning.*
""",
    "dhan": """\
### Connect Dhan

1. Go to https://web.dhan.co/profile (or https://api.dhan.co)
2. Generate an **Access Token** in the API section.
3. Tokens are **long-lived** (months) — no daily relogin needed.
4. Paste the token in **Access Token** below → Save → Make Active.
""",
    "angel": """\
### Connect Angel One (SmartAPI)

1. Sign up at https://smartapi.angelbroking.com
2. **Create new app** → choose **"Trading API"**.
3. Copy:
   - **API Key**
   - **API Secret**
4. **Client Code** — your normal Angel trading account ID.
5. **TOTP seed** — when you scan the Angel SmartAPI QR for 2FA, the underlying secret
   is what we need. (In Angel's web 2FA setup, click "Can't scan?" to see it as text.)

*With TOTP, auto-login works daily. Without it, manual login each session.*
""",
    "fyers": """\
### Connect Fyers

1. Go to https://myapi.fyers.in
2. **Create app**. Type: **Web app**.
3. **Redirect URL** — paste:
   ```
   {{REDIRECT_URL}}
   ```
4. Save the **APP_ID** (this is the API Key field) and **APP_SECRET**.
5. Paste here → Save → Make Active.
""",
    "groww": """\
### Connect Groww

1. Open Groww app → Profile → API Access → Request developer access.
2. After approval, copy the **API Key**.
3. **TOTP seed** — required for Groww auto-login (Groww has no OAuth, only TOTP).
   Find it in Groww's 2FA setup. Auto-login uses this each day.
""",
}

# Fallback instructions for brokers that don't have a custom entry yet.
DEFAULT_INSTRUCTIONS = """\
### Connect {{BROKER}}

This broker is supported but doesn't have detailed instructions in our docs yet.
General steps:

1. Sign up for the broker's developer / API program.
2. Create a new API app.
3. **Redirect URL** — use:
   ```
   {{REDIRECT_URL}}
   ```
4. Copy the API Key and API Secret (and Client Code / TOTP if asked).
5. Paste below → Save → Make Active.

*If you need help, reach out to support — we'll write proper instructions for {{BROKER}}.*
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
    return template.replace("{{REDIRECT_URL}}", redirect_url or "<your-redirect-url>").replace("{{BROKER}}", broker)
