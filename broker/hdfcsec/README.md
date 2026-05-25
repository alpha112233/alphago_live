# HDFC Securities (InvestRight API) plugin

Full REST trading port. Replaces the 2026-05-20 scaffolding.

## What's implemented

| Surface | Status | Notes |
|---|---|---|
| Auth (OAuth-style daily click) | ✅ | Customer pastes Consumer Key + Consumer Secret once; InvestRight's `?request_token=` callback captures the daily access_token at `/broker/hdfcsec/callback`. |
| Place order (CNC / MIS / NRML / MTF — NSE/BSE/NFO/BFO/CDS/MCX) | ✅ | F&O orders auto-multiply by lot_size from the master contract. F&O contracts pass `underlying_symbol` + `expiry_date` (DDMMYYYY) + `strike_price` + `option_type` as separate fields per InvestRight's spec. |
| Smart order (position-size targeting) | ✅ | Per-symbol lock + 1s position cache, same shape as the icicidirect / definedge plugins. |
| Modify / cancel / cancel-all | ✅ | Cancel is a PUT to `/orders/{id}` with no body. |
| Order book / trade book / order status | ✅ | InvestRight wraps row arrays under `data`; single-order details under `data[0]`. |
| Positions / holdings | ✅ | Positions live under `data.net[]`; we normalize "ALL" exchange to NSE. Holdings derive exchange from the first 3 chars of `instrument_token`. |
| Funds / margin | ✅ | `data.equity.totalAvailableLimitDetails.cash` → availablecash; utilised + collateral surfaced separately. |
| Master contract (NSE/BSE/NFO/BFO/CDS/MCX) | ✅ | Daily download of the security-master CSV from `/oapi/v1/security-master`. F&O rows pack underlying + expiry + strike + right into the OpenAlgo packed symbol. |
| Quotes / depth / historical OHLCV | ⚠️ Stubbed | InvestRight REST does not expose live quotes or OHLCV. Returns 0-filled quote with a `note`. Use a different broker for tick-driven strategies. |
| WS live ticks (NOWStream) | ⚠️ Stubbed | Different host + binary protocol from InvestRight. Tracked as a follow-up; adapter loads cleanly and reports a clear error on `connect`. |
| TOTP seed storage | ✅ | Field exposed in broker_metadata; stored encrypted for future automated daily login. |

## Customer setup

1. Sign in at https://developer.hdfcsec.com → **Apps** → create or
   open an app and copy the **Consumer Key** and **Consumer Secret**.
   Configure the redirect URL to `<host>/broker/hdfcsec/callback`.
2. Paste **Consumer Key** as the API Key and **Consumer Secret** as the
   API Secret in AlphaGo's Manage Brokers screen.
3. (Optional) Save your HDFC Securities mobile app's **TOTP Seed** to
   the same form — stored encrypted for future automated daily login.
4. Click **Connect HDFC Securities** — you'll be redirected to the
   InvestRight login page; after 2FA, InvestRight posts
   `?request_token=<token>` back to AlphaGo, which exchanges it for an
   `accessToken` and stores the auth-string (`access_token:::api_key:::api_secret`).

## Architecture

```
broker/hdfcsec/
├── plugin.json
├── baseurl.py                ← all InvestRight REST URLs
├── api/
│   ├── auth_api.py           ← OAuth-style daily auth + access-token exchange
│   ├── hdfc_http.py          ← shared signed-request helper + retries
│   ├── data.py               ← quote/depth/history stubs (REST has none)
│   ├── funds.py              ← margins → OpenAlgo shape
│   └── order_api.py          ← place / modify / cancel / book / positions / holdings
├── database/
│   └── master_contract_db.py ← security-master CSV → symtoken
├── mapping/
│   ├── symbol_map.py         ← OA↔HDFC symbol pre-processor
│   ├── transform_data.py     ← enum + F&O symbol decode + instrument_segment
│   └── order_data.py         ← InvestRight response → OpenAlgo lists
└── streaming/
    ├── hdfcsec_adapter.py    ← OpenAlgo WS adapter (NOWStream stub)
    └── hdfcsec_mapping.py    ← WS exchange + mode tables
```

## Reference

- Canonical InvestRight SDK: `ccxt-india/brokers/hdfc/hdfcsec.py` (749 LOC)
- Scrip master: `ccxt-india/brokers/hdfc/hdfcsec_scrip_master.py` (254 LOC)
- Prod Node implementation: `aq_backend_github/Routes/Broker/Hdfc.js`

## IPv6 status

✅ `developer.hdfcsec.com` has AAAA via AWS ALB CNAME (confirmed
2026-05-20). After this PR merges and the alphago_live image is
republished, hdfcsec can be added to hostingsol's `SUPPORTED_BROKERS`
allowlist.

## Known follow-ups

- **NOWStream WebSocket port** — InvestRight's real-time tick feed
  runs on a separate host with a binary protocol; the OpenAlgo adapter
  stub is in place so dispatch loads cleanly.
- **Quotes / OHLCV via a different broker's feed** — strategies that
  need real-time ticks should subscribe via Zerodha / Upstox / Dhan
  while routing orders through HDFC.
- **Programmatic TOTP daily autologin** — uses the stored `totp_seed`.
  Same shape as the planned ICICI Direct autologin path.
