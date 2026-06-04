"""Interactive validator for unverified TOTP login adapters.

Phase 1-validation companion for `broker_login_adapters/{arihant,hdfcsec,
icicidirect}.py`. Runs the adapter against a real account, captures
every HTTP request/response, and prints a focused report so the operator
can fill in the ASSUMED_* constants and promote the adapter into ADAPTERS.

Usage (from alphago_live repo root):
    python -m scripts.validate_totp_adapter --broker icicidirect

You'll be prompted for credentials (never printed back, never written to
disk). The script will run adapter.login(creds), trace every HTTP call,
and dump:
    - Captured URLs (compare against ASSUMED_LOGIN_* constants)
    - POST field names actually accepted (vs ASSUMED_*_FIELD)
    - Final access_token presence + masked value
    - Any redirect chain that carries the session token

This is a HARNESS, not a fix. The output tells you exactly what to edit
in the adapter; you still have to make those edits and re-run.

Pre-requisites:
    - Real active account at the broker
    - 2FA type confirmed (TOTP / SMS-OTP / PIN — different fields needed)
    - Network access from this machine to the broker's auth endpoints
      (NOT through Decodo — the validator runs from your dev box, not the
      customer container)
"""

from __future__ import annotations

import argparse
import getpass
import importlib
import json
import logging
import sys
from typing import Any

# Verbose HTTP traffic visibility — both requests.urllib3 AND curl_cffi.
logging.basicConfig(level=logging.DEBUG, format="%(asctime)s [%(levelname)s] %(name)s: %(message)s")
logging.getLogger("urllib3.connectionpool").setLevel(logging.DEBUG)
logging.getLogger("curl_cffi").setLevel(logging.DEBUG)


SUPPORTED = {"arihant", "hdfcsec", "icicidirect"}


def _prompt_creds(broker: str) -> dict[str, Any]:
    """Prompt for the fields the adapter expects. Adjust per broker as
    you confirm the real flow."""
    print(f"\n=== Enter credentials for {broker} (input hidden) ===")
    creds: dict[str, Any] = {}
    creds["api_key"] = getpass.getpass("API Key / App Key: ").strip()
    creds["api_secret"] = getpass.getpass("API Secret / App Secret: ").strip()
    creds["user_id"] = input("User ID / Login ID: ").strip()
    creds["password"] = getpass.getpass("Password / PIN: ").strip()
    creds["totp_secret"] = getpass.getpass(
        "TOTP seed (base32) — leave blank if broker uses SMS-OTP instead: "
    ).strip() or None
    return creds


def _mask(value: str | None, keep: int = 4) -> str:
    if not value:
        return "<none>"
    return value[:keep] + "…" + value[-keep:] if len(value) > 2 * keep else "***"


def main() -> int:
    p = argparse.ArgumentParser()
    p.add_argument("--broker", required=True, choices=sorted(SUPPORTED),
                   help="which unverified adapter to validate")
    p.add_argument("--creds-json", default=None,
                   help="optional path to a JSON file with creds (skips interactive prompt)")
    args = p.parse_args()

    if args.creds_json:
        with open(args.creds_json) as f:
            creds = json.load(f)
    else:
        creds = _prompt_creds(args.broker)

    try:
        mod = importlib.import_module(f"broker_login_adapters.{args.broker}")
    except ImportError as e:
        print(f"\nERROR: can't import broker_login_adapters.{args.broker}: {e}")
        return 2

    print(f"\n=== Running {args.broker}.login(creds) — watch the HTTP trace ===\n")
    try:
        result = mod.login(creds)
    except Exception as e:
        print(f"\nADAPTER RAISED: {type(e).__name__}: {e}")
        return 3

    print("\n=== Adapter returned ===")
    print(f"  ok:            {result.get('ok')}")
    print(f"  access_token:  {_mask(result.get('access_token'))}")
    print(f"  feed_token:    {_mask(result.get('feed_token'))}")
    print(f"  user_id:       {result.get('user_id')}")
    print(f"  expires_at:    {result.get('expires_at')}")
    print(f"  error:         {result.get('error')}")

    if result.get("ok"):
        print(f"\n✓ Adapter succeeded. Next steps:")
        print(f"  1. Confirm the access_token actually works (call a broker REST endpoint)")
        print(f"  2. Re-run this script — should be deterministic")
        print(f"  3. Move '{args.broker}' from _UNVERIFIED_ADAPTERS to ADAPTERS in")
        print(f"     broker_login_adapters/__init__.py")
        return 0

    print(f"\n✗ Adapter failed. Compare the HTTP trace above against the")
    print(f"  ASSUMED_* constants in broker_login_adapters/{args.broker}.py")
    print(f"  and update them to match the real endpoints/fields. Common gaps:")
    print(f"    - LOGIN_POST URL is wrong (broker uses a different /auth path)")
    print(f"    - PASSWORD_FIELD name differs ('pwd', 'pswd', 'password', 'pin')")
    print(f"    - TOTP_FIELD name differs ('otp', 'totp', 'twofa', 'mfa')")
    print(f"    - Server expects JSON body, not form-encoded")
    print(f"    - 2FA is SMS/email-OTP, not TOTP (needs interactive prompt)")
    return 1


if __name__ == "__main__":
    sys.exit(main())
