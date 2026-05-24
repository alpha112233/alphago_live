# Arihant Capital (TradeBridge L2) plugin

Status: **trading-critical baseline ported** — place/modify/cancel orders,
order book, trade book, holdings, positions, funds. WS streaming,
master-contract upsert, GTT/OCO, and basket margin are deferred to
follow-up PRs.

## Source

This plugin is a port of two existing systems:

| Function | Source |
|---|---|
| Auth flow (login → OTP → refresh) | `prod-alphaquark-github/aq_backend/Routes/Broker/Arihant.js` + `ccxt-india/brokers/arihant/arihant.py` |
| Order / portfolio | `ccxt-india/brokers/arihant/arihant.py` (canonical SDK, ~700 LOC) |
| Enum mapping | `arihant.py:_TX_TYPE / _ORD_TYPE / _PRD_TYPE / _DURATION` |

When updating routes or fixing protocol drift, look at those two files
first — they have been live against Arihant for months.

## IPv6 status (hostingsol context)

As of 2026-05-20, Arihant's API hosts have **no AAAA records**:

- `tradebridge.arihantplus.com` — A only
- `smartapi.arihantplus.com` — A only
- `auth-services.arihantplus.com` — A only

Plugin is design-parity-ready but the broker cannot be enabled in
hostingsol's `SUPPORTED_BROKERS` until Arihant publishes AAAA. The
`scripts/verify_broker_ipv6.py --check-allowlist` script in hostingsol
will catch the DNS change automatically.

## Auth flow

Three pieces, called in order:

1. `POST /auth-services/api/auth/v1/login` — user_id + password →
   returns `{txnId, twoFAType, ...}`. Caller surfaces OTP entry UI.
2. `POST /auth-services/api/auth/v1/verify-otp` — txnId + otp →
   returns `{accessToken, refreshToken, ...}`. Both tokens persisted.
3. `POST /auth-services/api/auth/v1/refresh-token` — refresh_token →
   new accessToken. Called daily by the auto-login scheduler.

`authenticate_broker(code)` (OpenAlgo's standard contract) bypasses the
OAuth shape and uses path 3 — reads `{user_id}:::{refresh_token}` from
`BROKER_API_SECRET` and mints a fresh access token. If no refresh token
is saved yet, returns a clear "complete OTP login first" message
pointing the customer at `/broker/arihant/login`.

The interactive OTP entry blueprint (`/broker/arihant/login`,
`/broker/arihant/verify-otp`) is **not in this PR** — it follows the same
pattern as `blueprints/broker_definedge.py` and lands in a follow-up.

## Order placement

Canonical OpenAlgo order dict → Arihant body via
`mapping/transform_data.transform_data(data, token)`. Arihant requires:

- `excToken`: numeric exchange token (from `database.token_db.get_token`)
- `instrument`: `STK | FUT | OPT` (derived from exchange)
- `lotSize`: 1 for equity, contract lot for FNO (callers pass `data['lot_size']`)
- `X-latitude` / `X-longitude` headers (default Mumbai office coords;
  override via `ARIHANT_LATITUDE` / `ARIHANT_LONGITUDE` env vars)

Response shape:
- Success: `{"infoID": "INFO00...", "data": {"ordId": "..."}}`
- Failure: `{"infoID": "ERR...", "infoMsg": "..."}` or non-2xx HTTP

## What's deferred

| Feature | Why deferred |
|---|---|
| WebSocket tick streaming | Each broker's WS protocol is ~700 LOC; not needed for REST order placement. Customers using strategies that don't need live ticks can trade today. |
| `database/token_db.py` upsert in `master_contract_download` | The Arihant master needs a normalizer (symbol-format alignment with NSE conventions). Easier to land as a focused PR with tests. |
| GTT / OCO | Arihant supports GTT natively but the OpenAlgo GTT model needs adapter work — single-broker context that's safer in its own PR. |
| Basket margin | Same. |
| Options chain | Same. |
| `cancel_all_orders` parallelism | Currently iterates serially. A real-world panic-cancel needs concurrent calls + rate-limit awareness. |

## Operator notes

- Set `ARIHANT_BASE_URL=https://uat-smartapi.arihantplus.com` to point
  at UAT for testing without trading real money. Default is production.
- Geo-headers are required for write-side calls (place/modify/cancel).
  Arihant rejects orders without them. The plugin sends Mumbai office
  defaults; override per-customer via env if needed.
- 401 from any endpoint = access token expired. The OpenAlgo auth
  refresh path picks it up on next request and re-mints via the saved
  refresh token.

## Testing

End-to-end paper-trading checklist (run when DNS publishes AAAA + the
plugin is added to hostingsol `SUPPORTED_BROKERS`):

1. Save credentials via UI (`api_key`, save refresh_token via OTP page).
2. `authenticate_broker(None)` should return `(access_token, None)`.
3. `get_holdings(auth)` should return a list (likely empty).
4. `get_positions(auth)` should return a list.
5. `place_order_api({"symbol": "SBIN", "exchange": "NSE", "action":
   "BUY", "ordertype": "LIMIT", "quantity": 1, "price": 100.00,
   "product": "MIS"}, auth)` — should return `(_, {"status":
   "success", "orderid": ...}, orderid)`.
6. `get_order_book(auth)` should show the order.
7. `cancel_order(orderid, auth)` should succeed.
