"""Interactive validator + capture-mode for unverified TOTP login adapters.

Phase 1-validation companion for ``broker_login_adapters/{hdfcsec,
icicidirect}.py``. Runs the adapter against a real account, captures
every HTTP request/response, and writes a JSON file the operator pastes
into the ASSUMED_* constants block.

Three modes:

  python -m scripts.validate_totp_adapter --broker icicidirect --probe
      Pre-flight only: verifies the broker's login page is reachable
      from THIS container (i.e., Decodo egress is wired), and that the
      api_key passes the broker's "Public Key exists?" check. No login
      attempted. Doesn't need user_id/password — only api_key.

  python -m scripts.validate_totp_adapter --broker icicidirect --capture
      Drives the adapter end-to-end with HTTP DEBUG tracing, writes a
      JSON file with one entry per request (URL, method, request headers,
      request body, response status, response headers, response body
      head). The operator inspects this file to identify the real
      endpoints + field names, then edits the adapter.

  python -m scripts.validate_totp_adapter --broker icicidirect
      Verify mode (final): runs the adapter, prints the login result
      (access_token masked). Run AFTER editing ASSUMED_* constants.

Usage (from alphago_live repo root, inside the container):
    python -m scripts.validate_totp_adapter --broker icicidirect --probe
    python -m scripts.validate_totp_adapter --broker icicidirect --capture

You'll be prompted for credentials (never printed back, never written
to disk except as captures with auth headers redacted). Output capture
file lands at /tmp/totp-capture-<broker>-<timestamp>.json.

Pre-requisites:
    - Real active account at the broker
    - 2FA type confirmed (TOTP / SMS-OTP / PIN — different fields needed)
    - For v4-only brokers (hdfcsec): Decodo v4 egress wired — verify
      with --probe first. ICICI Direct egresses via the customer's
      dedicated /128 (api.icicidirect.com has AAAA records), so no v4
      egress prerequisite for that broker.
"""

from __future__ import annotations

import argparse
import getpass
import importlib
import json
import logging
import sys
import time
from typing import Any
from urllib.parse import urlparse

logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)s] %(name)s: %(message)s")

SUPPORTED = {"hdfcsec", "icicidirect"}


# ---------------------------------------------------------------------------
# Probe mode — pre-flight checks without account creds
# ---------------------------------------------------------------------------

PROBE_TARGETS = {
    "icicidirect": [
        ("login_page_with_bogus_key",
         "https://api.icicidirect.com/apiuser/login?api_key=BOGUS",
         "Public Key does not exist"),
        ("login_page_real_key",
         "https://api.icicidirect.com/apiuser/login?api_key={api_key}",
         None),
    ],
    "hdfcsec": [
        ("oauth_login_page",
         "https://developer.hdfcsec.com/oapi/v1/login?api_key={api_key}",
         None),
    ],
}


def run_probe(broker: str, api_key: str | None) -> int:
    """Reachability + signature probe. No login attempted."""
    import httpx
    print(f"\n=== Probe mode: {broker} ===\n")
    for label, url_tpl, expect in PROBE_TARGETS[broker]:
        url = url_tpl.format(api_key=api_key or "BOGUS")
        print(f"→ {label}")
        print(f"  URL: {url}")
        try:
            r = httpx.get(url, timeout=15, follow_redirects=False)
        except Exception as e:
            print(f"  ✗ ERR: {type(e).__name__}: {e}")
            print(f"    Likely cause: Decodo egress not configured, or host unreachable.")
            return 2
        host_in_url = urlparse(url).hostname
        print(f"  status: {r.status_code}  body_len: {len(r.text)}")
        body_head = r.text[:160].replace("\n", " ")
        print(f"  body head: {body_head!r}")
        if expect and expect in r.text:
            print(f"  ✓ matched expected substring: {expect!r}")
        elif expect:
            print(f"  ✗ did NOT match expected substring: {expect!r}")
        try:
            from utils.decodo_proxy import needs_v4_proxy
            v4 = needs_v4_proxy(host_in_url)
            print(f"  routes via Decodo v4: {v4}")
        except Exception:
            pass
        print()
    print("Probe complete. If reachability looks good, proceed with --capture.")
    return 0


# ---------------------------------------------------------------------------
# Capture mode — full login with request/response trace to a JSON file
# ---------------------------------------------------------------------------

