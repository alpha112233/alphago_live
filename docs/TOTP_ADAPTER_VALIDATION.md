# Validating + promoting unverified TOTP login adapters

Status as of 2026-06-07:

| Broker        | State        | Blocker                                    |
|---------------|--------------|--------------------------------------------|
| arihant       | ✅ ADAPTERS  | Done (#84). Hands-free renewal via TOTP wired. |
| icicidirect   | ⏳ _UNVERIFIED | Login form mechanics uncaptured. Needs operator-side validation with a real Breeze account. |
| hdfcsec       | ⏳ _UNVERIFIED | Login form mechanics uncaptured. Needs operator-side validation with a real InvestRight account. (Previously also IPv4-blocked — resolved by Decodo Phase 7.) |

This document is the runbook the operator follows to promote each
broker from `_UNVERIFIED_ADAPTERS` → `ADAPTERS`.

---

## Why these can't be validated without your account

Both ICICI Direct (Breeze) and HDFC InvestRight use OAuth2-style
browser flows on JS-rendered pages. The form action URLs and field
names are decided by the broker's JavaScript at runtime — there's
nothing useful to scrape from the static HTML. Capturing the real
shape requires logging in once with Chrome DevTools open and saving
the network log.

Arihant's flow was simpler (REST API with documented endpoints), which
is why it's promoted already.

---

## Step 0 — Pre-flight (no creds needed)

Run the probe from inside a hostingsol customer container that has
Decodo v4 egress configured:

```bash
docker exec -it openalgo-<sub> /app/.venv/bin/python -m scripts.validate_totp_adapter --broker icicidirect --probe
docker exec -it openalgo-<sub> /app/.venv/bin/python -m scripts.validate_totp_adapter --broker hdfcsec     --probe
```

Expected:
- Each broker's login URL is reachable (HTTP 200) from the container
- `routes via Decodo v4: True` (proves the egress mount is wired)
- For ICICI with a bogus key: response body is `Public Key does not exist.`

If `routes via Decodo v4: False`, the broker host isn't in
`utils/decodo_proxy.py:DEFAULT_V4_HOSTS` — add it.

---

## Step 1 — Capture the real flow (one-time, ~10 min per broker)

Per broker:

### A. Open the OAuth login page

ICICI Direct Breeze:
```
https://api.icicidirect.com/apiuser/login?api_key=<YOUR_REAL_API_KEY>
```

HDFC InvestRight:
```
https://developer.hdfcsec.com/oapi/v1/login?api_key=<YOUR_REAL_API_KEY>
```

(Use your real Breeze App Key / InvestRight Consumer Key — get from
the respective developer console.)

### B. Open Chrome DevTools BEFORE clicking anything

1. F12 → Network tab
2. ✅ Check "Preserve log"
3. ✅ Set filter to "Fetch/XHR"
4. Clear the log

### C. Complete the login manually

1. Enter your User ID + Password → Submit
2. Complete 2FA (TOTP / SMS-OTP / PIN — depends on what you've set up
   in your broker's profile settings)
3. You'll be redirected back with `?apisession=...` (ICICI) or
   `?request_token=...` (HDFC)

### D. Capture what you need from DevTools

For each POST request in the Network log:
- Request URL
- Form data field names (in Payload tab)
- Response shape (in Response tab)

You're looking for THREE URLs and their field names:
1. **LOGIN_POST** — where user_id + password is sent
2. **TWOFA_POST** — where the OTP/TOTP code is sent
3. **The final redirect** that carries `apisession=` (ICICI) or `request_token=` (HDFC)

### E. Note the 2FA shape

What does the OTP step look like?
- A **TOTP input box** (6-digit, customer types from authenticator app) → adapter generates via `pyotp.TOTP(seed).now()`
- An **SMS-OTP wait** (no programmatic generation) → adapter cannot be fully hands-free; would need email/SMS-reading integration
- A **PIN** (static 4-6 digit number) → adapter stores PIN like a password

---

## Step 2 — Edit the adapter (~5 min after capture)

Edit `broker_login_adapters/icicidirect.py` (or `hdfcsec.py`):

1. Replace ASSUMED_LOGIN_PAGE / ASSUMED_LOGIN_POST / ASSUMED_TWOFA_POST
   with the captured URLs.
2. Replace ASSUMED_USERID_FIELD / ASSUMED_PASSWORD_FIELD /
   ASSUMED_TOTP_FIELD with the captured form field names.
3. Remove the hard-coded `return _fail(...)` guard at the top of `login()`.

---

## Step 3 — Capture-mode dry run

Run the harness in `--capture` mode against your real account:

```bash
docker exec -it openalgo-<sub> /app/.venv/bin/python -m scripts.validate_totp_adapter \
    --broker icicidirect --capture
```

You'll be prompted for creds (input hidden). The script drives the
adapter end-to-end and writes a JSON file at
`/tmp/totp-capture-icicidirect-<timestamp>.json` containing one entry
per HTTP call (URL, request body, response head — Authorization +
Cookie headers redacted).

If the adapter succeeds, you'll see `ok: True` and a masked access_token.
If it fails, inspect the capture file to see where the flow diverged
from what you saw in DevTools.

---

## Step 4 — Verify mode

After the adapter works in capture mode, re-run without `--capture`:

```bash
docker exec -it openalgo-<sub> /app/.venv/bin/python -m scripts.validate_totp_adapter --broker icicidirect
```

This is the final "is it working clean?" check.

---

## Step 5 — Promote to ADAPTERS

Edit `broker_login_adapters/__init__.py`:

```python
# Move arihant + icicidirect (newly verified) into ADAPTERS:
from .icicidirect import login as icicidirect_login

ADAPTERS = {
    "upstox": upstox_login,
    # … existing entries
    "arihant": arihant_login,
    "icicidirect": icicidirect_login,   # ← new
}

# And remove from _UNVERIFIED_ADAPTERS:
_UNVERIFIED_ADAPTERS = {
    "hdfcsec": hdfcsec_login,
    # icicidirect: removed
}
```

Commit + ship. The daily auto-login scheduler at 08:00 IST will now
re-mint access tokens for icicidirect customers hands-free, just like
arihant.

---

## What to do if you get blocked

### "I can't find the LOGIN_POST URL in DevTools"

ICICI Direct + HDFC InvestRight may handle the login as a same-document
POST (no separate XHR). Check the **Doc** filter in DevTools too, not
just Fetch/XHR.

### "TOTP isn't an option in my broker settings"

Email broker support and ask: *"Does your developer / API customer
2FA support TOTP via Google Authenticator / Authy? My existing options
are SMS-OTP only."* For ICICI Direct, this is `api.icicidirect.com →
Settings → Two-Factor Authentication`. For HDFC InvestRight, it's
`developer.hdfcsec.com → Profile → Security`.

If TOTP is genuinely not supported (SMS-OTP only):
- The adapter cannot be fully hands-free
- Customer keeps the daily Connect-button click
- File a feature request to the broker

### "Capture file shows the request happens but the adapter still fails"

Common causes:
- Form encoding mismatch: broker expects JSON body but you're sending
  form-encoded (or vice versa). Look at the request `Content-Type`
  header in the capture.
- Missing CSRF token: some flows have a hidden `_csrf` field. Add a
  step to GET the login page and extract the CSRF token before POST.
- Cookies aren't persisting: check that the curl_cffi session re-uses
  cookies across POSTs (it should by default).

### "I want help debugging"

Paste the capture file (with Authorization/Cookie already auto-
redacted by the harness) into the GitHub issue. The unredacted
fields don't contain account secrets, only protocol shape.
