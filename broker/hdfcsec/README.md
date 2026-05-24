# HDFC Securities plugin — scaffolding

Status: **scaffolding only**. plugin.json + auth stub so the broker shows
in the dashboard. Trading endpoints raise NotImplementedError until the
follow-up PR ports them.

## IPv6 status

✅ `developer.hdfcsec.com` has AAAA via AWS ALB CNAME (confirmed
2026-05-20). Eligible for hostingsol SUPPORTED_BROKERS once the full
trading port lands.

## Port roadmap (follow-up PR)

| Function | Reference |
|---|---|
| OAuth flow (auth_code → access_token) | `prod-alphaquark-github/aq_backend/Routes/Broker/Hdfc.js` |
| Order / portfolio | `ccxt-india/brokers/hdfc/hdfcsec.py` (749 LOC) |

The full port adds:

- `api/auth_api.py` — OAuth2 token exchange + daily refresh
- `api/order_api.py` — place/modify/cancel/book/positions/holdings
- `mapping/transform_data.py` — HDFC enum mapping
- `mapping/order_data.py` — HDFC response normalization
- `database/master_contract_db.py` — daily contract master download
- `streaming/hdfcsec_adapter.py` — WS tick subscription

Estimated scope: ~2500-3500 lines. Simpler than ICICI (smaller source),
larger than Arihant (broader product surface — covers MF, derivatives,
deposits etc., though we only need equity + FNO).
