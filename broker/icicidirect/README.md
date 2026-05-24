# ICICI Direct (Breeze API) plugin — scaffolding

Status: **scaffolding only**. plugin.json + auth stub so the broker shows
in the dashboard's Manage Brokers list. **Trading endpoints raise
NotImplementedError until the follow-up PR ports them.**

## IPv6 status

✅ `api.icicidirect.com` has AAAA records (`2001:df3:140:1::b`, confirmed
2026-05-20). Eligible for the hostingsol SUPPORTED_BROKERS allowlist
as soon as the full trading port lands.

## Port roadmap (follow-up PR)

Source for the full port:

| Function | Reference |
|---|---|
| Auth (Breeze session generation) | `prod-alphaquark-github/aq_backend/Routes/Broker/icici.js` |
| Order / portfolio | `ccxt-india/brokers/icici/icici.py` (1375 LOC — the canonical SDK) |

The full port adds:

- `api/auth_api.py` — generate_session (SHA-256 of api_secret +
  session_token + timestamp), customer-details call, daily refresh
- `api/order_api.py` — place/modify/cancel, order_book, trade_book,
  positions, holdings
- `mapping/transform_data.py` — Breeze enum mapping (product, action,
  ordertype, validity)
- `mapping/order_data.py` — Breeze response normalization
- `database/master_contract_db.py` — daily symbol-master download from
  ICICI's contract master endpoint
- `streaming/icicidirect_adapter.py` — WS tick subscription

Estimated scope: ~3000-4000 lines (Breeze's response shapes have more
edge cases than Arihant's; the existing `icici.py` has 1375 lines just
for the SDK before adapting to the OpenAlgo contract).

## Why scaffolding ships now

1. Customer can save credentials in the dashboard (auth field renders
   from `broker_metadata.py`), proving the wiring is right.
2. The plugin directory + plugin.json reserve the broker name in the
   registry — no risk of a naming collision when the trading port lands.
3. NotImplementedError on trading endpoints is safer than half-built
   code: the operator sees the broker as "scaffolding only" rather than
   risking a real trade through a buggy untested path.