def run_capture(broker: str, creds: dict) -> int:
    """Drive the adapter and record every HTTP call."""
    captures: list[dict] = []
    import httpx

    _orig_send = httpx.Client.send

    def _wrapped_send(self, request, *args, **kwargs):
        req_body_preview = ""
        try:
            req_body_preview = (request.content or b"").decode("utf-8", "replace")[:1200]
        except Exception:
            pass
        cap = {
            "method": request.method,
            "url": str(request.url),
            "request_headers": _redact_headers(dict(request.headers)),
            "request_body": req_body_preview,
            "started_at": time.time(),
        }
        try:
            resp = _orig_send(self, request, *args, **kwargs)
            cap.update({
                "status": resp.status_code,
                "response_headers": _redact_headers(dict(resp.headers)),
                "response_body": (resp.text or "")[:2000],
                "ms": int((time.time() - cap["started_at"]) * 1000),
            })
        except Exception as e:
            cap.update({"error": f"{type(e).__name__}: {e}",
                        "ms": int((time.time() - cap["started_at"]) * 1000)})
            captures.append(cap)
            raise
        captures.append(cap)
        return resp

    httpx.Client.send = _wrapped_send  # type: ignore[assignment]

    try:
        mod = importlib.import_module(f"broker_login_adapters.{broker}")
    except ImportError as e:
        print(f"\nERROR: can't import broker_login_adapters.{broker}: {e}")
        return 2

    print(f"\n=== Running {broker}.login(creds) with capture ===\n")
    result = mod.login(creds)
    print("\n=== Adapter returned ===")
    print(f"  ok:            {result.get('ok')}")
    print(f"  access_token:  {_mask(result.get('access_token'))}")
    print(f"  error:         {result.get('error')}")

    ts = time.strftime("%Y%m%dT%H%M%SZ", time.gmtime())
    outfile = f"/tmp/totp-capture-{broker}-{ts}.json"
    with open(outfile, "w") as f:
        json.dump({
            "broker": broker,
            "captured_at": ts,
            "result": {**result, "access_token": _mask(result.get("access_token"))},
            "captures": captures,
        }, f, indent=2, default=str)
    print(f"\n✓ Wrote {len(captures)} HTTP calls to {outfile}")
    print("\nNext steps:")
    print(f"  1. cat {outfile}")
    print(f"  2. Identify the real LOGIN_PAGE / LOGIN_POST / TWOFA_POST URLs + field names")
    print(f"  3. Edit broker_login_adapters/{broker}.py — replace ASSUMED_* constants")
    print(f"  4. Re-run without --capture to confirm: python -m scripts.validate_totp_adapter --broker {broker}")
    return 0 if result.get("ok") else 1


def _redact_headers(headers: dict) -> dict:
    out = {}
    for k, v in headers.items():
        if k.lower() in ("authorization", "cookie", "set-cookie"):
            out[k] = "<redacted>"
        else:
            out[k] = v
    return out


def _mask(value: str | None, keep: int = 4) -> str:
    if not value:
        return "<none>"
    return value[:keep] + "…" + value[-keep:] if len(value) > 2 * keep else "***"


# ---------------------------------------------------------------------------
# Interactive cred prompt
# ---------------------------------------------------------------------------

def _prompt_creds(broker: str) -> dict[str, Any]:
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


def main() -> int:
    p = argparse.ArgumentParser()
    p.add_argument("--broker", required=True, choices=sorted(SUPPORTED))
    p.add_argument("--probe", action="store_true",
                   help="Pre-flight reachability only — no login attempted")
    p.add_argument("--capture", action="store_true",
                   help="Run login with full HTTP trace → JSON file")
    p.add_argument("--creds-json", default=None,
                   help="Optional path to a JSON file with creds (skips prompt)")
    args = p.parse_args()

    if args.probe:
        api_key = None
        if args.creds_json:
            with open(args.creds_json) as f:
                api_key = json.load(f).get("api_key")
        else:
            v = input("API Key (blank for unauth probe only): ").strip()
            api_key = v or None
        return run_probe(args.broker, api_key)

    if args.creds_json:
        with open(args.creds_json) as f:
            creds = json.load(f)
    else:
        creds = _prompt_creds(args.broker)

    if args.capture:
        return run_capture(args.broker, creds)

    try:
        mod = importlib.import_module(f"broker_login_adapters.{args.broker}")
    except ImportError as e:
        print(f"ERROR: can't import broker_login_adapters.{args.broker}: {e}")
        return 2
    print(f"\n=== Running {args.broker}.login(creds) ===\n")
    result = mod.login(creds)
    print(f"  ok:           {result.get('ok')}")
    print(f"  access_token: {_mask(result.get('access_token'))}")
    print(f"  error:        {result.get('error')}")
    if result.get("ok"):
        print(f"\n✓ Success. Move '{args.broker}' from _UNVERIFIED_ADAPTERS → ADAPTERS in")
        print(f"  broker_login_adapters/__init__.py")
        return 0
    print(f"\n✗ Failed. Re-run with --capture to dump HTTP trace.")
    return 1


if __name__ == "__main__":
    sys.exit(main())
