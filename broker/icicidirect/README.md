# ICICI Direct (Breeze API) plugin

Full trading port. Replaces the 2026-05-20 scaffolding.

## What's implemented

| Surface | Status | Notes |
|---|---|---|
| Auth (OAuth-style daily click) | ✅ | Customer pastes App Key + Secret Key once; Breeze's `?apisession=...` callback captures the daily session token at `/broker/icicidirect/callback`. |
| Place order (CNC / MIS / NRML, NSE/BSE/NFO/BFO/CDS) | ✅ | MARKET orders auto-convert to IOC-limit (NSE) or DAY-limit (BSE) at LTP±tiered-buffer, mirroring the ccxt-india Breeze SDK. |
| Smart order (position-size targeting) | ✅ | Per-symbol lock + 1s position cache, same shape as the definedge plugin. |
| Modify / cancel / cancel-all | ✅ | |
| Order book / trade book / order status | ✅ | Breeze's `Success`-envelope normalized to OpenAlgo's flat list. |
| Positions / holdings | ✅ | Net qty respects Breeze's `mtf_sell_quantity` field. |
| Funds / margin | ✅ | `unallocated_balance` → availablecash; allocated + blocked → utilised. |
| Quotes (LTP / OHLC / best bid-ask) | ✅ | Single-level depth via /quotes; Breeze has no REST 5-level depth. |
| Historical OHLCV | ✅ | 1m / 5m / 30m / 1d via `/historicalcharts`. |
| GTT (single-leg + OCO three-leg) | ✅ | `/gttorder` and `/gttthreelegorder`. |
| Master contract (NSE/BSE/NFO/BFO/CDS) | ✅ | Daily download of `SecurityMaster.zip` → symtoken refresh. Series-fallback (EQ↔BE↔SM) handled at lookup time via existing `database.token_db`. |
| WS live ticks (LTP / quote / depth-5) | ✅ (via `breeze-connect`) | Adapter loads cleanly even if `breeze-connect` is not installed; surfaces a clear error at `connect` time. |
| TOTP seed storage | ✅ | Field exposed in broker_metadata; stored encrypted. Automated TOTP-driven daily login is a follow-up. |

## Customer setup

1. Log in at https://api.icicidirect.com → **Developer Console** → **Apps** →
   create a new app with the redirect URL `<host>/broker/icicidirect/callback`.
2. Copy the **App Key** and **Secret Key** into AlphaGo's Manage Brokers screen.
3. (Optional) From the ICICI Direct mobile app, save the **TOTP seed** for
   future automated daily login.
4. Click **Connect ICICI Direct** — you'll be redirected to the Breeze login
   page; after 2FA, Breeze posts `?apisession=<session_token>` back to
   AlphaGo, which validates the token against `/customerdetails` and
   stores the full auth-string (`session_token:::app_key:::secret_key`).

## Architecture

```
broker/icicidirect/
├── plugin.json
├── baseurl.py                  ← all Breeze REST URLs
├── api/
│   ├── auth_api.py             ← OAuth-style daily auth + customerdetails validation
│   ├── breeze_http.py          ← shared SHA-256 signing layer + retries
│   ├── data.py                 ← quotes, historical, depth (single-level)
│   ├── funds.py                ← margin/limits → OpenAlgo shape
│   ├── gtt_api.py              ← single-leg + OCO three-leg GTT
│   └── order_api.py            ← place / modify / cancel / book / positions / holdings
├── database/
│   └── master_contract_db.py   ← SecurityMaster.zip → symtoken
├── mapping/
│   ├── symbol_map.py           ← thin OA↔Breeze symbol pre-processor
│   ├── transform_data.py       ← enum + F&O symbol decode
│   └── order_data.py           ← Breeze response → OpenAlgo lists
└── streaming/
    ├── icicidirect_adapter.py  ← OpenAlgo WS adapter (breeze-connect backed)
    └── icicidirect_mapping.py  ← WS exchange + mode tables
```

## Reference

- Canonical Breeze SDK: `ccxt-india/brokers/icici/icici.py` (1375 LOC)
- Scrip master: `ccxt-india/brokers/icici/icici_scrip_master.py` (580 LOC)
- Prod Node implementation: `aq_backend_github/Routes/Broker/icici.js`

The port mirrors the ccxt-india SDK's request signing and MARKET→limit
conversion behavior **byte-for-byte**; any divergence is a bug.

## IPv6 status

✅ `api.icicidirect.com` has AAAA records (`2001:df3:140:1::b`, confirmed
2026-05-20). After this PR merges and the alphago_live image is republished,
icicidirect can be added to hostingsol's `SUPPORTED_BROKERS` allowlist.

## Known follow-ups

- **Programmatic TOTP daily login** — wires the stored `totp_seed` into
  a Selenium-driven login script so customers never click. Tracked
  separately; the field is already stored.
- **5-level depth via WS** — REST `/quotes` exposes only best bid/ask;
  full depth-5 is available on the breeze-connect WebSocket feed.
- **Per-customer 429 backoff coordination** — current retries are
  per-request; for high-frequency strategies we may want a global
  Breeze token-bucket.
